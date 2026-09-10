"""The local clip cache: what has been downloaded, what serves it, what evicts it.

Feature-video media is hosted, not bundled (see
:mod:`kiro_crew.feature_videos_manifest`), so between "a clip exists" and "a clip
can play" sits a transfer. This module owns that middle: a background task that
walks the verified manifest one entry at a time, the read-only route that serves
what landed, and the eviction that keeps the cache inside its budget.

Four decisions worth stating, because each is the answer to a way this could go
wrong:

**One clip at a time, paced.** The task runs at gateway boot, when the user is
doing something else. A parallel fetch of a dozen clips would take the link they
are working over for a feature nobody asked for yet, so the transfer is
serialized and rate-limited (:data:`DEFAULT_RATE_LIMIT_BYTES_PER_S`). The limit
is lifted for ``POST /api/feature-videos/fetch-all``, where the user asked and is
waiting.

**Resumable across restarts, and idempotent.** A partial transfer stays in a
``.part`` staging file under a stable name, so the next boot continues it instead
of starting over — and a clip already on disk is skipped without a request.
Nothing here is a one-shot: running it twice is the normal case.

**The running release is never evicted.** Eviction frees space by removing whole
release folders, oldest first, and refuses to remove the one this build plays
from. Freeing the release currently in use would delete a clip the dashboard is
about to fetch, and the next boot would download it again — a cache that evicts
its own working set is worse than a full one.

**The serving route derives every path component itself.** It takes a release and
a basename out of the URL, validates both against the same rules the manifest
applies, and then re-checks that the resolved file is inside the release folder.
Not a directory index, not ``add_static``, and never a path that came from the
request unchecked: this route reads from the user's data home, where a traversal
would be a file-disclosure bug rather than a 404.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import shutil
import stat
import threading
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web

import kiro_crew
from kiro_crew import asset_downloader
from kiro_crew import feature_videos_manifest as manifest_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.feature_videos_manifest import ManifestEntry, VideoManifest

logger = logging.getLogger(__name__)

#: Background pacing for the boot-time transfer, in bytes per second. 512 KiB/s
#: fills a 20-second clip in a few seconds while leaving a slow link usable —
#: this runs unasked, so it yields to whatever the user is doing.
DEFAULT_RATE_LIMIT_BYTES_PER_S = 512 * 1024

#: URL prefix the cache is served under. A sibling of ``/app-assets`` rather than
#: a child: those are files inside the wheel, these are files in the user's data
#: home, and one route must never be able to reach the other's tree.
SERVE_PREFIX = "/feature-videos/"

#: Ceiling for a poster transfer. The manifest declares an exact ``bytes`` for the
#: clip but not for the poster, and bytes are written to the staging file as they
#: arrive — so without a bound an endless poster body fills the disk before the
#: end-of-stream digest can reject it, which would break this module's own claim
#: that a tampered object can only fail verification. 8 MiB is generous for a
#: single still frame and small enough that reaching it is a fault, not a big poster.
MAX_POSTER_BYTES = 8 * 1024 * 1024

#: Escape hatch for tests and CI, mirroring ``KIROCREW_SKIP_MODEL_DOWNLOAD``: a
#: test run must never pull media over the network.
SKIP_DOWNLOAD_ENV = "KIROCREW_SKIP_FEATURE_VIDEO_DOWNLOAD"

#: Download-state values reported by ``/api/feature-videos/status`` as
#: ``download_state``. ``denied`` is distinct from ``failed`` on purpose: an
#: operator reading a fleet needs to see "the ceiling forbids this" rather than a
#: transfer error that will never resolve.
STATE_IDLE = "idle"
STATE_FETCHING_MANIFEST = "fetching_manifest"
STATE_DOWNLOADING = "downloading"
STATE_READY = "ready"
STATE_FAILED = "failed"
STATE_DENIED = "denied"
STATE_DISABLED = "disabled"


@dataclass(frozen=True)
class CachedAsset:
    """A resolved local file for one manifest entry, and the path that serves it."""

    path: Path
    url_path: str


def _dashboard_config() -> object:
    """The loaded dashboard config section. Blocking."""
    return KiroCrewConfig.load().dashboard


def is_cached(entry: ManifestEntry, release: str) -> bool:
    """Whether both media files for *entry* are present and the clip's size matches.

    Size, not a re-hash: the sha256 was verified before the file was installed,
    and re-hashing every clip on every ``/next`` request would put a disk read
    proportional to the library on a polled route. What the size check catches is
    a truncated file — and it cannot be one we installed, only one a later disk
    problem produced.
    """
    try:
        folder = manifest_mod.release_dir(release)
    except ValueError:
        return False
    clip = folder / entry.file
    poster = folder / entry.poster
    try:
        return clip.is_file() and clip.stat().st_size == entry.bytes and poster.is_file()
    except OSError:
        return False


def cached_asset(entry: ManifestEntry, release: str) -> "CachedAsset | None":
    """The local clip for *entry*, or None when it is not cached."""
    if not is_cached(entry, release):
        return None
    return CachedAsset(
        path=manifest_mod.release_dir(release) / entry.file,
        url_path=f"{SERVE_PREFIX}{release}/{entry.file}",
    )


def poster_url_path(entry: ManifestEntry, release: str) -> str:
    """Served path for *entry*'s poster in *release*."""
    return f"{SERVE_PREFIX}{release}/{entry.poster}"


