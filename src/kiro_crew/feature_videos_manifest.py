"""The signed catalog of hosted feature-video clips, and the gate over fetching it.

:mod:`kiro_crew.feature_videos` ships a static catalog compiled into the package.
That is the right shape for two clips, and the wrong one for a growing library:
every added video would need a release, and the media would have to ride in the
wheel. This module is the hosted half — a manifest published per release folder on
the CDN, listing the clips available for that release with a sha256 for each.

Three properties make it safe to act on bytes fetched from a CDN:

**The manifest is signed, and an unverified manifest is discarded WHOLE.** Not
"the bad entries are dropped" — the document is refused, because the signature
covers the document. It is verified against the same offline key ``cli.sh`` pins
for the update feed (:mod:`kiro_crew.platform.feed_trust`), under a distinct
``schema`` string so a manifest of one kind can never be replayed as the other.
The fail-safe direction is "no manifest": the static catalog still serves, and
nothing is downloaded.

**Every clip is sha256-pinned by the signed manifest, so the CDN is not trusted.**
A tampered object can only fail verification in
:mod:`kiro_crew.asset_downloader`; it cannot reach the cache directory.

**Remote playback is pinned to the manifest's own ``cdn_base`` host.** The one
case where a browser fetches media off this origin is an eligible entry that is
not cached yet, and the host it may fetch from is stated by the signed document —
not by config, and not by the entry.

Fetching any of this is governed by ``capabilities.feature_videos_download``
(fail-closed). A denied ceiling means no manifest request, no clip download and
no remote playback: only what is already on disk is offered.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from kiro_crew import asset_downloader, platform_compat
from kiro_crew import sel as _sel_mod
from kiro_crew.apps.version import parse_version
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.platform import governance_profiles as _governance_profiles
from kiro_crew.platform.feed_trust import verify_document_signature
from kiro_crew.tips_allowlist import TIP_DOC_ALLOWLIST

logger = logging.getLogger(__name__)

#: Schema string every hosted manifest MUST carry. Distinct from the CLI update
#: feed's so a signature valid for one document kind cannot be replayed as the
#: other: both are signed by the same offline key, and the schema is what makes
#: the two payloads non-interchangeable.
MANIFEST_SCHEMA = "kirocrew-feature-videos-manifest-v1"

#: Basename of the manifest inside a release folder, on the CDN and in the cache.
MANIFEST_FILENAME = "manifest.json"

#: Public CloudFront distribution in front of Kiro Crew's own asset bucket — the
#: same distribution the embedding model is served from, a different prefix. The
#: signature, not the origin, is the trust anchor.
DEFAULT_CDN_BASE = "https://d3j0sthz5doyui.cloudfront.net/feature-videos"

#: Operator override for the manifest URL (mirrored or air-gapped deployments).
#: Wins over the config knob. Must be ``https://``.
MANIFEST_URL_ENV = "KIROCREW_FEATURE_VIDEOS_MANIFEST_URL"

#: Governance scope. Fail-closed, in the egress family with
#: ``capabilities.telemetry`` and ``capabilities.publish``.
DOWNLOAD_SCOPE = "capabilities.feature_videos_download"

#: Tool name on the SEL ``governance_decision`` row, so an operator reading the
#: trail can tell this decision apart from the other capability probes.
AUDIT_TOOL = "feature_videos_download"

#: Surface key for the evaluation. Pinned rather than taken from the request's
#: ``X-Session-Key``, which is CALLER-CONTROLLED: classifying by it would let a
#: request naming another surface dodge a profile bound to the dashboard. Same
#: pin, same rationale, as ``dashboard/social_share.py``.
DASHBOARD_SURFACE_KEY = "dashboard:ui"

_UNEVALUABLE_REASON = "governance unavailable (fail-closed)"

#: Last answer :func:`download_denied` produced, for the ONE caller that cannot
#: ask: the CSP header builder runs synchronously on every dashboard response, and
#: resolving a profile there would put a disk read (and an SEL append) on the hot
#: path. Starts denied, so a process that has not evaluated the ceiling yet emits
#: no CDN host — fail-closed in the direction that costs a lagging permit rather
#: than a lagging denial. Refreshed by every real evaluation: the boot task, the
#: dashboard config read and each selection all pay for one, so the memo tracks
#: the ceiling within a request of it changing. Process-wide state about the
#: MACHINE, not about any caller, which is why a module global is the right home.
_last_download_permitted = False

#: How long :func:`download_permitted_cached` may serve the memo before it
#: re-evaluates. Sixty seconds bounds a polled route to one audited SEL row per
#: minute; the action chokepoints do not use it and are never stale.
_GOVERNANCE_TTL_SECS = 60.0

#: ``time.monotonic()`` of the last real evaluation. 0.0 means "never", which makes
#: the first cached read evaluate rather than serve the denied default.
_last_download_check_ts = 0.0

#: How many earlier minor lines to try when this release has no manifest. Three
#: covers "the operator is a few releases behind and no new clips shipped since",
#: and bounds the boot-time cost at a handful of small JSON requests.
_FALLBACK_MINORS = 3

#: Whole-document size cap, matching the publisher's own
#: (``scripts/feature-videos/_manifest.py``). Bounds what an unauthenticated
#: response can make us buffer before the signature is even checked. It has to sit
#: ABOVE :data:`_SIGNED_PAYLOAD_MAX_BYTES`, because the document is the payload
#: plus a signature — a fetch cap at the payload cap would reject a manifest that
#: is exactly at its published limit.
_MANIFEST_MAX_BYTES = 1024 * 1024

#: Manifest request timeout. Short: this runs at boot behind a background task,
#: and a hanging CDN must not hold the task open for the download timeout.
_MANIFEST_TIMEOUT_SECS = 10

#: Signed-payload cap, passed to the shared verifier. The CLI feed's own cap
#: stays what it was — a manifest of clips is a larger document than a channel
#: descriptor and needs its own bound. The value is the PUBLISHER's
#: ``DEFAULT_MAX_PAYLOAD_BYTES``: a cap tighter than the one a release is built
#: against would discard a valid manifest, and the discard is whole-document, so
#: the failure would be "no videos" rather than "one clip missing".
_SIGNED_PAYLOAD_MAX_BYTES = 256 * 1024

#: Per-clip size ceiling. A feature intro is a ~20 second screen recording; a
#: manifest asking for more than this is either wrong or hostile, and the
#: declared size is checked against the pin before a byte is written.
_MAX_ENTRY_BYTES = 64 * 1024 * 1024

#: Cap on how many entries one manifest may carry — a cheap coherence check, not the
#: real one. :data:`_SIGNED_PAYLOAD_MAX_BYTES` is what actually limits a manifest,
#: and this sits far enough above it that a document the publisher would emit
#: cannot trip this instead: tripping it discards the whole manifest, and "we
#: refused a valid release because it listed too many clips" is not a failure
#: worth having.
_MAX_ENTRIES = 1000

#: A media basename: no directory component, no traversal, no escaping. An
#: allowlist rather than a denylist, because this string becomes a path segment
#: on disk AND a URL segment the browser fetches — the two decodings do not
#: agree, so enumerating what is forbidden is the losing side of that.
_SAFE_BASENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")

#: A 64-character lowercase hex digest.
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: A release folder name. This is a path segment under the cache root AND a URL
#: segment, so it is validated as strictly as a filename: digits and dots only,
#: each component bounded.
#:
#: One to four components, because the publisher accepts a bare numeric version of
#: any depth (``scripts/feature-videos/_manifest.py``) while Kiro Crew's own
#: releases are always ``major.minor.patch``. Accepting what the publisher can emit
#: costs nothing — the fetch path only ever ASKS for ``major.minor.patch`` folders
#: (:func:`release_candidates`) — and refusing it would discard a valid release
#: whole.
_RELEASE_RE = re.compile(r"^\d{1,5}(?:\.\d{1,5}){0,3}$")

_CLIP_SUFFIX = ".mp4"
_POSTER_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")


# ── Governance ──


def download_denied() -> bool:
    """Whether the ceiling forbids fetching hosted feature videos. Blocking.

    FAIL-CLOSED. The two dispositions are not symmetric: a wrong-DENY withholds
    an intro clip a user might have enjoyed, while a wrong-PERMIT makes an
    outbound request to a vendor CDN on a fleet that forbade vendor egress —
    which puts this row with ``capabilities.telemetry`` / ``capabilities.publish``
    rather than with the advisory probes. ``fail_closed`` also makes the
    evaluator audit an unevaluable ceiling as a critical SEL event.

    Consulted at three chokepoints, because any one alone is a half-control: the
    manifest fetch, the clip download, and the remote ``src`` a denied install
    must never be handed. The dashboard reads the same answer as
    ``feature_videos_download_enabled`` so the frontend never guesses.
    """
    global _last_download_permitted
    try:
        # Looked up on the MODULE at call time, not bound by `from ... import`: the
        # governance tests patch `governance_profiles.vet_and_audit` at its
        # definition site, and an import-time name binding would hold the original.
        decision = _governance_profiles.vet_and_audit(
            DOWNLOAD_SCOPE,
            "",
            session_key=DASHBOARD_SURFACE_KEY,
            tool_name=AUDIT_TOOL,
            log_warning=False,
            fail_closed=True,
        )
    except Exception:
        # governance_permits converts its own internal errors into a denying
        # Decision, so reaching here means the import, the composition or the
        # call itself failed — the ceiling is unevaluable, which is the same
        # condition as a degrade. Fail closed, and record what the seam could not.
        logger.debug("feature-video download governance probe failed; denying", exc_info=True)
        _audit_unevaluable()
        _last_download_permitted = False
        return True
    _last_download_permitted = bool(getattr(decision, "permitted", False))
    return not _last_download_permitted


def download_permitted_memo() -> bool:
    """The last evaluated answer, WITHOUT evaluating one. See :data:`_last_download_permitted`.

    Only for callers that cannot block — today that is the CSP header builder.
    Anything that can afford a decision calls :func:`download_denied` (an action
    chokepoint) or :func:`download_permitted_cached` (a polled read): a memo is a
    cached fact, not an authorization.
    """
    return _last_download_permitted


def download_permitted_cached() -> bool:
    """Permitted?, re-evaluated at most once per :data:`_GOVERNANCE_TTL_SECS`. Blocking.

    For a POLLED route. ``/api/feature-videos/status`` and ``/next`` are polled
    while a download runs, and :func:`download_denied` writes a
    ``governance_decision`` SEL row on every call — so evaluating per request turns
    a progress readout into an audit-log flood and a disk write per poll, which
    also buries the rows that record a real decision.

    The split this creates is the point: an audited evaluation happens where the
    ACTION is (each clip download, ``fetch-all``), and a polled read gets a bounded
    stale answer. Worst case a denial is up to a minute old on one offer, while the
    transfer it would stop is re-checked before every request it makes.

    Fail-closed on the cold path: with nothing evaluated yet the first call
    evaluates rather than returning the denied default, so a fresh process reports
    the truth instead of claiming the feature is off.
    """
    global _last_download_check_ts
    now = time.monotonic()
    if _last_download_check_ts and (now - _last_download_check_ts) < _GOVERNANCE_TTL_SECS:
        return _last_download_permitted
    _last_download_check_ts = now
    return not download_denied()


def _audit_unevaluable() -> None:
    """Best-effort SEL record for the path ``vet_and_audit`` never reached."""
    try:
        # `_sel_mod.sel()` rather than a bound `sel`: the suite patches
        # `kiro_crew.sel.sel`, and a module-attribute lookup at call time sees it.
        _sel_mod.sel().log_governance_decision(
            session_key=DASHBOARD_SURFACE_KEY,
            tool_name=AUDIT_TOOL,
            scope=DOWNLOAD_SCOPE,
            item="",
            outcome="denied",
            reason=_UNEVALUABLE_REASON,
        )
    except Exception:
        # SEL writes to a file, and an audit failure must never wedge the probe.
        pass


# ── Shapes ──


@dataclass(frozen=True)
class ManifestEntry:
    """One hosted clip, as the signed manifest declares it.

    Frozen for the same reason :class:`~kiro_crew.feature_videos.VideoEntry` is:
    a handler that could mutate one in place would leak a request's edit into
    every later request in the process.

    ``file`` and ``poster`` are BASENAMES, not paths and not URLs. The release
    folder they sit in is the manifest's, so an entry cannot name a location —
    which is what keeps both the on-disk path and the served URL derivable from
    values this module validated.
    """

    id: str
    feature: str
    title: str
    description: str
    file: str
    poster: str
    sha256: str
    poster_sha256: str
    bytes: int
    duration_s: float
    doc: str
    used_when: tuple[str, ...] = ()
    min_version: str = ""


@dataclass(frozen=True)
class VideoManifest:
    """A verified manifest for one release folder."""

    release: str
    cdn_base: str
    generated_at: str
    entries: tuple[ManifestEntry, ...]

    @property
    def cdn_host(self) -> str:
        """Host of ``cdn_base``, or ``""``. The only host remote playback allows."""
        try:
            return (urllib.parse.urlsplit(self.cdn_base).hostname or "").lower()
        except ValueError:
            return ""

    def asset_url(self, name: str) -> str:
        """Absolute CDN url for a basename in this manifest's release folder."""
        return f"{self.cdn_base.rstrip('/')}/{self.release}/{name}"

    def entry(self, video_id: str) -> "ManifestEntry | None":
        for candidate in self.entries:
            if candidate.id == video_id:
                return candidate
        return None


