"""Tests for the HOSTED half of feature videos: signed manifest, cache, serving.

Signatures here are real. Keys and signing go through
``test/feature_video_fixture.py``, which mints a throwaway RSA pair per test and
repoints ``feed_trust``'s pins at it, so the positive path is exercised with genuine
openssl verification rather than a stubbed "it verified" — a stub would pass for a
manifest nobody signed, which is the one failure this module exists to prevent.

:class:`TestSharedFixture` additionally verifies the COMMITTED fixture at
``test/fixtures/feature-videos/manifest.json``. That artifact is the cross-tool
contract: the publishing tool's tests verify the same bytes, so the signed byte
format is pinned in one place instead of in two helpers that can drift.

Network is faked throughout: ``urllib.request.urlopen`` is replaced per test, in
whichever module owns the request (``feature_videos_manifest`` for the manifest,
``asset_downloader`` for media, ``feature_videos`` for the remote probe).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import http.client
import json
import os
import stat
import subprocess
import sys
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import feature_video_fixture as fixture
import pytest
from aiohttp import web

from kiro_crew import feature_videos as fv
from kiro_crew import feature_videos_cache as cache_mod
from kiro_crew import feature_videos_manifest as manifest_mod
from kiro_crew.platform import feed_trust

_CLIP = b"clip-bytes" * 64
_POSTER = b"poster-bytes" * 8
_CLIP_SHA = hashlib.sha256(_CLIP).hexdigest()
_POSTER_SHA = hashlib.sha256(_POSTER).hexdigest()
_CDN = "https://cdn.example.com/feature-videos"
_CDN_HOST = "cdn.example.com"


# ── fixtures ──


@pytest.fixture(autouse=True)
def _fresh_cache() -> "object":
    cache_mod.reset_feature_video_cache()
    yield None
    cache_mod.reset_feature_video_cache()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test that forgets to install a fake must fail, never reach the network."""

    def _blocked(*_a: object, **_k: object) -> None:
        raise urllib.error.URLError("blocked by test fixture")

    # build_opener is the one seam all three clients share now (the manifest fetch,
    # the media transfer and the remote probe all route through it, so they cannot
    # differ on redirect policy). urlopen stays blocked too, so a test that reaches
    # for the old path fails rather than escaping to the network.
    monkeypatch.setattr(
        "kiro_crew.asset_downloader.build_opener",
        lambda *a, **k: SimpleNamespace(open=_blocked),
    )
    for module in (
        "kiro_crew.feature_videos_manifest",
        "kiro_crew.asset_downloader",
        "kiro_crew.feature_videos",
    ):
        monkeypatch.setattr(f"{module}.urllib.request.urlopen", _blocked)


@pytest.fixture(scope="session")
def _throwaway_pair(tmp_path_factory: pytest.TempPathFactory) -> "tuple[Path, Path]":
    """One throwaway RSA pair for the whole session.

    Session-scoped because a 3072-bit keygen is ~0.5s and a dozen tests want a
    signing key: minting per test spent most of this file's runtime on openssl.
    The PINNING stays per-test (below), so no test inherits another's patch.
    """
    return fixture.mint_throwaway_key(tmp_path_factory.mktemp("fv-signing-key"))


@pytest.fixture()
def signing_key(_throwaway_pair: "tuple[Path, Path]", monkeypatch: pytest.MonkeyPatch) -> Path:
    """The session key, with ``feed_trust``'s pins repointed at it for this test.

    Both halves come from ``feature_video_fixture``, which is also what the
    publishing tool's tests use — one helper, so a key minted here and a key minted
    there cannot differ in a way that hides an encoding disagreement.
    """
    private, public = _throwaway_pair
    fixture.pin_fixture_key(monkeypatch, public)
    return private


def _entry(video_id: str = "hosted-clip", **overrides: object) -> dict:
    row: dict = {
        "id": video_id,
        "feature": video_id,
        "title": "A hosted clip",
        "description": "One or two plain sentences.",
        "file": f"{video_id}.mp4",
        "poster": f"{video_id}.jpg",
        "sha256": _CLIP_SHA,
        "poster_sha256": _POSTER_SHA,
        "bytes": len(_CLIP),
        "duration_s": 18.0,
        "doc": "feature-tips.md",
        "used_when": [],
        "min_version": "",
    }
    row.update(overrides)
    return row


def _document(
    release: str = "0.6.0", entries: "list[dict] | None" = None, **overrides: object
) -> dict:
    doc: dict = {
        "schema": manifest_mod.MANIFEST_SCHEMA,
        "release": release,
        "cdn_base": _CDN,
        "generated_at": "2026-09-10T00:00:00Z",
        "entries": entries if entries is not None else [_entry()],
    }
    doc.update(overrides)
    return doc


def _sign(private: Path, tmp_path: Path, document: dict) -> dict:
    """Attach a real signature over the canonical payload."""
    return fixture.sign_document(private, document, tmp_path)


def _seed(manifest: manifest_mod.VideoManifest) -> manifest_mod.VideoManifest:
    """Put *manifest* straight into the cache singleton's memory.

    Bypasses the loader on purpose: the selection tests are about what happens
    once a manifest is in force, and ``TestManifestOnDisk`` covers how one gets
    there (including that it is re-verified on the way).
    """
    cache_mod.feature_video_cache()._manifest = manifest
    return manifest


def _parsed(
    release: str = "0.6.0", entries: "list[dict] | None" = None
) -> manifest_mod.VideoManifest:
    parsed = manifest_mod.parse_manifest(_document(release=release, entries=entries))
    assert parsed is not None
    return parsed


def _write_media(release: str, entry_id: str = "hosted-clip", *, clip: bytes = _CLIP) -> Path:
    folder = manifest_mod.ensure_cache_dir(release)
    (folder / f"{entry_id}.mp4").write_bytes(clip)
    (folder / f"{entry_id}.jpg").write_bytes(_POSTER)
    return folder


