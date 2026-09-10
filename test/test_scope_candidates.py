"""The scope reviewer's deterministic seams must fail closed, and only pass on evidence.

``scripts/scope_candidates.py`` sits on both sides of the security-scope review: it
turns a MODEL's candidate file into a corpus the denial differential can classify,
and it folds the per-platform differential reports back into one verdict.

Both seams are places the lane could publish a false green, and a false green here
is worse than no lane at all -- the check name stands as evidence the question was
asked. So every test below stages one specific way that could happen:

*The candidate file is untrusted input.* It is written by a model, from a diff, and
a diff can carry instructions. Malformed rows, a runaway row count, and a
kilobyte-long "command" must all be exit 2 rather than a silently narrowed corpus,
because a differential over a subset nobody chose reports "no regressions" about
rows it never saw.

*An unmeasured run is not a clean run.* No report, a report whose shape is wrong,
and legs that classified nothing all mean the change was never actually judged. Each
is exit 2. Only a leg that classified rows and found no flip earns exit 0.

*The schema has one owner.* ``validate`` proves its output against
``deny_diff.load_corpus`` itself rather than a second validator that could drift, so
the round-trip tests here are the pin: what this script writes, the differential can
read, and what it proposes, a human can paste.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "scope_candidates.py"
DENY_DIFF = ROOT / "scripts" / "deny_diff.py"


def _load(name: str, path: Path):
    """Import a script by path, registered in ``sys.modules`` before exec.

    A script's dataclasses resolve their own annotations through
    ``sys.modules[cls.__module__]``, so a module executed without a registration
    raises at class-creation time rather than at use.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


scope = _load("scope_candidates", SCRIPT)
deny_diff = _load("deny_diff_for_scope", DENY_DIFF)


def _row(command: str, platform: str = "any", kind: str = "shell", reason: str = "why") -> dict:
    return {
        "kind": kind,
        "command_or_flow": command,
        "platform": platform,
        "reason": reason,
    }


def _write(path: Path, rows: list[dict]) -> Path:
    path.write_text(json.dumps({"golden_paths": rows}, indent=2), encoding="utf-8")
    return path


def _report(
    platform: str,
    *,
    classified: int,
    regressions: list[dict] | None = None,
    total_rows: int = 1,
    skipped_platform: int = 0,
    skipped_kind: int = 0,
    base_absent_tiers: list[str] | None = None,
) -> dict:
    """One report leg in ``deny_diff``'s OWN rendering, not an imitation of it.

    The leg is built as a :class:`deny_diff.Report` and rendered by
    ``render_json``, so every field here carries the type the differential
    actually emits. A hand-written stand-in describes a neighbouring module from
    memory and drifts from it, and the drift lands as a reader of ``counts``
    guessing a field's type -- which crashes mid-render instead of failing
    closed, and turns a legitimate change into a confirmed regression.
    """
    rows = regressions or []
    pairs = [
        (
            deny_diff.Row(
                index=position,
                kind=str(entry.get("kind", "shell")),
                command=str(entry.get("command", "")),
                platform=str(entry.get("platform", "any")),
                reason=str(entry.get("why_legitimate", "")),
            ),
            deny_diff.Verdict(
                denied=True,
                reason=str(entry.get("head_refusal", "")),
                tier=str(entry.get("head_tier", "")),
            ),
        )
        for position, entry in enumerate(rows)
    ]
    report = deny_diff.Report(
        base="base",
        head="head",
        base_sha="a" * 7,
        head_sha="b" * 7,
        platform=platform,
        source="corpus.json",
        total_rows=total_rows,
        skipped_kind=skipped_kind,
        skipped_platform=skipped_platform,
        base_absent_tiers=list(base_absent_tiers or []),
        regressions=pairs,
        unchanged_allowed=max(classified - len(pairs), 0),
    )
    return json.loads(deny_diff.render_json(report))