# ── Paths ──


def cache_root() -> Path:
    """Root of the local clip cache. Respects ``KIROCREW_HOME``.

    Through ``config_dir()`` rather than a raw environment read, so tilde
    expansion and unsafe-system-directory rejection match the rest of the config
    stack.
    """
    return config_dir() / "feature-videos"


def release_dir(release: str) -> Path:
    """Cache folder for *release*. Raises ``ValueError`` on an unsafe name.

    Raises rather than returning a fallback: every caller here derives *release*
    from a validated manifest or from the running version, so an unsafe value is
    a bug in this module and must not silently resolve to a path.
    """
    if not _RELEASE_RE.match(release):
        raise ValueError(f"unsafe release folder name: {release!r}")
    return cache_root() / release


def cached_manifest_path(release: str) -> Path:
    """Where the verified manifest for *release* is kept.

    INSIDE the release folder, so evicting a release removes its manifest with
    its clips in one ``rmtree`` and cannot leave a manifest advertising files
    that are gone.
    """
    return release_dir(release) / MANIFEST_FILENAME


def ensure_cache_dir(release: str) -> Path:
    """Create the release folder owner-only and return it.

    Owner-only for the same reason the state file is: which clips this install
    has fetched is a behavioural signal, and on a shared box it must not be
    world-readable. ``make_owner_only_dir`` creates the parents, tolerates an
    existing directory, and tightens it either way — a directory created before
    this guarantee existed would otherwise stay readable.
    """
    path = release_dir(release)
    platform_compat.make_owner_only_dir(path)
    return path


