"""Lane gate evaluator (v0.2 ticket 05, spec §18.4).

The lane gate is the deterministic final judge for one implement -> review/gap
-> verify -> issue-bundle loop. It consumes only lane artifacts, applies the
§18.4 / §15.2 blocking rules, and writes the lane-decision double product.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from ai_dev.checking_legs import write_review_report, write_spec_gap_report
from ai_dev.cli import main
from ai_dev.issue_bundle import ISSUE_BUNDLE_JSON, collect_issue_bundle
from ai_dev.issue_status import ISSUE_STATUSES
from ai_dev.json_artifact import write_json
from ai_dev.lane_gate import (
    LANE_DECISION_JSON,
    LANE_DECISION_MD,
    evaluate_lane_gate,
)
from ai_dev.paths import lane_dir
from ai_dev.shell_verifier import CommandResult, write_verification_report
from ai_dev.validate import ValidationResult

from test_checking_legs import (  # noqa: E402
    _REVIEW_RUN_METADATA,
    _stage_implement_run,
)
from test_issue_bundle import _issue  # noqa: E402
from test_implement_leg import _feature_root  # noqa: E402

_TRIAGE_TS = "2026-07-20T12:30:00Z"

_PASSING_RESULTS = [
    CommandResult(
        name="pytest",
        command="pytest tests/",
        exit_code=0,
        stdout="ok",
        stderr="",
    )
]
_FAILING_RESULTS = [
    CommandResult(
        name="pytest",
        command="pytest tests/",
        exit_code=1,
        stdout="",
        stderr="failed",
    )
]


def _stage_lane_gate_inputs(
    repo_root: Path,
    *,
    review_issues: list[dict[str, Any]] | None = None,
    gap_issues: list[dict[str, Any]] | None = None,
    verification_results: list[CommandResult] | None = None,
    collect_issues: bool = True,
) -> tuple[str, str]:
    feature_id, lane_id, _ = _stage_implement_run(repo_root)
    feature_root = _feature_root(repo_root, feature_id)
    write_review_report(
        feature_root,
        lane_id,
        run_id="RUN-002",
        result={"issues": review_issues or []},
        metadata=_REVIEW_RUN_METADATA,
        validation=ValidationResult("RUN-002", []),
    )
    write_spec_gap_report(
        feature_root,
        lane_id,
        run_id="RUN-003",
        result={"issues": gap_issues or []},
        metadata={**_REVIEW_RUN_METADATA, "run_id": "RUN-003"},
        validation=ValidationResult("RUN-003", []),
    )
    write_verification_report(
        feature_root,
        lane_id,
        implement_run_id="RUN-001",
        results=verification_results or _PASSING_RESULTS,
        started_at="2026-07-20T12:00:00Z",
        ended_at="2026-07-20T12:00:01Z",
    )
    if collect_issues:
        collect_issue_bundle(repo_root, feature_id, lane_id)
    return feature_id, lane_id


def _set_first_bundle_issue_triage(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    triage: dict[str, Any] | None,
) -> None:
    """Stamp triage onto the first projected bundle issue and its feature SoT."""
    bundle_path = lane_dir(repo_root, feature_id, lane_id) / ISSUE_BUNDLE_JSON
    bundle = json.loads(bundle_path.read_text())
    issue_id = bundle["issues"][0]["id"]
    bundle["issues"][0]["triage"] = triage
    write_json(bundle_path, bundle)

    issue_path = _feature_root(repo_root, feature_id) / "issues" / f"{issue_id}.json"
    issue = json.loads(issue_path.read_text())
    issue["triage"] = triage
    write_json(issue_path, issue)


def _disarming_triage(action: str, *decision_ids: str) -> dict[str, Any]:
    return {
        "action": action,
        "reason": "human triage rationale",
        "by": "human",
        "ts": _TRIAGE_TS,
        "decision_ids": list(decision_ids),
    }


class TestEvaluateLaneGate:
    """Library seam: evaluate §18.4 and write lane-decision artifacts."""

    def test_all_conditions_pass_writes_pass_decision(self, repo_root: Path) -> None:
        feature_id, lane_id = _stage_lane_gate_inputs(repo_root)

        result = evaluate_lane_gate(repo_root, feature_id, lane_id)

        assert result.decision == "pass"
        assert result.passed is True
        assert result.condition_count == 5
        assert result.failed_conditions == []
        lane_root = lane_dir(repo_root, feature_id, lane_id)
        decision = json.loads((lane_root / LANE_DECISION_JSON).read_text())
        assert decision["decision"] == "pass"
        assert [c["name"] for c in decision["conditions"]] == [
            "proposed_done",
            "verification_passed",
            "review_no_blocking_issues",
            "spec_gap_no_blocking_issues",
            "issue_bundle_generated",
        ]
        assert all(c["passed"] for c in decision["conditions"])
        assert decision["blocking_issue_count"] == 0
        assert (lane_root / LANE_DECISION_MD).is_file()
        assert "# Lane Decision" in (lane_root / LANE_DECISION_MD).read_text()

    def test_p0_review_issue_fails_gate(self, repo_root: Path) -> None:
        feature_id, lane_id = _stage_lane_gate_inputs(
            repo_root,
            review_issues=[
                _issue(
                    id="agent-review-p0",
                    source="code_review",
                    severity="P0",
                    title="Review blocker",
                )
            ],
        )

        result = evaluate_lane_gate(repo_root, feature_id, lane_id)

        assert result.decision == "fail"
        assert result.failed_conditions == ["review_no_blocking_issues"]
        decision = json.loads(
            (lane_dir(repo_root, feature_id, lane_id) / LANE_DECISION_JSON).read_text()
        )
        assert decision["blocking_issue_count"] == 1
        assert decision["blocking_issues"][0]["severity"] == "P0"
        assert decision["conditions"][2]["passed"] is False
        assert "P0 is untriaged" in decision["conditions"][2]["reason"]

    def test_p1_spec_gap_issue_fails_by_default(self, repo_root: Path) -> None:
        feature_id, lane_id = _stage_lane_gate_inputs(
            repo_root,
            gap_issues=[
                _issue(
                    id="agent-gap-p1",
                    source="spec_gap",
                    severity="P1",
                    title="Spec gap blocker",
                )
            ],
        )

        result = evaluate_lane_gate(repo_root, feature_id, lane_id)

        assert result.decision == "fail"
        assert result.failed_conditions == ["spec_gap_no_blocking_issues"]
        decision = json.loads(
            (lane_dir(repo_root, feature_id, lane_id) / LANE_DECISION_JSON).read_text()
        )
        assert decision["blocking_issues"][0]["severity"] == "P1"
        assert decision["conditions"][3]["passed"] is False
        assert "untriaged" in decision["conditions"][3]["reason"]

    def test_p0_reject_with_decision_disarms_gate(self, repo_root: Path) -> None:
        feature_id, lane_id = _stage_lane_gate_inputs(
            repo_root,
            review_issues=[
                _issue(
                    id="agent-review-p0",
                    source="code_review",
                    severity="P0",
                    title="Rejected false positive",
                )
            ],
        )
        _set_first_bundle_issue_triage(
            repo_root,
            feature_id,
            lane_id,
            _disarming_triage("reject", "DEC-001"),
        )

        result = evaluate_lane_gate(repo_root, feature_id, lane_id)

        assert result.decision == "pass"
        assert result.blocking_issues == []
        decision = json.loads(
            (lane_dir(repo_root, feature_id, lane_id) / LANE_DECISION_JSON).read_text()
        )
        assert decision["blocking_issue_count"] == 0
        assert decision["conditions"][2]["passed"] is True

    def test_p0_reject_without_reason_still_fails_gate(self, repo_root: Path) -> None:
        feature_id, lane_id = _stage_lane_gate_inputs(
            repo_root,
            review_issues=[
                _issue(
                    id="agent-review-p0",
                    source="code_review",
                    severity="P0",
                    title="Decision without rationale",
                )
            ],
        )
        _set_first_bundle_issue_triage(
            repo_root,
            feature_id,
            lane_id,
            {
                "action": "reject",
                "reason": "",
                "by": "human",
                "ts": _TRIAGE_TS,
                "decision_ids": ["DEC-001"],
            },
        )

        result = evaluate_lane_gate(repo_root, feature_id, lane_id)

        assert result.decision == "fail"
        assert result.failed_conditions == ["review_no_blocking_issues"]
        assert "missing required triage reason" in result.blocking_issues[0]["blocking_reason"]

    @pytest.mark.parametrize("action", ["override", "reject"])
    def test_p1_override_or_reject_with_decision_disarms_gate(
        self, repo_root: Path, action: str
    ) -> None:
        feature_id, lane_id = _stage_lane_gate_inputs(
            repo_root,
            gap_issues=[
                _issue(
                    id="agent-gap-p1",
                    source="spec_gap",
                    severity="P1",
                    title="Human-disarmed P1",
                )
            ],
        )
        _set_first_bundle_issue_triage(
            repo_root,
            feature_id,
            lane_id,
            _disarming_triage(action, "DEC-001"),
        )

        result = evaluate_lane_gate(repo_root, feature_id, lane_id)

        assert result.decision == "pass"
        assert result.blocking_issues == []
        decision = json.loads(
            (lane_dir(repo_root, feature_id, lane_id) / LANE_DECISION_JSON).read_text()
        )
        assert decision["conditions"][3]["passed"] is True

    def test_p0_override_still_fails_as_second_defense(self, repo_root: Path) -> None:
        feature_id, lane_id = _stage_lane_gate_inputs(
            repo_root,
            review_issues=[
                _issue(
                    id="agent-review-p0",
                    source="code_review",
                    severity="P0",
                    title="Illegal override leaked through",
                )
            ],
        )
        _set_first_bundle_issue_triage(
            repo_root,
            feature_id,
            lane_id,
            _disarming_triage("override", "DEC-001"),
        )

        result = evaluate_lane_gate(repo_root, feature_id, lane_id)

        assert result.decision == "fail"
        assert result.failed_conditions == ["review_no_blocking_issues"]
        assert result.blocking_issues[0]["triage_action"] == "override"
        decision = json.loads(
            (lane_dir(repo_root, feature_id, lane_id) / LANE_DECISION_JSON).read_text()
        )
        assert "P0 override" in decision["conditions"][2]["reason"]

    @pytest.mark.parametrize("action", ["request_fix", "request_change_proposal", "request_cp"])
    def test_p1_request_fix_or_change_proposal_blocks_gate(
        self, repo_root: Path, action: str
    ) -> None:
        feature_id, lane_id = _stage_lane_gate_inputs(
            repo_root,
            review_issues=[
                _issue(
                    id="agent-review-p1",
                    source="code_review",
                    severity="P1",
                    title="Needs follow-up",
                )
            ],
        )
        _set_first_bundle_issue_triage(
            repo_root,
            feature_id,
            lane_id,
            {
                "action": action,
                "reason": "needs follow-up",
                "by": "human",
                "ts": _TRIAGE_TS,
            },
        )

        result = evaluate_lane_gate(repo_root, feature_id, lane_id)

        assert result.decision == "fail"
        assert result.failed_conditions == ["review_no_blocking_issues"]
        assert result.blocking_issues[0]["resolution_path"] == action
        decision = json.loads(
            (lane_dir(repo_root, feature_id, lane_id) / LANE_DECISION_JSON).read_text()
        )
        assert action in decision["conditions"][2]["reason"]

    def test_verification_fail_verdict_fails_gate(self, repo_root: Path) -> None:
        feature_id, lane_id = _stage_lane_gate_inputs(
            repo_root, verification_results=_FAILING_RESULTS
        )

        result = evaluate_lane_gate(repo_root, feature_id, lane_id)

        assert result.decision == "fail"
        assert result.failed_conditions == ["verification_passed"]
        decision = json.loads(
            (lane_dir(repo_root, feature_id, lane_id) / LANE_DECISION_JSON).read_text()
        )
        assert decision["conditions"][1] == {
            "name": "verification_passed",
            "passed": False,
            "reason": "verification verdict is fail (0/1 commands passed)",
        }

    def test_lane_gate_does_not_mutate_current_gate(self, repo_root: Path) -> None:
        feature_id, lane_id = _stage_lane_gate_inputs(repo_root)
        status_path = _feature_root(repo_root, feature_id) / "status" / "feature-status.yml"
        before = yaml.safe_load(status_path.read_text())

        result = evaluate_lane_gate(repo_root, feature_id, lane_id)

        after = yaml.safe_load(status_path.read_text())
        assert result.decision == "pass"
        assert after["feature"]["current_gate"] == before["feature"]["current_gate"]
        assert after == before

    def test_missing_prerequisite_artifact_fails_loud(self, repo_root: Path) -> None:
        feature_id, lane_id = _stage_lane_gate_inputs(repo_root, collect_issues=False)

        try:
            evaluate_lane_gate(repo_root, feature_id, lane_id)
        except ValueError as exc:
            message = str(exc)
        else:  # pragma: no cover - assertion branch
            raise AssertionError("missing issue-bundle should fail loud")

        assert "required lane gate artifact missing or invalid" in message
        assert "issue-bundle.json" in message
        assert not (lane_dir(repo_root, feature_id, lane_id) / LANE_DECISION_JSON).exists()

    def test_corrupt_issue_bundle_fails_loud(self, repo_root: Path) -> None:
        # §24.2: a valid-JSON bundle missing its `issues` list must fail loud,
        # not silently yield zero issues and a wrong PASS.
        feature_id, lane_id = _stage_lane_gate_inputs(repo_root)
        bundle_path = lane_dir(repo_root, feature_id, lane_id) / ISSUE_BUNDLE_JSON
        bundle_path.write_text(json.dumps({"feature": feature_id, "lane": lane_id}))

        try:
            evaluate_lane_gate(repo_root, feature_id, lane_id)
        except ValueError as exc:
            message = str(exc)
        else:  # pragma: no cover - assertion branch
            raise AssertionError("corrupt issue-bundle should fail loud")

        assert "structurally invalid" in message
        assert "issues" in message
        assert not (lane_dir(repo_root, feature_id, lane_id) / LANE_DECISION_JSON).exists()


class TestLaneGateIgnoresIssueStatus:
    """ADR-0002 D2 regression: the gate reads ``severity`` (+ ``triage`` in
    ticket 06), never ``status``. ``status`` is collector/driver bookkeeping;
    flipping it across all four lifecycle values must not move the decision.
    """

    @staticmethod
    def _set_first_issue_status(
        repo_root: Path, feature_id: str, lane_id: str, status: str
    ) -> None:
        """Stamp ``status`` onto the bundle's first issue in place."""
        bundle_path = lane_dir(repo_root, feature_id, lane_id) / ISSUE_BUNDLE_JSON
        bundle = json.loads(bundle_path.read_text())
        bundle["issues"][0]["status"] = status
        bundle_path.write_text(json.dumps(bundle))

    @pytest.mark.parametrize("status", ISSUE_STATUSES)
    def test_p2_passes_regardless_of_status(self, repo_root: Path, status: str) -> None:
        feature_id, lane_id = _stage_lane_gate_inputs(
            repo_root,
            review_issues=[
                _issue(
                    id="agent-review-p2",
                    source="code_review",
                    severity="P2",
                    title="Non-blocking nit",
                )
            ],
        )
        self._set_first_issue_status(repo_root, feature_id, lane_id, status)

        result = evaluate_lane_gate(repo_root, feature_id, lane_id)

        assert result.decision == "pass"
        assert result.blocking_issues == []

    @pytest.mark.parametrize("status", ISSUE_STATUSES)
    def test_p1_untriaged_fails_regardless_of_status(
        self, repo_root: Path, status: str
    ) -> None:
        feature_id, lane_id = _stage_lane_gate_inputs(
            repo_root,
            review_issues=[
                _issue(
                    id="agent-review-p1",
                    source="code_review",
                    severity="P1",
                    title="Blocking issue",
                )
            ],
        )
        self._set_first_issue_status(repo_root, feature_id, lane_id, status)

        result = evaluate_lane_gate(repo_root, feature_id, lane_id)

        assert result.decision == "fail"
        assert result.failed_conditions == ["review_no_blocking_issues"]
        # The blocking summary carries severity and triage, not lifecycle status.
        assert result.blocking_issues[0]["severity"] == "P1"
        assert result.blocking_issues[0]["triage_action"] is None
        assert "status" not in result.blocking_issues[0]

    @pytest.mark.parametrize("status", ISSUE_STATUSES)
    def test_p1_triaged_reject_passes_regardless_of_status(
        self, repo_root: Path, status: str
    ) -> None:
        feature_id, lane_id = _stage_lane_gate_inputs(
            repo_root,
            review_issues=[
                _issue(
                    id="agent-review-p1",
                    source="code_review",
                    severity="P1",
                    title="Disarmed issue",
                )
            ],
        )
        _set_first_bundle_issue_triage(
            repo_root,
            feature_id,
            lane_id,
            _disarming_triage("reject", "DEC-001"),
        )
        self._set_first_issue_status(repo_root, feature_id, lane_id, status)

        result = evaluate_lane_gate(repo_root, feature_id, lane_id)

        assert result.decision == "pass"
        assert result.blocking_issues == []


