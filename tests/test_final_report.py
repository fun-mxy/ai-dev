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
import yaml

from ai_dev.cli import main
from ai_dev.coherence_gate import evaluate_coherence_gate
from ai_dev.final_report import (
    FINAL_REPORT_JSON,
    FINAL_REPORT_MD,
    FIVE_QUESTION_KEYS,
    FinalReportResult,
    generate_final_report,
)
from ai_dev.feature_ids import allocate_id
from ai_dev.implement_leg import IMPLEMENT_RESULT_JSON, write_implement_result
from ai_dev.checking_legs import write_review_report, write_spec_gap_report
from ai_dev.issue_bundle import ISSUE_BUNDLE_JSON, collect_issue_bundle
from ai_dev.json_artifact import write_json
from ai_dev.lane_gate import LANE_DECISION_JSON, evaluate_lane_gate
from ai_dev.lane_pr_projection import GITHUB_PROJECTION_DIR, LANE_PR_MAPPING_JSON
from ai_dev.lane_worktree import LANE_WORKTREE_FILE
from ai_dev.paths import lane_dir
from ai_dev.shell_verifier import CommandResult, write_verification_report
from ai_dev.status import sync_lane_statuses
from ai_dev.templates import LANE_GRAPH_YML, REQUIREMENTS_JSON
from ai_dev.triage import REQUEST_CHANGE_PROPOSAL, apply_triage
from ai_dev.validate import ValidationResult

from test_checking_legs import _REVIEW_RUN_METADATA  # noqa: E402
from test_coherence_gate import _stage_coherence_inputs  # noqa: E402
from test_implement_leg import _feature_root  # noqa: E402
from test_issue_bundle import _issue  # noqa: E402
from test_lane_gate import _FAILING_RESULTS, _PASSING_RESULTS  # noqa: E402


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

    def test_missing_decision_for_one_declared_lane_fails_loud(
        self, repo_root: Path
    ) -> None:
        feature_id, lane_id = _stage_coherence_inputs(repo_root)
        evaluate_coherence_gate(repo_root, feature_id)
        feature_root = _feature_root(repo_root, feature_id)
        status_path = feature_root / "status" / "lane-status.yml"
        graph_path = feature_root / "04-lane-graph.yml"
        graph = yaml.safe_load(graph_path.read_text())
        lane2 = dict(graph["lanes"][0])
        lane2["id"] = "LANE-002"
        graph["lanes"].append(lane2)
        graph_path.write_text(yaml.safe_dump(graph, sort_keys=False))
        status = yaml.safe_load(status_path.read_text())
        status["lanes"]["LANE-002"] = dict(status["lanes"][lane_id])
        status_path.write_text(yaml.safe_dump(status, sort_keys=False))

        with pytest.raises(ValueError, match="LANE-002"):
            generate_final_report(repo_root, feature_id)

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
        # ADR-0007: when the implementer declared the AC (verified=True with
        # evidence), the old "no AC->test traceability index" known-gap retires
        # - the section is genuinely populated from the self-attested declaration.
        assert not any(
            "acceptance_verification" in g for g in report["meta"]["known_gaps"]
        )

    def test_acceptance_gap_note_when_nothing_declared(self, repo_root: Path) -> None:
        # ADR-0007: when no run declared the AC (verified=False across the board)
        # the Q3 section is non-empty but uncovered -> the known-gap note stays.
        feature_id, lane_id = _stage_coherence_inputs(repo_root)
        _seed_requirements(repo_root, feature_id)
        # Rewrite the implement-result to drop the AC declaration so AC-001 reads
        # NOT verified, then regenerate.
        impl_path = (
            _feature_root(repo_root, feature_id)
            / "lanes"
            / lane_id
            / IMPLEMENT_RESULT_JSON
        )
        impl = json.loads(impl_path.read_text())
        impl["related_acceptance_criteria"] = []
        write_json(impl_path, impl)
        evaluate_coherence_gate(repo_root, feature_id)

        report = _generate(repo_root, feature_id)

        ver = report["acceptance_verification"]
        assert ver[0]["verified"] is False
        assert ver[0]["evidence_runs"] == []
        assert any(
            "acceptance_verification" in g for g in report["meta"]["known_gaps"]
        )

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


# ---------------------------------------------------------------------------
# v0.7 ticket 06 - multi-lane final-report aggregation (ADR-0009 D1/D6/D7).
#
# The final report must aggregate every lane's gate verdict, worktree metadata,
# branch/diff/commits, run/profile metadata, reviewer/spec-gap/verifier
# outcomes, unresolved P0/P1/P2/P3 issues, dependency state, and lane PR
# projection metadata - and label PRs as projections, not canonical state.
# ---------------------------------------------------------------------------