def is_safe_media_name(name: object) -> bool:
    """Whether *name* is a basename a manifest could legitimately carry.

    Public because the serving route validates a URL segment against the SAME
    rule the manifest parser applies. Two copies of "what is a safe basename"
    would be two chances to disagree, and the route is the one where disagreeing
    means reading a file outside the cache.
    """
    return isinstance(name, str) and bool(_SAFE_BASENAME_RE.match(name)) and ".." not in name


# ── Release resolution ──


def running_release(version: str) -> str:
    """The release folder name for *version*, or ``""`` if it cannot be parsed.

    The numeric release segment only: an insider build stamped ``0.6.0rc3`` and
    the stable ``0.6.0`` read the same folder, because a prerelease of a version
    plays that version's clips.
    """
    try:
        major, minor, patch = parse_version(version)
    except ValueError:
        return ""
    return f"{major}.{minor}.{patch}"


def release_candidates(version: str) -> tuple[str, ...]:
    """Release folders to try for *version*, newest first.

    A release with no manifest of its own falls back to the newest LOWER release
    that has one — a build cut with no new clips must still play the existing
    library rather than showing nothing. The chain is, in order: this exact
    release; this minor line's ``.0`` when the running patch is not 0 (clips are
    published per minor line, so ``0.6.3`` reads ``0.6.0``'s folder); then the
    ``.0`` of up to :data:`_FALLBACK_MINORS` preceding minors.

    Never walks DOWN a major boundary. A major bump is where a clip is most
    likely to show a UI the running build does not have, which is the one thing a
    stale intro must not do.
    """
    base = running_release(version)
    if not base:
        return ()
    # running_release() always emits three components (parse_version pads), so this
    # unpack is safe regardless of what shape a MANIFEST is allowed to declare.
    major, minor, patch = (int(part) for part in base.split("."))
    out = [base]
    if patch:
        out.append(f"{major}.{minor}.0")
    for step in range(1, _FALLBACK_MINORS + 1):
        if minor - step < 0:
            break
        out.append(f"{major}.{minor - step}.0")
    # dict.fromkeys: order-preserving dedupe, for the patch==0 case where the
    # second entry would repeat the first.
    return tuple(dict.fromkeys(out))


