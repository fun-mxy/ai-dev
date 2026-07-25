"""Lane dependency validation and precondition checks (v0.7 ticket 04).

Tests cover:
* ``validate_lane_graph`` — structural validation (cycles, unknowns, self-deps)
* ``check_dependency_precondition`` — runtime gate-verdict precheck
* ``aggregate_lane_gate_states`` — feature-level read-only aggregation
* Lane gate verdict writeback to lane-status
* Implementer leg dependency precheck integration
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from ai_dev.cli import main
from ai_dev.feature_run import create_feature_run
from ai_dev.lane_dependency import (
    DependencyPrecheckResult,
    LaneGateState,
    aggregate_lane_gate_states,
    check_dependency_precondition,
    validate_lane_graph,
)
from ai_dev.lane_gate import LANE_DECISION_JSON
from ai_dev.paths import lane_dir
from ai_dev.templates import LANE_GRAPH_YML

# Reuse test helpers from the lane-gate tests for staging lane gate evidence.
from test_lane_gate import _stage_lane_gate_inputs  # noqa: E402

from test_implement_leg import _feature_root  # noqa: E402


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _write_lane_graph(feature_root: Path, feature_id: str, lanes: list[dict[str, Any]]) -> Path:
    """Write ``04-lane-graph.yml`` with the given lane entries."""
    feature_root.mkdir(parents=True, exist_ok=True)
    path = feature_root / LANE_GRAPH_YML
    graph = {"feature": feature_id, "frozen": False, "lanes": lanes}
    path.write_text(yaml.safe_dump(graph, sort_keys=False))
    return path


def _write_lane_decisions(
    repo_root: Path, feature_id: str, decisions: dict[str, str]
) -> None:
    """Write a ``lane-decision.json`` per lane id→verdict mapping."""
    for lid, verdict in decisions.items():
        ld = lane_dir(repo_root, feature_id, lid)
        ld.mkdir(parents=True, exist_ok=True)
        decision_doc = {
            "feature": feature_id,
            "lane": lid,
            "decision": verdict,
            "conditions": [
                {"name": "proposed_done", "passed": verdict == "pass", "reason": "test"},
                {"name": "verification_passed", "passed": verdict == "pass", "reason": "test"},
                {"name": "review_no_blocking_issues", "passed": True, "reason": "test"},
                {"name": "spec_gap_no_blocking_issues", "passed": True, "reason": "test"},
                {"name": "issue_bundle_generated", "passed": True, "reason": "test"},
            ],
            "blocking_issue_count": 0,
            "blocking_issues": [],
        }
        (ld / LANE_DECISION_JSON).write_text(
            json.dumps(decision_doc, indent=2) + "\n"
        )


def _seed_two_lane_feature(repo_root: Path) -> tuple[str, str, str]:
    """Create a feature run with two lanes: LANE-001 depends on LANE-002.

    The default single-lane graph from ``create_feature_run`` is overwritten
    with a two-lane graph carrying a dependency: LANE-001 → LANE-002.
    Lane-status is synced for both lanes.
    """
    feature_id = create_feature_run(repo_root, "two-lane dependency test")
    root = repo_root / ".ai-dev" / "features" / feature_id
    lane1 = "LANE-001"
    lane2 = "LANE-002"
    # Allocate the second lane id so it's a real allocated id.
    from ai_dev.feature_ids import allocate_id
    allocate_id(root, "LANE")
    _write_lane_graph(
        root,
        feature_id,
        [
            {
                "id": lane1,
                "purpose": "Dependent lane",
                "tasks": [],
                "depends_on": [lane2],
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
                "verification_commands": [],
            },
            {
                "id": lane2,
                "purpose": "Upstream dependency lane",
                "tasks": [],
                "depends_on": [],
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
                "verification_commands": [],
            },
        ],
    )
    # Sync lane-status for both lanes.
    from ai_dev.status import write_initial_lane_statuses
    write_initial_lane_statuses(root / "status", [lane1, lane2])
    return feature_id, lane1, lane2


def _seed_no_dep_feature(repo_root: Path) -> tuple[str, str]:
    """Create a feature run with a single lane and no dependencies."""
    feature_id = create_feature_run(repo_root, "no-dependency lane test")
    root = repo_root / ".ai-dev" / "features" / feature_id
    lane_id = "LANE-001"
    return feature_id, lane_id


# ---------------------------------------------------------------------------
# validate_lane_graph (public seam — structural validation)
# ---------------------------------------------------------------------------


def _minimal_lane(*, lid: str, deps: list[str] | None = None) -> dict[str, Any]:
    """Build a minimal lane dict for ``_write_lane_graph``."""
    return {
        "id": lid,
        "purpose": None,
        "tasks": [],
        "depends_on": deps if deps is not None else [],
        "expected_files": [],
        "exclusive_files": [],
        "provides": [],
        "consumes": [],
        "verification_scope": [],
        "verification_commands": [],
        "merge_policy": {
            "auto_merge": False,
            "allowed_mechanical_resolutions": [],
            "semantic_conflict_policy": "human_triage",
        },
    }


class TestValidateLaneGraph:
    """Structural validation exercised through the public ``validate_lane_graph``
    seam (writes a YAML file, then validates it)."""

    def test_empty_depends_on_passes(self, repo_root: Path) -> None:
        root = repo_root / ".ai-dev" / "features" / "FEATURE-001"
        _write_lane_graph(root, "FEATURE-001", [
            _minimal_lane(lid="LANE-001"),
            _minimal_lane(lid="LANE-002"),
        ])
        result = validate_lane_graph(root)
        assert result == ["LANE-001", "LANE-002"]

    def test_valid_chain_passes(self, repo_root: Path) -> None:
        root = repo_root / ".ai-dev" / "features" / "FEATURE-001"
        _write_lane_graph(root, "FEATURE-001", [
            _minimal_lane(lid="LANE-001", deps=["LANE-002"]),
            _minimal_lane(lid="LANE-002"),
        ])
        result = validate_lane_graph(root)
        assert result == ["LANE-001", "LANE-002"]

    def test_self_dependency_fails(self, repo_root: Path) -> None:
        root = repo_root / ".ai-dev" / "features" / "FEATURE-001"
        _write_lane_graph(root, "FEATURE-001", [
            _minimal_lane(lid="LANE-001", deps=["LANE-001"]),
        ])
        with pytest.raises(ValueError, match="cannot depend on itself"):
            validate_lane_graph(root)

    def test_direct_cycle_fails(self, repo_root: Path) -> None:
        root = repo_root / ".ai-dev" / "features" / "FEATURE-001"
        _write_lane_graph(root, "FEATURE-001", [
            _minimal_lane(lid="LANE-001", deps=["LANE-002"]),
            _minimal_lane(lid="LANE-002", deps=["LANE-001"]),
        ])
        with pytest.raises(ValueError, match="cycle"):
            validate_lane_graph(root)

    def test_indirect_cycle_fails(self, repo_root: Path) -> None:
        root = repo_root / ".ai-dev" / "features" / "FEATURE-001"
        _write_lane_graph(root, "FEATURE-001", [
            _minimal_lane(lid="LANE-001", deps=["LANE-002"]),
            _minimal_lane(lid="LANE-002", deps=["LANE-003"]),
            _minimal_lane(lid="LANE-003", deps=["LANE-001"]),
        ])
        with pytest.raises(ValueError, match="cycle"):
            validate_lane_graph(root)

    def test_unknown_dependency_fails(self, repo_root: Path) -> None:
        root = repo_root / ".ai-dev" / "features" / "FEATURE-001"
        _write_lane_graph(root, "FEATURE-001", [
            _minimal_lane(lid="LANE-001", deps=["LANE-099"]),
            _minimal_lane(lid="LANE-002"),
        ])
        with pytest.raises(ValueError, match="LANE-099"):
            validate_lane_graph(root)

    def test_duplicate_lane_id_fails(self, repo_root: Path) -> None:
        root = repo_root / ".ai-dev" / "features" / "FEATURE-001"
        _write_lane_graph(root, "FEATURE-001", [
            _minimal_lane(lid="LANE-001"),
            _minimal_lane(lid="LANE-001"),
        ])
        with pytest.raises(ValueError, match="duplicate"):
            validate_lane_graph(root)

    def test_missing_id_fails(self, repo_root: Path) -> None:
        root = repo_root / ".ai-dev" / "features" / "FEATURE-001"
        path = _write_lane_graph(root, "FEATURE-001", [
            _minimal_lane(lid="LANE-001"),
            {"purpose": "no id here"},
        ])
        with pytest.raises(ValueError, match="has no string id"):
            validate_lane_graph(root)

    def test_valid_two_lane_feature_passes(self, repo_root: Path) -> None:
        feature_id, lane1, lane2 = _seed_two_lane_feature(repo_root)
        root = _feature_root(repo_root, feature_id)
        result = validate_lane_graph(root)
        assert lane1 in result
        assert lane2 in result


# ---------------------------------------------------------------------------
# check_dependency_precondition
# ---------------------------------------------------------------------------


class TestCheckDependencyPrecondition:
    """Runtime gate-verdict precheck (ADR-0009 D4)."""

    def test_no_dependencies_passes(self, repo_root: Path) -> None:
        feature_id, lane_id = _seed_no_dep_feature(repo_root)
        result = check_dependency_precondition(repo_root, feature_id, lane_id)
        assert isinstance(result, DependencyPrecheckResult)
        assert result.passed is True
        assert result.blocked_by == []
        assert result.lane_id == lane_id

    def test_satisfied_dependency_passes(self, repo_root: Path) -> None:
        feature_id, lane1, lane2 = _seed_two_lane_feature(repo_root)
        # Write a pass decision for the upstream lane (LANE-002).
        _write_lane_decisions(repo_root, feature_id, {lane2: "pass"})
        result = check_dependency_precondition(repo_root, feature_id, lane1)
        assert result.passed is True
        assert result.blocked_by == []

    def test_unsatisfied_dependency_blocks(self, repo_root: Path) -> None:
        feature_id, lane1, lane2 = _seed_two_lane_feature(repo_root)
        # Write a fail decision for the upstream lane.
        _write_lane_decisions(repo_root, feature_id, {lane2: "fail"})
        result = check_dependency_precondition(repo_root, feature_id, lane1)
        assert result.passed is False
        assert lane2 in result.blocked_by
        assert "fail" in result.details[lane2]

    def test_missing_decision_blocks(self, repo_root: Path) -> None:
        feature_id, lane1, lane2 = _seed_two_lane_feature(repo_root)
        # No lane-decision.json for the upstream lane at all.
        result = check_dependency_precondition(repo_root, feature_id, lane1)
        assert result.passed is False
        assert lane2 in result.blocked_by
        assert "not yet executed" in result.details[lane2]

    def test_mixed_dependencies(self, repo_root: Path) -> None:
        """Three-lane feature: LANE-001 depends on both LANE-002 (pass) and LANE-003 (fail)."""
        feature_id = create_feature_run(repo_root, "mixed-dep test")
        root = _feature_root(repo_root, feature_id)
        lane1, lane2, lane3 = "LANE-001", "LANE-002", "LANE-003"
        from ai_dev.feature_ids import allocate_id
        allocate_id(root, "LANE")  # LANE-002
        allocate_id(root, "LANE")  # LANE-003
        _write_lane_graph(
            root,
            feature_id,
            [
                {"id": lane1, "depends_on": [lane2, lane3], "expected_files": [],
                 "exclusive_files": [], "provides": [], "consumes": [],
                 "verification_scope": [], "verification_commands": [], "tasks": [],
                 "purpose": None,
                 "merge_policy": {"auto_merge": False, "allowed_mechanical_resolutions": [],
                                  "semantic_conflict_policy": "human_triage"}},
                {"id": lane2, "depends_on": [], "expected_files": [], "exclusive_files": [],
                 "provides": [], "consumes": [], "verification_scope": [],
                 "verification_commands": [], "tasks": [], "purpose": None,
                 "merge_policy": {"auto_merge": False, "allowed_mechanical_resolutions": [],
                                  "semantic_conflict_policy": "human_triage"}},
                {"id": lane3, "depends_on": [], "expected_files": [], "exclusive_files": [],
                 "provides": [], "consumes": [], "verification_scope": [],
                 "verification_commands": [], "tasks": [], "purpose": None,
                 "merge_policy": {"auto_merge": False, "allowed_mechanical_resolutions": [],
                                  "semantic_conflict_policy": "human_triage"}},
            ],
        )
        from ai_dev.status import write_initial_lane_statuses
        write_initial_lane_statuses(root / "status", [lane1, lane2, lane3])
        _write_lane_decisions(repo_root, feature_id, {lane2: "pass", lane3: "fail"})

        result = check_dependency_precondition(repo_root, feature_id, lane1)
        assert result.passed is False
        assert lane3 in result.blocked_by
        assert lane2 not in result.blocked_by
        assert "fail" in result.details[lane3]
        assert "passed" in result.details[lane2]

    def test_blocked_by_reason_includes_lane_id(self, repo_root: Path) -> None:
        """Each blocker's reason must be keyed by the dep lane id."""
        feature_id, lane1, lane2 = _seed_two_lane_feature(repo_root)
        result = check_dependency_precondition(repo_root, feature_id, lane1)
        assert lane2 in result.details
        assert lane2 in result.blocked_by

    def test_unknown_lane_in_graph_rejected(self, repo_root: Path) -> None:
        """check_dependency_precondition calls validate_lane_graph first, so
        a corrupt graph with an unknown dep is caught before the precheck."""
        feature_id, lane1, _ = _seed_two_lane_feature(repo_root)
        root = _feature_root(repo_root, feature_id)
        # Corrupt the graph: LANE-001 depends on LANE-999.
        _write_lane_graph(
            root,
            feature_id,
            [
                {"id": lane1, "depends_on": ["LANE-999"], "expected_files": [],
                 "exclusive_files": [], "provides": [], "consumes": [],
                 "verification_scope": [], "verification_commands": [], "tasks": [],
                 "purpose": None,
                 "merge_policy": {"auto_merge": False, "allowed_mechanical_resolutions": [],
                                  "semantic_conflict_policy": "human_triage"}},
                {"id": "LANE-002", "depends_on": [], "expected_files": [],
                 "exclusive_files": [], "provides": [], "consumes": [],
                 "verification_scope": [], "verification_commands": [], "tasks": [],
                 "purpose": None,
                 "merge_policy": {"auto_merge": False, "allowed_mechanical_resolutions": [],
                                  "semantic_conflict_policy": "human_triage"}},
            ],
        )
        from ai_dev.status import write_initial_lane_statuses
        write_initial_lane_statuses(root / "status", [lane1, "LANE-002"])
        with pytest.raises(ValueError, match="LANE-999"):
            check_dependency_precondition(repo_root, feature_id, lane1)


