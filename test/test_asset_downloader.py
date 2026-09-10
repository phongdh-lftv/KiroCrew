"""Unit tests for kiro_crew.asset_downloader — the shared verified-transfer engine.

No network: ``build_opener`` is replaced with a fake opener that streams bytes from
memory and honours (or deliberately ignores) a ``Range`` header, which is the only
way to exercise resume without a real partial-content server. The seam is the
opener rather than ``urlopen`` because that is what the module calls -- every
request goes through an opener carrying the same-host redirect handler, and a test
that patched ``urlopen`` would bypass the thing under test.

The properties under test are the ones the callers depend on: nothing reaches the
final path unverified, a partial is a staging file nobody serves, and a failure
answers with a reason rather than raising into a background thread.
"""

from __future__ import annotations

import hashlib
import stat
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import asset_downloader as dl

#: The real ``build_opener``, captured before the autouse no-network fixture
#: replaces it -- the handler-wiring test is about the real one.
_REAL_BUILD_OPENER = dl.build_opener

_PAYLOAD = b"".join(bytes([i % 251]) for i in range(4096))
_SHA = hashlib.sha256(_PAYLOAD).hexdigest()
_URL = "https://cdn.example.com/feature-videos/0.6.0/clip.mp4"


class _FakeResponse:
    def __init__(self, data: bytes, *, status: int, total: int) -> None:
        self._data = data
        self._pos = 0
        self.status = status
        self.headers = {"Content-Length": str(total)}

    def read(self, n: int) -> bytes:
        chunk = self._data[self._pos : self._pos + n]
        self._pos += n
        return chunk

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _EndlessResponse:
    """A response whose body never ends -- the disk-fill shape."""

    status = 200
    headers: dict = {}

    def read(self, n: int) -> bytes:
        return b"\x00" * n

    def __enter__(self) -> "_EndlessResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _fake_urlopen(
    payload: bytes = _PAYLOAD,
    *,
    honour_range: bool = True,
    fail_first: bool = False,
    endless: bool = False,
):
    """Build a ``build_opener`` replacement, plus a state object recording what it saw.

    *endless* streams forever, which is how the disk-fill ceiling is tested: there
    is no other way to reach it, since a bounded payload always ends first.
    """
    state = SimpleNamespace(calls=0, ranges=[], urls=[])

    def _open(request, timeout=None):  # noqa: ANN001 - OpenerDirector.open signature
        state.calls += 1
        state.urls.append(getattr(request, "full_url", str(request)))
        rng = request.get_header("Range") if hasattr(request, "get_header") else None
        state.ranges.append(rng)
        if fail_first and state.calls == 1:
            raise urllib.error.URLError("fake network unreachable")
        if endless:
            return _EndlessResponse()
        if rng and honour_range:
            offset = int(str(rng).split("=", 1)[1].rstrip("-"))
            return _FakeResponse(payload[offset:], status=206, total=len(payload) - offset)
        return _FakeResponse(payload, status=200, total=len(payload))

    return SimpleNamespace(open=_open), state


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any test that forgets to install a fake must fail, not reach the network."""

    def _blocked(*_a: object, **_k: object) -> None:
        raise urllib.error.URLError("blocked by test fixture")

    monkeypatch.setattr(
        "kiro_crew.asset_downloader.build_opener",
        lambda *a, **k: SimpleNamespace(open=_blocked),
    )
    monkeypatch.setattr("kiro_crew.asset_downloader.urllib.request.urlopen", _blocked)


class TestHappyPath:
    def test_installs_the_verified_payload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        open_fn, state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clips" / "clip.mp4"
        ok, err = dl.download_to(target, _URL, sha256=_SHA)
        assert (ok, err) == (True, "")
        assert target.read_bytes() == _PAYLOAD
        assert state.calls == 1
        # Nothing left staged: the install is one os.replace off a temp name.
        assert [p.name for p in target.parent.iterdir()] == ["clip.mp4"]

    def test_reports_progress_and_verification(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        open_fn, _state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        seen: list[tuple[int, int]] = []
        verifying: list[bool] = []
        ok, _err = dl.download_to(
            tmp_path / "clip.mp4",
            _URL,
            sha256=_SHA,
            chunk_bytes=512,
            progress_every_bytes=1024,
            on_progress=lambda done, total: seen.append((done, total)),
            on_verifying=lambda: verifying.append(True),
        )
        assert ok is True
        assert verifying == [True]
        assert seen and seen[-1][0] <= len(_PAYLOAD)
        assert all(total == len(_PAYLOAD) for _done, total in seen)

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX mode bits")
    def test_restrict_to_owner_locks_the_installed_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        open_fn, _state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        assert dl.download_to(target, _URL, sha256=_SHA, restrict_to_owner=True)[0] is True
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


class TestRefusals:
    def test_a_non_https_url_is_refused_without_a_request(self, tmp_path: Path) -> None:
        ok, err = dl.download_to(
            tmp_path / "clip.mp4", "http://cdn.example.com/clip.mp4", sha256=_SHA
        )
        assert ok is False
        assert "non-https" in err
        assert not (tmp_path / "clip.mp4").exists()

    def test_a_file_url_is_refused(self, tmp_path: Path) -> None:
        ok, _err = dl.download_to(tmp_path / "clip.mp4", "file:///etc/passwd", sha256=_SHA)
        assert ok is False

    def test_no_sha_pin_is_refused(self, tmp_path: Path) -> None:
        ok, err = dl.download_to(tmp_path / "clip.mp4", _URL, sha256="")
        assert ok is False
        assert "no sha256 pin" in err

    def test_sha_mismatch_installs_nothing_and_cleans_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        open_fn, _state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        ok, err = dl.download_to(target, _URL, sha256="0" * 64)
        assert ok is False
        assert "sha256 mismatch" in err
        assert not target.exists()
        assert list(tmp_path.iterdir()) == []

    def test_a_body_longer_than_the_declared_size_is_abandoned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A manifest states the size; a longer body is a lie, not a bigger file."""
        open_fn, _state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        ok, err = dl.download_to(target, _URL, sha256=_SHA, size=100, chunk_bytes=64)
        assert ok is False
        assert "ceiling" in err
        assert not target.exists()

    def test_a_too_small_payload_names_the_real_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        open_fn, _state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        ok, err = dl.download_to(target, _URL, sha256=_SHA, min_bytes=len(_PAYLOAD) + 1)
        assert ok is False
        assert "too small" in err
        assert not target.exists()

    def test_a_transport_failure_reports_it_and_leaves_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        open_fn, _state = _fake_urlopen(fail_first=True)
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        ok, err = dl.download_to(target, _URL, sha256=_SHA)
        assert ok is False
        assert "HTTPS download failed" in err
        assert list(tmp_path.iterdir()) == []