# ── Parsing and verification ──


def _safe_media_name(value: object, suffixes: tuple[str, ...]) -> str:
    """Return *value* if it is a safe media basename with one of *suffixes*."""
    if not is_safe_media_name(value):
        return ""
    assert isinstance(value, str)  # narrowed by is_safe_media_name
    return value if value.lower().endswith(suffixes) else ""


def _parse_entry(
    raw: object, *, doc_allowlist: frozenset[str] | set[str]
) -> "ManifestEntry | None":
    """Validate one manifest entry. Returns None (with a reason logged) if unsafe."""
    if not isinstance(raw, dict):
        return None
    video_id = raw.get("id")
    reason = ""
    if not isinstance(video_id, str) or not _SAFE_BASENAME_RE.match(video_id):
        logger.warning("hosted feature video dropped: unsafe id %r", video_id)
        return None
    clip = _safe_media_name(raw.get("file"), (_CLIP_SUFFIX,))
    poster = _safe_media_name(raw.get("poster"), _POSTER_SUFFIXES)
    sha = raw.get("sha256")
    poster_sha = raw.get("poster_sha256")
    size = raw.get("bytes")
    doc = raw.get("doc")
    if not clip:
        reason = f"unsafe file {raw.get('file')!r}"
    elif not poster:
        reason = f"unsafe poster {raw.get('poster')!r}"
    elif not isinstance(sha, str) or not _SHA256_RE.match(sha):
        reason = "sha256 is not a 64-character lowercase hex digest"
    elif not isinstance(poster_sha, str) or not _SHA256_RE.match(poster_sha):
        reason = "poster_sha256 is not a 64-character lowercase hex digest"
    elif isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= _MAX_ENTRY_BYTES:
        reason = f"bytes {size!r} outside 1..{_MAX_ENTRY_BYTES}"
    elif doc not in doc_allowlist:
        # The same gate the static catalog applies, so a hosted clip cannot
        # point the "Learn more" link at an internal design note either.
        reason = f"doc {doc!r} is not in the tips doc allowlist"
    if reason:
        logger.warning("hosted feature video %r dropped: %s", video_id, reason)
        return None
    min_version = raw.get("min_version", "")
    if not isinstance(min_version, str):
        min_version = ""
    if min_version:
        try:
            parse_version(min_version)
        except ValueError:
            logger.warning(
                "hosted feature video %r dropped: unparseable min_version %r",
                video_id,
                min_version,
            )
            return None
    raw_signals = raw.get("used_when", ())
    signals = (
        tuple(s for s in raw_signals if isinstance(s, str) and s)
        if isinstance(raw_signals, (list, tuple))
        else ()
    )
    duration = raw.get("duration_s", 0.0)
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        duration = 0.0
    return ManifestEntry(
        id=video_id,
        feature=str(raw.get("feature", video_id)),
        title=str(raw.get("title", "")),
        description=str(raw.get("description", "")),
        file=clip,
        poster=poster,
        sha256=sha,  # type: ignore[arg-type]
        poster_sha256=poster_sha,  # type: ignore[arg-type]
        bytes=size,  # type: ignore[arg-type]
        duration_s=float(duration),
        doc=doc,  # type: ignore[arg-type]
        used_when=signals,
        min_version=min_version,
    )


