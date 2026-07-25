"""Feature coherence gate evaluator (v0.3, ADR-0003 D1/D2/D4, ticket 08).

The terminal §18.5 gate. Deterministically checks the three D1 input conditions
and atomically writes ``current_gate = feature_coherence_gate`` + ``verdict``
+ derived ``feature.status``. These tests pin the three conditions, the atomic
verdict write, verdict mutability (re-coherence overwrites), and the CLI exit
contract (0=pass / 1=fail).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from ai_dev.audit import AUDIT_LOG_JSON
from ai_dev.cli import main
from ai_dev.coherence_gate import (
    COHERENCE_DECISION_JSON,
    COHERENCE_DECISION_MD,
    CoherenceResult,
    evaluate_coherence_gate,
)
from ai_dev.issue_bundle import collect_issue_bundle
from ai_dev.issue_status import STATUS_RESOLVED
from ai_dev.json_artifact import write_json
from ai_dev.lane_gate import evaluate_lane_gate
from ai_dev.paths import feature_dir
from ai_dev.triage import DECISIONS_DIR, REJECT, apply_triage

from test_checking_legs import _REVIEW_RUN_METADATA, _stage_implement_run  # noqa: E402
from test_implement_leg import _feature_root  # noqa: E402
from test_issue_bundle import _issue  # noqa: E402
from test_lane_gate import (  # noqa: E402
    _FAILING_RESULTS,
    _PASSING_RESULTS,
    _stage_lane_gate_inputs,
)

_TRIAGE_TS = "2026-07-21T09:00:00Z"
_COHERENCE_TS = "2026-07-21T10:00:00Z"


def _stage_coherence_inputs(
    repo_root: Path,
    *,
    review_issues: list[dict[str, Any]] | None = None,
    gap_issues: list[dict[str, Any]] | None = None,
    verification_results: list[Any] | None = None,
    run_lane_gate: bool = True,
) -> tuple[str, str]:
    """Stage a feature with implement/review/gap/verify/bundle artifacts, then
    (by default) run the lane gate so ``lane-decision.json`` exists for the
    coherence gate to read. Returns ``(feature_id, lane_id)``."""
    feature_id, lane_id = _stage_lane_gate_inputs(
        repo_root,
        review_issues=review_issues,
        gap_issues=gap_issues,
        verification_results=verification_results,
    )
    if run_lane_gate:
        evaluate_lane_gate(repo_root, feature_id, lane_id)
    return feature_id, lane_id


def _disarm_first_issue(
    repo_root: Path, feature_id: str, lane_id: str, *, action: str = REJECT
) -> str:
    """Triage the first collected issue (ISSUE-001) with a disarming action via
    the real ``apply_triage``, re-collect so the triage projects into the lane
    bundle, and re-run the lane gate (which now passes - the blocker is
    disarmed). Returns the minted DEC id."""
    apply_triage(
        repo_root,
        feature_id,
        "ISSUE-001",
        action,
        "human triage rationale",
        "human",
        timestamp=_TRIAGE_TS,
    )
    collect_issue_bundle(repo_root, feature_id, lane_id)
    evaluate_lane_gate(repo_root, feature_id, lane_id)
    return "DEC-001"


def _feature_status(repo_root: Path, feature_id: str) -> dict[str, Any]:
    path = _feature_root(repo_root, feature_id) / "status" / "feature-status.yml"
    return yaml.safe_load(path.read_text())["feature"]


def _audit_events(feature_root: Path) -> list[dict[str, Any]]:
    log = feature_root / AUDIT_LOG_JSON
    if not log.is_file():
        return []
    return json.loads(log.read_text())


class TestEvaluateCoherenceGate:
    """Library seam: evaluate §18.5 and write the terminal verdict."""

    def test_all_pass_writes_pass_verdict_and_terminal_state(self, repo_root: Path) -> None:
        feature_id, lane_id = _stage_coherence_inputs(repo_root)

        result = evaluate_coherence_gate(repo_root, feature_id)

        assert isinstance(result, CoherenceResult)
        assert result.verdict == "pass"
        assert result.passed is True
        assert result.failed_conditions == []
        # ADR-0003 D2/D4: the atomic terminal write - current_gate + verdict +
        # derived feature.status land together on feature-status.yml.
        feature = _feature_status(repo_root, feature_id)
        assert feature["current_gate"] == "feature_coherence_gate"
        assert feature["verdict"] == "pass"
        assert feature["status"] == "done"
        # The decision double product records the three D1 conditions.
        feature_root = _feature_root(repo_root, feature_id)
        decision = json.loads((feature_root / COHERENCE_DECISION_JSON).read_text())
        assert decision["verdict"] == "pass"
        assert [c["name"] for c in decision["conditions"]] == [
            "status_consistent",
            "lane_passed_and_p0_p1_handled",
            "decisions_recorded",
        ]
        assert all(c["passed"] for c in decision["conditions"])
        assert (feature_root / COHERENCE_DECISION_MD).is_file()
        assert "# Coherence Decision" in (feature_root / COHERENCE_DECISION_MD).read_text()

    def test_p1_unhandled_fails_verdict_blocked(self, repo_root: Path) -> None:
        # A P1 issue that was never triaged: the lane gate FAILs (P1 untriaged)
        # and the feature-level scan finds it unhandled -> coherence FAIL.
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

        result = evaluate_coherence_gate(repo_root, feature_id)

        assert result.verdict == "fail"
        assert result.passed is False
        assert "lane_passed_and_p0_p1_handled" in result.failed_conditions
        feature = _feature_status(repo_root, feature_id)
        assert feature["current_gate"] == "feature_coherence_gate"
        assert feature["verdict"] == "fail"
        assert feature["status"] == "blocked"

    def test_missing_dec_fails_verdict(self, repo_root: Path) -> None:
        # A disarmed P1 whose DEC file is missing: the lane gate PASSes (it
        # checks the DEC *id* in triage, not the file), but coherence condition
        # 3 catches the missing DEC-NNN file (ADR-0001 invariant #15).
        feature_id, lane_id = _stage_coherence_inputs(
            repo_root,
            review_issues=[
                _issue(
                    id="agent-review-p1",
                    source="code_review",
                    severity="P1",
                    title="Disarmed then DEC deleted",
                )
            ],
        )
        dec_id = _disarm_first_issue(repo_root, feature_id, lane_id)
        feature_root = _feature_root(repo_root, feature_id)
        # Lane gate passed (disarmed), so condition 2 is clean.
        lane_decision = json.loads(
            (feature_root / "lanes" / lane_id / "lane-decision.json").read_text()
        )
        assert lane_decision["decision"] == "pass"
        # Delete the DEC file the triage references.
        (feature_root / DECISIONS_DIR / f"{dec_id}.json").unlink()

        result = evaluate_coherence_gate(repo_root, feature_id)

        assert result.verdict == "fail"
        assert result.failed_conditions == ["decisions_recorded"]
        assert "lane_passed_and_p0_p1_handled" not in result.failed_conditions
        decision = json.loads((feature_root / COHERENCE_DECISION_JSON).read_text())
        assert "DEC-001 file missing" in decision["conditions"][2]["reason"]

    def test_disarmed_p1_with_dec_passes(self, repo_root: Path) -> None:
        # The happy disarmed path: a P1 rejected with a real DEC -> conditions
        # 2 (disarmed) and 3 (DEC file exists) both pass.
        feature_id, lane_id = _stage_coherence_inputs(
            repo_root,
            review_issues=[
                _issue(
                    id="agent-review-p1",
                    source="code_review",
                    severity="P1",
                    title="False positive",
                )
            ],
        )
        _disarm_first_issue(repo_root, feature_id, lane_id)

        result = evaluate_coherence_gate(repo_root, feature_id)

        assert result.verdict == "pass"
        assert result.failed_conditions == []

    def test_re_coherence_overwrites_fail_with_pass(self, repo_root: Path) -> None:
        # ADR-0003 D4: verdict is mutable. First coherence FAILs (unhandled P1);
        # after the human disarms it and the lane gate re-passes, a re-coherence
        # overwrites verdict fail -> pass (status blocked -> done).
        feature_id, lane_id = _stage_coherence_inputs(
            repo_root,
            review_issues=[
                _issue(
                    id="agent-review-p1",
                    source="code_review",
                    severity="P1",
                    title="Later disarmed",
                )
            ],
        )
        feature_root = _feature_root(repo_root, feature_id)

        first = evaluate_coherence_gate(repo_root, feature_id)
        assert first.verdict == "fail"
        assert _feature_status(repo_root, feature_id)["status"] == "blocked"

        # Human disarms the P1; lane gate re-runs to pass.
        _disarm_first_issue(repo_root, feature_id, lane_id)

        second = evaluate_coherence_gate(repo_root, feature_id)
        assert second.verdict == "pass"
        feature = _feature_status(repo_root, feature_id)
        assert feature["verdict"] == "pass"
        assert feature["status"] == "done"  # overwritten from blocked
        assert feature["current_gate"] == "feature_coherence_gate"

        # Two coherence_gate audit events: fail then pass (the verdict writes).
        events = [e for e in _audit_events(feature_root) if e["event"] == "coherence_gate"]
        assert [e["payload"]["verdict"] for e in events] == ["fail", "pass"]

    def test_resolved_p0_p1_is_handled(self, repo_root: Path) -> None:
        # A resolved P0/P1 (fixed / gone) is handled - condition 2 passes even
        # though the issue still lives in issues/. The lane gate passed on a
        # clean bundle; the issue SoT carries status=resolved.
        feature_id, lane_id = _stage_coherence_inputs(repo_root)
        feature_root = _feature_root(repo_root, feature_id)
        write_json(
            feature_root / "issues" / "ISSUE-001.json",
            _issue(
                id="ISSUE-001",
                source="code_review",
                severity="P1",
                title="Already resolved",
                status=STATUS_RESOLVED,
            ),
        )

        result = evaluate_coherence_gate(repo_root, feature_id)

        assert result.verdict == "pass"
        assert "lane_passed_and_p0_p1_handled" not in result.failed_conditions

    def test_p2_issue_does_not_fail_gate(self, repo_root: Path) -> None:
        # P2/P3 are never blocking; an untriaged P2 must not fail coherence.
        feature_id, lane_id = _stage_coherence_inputs(
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

        result = evaluate_coherence_gate(repo_root, feature_id)

        assert result.verdict == "pass"

    def test_verification_fail_lane_gate_fail_blocks_coherence(self, repo_root: Path) -> None:
        # If the lane gate FAILed (here: verification failed), coherence cannot
        # pass - condition 2 requires the lane gate to have PASSed.
        feature_id, lane_id = _stage_coherence_inputs(
            repo_root, verification_results=_FAILING_RESULTS
        )

        result = evaluate_coherence_gate(repo_root, feature_id)

        assert result.verdict == "fail"
        assert "lane_passed_and_p0_p1_handled" in result.failed_conditions

    def test_verdict_write_is_audited(self, repo_root: Path) -> None:
        feature_id, _lane_id = _stage_coherence_inputs(repo_root)
        feature_root = _feature_root(repo_root, feature_id)

        evaluate_coherence_gate(repo_root, feature_id)

        events = [e for e in _audit_events(feature_root) if e["event"] == "coherence_gate"]
        assert len(events) == 1
        payload = events[0]["payload"]
        assert payload["verdict"] == "pass"
        assert payload["current_gate"] == "feature_coherence_gate"
        assert payload["feature"] == feature_id
        assert payload["condition_count"] == 3
        assert payload["failed_conditions"] == []


class TestCoherenceGateFailLoud:
    """§24.2: missing/invalid prerequisites fail loud *before* any verdict is
    written. Corruption must not produce a misleading verdict."""

    def test_missing_lane_decision_fails_loud(self, repo_root: Path) -> None:
        feature_id, _lane_id = _stage_coherence_inputs(repo_root, run_lane_gate=False)

        with pytest.raises(ValueError, match="lane-decision.json"):
            evaluate_coherence_gate(repo_root, feature_id)

        # No verdict written: current_gate stays at lane_gate, verdict null.
        feature = _feature_status(repo_root, feature_id)
        assert feature["current_gate"] == "lane_gate"
        assert feature["verdict"] is None
        feature_root = _feature_root(repo_root, feature_id)
        assert not (feature_root / COHERENCE_DECISION_JSON).exists()

    def test_missing_decision_for_one_declared_lane_fails_loud(self, repo_root: Path) -> None:
        feature_id, lane_id = _stage_coherence_inputs(repo_root)
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
            evaluate_coherence_gate(repo_root, feature_id)

        assert _feature_status(repo_root, feature_id)["verdict"] is None

    def test_status_inconsistent_fails_loud(self, repo_root: Path) -> None:
        # Corrupt the status field so it no longer matches the derived
        # projection. Condition 1 is a corruption guard (§24.2), not a verdict
        # condition - a corrupt status must not flip a would-be pass to fail.
        feature_id, _lane_id = _stage_coherence_inputs(repo_root)
        status_path = _feature_root(repo_root, feature_id) / "status" / "feature-status.yml"
        doc = yaml.safe_load(status_path.read_text())
        doc["feature"]["status"] = "planning"  # derive(lane_gate, null)=implementing
        status_path.write_text(
            yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, allow_unicode=True)
        )

        with pytest.raises(ValueError, match="inconsistent"):
            evaluate_coherence_gate(repo_root, feature_id)

        # Nothing written.
        feature = _feature_status(repo_root, feature_id)
        assert feature["current_gate"] == "lane_gate"
        assert feature["verdict"] is None

    def test_unreachable_fcg_null_transient_fails_loud(self, repo_root: Path) -> None:
        # The (feature_coherence_gate, null) state is disk-never-exposed
        # (D3 note †). If it somehow appears, derive_feature_status raises and
        # coherence fail-louds rather than writing a verdict over it.
        feature_id, _lane_id = _stage_coherence_inputs(repo_root)
        status_path = _feature_root(repo_root, feature_id) / "status" / "feature-status.yml"
        doc = yaml.safe_load(status_path.read_text())
        doc["feature"]["current_gate"] = "feature_coherence_gate"
        # verdict stays null -> the unreachable transient.
        status_path.write_text(
            yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, allow_unicode=True)
        )

        with pytest.raises(ValueError, match="corrupt"):
            evaluate_coherence_gate(repo_root, feature_id)

    def test_coherence_from_earlier_gate_fails_loud(self, repo_root: Path) -> None:
        # A fresh feature run has current_gate=requirements_gate (nothing
        # frozen, no lane gate). Coherence must refuse - it would skip the
        # human gates and the lane gate.
        from ai_dev.feature_run import create_feature_run

        feature_id = create_feature_run(repo_root, "premature coherence")

        with pytest.raises(ValueError, match="coherence gate cannot run"):
            evaluate_coherence_gate(repo_root, feature_id)

    def test_missing_feature_run_fails_loud(self, repo_root: Path) -> None:
        with pytest.raises(ValueError, match="not found"):
            evaluate_coherence_gate(repo_root, "FEATURE-999")


class TestCoherenceGateCli:
    """CLI seam: ``ai-dev coherence-gate <FEATURE>``."""

    def test_cli_pass_exits_zero(self, repo_root: Path, capsys: Any) -> None:
        feature_id, _lane_id = _stage_coherence_inputs(repo_root)

        rc = main(["coherence-gate", feature_id, "--repo-root", str(repo_root)])

        assert rc == 0
        out = capsys.readouterr().out
        assert "COHERENCE-GATE PASS" in out
        assert f"feature={feature_id}" in out
        assert "verdict=pass" in out
        feature_root = _feature_root(repo_root, feature_id)
        assert (feature_root / COHERENCE_DECISION_JSON).is_file()

    def test_cli_fail_exits_one(self, repo_root: Path, capsys: Any) -> None:
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

        rc = main(["coherence-gate", feature_id, "--repo-root", str(repo_root)])

        assert rc == 1
        out = capsys.readouterr().out
        assert "COHERENCE-GATE FAIL" in out
        assert "verdict=fail" in out
        assert "lane_passed_and_p0_p1_handled" in out

    def test_cli_missing_lane_gate_exits_one_clean_error(
        self, repo_root: Path, capsys: Any
    ) -> None:
        feature_id, _lane_id = _stage_coherence_inputs(repo_root, run_lane_gate=False)

        rc = main(["coherence-gate", feature_id, "--repo-root", str(repo_root)])

        assert rc == 1
        err = capsys.readouterr().err
        assert "error:" in err
        assert "lane-decision.json" in err
        # Nothing canonical written.
        assert _feature_status(repo_root, feature_id)["verdict"] is None