class TestValidate:
    def test_novel_candidates_survive_and_reach_the_differential(self, tmp_path: Path) -> None:
        """The kept rows must be readable by the classifier's OWN corpus parser.

        This is the round-trip that makes the single-owner schema claim true: a row
        this script accepted and the differential then rejected would surface as a
        gate that errored rather than as the malformed row it is.
        """
        candidates = _write(tmp_path / "c.json", [_row("gh pr view 1"), _row("ls -la")])
        out = tmp_path / "normalized.json"

        assert scope.main(["validate", "--candidates", str(candidates), "--out", str(out)]) == 0

        parsed = deny_diff.load_corpus(out)
        assert [row.command for row in parsed] == ["gh pr view 1", "ls -la"]

    def test_a_row_the_committed_corpus_already_holds_is_dropped(self, tmp_path: Path) -> None:
        """Re-probing a committed row spends a slot on a finding another lane owns."""
        corpus = _write(tmp_path / "base.json", [_row("gh pr view 1")])
        candidates = _write(tmp_path / "c.json", [_row("gh pr view 1"), _row("ls -la")])
        out = tmp_path / "normalized.json"

        code = scope.main(
            [
                "validate",
                "--candidates",
                str(candidates),
                "--corpus",
                str(corpus),
                "--out",
                str(out),
            ]
        )

        assert code == 0
        assert [row.command for row in deny_diff.load_corpus(out)] == ["ls -la"]

    def test_a_candidate_repeated_within_the_file_is_dropped_once(self, tmp_path: Path) -> None:
        candidates = _write(tmp_path / "c.json", [_row("ls -la"), _row("ls -la")])
        out = tmp_path / "normalized.json"

        assert scope.main(["validate", "--candidates", str(candidates), "--out", str(out)]) == 0
        assert [row.command for row in deny_diff.load_corpus(out)] == ["ls -la"]

    def test_a_platform_variant_is_its_own_row(self, tmp_path: Path) -> None:
        """Dedupe keys on platform, or the windows spelling would vanish as a dupe.

        The platform triple is half of what this lane exists to check, so the two
        spellings of one operation must both survive to be classified.
        """
        candidates = _write(
            tmp_path / "c.json",
            [_row("pytest test", platform="posix"), _row("pytest test", platform="windows")],
        )
        out = tmp_path / "normalized.json"

        assert scope.main(["validate", "--candidates", str(candidates), "--out", str(out)]) == 0
        assert {row.platform for row in deny_diff.load_corpus(out)} == {"posix", "windows"}

    def test_everything_already_covered_is_exit_3_not_a_written_empty_corpus(
        self, tmp_path: Path
    ) -> None:
        """An empty corpus would make the differential error; say so with its own code."""
        corpus = _write(tmp_path / "base.json", [_row("gh pr view 1")])
        candidates = _write(tmp_path / "c.json", [_row("gh pr view 1")])
        out = tmp_path / "normalized.json"

        code = scope.main(
            [
                "validate",
                "--candidates",
                str(candidates),
                "--corpus",
                str(corpus),
                "--out",
                str(out),
            ]
        )

        assert code == 3
        assert not out.exists()

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param("not json at all", id="unparseable"),
            pytest.param(json.dumps({"rows": []}), id="no-golden-paths-key"),
            pytest.param(json.dumps({"golden_paths": []}), id="empty"),
            pytest.param(json.dumps({"golden_paths": ["a string"]}), id="row-not-an-object"),
            pytest.param(
                json.dumps({"golden_paths": [{"kind": "nope", "command_or_flow": "ls"}]}),
                id="unknown-kind",
            ),
            pytest.param(
                json.dumps({"golden_paths": [{"kind": "shell", "command_or_flow": "  "}]}),
                id="blank-command",
            ),
            pytest.param(
                json.dumps(
                    {
                        "golden_paths": [
                            {"kind": "shell", "command_or_flow": "ls", "platform": "solaris"}
                        ]
                    }
                ),
                id="unknown-platform",
            ),
        ],
    )
    def test_an_untrustworthy_candidate_file_is_exit_2(self, tmp_path: Path, payload: str) -> None:
        """Never a narrowed corpus: a file that cannot be trusted stops the lane."""
        candidates = tmp_path / "c.json"
        candidates.write_text(payload, encoding="utf-8")
        out = tmp_path / "normalized.json"

        assert scope.main(["validate", "--candidates", str(candidates), "--out", str(out)]) == 2
        assert not out.exists()

    def test_row_count_is_capped_before_dedupe(self, tmp_path: Path) -> None:
        """Counting after dedupe would let a padded file buy itself room.

        Driven through :func:`scope.validate` with a small cap, because the caps
        have one spelling on the CLI path -- the module constants -- and a flag
        no invocation passes is a second spelling that can disagree with them.
        ``CandidateError`` is what ``main`` maps to exit 2.
        """
        candidates = _write(tmp_path / "c.json", [_row("ls -la")] * 6)
        out = tmp_path / "normalized.json"

        with pytest.raises(scope.CandidateError):
            scope.validate(candidates, None, out, max_rows=5)

        assert not out.exists()

    def test_the_module_row_cap_holds_on_the_cli_path(self, tmp_path: Path) -> None:
        """The CLI carries no cap flag, so the constant is the only ceiling it has."""
        candidates = _write(
            tmp_path / "c.json", [_row(f"ls -la {index}") for index in range(scope.MAX_ROWS + 1)]
        )
        out = tmp_path / "normalized.json"

        assert scope.main(["validate", "--candidates", str(candidates), "--out", str(out)]) == 2
        assert not out.exists()

    def test_an_oversized_command_is_refused(self, tmp_path: Path) -> None:
        """The corpus holds operations, not payloads."""
        candidates = _write(tmp_path / "c.json", [_row("ls " + "a" * 600)])
        out = tmp_path / "normalized.json"

        assert scope.main(["validate", "--candidates", str(candidates), "--out", str(out)]) == 2

    def test_a_missing_candidate_file_is_exit_2(self, tmp_path: Path) -> None:
        out = tmp_path / "normalized.json"
        code = scope.main(
            ["validate", "--candidates", str(tmp_path / "absent.json"), "--out", str(out)]
        )
        assert code == 2

    @pytest.mark.parametrize("kind", ["flow", "cron"])
    def test_a_candidate_the_classifier_cannot_classify_is_refused(
        self, tmp_path: Path, kind: str
    ) -> None:
        """A kind the differential skips would be submitted and never adjudicated.

        ``deny_diff`` counts a non-``shell`` row into ``skipped_kind`` and
        classifies nothing for it, so such a row reaches the corpus, spends a
        slot, and comes back with no verdict while the leg still reports rows
        classified. Refusing it here is what keeps the submitted corpus equal to
        the corpus that gets a verdict.
        """
        candidates = _write(tmp_path / "c.json", [_row("some flow", kind=kind)])
        out = tmp_path / "normalized.json"

        assert scope.main(["validate", "--candidates", str(candidates), "--out", str(out)]) == 2
        assert not out.exists()

    def test_the_base_corpus_may_hold_a_kind_a_candidate_may_not(self, tmp_path: Path) -> None:
        """The base corpus is read for DEDUPE only, so its rows are not candidates.

        The committed corpus legitimately carries ``flow`` and ``cron`` rows that
        another lane owns. Rejecting the base over them would take this lane down
        on a corpus it does not submit.
        """
        corpus = _write(tmp_path / "base.json", [_row("nightly", kind="cron")])
        candidates = _write(tmp_path / "c.json", [_row("ls -la")])
        out = tmp_path / "normalized.json"

        code = scope.main(
            [
                "validate",
                "--candidates",
                str(candidates),
                "--corpus",
                str(corpus),
                "--out",
                str(out),
            ]
        )

        assert code == 0
        assert [row.command for row in deny_diff.load_corpus(out)] == ["ls -la"]