class TestSharedFixture:
    """The committed cross-tool fixture: ``test/fixtures/feature-videos/``.

    The publishing tool's tests verify these same bytes, so what is pinned here is
    the signed byte FORMAT rather than any authority. The private half is minted on
    demand instead of committed — see the header of
    ``test/fixtures/feature-videos/regenerate.py``.
    """

    @pytest.fixture()
    def pinned(self, monkeypatch: pytest.MonkeyPatch) -> str:
        return fixture.pin_fixture_key(monkeypatch)

    def test_the_committed_fixture_verifies_and_parses(self, pinned: str) -> None:
        manifest = manifest_mod.verified_manifest(fixture.load_fixture_manifest())
        assert manifest is not None
        assert manifest.release == "0.6.0"
        assert [e.id for e in manifest.entries] == ["fixture-clip", "fixture-clip-2"]
        assert manifest.cdn_host == "cdn.example.invalid"

    def test_the_non_ascii_entry_survives_the_round_trip(self, pinned: str) -> None:
        """The case that separates an ASCII-escaped canonical form from a UTF-8 one.

        Two tools that disagree on this produce a document that verifies on neither
        side — but ONLY for entries carrying non-ASCII text, so a fixture without
        one would look fine while the encodings differed.
        """
        manifest = manifest_mod.verified_manifest(fixture.load_fixture_manifest())
        assert manifest is not None
        assert "日本語" in manifest.entries[1].title

    def test_the_stored_indentation_is_not_what_is_signed(
        self, pinned: str, tmp_path: Path
    ) -> None:
        """Re-serializing the file changes its bytes and not its validity.

        The signature covers the CANONICAL form, so a publisher may write the file
        however it likes. Worth pinning: a verifier that hashed the file as stored
        would pass this fixture and fail every real release.
        """
        loaded = fixture.load_fixture_manifest()
        reserialized = json.loads(json.dumps(loaded, indent=8, sort_keys=False))
        assert json.dumps(reserialized) != json.dumps(loaded, indent=2, sort_keys=True)
        assert manifest_mod.verify_manifest(reserialized) is True

    def test_tampering_with_the_fixture_fails(self, pinned: str) -> None:
        doctored = fixture.load_fixture_manifest()
        doctored["cdn_base"] = "https://attacker.example/videos"
        assert manifest_mod.verify_manifest(doctored) is False

    def test_the_helpers_canonical_bytes_match_the_verifier(
        self, signing_key: Path, tmp_path: Path
    ) -> None:
        """The helper's local canonicalization is the verifier's, byte for byte.

        The helper deliberately does NOT import the verifier's copy: signing with
        the same function that verifies cannot detect the two disagreeing, which is
        the failure the whole fixture exists to catch. So the agreement is asserted
        instead — a signature the helper produced verifies, and one produced over
        any other byte form does not.
        """
        document = _document(entries=[_entry("canon", title="Ünïcödé")])
        assert manifest_mod.verify_manifest(_sign(signing_key, tmp_path, document)) is True
        # A UTF-8, unsorted, pretty-printed payload is the exact mistake a second
        # implementation makes; the same key over those bytes must not verify.
        wrong = tmp_path / "wrong.json"
        wrong.write_bytes(
            json.dumps(document, sort_keys=False, indent=2, ensure_ascii=False).encode("utf-8")
        )
        signature = subprocess.run(
            [fixture.openssl_or_skip(), "dgst", "-sha256", "-sign", str(signing_key), str(wrong)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ).stdout
        mis_signed = {**document, "signature": base64.b64encode(signature).decode("ascii")}
        assert manifest_mod.verify_manifest(mis_signed) is False

    def test_the_fixture_key_is_not_the_production_trust_root(self) -> None:
        """A test key must never be able to become the thing that grants trust.

        Asserted WITHOUT the pinning fixture, against the real committed pins: if
        someone ever pasted this key into ``feed_trust``, every fixture-signed
        document would verify on a shipped build.
        """
        assert fixture.key_id_of(fixture.PUBLIC_KEY_PATH) != feed_trust.PINNED_KEY_ID
        pem = fixture.PUBLIC_KEY_PATH.read_bytes()
        assert base64.b64encode(pem).decode("ascii") != feed_trust.PINNED_PUBLIC_KEY_B64

    def test_the_fixture_carries_no_private_key(self) -> None:
        """The SAST secrets gate is not something to add an exclusion for."""
        for path in fixture.FIXTURE_DIR.iterdir():
            assert "PRIVATE KEY" not in path.read_text(encoding="utf-8", errors="ignore")


# ── verification ──


class TestManifestVerification:
    def test_a_correctly_signed_manifest_verifies_and_parses(
        self, signing_key: Path, tmp_path: Path
    ) -> None:
        signed = _sign(signing_key, tmp_path, _document())
        manifest = manifest_mod.verified_manifest(signed)
        assert manifest is not None
        assert manifest.release == "0.6.0"
        assert [e.id for e in manifest.entries] == ["hosted-clip"]

    def test_tampering_with_one_entry_discards_the_WHOLE_manifest(
        self, signing_key: Path, tmp_path: Path
    ) -> None:
        """The signature covers the document, so a doctored entry invalidates all of it."""
        signed = _sign(signing_key, tmp_path, _document())
        signed["entries"][0]["sha256"] = "1" * 64
        assert manifest_mod.verify_manifest(signed) is False
        assert manifest_mod.verified_manifest(signed) is None

    def test_an_unsigned_manifest_is_refused(self, tmp_path: Path) -> None:
        assert manifest_mod.verified_manifest(_document()) is None

    def test_a_signature_from_another_key_is_refused(
        self, signing_key: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        other, _public = fixture.mint_throwaway_key(other_dir)
        assert manifest_mod.verify_manifest(_sign(other, tmp_path, _document())) is False

    def test_a_key_id_naming_another_key_is_refused(
        self, signing_key: Path, tmp_path: Path
    ) -> None:
        """key_id is optional, but a value that disagrees with the pin is a lie."""
        assert (
            manifest_mod.verify_manifest(
                _sign(signing_key, tmp_path, _document(key_id="sha256:" + "0" * 64))
            )
            is False
        )

    def test_a_matching_key_id_is_accepted_inside_the_signed_payload(
        self, signing_key: Path, tmp_path: Path
    ) -> None:
        signed = _sign(signing_key, tmp_path, _document(key_id=feed_trust.PINNED_KEY_ID))
        assert manifest_mod.verify_manifest(signed) is True

    def test_the_cli_feed_shape_is_not_interchangeable(
        self, signing_key: Path, tmp_path: Path
    ) -> None:
        """One key, two documents: each consumer refuses the other's schema."""
        signed = _sign(signing_key, tmp_path, _document())
        # Signature is valid, but the strict CLI-feed entry point still refuses it
        # (nested payload, no key_id) — and the video consumer refuses a feed schema.
        assert feed_trust.verify_manifest_signature(signed) is False
        feed_shaped = _sign(
            signing_key,
            tmp_path,
            {"schema": "kirocrew-cli-artifact-manifest-v1", "channel": "stable"},
        )
        assert manifest_mod.verify_manifest(feed_shaped) is True
        assert manifest_mod.verified_manifest(feed_shaped) is None

    def test_an_oversized_payload_is_refused(self, signing_key: Path, tmp_path: Path) -> None:
        """Over the cap is refused, not truncated — and the cap is the publisher's."""
        big = _document(entries=[_entry(f"clip-{i}", description="x" * 1500) for i in range(200)])
        assert manifest_mod.verify_manifest(_sign(signing_key, tmp_path, big)) is False


# ── structural parsing ──


class TestManifestParsing:
    def test_a_foreign_schema_is_rejected(self) -> None:
        assert manifest_mod.parse_manifest(_document(schema="something-else")) is None

    @pytest.mark.parametrize(
        "release",
        ["", "latest", "../0.6.0", "0.6.0-rc1", "0.6.0/", "1.2.3.4.5", "0.6.0 ", "-1.0.0"],
    )
    def test_an_unsafe_release_is_rejected(self, release: str) -> None:
        assert manifest_mod.parse_manifest(_document(release=release)) is None

    @pytest.mark.parametrize("release", ["0.6", "1", "0.6.0", "1.2.3.4"])
    def test_every_release_shape_the_publisher_can_emit_is_accepted(self, release: str) -> None:
        """A cap tighter than the publisher's would discard a valid release WHOLE.

        ``scripts/feature-videos/_manifest.py`` accepts a bare numeric version of
        any depth; Kiro Crew's own releases are always three components, and the
        fetch path only ever asks for those.
        """
        parsed = manifest_mod.parse_manifest(_document(release=release))
        assert parsed is not None and parsed.release == release

    @pytest.mark.parametrize("base", ["http://cdn.example.com", "//cdn.example.com", "", 42])
    def test_a_non_https_cdn_base_is_rejected(self, base: object) -> None:
        assert manifest_mod.parse_manifest(_document(cdn_base=base)) is None

    def test_entries_must_be_a_list(self) -> None:
        assert manifest_mod.parse_manifest(_document(entries={"a": 1})) is None

    def test_the_entry_count_has_a_coherence_bound(self) -> None:
        """A cheap bound well above what the signed-payload cap already allows."""
        too_many = [_entry(f"clip-{i}") for i in range(manifest_mod._MAX_ENTRIES + 1)]
        assert manifest_mod.parse_manifest(_document(entries=too_many)) is None
        just_under = [_entry(f"clip-{i}") for i in range(3)]
        assert manifest_mod.parse_manifest(_document(entries=just_under)) is not None

    @pytest.mark.parametrize(
        "overrides",
        [
            {"file": "../../etc/passwd"},
            {"file": "clip.mp4/../../x.mp4"},
            {"file": "sub/dir/clip.mp4"},
            {"file": "clip.html"},
            {"file": "clip"},
            {"file": ".hidden.mp4"},
            {"poster": "poster.svg"},
            {"poster": "../poster.jpg"},
            {"sha256": "not-a-digest"},
            {"sha256": _CLIP_SHA.upper()},
            {"poster_sha256": ""},
            {"bytes": 0},
            {"bytes": -1},
            {"bytes": True},
            {"bytes": 10**12},
            {"doc": "internal-design-note.md"},
            {"min_version": "not-a-version"},
            {"id": "has spaces"},
            {"id": "../escape"},
        ],
    )
    def test_an_unsafe_entry_is_dropped(self, overrides: dict) -> None:
        """A bad ENTRY costs one clip; only a bad DOCUMENT costs the manifest."""
        parsed = manifest_mod.parse_manifest(_document(entries=[_entry(**overrides)]))
        assert parsed is not None
        assert parsed.entries == ()

    def test_good_entries_survive_beside_a_bad_one(self) -> None:
        parsed = manifest_mod.parse_manifest(
            _document(entries=[_entry("good"), _entry("bad", sha256="nope"), _entry("also-good")])
        )
        assert parsed is not None
        assert [e.id for e in parsed.entries] == ["good", "also-good"]

    def test_duplicate_ids_keep_only_the_first(self) -> None:
        parsed = manifest_mod.parse_manifest(
            _document(entries=[_entry("dup", title="first"), _entry("dup", title="second")])
        )
        assert parsed is not None
        assert [e.title for e in parsed.entries] == ["first"]

    def test_used_when_keeps_only_non_empty_strings(self) -> None:
        parsed = manifest_mod.parse_manifest(
            _document(entries=[_entry(used_when=["tips_feedback_exists", "", 7, None])])
        )
        assert parsed is not None
        assert parsed.entries[0].used_when == ("tips_feedback_exists",)

    def test_cdn_host_and_asset_url_are_derived_not_declared(self) -> None:
        manifest = _parsed()
        assert manifest.cdn_host == _CDN_HOST
        assert manifest.asset_url("x.mp4") == f"{_CDN}/0.6.0/x.mp4"


# ── release resolution ──


class TestReleaseResolution:
    @pytest.mark.parametrize(
        ("version", "expected"),
        [
            ("0.6.0", ("0.6.0", "0.5.0", "0.4.0", "0.3.0")),
            ("0.6.3", ("0.6.3", "0.6.0", "0.5.0", "0.4.0", "0.3.0")),
            ("0.6.0rc3", ("0.6.0", "0.5.0", "0.4.0", "0.3.0")),
            ("1.1.0", ("1.1.0", "1.0.0")),
            ("2.0.0", ("2.0.0",)),
        ],
    )
    def test_candidates_walk_down_minors_only(self, version: str, expected: tuple) -> None:
        """Never down a MAJOR boundary: that is where a clip most likely shows dead UI."""
        assert manifest_mod.release_candidates(version) == expected

    def test_an_unparseable_version_has_no_candidates(self) -> None:
        assert manifest_mod.release_candidates("not-a-version") == ()
        assert manifest_mod.running_release("not-a-version") == ""

    def test_release_dir_refuses_an_unsafe_folder_name(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            with pytest.raises(ValueError):
                manifest_mod.release_dir("../escape")


# ── fetching ──


def _as_opener(open_fn):
    """Wrap a urlopen-shaped fake as a ``build_opener`` replacement.

    All three clients (manifest, media, probe) call ``build_opener`` so they cannot
    differ on redirect policy, which makes it the one seam a test replaces.
    """
    return lambda *args, **kwargs: SimpleNamespace(open=open_fn)


def _fake_manifest_fetch(by_url: dict, state: SimpleNamespace):
    class _Resp:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self, n: int = -1) -> bytes:
            return self._body if n < 0 else self._body[:n]

        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    def _open(request, timeout=None):  # noqa: ANN001 - OpenerDirector.open
        url = getattr(request, "full_url", str(request))
        state.urls.append(url)
        if url not in by_url:
            raise urllib.error.HTTPError(url, 404, "not found", {}, None)  # type: ignore[arg-type]
        return _Resp(by_url[url])

    return _open


class TestManifestFetch:
    def test_fetches_verifies_and_stops_at_the_running_release(
        self, signing_key: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        signed = _sign(signing_key, tmp_path, _document(release="0.6.0"))
        state = SimpleNamespace(urls=[])
        monkeypatch.setattr(
            "kiro_crew.asset_downloader.build_opener",
            _as_opener(
                _fake_manifest_fetch(
                    {
                        f"{manifest_mod.DEFAULT_CDN_BASE}/0.6.0/manifest.json": json.dumps(
                            signed
                        ).encode()
                    },
                    state,
                )
            ),
        )
        manifest, raw = manifest_mod.fetch_manifest("0.6.0")
        assert manifest is not None and manifest.release == "0.6.0"
        assert raw["signature"] == signed["signature"]
        assert len(state.urls) == 1

    def test_falls_back_to_the_newest_lower_release(
        self, signing_key: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A build with no clips of its own still plays the existing library."""
        signed = _sign(signing_key, tmp_path, _document(release="0.4.0"))
        state = SimpleNamespace(urls=[])
        monkeypatch.setattr(
            "kiro_crew.asset_downloader.build_opener",
            _as_opener(
                _fake_manifest_fetch(
                    {
                        f"{manifest_mod.DEFAULT_CDN_BASE}/0.4.0/manifest.json": json.dumps(
                            signed
                        ).encode()
                    },
                    state,
                )
            ),
        )
        manifest, _raw = manifest_mod.fetch_manifest("0.6.0")
        assert manifest is not None and manifest.release == "0.4.0"
        # Tried 0.6.0 and 0.5.0 first, in order.
        assert [u.rsplit("/", 2)[1] for u in state.urls] == ["0.6.0", "0.5.0", "0.4.0"]

    def test_a_manifest_disagreeing_with_its_own_location_is_refused(
        self, signing_key: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A misfiled document must not redirect one release's clips into another's."""
        signed = _sign(signing_key, tmp_path, _document(release="0.4.0"))
        state = SimpleNamespace(urls=[])
        monkeypatch.setattr(
            "kiro_crew.asset_downloader.build_opener",
            _as_opener(
                _fake_manifest_fetch(
                    {
                        f"{manifest_mod.DEFAULT_CDN_BASE}/0.6.0/manifest.json": json.dumps(
                            signed
                        ).encode()
                    },
                    state,
                )
            ),
        )
        manifest, _raw = manifest_mod.fetch_manifest("0.6.0")
        assert manifest is None

    def test_an_unsigned_response_yields_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = SimpleNamespace(urls=[])
        monkeypatch.setattr(
            "kiro_crew.asset_downloader.build_opener",
            _as_opener(
                _fake_manifest_fetch(
                    {
                        f"{manifest_mod.DEFAULT_CDN_BASE}/{rel}/manifest.json": json.dumps(
                            _document(release=rel)
                        ).encode()
                        for rel in ("0.6.0", "0.5.0", "0.4.0", "0.3.0")
                    },
                    state,
                )
            ),
        )
        manifest, raw = manifest_mod.fetch_manifest("0.6.0")
        assert (manifest, raw) == (None, {})

    def test_an_oversized_response_is_ignored_before_parsing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = SimpleNamespace(urls=[])
        url = f"{manifest_mod.DEFAULT_CDN_BASE}/0.6.0/manifest.json"
        monkeypatch.setattr(
            "kiro_crew.asset_downloader.build_opener",
            _as_opener(_fake_manifest_fetch({url: b"x" * (256 * 1024 + 10)}, state)),
        )
        assert manifest_mod._fetch_json(url) is None

    def test_a_malformed_url_reads_as_no_manifest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A mistyped https override must not take the background task with it.

        urllib raises http.client.InvalidURL before any socket exists. It is not
        an OSError and not a ValueError, so it escapes a tuple that lists only
        those and kills the refresh task that called this.
        """

        def _explode(*_a: object, **_k: object) -> object:
            raise http.client.InvalidURL("nonnumeric port: 'not-a-port'")

        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", _as_opener(_explode))
        assert manifest_mod._fetch_json("https://cdn.example.com:not-a-port/manifest.json") is None


class TestManifestUrlResolution:
    def test_the_default_is_the_cdn_release_folder(self) -> None:
        assert (
            manifest_mod.manifest_url("0.6.0")
            == f"{manifest_mod.DEFAULT_CDN_BASE}/0.6.0/manifest.json"
        )

    def test_the_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(manifest_mod.MANIFEST_URL_ENV, "https://mirror.example/m.json")
        assert manifest_mod.manifest_url("0.6.0", "https://cfg.example/m.json") == (
            "https://mirror.example/m.json"
        )

    def test_the_config_knob_is_used_when_no_env_is_set(self) -> None:
        assert manifest_mod.manifest_url("0.6.0", "https://cfg.example/m.json") == (
            "https://cfg.example/m.json"
        )

    @pytest.mark.parametrize("bad", ["http://mirror.example/m.json", "file:///tmp/m.json", " "])
    def test_a_non_https_override_is_ignored_not_honoured(
        self, bad: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(manifest_mod.MANIFEST_URL_ENV, bad)
        assert manifest_mod.manifest_url("0.6.0") == (
            f"{manifest_mod.DEFAULT_CDN_BASE}/0.6.0/manifest.json"
        )


# ── the on-disk manifest cache ──


class TestManifestOnDisk:
    def test_a_stored_manifest_is_re_verified_on_read(
        self, signing_key: Path, tmp_path: Path
    ) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            signed = _sign(signing_key, tmp_path, _document())
            manifest_mod.store_manifest(_parsed(), signed)
            loaded = manifest_mod.load_cached_manifest("0.6.0")
            assert loaded is not None and loaded.release == "0.6.0"

    def test_an_edited_cache_file_is_refused(self, signing_key: Path, tmp_path: Path) -> None:
        """The cache states cdn_base, so trusting it because we once did is not enough."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            signed = _sign(signing_key, tmp_path, _document())
            manifest_mod.store_manifest(_parsed(), signed)
            path = manifest_mod.cached_manifest_path("0.6.0")
            doctored = json.loads(path.read_text(encoding="utf-8"))
            doctored["cdn_base"] = "https://attacker.example/videos"
            path.write_text(json.dumps(doctored), encoding="utf-8")
            assert manifest_mod.load_cached_manifest("0.6.0") is None

    def test_a_corrupt_cache_file_reads_as_no_manifest(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest_mod.ensure_cache_dir("0.6.0")
            manifest_mod.cached_manifest_path("0.6.0").write_text("{not json", encoding="utf-8")
            assert manifest_mod.load_cached_manifest("0.6.0") is None

    def test_a_lower_release_cache_is_used_for_a_newer_build(
        self, signing_key: Path, tmp_path: Path
    ) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            signed = _sign(signing_key, tmp_path, _document(release="0.4.0"))
            manifest_mod.store_manifest(_parsed(release="0.4.0"), signed)
            loaded = manifest_mod.load_cached_manifest("0.6.0")
            assert loaded is not None and loaded.release == "0.4.0"

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX mode bits")
    def test_the_cache_is_owner_only(self, signing_key: Path, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest_mod.store_manifest(_parsed(), _sign(signing_key, tmp_path, _document()))
            folder = manifest_mod.release_dir("0.6.0")
            assert stat.S_IMODE(folder.stat().st_mode) == 0o700
            path = manifest_mod.cached_manifest_path("0.6.0")
            assert stat.S_IMODE(path.stat().st_mode) == 0o600


# ── eviction ──


class TestEviction:
    def _seed_releases(self, sizes: "dict[str, int]") -> None:
        for i, (release, size) in enumerate(sizes.items()):
            folder = manifest_mod.ensure_cache_dir(release)
            (folder / "clip.mp4").write_bytes(b"x" * size)
            # Distinct mtimes, oldest first in insertion order.
            stamp = time.time() - (len(sizes) - i) * 100
            os.utime(folder, (stamp, stamp))

    def test_evicts_oldest_first_until_the_cap_is_met(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            self._seed_releases({"0.3.0": 400, "0.4.0": 400, "0.5.0": 400, "0.6.0": 400})
            evicted = cache_mod.evict("0.6.0", max_bytes=900, keep_releases=0)
            assert evicted == ["0.3.0", "0.4.0"]
            remaining = {r for r, _p, _m, _s in cache_mod.release_folders()}
            assert remaining == {"0.5.0", "0.6.0"}

    def test_the_running_release_is_never_evicted_even_over_the_cap(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            self._seed_releases({"0.6.0": 5000})
            assert cache_mod.evict("0.6.0", max_bytes=10, keep_releases=0) == []
            assert manifest_mod.release_dir("0.6.0").is_dir()

    @pytest.mark.parametrize(
        ("max_mb", "expected"),
        [
            (500.0, 500 * 1024 * 1024),
            (0.0, 0),
            (-5.0, 0),
            (float("nan"), 0),
            (float("inf"), 0),
            (1e308, cache_mod.MAX_CACHE_BYTES),
            (1e300, cache_mod.MAX_CACHE_BYTES),
        ],
    )
    def test_the_ceiling_is_always_a_byte_count(self, max_mb: float, expected: int) -> None:
        """A configured megabyte value becomes an int, or eviction cannot run at all.

        1e308 is the one that bites: the multiply alone overflows to inf, and
        int(inf) raises OverflowError out of the eviction pass.
        """
        assert cache_mod.cache_ceiling_bytes(max_mb) == expected

    def test_an_absurd_configured_ceiling_evicts_nothing_instead_of_raising(
        self, tmp_path: Path
    ) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            self._seed_releases({"0.5.0": 400, "0.6.0": 400})
            cache = cache_mod.FeatureVideoCache()
            dashboard = SimpleNamespace(
                feature_videos_cache_max_mb=1e308, feature_videos_keep_releases=0
            )
            cache._evict_now("0.6.0", dashboard)
            remaining = {r for r, _p, _m, _s in cache_mod.release_folders()}
            assert remaining == {"0.5.0", "0.6.0"}

    def test_keep_releases_bounds_the_count_and_spares_the_running_one(
        self, tmp_path: Path
    ) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            self._seed_releases({"0.3.0": 10, "0.4.0": 10, "0.5.0": 10, "0.6.0": 10})
            evicted = cache_mod.evict("0.6.0", max_bytes=0, keep_releases=2)
            assert evicted == ["0.3.0", "0.4.0"]
            remaining = {r for r, _p, _m, _s in cache_mod.release_folders()}
            assert remaining == {"0.5.0", "0.6.0"}

    def test_keep_one_release_removes_every_other(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            self._seed_releases({"0.4.0": 10, "0.5.0": 10, "0.6.0": 10})
            assert cache_mod.evict("0.6.0", max_bytes=0, keep_releases=1) == ["0.4.0", "0.5.0"]

    def test_no_bounds_evicts_nothing(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            self._seed_releases({"0.4.0": 10, "0.6.0": 10})
            assert cache_mod.evict("0.6.0", max_bytes=0, keep_releases=0) == []

    def test_an_unrecognized_directory_is_left_alone(self, tmp_path: Path) -> None:
        """We never created it, so we never delete it."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            stray = manifest_mod.cache_root() / "not-a-release"
            stray.mkdir(parents=True)
            (stray / "keep.txt").write_text("x", encoding="utf-8")
            cache_mod.evict("0.6.0", max_bytes=1, keep_releases=1)
            assert stray.is_dir()


# ── cached-ness and the download pass ──


def _fake_media_fetch(available: "set[str]"):
    class _Resp:
        def __init__(self, body: bytes) -> None:
            self._body = body
            self._pos = 0
            self.status = 200
            self.headers = {"Content-Length": str(len(body))}

        def read(self, n: int) -> bytes:
            chunk = self._body[self._pos : self._pos + n]
            self._pos += n
            return chunk

        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    def _open(request, timeout=None):  # noqa: ANN001 - OpenerDirector.open
        url = getattr(request, "full_url", str(request))
        name = url.rsplit("/", 1)[-1]
        if name not in available:
            raise urllib.error.HTTPError(url, 404, "not found", {}, None)  # type: ignore[arg-type]
        return _Resp(_POSTER if name.endswith(".jpg") else _CLIP)

    return _open


class TestCachedness:
    def test_both_files_present_and_the_right_size_counts_as_cached(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest = _parsed()
            assert cache_mod.is_cached(manifest.entries[0], "0.6.0") is False
            _write_media("0.6.0")
            assert cache_mod.is_cached(manifest.entries[0], "0.6.0") is True

    def test_a_truncated_clip_does_not_count(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest = _parsed()
            _write_media("0.6.0", clip=_CLIP[:10])
            assert cache_mod.is_cached(manifest.entries[0], "0.6.0") is False

    def test_a_missing_poster_alone_does_not_count(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest = _parsed()
            folder = manifest_mod.ensure_cache_dir("0.6.0")
            (folder / "hosted-clip.mp4").write_bytes(_CLIP)
            assert cache_mod.is_cached(manifest.entries[0], "0.6.0") is False


class TestDownloadPass:
    def test_downloads_poster_and_clip_verified(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "kiro_crew.asset_downloader.build_opener",
            _as_opener(_fake_media_fetch({"hosted-clip.mp4", "hosted-clip.jpg"})),
        )
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest = _parsed()
            cache = cache_mod.FeatureVideoCache()
            ok, err = cache._download_entry(manifest.entries[0], manifest, 0)
            assert (ok, err) == (True, "")
            folder = manifest_mod.release_dir("0.6.0")
            assert (folder / "hosted-clip.mp4").read_bytes() == _CLIP
            assert (folder / "hosted-clip.jpg").read_bytes() == _POSTER

    def test_a_missing_poster_fails_before_the_clip_transfer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "kiro_crew.asset_downloader.build_opener",
            _as_opener(_fake_media_fetch({"hosted-clip.mp4"})),
        )
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest = _parsed()
            cache = cache_mod.FeatureVideoCache()
            ok, _err = cache._download_entry(manifest.entries[0], manifest, 0)
            assert ok is False
            assert not (manifest_mod.release_dir("0.6.0") / "hosted-clip.mp4").exists()

    def test_a_wrong_sha_leaves_nothing_installed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "kiro_crew.asset_downloader.build_opener",
            _as_opener(_fake_media_fetch({"hosted-clip.mp4", "hosted-clip.jpg"})),
        )
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest = _parsed(entries=[_entry(sha256="0" * 64)])
            cache = cache_mod.FeatureVideoCache()
            ok, err = cache._download_entry(manifest.entries[0], manifest, 0)
            assert ok is False and "sha256 mismatch" in err
            assert not (manifest_mod.release_dir("0.6.0") / "hosted-clip.mp4").exists()

    def test_the_skip_env_makes_the_pass_a_no_op(self, tmp_path: Path) -> None:
        with patch.dict(
            os.environ,
            {"KIROCREW_HOME": str(tmp_path), cache_mod.SKIP_DOWNLOAD_ENV: "1"},
        ):
            assert asyncio.run(cache_mod.feature_video_cache().ensure_all()) is False
            assert cache_mod.start_background_feature_video_download() is None

    def test_the_kill_switch_stops_the_pass_before_any_governance_probe(
        self, tmp_path: Path
    ) -> None:
        with patch.dict(
            os.environ, {"KIROCREW_HOME": str(tmp_path), cache_mod.SKIP_DOWNLOAD_ENV: ""}
        ):
            cfg = MagicMock()
            cfg.dashboard.feature_videos_enabled = False
            cache = cache_mod.feature_video_cache()
            with (
                patch("kiro_crew.feature_videos_cache.KiroCrewConfig") as cfg_cls,
                patch.object(manifest_mod, "download_denied", side_effect=AssertionError),
            ):
                cfg_cls.load.return_value = cfg
                assert asyncio.run(cache.ensure_all()) is False
            assert cache.status["download_state"] == cache_mod.STATE_DISABLED

    def test_a_denied_ceiling_makes_no_request_and_reports_denied(self, tmp_path: Path) -> None:
        with patch.dict(
            os.environ, {"KIROCREW_HOME": str(tmp_path), cache_mod.SKIP_DOWNLOAD_ENV: ""}
        ):
            cfg = MagicMock()
            cfg.dashboard.feature_videos_enabled = True
            cache = cache_mod.feature_video_cache()
            with (
                patch("kiro_crew.feature_videos_cache.KiroCrewConfig") as cfg_cls,
                patch.object(manifest_mod, "download_denied", return_value=True),
                patch.object(
                    manifest_mod, "fetch_manifest", side_effect=AssertionError("fetched anyway")
                ),
            ):
                cfg_cls.load.return_value = cfg
                assert asyncio.run(cache.ensure_all()) is False
            assert cache.status["download_state"] == cache_mod.STATE_DENIED


class TestGovernanceDuringAPass:
    """A paced pass runs for minutes, so the ceiling is re-read per clip."""

    def test_a_denial_mid_pass_stops_the_remaining_clips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Checking once before the loop would keep downloading under a lifted grant."""
        monkeypatch.setattr(
            "kiro_crew.asset_downloader.build_opener",
            _as_opener(_fake_media_fetch({"a.mp4", "a.jpg", "b.mp4", "b.jpg", "c.mp4", "c.jpg"})),
        )
        with patch.dict(
            os.environ, {"KIROCREW_HOME": str(tmp_path), cache_mod.SKIP_DOWNLOAD_ENV: ""}
        ):
            manifest = _parsed(entries=[_entry("a"), _entry("b"), _entry("c")])
            cfg = MagicMock()
            cfg.dashboard.feature_videos_enabled = True
            cfg.dashboard.feature_videos_cache_max_mb = 500
            cfg.dashboard.feature_videos_keep_releases = 0
            cache = cache_mod.feature_video_cache()
            # Permit the pre-loop check and the first clip; deny from then on.
            answers = iter([False, False, True, True, True, True])

            def _denied() -> bool:
                return next(answers, True)

            with (
                patch("kiro_crew.feature_videos_cache.KiroCrewConfig") as cfg_cls,
                patch.object(manifest_mod, "download_denied", side_effect=_denied),
                patch.object(cache, "refresh_manifest", return_value=manifest),
            ):
                cfg_cls.load.return_value = cfg
                assert asyncio.run(cache.ensure_all()) is False
            assert cache.status["download_state"] == cache_mod.STATE_DENIED
            folder = manifest_mod.release_dir("0.6.0")
            # The first clip landed; the pass stopped before the rest.
            assert (folder / "a.mp4").is_file()
            assert not (folder / "c.mp4").exists()

    def test_the_poster_transfer_carries_a_ceiling(self, tmp_path: Path) -> None:
        """The manifest declares no poster byte count, so the call must bound it itself.

        Without a bound the only disk-fill guard in ``download_to`` never fires and
        an endless poster body fills the disk before the digest can reject it.
        """
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest = _parsed()
            seen: list[dict] = []

            def _record(*_args: object, **kwargs: object) -> tuple[bool, str]:
                seen.append(kwargs)
                return False, "stopped"

            with patch.object(cache_mod.asset_downloader, "download_to", _record):
                cache_mod.FeatureVideoCache()._download_entry(manifest.entries[0], manifest, 0)
        assert seen, "download_to was never called"
        assert seen[0]["max_bytes"] == cache_mod.MAX_POSTER_BYTES
        assert "size" not in seen[0], "a bound must not masquerade as a declared length"


class TestGovernanceReadSplit:
    """An audited evaluation belongs at the action; a polled read gets the memo."""

    def test_the_cached_read_evaluates_once_then_serves_the_memo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[int] = []

        def _denied() -> bool:
            calls.append(1)
            return False

        monkeypatch.setattr(manifest_mod, "download_denied", _denied)
        monkeypatch.setattr(manifest_mod, "_last_download_check_ts", 0.0)
        monkeypatch.setattr(manifest_mod, "_last_download_permitted", True)
        for _ in range(25):
            assert manifest_mod.download_permitted_cached() is True
        assert len(calls) == 1, "a polled route must not write one SEL row per poll"

    def test_the_cached_read_re_evaluates_after_the_ttl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[int] = []
        monkeypatch.setattr(manifest_mod, "download_denied", lambda: bool(calls.append(1)))
        monkeypatch.setattr(manifest_mod, "_last_download_check_ts", 0.0)
        manifest_mod.download_permitted_cached()
        monkeypatch.setattr(
            manifest_mod, "_last_download_check_ts", -manifest_mod._GOVERNANCE_TTL_SECS
        )
        manifest_mod.download_permitted_cached()
        assert len(calls) == 2

    def test_a_cold_process_evaluates_rather_than_reporting_denied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Serving the fail-closed default here would tell the user the feature is off."""
        monkeypatch.setattr(manifest_mod, "_last_download_check_ts", 0.0)
        monkeypatch.setattr(manifest_mod, "_last_download_permitted", False)
        monkeypatch.setattr(manifest_mod, "download_denied", lambda: False)
        assert manifest_mod.download_permitted_cached() is True


# ── serving ──


def _file_request(release: str, name: str) -> MagicMock:
    request = MagicMock()
    request.match_info = {"release": release, "name": name}
    return request


class TestServingRoute:
    def test_serves_a_cached_file(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _write_media("0.6.0")
            resp = asyncio.run(
                cache_mod.api_feature_video_file(_file_request("0.6.0", "hosted-clip.mp4"))
            )
            assert isinstance(resp, web.FileResponse)
            canonical = manifest_mod.cache_root().resolve(strict=True) / "0.6.0"
            assert Path(resp._path) == canonical / "hosted-clip.mp4"

    @pytest.mark.parametrize(
        ("release", "name"),
        [
            ("0.6.0", "../../../etc/passwd"),
            ("0.6.0", "..%2fsecret"),
            ("0.6.0", "sub/clip.mp4"),
            ("0.6.0", "clip.mp4%00.txt"),
            ("0.6.0", "\\clip.mp4"),
            ("0.6.0", "/etc/passwd"),
            ("0.6.0", "C:clip.mp4"),
            ("0.6.0", ""),
            ("0.6.0", "."),
            ("0.6.0", ".."),
            ("../0.6.0", "hosted-clip.mp4"),
            ("0.6.0/../..", "hosted-clip.mp4"),
            ("latest", "hosted-clip.mp4"),
            ("", "hosted-clip.mp4"),
        ],
    )
    def test_an_unsafe_component_is_a_flat_404(
        self, tmp_path: Path, release: str, name: str
    ) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _write_media("0.6.0")
            with pytest.raises(web.HTTPNotFound):
                asyncio.run(cache_mod.api_feature_video_file(_file_request(release, name)))

    def test_a_file_that_is_not_there_is_a_404(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest_mod.ensure_cache_dir("0.6.0")
            with pytest.raises(web.HTTPNotFound):
                asyncio.run(cache_mod.api_feature_video_file(_file_request("0.6.0", "gone.mp4")))

    def test_a_directory_is_never_served(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            folder = manifest_mod.ensure_cache_dir("0.6.0")
            (folder / "sub.mp4").mkdir()
            with pytest.raises(web.HTTPNotFound):
                asyncio.run(cache_mod.api_feature_video_file(_file_request("0.6.0", "sub.mp4")))

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="symlinks need privilege on Windows")
    def test_a_symlink_out_of_the_cache_is_refused(self, tmp_path: Path) -> None:
        """Name validation cannot see a symlink; the containment re-check can."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            folder = manifest_mod.ensure_cache_dir("0.6.0")
            secret = tmp_path / "secret.mp4"
            secret.write_bytes(b"not yours")
            (folder / "escape.mp4").symlink_to(secret)
            with pytest.raises(web.HTTPNotFound):
                asyncio.run(cache_mod.api_feature_video_file(_file_request("0.6.0", "escape.mp4")))

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="symlinks need privilege on Windows")
    def test_a_symlinked_release_folder_cannot_become_the_boundary(self, tmp_path: Path) -> None:
        """The escape one level UP from the file: the release folder is the link.

        Anchoring containment to the release folder makes this pass every check —
        the folder resolves to the outside directory, and a file in that directory
        is then "inside" the root. Anchoring to the cache root is what refuses it.
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "hosted-clip.mp4").write_bytes(b"not yours")
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            root = manifest_mod.cache_root()
            root.mkdir(parents=True, exist_ok=True)
            (root / "0.6.0").symlink_to(outside, target_is_directory=True)
            assert cache_mod.resolve_served_path("0.6.0", "hosted-clip.mp4") is None
            with pytest.raises(web.HTTPNotFound):
                asyncio.run(
                    cache_mod.api_feature_video_file(_file_request("0.6.0", "hosted-clip.mp4"))
                )

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="symlinks need privilege on Windows")
    def test_a_symlink_inside_the_cache_is_refused_too(self, tmp_path: Path) -> None:
        """Refused for being a link, not for where it points.

        A manifest names files, so a link has no legitimate reader even when its
        target is a clip in the same folder. Checking containment alone would
        serve this one.
        """
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            folder = _write_media("0.6.0")
            (folder / "alias.mp4").symlink_to(folder / "hosted-clip.mp4")
            assert cache_mod.resolve_served_path("0.6.0", "alias.mp4") is None

    def test_a_missing_cache_root_is_a_404_not_a_crash(self, tmp_path: Path) -> None:
        """Nothing fetched yet: the anchor cannot be resolved, so there is no file."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            assert cache_mod.resolve_served_path("0.6.0", "hosted-clip.mp4") is None


class TestRemoteEgressTakesTheAuditedAnswer:
    """A remote offer and a remote probe both authorize traffic, so neither reads the memo.

    The memo can be a full TTL stale. For a readout that costs nothing; for these two
    it would answer a denial with a CDN url the browser then fetches, which is egress
    the ceiling had already withdrawn.
    """

    def test_a_denial_removes_the_remote_pool_within_the_memo_window(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest = _parsed()
            cache = cache_mod.feature_video_cache()
            monkeypatch.setattr(cache, "current_manifest", lambda: manifest)
            # Warm the memo with a PERMIT, then deny. A reader of the memo would
            # still see the permit for the rest of the TTL.
            with patch(
                "kiro_crew.platform.governance_profiles.vet_and_audit",
                return_value=SimpleNamespace(permitted=True),
            ):
                manifest_mod.download_denied()
            assert manifest_mod.download_permitted_memo() is True
            with patch(
                "kiro_crew.platform.governance_profiles.vet_and_audit",
                return_value=SimpleNamespace(permitted=False),
            ):
                _preferred, fallback = fv._offer_pools("0.6.0")
            assert fallback == ()

    def test_a_denial_stops_the_outbound_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The gate sits in _probe_remote, so no caller can reach the network past it."""
        sent: list[str] = []

        def _explode(*_a: object, **_k: object) -> object:
            sent.append("request")
            raise AssertionError("a denied probe must not open a connection")

        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", _as_opener(_explode))
        with patch(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            return_value=SimpleNamespace(permitted=False),
        ):
            assert fv._probe_remote("https://cdn.example.com/0.6.0/hosted-clip.mp4") is False
        assert sent == []

    def test_a_malformed_reply_is_false_not_a_500(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """http.client.BadStatusLine is neither OSError nor ValueError."""

        def _explode(*_a: object, **_k: object) -> object:
            raise http.client.BadStatusLine("\x16\x03\x01")

        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", _as_opener(_explode))
        with patch(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            return_value=SimpleNamespace(permitted=True),
        ):
            assert fv._probe_remote("https://cdn.example.com/0.6.0/hosted-clip.mp4") is False

    def test_the_status_readout_still_uses_the_memo(self, tmp_path: Path) -> None:
        """The split is kept: a display field must not spend an audited decision."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            with patch(
                "kiro_crew.platform.governance_profiles.vet_and_audit",
                return_value=SimpleNamespace(permitted=True),
            ) as vet:
                manifest_mod.download_permitted_cached()
                before = vet.call_count
                manifest_mod.download_permitted_cached()
                assert vet.call_count == before


# ── source selection ──


class TestRemoteUrlValidation:
    def test_the_manifest_host_is_accepted(self) -> None:
        url = f"{_CDN}/0.6.0/clip.mp4"
        assert fv.validate_asset_path(url, allowed_hosts=frozenset({_CDN_HOST})) == url

    def test_no_allowed_hosts_means_no_remote_source(self) -> None:
        assert fv.validate_asset_path(f"{_CDN}/0.6.0/clip.mp4") == ""
        assert fv.validate_asset_path(f"{_CDN}/0.6.0/clip.mp4", allowed_hosts=frozenset()) == ""

    @pytest.mark.parametrize(
        "url",
        [
            "https://attacker.example/feature-videos/0.6.0/clip.mp4",
            "https://cdn.example.com.attacker.example/clip.mp4",
            "https://user:pw@cdn.example.com/0.6.0/clip.mp4",
            "https://cdn.example.com:8443/0.6.0/clip.mp4",
            "https://cdn.example.com/0.6.0/clip.mp4?X-Amz-Signature=abc",
            "https://cdn.example.com/0.6.0/clip.mp4#frag",
            "https://cdn.example.com/0.6.0/../../../secret.mp4",
            "https://cdn.example.com/0.6.0//clip.mp4",
            "https://cdn.example.com/0.6.0/clip.mp4%00",
            "https://cdn.example.com/0.6.0/index.html",
            "https://cdn.example.com/0.6.0/clip",
            "https://cdn.example.com",
            "https://cdn.example.com/0.6.0/clip .mp4",
        ],
    )
    def test_anything_else_is_refused(self, url: str) -> None:
        assert fv.validate_asset_path(url, allowed_hosts=frozenset({_CDN_HOST})) == ""

    def test_the_cache_prefix_is_same_origin(self) -> None:
        path = "/feature-videos/0.6.0/clip.mp4"
        assert fv.validate_asset_path(path) == path

    @pytest.mark.parametrize(
        "path",
        [
            "/feature-videos/",
            "/feature-videos/0.6.0/../secret",
            "/feature-videos//0.6.0/clip.mp4",
            "/feature-videos/0.6.0/clip%2e%2e.mp4",
        ],
    )
    def test_an_unsafe_cache_path_is_refused(self, path: str) -> None:
        assert fv.validate_asset_path(path) == ""


class TestHostedSelection:
    def test_a_cached_entry_is_offered_as_a_local_source(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _write_media("0.6.0")
            _seed(_parsed())
            picked = fv.select_next("9.9.9")
            assert picked is not None
            assert picked.source == fv.SOURCE_LOCAL
            assert picked.src == "/feature-videos/0.6.0/hosted-clip.mp4"
            assert picked.poster == "/feature-videos/0.6.0/hosted-clip.jpg"

    def test_an_uncached_entry_is_offered_from_the_manifest_host(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _seed(_parsed())
            picked = fv.select_next("9.9.9")
            assert picked is not None
            assert picked.source == fv.SOURCE_REMOTE
            assert picked.src == f"{_CDN}/0.6.0/hosted-clip.mp4"

    def test_local_wins_over_remote(self, tmp_path: Path) -> None:
        """A cached clip plays with no egress and no spinner, so it is preferred."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _write_media("0.6.0", "cached-one")
            _seed(_parsed(entries=[_entry("uncached-one"), _entry("cached-one")]))
            for _ in range(20):
                picked = fv.select_next("9.9.9")
                assert picked is not None
                assert picked.id == "cached-one"
                assert picked.source == fv.SOURCE_LOCAL

    def test_a_denied_ceiling_offers_only_cached_clips(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _seed(_parsed())
            with patch.object(manifest_mod, "download_denied", return_value=True):
                assert fv.select_next("9.9.9") is None
            _write_media("0.6.0")
            with patch.object(manifest_mod, "download_denied", return_value=True):
                picked = fv.select_next("9.9.9")
            assert picked is not None and picked.source == fv.SOURCE_LOCAL

    def test_a_manifest_replaces_the_static_catalog(self, tmp_path: Path) -> None:
        """Mixing them would offer a bundled clip and its hosted successor as two."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _seed(_parsed())
            with patch.object(fv, "_asset_exists", lambda _p: True):
                for _ in range(20):
                    picked = fv.select_next("9.9.9")
                    assert picked is not None and picked.id == "hosted-clip"

    def test_the_static_catalog_serves_when_no_manifest_was_ever_fetched(
        self, tmp_path: Path
    ) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            with patch.object(fv, "_asset_exists", lambda _p: True):
                picked = fv.select_next("9.9.9")
            assert picked is not None
            assert picked.id in {e.id for e in fv.CATALOG}
            assert picked.source == fv.SOURCE_LOCAL

    def test_recorded_state_and_probes_still_withdraw_a_hosted_entry(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _seed(_parsed(entries=[_entry("recorded"), _entry("probed", used_when=["always"])]))
            fv.save_state(fv.FeatureVideoState(videos={"recorded": {"status": "seen", "ts": 1.0}}))
            with patch.dict(fv._PROBES, {"always": lambda: True}):
                assert fv.select_next("9.9.9") is None

    def test_a_version_floor_withdraws_a_hosted_entry(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _seed(_parsed(entries=[_entry("future", min_version="99.0.0")]))
            assert fv.select_next("1.2.3") is None

    def test_a_hosted_id_can_record_a_verdict(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _seed(_parsed())
            ids = fv.known_video_ids()
            # Both catalogs: a clip shown before the manifest landed must stay
            # retirable, and its recorded id is what keeps it retired.
            assert "hosted-clip" in ids
            assert {e.id for e in fv.CATALOG} <= ids


# ── the probe route ──


def _probe_request(video_id: str) -> MagicMock:
    request = MagicMock()
    request.query = {"id": video_id}
    return request


class TestProbeRoute:
    def test_a_cached_local_clip_probes_ok(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _write_media("0.6.0")
            _seed(_parsed())
            resp = asyncio.run(fv.api_feature_videos_probe(_probe_request("hosted-clip")))
            assert json.loads(resp.body) == {"ok": True}  # type: ignore[arg-type]

    def test_a_bundled_clip_probes_against_the_shipped_assets(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            with patch.object(fv, "_asset_exists", lambda _p: True):
                resp = asyncio.run(fv.api_feature_videos_probe(_probe_request("feature-tips")))
            assert json.loads(resp.body) == {"ok": True}  # type: ignore[arg-type]

    def test_an_unknown_id_is_not_ok(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            resp = asyncio.run(fv.api_feature_videos_probe(_probe_request("nope")))
            assert json.loads(resp.body) == {"ok": False}  # type: ignore[arg-type]

    def test_a_missing_id_parameter_is_not_ok(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            resp = asyncio.run(fv.api_feature_videos_probe(_probe_request("")))
            assert json.loads(resp.body) == {"ok": False}  # type: ignore[arg-type]

    def test_a_remote_clip_is_probed_with_a_head_request(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []

        class _Resp:
            status = 200

            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *exc: object) -> bool:
                return False

        def _open(request, timeout=None):  # noqa: ANN001 - OpenerDirector.open
            seen.append(f"{request.get_method()} {request.full_url}")
            return _Resp()

        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", _as_opener(_open))
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _seed(_parsed())
            resp = asyncio.run(fv.api_feature_videos_probe(_probe_request("hosted-clip")))
        assert json.loads(resp.body) == {"ok": True}  # type: ignore[arg-type]
        assert seen == [f"HEAD {_CDN}/0.6.0/hosted-clip.mp4"]

    def test_a_remote_clip_that_is_gone_is_not_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _open(request, timeout=None):  # noqa: ANN001 - OpenerDirector.open
            raise urllib.error.HTTPError(request.full_url, 404, "gone", {}, None)  # type: ignore[arg-type]

        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", _as_opener(_open))
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _seed(_parsed())
            resp = asyncio.run(fv.api_feature_videos_probe(_probe_request("hosted-clip")))
        assert json.loads(resp.body) == {"ok": False}  # type: ignore[arg-type]

    def test_a_denied_ceiling_never_probes_a_remote_clip(self, tmp_path: Path) -> None:
        """The offer does not exist under a denial, so there is nothing to reach for."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _seed(_parsed())
            with patch.object(manifest_mod, "download_permitted_cached", return_value=False):
                resp = asyncio.run(fv.api_feature_videos_probe(_probe_request("hosted-clip")))
            assert json.loads(resp.body) == {"ok": False}  # type: ignore[arg-type]


# ── next, status and fetch-all ──


def _dashboard_request(path: str = "/api/feature-videos/status") -> MagicMock:
    request = MagicMock()
    state = MagicMock()
    state._restricted_keys = set()
    state._slots = {}
    request.app = {"state": state}
    request.headers = {"X-Session-Key": "dashboard:ui"}
    request.method = "GET"
    request.path = path
    return request


def _next_body(tmp_path: Path) -> dict[str, object]:
    cfg = MagicMock()
    cfg.dashboard.feature_videos_enabled = True
    with patch("kiro_crew.feature_videos.KiroCrewConfig") as cfg_cls:
        cfg_cls.load.return_value = cfg
        resp = asyncio.run(
            fv.api_feature_videos_next(_dashboard_request("/api/feature-videos/next"))
        )
    return json.loads(resp.body)  # type: ignore[arg-type]


class TestNextRouteDownloadPermit:
    """``/next`` carries ``download_enabled`` — the modal fails closed on a remote offer without it.

    ``StartupVideoModal`` opens a ``'remote'`` clip only when the field is exactly
    ``true``; an absent field reads as OFF by design (an older gateway). So a
    backend that hands back a CDN url without the permit offers a clip that never
    plays.
    """

    def test_a_remote_offer_arrives_with_the_permit_that_allowed_it(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _seed(_parsed())
            with patch("kiro_crew.feature_videos.download_denied_now", return_value=False):
                body = _next_body(tmp_path)
        assert body["enabled"] is True
        assert body["video"]["source"] == fv.SOURCE_REMOTE  # type: ignore[index]
        assert body["download_enabled"] is True

    def test_a_denied_ceiling_reports_false_and_offers_no_remote_clip(self, tmp_path: Path) -> None:
        """The two fields come from ONE evaluation, so they cannot disagree."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _seed(_parsed())
            with patch("kiro_crew.feature_videos.download_denied_now", return_value=True):
                body = _next_body(tmp_path)
        assert body["enabled"] is True
        assert body["video"] is None
        assert body["download_enabled"] is False

    def test_a_cached_clip_under_a_denial_still_plays_locally(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _write_media("0.6.0")
            _seed(_parsed())
            with patch("kiro_crew.feature_videos.download_denied_now", return_value=True):
                body = _next_body(tmp_path)
        assert body["video"]["source"] == fv.SOURCE_LOCAL  # type: ignore[index]
        assert body["download_enabled"] is False

    def test_without_a_manifest_the_permit_is_the_memo_not_an_audit(self, tmp_path: Path) -> None:
        """No remote offer can exist, so the answer authorizes nothing and takes the memo."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            with (
                patch.object(fv, "_asset_exists", lambda _p: True),
                patch.object(manifest_mod, "download_permitted_cached", return_value=False),
                patch.object(manifest_mod, "download_denied", side_effect=AssertionError),
            ):
                body = _next_body(tmp_path)
        assert body["video"] is not None
        assert body["download_enabled"] is False

    def test_the_kill_switch_answer_carries_no_permit(self, tmp_path: Path) -> None:
        """Nothing to gate, and no governance read on every load of a disabled install."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            cfg = MagicMock()
            cfg.dashboard.feature_videos_enabled = False
            with (
                patch("kiro_crew.feature_videos.KiroCrewConfig") as cfg_cls,
                patch.object(manifest_mod, "download_denied", side_effect=AssertionError),
                patch.object(manifest_mod, "download_permitted_cached", side_effect=AssertionError),
            ):
                cfg_cls.load.return_value = cfg
                resp = asyncio.run(
                    fv.api_feature_videos_next(_dashboard_request("/api/feature-videos/next"))
                )
        assert json.loads(resp.body) == {"video": None, "enabled": False}  # type: ignore[arg-type]


class TestStatusRoute:
    def test_reports_the_release_and_the_cache_progress(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _write_media("0.6.0", "cached-one")
            _seed(_parsed(entries=[_entry("cached-one"), _entry("uncached-one")]))
            cfg = MagicMock()
            cfg.dashboard.feature_videos_enabled = True
            with patch("kiro_crew.feature_videos.KiroCrewConfig") as cfg_cls:
                cfg_cls.load.return_value = cfg
                resp = asyncio.run(fv.api_feature_videos_status(_dashboard_request()))
            body = json.loads(resp.body)  # type: ignore[arg-type]
        assert body["release"] == "0.6.0"
        assert (body["cached"], body["total"]) == (1, 2)
        assert body["download_enabled"] is True
        assert body["downloading"] is None
        assert body["download_state"] == cache_mod.STATE_IDLE

    def test_reports_a_denied_ceiling(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            cfg = MagicMock()
            cfg.dashboard.feature_videos_enabled = True
            with (
                patch("kiro_crew.feature_videos.KiroCrewConfig") as cfg_cls,
                patch.object(manifest_mod, "download_permitted_cached", return_value=False),
            ):
                cfg_cls.load.return_value = cfg
                resp = asyncio.run(fv.api_feature_videos_status(_dashboard_request()))
            body = json.loads(resp.body)  # type: ignore[arg-type]
        assert body["download_enabled"] is False


class TestFetchAllRoute:
    def test_a_denied_ceiling_is_a_403_and_starts_nothing(self, tmp_path: Path) -> None:
        """An ACTION chokepoint, so it takes the audited answer, not the polled memo."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            with (
                patch.object(manifest_mod, "download_denied", return_value=True),
                patch.object(cache_mod.FeatureVideoCache, "ensure_all", side_effect=AssertionError),
            ):
                resp = asyncio.run(cache_mod.api_feature_videos_fetch_all(MagicMock()))
            body = json.loads(resp.body)  # type: ignore[arg-type]
        assert resp.status == 403
        assert body["code"] == "governance_denied"
        assert cache_mod.feature_video_cache().status["download_state"] == cache_mod.STATE_DENIED

    def test_a_permitted_call_starts_one_unpaced_pass(self, tmp_path: Path) -> None:
        calls: list[bool] = []

        async def _fake_ensure_all(self: object, *, unlimited: bool = False) -> bool:
            calls.append(unlimited)
            return True

        async def run() -> web.Response:
            with (
                patch.object(manifest_mod, "download_denied", return_value=False),
                patch.object(cache_mod.FeatureVideoCache, "ensure_all", _fake_ensure_all),
            ):
                resp = await cache_mod.api_feature_videos_fetch_all(MagicMock())
                assert cache_mod._task is not None
                await cache_mod._task
                return resp

        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            resp = asyncio.run(run())
        assert resp.status == 200
        assert calls == [True]


# ── governance probe itself ──


class TestGovernanceProbe:
    def test_the_scope_is_in_the_catalog_as_a_capability(self) -> None:
        from kiro_crew.platform.governance import SCOPE_CATALOG

        spec = SCOPE_CATALOG[manifest_mod.DOWNLOAD_SCOPE]
        assert spec.capability_default is True

    def test_a_permitting_decision_reads_as_not_denied(self) -> None:
        decision = SimpleNamespace(permitted=True)
        with patch(
            "kiro_crew.platform.governance_profiles.vet_and_audit", return_value=decision
        ) as vet:
            assert manifest_mod.download_denied() is False
        # Fail-closed posture and the pinned surface are part of the contract.
        assert vet.call_args.kwargs["fail_closed"] is True
        assert vet.call_args.kwargs["session_key"] == manifest_mod.DASHBOARD_SURFACE_KEY
        assert vet.call_args.kwargs["tool_name"] == manifest_mod.AUDIT_TOOL

    def test_a_denying_decision_denies(self) -> None:
        with patch(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            return_value=SimpleNamespace(permitted=False),
        ):
            assert manifest_mod.download_denied() is True

    def test_an_unevaluable_ceiling_denies_and_is_audited(self) -> None:
        with (
            patch(
                "kiro_crew.platform.governance_profiles.vet_and_audit",
                side_effect=RuntimeError("composition failed"),
            ),
            patch("kiro_crew.sel.sel") as sel_factory,
        ):
            assert manifest_mod.download_denied() is True
        sel_factory.return_value.log_governance_decision.assert_called_once()
        kwargs = sel_factory.return_value.log_governance_decision.call_args.kwargs
        assert kwargs["outcome"] == "denied"
        assert kwargs["scope"] == manifest_mod.DOWNLOAD_SCOPE

    def test_the_memo_tracks_the_last_real_answer(self) -> None:
        """The CSP header builder cannot block, so it reads a memo — never a decision."""
        with patch(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            return_value=SimpleNamespace(permitted=True),
        ):
            manifest_mod.download_denied()
        assert manifest_mod.download_permitted_memo() is True
        with patch(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            return_value=SimpleNamespace(permitted=False),
        ):
            manifest_mod.download_denied()
        assert manifest_mod.download_permitted_memo() is False