class TestLaneGateCli:
    """CLI seam: ``ai-dev lane-gate <FEATURE> <LANE>``."""

    def test_lane_gate_command_exits_zero_on_pass(self, repo_root: Path, capsys: Any) -> None:
        feature_id, lane_id = _stage_lane_gate_inputs(repo_root)

        rc = main(["lane-gate", feature_id, lane_id, "--repo-root", str(repo_root)])

        assert rc == 0
        out = capsys.readouterr().out
        assert "LANE-GATE PASS" in out
        assert f"lane={lane_id}" in out
        assert "conditions=5/5" in out
        assert (lane_dir(repo_root, feature_id, lane_id) / LANE_DECISION_JSON).is_file()

    def test_lane_gate_command_exits_one_on_fail(self, repo_root: Path, capsys: Any) -> None:
        feature_id, lane_id = _stage_lane_gate_inputs(
            repo_root, verification_results=_FAILING_RESULTS
        )

        rc = main(["lane-gate", feature_id, lane_id, "--repo-root", str(repo_root)])

        assert rc == 1
        out = capsys.readouterr().out
        assert "LANE-GATE FAIL" in out
        assert "failed_conditions=verification_passed" in out

    def test_lane_gate_command_missing_artifact_exits_one(
        self, repo_root: Path, capsys: Any
    ) -> None:
        feature_id, lane_id = _stage_lane_gate_inputs(repo_root, collect_issues=False)

        rc = main(["lane-gate", feature_id, lane_id, "--repo-root", str(repo_root)])

        assert rc == 1
        err = capsys.readouterr().err
        assert "error:" in err
        assert "issue-bundle.json" in err