class TestVerdict:
    def test_a_confirmed_regression_is_exit_1(self, tmp_path: Path) -> None:
        report = tmp_path / "posix.json"
        report.write_text(
            json.dumps(
                _report(
                    "posix",
                    classified=2,
                    regressions=[
                        {
                            "command": "gh pr view 1",
                            "platform": "any",
                            "kind": "shell",
                            "why_legitimate": "maintainers read PR state",
                            "head_tier": "rule-catalog",
                            "head_refusal": "rule=broadened-gh",
                        }
                    ],
                )
            ),
            encoding="utf-8",
        )
        body = tmp_path / "body.md"

        code = scope.main(["verdict", "--report", f"ubuntu-latest={report}", "--out-md", str(body)])

        assert code == 1
        text = body.read_text(encoding="utf-8")
        assert "gh pr view 1" in text
        # The tier decides the fix, so it must reach the reader.
        assert "rule-catalog" in text

    def test_a_clean_measured_run_is_exit_0(self, tmp_path: Path) -> None:
        report = tmp_path / "posix.json"
        report.write_text(json.dumps(_report("posix", classified=3)), encoding="utf-8")

        assert scope.main(["verdict", "--report", f"ubuntu-latest={report}"]) == 0

    def test_no_report_at_all_is_exit_2(self) -> None:
        """The caller must not reach a green by producing no evidence."""
        assert scope.main(["verdict"]) == 2

    def test_legs_that_classified_nothing_are_exit_2(self, tmp_path: Path) -> None:
        """NO VERDICT everywhere is an unmeasured change, not a pass."""
        report = tmp_path / "windows.json"
        report.write_text(
            json.dumps(_report("windows", classified=0, total_rows=2, skipped_platform=2)),
            encoding="utf-8",
        )

        assert scope.main(["verdict", "--report", f"ubuntu-latest={report}"]) == 2

    def test_one_measured_leg_beside_an_unmeasured_one_still_reports_the_gap(
        self, tmp_path: Path
    ) -> None:
        """A platform with no verdict must be named, not averaged away into a pass."""
        measured = tmp_path / "posix.json"
        measured.write_text(json.dumps(_report("posix", classified=2)), encoding="utf-8")
        unmeasured = tmp_path / "windows.json"
        unmeasured.write_text(
            json.dumps(_report("windows", classified=0, total_rows=2, skipped_platform=2)),
            encoding="utf-8",
        )
        body = tmp_path / "body.md"

        code = scope.main(
            [
                "verdict",
                "--report",
                f"ubuntu-latest={measured}",
                "--report",
                f"windows-latest={unmeasured}",
                "--out-md",
                str(body),
            ]
        )

        assert code == 0
        text = body.read_text(encoding="utf-8")
        # Named by its LEG, which is what a reader can map back to a job: the
        # corpus platform is now never the label, because two hosts share one.
        assert "windows-latest (platform windows): NO VERDICT" in text
        assert "Not a pass for this platform." in text

    def test_a_leg_the_caller_required_and_did_not_hand_over_is_exit_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The fold sees only the reports it was handed, so absence must be declared.

        A leg whose job died uploads nothing. Folding the survivors would publish
        a clean verdict for a platform that never reported, which is the false
        green this lane exists to prevent -- so the caller names the legs it
        requires and a missing one stops the fold, by name.
        """
        present = tmp_path / "ubuntu.json"
        present.write_text(json.dumps(_report("posix", classified=2)), encoding="utf-8")

        code = scope.main(
            [
                "verdict",
                "--report",
                f"ubuntu-latest={present}",
                "--expect-leg",
                "ubuntu-latest",
                "--expect-leg",
                "windows-latest",
            ]
        )

        assert code == 2
        assert "windows-latest" in capsys.readouterr().err

    def test_every_required_leg_present_folds_to_a_verdict(self, tmp_path: Path) -> None:
        posix = tmp_path / "ubuntu.json"
        posix.write_text(json.dumps(_report("posix", classified=2)), encoding="utf-8")
        windows = tmp_path / "windows.json"
        windows.write_text(json.dumps(_report("windows", classified=2)), encoding="utf-8")

        code = scope.main(
            [
                "verdict",
                "--report",
                f"ubuntu-latest={posix}",
                "--report",
                f"windows-latest={windows}",
                "--expect-leg",
                "ubuntu-latest",
                "--expect-leg",
                "windows-latest",
            ]
        )

        assert code == 0

    def test_an_unlabelled_report_is_refused_rather_than_named_for_it(self, tmp_path: Path) -> None:
        """``--report PATH`` with no label is refused, not guessed at.

        The bare form once took its label from the report's own ``platform``,
        which made the label a thing the caller could not predict: two hosts map
        to ``posix``, so two bare reports rendered under one name and a reader
        could not tell a duplicated leg from an absent one. Both callers always
        pass ``LABEL=PATH``, so the accepted shape is now exactly one.
        """
        report = tmp_path / "posix.json"
        report.write_text(json.dumps(_report("posix", classified=2)), encoding="utf-8")

        with pytest.raises(scope.CandidateError) as excinfo:
            scope.verdict([str(report)], ["posix"], None, None)

        assert "must be LABEL=PATH" in str(excinfo.value)

    def test_a_label_with_no_path_is_refused(self, tmp_path: Path) -> None:
        """``LABEL=`` names a leg and hands over no report to fold into it."""
        with pytest.raises(scope.CandidateError) as excinfo:
            scope.verdict(["ubuntu-latest="], [], None, None)

        assert "must be LABEL=PATH" in str(excinfo.value)

    def test_two_legs_on_one_platform_are_told_apart_by_their_labels(self, tmp_path: Path) -> None:
        """Two hosts map to ``posix``, and two identical lines hide a missing leg.

        A reader who cannot tell which OS reported which line cannot tell a
        duplicated leg from an absent one either, so each leg renders under the
        label its caller gave it.
        """
        ubuntu = tmp_path / "ubuntu.json"
        ubuntu.write_text(json.dumps(_report("posix", classified=11)), encoding="utf-8")
        macos = tmp_path / "macos.json"
        macos.write_text(json.dumps(_report("posix", classified=7)), encoding="utf-8")
        body = tmp_path / "body.md"

        code = scope.main(
            [
                "verdict",
                "--report",
                f"ubuntu-latest={ubuntu}",
                "--report",
                f"macos-latest={macos}",
                "--out-md",
                str(body),
            ]
        )

        assert code == 0
        text = body.read_text(encoding="utf-8")
        assert "ubuntu-latest" in text
        assert "macos-latest" in text
        # The corpus platform stays visible beside the label: the label says which
        # host reported, the platform says which rows it was eligible to classify.
        assert text.count("posix") >= 2
        leg_lines = [line for line in text.splitlines() if "classified" in line]
        assert len(leg_lines) == len(set(leg_lines)) == 2

    def test_a_row_no_reporting_leg_is_eligible_for_is_exit_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A row every leg skipped on platform grounds is classified NOWHERE.

        The per-leg check is satisfied by the rows the other legs did classify, so
        without a row-coverage check the fold publishes a green badge over a
        candidate no host was ever asked about -- this lane's worst failure mode.
        Here a three-row corpus holds one `windows` row and only a `posix` leg
        reports: the leg classifies two rows, skips the third, and nothing else
        ever looks at it.
        """
        report = tmp_path / "ubuntu.json"
        report.write_text(
            json.dumps(_report("posix", classified=2, total_rows=3, skipped_platform=1)),
            encoding="utf-8",
        )

        code = scope.main(["verdict", "--report", f"ubuntu-latest={report}"])

        assert code == 2
        err = capsys.readouterr().err
        # The platform nobody covered, and the leg whose skip revealed it.
        assert "windows" in err
        assert "ubuntu-latest" in err

    def test_a_single_platform_corpus_with_a_zero_classifying_leg_stays_clean(
        self, tmp_path: Path
    ) -> None:
        """The false refusal the naive per-leg fix would cause. Pinned so it stays gone.

        A corpus of only `posix` rows is honest: the `posix` leg classifies every
        row and the `windows` leg classifies none, reporting all of them as
        OTHER-PLATFORM. "Exit 2 when any leg classified zero" would red this run,
        and every run whose corpus has no Windows-eligible row. Every row here HAS
        a leg eligible for it, so the fold is clean and the markdown says NO
        VERDICT for the leg that had nothing to answer.
        """
        posix = tmp_path / "ubuntu.json"
        posix.write_text(
            json.dumps(_report("posix", classified=3, total_rows=3, skipped_platform=0)),
            encoding="utf-8",
        )
        windows = tmp_path / "windows.json"
        windows.write_text(
            json.dumps(_report("windows", classified=0, total_rows=3, skipped_platform=3)),
            encoding="utf-8",
        )
        body = tmp_path / "body.md"

        code = scope.main(
            [
                "verdict",
                "--report",
                f"ubuntu-latest={posix}",
                "--report",
                f"windows-latest={windows}",
                "--out-md",
                str(body),
            ]
        )

        assert code == 0
        assert "windows-latest (platform windows): NO VERDICT" in body.read_text(encoding="utf-8")

    def test_an_any_platform_leg_does_not_invent_an_uncovered_platform(
        self, tmp_path: Path
    ) -> None:
        """`deny_diff --platform any` skips rows it cannot attribute to a platform.

        A leg reporting platform `any` classifies only the `any` rows, so against a
        posix-only corpus it skips every row -- and its aggregate count cannot say
        WHICH concrete platform those rows were pinned to. Reading that skip as
        evidence refuses a corpus whose rows are all covered: three posix rows, a
        posix leg that classifies them, and this leg reads as an uncovered WINDOWS
        row that does not exist. That would be this lane over-refusing a legitimate
        run, which is the failure it exists to catch.
        """
        posix = tmp_path / "ubuntu.json"
        posix.write_text(
            json.dumps(_report("posix", classified=3, total_rows=3, skipped_platform=0)),
            encoding="utf-8",
        )
        anyleg = tmp_path / "any.json"
        anyleg.write_text(
            json.dumps(_report("any", classified=0, total_rows=3, skipped_platform=3)),
            encoding="utf-8",
        )

        code = scope.main(
            [
                "verdict",
                "--report",
                f"ubuntu-latest={posix}",
                "--report",
                f"any-leg={anyleg}",
            ]
        )

        assert code == 0

    def test_only_any_platform_legs_reporting_skipped_rows_is_exit_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """With no concrete leg at all, a skipped row IS provably uncovered.

        The leg above cannot name the platform it skipped, but when NO leg reported
        for a concrete platform there is nothing to have covered those rows, so the
        gap is certain rather than merely possible. Fail closed and name both
        platforms, rather than passing a verdict over rows nobody classified.
        """
        anyleg = tmp_path / "any.json"
        anyleg.write_text(
            json.dumps(_report("any", classified=1, total_rows=3, skipped_platform=2)),
            encoding="utf-8",
        )

        code = scope.main(["verdict", "--report", f"any-leg={anyleg}"])

        assert code == 2
        err = capsys.readouterr().err
        assert "posix" in err
        assert "windows" in err

    def test_a_mixed_corpus_with_every_platform_reporting_stays_clean(self, tmp_path: Path) -> None:
        """Rows of both platforms, each with an eligible leg: nothing is uncovered.

        Each leg skips the other platform's rows, and the check must read that as
        covered rather than as a gap -- otherwise the normal three-leg run reds.
        """
        posix = tmp_path / "ubuntu.json"
        posix.write_text(
            json.dumps(_report("posix", classified=4, total_rows=5, skipped_platform=1)),
            encoding="utf-8",
        )
        windows = tmp_path / "windows.json"
        windows.write_text(
            json.dumps(_report("windows", classified=3, total_rows=5, skipped_platform=2)),
            encoding="utf-8",
        )

        code = scope.main(
            [
                "verdict",
                "--report",
                f"ubuntu-latest={posix}",
                "--report",
                f"windows-latest={windows}",
            ]
        )

        assert code == 0

    def test_a_row_no_classifier_could_settle_is_exit_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A skipped-on-kind row is an unsettled question, not a row that passed.

        The differential classifies nothing for it, yet the leg still counts other
        rows as classified -- so folding the leg as measured publishes a verdict
        over a candidate that never got one. The leg is named so the reader knows
        where the unclassifiable row was submitted.
        """
        report = tmp_path / "ubuntu.json"
        report.write_text(
            json.dumps(_report("posix", classified=2, total_rows=3, skipped_kind=1)),
            encoding="utf-8",
        )

        code = scope.main(["verdict", "--report", f"ubuntu-latest={report}"])

        assert code == 2
        # Named by leg, so the reader knows which submission carried the row.
        assert "ubuntu-latest" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param("{", id="unparseable"),
            pytest.param(json.dumps([]), id="not-an-object"),
            pytest.param(json.dumps({"platform": "posix", "counts": {}}), id="no-regressions-key"),
            pytest.param(
                json.dumps({"platform": "posix", "counts": [], "regressions": []}),
                id="malformed-counts",
            ),
        ],
    )
    def test_a_report_that_cannot_be_read_is_exit_2(self, tmp_path: Path, payload: str) -> None:
        report = tmp_path / "leg.json"
        report.write_text(payload, encoding="utf-8")

        assert scope.main(["verdict", "--report", f"ubuntu-latest={report}"]) == 2

    def test_a_tier_absent_at_the_base_ref_is_named_and_is_not_a_regression(
        self, tmp_path: Path
    ) -> None:
        """``base_absent_tiers`` is a list of tier NAMES, and reading it must not crash.

        A change that adds a deny check reports the check as absent at the base
        ref. Treating that field as a count raises inside rendering, Python turns
        an escaping exception into exit 1, and exit 1 is this lane's claim that
        the classifier confirmed a newly-refused operation -- so the crash
        publishes a block against a change with no regression in it. The names
        reach the reader because the name is what says which check is new.
        """
        report = tmp_path / "posix.json"
        report.write_text(
            json.dumps(_report("posix", classified=2, base_absent_tiers=["exfil", "deny-rules"])),
            encoding="utf-8",
        )
        body = tmp_path / "body.md"

        code = scope.main(["verdict", "--report", f"ubuntu-latest={report}", "--out-md", str(body)])

        assert code == 0
        text = body.read_text(encoding="utf-8")
        assert "exfil" in text
        assert "deny-rules" in text

    def test_a_report_whose_tier_list_is_not_a_list_is_exit_2(self, tmp_path: Path) -> None:
        """A field of the wrong type is an unreadable report, never a finding."""
        payload = _report("posix", classified=1)
        payload["counts"]["base_absent_tiers"] = 3
        report = tmp_path / "posix.json"
        report.write_text(json.dumps(payload), encoding="utf-8")

        assert scope.main(["verdict", "--report", f"ubuntu-latest={report}"]) == 2

    def test_a_count_of_the_wrong_type_is_exit_2(self, tmp_path: Path) -> None:
        """The reader folds the counts, so a count it cannot fold is unsettled."""
        payload = _report("posix", classified=1)
        payload["counts"]["classified"] = "1"
        report = tmp_path / "posix.json"
        report.write_text(json.dumps(payload), encoding="utf-8")

        assert scope.main(["verdict", "--report", f"ubuntu-latest={report}"]) == 2

    def test_an_unexpected_internal_error_is_exit_2_never_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crash is an unsettled question, and exit 1 is a factual claim.

        Python exits 1 on an escaping exception, which collides with the code
        that means "the classifier confirmed a newly-refused operation". Any
        failure that is not that finding must arrive as exit 2, or an internal
        error publishes a block nothing measured.
        """
        report = tmp_path / "posix.json"
        report.write_text(
            json.dumps(
                _report(
                    "posix",
                    classified=1,
                    regressions=[
                        {
                            "command": "gh pr view 1",
                            "platform": "any",
                            "kind": "shell",
                            "why_legitimate": "maintainers read PR state",
                            "head_tier": "rule-catalog",
                            "head_refusal": "rule=broadened-gh",
                        }
                    ],
                )
            ),
            encoding="utf-8",
        )

        def _boom(path: Path) -> dict:
            raise RuntimeError("a defect nobody anticipated")

        monkeypatch.setattr(scope, "_report_leg", _boom)

        assert scope.main(["verdict", "--report", f"ubuntu-latest={report}"]) == 2

    def test_the_report_fixture_carries_the_field_types_deny_diff_emits(self) -> None:
        """The fixture is ``render_json``'s output, so a shape change breaks a test here.

        A report described from memory drifts from the one the differential
        writes, and the drift is invisible until CI reads the real thing.
        """
        payload = _report("posix", classified=1, base_absent_tiers=["exfil"])

        assert payload["counts"]["base_absent_tiers"] == ["exfil"]
        assert isinstance(payload["counts"]["classified"], int)
        assert {"platform", "counts", "regressions"} <= set(payload)

    def test_proposed_rows_are_a_corpus_a_human_can_paste(self, tmp_path: Path) -> None:
        """A confirmed regression IS the row the corpus was missing.

        The paste-ready output must therefore satisfy the committed corpus's own
        parser, or the reviewer's only actionable artifact is a snippet that would
        break the gate it is meant to feed.
        """
        report = tmp_path / "posix.json"
        report.write_text(
            json.dumps(
                _report(
                    "posix",
                    classified=1,
                    regressions=[
                        {
                            "command": "py -3 -m pytest test\\unit",
                            "platform": "windows",
                            "kind": "shell",
                            "why_legitimate": "the Windows spelling of the test run",
                            "head_tier": "argv-floor",
                            "head_refusal": "rule=inline-interpreter",
                        }
                    ],
                )
            ),
            encoding="utf-8",
        )
        rows = tmp_path / "rows.json"

        assert (
            scope.main(["verdict", "--report", f"ubuntu-latest={report}", "--out-rows", str(rows)])
            == 1
        )

        parsed = deny_diff.load_corpus(rows)
        assert len(parsed) == 1
        assert parsed[0].platform == "windows"
        assert parsed[0].reason == "the Windows spelling of the test run"