def parse_manifest(raw: object) -> "VideoManifest | None":
    """Validate a VERIFIED manifest document into a :class:`VideoManifest`.

    Signature checking is :func:`verify_manifest` — this is the structural half,
    and it is deliberately separate so the order is impossible to get wrong: a
    caller that reaches this function has already established the document is the
    one the publisher signed.

    An individual malformed ENTRY is dropped with a logged reason; a malformed
    DOCUMENT (wrong schema, no release, off-origin ``cdn_base``) returns None.
    The asymmetry is on purpose: a bad entry costs one clip, while a bad document
    would make every path derived from it wrong.
    """
    if not isinstance(raw, dict):
        return None
    if raw.get("schema") != MANIFEST_SCHEMA:
        logger.warning("feature-video manifest rejected: schema %r", raw.get("schema"))
        return None
    release = raw.get("release")
    if not isinstance(release, str) or not _RELEASE_RE.match(release):
        logger.warning("feature-video manifest rejected: release %r", raw.get("release"))
        return None
    cdn_base = raw.get("cdn_base")
    if not isinstance(cdn_base, str) or not cdn_base.lower().startswith("https://"):
        logger.warning("feature-video manifest rejected: cdn_base is not https")
        return None
    raw_entries = raw.get("entries")
    if not isinstance(raw_entries, list) or len(raw_entries) > _MAX_ENTRIES:
        logger.warning("feature-video manifest rejected: entries is not a bounded list")
        return None
    entries: list[ManifestEntry] = []
    seen: set[str] = set()
    for item in raw_entries:
        entry = _parse_entry(item, doc_allowlist=TIP_DOC_ALLOWLIST)
        if entry is None or entry.id in seen:
            continue
        seen.add(entry.id)
        entries.append(entry)
    generated_at = raw.get("generated_at", "")
    return VideoManifest(
        release=release,
        cdn_base=cdn_base,
        generated_at=generated_at if isinstance(generated_at, str) else "",
        entries=tuple(entries),
    )