class TestResume:
    def _part(self, target: Path, prefix: int) -> Path:
        part = target.parent / f"{target.name}{dl.PART_SUFFIX}"
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(_PAYLOAD[:prefix])
        return part

    def test_continues_from_a_partial_and_still_verifies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        open_fn, state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        self._part(target, 1000)
        ok, err = dl.download_to(target, _URL, sha256=_SHA, size=len(_PAYLOAD), resume=True)
        assert (ok, err) == (True, "")
        assert target.read_bytes() == _PAYLOAD
        assert state.ranges == ["bytes=1000-"]

    def test_a_server_ignoring_the_range_restarts_rather_than_appending(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 200 answer to a ranged request must not be appended to the prefix."""
        open_fn, state = _fake_urlopen(honour_range=False)
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        self._part(target, 1000)
        ok, err = dl.download_to(target, _URL, sha256=_SHA, resume=True)
        assert (ok, err) == (True, "")
        assert target.read_bytes() == _PAYLOAD
        assert state.ranges == ["bytes=1000-"]

    def test_a_partial_at_or_past_the_declared_size_is_discarded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Those bytes are not a PREFIX of the wanted file, so resuming them is wrong."""
        open_fn, state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        part = target.parent / f"{target.name}{dl.PART_SUFFIX}"
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(b"x" * (len(_PAYLOAD) + 10))
        ok, _err = dl.download_to(target, _URL, sha256=_SHA, size=len(_PAYLOAD), resume=True)
        assert ok is True
        assert target.read_bytes() == _PAYLOAD
        assert state.ranges == [None]

    def test_a_transport_failure_keeps_the_partial_for_the_next_attempt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        open_fn, _state = _fake_urlopen(fail_first=True)
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        part = self._part(target, 1000)
        ok, _err = dl.download_to(target, _URL, sha256=_SHA, resume=True)
        assert ok is False
        assert part.is_file() and part.stat().st_size == 1000

    def test_a_corrupt_partial_fails_verification_rather_than_installing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The digest is end to end, so wrong bytes in the prefix cannot slip through."""
        open_fn, _state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        part = target.parent / f"{target.name}{dl.PART_SUFFIX}"
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(b"\x00" * 1000)  # right length, wrong content
        ok, err = dl.download_to(target, _URL, sha256=_SHA, size=len(_PAYLOAD), resume=True)
        assert ok is False
        assert "sha256 mismatch" in err
        assert not target.exists()

    def test_a_non_resuming_caller_ignores_and_replaces_a_stale_staging_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        open_fn, state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        staging = tmp_path / ".clip.mp4.tmp"
        staging.write_bytes(b"junk from a dead process")
        ok, _err = dl.download_to(target, _URL, sha256=_SHA, staging=staging, resume=False)
        assert ok is True
        assert target.read_bytes() == _PAYLOAD
        assert state.ranges == [None]


class TestRateLimit:
    def test_pacing_sleeps_and_the_payload_is_unaffected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        open_fn, _state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        slept: list[float] = []
        monkeypatch.setattr(dl.time, "sleep", lambda s: slept.append(s))
        target = tmp_path / "clip.mp4"
        # 1 KiB/s over 4 KiB of payload: every chunk owes time it has not spent.
        ok, _err = dl.download_to(
            target, _URL, sha256=_SHA, rate_limit_bytes_per_s=1024, chunk_bytes=512
        )
        assert ok is True
        assert target.read_bytes() == _PAYLOAD
        assert slept and sum(slept) > 0

    def test_no_limit_means_no_sleeping(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        open_fn, _state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        slept: list[float] = []
        monkeypatch.setattr(dl.time, "sleep", lambda s: slept.append(s))
        assert dl.download_to(tmp_path / "clip.mp4", _URL, sha256=_SHA, chunk_bytes=512)[0] is True
        assert slept == []


class TestRedaction:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://user:pw@cdn.example.com/a/b.mp4", "https://cdn.example.com"),
            ("https://cdn.example.com/a.mp4?X-Amz-Signature=deadbeef", "https://cdn.example.com"),
            ("https://cdn.example.com/a.mp4#frag", "https://cdn.example.com"),
            ("https://cdn.example.com:8443/a.mp4", "https://cdn.example.com:8443"),
            ("https://cdn.example.com/t0ken-in-a-path/a.mp4", "https://cdn.example.com"),
        ],
    )
    def test_only_scheme_and_host_survive(self, raw: str, expected: str) -> None:
        """The PATH is dropped too -- a mirror can put a token in a path segment.

        Userinfo and a query string are the obvious credential carriers; the path
        is the one that looks safe and is not, and this string reaches the gateway
        log on every transfer. What a reader needs instead is the caller's *label*.
        """
        assert dl.redact_url(raw) == expected


class TestSameHostRedirects:
    """A url is authorized against ONE host, so a redirect may not change it."""

    def _handler_verdict(self, origin: str, target: str) -> object:
        handler = dl._SameHostRedirectHandler()
        request = urllib.request.Request(origin)
        # redirect_request returns a new Request to follow, or None to refuse.
        return handler.redirect_request(request, None, 302, "Found", {}, target)

    def test_a_same_host_redirect_is_followed(self) -> None:
        verdict = self._handler_verdict(
            "https://cdn.example.com/a/clip.mp4", "https://cdn.example.com/b/clip.mp4"
        )
        assert verdict is not None

    @pytest.mark.parametrize(
        "target",
        [
            "https://attacker.example/clip.mp4",
            "https://169.254.169.254/latest/meta-data/",
            "https://127.0.0.1:5476/api/sessions",
            "http://cdn.example.com/clip.mp4",
            "https://cdn.example.com.attacker.example/clip.mp4",
        ],
    )
    def test_a_cross_host_or_plaintext_redirect_is_refused(self, target: str) -> None:
        assert self._handler_verdict("https://cdn.example.com/clip.mp4", target) is None

    def test_the_opener_carries_the_handler(self) -> None:
        """Every request goes through this opener; urlopen's default would not."""
        opener = _REAL_BUILD_OPENER()
        assert any(isinstance(h, dl._SameHostRedirectHandler) for h in opener.handlers)


class TestCeiling:
    """Bytes are written as they arrive, so an endless body must be abandoned."""

    def test_an_endless_body_is_abandoned_at_the_explicit_ceiling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        open_fn, _state = _fake_urlopen(endless=True)
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        ok, err = dl.download_to(target, _URL, sha256=_SHA, max_bytes=4096, chunk_bytes=512)
        assert ok is False
        assert "ceiling" in err
        assert not target.exists()
        assert list(tmp_path.iterdir()) == []

    def test_the_ceiling_refusal_survives_a_locked_staging_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal names the ceiling, not the OS.

        The staging file is unlinked only after the write handle closes. Unlinking it
        while open raises WinError 32 on Windows, and the transport handler would then
        answer with that OS error instead of the ceiling refusal. Asserting on the
        MESSAGE is what makes this test fail on Windows if the unlink moves back
        inside the open block.
        """
        open_fn, _state = _fake_urlopen(endless=True)
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        ok, err = dl.download_to(target, _URL, sha256=_SHA, max_bytes=64, chunk_bytes=32)
        assert ok is False
        assert "ceiling" in err
        assert "WinError" not in err
        assert "being used by another process" not in err
        assert not target.exists()
        assert list(tmp_path.iterdir()) == []

    def test_a_caller_that_declares_nothing_still_has_a_ceiling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No transfer is unbounded -- DEFAULT_MAX_BYTES applies when nothing is given."""
        open_fn, _state = _fake_urlopen(endless=True)
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        monkeypatch.setattr(dl, "DEFAULT_MAX_BYTES", 2048)
        ok, err = dl.download_to(tmp_path / "clip.mp4", _URL, sha256=_SHA, chunk_bytes=512)
        assert ok is False
        assert "ceiling" in err

    def test_max_bytes_does_not_discard_a_resumable_partial(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It is a BOUND, not a declared length, so it cannot mean "already complete"."""
        open_fn, state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        part = tmp_path / f"clip.mp4{dl.PART_SUFFIX}"
        part.write_bytes(_PAYLOAD[:1000])
        ok, _err = dl.download_to(
            target, _URL, sha256=_SHA, max_bytes=len(_PAYLOAD) * 4, resume=True
        )
        assert ok is True
        assert state.ranges == ["bytes=1000-"]


class TestStagingSymlink:
    """The staging path sits wherever the target does, so it may be plantable."""

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlink semantics")
    def test_a_symlinked_staging_path_is_refused_without_writing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        open_fn, _state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        victim = tmp_path / "protected.json"
        victim.write_bytes(b"do not touch")
        target = tmp_path / "clip.mp4"
        (tmp_path / f"clip.mp4{dl.PART_SUFFIX}").symlink_to(victim)
        ok, err = dl.download_to(target, _URL, sha256=_SHA, resume=True)
        assert ok is False
        assert "symlink" in err.lower()
        assert victim.read_bytes() == b"do not touch"
        assert not target.exists()

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlink semantics")
    def test_a_fresh_download_discards_a_planted_symlink_instead_of_following_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-resuming caller unlinks the staging path first, which is the safe act.

        ``unlink`` removes the LINK, never its target, so the plant is destroyed
        rather than written through and the transfer proceeds normally. Pinned
        because the opposite -- opening the stale path -- is the bug, and because
        this branch cannot be reached by the resume test above.
        """
        open_fn, _state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        victim = tmp_path / "protected.json"
        victim.write_bytes(b"do not touch")
        staging = tmp_path / "staged.tmp"
        staging.symlink_to(victim)
        target = tmp_path / "clip.mp4"
        ok, err = dl.download_to(target, _URL, sha256=_SHA, staging=staging, resume=False)
        assert (ok, err) == (True, "")
        assert victim.read_bytes() == b"do not touch"
        assert target.read_bytes() == _PAYLOAD