# --------------------------------------------------------------------------- #
# conclude -- the ONE conclusion table both lanes now share.
# --------------------------------------------------------------------------- #

_FOLDS = ("clean", "regression", "redacted", "unscrubbable", "error", "no-report")
_MODELS = ("PASS", "CONCERNS", "BLOCK", "UNKNOWN")
_BOOLS = (False, True)

#: Blocking severity. `pass` is the softest, the two reds the hardest. Ranks
#: conclusions so a test can assert the model-authored gap only ever TIGHTENS a
#: conclusion and never softens one -- the property that makes it safe to honour
#: an untrusted signal on a
#: merge-blocking gate.
_SEVERITY = {"pass": 0, "nothing-new": 1, "concerns": 2, "error": 3, "block": 3}


def _all_inputs():
    for fold, model, marker, nothing_new, gap_s, gap_m in itertools.product(
        _FOLDS, _MODELS, _BOOLS, _BOOLS, _BOOLS, _BOOLS
    ):
        yield dict(
            fold=fold,
            model=model,
            marker_present=marker,
            nothing_new=nothing_new,
            gap_script=gap_s,
            gap_model=gap_m,
        )


class TestConcludeTableIsOneTableForBothLanes:
    """One Python table decides both lanes, so for the SAME inputs a fork and a
    same-repo run reach the SAME conclusion. These tests assert that property
    across the whole input grid -- a per-lane implementation drifts on the
    platform-gap source, which is the divergence the property forbids.
    """

    def test_no_input_combination_differs_by_lane(self) -> None:
        # (FOLDED x marker x model header x gap x lane), every combination the two
        # shell blocks can reach. The ruling resolved the one asymmetry to the
        # stricter side, so NO row deliberately differs by lane -- assert exactly
        # that, across the whole grid.
        diverged = []
        for kwargs in _all_inputs():
            fork = scope.conclude_lane(lane="fork", **kwargs)
            same = scope.conclude_lane(lane="same-repo", **kwargs)
            if fork[0] != same[0]:
                diverged.append((kwargs, fork[0], same[0]))
        assert not diverged, f"lane divergence in {len(diverged)} rows, e.g. {diverged[:3]}"

    def test_the_conclusion_vocabulary_is_closed(self) -> None:
        for kwargs in _all_inputs():
            conclusion, why = scope.conclude_lane(lane="fork", **kwargs)
            assert conclusion in _SEVERITY, (conclusion, kwargs)
            assert why and "\n" not in why


