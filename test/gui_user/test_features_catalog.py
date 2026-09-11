"""The feature inventory: features.json is well-formed and FEATURES.md is rendered from it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from gui_user import features_catalog as fc
from gui_user import scenarios

HERE = Path(__file__).parent


class TestShippedInventory:
    def test_features_json_validates(self) -> None:
        doc = fc.load()
        records = fc.validate(doc)
        assert len(records) == doc["counts"]["total"] >= 200
        assert {r["feature"] for r in records} <= set(fc.FEATURE_TITLES)

    def test_registry_matches_the_scenario_loader(self) -> None:
        # One taxonomy: a slug the inventory groups by is a slug a scenario may claim.
        # The loader's registry ships with the feature/user_story scenario schema; on a
        # branch without it the check is skipped, not faked -- once both are on main the
        # two must agree.
        registry = getattr(scenarios, "FEATURES", None)
        if registry is None:
            pytest.skip("scenarios.FEATURES is not on this branch yet")
        assert fc.FEATURE_TITLES == registry

    def test_features_md_is_rendered_from_features_json(self) -> None:
        assert (HERE / "FEATURES.md").read_text(encoding="utf-8") == fc.render(fc.load())

    def test_every_p0_is_runnable_on_the_target(self) -> None:
        # P0 is "first scenario batch"; a P0 the lane cannot drive is a contradiction.
        for r in fc.validate(fc.load()):
            if r["priority"] == "P0":
                assert r["runnable"] in ("smoke", "nightly"), r["id"]
            if r["runnable"] in ("native-only", "needs-secret", "excluded"):
                assert r["priority"] == "P3", r["id"]

    def test_seeds_are_shipped_fixtures(self) -> None:
        fixtures = {
            p.name
            for p in (HERE.parents[1] / "src" / "kiro_crew" / "tests_fixtures").iterdir()
            if p.is_dir()
        }
        for r in fc.validate(fc.load()):
            seed = r["preconditions"].get("seed")
            assert seed in fixtures, f"{r['id']}: seed {seed!r} is not a fixture"


def _doc(**overrides):
    rec = {
        "id": "chat-demo",
        "feature": "chat",
        "title": "Demo",
        "user_story": "As a user, I want a thing, so that it is done.",
        "start_url": "/chat",
        "entry_path": "rail",
        "preconditions": {"seed": "rich", "members": [], "feature_flags": [], "notes": ""},
        "runnable": "smoke",
        "estimated_steps": 3,
        "rationale": "r",
        "source": ["docs/feature-map/README.md"],
        "priority": "P1",
        "merged_from": ["chat-demo"],
    }
    rec.update(overrides)
    return {"generated_from_sha": "abc", "counts": fc.compute_counts([rec]), "features": [rec]}


class TestValidation:
    def test_minimal_document_renders(self) -> None:
        md = fc.render(_doc())
        assert "## Chat sessions (`chat`)" in md
        assert "| P1 | `chat-demo` |" in md
        assert "| **All features** | 1 | 1 | 0 | 0 | 0 | 0 |" in md

    @pytest.mark.parametrize(
        "overrides,match",
        [
            ({"feature": "terminal"}, "not a registry slug"),
            ({"start_url": "chat"}, "absolute path"),
            ({"start_url": "/capabilities?tab=knowledge"}, "absolute path"),
            ({"runnable": "weekly"}, "runnable"),
            ({"priority": "P9"}, "priority"),
            ({"estimated_steps": "3"}, "estimated_steps"),
            ({"estimated_steps": True}, "estimated_steps"),
            ({"source": "x"}, "must be lists"),
        ],
    )
    def test_rejects_malformed_records(self, overrides, match) -> None:
        with pytest.raises(fc.CatalogError, match=match):
            fc.validate(_doc(**overrides))

    def test_rejects_missing_key_duplicate_id_and_stale_counts(self) -> None:
        doc = _doc()
        del doc["features"][0]["rationale"]
        with pytest.raises(fc.CatalogError, match="missing rationale"):
            fc.validate(doc)
        doc = _doc()
        doc["features"].append(dict(doc["features"][0]))
        doc["counts"] = fc.compute_counts(doc["features"])
        with pytest.raises(fc.CatalogError, match="duplicate id"):
            fc.validate(doc)
        doc = _doc()
        doc["counts"]["smoke"] = 7
        with pytest.raises(fc.CatalogError, match="counts"):
            fc.validate(doc)

    def test_rejects_non_mapping_records_without_crashing(self) -> None:
        for bad in ("chat-demo", 7, None, ["chat-demo"]):
            doc = _doc()
            doc["features"].append(bad)
            doc["counts"] = fc.compute_counts(doc["features"])
            with pytest.raises(fc.CatalogError, match="must be a mapping"):
                fc.validate(doc)

    def test_rejects_out_of_order_records(self) -> None:
        doc = _doc()
        second = dict(doc["features"][0], id="chat-aaa")
        doc["features"].append(second)  # 'chat-aaa' sorts before 'chat-demo'
        doc["counts"] = fc.compute_counts(doc["features"])
        with pytest.raises(fc.CatalogError, match="ordered"):
            fc.validate(doc)

    def test_cell_neutralizes_pipes_and_newlines(self) -> None:
        md = fc.render(_doc(user_story="a | b\nc"))
        assert "a / b c" in md and "a | b" not in md


class TestCli:
    def test_check_and_write(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        j = tmp_path / "features.json"
        m = tmp_path / "FEATURES.md"
        j.write_text(json.dumps(_doc()), encoding="utf-8")
        monkeypatch.setattr(fc, "FEATURES_JSON", j)
        monkeypatch.setattr(fc, "FEATURES_MD", m)
        assert fc.main(["--check"]) == 1  # no markdown yet
        assert "stale" in capsys.readouterr().err
        assert fc.main(["--write"]) == 0
        assert fc.main(["--check"]) == 0
        j.write_text("not json", encoding="utf-8")
        assert fc.main(["--check"]) == 2