def verify_manifest(raw: object) -> bool:
    """Whether *raw* carries a signature valid for the pinned offline key.

    Blocking — it shells out to openssl, so async callers offload it.
    """
    if not isinstance(raw, dict):
        return False
    return verify_document_signature(raw, max_payload_bytes=_SIGNED_PAYLOAD_MAX_BYTES)


def verified_manifest(raw: object) -> "VideoManifest | None":
    """Verify then parse. The ONLY way a manifest becomes a usable object here.

    Verification first, in one place, so no caller can accidentally act on a
    document it only parsed. An unverified document is discarded WHOLE — with a
    warning, because a real signing regression and a tampered CDN look the same
    from here and both need to be visible.
    """
    if not verify_manifest(raw):
        logger.warning(
            "feature-video manifest signature did not verify; discarding the whole manifest"
        )
        return None
    return parse_manifest(raw)


# ── Fetching ──


def manifest_url(release: str, configured: str = "") -> str:
    """Resolve the manifest url for *release*: env > config knob > CDN default.

    An override names the manifest DIRECTLY (it is one document, not a base), so
    a mirror can serve a single manifest for every release. Both override sources
    must be ``https://``; anything else is ignored with a warning rather than
    honoured, so an operator-controlled value cannot downgrade the fetch to
    plaintext or point it at a local file.
    """
    env_url = os.environ.get(MANIFEST_URL_ENV, "").strip()
    if env_url:
        if env_url.lower().startswith("https://"):
            return env_url
        logger.warning("%s must be an https:// url — ignoring the override", MANIFEST_URL_ENV)
    configured = (configured or "").strip()
    if configured:
        if configured.lower().startswith("https://"):
            return configured
        logger.warning(
            "dashboard.feature_videos_manifest_url must be an https:// url — ignoring it"
        )
    return f"{DEFAULT_CDN_BASE}/{release}/{MANIFEST_FILENAME}"


def _fetch_json(url: str) -> "dict | None":
    """GET *url* and parse a bounded JSON object. Never raises.

    Bounded BEFORE parsing: the response is unauthenticated until the signature
    check, so the only safe assumption about its size is the one we impose.
    """
    try:
        request = urllib.request.Request(url, method="GET")
        # build_opener, never urlopen: the url was authorized against ONE host, and
        # urlopen's default handler would follow a cross-host redirect and spend
        # that authorization on a destination the CDN chose.
        opener = asset_downloader.build_opener()
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- manifest_url enforces https://, redirects are host-pinned, and the document is signature-verified before use
        with opener.open(request, timeout=_MANIFEST_TIMEOUT_SECS) as resp:
            body = resp.read(_MANIFEST_MAX_BYTES + 1)
        if len(body) > _MANIFEST_MAX_BYTES:
            logger.warning(
                "feature-video manifest at %s exceeds %d bytes; ignoring",
                asset_downloader.redact_url(url),
                _MANIFEST_MAX_BYTES,
            )
            return None
        data = json.loads(body.decode("utf-8"))
    except (
        urllib.error.URLError,
        OSError,
        TimeoutError,
        ValueError,
        RecursionError,
        http.client.HTTPException,
    ) as exc:
        # Two entries here are not the obvious ones. json.loads raises
        # RecursionError on a document nested past the interpreter's limit, and
        # urllib raises http.client.InvalidURL on a malformed url — a mistyped
        # https override reaches this line, not the network. Neither is an
        # OSError or a ValueError, so without them a hostile response or a typo
        # propagates out of a background task instead of reading as "no
        # manifest". HTTPException is the base class, so its siblings
        # (BadStatusLine, LineTooLong, IncompleteRead) come with it.
        # TYPE only, never `exc`. `http.client.InvalidURL` embeds the offending url
        # verbatim in its message ("nonnumeric port: 'secretpw@…'"), so logging the
        # exception hands the log ring and /api/logs the credential that
        # `redact_url` two lines up exists to keep out. The type is what aids
        # diagnosis anyway (timeout vs refused vs malformed), and it cannot carry
        # one. No `exc_info=True` for the same reason: a traceback renders the
        # exception's own `str()`, which would relocate the leak rather than close
        # it. Same rule as papyrus/tectonic.py's download path.
        logger.info(
            "could not fetch the feature-video manifest from %s: %s",
            asset_downloader.redact_url(url),
            type(exc).__name__,
        )
        return None
    return data if isinstance(data, dict) else None