# ── Eviction ──


def _dir_size(path: Path) -> int:
    """Total bytes under *path*, ignoring anything unreadable."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def release_folders() -> list[tuple[str, Path, float, int]]:
    """Every release folder in the cache as ``(release, path, mtime, bytes)``."""
    out: list[tuple[str, Path, float, int]] = []
    try:
        entries = list(os.scandir(manifest_mod.cache_root()))
    except OSError:
        return out
    for item in entries:
        if not item.is_dir():
            continue
        try:
            folder = manifest_mod.release_dir(item.name)
        except ValueError:
            # Not a release folder name, so never created by us — and therefore
            # never removed by us. Deleting an unrecognized directory in the
            # user's data home is not this function's call to make.
            continue
        try:
            mtime = item.stat().st_mtime
        except OSError:
            mtime = 0.0
        out.append((item.name, folder, mtime, _dir_size(folder)))
    return out


# 8 PiB. Larger than any disk this cache runs on, so it never binds a real
# budget, and small enough that the byte count stays an exact int.
MAX_CACHE_BYTES = 1 << 53
_MAX_CACHE_MB = MAX_CACHE_BYTES / (1024 * 1024)


def cache_ceiling_bytes(max_mb: float) -> int:
    """Configured megabytes as a byte count. Never raises.

    The comparison happens BEFORE the multiply, which is the whole point: a
    configured 1e308 overflows ``max_mb * 1024 * 1024`` to ``inf`` on its own,
    and ``int(inf)`` raises ``OverflowError``. Clamping after the multiply would
    already be too late.

    A value that is not a finite positive number reads as "no size cap", the same
    answer a missing key gives, because a cap nobody can act on must not be the
    reason a cache starts deleting clips.
    """
    if not math.isfinite(max_mb) or max_mb <= 0:
        return 0
    if max_mb >= _MAX_CACHE_MB:
        return MAX_CACHE_BYTES
    return int(max_mb * 1024 * 1024)


def evict(keep_release: str, *, max_bytes: int, keep_releases: int) -> list[str]:
    """Trim the cache to its budget. Returns the releases removed. Blocking.

    Two independent bounds, applied in this order:

    * *keep_releases* (0 = no bound) keeps that many newest release folders,
      which is the knob for an operator who wants "just the current one";
    * *max_bytes* (0 = no bound) is the size cap, satisfied by removing whole
      release folders oldest-first until the total fits.

    *keep_release* survives both bounds, even when it alone exceeds the cap: a
    cap is not a reason to delete the clips this build plays. A single release
    over budget stays visible in the status endpoint's counts instead.
    """
    removed: list[str] = []
    folders = sorted(release_folders(), key=lambda row: row[2])  # oldest first
    evictable = [row for row in folders if row[0] != keep_release]

    if keep_releases > 0:
        # The running release is kept regardless, so it does not consume one of
        # the slots: keep_releases=1 means "the running one and nothing else".
        surplus = max(0, len(evictable) - max(0, keep_releases - 1))
        for release, path, _mtime, _size in evictable[:surplus]:
            if _remove_release(release, path):
                removed.append(release)
        evictable = [row for row in evictable if row[0] not in removed]

    if max_bytes <= 0:
        return removed
    total = sum(row[3] for row in folders if row[0] not in removed)
    for release, path, _mtime, size in evictable:
        if total <= max_bytes:
            break
        if _remove_release(release, path):
            removed.append(release)
            total -= size
    return removed


def _remove_release(release: str, path: Path) -> bool:
    """Delete one release folder. Never raises."""
    try:
        shutil.rmtree(path)
    except OSError:
        logger.warning("could not evict cached feature videos for %s", release, exc_info=True)
        return False
    logger.info("evicted cached feature videos for release %s", release)
    return True


# ── The cache manager ──


class FeatureVideoCache:
    """Process-wide state for the hosted-clip cache.

    Holds the in-memory manifest and the download status. Deliberately NOT a
    per-request object: the manifest is instance-wide, and re-reading (let alone
    re-fetching) it per request would put a disk read or a network call on a
    polled route.
    """

    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None  # created lazily inside the loop
        self._manifest: VideoManifest | None = None
        # A threading lock, not an asyncio one: the manifest is read from
        # executor THREADS, and a module-level asyncio primitive binds to
        # whichever loop first awaited it.
        self._manifest_lock = threading.Lock()
        self.status: dict[str, object] = {
            "download_state": STATE_IDLE,
            "downloading": None,
            "error": "",
        }

    # ── manifest ──

    @property
    def manifest(self) -> "VideoManifest | None":
        """The manifest in memory, without touching disk or network."""
        return self._manifest

    def current_manifest(self) -> "VideoManifest | None":
        """The manifest for this build, from memory or the on-disk cache. Blocking.

        Never makes a network request: the request paths (``/next``, ``/status``,
        ``/probe``) must not depend on the CDN being reachable, and the only
        component that fetches is the background task below.
        """
        with self._manifest_lock:
            if self._manifest is None:
                self._manifest = manifest_mod.load_cached_manifest(kiro_crew.__version__)
            return self._manifest

    def refresh_manifest(self) -> "VideoManifest | None":
        """Fetch, verify and cache the manifest. Blocking; makes a request.

        Network first, on-disk cache as the fallback: a build that can reach the
        CDN should pick up clips published after it shipped, and one that cannot
        must still play what it already holds.

        The caller MUST have established the ceiling permits downloading.
        """
        configured = str(getattr(_dashboard_config(), "feature_videos_manifest_url", "") or "")
        fetched, raw = manifest_mod.fetch_manifest(kiro_crew.__version__, configured)
        if fetched is None:
            return self.current_manifest()
        manifest_mod.store_manifest(fetched, raw)
        with self._manifest_lock:
            self._manifest = fetched
        return fetched

    # ── counts ──

    def counts(self) -> tuple[int, int]:
        """``(cached, total)`` for the current manifest. Blocking (stats files)."""
        current = self.current_manifest()
        if current is None:
            return 0, 0
        cached = sum(1 for entry in current.entries if is_cached(entry, current.release))
        return cached, len(current.entries)

    # ── the background transfer ──

    async def ensure_all(self, *, unlimited: bool = False) -> bool:
        """Download every manifest entry not cached yet. Returns "everything cached".

        Serialized by an asyncio lock, so the boot task and a ``fetch-all`` click
        share one in-flight pass rather than racing two writers into the same
        staging files.

        *unlimited* lifts the rate limit, for the interactive path only.
        """
        if os.environ.get(SKIP_DOWNLOAD_ENV) == "1":
            return False
        dashboard = await asyncio.to_thread(_dashboard_config)
        if not bool(getattr(dashboard, "feature_videos_enabled", False)):
            self._set_state(STATE_DISABLED)
            return False
        if await asyncio.to_thread(manifest_mod.download_denied):
            # No manifest request and no transfer. Already-cached clips stay
            # playable: withdrawing what is on disk would be a second, separate
            # decision that the ceiling did not make.
            self._set_state(STATE_DENIED)
            return False
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            self._set_state(STATE_FETCHING_MANIFEST)
            current = await asyncio.to_thread(self.refresh_manifest)
            if current is None:
                self._set_state(STATE_FAILED, error="no verified manifest available")
                return False
            await asyncio.to_thread(self._evict_now, current.release, dashboard)
            pending = await asyncio.to_thread(self._pending_entries, current)
            if not pending:
                self._set_state(STATE_READY)
                return True
            rate = 0 if unlimited else DEFAULT_RATE_LIMIT_BYTES_PER_S
            last_error = ""
            for entry in pending:
                # Re-evaluated per entry, not once before the loop. A paced pass
                # runs for minutes, so an administrator tightening the ceiling
                # mid-pass would otherwise keep every remaining clip downloading
                # under a permission the ceiling has withdrawn. This is an action
                # chokepoint, so it takes the audited answer rather than the memo.
                if await asyncio.to_thread(manifest_mod.download_denied):
                    logger.info("feature-video download stopped: the ceiling now denies it")
                    self._set_state(STATE_DENIED)
                    return False
                self._set_state(STATE_DOWNLOADING, downloading=entry.id)
                ok, err = await asyncio.to_thread(self._download_entry, entry, current, rate)
                if not ok:
                    last_error = err
                    logger.info("feature video %r not cached: %s", entry.id, err)
            cached, total = await asyncio.to_thread(self.counts)
            if cached == total:
                self._set_state(STATE_READY)
                return True
            # A failed pass is retried by the next gateway boot or a fetch-all
            # click, NOT by a retry loop here: a background task that kept
            # retrying on a host with no egress would spin for the process's
            # lifetime for a feature nobody is waiting on.
            self._set_state(STATE_FAILED, error=last_error or "download incomplete")
            return False

    def _pending_entries(self, current: VideoManifest) -> list[ManifestEntry]:
        """Manifest entries with no complete local copy. Blocking (stats files)."""
        return [e for e in current.entries if not is_cached(e, current.release)]

    def _evict_now(self, release: str, dashboard: object) -> None:
        max_mb = float(getattr(dashboard, "feature_videos_cache_max_mb", 500) or 0)
        keep = int(getattr(dashboard, "feature_videos_keep_releases", 0) or 0)
        evict(release, max_bytes=cache_ceiling_bytes(max_mb), keep_releases=keep)

    def _download_entry(
        self, entry: ManifestEntry, current: VideoManifest, rate: int
    ) -> tuple[bool, str]:
        """Fetch one entry's poster and clip, both verified. Blocking.

        The poster goes first because it is the small file, and an entry with no
        poster cannot be shown even with the clip in place — failing on the cheap
        half saves the expensive transfer.
        """
        try:
            folder = manifest_mod.ensure_cache_dir(current.release)
        except (OSError, ValueError) as exc:
            return False, f"cache directory unavailable: {exc}"
        poster_ok, poster_err = asset_downloader.download_to(
            folder / entry.poster,
            current.asset_url(entry.poster),
            sha256=entry.poster_sha256,
            # A bound, not a declared length: the manifest carries a poster sha but
            # no poster byte count, and max_bytes caps the transfer without
            # claiming to know its size (so it never discards a resumable partial).
            max_bytes=MAX_POSTER_BYTES,
            resume=True,
            rate_limit_bytes_per_s=rate,
            restrict_to_owner=True,
            label=f"feature-video poster {entry.id}",
        )
        if not poster_ok:
            return False, poster_err
        return asset_downloader.download_to(
            folder / entry.file,
            current.asset_url(entry.file),
            sha256=entry.sha256,
            size=entry.bytes,
            resume=True,
            rate_limit_bytes_per_s=rate,
            restrict_to_owner=True,
            label=f"feature-video clip {entry.id}",
        )

    def _set_state(self, state: str, *, downloading: str | None = None, error: str = "") -> None:
        self.status = {"download_state": state, "downloading": downloading, "error": error}

    def note_denied(self) -> None:
        """Record that the ceiling refused a transfer, for the status endpoint."""
        self._set_state(STATE_DENIED)


_cache: FeatureVideoCache | None = None
_cache_lock = threading.Lock()
# Module-level anchor for the in-flight task: asyncio holds only weak references
# to tasks, so a caller that drops the return value could see the transfer
# collected mid-flight. Same reason ``embeddings._download_task`` exists.
_task: "asyncio.Task[bool] | None" = None


def feature_video_cache() -> FeatureVideoCache:
    """Process-wide cache singleton (shared by the gateway and the dashboard)."""
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = FeatureVideoCache()
        return _cache


def reset_feature_video_cache() -> None:
    """Drop the singleton (tests, ``KIROCREW_HOME`` changes)."""
    global _cache, _task
    with _cache_lock:
        _cache = None
        if _task is not None and not _task.done():
            _task.cancel()
        _task = None


def start_background_feature_video_download() -> "asyncio.Task[bool] | None":
    """Kick the boot-time cache fill. Returns the task, or None when it is a no-op.

    Called from gateway startup beside ``start_background_model_download``, for
    the same reason: boot must not wait on a transfer. Idempotent — a second call
    while a pass is in flight returns the existing task.

    The kill switch, the ceiling and the manifest are all checked INSIDE the
    task, not here: each needs blocking work (config load, profile resolution, a
    network request), and doing any of it on the event loop at startup is what
    this function exists to avoid.
    """
    global _task
    if os.environ.get(SKIP_DOWNLOAD_ENV) == "1":
        return None
    if _task is not None and not _task.done():
        return _task
    _task = asyncio.create_task(feature_video_cache().ensure_all())
    return _task


# ── Serving ──


def resolve_served_path(release: str, name: str) -> "Path | None":
    """Resolve one cache file from URL components, or None if anything is off.

    Every component is validated against the SAME rules the manifest applies, so
    a request can only name a file a manifest could have named. aiohttp has
    already percent-decoded ``match_info``, so an encoded traversal arrives here
    as the literal characters and is caught by those rules rather than slipping
    past a check on the raw string.

    Containment is anchored to the CANONICAL cache root, and neither component
    below it may be a symlink. Both halves are load-bearing, and the release
    folder is the half that is easy to miss: anchoring to the release folder
    instead lets that folder BE the boundary it is supposed to sit inside, so a
    ``<root>/<release>`` symlink pointing at ``/etc`` makes ``/etc`` the root and
    ``/etc/passwd`` an ordinary file "inside" it.

    A symlink is refused rather than followed even when its target is contained.
    The cache holds files a remote CDN named, and a link is not something a
    manifest can describe, so there is no legitimate reader to keep working.

    Residual, stated because it is not closed here: a local process that can
    WRITE into the release folder can swap a checked file for a symlink between
    this check and the read that follows it. That attacker already has the user's
    own filesystem permissions, and the folder is created owner-only.
    """
    if not manifest_mod.is_safe_media_name(name):
        return None
    try:
        manifest_mod.release_dir(release)  # raises on an unsafe release name
        root = manifest_mod.cache_root().resolve(strict=True)
    except (ValueError, OSError, RuntimeError):
        return None
    served = root / release / name
    try:
        # lstat, never stat: stat() answers about a symlink's TARGET, which is
        # the question that lets the link through. Two components are checked
        # because either can be the link, and `name` is a single validated
        # basename, so the resolved root plus these two are the whole path.
        if not stat.S_ISDIR(os.lstat(served.parent).st_mode):
            return None
        if not stat.S_ISREG(os.lstat(served).st_mode):
            return None
    except (OSError, ValueError):
        return None
    return served


async def api_feature_video_file(request: web.Request) -> web.StreamResponse:
    """GET /feature-videos/{release}/{name} — one cached clip or poster.

    Read-only, one file per request, no listing. A path that does not resolve to
    a regular file inside the named release folder is a flat 404: telling "no
    such release" apart from "no such file" would answer questions about the
    user's cache contents that a 404 does not need to answer.
    """
    resolved = resolve_served_path(
        request.match_info.get("release", ""), request.match_info.get("name", "")
    )
    if resolved is None:
        raise web.HTTPNotFound()
    return web.FileResponse(resolved)


async def api_feature_videos_fetch_all(request: web.Request) -> web.Response:
    """POST /api/feature-videos/fetch-all — start the transfer now, unpaced.

    Fire-and-forget: the pass can take a while and the client polls ``/status``,
    so the response says it was started rather than waiting for it. The rate
    limit is lifted because a user pressed a button and is watching a readout;
    the boot-time pass stays paced.

    Answers 403 when the ceiling forbids downloading, rather than starting a task
    that would immediately deny itself: the caller asked for an action the fleet
    withdrew, and a silent "ok" would leave the dashboard waiting for bytes that
    are never coming.
    """
    del request  # the route takes no input; the manifest decides what is fetched
    global _task
    cache = feature_video_cache()
    if await asyncio.to_thread(manifest_mod.download_denied):
        cache.note_denied()
        return web.json_response(
            {"error": "downloading feature videos is not permitted", "code": "governance_denied"},
            status=403,
        )
    if _task is None or _task.done():
        _task = asyncio.create_task(cache.ensure_all(unlimited=True))
    return web.json_response({"ok": True, "download_state": cache.status.get("download_state")})


__all__ = [
    "DEFAULT_RATE_LIMIT_BYTES_PER_S",
    "SERVE_PREFIX",
    "MAX_POSTER_BYTES",
    "SKIP_DOWNLOAD_ENV",
    "STATE_DENIED",
    "STATE_DISABLED",
    "STATE_DOWNLOADING",
    "STATE_FAILED",
    "STATE_FETCHING_MANIFEST",
    "STATE_IDLE",
    "STATE_READY",
    "CachedAsset",
    "FeatureVideoCache",
    "api_feature_video_file",
    "api_feature_videos_fetch_all",
    "cached_asset",
    "evict",
    "feature_video_cache",
    "is_cached",
    "poster_url_path",
    "release_folders",
    "resolve_served_path",
    "reset_feature_video_cache",
    "start_background_feature_video_download",
]