_LANE2_PURPOSE = "Lane two: extend the answer module with world()"


def _minimal_lane_entry(
    lane_id: str, *, purpose: str, depends_on: list[str] | None = None
) -> dict[str, Any]:
    """A §7.5-shaped lane-graph entry (mirrors ``_fill_artifacts``'s shape)."""
    return {
        "id": lane_id,
        "purpose": purpose,
        "tasks": [],
        "depends_on": depends_on if depends_on is not None else [],
        "expected_files": [],
        "exclusive_files": [],
        "provides": [],
        "consumes": [],
        "verification_scope": [],
        "merge_policy": {
            "auto_merge": False,
            "allowed_mechanical_resolutions": [],
            "semantic_conflict_policy": "human_triage",
        },
    }


def _append_lane_to_graph(
    repo_root: Path, feature_id: str, entry: dict[str, Any]
) -> list[str]:
    """Append ``entry`` to ``04-lane-graph.yml`` and sync lane-status to match.

    Returns the full declared lane-id list (in graph order) so callers can sync
    the runtime registry in the same edit. LANE-001's existing runtime row is
    preserved by ``sync_lane_statuses``; the new lane gets the initial row.
    """
    root = _feature_root(repo_root, feature_id)
    graph = yaml.safe_load((root / LANE_GRAPH_YML).read_text())
    graph["lanes"].append(entry)
    with (root / LANE_GRAPH_YML).open("w") as f:
        yaml.safe_dump(
            graph, f, sort_keys=False, default_flow_style=False, allow_unicode=True
        )
    lane_ids = [str(lane["id"]) for lane in graph["lanes"]]
    sync_lane_statuses(root / "status", lane_ids)
    return lane_ids


def _set_lane_depends_on(
    repo_root: Path, feature_id: str, lane_id: str, depends_on: list[str]
) -> None:
    """Rewrite one lane's ``depends_on`` in the lane graph (metadata-only edit).

    The lane gate does not check dependencies (that is the start precheck, ticket
    04); the final report reads the graph to surface per-lane dependency state.
    """
    root = _feature_root(repo_root, feature_id)
    graph = yaml.safe_load((root / LANE_GRAPH_YML).read_text())
    for lane in graph["lanes"]:
        if lane.get("id") == lane_id:
            lane["depends_on"] = list(depends_on)
    with (root / LANE_GRAPH_YML).open("w") as f:
        yaml.safe_dump(
            graph, f, sort_keys=False, default_flow_style=False, allow_unicode=True
        )


def _stage_lane_artifacts(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    *,
    run_id: str,
    verification_results: list[CommandResult] | None = None,
    review_issues: list[dict[str, Any]] | None = None,
    gap_issues: list[dict[str, Any]] | None = None,
    changed_files: list[str] | None = None,
) -> None:
    """Write a lane's full artifact set + run its lane gate.

    Stands up implement-result / review-report / spec-gap-report /
    verification-report / issue-bundle via the real writers (so the JSON shapes
    match production), then evaluates the lane gate so ``lane-decision.json``
    exists. ``verification_results`` defaults to passing; pass
    ``_FAILING_RESULTS`` to make the lane gate FAIL.
    """
    root = _feature_root(repo_root, feature_id)
    impl_metadata = {
        **_REVIEW_RUN_METADATA,
        "run_id": run_id,
        "changed_files": changed_files
        if changed_files is not None
        else [f"workspace/{lane_id.lower()}.py"],
        "started_at": "2026-07-26T01:00:00Z",
        "ended_at": "2026-07-26T01:00:05Z",
    }
    impl_result = {
        "status": "proposed_done",
        "summary": f"Lane {lane_id} implement summary",
        "tasks": [
            {"id": "TASK-002", "status": "proposed_done", "evidence": []}
        ],
        "related_requirements": [],
        "related_acceptance_criteria": [],
        "known_issues": [],
        "change_proposals": [],
    }
    write_implement_result(
        root,
        lane_id,
        run_id=run_id,
        result=impl_result,
        metadata=impl_metadata,
        validation=ValidationResult(run_id, []),
    )
    review_run = f"{run_id}-r"
    write_review_report(
        root,
        lane_id,
        run_id=review_run,
        result={"issues": review_issues or []},
        metadata={**_REVIEW_RUN_METADATA, "run_id": review_run},
        validation=ValidationResult(review_run, []),
    )
    gap_run = f"{run_id}-g"
    write_spec_gap_report(
        root,
        lane_id,
        run_id=gap_run,
        result={"issues": gap_issues or []},
        metadata={**_REVIEW_RUN_METADATA, "run_id": gap_run},
        validation=ValidationResult(gap_run, []),
    )
    write_verification_report(
        root,
        lane_id,
        implement_run_id=run_id,
        results=verification_results or _PASSING_RESULTS,
        started_at="2026-07-26T01:06:00Z",
        ended_at="2026-07-26T01:06:01Z",
    )
    collect_issue_bundle(repo_root, feature_id, lane_id)
    evaluate_lane_gate(repo_root, feature_id, lane_id)