def store_manifest(manifest: VideoManifest, raw: dict) -> None:
    """Cache the verified manifest document beside the clips it describes.

    The RAW document is stored, signature included, so the cached copy can be
    re-verified on the next read rather than trusted because we once trusted it.
    """
    try:
        ensure_cache_dir(manifest.release)
        atomic_write(
            cached_manifest_path(manifest.release),
            json.dumps(raw, indent=2, sort_keys=True) + "\n",
            restrict_to_owner=True,
            restrict_on_error="warn",
        )
    except (OSError, ValueError):
        logger.warning("could not cache the feature-video manifest", exc_info=True)


def load_cached_manifest(version: str) -> "VideoManifest | None":
    """The newest cached manifest eligible for *version*, re-verified. Blocking.

    Re-verified on every read, not trusted because it was verified when written.
    The file sits in the user's data home where an operator (or anything running
    as them) can edit it, and what it states includes ``cdn_base`` — the one host
    remote playback is allowed to reach. A cache that could be edited into a
    trust decision would make the signature pointless.
    """
    for release in release_candidates(version):
        try:
            path = cached_manifest_path(release)
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            continue
        manifest = verified_manifest(raw)
        if manifest is not None:
            return manifest
    return None


def fetch_manifest(version: str, configured_url: str = "") -> "tuple[VideoManifest | None, dict]":
    """Fetch, verify and parse the manifest for *version*. Blocking.

    Returns ``(manifest, raw_document)`` so the caller can cache the exact bytes
    that verified. Walks :func:`release_candidates` and stops at the first
    release whose manifest verifies AND whose ``release`` field matches the
    folder it was served from — a manifest that disagrees with its own location
    is refused, so a stale or misfiled document cannot silently redirect one
    release's clips into another release's folder.

    Caller-side gate: this makes a network request, so a caller MUST have
    established :func:`download_denied` is False first. It is not re-checked here
    because the check belongs where the decision is (and being audited twice per
    boot would make the SEL trail read like two attempts).
    """
    for release in release_candidates(version):
        url = manifest_url(release, configured_url)
        raw = _fetch_json(url)
        if raw is None:
            continue
        manifest = verified_manifest(raw)
        if manifest is None:
            continue
        if manifest.release != release and not configured_url:
            logger.warning(
                "feature-video manifest at %s declares release %r; refusing the mismatch",
                asset_downloader.redact_url(url),
                manifest.release,
            )
            continue
        return manifest, raw
    return None, {}


__all__ = [
    "AUDIT_TOOL",
    "DEFAULT_CDN_BASE",
    "DOWNLOAD_SCOPE",
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA",
    "MANIFEST_URL_ENV",
    "ManifestEntry",
    "VideoManifest",
    "cache_root",
    "cached_manifest_path",
    "download_denied",
    "download_permitted_cached",
    "download_permitted_memo",
    "ensure_cache_dir",
    "fetch_manifest",
    "is_safe_media_name",
    "load_cached_manifest",
    "manifest_url",
    "parse_manifest",
    "release_candidates",
    "release_dir",
    "running_release",
    "store_manifest",
    "verified_manifest",
    "verify_manifest",
]