class TestConcludeStricterGapResolution:
    """The exact defect this change removes: an unadjudicable tightening that a
    model marked in PROSE only. It blocked same-repo (which read the review's
    UNADJUDICATED: marker) and published `neutral` on a fork (which read the
    script body alone) -- the softer verdict against the more hostile source.
    """

    def test_model_prose_gap_now_blocks_on_both_lanes(self) -> None:
        for lane in ("fork", "same-repo"):
            conclusion, _ = scope.conclude_lane(
                lane=lane,
                fold="clean",
                model="BLOCK",
                marker_present=True,
                nothing_new=False,
                gap_script=False,
                gap_model=True,
            )
            assert conclusion == "block", lane

    def test_script_gap_blocks_and_no_gap_is_advisory(self) -> None:
        base = dict(fold="clean", model="BLOCK", marker_present=True, nothing_new=False)
        for lane in ("fork", "same-repo"):
            assert scope.conclude_lane(lane=lane, gap_script=True, gap_model=False, **base)[0] == "block"
            assert scope.conclude_lane(lane=lane, gap_script=False, gap_model=False, **base)[0] == "concerns"

    def test_the_model_gap_can_only_tighten_never_soften(self) -> None:
        # The untrusted signal is honoured ONLY where it makes the verdict
        # stricter. So for every other input held equal, turning gap_model on can
        # never lower the blocking severity.
        for kwargs in _all_inputs():
            if kwargs["gap_model"]:
                continue
            without = scope.conclude_lane(lane="fork", **kwargs)[0]
            with_ = scope.conclude_lane(lane="fork", **{**kwargs, "gap_model": True})[0]
            assert _SEVERITY[with_] >= _SEVERITY[without], (kwargs, without, with_)