# ---------------------------------------------------------------------------
# aggregate_lane_gate_states
# ---------------------------------------------------------------------------


class TestAggregateLaneGateStates:
    """Feature-level read-only lane gate state aggregation."""

    def test_aggregates_all_lanes(self, repo_root: Path) -> None:
        feature_id, lane1, lane2 = _seed_two_lane_feature(repo_root)
        states = aggregate_lane_gate_states(repo_root, feature_id)
        assert isinstance(states, dict)
        assert lane1 in states
        assert lane2 in states
        assert isinstance(states[lane1], LaneGateState)
        assert isinstance(states[lane2], LaneGateState)

    def test_no_decision_yields_not_passed(self, repo_root: Path) -> None:
        feature_id, lane1, lane2 = _seed_two_lane_feature(repo_root)
        states = aggregate_lane_gate_states(repo_root, feature_id)
        assert states[lane1].passed is False
        assert states[lane1].decision_path is None
        assert states[lane2].passed is False

    def test_passing_lane_yields_passed(self, repo_root: Path) -> None:
        feature_id, lane1, lane2 = _seed_two_lane_feature(repo_root)
        _write_lane_decisions(repo_root, feature_id, {lane1: "pass", lane2: "pass"})
        states = aggregate_lane_gate_states(repo_root, feature_id)
        assert states[lane1].passed is True
        assert states[lane2].passed is True

    def test_does_not_compute_feature_verdict(self, repo_root: Path) -> None:
        """Aggregation is purely per-lane; it does NOT produce a feature-level
        verdict. A mixed-passing set (one pass, one fail) is returned as-is
        with no warning or synthesis."""
        feature_id, lane1, lane2 = _seed_two_lane_feature(repo_root)
        _write_lane_decisions(repo_root, feature_id, {lane1: "pass", lane2: "fail"})
        states = aggregate_lane_gate_states(repo_root, feature_id)
        assert states[lane1].passed is True
        assert states[lane2].passed is False
        # The returned dict is flat — no feature-level key.
        assert "__feature_verdict__" not in states

    def test_failed_conditions_are_extracted(self, repo_root: Path) -> None:
        feature_id, lane1, _ = _seed_two_lane_feature(repo_root)
        _write_lane_decisions(repo_root, feature_id, {lane1: "fail"})
        states = aggregate_lane_gate_states(repo_root, feature_id)
        assert "proposed_done" in states[lane1].failed_conditions
        assert "verification_passed" in states[lane1].failed_conditions


