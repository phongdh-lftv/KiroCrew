"""One verified HTTPS download of one pinned file — the shared transfer engine.

Extracted from :mod:`kiro_crew.embeddings`, which had the only copy: a streamed
GET whose bytes are hashed as they arrive, an atomic install, and an error string
per failure mode. The embedding model was the first pinned artifact Kiro Crew
fetches at runtime; feature-video clips are the second, and a second private
downloader would mean two places for "did we verify this before installing it?"
to be answered differently.

What this module owns, and what it deliberately does not:

* it owns the TRANSFER — connect, stream, hash, verify, install, and the wording
  of each failure — so every caller inherits the same verify-before-install
  order;
* it does NOT own retry policy, backoff, concurrency, or WHICH url to fetch.
  Those are caller decisions (the model manager retries for hours across gateway
  boots; the video cache walks a manifest one entry at a time), and a retry loop
  buried here would make a caller's own loop invisible.

Two invariants hold for every caller:

**The sha256 pin is the trust anchor, not the origin.** The url may come from an
operator override or a signed manifest; either way nothing is installed until the
streamed digest matches the expected one, so a tampered CDN object can only fail
verification. A caller with no pin has no business using this module.

**Nothing lands at the final path until it is verified.** Bytes accumulate in a
staging file beside the target and reach *path* through one ``os.replace``, so a
reader either sees the previous file or the complete new one — never a truncated
prefix. This is what makes an interrupted transfer safe to resume: the partial is
a staging file nobody serves.

**The transfer stays inside the host it was authorized for.** ``urlopen`` follows
a redirect by default, and the url this module is handed was authorized by one
check against one host — so a cross-host redirect would spend that authorization
somewhere nobody approved, which on a gateway that can reach an internal network
is an SSRF. :class:`_SameHostRedirectHandler` refuses any redirect that changes
the host or leaves https, for every caller.

**No transfer is unbounded.** The staging file is written as bytes arrive, so a
body that never ends fills the disk before the end-of-stream digest can reject
it. Every transfer therefore carries a ceiling: *size* when a manifest declares
the exact length, *max_bytes* when only a bound is known, and
:data:`DEFAULT_MAX_BYTES` when a caller states neither.

**The staging file is opened without following a symlink.** The staging path is
derived from the target, which can sit in a directory something else may write —
so a symlink planted there would redirect the append onto whatever it points at.
The open refuses a symlink outright.
"""

from __future__ import annotations

import hashlib
import logging
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from kiro_crew import platform_compat
from kiro_crew._ssl_compat import _ssl_context_has_ca_trust

logger = logging.getLogger(__name__)

#: Default per-request timeout. Generous because the first caller pulls 610MB:
#: a slow link must retry under the CALLER's backoff, not die mid-transfer.
DEFAULT_TIMEOUT_SECS = 1800

#: Read size. One MiB is large enough that the per-chunk Python overhead is
#: noise against the socket read, and small enough that a rate limiter can pace
#: to a few hundred KiB/s without overshooting a whole second.
DEFAULT_CHUNK_BYTES = 1 << 20

#: How often ``on_progress`` fires, in bytes. A progress callback that ran per
#: chunk would write a status dict a thousand times for a 1GB file.
DEFAULT_PROGRESS_EVERY_BYTES = 16 << 20

#: Suffix of the resumable staging file. Stable (no pid) BECAUSE resume is the
#: point: a partial from a previous process must be recognizable by the next one.
#: A caller that does not want cross-process reuse passes its own *staging* path.
PART_SUFFIX = ".part"

#: Ceiling applied when a caller declares neither an exact *size* nor a
#: *max_bytes*. Generous — the largest thing Kiro Crew fetches is a ~610MB model —
#: because its job is only to make "unbounded" unreachable: bytes are written as
#: they arrive, so without a ceiling an endless body fills the disk long before the
#: end-of-stream digest gets to reject it. A caller that knows its payload passes a
#: real bound and gets a far tighter guarantee.
DEFAULT_MAX_BYTES = 8 * 1024 * 1024 * 1024

_SSL_CA_PATHS = (
    "/etc/pki/tls/certs/ca-bundle.crt",  # AL2, RHEL, CentOS
    "/etc/ssl/certs/ca-certificates.crt",  # Debian/Ubuntu
    "/etc/ssl/cert.pem",  # macOS, Alpine
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",  # Fedora
)

#: HTTP status for a satisfied ``Range`` request. Anything else on a ranged
#: request means the server ignored the range and is sending the whole body.
_HTTP_PARTIAL_CONTENT = 206