def _write_lane_worktree(
    repo_root: Path, feature_id: str, lane_id: str, *, clean: bool = True
) -> str:
    """Write a ``worktree.json`` for the lane (mirrors the projection-test helper)."""
    branch = f"ai-dev/{feature_id}/{lane_id}"
    write_json(
        lane_dir(repo_root, feature_id, lane_id) / LANE_WORKTREE_FILE,
        {
            "lane_id": lane_id,
            "feature_id": feature_id,
            "branch": branch,
            "base_ref": "HEAD",
            "path": str(
                repo_root / ".ai-dev" / "worktrees" / feature_id / lane_id
            ),
            "created_at": "2026-07-26T00:30:00Z",
            "updated_at": "2026-07-26T00:30:00Z",
            "lifecycle": "active",
            "clean": clean,
        },
    )
    return branch


def _write_lane_commits_log(
    repo_root: Path, feature_id: str, lane_id: str, *, commits: list[str]
) -> None:
    """Write a ``commits.log`` for the lane (one subject line per commit)."""
    lane_dir(repo_root, feature_id, lane_id).mkdir(parents=True, exist_ok=True)
    (lane_dir(repo_root, feature_id, lane_id) / "commits.log").write_text(
        "\n".join(commits) + ("\n" if commits else "")
    )


def _write_lane_pr_mapping(
    repo_root: Path, feature_id: str, entries: dict[str, dict[str, Any]]
) -> Path:
    """Write ``projections/github/lane-prs.json`` with the given lane->entry map."""
    path = (
        _feature_root(repo_root, feature_id)
        / "projections"
        / GITHUB_PROJECTION_DIR
        / LANE_PR_MAPPING_JSON
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, {"feature": feature_id, "lanes": entries})
    return path


def _stage_two_lane_feature(
    repo_root: Path,
    *,
    lane2_fail: bool = False,
    lane2_review_issues: list[dict[str, Any]] | None = None,
    lane1_depends_on_lane2: bool = False,
) -> tuple[str, str, str]:
    """Stage a two-lane feature and run coherence; return (feature, lane1, lane2).

    LANE-001 is staged passing via ``_stage_coherence_inputs`` (lane gate passed,
    coherence NOT yet run). LANE-002 is then added with full artifacts and its
    lane gate evaluated (passing by default, failing when ``lane2_fail``). When
    ``lane1_depends_on_lane2`` is set, LANE-001's graph entry is rewritten to
    ``depends_on: [LANE-002]`` before coherence runs. Coherence is run once at
    the end so the final report has a real verdict to consume.
    """
    feature_id, lane1 = _stage_coherence_inputs(repo_root)
    root = _feature_root(repo_root, feature_id)
    lane2 = allocate_id(root, "LANE")
    assert lane2 == "LANE-002", f"expected LANE-002, got {lane2}"
    _append_lane_to_graph(
        repo_root, feature_id, _minimal_lane_entry(lane2, purpose=_LANE2_PURPOSE)
    )
    _stage_lane_artifacts(
        repo_root,
        feature_id,
        lane2,
        run_id="RUN-004",
        verification_results=_FAILING_RESULTS if lane2_fail else None,
        review_issues=lane2_review_issues,
    )
    if lane1_depends_on_lane2:
        _set_lane_depends_on(repo_root, feature_id, lane1, [lane2])
    evaluate_coherence_gate(repo_root, feature_id)
    return feature_id, lane1, lane2


def _lane_in_report(report: dict[str, Any], lane_id: str) -> dict[str, Any]:
    return next(l for l in report["lanes"] if l["lane_id"] == lane_id)