# ---------------------------------------------------------------------------
# Lane gate verdict writeback to lane-status
# ---------------------------------------------------------------------------


class TestLaneGateUpdatesVerdict:
    """After ``evaluate_lane_gate``, the lane's ``gate_verdict`` is recorded in
    ``lane-status.yml`` so downstream lanes' prechecks can read it."""

    def test_gate_verdict_pass_written_to_lane_status(self, repo_root: Path) -> None:
        feature_id, lane_id = _stage_lane_gate_inputs(repo_root)
        # _stage_lane_gate_inputs only stages the inputs; we must call the
        # lane gate evaluator to write the verdict.
        from ai_dev.lane_gate import evaluate_lane_gate
        evaluate_lane_gate(repo_root, feature_id, lane_id)
        status_path = (
            _feature_root(repo_root, feature_id) / "status" / "lane-status.yml"
        )
        doc = yaml.safe_load(status_path.read_text())
        assert doc["lanes"][lane_id]["gate_verdict"] == "pass"

    def test_gate_verdict_fail_written_to_lane_status(self, repo_root: Path) -> None:
        # Use the _FAILING_RESULTS from the lane gate test to trigger a fail.
        from test_lane_gate import _FAILING_RESULTS
        from ai_dev.checking_legs import write_review_report, write_spec_gap_report
        from ai_dev.shell_verifier import write_verification_report
        from ai_dev.issue_bundle import collect_issue_bundle
        from ai_dev.validate import ValidationResult
        from ai_dev.lane_gate import evaluate_lane_gate
        from test_checking_legs import _REVIEW_RUN_METADATA, _stage_implement_run

        feature_id, lane_id, _ = _stage_implement_run(repo_root)
        root = _feature_root(repo_root, feature_id)
        write_review_report(
            root, lane_id, run_id="RUN-002", result={"issues": []},
            metadata=_REVIEW_RUN_METADATA,
            validation=ValidationResult("RUN-002", []),
        )
        write_spec_gap_report(
            root, lane_id, run_id="RUN-003", result={"issues": []},
            metadata={**_REVIEW_RUN_METADATA, "run_id": "RUN-003"},
            validation=ValidationResult("RUN-003", []),
        )
        write_verification_report(
            root, lane_id, implement_run_id="RUN-001",
            results=_FAILING_RESULTS,
            started_at="2026-07-20T12:00:00Z", ended_at="2026-07-20T12:00:01Z",
        )
        collect_issue_bundle(repo_root, feature_id, lane_id)
        evaluate_lane_gate(repo_root, feature_id, lane_id)

        status_path = root / "status" / "lane-status.yml"
        doc = yaml.safe_load(status_path.read_text())
        assert doc["lanes"][lane_id]["gate_verdict"] == "fail"