def make_ssl_context() -> ssl.SSLContext:
    """An SSL context that finds system CA certs on all supported platforms.

    Bundled Python runtimes (like the desktop backend's interpreter) may not ship
    their own CA bundle and rely on ``load_default_certs()``, which calls
    OpenSSL's compiled-in defaults — and those can miss when the compiled path
    does not match the host OS (common on AL2 with a cross-compiled Python).
    """
    ctx = ssl.create_default_context()
    try:
        ctx.load_default_certs()
        if _ssl_context_has_ca_trust(ctx):
            return ctx
    except ssl.SSLError:
        pass
    for path in _SSL_CA_PATHS:
        if os.path.isfile(path):
            ctx.load_verify_locations(cafile=path)
            return ctx
    # Last resort: honour SSL_CERT_FILE / SSL_CERT_DIR from the environment.
    return ctx


class _SameHostRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse any redirect that leaves https or changes the host.

    The authorization to make a request is granted per HOST — a manifest's signed
    ``cdn_base``, or an operator's https-only override. ``urlopen`` follows a
    redirect without re-asking, so the default behaviour spends that grant on a
    destination the CDN chose: on a gateway that can route to an internal network,
    that is a blind SSRF with the response fed straight back into the caller.

    Same-host redirects stay allowed (a CDN legitimately reshapes its own paths).
    Anything else raises, which every caller here already treats as a transport
    failure — the fail-safe direction.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        try:
            target = urllib.parse.urlsplit(newurl)
        except ValueError:
            return None
        if target.scheme != "https":
            return None
        origin = urllib.parse.urlsplit(req.full_url)
        if (target.hostname or "").lower() != (origin.hostname or "").lower():
            logger.warning(
                "refusing a cross-host redirect from %s to %s",
                redact_url(req.full_url),
                redact_url(newurl),
            )
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_opener(context: "ssl.SSLContext | None" = None) -> urllib.request.OpenerDirector:
    """An opener that verifies TLS and refuses cross-host redirects.

    The ONE way this package makes an outbound asset/manifest request. A caller
    reaching for ``urllib.request.urlopen`` directly gets urllib's default
    redirect handler back, which is the SSRF this exists to close.
    """
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context or make_ssl_context()),
        _SameHostRedirectHandler,
    )


