"""Feature videos — deterministic, one-shot feature-intro clips for the dashboard.

Sibling of :mod:`kiro_crew.tips`, and deliberately NOT built like it. A tip is
generated: the model picks a feature from a catalog and writes prose about it, so
the engine is a cadence gate plus a weighted-random selector over a pool that
changes every six hours. A video is a shipped artifact — a recorded clip with a
title and a poster — so nothing about it can be generated at request time, and
"which one do I show" has exactly one right answer for a given install state.

That makes ELIGIBILITY deterministic:

* the catalog is DATA, never generated at request time — a signed manifest
  published per release (:mod:`kiro_crew.feature_videos_manifest`), with the
  static tuple in this module (:data:`CATALOG`) as the fallback for an install
  that has never fetched one;
* "has the user already used this feature?" is answered by named probes
  (:data:`_PROBES` / :data:`_PARAM_PROBES`) that read local state, never by a
  model's guess;
* a clip is only offered once its media is actually on this machine, or — when
  nothing is cached yet — from the one CDN host the signed manifest names.

WHICH of several equally-eligible clips gets shown is RANDOM (:func:`select_next`).
Not because order starves a clip — it does not, since a verdict is permanent and
each launch retires the clip it showed, so a fixed order reaches the whole library
too. The draw is about the LIBRARY rather than one install: publication order is
the same for everyone, so a deterministic pick shows every install the same first
clip, and the newest entry is the last thing anybody sees. Drawing uniformly
spreads first impressions across the set, which is what makes early feedback on a
new clip arrive at all. Nothing about ELIGIBILITY is random, so a video the user
has retired, or one for a feature they already use, still cannot appear.

Both statuses a user can record (``seen`` / ``dismissed``) are PERMANENT. There
is no snooze, because a feature intro is not a recurring nudge: once it has been
watched or waved away, showing it again is noise.

Three source shapes reach a client, and :func:`validate_asset_path` is the one
gate for all of them: a bundled clip under ``/app-assets/feature-videos/``, a
cached clip under ``/feature-videos/<release>/`` served from the data home, and —
only when nothing is cached yet — an absolute ``https://`` url whose host must
equal the one the signed manifest declares. That host pin is the whole relaxation
of the original same-origin rule: it comes from a signed document, never from
config and never from the entry.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import math
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from aiohttp import web

import kiro_crew
from kiro_crew import asset_downloader
from kiro_crew import feature_videos_cache as cache_mod
from kiro_crew import feature_videos_manifest as manifest_mod
from kiro_crew.apps.version import parse_version
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import KiroCrewConfig, config_local_path, config_path
from kiro_crew.config.paths import config_dir
from kiro_crew.dashboard.handlers._shared import (
    _blocks_reads_session,
    _is_restricted_session,
    read_bounded_json,
)
from kiro_crew.sel import sel
from kiro_crew.tips_allowlist import TIP_DOC_ALLOWLIST

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: Every shipped clip and poster lives under this same-origin prefix, served
#: from ``website/public/app-assets/`` like every other bundled app asset.
ASSET_PREFIX = "/app-assets/feature-videos/"

#: The two same-origin prefixes a clip may be served from: bundled assets inside
#: the wheel, and downloaded clips in the user's data home
#: (``feature_videos_cache.SERVE_PREFIX``). Two prefixes rather than one tree
#: because the trees have different owners and different lifetimes — a route over
#: the data home must never be reachable through the bundled-asset prefix.
_SAME_ORIGIN_PREFIXES = (ASSET_PREFIX, cache_mod.SERVE_PREFIX)

#: Media suffixes a validated url may end in. An allowlist, so a manifest cannot
#: put ``.html`` (or an extensionless path) behind a ``<video src>``.
_MEDIA_SUFFIXES = (".mp4", ".jpg", ".jpeg", ".png", ".webp")

#: Timeout for the remote half of ``/api/feature-videos/probe``. Short on purpose:
#: the client is waiting on a yes/no about one url, and a slow answer is a "no"
#: worth giving quickly.
_PROBE_TIMEOUT_SECS = 3

#: Bound on a CATALOG entry's ``id``. The feedback route does not use it: there,
#: catalog membership is the tighter check and already closes the state file's
#: key set to the shipped slugs.
_VIDEO_ID_MAX_CHARS = 100

#: How far back a ``sel_event_seen`` probe reads. The SEL log is scanned
#: backward from the tail, so this is a real cost ceiling rather than a hint.
#: A tool used so long ago that it has fallen past this window reads as "not
#: used", which shows the video again — the safe direction for a probe
#: (see :func:`probe_fires`).
_SEL_PROBE_LIMIT = 500

#: The two statuses a user can record. Both are permanent.
VALID_STATUSES = ("seen", "dismissed")

#: Serializes the feedback route's read-modify-write of the state file. The
#: record step loads, mutates one key and saves, so two tabs recording different
#: videos at once would otherwise have the second save overwrite the first
#: video's row and offer it again. A ``threading.Lock`` rather than an
#: ``asyncio.Lock`` because the critical section runs in an executor THREAD, and
#: because a module-level asyncio primitive binds to whichever loop first
#: awaited it — the defect ``LoopBoundLock`` exists for. It holds no per-caller
#: data: the state it guards is one instance-wide file.
_state_write_lock = threading.Lock()


# ── Catalog ──


@dataclass(frozen=True)
class VideoEntry:
    """One shipped feature-intro clip.

    Frozen: the catalog is a constant, and a handler that could mutate an entry
    in place would leak one request's edit into every later request in the
    process.
    """

    #: Stable slug. Doubles as the state-file key and the asset basename.
    id: str
    #: The feature this clip introduces, as the docs name it.
    feature: str
    title: str
    description: str
    #: Same-origin relative path to the clip (see :func:`validate_asset_path`).
    src: str
    #: Same-origin relative path to the still frame shown before playback.
    poster: str
    duration_s: float
    #: User-facing doc for the feature. Must be in ``TIP_DOC_ALLOWLIST`` — the
    #: same gate tips use, so a video cannot point at an internal design note.
    doc: str
    #: Deterministic "the user already found this feature" signals. ANY of them
    #: firing withdraws the video: an intro for a feature already in use is the
    #: one thing a feature intro must not do.
    used_when: tuple[str, ...] = ()
    #: Minimum running version, or ``""`` for no floor. A clip recorded against
    #: a feature that does not exist on this build must not be offered.
    min_version: str = ""

    # No ``payload()`` here on purpose. A catalog entry is not what reaches a
    # client: the client-facing shape is :meth:`Offer.payload`, which carries the
    # resolved ``source`` and ``src`` a bundled entry cannot state on its own. Two
    # payload builders would be two chances for the hosted and bundled shapes to
    # drift, and only one of them is ever serialized.


def validate_asset_path(value: object, *, allowed_hosts: "frozenset[str]" = frozenset()) -> str:
    """Return *value* if it is a safe clip source, else ``""``.

    A video element's ``src`` is fetched by the browser with the dashboard's own
    credentials, so an attacker-controlled value here is an outbound request the
    user authorized without knowing it. This is the ONE function every source
    reaches the client through — bundled, cached and remote alike.

    Same-origin case (no *allowed_hosts*, or a value that is not an https url).
    Everything that could redirect the fetch off this origin, or walk out of the
    asset directory, is refused:

    * a scheme (``http:``, ``data:``, ``javascript:``) — any ``:`` at all, which
      also catches a Windows drive letter;
    * a protocol-relative ``//`` prefix, and any ``//`` elsewhere (an empty path
      segment is never meaningful for an asset);
    * ``..`` in any form, plus ``%`` so a percent-encoded ``%2e%2e`` cannot
      reconstitute one after the browser decodes it;
    * a backslash, which some clients normalize to ``/``;
    * anything outside :data:`_SAME_ORIGIN_PREFIXES`.

    Remote case, which is the relaxation the same-origin rule always reserved for
    hosted clips: an ``https://`` url is accepted ONLY when its host is in
    *allowed_hosts*, which callers derive from the signed manifest's own
    ``cdn_base`` — never from config and never from the entry. On top of the host
    pin the url must carry no userinfo (which would be a credential in a
    ``src``), no query or fragment (which is where a presigned credential or a
    redirect parameter would ride), no explicit port, and a path that passes the
    same traversal rules plus a media-suffix allowlist. An empty *allowed_hosts*
    means no remote source is acceptable at all, which is what makes "the ceiling
    denied downloading" express itself as "no remote clip" without a second
    branch at the caller.

    Returns the empty string rather than raising: a bad path in the shipped
    catalog, or in a manifest, is a bug — but it must degrade to "this entry is
    not offered" rather than 500 every ``/api/feature-videos/*`` request until it
    is fixed.
    """
    if not isinstance(value, str) or not value:
        return ""
    if value.lower().startswith("https://"):
        return _validate_remote_asset_url(value, allowed_hosts)
    if ":" in value or "//" in value or ".." in value or "%" in value or "\\" in value:
        return ""
    if any(ch.isspace() or ord(ch) < 0x20 for ch in value):
        return ""
    for prefix in _SAME_ORIGIN_PREFIXES:
        # Longer than the prefix: a value equal to the prefix is the directory
        # itself, with no filename.
        if value.startswith(prefix) and len(value) > len(prefix):
            return value
    return ""


def _validate_remote_asset_url(value: str, allowed_hosts: "frozenset[str]") -> str:
    """Return *value* if it is an https url on an allowed host, else ``""``."""
    if not allowed_hosts:
        return ""
    if any(ch.isspace() or ord(ch) < 0x20 for ch in value):
        return ""
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return ""
    if parts.scheme != "https":
        return ""
    if parts.username or parts.password:
        return ""
    try:
        if parts.port is not None:
            return ""
    except ValueError:
        # An unparseable port in the authority. Refusing beats guessing which
        # host a client would end up resolving.
        return ""
    if (parts.hostname or "").lower() not in allowed_hosts:
        return ""
    if parts.query or parts.fragment:
        return ""
    path = parts.path
    if not path.startswith("/") or ".." in path or "%" in path or "//" in path or "\\" in path:
        return ""
    if not path.lower().endswith(_MEDIA_SUFFIXES):
        return ""
    return value


def _entry_is_valid(entry: VideoEntry) -> bool:
    """Whether *entry* is safe to offer. Logs the reason when it is not."""
    reason = ""
    if not entry.id or len(entry.id) > _VIDEO_ID_MAX_CHARS:
        reason = "id missing or too long"
    elif not validate_asset_path(entry.src):
        reason = f"unsafe src {entry.src!r}"
    elif not validate_asset_path(entry.poster):
        reason = f"unsafe poster {entry.poster!r}"
    elif entry.doc not in TIP_DOC_ALLOWLIST:
        reason = f"doc {entry.doc!r} is not in the tips doc allowlist"
    elif entry.min_version:
        try:
            parse_version(entry.min_version)
        except ValueError:
            reason = f"unparseable min_version {entry.min_version!r}"
    if reason:
        logger.warning("feature video %r dropped from the catalog: %s", entry.id, reason)
        return False
    return True


#: The shipped catalog, in offer order. Seeded with the two features whose
#: "have you found this yet?" signal is cheapest to answer honestly.
CATALOG: tuple[VideoEntry, ...] = (
    VideoEntry(
        id="feature-tips",
        feature="feature-tips",
        title="Feature tips above the composer",
        description=(
            "A short card appears above the composer while a turn runs, pointing at a "
            "feature you have not used yet. Dismiss one and it stays gone."
        ),
        src=f"{ASSET_PREFIX}feature-tips.mp4",
        poster=f"{ASSET_PREFIX}feature-tips.jpg",
        duration_s=18.0,
        doc="feature-tips.md",
        used_when=("tips_feedback_exists",),
    ),
    VideoEntry(
        id="monitor-loops",
        feature="monitor-loops",
        title="Let one session watch a pull request",
        description=(
            "A monitor loop re-injects your own check instructions into this session on "
            "an interval, so one session can follow a pull request or a CI run to done."
        ),
        src=f"{ASSET_PREFIX}monitor-loops.mp4",
        poster=f"{ASSET_PREFIX}monitor-loops.jpg",
        duration_s=22.0,
        doc="monitor-loops.md",
        used_when=("sel_event_seen:monitor_start",),
    ),
)


def catalog() -> tuple[VideoEntry, ...]:
    """The catalog with unsafe entries filtered out.

    This is STRUCTURAL validity only -- a well-formed entry whose media has not
    shipped yet is still in here. That is deliberate: the feedback route checks
    membership against this set, and a user who has already been shown a clip
    must be able to record a verdict on it even if its asset later goes missing.
    :func:`offerable` is the set that may actually be shown.
    """
    return tuple(e for e in CATALOG if _entry_is_valid(e))


def _asset_root() -> Path:
    """Where ``ASSET_PREFIX`` is served from on disk.

    ``server.py`` mounts ``static/dist/app-assets`` at ``/app-assets``, so a
    catalog ``src`` of ``/app-assets/feature-videos/x.mp4`` is the file
    ``<static>/dist/app-assets/feature-videos/x.mp4``. Resolved through a
    function, not a constant, so a test can point it at a temp directory.
    """
    return Path(__file__).resolve().parent / "static" / "dist" / "app-assets"


def _asset_exists(url_path: str) -> bool:
    """Whether the file behind a validated ``/app-assets/...`` path is on disk."""
    prefix = "/app-assets/"
    if not url_path.startswith(prefix):
        return False
    return (_asset_root() / url_path[len(prefix) :]).is_file()


def offerable() -> tuple[VideoEntry, ...]:
    """The entries that may be SHOWN: valid, and with both media files on disk.

    "Asset shipped" is a precondition of "on offer", enforced here rather than
    trusted to the client. The dialog opens on the JSON answer alone and its
    ``<video>`` is ``preload="none"``, so nothing is fetched -- and no media error
    can fire -- until the user presses play. An entry whose clip is not shipped
    would therefore open a dialog around a blank player, and the natural "Got it"
    writes a PERMANENT verdict, retiring the real intro before anyone saw it.
    Dropping such an entry here keeps it on offer for the launch after its clip
    lands, which is the recoverable outcome.
    """
    kept: list[VideoEntry] = []
    for entry in catalog():
        missing = [p for p in (entry.src, entry.poster) if not _asset_exists(p)]
        if missing:
            logger.info(
                "feature video %r withheld: asset(s) not shipped: %s", entry.id, ", ".join(missing)
            )
            continue
        kept.append(entry)
    return tuple(kept)


# ── "Already used this feature" probes ──


def _probe_tips_feedback_exists() -> bool:
    """True once the user has reacted to a feature tip in any way.

    Reads the tips state file directly rather than importing the tips runtime:
    the two engines share a data home, not a code path, and a probe must not
    drag an LLM-bearing module (and its cache init) into a route that is polled.

    ``opted_out`` counts, and is checked SEPARATELY from the collection keys
    below rather than added to them. Turning tips off in Settings is the
    strongest reaction a user can have to the feature, but it is the one that
    writes no collection and leaves ``last_shown_ts`` at ``0.0`` — so a user who
    opted out before the cadence gate ever opened is exactly the person the
    collection scan reads as never having seen a tip, and the person the intro
    would then be played to.
    """
    path = config_dir() / "tips_state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        # RecursionError for the same reason load_state catches it: a nested
        # tips_state.json must degrade to "no feedback", never to a 500.
        return False
    if not isinstance(data, dict):
        return False
    if data.get("opted_out") is True:
        return True
    for key in ("shown", "dismissed", "dismissed_docs", "snoozed", "snoozed_docs"):
        value = data.get(key)
        if isinstance(value, (dict, list)) and value:
            return True
    last_shown = data.get("last_shown_ts")
    if isinstance(last_shown, (int, float)) and not isinstance(last_shown, bool):
        return bool(last_shown > 0)
    return False


def _probe_artifacts_nonempty() -> bool:
    """True when the artifact library holds at least one artifact.

    Scans directory entries and stops at the first hit instead of going through
    ``ArtifactStore.list()``, which reads every ``meta.json`` — this runs on a
    polled route, and the question is only "any at all?".
    """
    root = config_dir() / "artifacts"
    try:
        with os.scandir(root) as it:
            for entry in it:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir() and (Path(entry.path) / "meta.json").is_file():
                    return True
    except OSError:
        return False
    return False


def _probe_sel_event_seen(tool_name: str) -> bool:
    """True when the audit log carries a recent row naming *tool_name*.

    The SEL read is bounded on both ends (:data:`_SEL_PROBE_LIMIT`, tail-first),
    so this stays cheap on a large log.
    """
    if not tool_name:
        return False
    for row in sel().recent(limit=_SEL_PROBE_LIMIT):
        if not isinstance(row, dict):
            continue
        if row.get("operation") == tool_name:
            return True
    return False


def _probe_config_key_set(dotted: str) -> bool:
    """True when the user has explicitly set *dotted* in their config on disk.

    Deliberately reads the FILES rather than the loaded config: every key in the
    effective config has a value, so an effective-config read would fire on the
    shipped default and withdraw the video from someone who never touched the
    setting. Presence in ``config.json`` or ``config.local.json`` is the actual
    "the user configured this" signal.

    Presence, not truth — so this signal says "the user has an opinion about
    this key", which is not the same as "the feature is on". Do not reach for it
    to express "the operator disabled X": the same probe fires when they
    explicitly enabled it.
    """
    if not dotted:
        return False
    for path in (config_path(), config_local_path()):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            continue
        cur: object = raw
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                break
            cur = cur[part]
        else:
            return True
    return False


#: Probes taking no argument, keyed by the exact signal name.
_PROBES: dict[str, Callable[[], bool]] = {
    "tips_feedback_exists": _probe_tips_feedback_exists,
    "artifacts_nonempty": _probe_artifacts_nonempty,
}

#: Probes taking one argument, keyed by the part before the first ``:``.
_PARAM_PROBES: dict[str, Callable[[str], bool]] = {
    "sel_event_seen": _probe_sel_event_seen,
    "config_key_set": _probe_config_key_set,
}


def probe_fires(signal: str) -> bool:
    """Evaluate one ``used_when`` signal. Blocking — call off the event loop.

    An unknown signal and a raising probe both answer False, i.e. "the user has
    NOT used this feature", i.e. show the video. That is the safe direction:
    the failure mode is one clip a user may not need, where the opposite
    default would silently withhold every intro on a host whose audit log or
    artifact directory happens to be unreadable. Both cases are logged, because
    a probe that never fires looks exactly like a feature nobody uses.
    """
    name, separator, arg = signal.partition(":")
    try:
        if not separator:
            fn = _PROBES.get(name)
            if fn is None:
                logger.warning("unknown feature-video used_when signal %r", signal)
                return False
            return bool(fn())
        param_fn = _PARAM_PROBES.get(name)
        if param_fn is None:
            logger.warning("unknown feature-video used_when signal %r", signal)
            return False
        return bool(param_fn(arg))
    except Exception:
        logger.warning("feature-video probe %r failed; treating as unused", signal, exc_info=True)
        return False


# ── State ──


@dataclass
class FeatureVideoState:
    """Persisted per-video display state: ``id -> {"status": ..., "ts": ...}``."""

    videos: dict[str, dict[str, object]] = field(default_factory=dict)

    def status_of(self, video_id: str) -> str:
        row = self.videos.get(video_id)
        if not isinstance(row, dict):
            return ""
        status = row.get("status")
        return status if isinstance(status, str) and status in VALID_STATUSES else ""


def _state_path() -> Path:
    # Beside tips_state.json, through the same path helper: KIROCREW_HOME
    # tilde-expansion and unsafe-system-directory rejection must match the rest
    # of the config stack, which a raw os.environ read would not.
    return config_dir() / "feature_videos_state.json"


def _finite_ts(value: object) -> float:
    """Coerce a persisted timestamp to a finite float, or ``0.0``.

    ``float()`` on a several-hundred-digit JSON integer raises ``OverflowError``,
    which is NOT a subclass of the ``ValueError`` the loader catches — so
    without this a hand-edited state file would take down every
    ``/api/feature-videos/*`` request with a 500 until it was repaired by hand.
    The same guard tips' ``_finite`` carries, for the same reason.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def load_state() -> FeatureVideoState:
    """Read the state file, degrading to empty on anything unexpected.

    Per-entry validation, not just a root type check: a syntactically valid file
    carrying ``{"videos": {"x": 3}}`` would otherwise crash the selector and 500
    every endpoint until someone repaired the file by hand.
    """
    path = _state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        # RecursionError is neither an OSError nor a ValueError: json.loads
        # raises it on JSON nested past the interpreter's recursion limit. Our
        # own writer only ever emits a three-level document, so reaching this
        # needs a hand-edited file -- but the file is on disk and an operator
        # can edit it, and the alternative is a 500 on every feature-video
        # endpoint until someone works out which file to repair. Degrading to
        # "no state recorded" re-offers a clip at worst.
        return FeatureVideoState()
    if not isinstance(data, dict):
        logger.warning("feature_videos_state.json has non-dict root; using defaults")
        return FeatureVideoState()
    raw = data.get("videos")
    if not isinstance(raw, dict):
        return FeatureVideoState()
    videos: dict[str, dict[str, object]] = {}
    for key, row in raw.items():
        if not isinstance(key, str) or not isinstance(row, dict):
            continue
        status = row.get("status")
        if not isinstance(status, str) or status not in VALID_STATUSES:
            continue
        videos[key] = {"status": status, "ts": _finite_ts(row.get("ts"))}
    return FeatureVideoState(videos=videos)


def save_state(st: FeatureVideoState) -> None:
    """Persist *st* owner-only."""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(
        path,
        json.dumps({"videos": st.videos}, indent=2) + "\n",
        # Owner-only, for the same reason tips_state.json is: the file records
        # which features this user has and has not engaged with, which is a
        # behavioural profile and must not be world-readable on a shared box.
        # restrict_to_owner locks the temp file down BEFORE content reaches it
        # (a post-rename lockdown leaves the payload readable under the
        # inherited DACL on Windows), implies 0o600 on POSIX — which also
        # corrects a pre-existing 0644 file on the next write — and applies the
        # owner-only DACL on Windows. Warn-and-continue: a lockdown failure must
        # not break persistence, but it must be visible.
        restrict_to_owner=True,
        restrict_on_error="warn",
    )


def record_status(video_id: str, status: str) -> None:
    """Record one permanent display status. Blocking — call off the loop.

    Load, mutate, save under :data:`_state_write_lock`: the three steps are one
    transaction, and two tabs recording different videos concurrently would
    otherwise both read the same bytes and the later save would drop the earlier
    video's row — offering an already-dismissed clip again.
    """
    with _state_write_lock:
        st = load_state()
        st.videos[video_id] = {"status": status, "ts": time.time()}
        save_state(st)


# ── Selection ──


def _version_ok(min_version: str, running: str) -> bool:
    """Whether *running* satisfies *min_version*.

    An unparseable RUNNING version passes: the floor exists to hide a clip for a
    feature this build does not have, and refusing to decide would hide EVERY
    floored clip on a build whose own version string cannot be read. An
    unparseable FLOOR fails, because a floor nobody can evaluate is not a floor.
    """
    if not min_version:
        return True
    try:
        floor = parse_version(min_version)
    except ValueError:
        return False
    try:
        return parse_version(running) >= floor
    except ValueError:
        return True


#: Where a clip's bytes come from, as reported to the client. ``local`` covers
#: both same-origin cases — a bundled asset and a downloaded one — because from
#: the browser's side they are the same fetch against this origin, and a client
#: that had to tell them apart would be re-deriving a server decision. ``remote``
#: is the one case where the media comes from the CDN.
SOURCE_LOCAL = "local"
SOURCE_REMOTE = "remote"

#: How many recently-offered ids stay acceptable for a verdict. Bounded because
#: this is the ONE thing keeping the state file's key set closed: a verdict may name
#: a known clip or one of the last few offered, and nothing else. 64 is far more
#: than the one dialog a user can have open, and small enough that the set cannot
#: become a store.
_ISSUED_ID_LIMIT = 64

#: Ids ``/next`` has handed out, newest last. A clip can leave the catalog between
#: ``/next`` and the user's click — a background manifest refresh replaces the
#: catalog, and the clip they are looking at may not be in the new one. Without this
#: the feedback POST answers 400 and the verdict is DISCARDED, so the permanent
#: "do not show me this again" the user just expressed is lost and the clip returns.
#: Process-wide, and about the machine's own recent offers rather than any caller.
_issued_ids: "OrderedDict[str, float]" = OrderedDict()

#: Guards :data:`_issued_ids`. A threading lock, not an asyncio one: both routes
#: touch it from executor THREADS.
_issued_lock = threading.Lock()


def _remember_issued(video_id: str) -> None:
    """Record that ``/next`` offered *video_id*, evicting the oldest past the cap."""
    if not video_id:
        return
    with _issued_lock:
        _issued_ids.pop(video_id, None)
        _issued_ids[video_id] = time.time()
        while len(_issued_ids) > _ISSUED_ID_LIMIT:
            _issued_ids.popitem(last=False)


def _was_issued(video_id: str) -> bool:
    """Whether *video_id* is one of the recently offered ids."""
    with _issued_lock:
        return video_id in _issued_ids


def _forget_issued(video_id: str) -> None:
    """Drop *video_id* once its verdict is recorded — the row on disk supersedes it."""
    with _issued_lock:
        _issued_ids.pop(video_id, None)


#: The selector's randomness source, as an instance rather than the ``random``
#: module functions, so a test can pin it (``monkeypatch.setattr(fv, "_rng",
#: random.Random(0))``) without reseeding global randomness for everything else
#: in the process. Not ``secrets``: which intro clip plays is not a secret, and a
#: CSPRNG here would imply it was.
_rng = random.Random()


@dataclass(frozen=True)
class Offer:
    """One clip that could be shown right now, with the source resolved.

    The union of what a bundled :class:`VideoEntry` and a hosted
    :class:`~kiro_crew.feature_videos_manifest.ManifestEntry` have in common,
    plus the two things only the server can answer: WHERE the bytes are
    (:attr:`source`) and the exact ``src`` for that source. Eligibility and
    payload logic run on this one shape, so a hosted clip and a bundled clip
    cannot drift into two different rule sets.
    """

    id: str
    feature: str
    title: str
    description: str
    src: str
    poster: str
    duration_s: float
    doc: str
    min_version: str
    used_when: tuple[str, ...]
    source: str

    def payload(self) -> dict[str, object]:
        """The client-facing shape.

        ``used_when`` is withheld on purpose. It names local state the frontend
        has no business reading, and shipping it would invite a client to
        re-evaluate eligibility itself and drift from this module's answer.
        """
        return {
            "id": self.id,
            "feature": self.feature,
            "title": self.title,
            "description": self.description,
            "src": self.src,
            "poster": self.poster,
            "duration_s": self.duration_s,
            "doc": self.doc,
            "min_version": self.min_version,
            "source": self.source,
        }


def _bundled_offers() -> tuple[Offer, ...]:
    """Offers from the static catalog — the fallback for a manifest-less install."""
    return tuple(
        Offer(
            id=entry.id,
            feature=entry.feature,
            title=entry.title,
            description=entry.description,
            src=entry.src,
            poster=entry.poster,
            duration_s=entry.duration_s,
            doc=entry.doc,
            min_version=entry.min_version,
            used_when=entry.used_when,
            source=SOURCE_LOCAL,
        )
        for entry in offerable()
    )


def _hosted_offers(
    manifest: "manifest_mod.VideoManifest", *, allow_remote: bool
) -> tuple[tuple[Offer, ...], tuple[Offer, ...]]:
    """``(cached, remote)`` offers from a verified manifest. Blocking (stats files).

    A cached entry produces a same-origin offer only; an uncached one produces a
    remote offer only. The two lists are returned separately rather than merged
    because the caller must prefer local: a clip already on disk plays instantly
    and costs no egress, so reaching for the CDN while a cached clip is eligible
    would be a worse answer to the same question.

    *allow_remote* False (the ceiling denied downloading) is expressed by handing
    :func:`validate_asset_path` an EMPTY host allowlist, not by skipping the
    branch. One code path, and the host pin is what refuses the url either way.
    """
    hosts: frozenset[str] = (
        frozenset(h for h in (manifest.cdn_host,) if h) if allow_remote else frozenset()
    )
    cached: list[Offer] = []
    remote: list[Offer] = []
    for entry in manifest.entries:
        if cache_mod.is_cached(entry, manifest.release):
            src = validate_asset_path(f"{cache_mod.SERVE_PREFIX}{manifest.release}/{entry.file}")
            poster = validate_asset_path(cache_mod.poster_url_path(entry, manifest.release))
            bucket, source = cached, SOURCE_LOCAL
        else:
            src = validate_asset_path(manifest.asset_url(entry.file), allowed_hosts=hosts)
            poster = validate_asset_path(manifest.asset_url(entry.poster), allowed_hosts=hosts)
            bucket, source = remote, SOURCE_REMOTE
        if not src or not poster:
            continue
        bucket.append(
            Offer(
                id=entry.id,
                feature=entry.feature,
                title=entry.title,
                description=entry.description,
                src=src,
                poster=poster,
                duration_s=entry.duration_s,
                doc=entry.doc,
                min_version=entry.min_version,
                used_when=entry.used_when,
                source=source,
            )
        )
    return tuple(cached), tuple(remote)


def _eligible(offer: Offer, st: FeatureVideoState, running_version: str) -> bool:
    """Whether *offer* may be shown. Probes LAST and lazily.

    Probes are the only expensive part, so an offer already ruled out by recorded
    state or by the version floor must not pay for them.
    """
    if st.status_of(offer.id):
        return False
    if not _version_ok(offer.min_version, running_version):
        return False
    return not any(probe_fires(signal) for signal in offer.used_when)


def _offer_pools(running_version: str) -> tuple[tuple[Offer, ...], tuple[Offer, ...]]:
    """``(preferred, fallback)`` eligible pools, in the order they may be drawn from.

    See :func:`_offer_pools_and_permit`; this is the same answer without the
    download permit, for callers that only need to know what is on offer.
    """
    preferred, fallback, _permit = _offer_pools_and_permit(running_version)
    return preferred, fallback


def _offer_pools_and_permit(
    running_version: str,
) -> tuple[tuple[Offer, ...], tuple[Offer, ...], bool]:
    """``(preferred, fallback, download_enabled)``: the eligible pools, in the order
    they may be drawn from, and whether this install may pull clip bytes at all.

    One place decides which catalog is in force, so selection and the probe route
    cannot disagree about what is on offer. A verified manifest REPLACES the
    static catalog rather than extending it: mixing them would offer a bundled
    clip and its hosted successor as two videos, and a user retiring one would
    still be shown the other.

    The permit travels WITH the pools because the modal reads it beside the offer
    (``download_enabled`` on ``/next``) and fails closed on a remote clip without
    it. Reporting it from the same evaluation that built the pools means the two
    cannot disagree — a remote offer is never handed back beside ``False``, and
    a second evaluation for the readout would be a second audit row for one
    decision.
    """
    st = load_state()
    current = cache_mod.feature_video_cache().current_manifest()
    if current is None:
        pool = tuple(o for o in _bundled_offers() if _eligible(o, st, running_version))
        # No hosted catalog, so no remote offer exists and this answer authorizes
        # nothing. The memo is enough here, as it is for /status: spending an
        # audited decision on a readout is the split this module avoids.
        return pool, (), bool(manifest_mod.download_permitted_cached())
    # The AUDITED answer, not the memo. Handing back a remote offer authorizes
    # egress: the browser fetches that url from the CDN itself, so there is no
    # later transfer of ours to re-check it. A memo up to one TTL stale would let
    # a denial be answered with a CDN url for the rest of that window. The memo
    # stays where a stale answer costs only a readout — /status's display field
    # and the CSP header builder, which cannot block at all.
    allow_remote = not download_denied_now()
    cached, remote = _hosted_offers(current, allow_remote=allow_remote)
    return (
        tuple(o for o in cached if _eligible(o, st, running_version)),
        tuple(o for o in remote if _eligible(o, st, running_version)),
        allow_remote,
    )


def select_next(running_version: str) -> "Offer | None":
    """One eligible clip at random, or None. Blocking — call off the loop.

    Local before remote: a cached clip plays without egress and without a
    spinner, so the CDN is only reached when nothing eligible is on disk yet.
    Within a pool the pick is uniform — see the module docstring for why order
    stopped being the right rule once the library grew.
    """
    offer, _permit = select_next_and_permit(running_version)
    return offer


def select_next_and_permit(running_version: str) -> "tuple[Offer | None, bool]":
    """:func:`select_next` plus the download permit the draw was made under.

    For ``/next``, which reports the permit beside the offer. One evaluation
    serves both, so the response can never pair a remote clip with
    ``download_enabled: false`` — the combination the modal refuses to open.
    """
    preferred, fallback, permit = _offer_pools_and_permit(running_version)
    pool = preferred or fallback
    return (_rng.choice(list(pool)) if pool else None), permit


def find_offer(video_id: str, running_version: str) -> "Offer | None":
    """The offer for *video_id* as the selector would resolve it, or None.

    Shares :func:`_offer_pools` with selection deliberately: the probe route must
    answer about the source the client was actually handed, and re-deriving it
    from the id would be a second answer to the same question.
    """
    for pool in _offer_pools(running_version):
        for offer in pool:
            if offer.id == video_id:
                return offer
    return None


def known_video_ids() -> set[str]:
    """Every id a verdict may be recorded against. Blocking.

    The union of the static catalog and the current manifest, NOT just whichever
    one selection is using: a user who was shown a bundled clip before a manifest
    landed must still be able to record a verdict on it, and the recorded id is
    what keeps that clip retired afterwards.
    """
    ids = {entry.id for entry in catalog()}
    current = cache_mod.feature_video_cache().current_manifest()
    if current is not None:
        ids |= {entry.id for entry in current.entries}
    return ids


def probe_offer(offer: Offer) -> bool:
    """Whether *offer*'s ``src`` can actually be fetched right now. Blocking.

    Server-side, because the point is to answer BEFORE the dialog opens: the
    modal's ``<video>`` is ``preload="none"``, so a client cannot discover a dead
    src until the user presses play — and by then the natural "Got it" records a
    PERMANENT verdict against a clip nobody saw.

    A local src is a stat. A remote src is a ``HEAD``, and only to the host the
    signed manifest already authorized — the url reaching this function has been
    through :func:`validate_asset_path`, so the request cannot be aimed anywhere
    else. Any failure answers False: the caller's next step on False is to not
    offer the clip, which is the safe direction.
    """
    if offer.source == SOURCE_REMOTE:
        return _probe_remote(offer.src)
    if offer.src.startswith(ASSET_PREFIX):
        return _asset_exists(offer.src)
    tail = offer.src[len(cache_mod.SERVE_PREFIX) :]
    release, _, name = tail.partition("/")
    return cache_mod.resolve_served_path(release, name) is not None


def download_denied_now() -> bool:
    """The audited download decision. Blocking — never call on the event loop.

    A one-line alias so an action site reads as a decision rather than as a
    module reach-through, and so the audited and memoized reads cannot be
    confused for one another at a glance.
    """
    return bool(manifest_mod.download_denied())


def _probe_remote(url: str) -> bool:
    """HEAD *url* and report whether the object is there. Never raises.

    The gate lives HERE rather than at each caller: this function is the one place
    an outbound request leaves, so every route that probes — /probe and eligibility
    alike — is covered by one check instead of one per caller. A probe is egress, so
    it takes the audited answer; False is the safe direction, and the caller's next
    step on False is to not offer the clip.
    """
    if download_denied_now():
        return False
    try:
        request = urllib.request.Request(url, method="HEAD")
        # build_opener, never urlopen: validate_asset_path pinned this url to the
        # signed manifest's host, and urlopen's default handler would follow a
        # redirect straight off it.
        opener = asset_downloader.build_opener()
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- validate_asset_path pinned the https host to the signed manifest's cdn_base and redirects are host-pinned
        with opener.open(request, timeout=_PROBE_TIMEOUT_SECS) as resp:
            return 200 <= int(getattr(resp, "status", 0) or 0) < 300
    except (
        urllib.error.URLError,
        OSError,
        TimeoutError,
        ValueError,
        http.client.HTTPException,
    ):
        # http.client.InvalidURL / BadStatusLine derive from HTTPException, which is
        # neither an OSError nor a ValueError. A malformed reply from an allowed CDN
        # would otherwise escape a "never raises" helper and 500 the probe route.
        return False


# ── HTTP handlers ──


def _enabled() -> bool:
    return bool(KiroCrewConfig.load().dashboard.feature_videos_enabled)


async def api_feature_videos_next(request: web.Request) -> web.Response:
    """GET /api/feature-videos/next — the next intro clip to play, or null.

    Returns ``{"video": <offer-without-used_when> | null, "enabled": <bool>,
    "download_enabled": <bool>}``, where the offer carries ``source`` (``local``
    or ``remote``) and the ``src`` for that source — resolved here so a client
    never has to guess whether a clip is on disk.

    ``enabled`` is reported even when it is false, rather than a 204: the
    settings panel and the modal both need to tell "the operator turned this
    off" apart from "nothing left to show", and a bodiless response cannot.

    ``download_enabled`` is whether this install may pull clip bytes at all,
    read from the same evaluation that chose the offer. The modal fails closed
    on a remote clip unless it is exactly ``true`` (``StartupVideoModal``), so
    a remote offer without it would never play. It is reported only where an
    offer could have been made: the kill-switch and restricted-session answers
    carry no clip to gate, and evaluating the download ceiling for them would
    spend a governance read on every dashboard load of an install that has the
    feature off.
    """
    state: DashboardState = request.app["state"]
    loop = asyncio.get_running_loop()

    enabled = await loop.run_in_executor(None, _enabled)
    if not enabled:
        return web.json_response({"video": None, "enabled": False})

    # A temporary or incognito session shows no intro. The modal records
    # permanent state for the whole instance, and a session the user opened
    # precisely so it would leave no trace must not write that — the same reason
    # tips do not fetch in a temporary session.
    if _is_restricted_session(state, request):
        return web.json_response({"video": None, "enabled": True})

    entry, download_enabled = await loop.run_in_executor(
        None, select_next_and_permit, kiro_crew.__version__
    )
    if entry is not None:
        # The catalog can change under the open dialog, so the id is remembered
        # until its verdict lands (:data:`_issued_ids`). Otherwise a refresh between
        # here and the user's click makes the feedback POST a 400 and throws away a
        # verdict they will not be asked for again.
        _remember_issued(entry.id)
    return web.json_response(
        {
            "video": entry.payload() if entry else None,
            "enabled": True,
            "download_enabled": download_enabled,
        }
    )


async def api_feature_videos_status(request: web.Request) -> web.Response:
    """GET /api/feature-videos/status — switches, cache progress, and the state map.

    Returns ``{"enabled", "download_enabled", "release", "cached", "total",
    "downloading", "download_state", "state"}`` for the settings panel, which
    needs to render (and later reset) what has been seen, and to show whether the
    hosted library has finished arriving.

    ``state`` keeps its original meaning — the ``{<id>: {"status", "ts"}}``
    engagement map — and the transfer's own step is reported separately as
    ``download_state``. Renaming would have been the smaller diff here and the
    larger break: ``state`` is what the settings panel already reads.

    Only ``state`` is history. It is gated by ``_blocks_reads_session`` rather
    than ``_is_restricted_session``: that is the product's own read/write split,
    not a looser gate — incognito withholds WRITES, and a temporary session
    withholds reads as well. ``/next`` uses the broader predicate because reaching
    it leads to a permanent write, while this route only reads, so an incognito
    session still renders its own settings panel and only a read-blocking session
    is served an empty map.

    Every other field is instance configuration or cache bookkeeping, reported
    truthfully to every session: withholding them would make the panel claim the
    feature is off, or that nothing has downloaded.
    """
    state: DashboardState = request.app["state"]
    loop = asyncio.get_running_loop()
    cache = cache_mod.feature_video_cache()
    enabled = await loop.run_in_executor(None, _enabled)
    # Cached, not audited: this route is polled while a download runs, and one SEL
    # row per poll would bury the rows that record a real decision.
    download_enabled = await asyncio.to_thread(manifest_mod.download_permitted_cached)
    current = await asyncio.to_thread(cache.current_manifest)
    cached, total = await asyncio.to_thread(cache.counts)
    body: dict[str, object] = {
        "enabled": enabled,
        "download_enabled": download_enabled,
        "release": current.release if current is not None else "",
        "cached": cached,
        "total": total,
        "downloading": cache.status.get("downloading"),
        "download_state": cache.status.get("download_state"),
        "state": {},
    }
    if _blocks_reads_session(state, request):
        return web.json_response(body)
    st = await loop.run_in_executor(None, load_state)
    body["state"] = st.videos
    return web.json_response(body)


async def api_feature_videos_probe(request: web.Request) -> web.Response:
    """GET /api/feature-videos/probe?id=<id> — is this clip's src actually fetchable?

    Returns ``{"ok": <bool>}``. An unknown id, a clip the selector would not
    offer, and a src that does not answer all report ``ok: false`` from one shape:
    the client's decision on False is the same in every case (do not open the
    dialog), and telling them apart would only invite the client to act on the
    difference.
    """
    video_id = request.query.get("id", "")
    if not isinstance(video_id, str) or not video_id:
        return web.json_response({"ok": False})
    offer = await asyncio.to_thread(find_offer, video_id, kiro_crew.__version__)
    if offer is None:
        return web.json_response({"ok": False})
    return web.json_response({"ok": await asyncio.to_thread(probe_offer, offer)})


async def api_feature_videos_feedback(request: web.Request) -> web.Response:
    """POST /api/feature-videos/feedback — record a permanent display status.

    Body: ``{"id": <catalog id>, "status": "seen" | "dismissed"}``.

    Both statuses are terminal: there is no snooze, so a recorded video is never
    offered again. The id must name a known clip (:func:`known_video_ids`, the union
    of the static catalog and the current manifest) OR one ``/next`` offered
    recently (:data:`_issued_ids`) — an id from neither is a client bug, and
    accepting it would let an unbounded set of keys accumulate in the state file
    forever. Those two checks together are the only id validation, and both are
    strictly tighter than any length or shape bound.

    The second check exists because the catalog can change under an open dialog: a
    background manifest refresh replaces it, and without this check the
    user's verdict on the clip in front of them would 400 and be discarded — after
    which the clip comes back, which is the one thing a permanent verdict promises
    it will not do.
    """
    state: DashboardState = request.app["state"]

    body, err = await read_bounded_json(request)
    if err is not None:
        return err
    if body is None:  # pragma: no cover — read_bounded_json returns one or the other
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)

    video_id = body.get("id", "")
    status = body.get("status", "")
    if not isinstance(video_id, str) or not isinstance(status, str):
        return web.json_response(
            {"error": "id and status must be strings", "code": "invalid_field_type"},
            status=400,
        )
    if status not in VALID_STATUSES:
        return web.json_response({"error": "invalid status", "code": "invalid_status"}, status=400)
    # Membership is the ONLY id check, deliberately: it is strictly tighter than
    # any length bound, so an oversized id is already refused here as "not a
    # known clip". A separate length branch would ship a second permanent `code`
    # for a case this one fully covers, and a `code` is API surface that cannot be
    # narrowed later. This check is also what keeps the state file's key set
    # closed to ids a publisher or the package actually shipped.
    if video_id not in await asyncio.to_thread(known_video_ids) and not _was_issued(video_id):
        return web.json_response({"error": "unknown video id", "code": "unknown_video"}, status=400)

    # The write side needs the SAME gate the read side has, not just a symmetric
    # gesture: this is where the permanent, instance-wide row is actually
    # created, so gating only /next would leave the trace a restricted session
    # exists to avoid one POST away. Reported as success because nothing the
    # client did was wrong — the session simply keeps no state.
    if _is_restricted_session(state, request):
        return web.json_response({"ok": True})

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, record_status, video_id, status)
    # The persisted row supersedes the offer, so the id need not stay acceptable.
    _forget_issued(video_id)
    return web.json_response({"ok": True})
