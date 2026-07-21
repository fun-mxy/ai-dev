"""``ai-dev final-report`` generator (v0.3, ADR-0003 D5/D6/D7, ticket 09).

The §23.5 step-21 projection writer. These tests pin: the §2.1 five-question
skeleton (keys always present), the fail-loud refusal on a null verdict, the
D6 failure-shape (``failure_class`` + ``blocking_reasons[]``) for both pass and
fail, the code->requirement known-gap discipline, recomputability, defensive
generation on missing *optional* artifacts vs fail-loud on missing *required*
ones, and the CLI exit contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ai_dev.cli import main
from ai_dev.coherence_gate import evaluate_coherence_gate
from ai_dev.final_report import (
    FINAL_REPORT_JSON,
    FINAL_REPORT_MD,
    FIVE_QUESTION_KEYS,
    FinalReportResult,
    generate_final_report,
)
from ai_dev.json_artifact import write_json
from ai_dev.templates import REQUIREMENTS_JSON
from ai_dev.triage import REQUEST_CHANGE_PROPOSAL, apply_triage
from ai_dev.issue_bundle import collect_issue_bundle

from test_coherence_gate import _stage_coherence_inputs  # noqa: E402
from test_implement_leg import _feature_root  # noqa: E402
from test_issue_bundle import _issue  # noqa: E402


def _generate(repo_root: Path, feature_id: str) -> dict[str, Any]:
    """Generate the report and return the parsed JSON."""
    generate_final_report(repo_root, feature_id)
    path = _feature_root(repo_root, feature_id) / FINAL_REPORT_JSON
    return json.loads(path.read_text())


def _seed_requirements(repo_root: Path, feature_id: str) -> None:
    """Fill 01-requirements.json with REQ-001 + AC-001 so coverage sections answer."""
    root = _feature_root(repo_root, feature_id)
    write_json(
        root / REQUIREMENTS_JSON,
        {
            "feature": feature_id,
            "frozen": True,
            "requirements": [{"id": "REQ-001", "statement": "answer() returns 42"}],
            "acceptance_criteria": [{"id": "AC-001", "statement": "answer() == 42"}],
            "priority": None,
            "scope": None,
            "constraints": [],
            "open_questions": [],
        },
    )


class TestFiveKeySkeleton:
    """ADR-0003 D5/OQ11: the five §2.1 question keys + meta + failure-shape are
    always present (mechanically checkable), values may be empty."""

    def test_pass_report_has_all_top_level_keys(self, repo_root: Path) -> None:
        feature_id, _lane_id = _stage_coherence_inputs(repo_root)
        evaluate_coherence_gate(repo_root, feature_id)

        report = _generate(repo_root, feature_id)

        for key in FIVE_QUESTION_KEYS:
            assert key in report, f"five-question key {key!r} missing"
        assert "meta" in report
        assert "verdict" in report
        assert "failure_class" in report
        assert "blocking_reasons" in report
        # Values may be empty (lists), but the keys are present.
        for key in FIVE_QUESTION_KEYS:
            assert isinstance(report[key], list)

    def test_pass_report_verdict_and_empty_failure_shape(self, repo_root: Path) -> None:
        feature_id, _lane_id = _stage_coherence_inputs(repo_root)
        evaluate_coherence_gate(repo_root, feature_id)

        report = _generate(repo_root, feature_id)

        assert report["verdict"] == "pass"
        assert report["failure_class"] is None
        assert report["blocking_reasons"] == []
        assert report["meta"]["feature_status"] == "done"
        assert report["meta"]["current_gate"] == "feature_coherence_gate"

    def test_meta_carries_coherence_conditions(self, repo_root: Path) -> None:
        feature_id, _lane_id = _stage_coherence_inputs(repo_root)
        evaluate_coherence_gate(repo_root, feature_id)

        report = _generate(repo_root, feature_id)

        names = [c["name"] for c in report["meta"]["coherence_conditions"]]
        assert names == [
            "status_consistent",
            "lane_passed_and_p0_p1_handled",
            "decisions_recorded",
        ]
        assert all(c["passed"] for c in report["meta"]["coherence_conditions"])


class TestPassReportSections:
    """A pass report still answers all five questions with real projections."""

    def test_code_to_requirement_maps_changed_files_to_requirements(
        self, repo_root: Path
    ) -> None:
        feature_id, _lane_id = _stage_coherence_inputs(repo_root)
        _seed_requirements(repo_root, feature_id)
        evaluate_coherence_gate(repo_root, feature_id)

        report = _generate(repo_root, feature_id)

        # RUN-001's metadata carries changed_files; the implement-result declared
        # related_requirements=["REQ-001"], so each changed file maps to REQ-001.
        index = report["code_to_requirement"]
        files = {entry["file"] for entry in index}
        assert "workspace/hello.py" in files
        for entry in index:
            if entry["file"] == "workspace/hello.py":
                assert entry["source_run"] == "RUN-001"
                assert entry["requirements"] == ["REQ-001"]
        # changed_files were contributed -> no Q1 known gap.
        assert not any(
            "code_to_requirement" in g for g in report["meta"]["known_gaps"]
        )

    def test_requirement_coverage_and_acceptance_verification(
        self, repo_root: Path
    ) -> None:
        feature_id, _lane_id = _stage_coherence_inputs(repo_root)
        _seed_requirements(repo_root, feature_id)
        evaluate_coherence_gate(repo_root, feature_id)

        report = _generate(repo_root, feature_id)

        cov = report["requirement_coverage"]
        assert [row["requirement"] for row in cov] == ["REQ-001"]
        assert cov[0]["implemented"] is True
        assert cov[0]["evidence_runs"] == ["RUN-001"]

        ver = report["acceptance_verification"]
        assert [row["acceptance_criterion"] for row in ver] == ["AC-001"]
        assert ver[0]["verified"] is True
        assert ver[0]["evidence_runs"] == ["RUN-001"]
        assert ver[0]["lane_verification"] == "pass"
        # AC->test traceability is a known v0.3 limitation once the section is
        # non-empty (D5 constraint 3's sibling discipline).
        assert any("acceptance_verification" in g for g in report["meta"]["known_gaps"])

    def test_agent_timeline_records_profile_role_and_times(self, repo_root: Path) -> None:
        feature_id, _lane_id = _stage_coherence_inputs(repo_root)
        evaluate_coherence_gate(repo_root, feature_id)

        report = _generate(repo_root, feature_id)

        timeline = report["agent_timeline"]
        assert len(timeline) >= 1
        impl = next(e for e in timeline if e["run_id"] == "RUN-001")
        assert impl["profile"] == "cc-glm52"
        assert impl["role"] == "Implementer"
        assert impl["started_at"] == "2026-07-20T10:00:00Z"
        assert impl["exit_code"] == 0
        assert "workspace/hello.py" in impl["changed_files"]

    def test_issue_dispositions_empty_on_clean_feature(self, repo_root: Path) -> None:
        feature_id, _lane_id = _stage_coherence_inputs(repo_root)
        evaluate_coherence_gate(repo_root, feature_id)

        report = _generate(repo_root, feature_id)

        assert report["issue_dispositions"] == []


class TestFailReport:
    """D6: a FAIL report exists and still answers all five questions; the
    failure-shape classifies recoverable vs terminal."""

    def test_fail_report_recoverable_pending_triage(self, repo_root: Path) -> None:
        feature_id, lane_id = _stage_coherence_inputs(
            repo_root,
            review_issues=[
                _issue(
                    id="agent-review-p1",
                    source="code_review",
                    severity="P1",
                    title="Unhandled P1",
                )
            ],
        )
        evaluate_coherence_gate(repo_root, feature_id)  # verdict=fail

        report = _generate(repo_root, feature_id)

        assert report["verdict"] == "fail"
        assert report["failure_class"] == "recoverable"
        # FAIL report still has all five keys (answers all five questions).
        for key in FIVE_QUESTION_KEYS:
            assert key in report
        # The unhandled P1 surfaces as a recoverable pending_triage reason.
        reasons = report["blocking_reasons"]
        assert reasons, "blocking_reasons must be non-empty on a fail"
        assert any(r["kind"] == "pending_triage" for r in reasons)
        issue_reasons = [r for r in reasons if r["issue_id"] == "ISSUE-001"]
        assert issue_reasons
        assert all(r["resolution_path"] == "human_triage" for r in issue_reasons)
        assert all(r["class"] == "recoverable" for r in issue_reasons)
        # Seeded from the coherence failed condition too.
        assert any(r["kind"].startswith("coherence_condition:") for r in reasons)

    def test_fail_report_terminal_on_request_change_proposal(
        self, repo_root: Path
    ) -> None:
        # A P1 triaged request_change_proposal is legal but non-disarming, so it
        # stays unhandled -> coherence FAIL. D6: v0.3 has no CP lifecycle, so the
        # deferral is terminal (cannot reach pass without the v0.4 CP lifecycle).
        feature_id, lane_id = _stage_coherence_inputs(
            repo_root,
            review_issues=[
                _issue(
                    id="agent-review-p1",
                    source="code_review",
                    severity="P1",
                    title="Needs a change proposal",
                )
            ],
        )
        apply_triage(
            repo_root,
            feature_id,
            "ISSUE-001",
            REQUEST_CHANGE_PROPOSAL,
            "spec-level conflict; defer to CP",
            "human",
            timestamp="2026-07-21T09:00:00Z",
        )
        collect_issue_bundle(repo_root, feature_id, lane_id)
        evaluate_coherence_gate(repo_root, feature_id)  # verdict=fail

        report = _generate(repo_root, feature_id)

        assert report["verdict"] == "fail"
        assert report["failure_class"] == "terminal"
        reasons = [r for r in report["blocking_reasons"] if r["issue_id"] == "ISSUE-001"]
        assert reasons
        assert any(r["kind"] == "pending_change_proposal" for r in reasons)
        assert all(r["resolution_path"] == "change_proposal" for r in reasons)
        assert all(r["class"] == "terminal" for r in reasons)

    def test_blocking_reasons_each_carry_issue_kind_resolution_path(
        self, repo_root: Path
    ) -> None:
        feature_id, _lane_id = _stage_coherence_inputs(
            repo_root,
            review_issues=[
                _issue(
                    id="agent-review-p1",
                    source="code_review",
                    severity="P1",
                    title="Unhandled",
                )
            ],
        )
        evaluate_coherence_gate(repo_root, feature_id)

        report = _generate(repo_root, feature_id)

        for reason in report["blocking_reasons"]:
            assert "issue_id" in reason
            assert "kind" in reason
            assert "resolution_path" in reason


class TestNullVerdictFailLoud:
    """ADR-0003 D7-c / §24.2: ``verdict == null`` (coherence has not run) is
    fail-loud refused - the report is downstream of the coherence verdict."""

    def test_null_verdict_raises(self, repo_root: Path) -> None:
        feature_id, _lane_id = _stage_coherence_inputs(repo_root)
        # Coherence NOT run -> verdict stays null.

        with pytest.raises(ValueError, match="verdict is null"):
            generate_final_report(repo_root, feature_id)

    def test_no_report_written_on_null_verdict(self, repo_root: Path) -> None:
        feature_id, _lane_id = _stage_coherence_inputs(repo_root)
        root = _feature_root(repo_root, feature_id)
        # The create-time placeholder final-report.json exists; assert the
        # generator did not overwrite it with a real report by checking it still
        # carries only the placeholder ``feature`` key.
        with pytest.raises(ValueError):
            generate_final_report(repo_root, feature_id)
        placeholder = json.loads((root / FINAL_REPORT_JSON).read_text())
        assert set(placeholder.keys()) == {"feature"}


class TestCodeToRequirementKnownGap:
    """D5 constraint 3: when no run contributes changed_files, the Q1 index is
    explicitly empty with a known_gap marker - never silently omitted."""

    def test_empty_changed_files_yields_known_gap(self, repo_root: Path) -> None:
        feature_id, _lane_id = _stage_coherence_inputs(repo_root)
        # Wipe changed_files from the only run's metadata -> Q1 index empties.
        root = _feature_root(repo_root, feature_id)
        md_path = root / "runs" / "RUN-001" / "output" / "metadata.json"
        md = json.loads(md_path.read_text())
        md["changed_files"] = []
        md_path.write_text(json.dumps(md))
        evaluate_coherence_gate(repo_root, feature_id)

        report = _generate(repo_root, feature_id)

        assert report["code_to_requirement"] == []
        assert any(
            "code_to_requirement" in g for g in report["meta"]["known_gaps"]
        )


class TestRecomputability:
    """D5: regenerating from the same artifacts yields byte-identical JSON (no
    wall-clock stamp; meta is stamped from the audit log artifact)."""

    def test_regenerate_is_byte_identical(self, repo_root: Path) -> None:
        feature_id, _lane_id = _stage_coherence_inputs(repo_root)
        evaluate_coherence_gate(repo_root, feature_id)

        generate_final_report(repo_root, feature_id)
        first = (_feature_root(repo_root, feature_id) / FINAL_REPORT_JSON).read_text()
        generate_final_report(repo_root, feature_id)
        second = (_feature_root(repo_root, feature_id) / FINAL_REPORT_JSON).read_text()

        assert first == second


class TestDefensiveGeneration:
    """D6: do not crash on missing *optional* artifacts; fail-loud on missing
    *required* ones."""

    def test_missing_optional_decisions_dir_is_fine(self, repo_root: Path) -> None:
        # No triage happened -> no decisions/ contents. The pass scenario already
        # has none; just assert generation succeeds and the report is well-formed.
        feature_id, _lane_id = _stage_coherence_inputs(repo_root)
        evaluate_coherence_gate(repo_root, feature_id)

        result = generate_final_report(repo_root, feature_id)

        assert isinstance(result, FinalReportResult)
        dec_files = list(
            (_feature_root(repo_root, feature_id) / "decisions").glob("DEC-*.json")
        )
        assert dec_files == []

    def test_missing_required_lane_decision_fails_loud(self, repo_root: Path) -> None:
        feature_id, lane_id = _stage_coherence_inputs(repo_root)
        evaluate_coherence_gate(repo_root, feature_id)
        # Corrupt: remove the required lane-decision.json.
        (_feature_root(repo_root, feature_id) / "lanes" / lane_id / "lane-decision.json").unlink()

        with pytest.raises(ValueError, match="lane-decision.json"):
            generate_final_report(repo_root, feature_id)

    def test_missing_required_coherence_decision_fails_loud(
        self, repo_root: Path
    ) -> None:
        feature_id, _lane_id = _stage_coherence_inputs(repo_root)
        evaluate_coherence_gate(repo_root, feature_id)
        # A non-null verdict implies coherence ran and wrote its decision record;
        # removing it is corruption.
        (_feature_root(repo_root, feature_id) / "coherence-decision.json").unlink()

        with pytest.raises(ValueError, match="coherence-decision.json"):
            generate_final_report(repo_root, feature_id)

    def test_missing_required_issue_bundle_fails_loud(self, repo_root: Path) -> None:
        # D6 names the issue bundle as a required artifact; its absence is a
        # generator error, not a silent empty report.
        feature_id, lane_id = _stage_coherence_inputs(repo_root)
        evaluate_coherence_gate(repo_root, feature_id)
        (_feature_root(repo_root, feature_id) / "lanes" / lane_id / "issue-bundle.json").unlink()

        with pytest.raises(ValueError, match="issue-bundle.json"):
            generate_final_report(repo_root, feature_id)

    def test_missing_feature_run_fails_loud(self, repo_root: Path) -> None:
        with pytest.raises(ValueError, match="not found"):
            generate_final_report(repo_root, "FEATURE-999")


class TestMarkdownSkeleton:
    """D5: the MD is a deterministic skeleton rendered from the JSON, with no
    narrative in v0.3 and spec/model content isolated."""

    def test_md_rendered_from_json_with_sections(self, repo_root: Path) -> None:
        feature_id, _lane_id = _stage_coherence_inputs(repo_root)
        evaluate_coherence_gate(repo_root, feature_id)

        generate_final_report(repo_root, feature_id)
        md = (_feature_root(repo_root, feature_id) / FINAL_REPORT_MD).read_text()

        assert "# Final Report - PASS" in md
        assert "verdict: **pass**" in md
        for heading in (
            "## Code -> Requirement (Q1)",
            "## Requirement Coverage (Q2)",
            "## Acceptance Verification (Q3)",
            "## Issue Dispositions (Q4)",
            "## Agent Timeline (Q5)",
            "## Failure Shape",
            "## Known Gaps",
        ):
            assert heading in md
        # The narrative isolation note is present (explains no narrative section).
        assert "no narrative section" in md

    def test_md_regenerate_is_byte_identical(self, repo_root: Path) -> None:
        feature_id, _lane_id = _stage_coherence_inputs(repo_root)
        evaluate_coherence_gate(repo_root, feature_id)

        generate_final_report(repo_root, feature_id)
        first = (_feature_root(repo_root, feature_id) / FINAL_REPORT_MD).read_text()
        generate_final_report(repo_root, feature_id)
        second = (_feature_root(repo_root, feature_id) / FINAL_REPORT_MD).read_text()

        assert first == second


class TestFinalReportCli:
    """CLI seam: ``ai-dev final-report <FEATURE>``."""

    def test_cli_pass_exits_zero(self, repo_root: Path, capsys: Any) -> None:
        feature_id, _lane_id = _stage_coherence_inputs(repo_root)
        evaluate_coherence_gate(repo_root, feature_id)

        rc = main(["final-report", feature_id, "--repo-root", str(repo_root)])

        assert rc == 0
        out = capsys.readouterr().out
        assert "FINAL-REPORT" in out
        assert f"feature={feature_id}" in out
        assert "verdict=pass" in out
        assert "failure_class=None" in out
        assert (_feature_root(repo_root, feature_id) / FINAL_REPORT_JSON).is_file()

    def test_cli_fail_exits_zero_report_still_written(
        self, repo_root: Path, capsys: Any
    ) -> None:
        # A FAIL report still generates (D6: report exists for both verdicts);
        # the CLI exits 0 because the *render* succeeded.
        feature_id, _lane_id = _stage_coherence_inputs(
            repo_root,
            review_issues=[
                _issue(
                    id="agent-review-p1",
                    source="code_review",
                    severity="P1",
                    title="Unhandled",
                )
            ],
        )
        evaluate_coherence_gate(repo_root, feature_id)

        rc = main(["final-report", feature_id, "--repo-root", str(repo_root)])

        assert rc == 0
        out = capsys.readouterr().out
        assert "verdict=fail" in out
        assert "failure_class=recoverable" in out

    def test_cli_null_verdict_exits_one_clean_error(
        self, repo_root: Path, capsys: Any
    ) -> None:
        feature_id, _lane_id = _stage_coherence_inputs(repo_root)
        # Coherence not run.

        rc = main(["final-report", feature_id, "--repo-root", str(repo_root)])

        assert rc == 1
        err = capsys.readouterr().err
        assert "error:" in err
        assert "verdict is null" in err