def redact_url(url: str) -> str:
    """Return *url* safe for logs: scheme and host only.

    Every other component can carry a credential. Userinfo and a signed query
    string are the obvious ones (a presigned URL is itself a credential), and the
    PATH is the one that looks safe and is not: a private mirror can put a token
    in a path segment, and this string goes to the gateway log on every transfer.

    The cost is that a log line does not name which file was fetched. That is
    covered: every caller passes a *label* (``feature-video clip <id>``), which is
    what a reader actually needs, and it is not attacker-controlled.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urllib.parse.urlunsplit((parts.scheme, host, "", "", ""))
    except Exception:
        return "<unparseable-url>"


def _sha256_prefix(path: Path, length: int) -> "hashlib._Hash | None":
    """Hash the first *length* bytes of *path*, or None if it cannot be read.

    Used only on the resume path: the digest of a partial transfer has to be
    rebuilt from disk, because the process that wrote those bytes is gone.
    Returning None (rather than raising) lets the caller fall back to a fresh
    download, which is always correct and only costs bandwidth.
    """
    h = hashlib.sha256()
    remaining = length
    try:
        with path.open("rb") as f:
            while remaining > 0:
                chunk = f.read(min(1 << 20, remaining))
                if not chunk:
                    return None
                h.update(chunk)
                remaining -= len(chunk)
    except OSError:
        return None
    return h


class StagingRefused(OSError):
    """The staging path itself was rejected before any transfer began.

    A distinct type because the handler treats it differently from a transport
    error: this message names a LOCAL path and nothing else, so it is returned
    verbatim, while a transport error's message can carry the url (and any
    credential in it) and is reduced to its type. Telling "something planted a
    symlink in your cache" apart from "the network failed" is the whole diagnostic
    value here, and a redacted message would erase it.
    """


def _open_staging_nofollow(staging: Path, *, append: bool):
    """Open *staging* for writing, refusing to follow a symlink. Raises ``OSError``.

    The staging path is derived from the target, so it lives wherever the target
    does — a directory something other than this process may be able to write. A
    symlink planted there would send the append (or the truncating create) to
    whatever it points at, which turns a download into an arbitrary-file write.

    Two layers, because neither alone covers both platforms: ``O_NOFOLLOW`` makes
    the kernel refuse a symlink at open time on POSIX, and the ``is_symlink``
    pre-check covers Windows, where the flag does not exist. The pre-check alone
    would be a TOCTOU race; the flag alone would silently do nothing on Windows.
    """
    if staging.is_symlink():
        raise StagingRefused(f"refusing to write through a symlinked staging path: {staging}")
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    # 0o600: the payload may be owner-only, and a staging file that is briefly
    # world-readable is the same exposure as the installed one being so.
    return os.fdopen(os.open(staging, flags, 0o600), "ab" if append else "wb")


def _install(staging: Path, path: Path, *, restrict_to_owner: bool) -> None:
    """Move the verified staging file onto *path* atomically.

    Lockdown happens BEFORE the rename, so the payload is never readable at its
    final name under the inherited umask/DACL — the same ordering
    ``atomic_write(restrict_to_owner=True)`` uses, and the reason this is not a
    ``chmod`` after ``os.replace``.
    """
    if restrict_to_owner:
        try:
            platform_compat.restrict_to_owner(staging)
        except OSError:
            # Warn-and-continue, matching atomic_write's "warn" posture: a
            # lockdown failure must not lose an otherwise verified download,
            # but it must be visible.
            logger.warning("could not restrict %s to its owner", staging, exc_info=True)
    os.replace(staging, path)


def download_to(
    path: Path,
    url: str,
    *,
    sha256: str,
    size: int = 0,
    max_bytes: int = 0,
    min_bytes: int = 0,
    resume: bool = False,
    rate_limit_bytes_per_s: int = 0,
    staging: Path | None = None,
    timeout_secs: int = DEFAULT_TIMEOUT_SECS,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    progress_every_bytes: int = DEFAULT_PROGRESS_EVERY_BYTES,
    on_progress: Callable[[int, int], None] | None = None,
    on_verifying: Callable[[], None] | None = None,
    restrict_to_owner: bool = False,
    label: str = "asset",
    error_prefix: str = "HTTPS download failed",
) -> tuple[bool, str]:
    """Fetch *url* into *path*, verified against *sha256*. Blocking.

    Returns ``(True, "")`` once the verified bytes are in place, else
    ``(False, <reason>)``. Never raises: a caller running this on a background
    thread has no place to handle an exception, and every failure here is one it
    should retry or report rather than crash on.

    * *sha256* is required. The digest is computed while streaming, so a
      complete-but-corrupt transfer costs no second pass over the file.
    * *size*, when known from a manifest, bounds the transfer: a body longer than
      *size* is abandoned rather than written to the end of the disk, and a
      partial larger than *size* is discarded instead of resumed.
    * *max_bytes* is the ceiling for a payload whose exact length is NOT declared —
      a poster, say, where the manifest carries a sha but no byte count. It bounds
      the transfer without claiming to know the length, so it never discards a
      resumable partial. With neither given the ceiling is
      :data:`DEFAULT_MAX_BYTES`: the guard exists because bytes are written as they
      arrive, so an endless body would fill the disk before the digest could
      reject it.
    * *min_bytes* is a floor for the "the CDN served us an error page" case,
      where a small body can still hash consistently across attempts.
    * *resume* sends a ``Range`` request when a staging file is already present.
      A server that ignores the range (answering 200) restarts the transfer from
      zero, which is why the digest is only ever trusted end to end.
    * *rate_limit_bytes_per_s* paces the read loop, so a background fetch does
      not take the user's link. It bounds THIS transfer only — a caller running
      several at once is doing its own budgeting.
    """
    if not sha256:
        return False, f"{error_prefix}: no sha256 pin for {label}"
    if not url.lower().startswith("https://"):
        # Belt-and-braces: every caller resolves its url through its own
        # https-only gate, but this function installs whatever it fetched, so it
        # refuses plaintext (and file://, which would read a local path) itself.
        return False, f"{error_prefix}: refusing a non-https url"

    # size (exact) beats max_bytes (a bound) beats the module default; the result
    # is never 0, so no transfer runs without a ceiling.
    ceiling = size or max_bytes or DEFAULT_MAX_BYTES
    target_staging = staging or path.parent / f"{path.name}{PART_SUFFIX}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"{error_prefix}: {exc}"

    offset = 0
    digest: "hashlib._Hash | None" = None
    if resume and target_staging.is_file():
        try:
            partial = target_staging.stat().st_size
        except OSError:
            partial = 0
        if partial and not (size and partial >= size):
            digest = _sha256_prefix(target_staging, partial)
            if digest is not None:
                offset = partial
        if offset == 0:
            # A partial we cannot hash, or one at/over the expected size (so the
            # bytes on disk are not a prefix of what we want), is worthless.
            target_staging.unlink(missing_ok=True)
    elif not resume:
        # A stale staging file from an abandoned attempt would otherwise be
        # appended to by the "ab" open below on a caller that never resumes.
        target_staging.unlink(missing_ok=True)

    if digest is None or offset == 0:
        digest = hashlib.sha256()
        offset = 0

    try:
        request = urllib.request.Request(url, method="GET")
        if offset:
            request.add_header("Range", f"bytes={offset}-")
        logger.info("Downloading %s from %s", label, redact_url(url))
        opener = build_opener()
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- https is enforced above, redirects are host-pinned, and the payload is sha256-pinned
        with opener.open(request, timeout=timeout_secs) as resp:
            status = int(getattr(resp, "status", 0) or 0)
            if offset and status != _HTTP_PARTIAL_CONTENT:
                # The server ignored our Range and is sending the whole body.
                # Start over rather than appending it to the prefix we hold.
                logger.info("%s: server ignored the range request; restarting", label)
                offset = 0
                digest = hashlib.sha256()
            declared = int(resp.headers.get("Content-Length", 0) or 0)
            total = (offset + declared) if declared else (size or max_bytes)
            downloaded = offset
            this_run = 0
            started = time.monotonic()
            overflow = 0
            with _open_staging_nofollow(target_staging, append=bool(offset)) as out:
                while True:
                    chunk = resp.read(chunk_bytes)
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    downloaded += len(chunk)
                    this_run += len(chunk)
                    if downloaded > ceiling:
                        # Recorded and broken out of — NOT unlinked here. The write
                        # handle is still open on this line, and Windows refuses to
                        # unlink an open file, so the unlink would raise WinError 32
                        # and the transport handler below would answer with that OS
                        # error instead of the ceiling refusal the caller needs.
                        overflow = downloaded
                        break
                    if on_progress is not None and this_run % progress_every_bytes < chunk_bytes:
                        on_progress(downloaded, total)
                    if rate_limit_bytes_per_s > 0:
                        owed = this_run / rate_limit_bytes_per_s - (time.monotonic() - started)
                        if owed > 0:
                            time.sleep(owed)
            if overflow:
                # The staging file is closed by now: the `with` above has exited.
                target_staging.unlink(missing_ok=True)
                return False, (
                    f"{error_prefix}: body longer than the {ceiling}-byte "
                    f"ceiling (got {overflow})"
                )
        if on_verifying is not None:
            on_verifying()
        got = digest.hexdigest()
        if got != sha256:
            target_staging.unlink(missing_ok=True)
            return False, (
                f"sha256 mismatch: got {got[:16]}…, expected {sha256[:16]}… (corrupt download)"
            )
        actual = target_staging.stat().st_size
        if min_bytes and actual < min_bytes:
            # Size read BEFORE the unlink: reading it after would raise inside
            # the message and surface a generic error instead of the real reason.
            target_staging.unlink(missing_ok=True)
            return False, f"downloaded file too small ({actual} bytes)"
        _install(target_staging, path, restrict_to_owner=restrict_to_owner)
        return True, ""
    except StagingRefused as exc:
        # Message kept verbatim: it names a local path, never the url. Listed BEFORE
        # the transport branch because it is an OSError and would otherwise be
        # reduced to "OSError", losing the one thing a reader needs to know.
        if not resume:
            target_staging.unlink(missing_ok=True)
        return False, f"{error_prefix}: {exc}"
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        if not resume:
            target_staging.unlink(missing_ok=True)
        # TYPE and a redacted url, never `exc`. This string is RETURNED, and the
        # feature-video caller puts it in the cache's failure state, which /status
        # serves to the dashboard — so an unredacted url here is a wider leak than a
        # log line. `http.client.InvalidURL` carries the url verbatim in its message
        # ("nonnumeric port: 'secretpw@…'"), which is how a credentialed override
        # reaches a reader. The type is the diagnostic that matters anyway.
        return False, f"{error_prefix}: {type(exc).__name__} from {redact_url(url)}"
    except Exception as exc:
        # A resumable partial is KEPT on a transport failure — that is the whole
        # point of resume — but discarded for a non-resuming caller so a stale
        # prefix cannot be mistaken for a fresh attempt.
        if not resume:
            target_staging.unlink(missing_ok=True)
        # No `exc_info=True`: a traceback renders the exception's own `str()`, which
        # for `InvalidURL` is the credential-bearing url this line exists to keep
        # out. Redacting the message while dumping the traceback beside it would
        # relocate the leak, not close it. The catch-all is where url-bearing
        # exceptions land when they are not one of the three types above.
        logger.warning("%s download from %s failed: %s", label, redact_url(url), type(exc).__name__)
        return False, f"{error_prefix}: {type(exc).__name__} from {redact_url(url)}"


__all__ = [
    "DEFAULT_CHUNK_BYTES",
    "StagingRefused",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_PROGRESS_EVERY_BYTES",
    "DEFAULT_TIMEOUT_SECS",
    "PART_SUFFIX",
    "build_opener",
    "download_to",
    "make_ssl_context",
    "redact_url",
]