class TestConcludeFailsClosed:
    """A run that could not settle is red by default, and the whole class of them
    routes through ONE constant -- the flip point for the still-open fail-closed
    vs neutral question.
    """

    def test_a_confirmed_regression_always_blocks(self) -> None:
        for model, marker, gap_s, gap_m in itertools.product(_MODELS, _BOOLS, _BOOLS, _BOOLS):
            conclusion, _ = scope.conclude_lane(
                lane="fork",
                fold="regression",
                model=model,
                marker_present=marker,
                nothing_new=False,
                gap_script=gap_s,
                gap_model=gap_m,
            )
            assert conclusion == "block", (model, marker)

    def test_every_unsettled_outcome_is_the_single_flip_constant(self) -> None:
        # These are exactly the "could not settle" rows. Each returns the one
        # constant, so flipping fail-closed -> neutral is a single edit and touches
        # nothing else. Default is the stricter answer.
        unsettled = [
            dict(fold="redacted", model="PASS", marker_present=True),
            dict(fold="unscrubbable", model="PASS", marker_present=True),
            dict(fold="error", model="PASS", marker_present=True),
            dict(fold="no-report", model="PASS", marker_present=True, nothing_new=False),
            dict(fold="no-report", model="PASS", marker_present=False, nothing_new=True),
            dict(fold="clean", model="PASS", marker_present=False),
            dict(fold="clean", model="UNKNOWN", marker_present=True),
        ]
        for kwargs in unsettled:
            kwargs.setdefault("nothing_new", False)
            kwargs.setdefault("gap_script", False)
            kwargs.setdefault("gap_model", False)
            conclusion, _ = scope.conclude_lane(lane="same-repo", **kwargs)
            assert conclusion == scope._UNSETTLED_CONCLUSION, kwargs

    def test_fail_closed_default_is_error_not_neutral(self) -> None:
        # Guards the ruling: the shipped default is the stricter answer. If someone
        # flips the constant to make errored runs neutral, this test is the place
        # that records the decision was made deliberately.
        assert scope._UNSETTLED_CONCLUSION == "error"