class TestMultiLaneAggregation:
    """ADR-0009 D1/D6/D7: the final report aggregates every declared lane and
    labels lane PRs as projections, not canonical/merged state."""

    def test_two_passing_lanes_listed_with_gate_verdicts(self, repo_root: Path) -> None:
        feature_id, lane1, lane2 = _stage_two_lane_feature(repo_root)

        report = _generate(repo_root, feature_id)

        assert [l["lane_id"] for l in report["lanes"]] == [lane1, lane2]
        for lane in report["lanes"]:
            assert lane["gate"]["verdict"] == "pass"
            assert lane["gate"]["failed_conditions"] == []
        # meta carries the lane count + the v0.7 integration disclaimer.
        assert report["meta"]["lane_count"] == 2
        disclaimer = report["meta"]["integration_disclaimer"]
        assert "projection" in disclaimer.lower()
        assert "not" in disclaimer.lower() and "merge" in disclaimer.lower()

    def test_per_lane_purpose_run_metadata_and_changed_files(self, repo_root: Path) -> None:
        feature_id, _lane1, lane2 = _stage_two_lane_feature(repo_root)

        report = _generate(repo_root, feature_id)
        lane2_entry = _lane_in_report(report, lane2)

        assert lane2_entry["purpose"] == _LANE2_PURPOSE
        # Run/profile metadata is projected from the implement-result rollup.
        assert lane2_entry["run"]["run_id"] == "RUN-004"
        assert lane2_entry["run"]["profile"] == "cc-glm52"
        assert lane2_entry["run"]["cli"] == "claude"
        assert lane2_entry["run"]["exit_code"] == 0
        assert lane2_entry["changed_files"] == [f"workspace/{lane2.lower()}.py"]
        # Reviewer / spec-gap / verifier outcomes per lane.
        assert lane2_entry["review"]["run_id"] == "RUN-004-r"
        assert lane2_entry["review"]["issue_count"] == 0
        assert lane2_entry["spec_gap"]["run_id"] == "RUN-004-g"
        assert lane2_entry["spec_gap"]["issue_count"] == 0
        assert lane2_entry["verification"]["verdict"] == "pass"
        assert lane2_entry["verification"]["passed_count"] == 1
        assert lane2_entry["verification"]["command_count"] == 1

    def test_worktree_and_commit_metadata_per_lane(self, repo_root: Path) -> None:
        feature_id, _lane1, lane2 = _stage_two_lane_feature(repo_root)
        branch = _write_lane_worktree(repo_root, feature_id, lane2)
        _write_lane_commits_log(
            repo_root, feature_id, lane2, commits=["feat: add world()", "docs: readme"]
        )

        report = _generate(repo_root, feature_id)
        lane2_entry = _lane_in_report(report, lane2)

        assert lane2_entry["worktree"] is not None
        assert lane2_entry["worktree"]["branch"] == branch
        assert lane2_entry["worktree"]["base_ref"] == "HEAD"
        assert lane2_entry["worktree"]["lifecycle"] == "active"
        assert lane2_entry["worktree"]["clean"] is True
        # commits are projected as a list (one {sha, subject} per commit),
        # mirroring changed_files - not just a scalar count.
        assert lane2_entry["commits"] is not None
        assert len(lane2_entry["commits"]) == 2
        assert lane2_entry["commits"][0]["subject"] == "feat: add world()"
        assert lane2_entry["commits"][1]["subject"] == "docs: readme"
        # LANE-001 has no worktree.json / commits.log -> null worktree / commits.
        assert _lane_in_report(report, "LANE-001")["worktree"] is None
        assert _lane_in_report(report, "LANE-001")["commits"] is None

    def test_one_lane_fail_blocks_feature_verdict(self, repo_root: Path) -> None:
        feature_id, _lane1, lane2 = _stage_two_lane_feature(repo_root, lane2_fail=True)

        report = _generate(repo_root, feature_id)

        assert report["verdict"] == "fail"
        assert report["failure_class"] == "recoverable"
        # The failing lane's gate verdict is surfaced per-lane.
        assert _lane_in_report(report, lane2)["gate"]["verdict"] == "fail"
        assert "verification_passed" in _lane_in_report(report, lane2)["gate"][
            "failed_conditions"
        ]
        # A non-pass lane gate produces a recoverable blocking reason, but the
        # report does NOT claim the branches are merged or integrated.
        reasons = report["blocking_reasons"]
        assert any(r["kind"] == "lane_gate_not_passed" for r in reasons)
        assert not any("merged" in str(r).lower() for r in reasons)

    def test_dependency_blocked_lane_shown(self, repo_root: Path) -> None:
        # LANE-001 depends_on LANE-002; LANE-002's gate FAILed, so LANE-001's
        # dependency state is blocked even though LANE-001 itself passed.
        feature_id, lane1, lane2 = _stage_two_lane_feature(
            repo_root, lane2_fail=True, lane1_depends_on_lane2=True
        )

        report = _generate(repo_root, feature_id)
        lane1_entry = _lane_in_report(report, lane1)

        assert lane1_entry["dependency_state"]["depends_on"] == [lane2]
        assert lane1_entry["dependency_state"]["satisfied"] is False
        assert lane1_entry["dependency_state"]["blocked_by"] == [lane2]
        # LANE-002 has no deps -> satisfied vacuously.
        assert _lane_in_report(report, lane2)["dependency_state"]["satisfied"] is True
        assert _lane_in_report(report, lane2)["dependency_state"]["blocked_by"] == []
        # The feature verdict is fail (LANE-002 non-pass) but nothing claims the
        # branches are merged or semantically integrated.
        assert report["verdict"] == "fail"
        assert report["meta"]["integration_disclaimer"]

    def test_unresolved_issues_by_severity_per_lane(self, repo_root: Path) -> None:
        # LANE-002 raises a P2 (non-blocking) review issue; its lane gate still
        # passes. The per-lane issue summary must count it as unresolved (P2).
        p2 = _issue(
            id="agent-review-p2",
            source="code_review",
            severity="P2",
            title="Missing docstring on world()",
        )
        feature_id, _lane1, lane2 = _stage_two_lane_feature(
            repo_root, lane2_review_issues=[p2]
        )

        report = _generate(repo_root, feature_id)
        issues = _lane_in_report(report, lane2)["issues"]

        assert issues["total"] == 1
        assert issues["by_severity"]["P2"] == 1
        assert issues["unresolved_by_severity"]["P2"] == 1
        assert issues["by_severity"]["P0"] == 0
        assert issues["unresolved_ids"]
        # LANE-001 raised no issues.
        assert _lane_in_report(report, "LANE-001")["issues"]["total"] == 0

    def test_lane_pr_projection_present_labeled_as_projection(
        self, repo_root: Path
    ) -> None:
        feature_id, lane1, lane2 = _stage_two_lane_feature(repo_root)
        _write_lane_pr_mapping(
            repo_root,
            feature_id,
            {
                lane1: {
                    "pr_number": 7,
                    "pr_url": "https://github.com/owner/repo/pull/7",
                    "head_branch": f"ai-dev/{feature_id}/{lane1}",
                    "base_branch": "main",
                    "remote": "origin",
                    "projected_at": "2026-07-26T02:00:00Z",
                }
            },
        )

        report = _generate(repo_root, feature_id)
        proj = _lane_in_report(report, lane1)["pr_projection"]

        assert proj["projected"] is True
        assert proj["pr_number"] == 7
        assert proj["pr_url"] == "https://github.com/owner/repo/pull/7"
        assert proj["base_branch"] == "main"
        assert proj["projected_at"] == "2026-07-26T02:00:00Z"
        # LANE-002 was not projected.
        assert _lane_in_report(report, lane2)["pr_projection"]["projected"] is False
        # The disclaimer reiterates PRs are projections, not canonical state.
        assert "projection" in report["meta"]["integration_disclaimer"].lower()

    def test_lane_pr_projection_missing_defaults_to_not_projected(
        self, repo_root: Path
    ) -> None:
        feature_id, lane1, _lane2 = _stage_two_lane_feature(repo_root)
        # No lane-prs.json written.

        report = _generate(repo_root, feature_id)

        for lane in report["lanes"]:
            assert lane["pr_projection"]["projected"] is False
            assert lane["pr_projection"]["pr_number"] is None
            assert lane["pr_projection"]["pr_url"] is None

    def test_md_renders_lanes_section_and_disclaimer(self, repo_root: Path) -> None:
        feature_id, _lane1, lane2 = _stage_two_lane_feature(repo_root)
        _write_lane_worktree(repo_root, feature_id, lane2)

        generate_final_report(repo_root, feature_id)
        md = (_feature_root(repo_root, feature_id) / FINAL_REPORT_MD).read_text()

        assert "## Lanes" in md
        assert "## Multi-lane & Projection Note" in md
        # Each lane id is rendered, with its gate verdict.
        assert "LANE-001" in md
        assert "LANE-002" in md
        # The disclaimer text is present verbatim.
        assert "projection" in md.lower()
        assert "does NOT" in md and "merge" in md.lower()

    def test_md_regenerate_with_lanes_is_byte_identical(self, repo_root: Path) -> None:
        feature_id, _lane1, lane2 = _stage_two_lane_feature(repo_root)
        _write_lane_worktree(repo_root, feature_id, lane2)

        generate_final_report(repo_root, feature_id)
        first = (_feature_root(repo_root, feature_id) / FINAL_REPORT_MD).read_text()
        generate_final_report(repo_root, feature_id)
        second = (_feature_root(repo_root, feature_id) / FINAL_REPORT_MD).read_text()

        assert first == second