# ---------------------------------------------------------------------------
# Implementer leg dependency precheck integration
# ---------------------------------------------------------------------------


class TestImplementerLegDependencyPrecheck:
    """``run_implementer_leg`` rejects a lane whose dependencies are not
    gate-passed."""

    def test_no_dependency_lane_runs(self, repo_root: Path) -> None:
        """A lane with empty depends_on must not be blocked by the precheck."""
        feature_id, lane_id = _stage_lane_gate_inputs(repo_root)
        # The frozen tasks + lane-graph precondition is already satisfied
        # by _stage_lane_gate_inputs. The lane has no depends_on, so the
        # precheck should pass without error. We test this by calling
        # check_dependency_precondition directly (run_implementer_leg would
        # also start a real agent; we are testing the precheck seam).
        result = check_dependency_precondition(repo_root, feature_id, lane_id)
        assert result.passed is True

    def test_blocked_lane_raises_in_precheck(self, repo_root: Path) -> None:
        """A lane with a failing dependency must produce a blocking precheck."""
        feature_id, lane1, lane2 = _seed_two_lane_feature(repo_root)
        # Freeze tasks + lane-graph so the frozen precondition is met.
        from ai_dev.status import freeze_artifact
        root = _feature_root(repo_root, feature_id)
        # Write a task body so the implementer leg can read it.
        (root / "03-tasks.md").write_text(
            "# Tasks - test\n\nFrozen: true\n\n## Tasks\n\n- [ ] test\n"
        )
        freeze_artifact(root, "tasks")
        freeze_artifact(root, "lane_graph")
        # No lane-decision for lane2 → precheck is blocked.
        result = check_dependency_precondition(repo_root, feature_id, lane1)
        assert result.passed is False
        assert lane2 in result.blocked_by

    def test_satisfied_dependency_lane_passes_precheck(self, repo_root: Path) -> None:
        """A lane with a pass-gate dependency must pass the precheck."""
        feature_id, lane1, lane2 = _seed_two_lane_feature(repo_root)
        root = _feature_root(repo_root, feature_id)
        (root / "03-tasks.md").write_text(
            "# Tasks - test\n\nFrozen: true\n\n## Tasks\n\n- [ ] test\n"
        )
        from ai_dev.status import freeze_artifact
        freeze_artifact(root, "tasks")
        freeze_artifact(root, "lane_graph")
        _write_lane_decisions(repo_root, feature_id, {lane2: "pass"})
        result = check_dependency_precondition(repo_root, feature_id, lane1)
        assert result.passed is True


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


class TestLaneGateCliVerdict:
    """CLI lane-gate command writes the verdict to lane-status."""

    def test_lane_gate_cli_writes_verdict(self, repo_root: Path, capsys: Any) -> None:
        import sys
        feature_id, lane_id = _stage_lane_gate_inputs(repo_root)
        rc = main(["lane-gate", feature_id, lane_id, "--repo-root", str(repo_root)])
        assert rc == 0
        status_path = _feature_root(repo_root, feature_id) / "status" / "lane-status.yml"
        doc = yaml.safe_load(status_path.read_text())
        assert doc["lanes"][lane_id]["gate_verdict"] == "pass"