class TestConcludeNothingNew:
    def test_nothing_new_is_green_only_with_a_marker(self) -> None:
        with_marker = scope.conclude_lane(
            lane="fork", fold="no-report", model="UNKNOWN", marker_present=True,
            nothing_new=True, gap_script=False, gap_model=False,
        )
        without = scope.conclude_lane(
            lane="fork", fold="no-report", model="UNKNOWN", marker_present=False,
            nothing_new=True, gap_script=False, gap_model=False,
        )
        assert with_marker[0] == "nothing-new"
        assert without[0] == scope._UNSETTLED_CONCLUSION


class TestConcludeCli:
    def test_cli_prints_github_output_lines_and_exits_zero(self, capsys) -> None:
        rc = scope.main(
            ["conclude", "--lane", "fork", "--fold", "clean", "--model", "PASS", "--marker", "present"]
        )
        assert rc == 0
        out = capsys.readouterr().out.splitlines()
        assert out == ["conclusion=pass", "why=adjudicated, zero confirmed regressions"]

    def test_cli_lowercases_and_normalizes_the_header(self, capsys) -> None:
        # The shell already uppercases, but a lenient normalizer keeps the contract
        # in ONE place: anything not PASS/CONCERNS/BLOCK reads as UNKNOWN.
        scope.main(["conclude", "--lane", "same-repo", "--fold", "clean", "--model", "wat", "--marker", "present"])
        assert "conclusion=error" in capsys.readouterr().out

    def test_cli_rejects_an_unknown_fold_as_exit_two(self) -> None:
        with pytest.raises(SystemExit) as exc:
            scope.main(["conclude", "--lane", "fork", "--fold", "bogus"])
        assert exc.value.code == 2

    def test_cli_rejects_an_unknown_lane(self) -> None:
        with pytest.raises(SystemExit) as exc:
            scope.main(["conclude", "--lane", "sideways", "--fold", "clean"])
        assert exc.value.code == 2
