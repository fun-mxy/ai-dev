"""status — canonical status writer (§8, tickets 01 + 04).

Ticket 01 wrote only the initial ``feature-status.yml``; ticket 04 extends the
deterministic writer with the freeze operation (§4.2), the ``current_gate``
advance, and the minimal ``lane-status.yml`` / ``task-status.yml`` seeds. These
are the only functions that mutate canonical status — models never call them
(§4.3 cardinal rule); the deterministic CLI / runtime does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ai_dev.audit import AUDIT_LOG_JSON, AUDIT_LOG_MD
from ai_dev.status import (
    FEATURE_STATUS_FILE,
    GATES,
    LANE_STATUS_FILE,
    TASK_STATUS_FILE,
    FrozenArtifactError,
    freeze_artifact,
    set_current_gate,
    write_initial_feature_status,
    write_initial_lane_status,
    write_initial_task_status,
)


def _load(status_dir: Path) -> dict:
    with (status_dir / FEATURE_STATUS_FILE).open() as f:
        return yaml.safe_load(f)


# --- helpers for the mutating operations (ticket 04) -----------------------
# freeze / set_current_gate take the feature-run root (they rewrite status and
# append to the run-level audit log), so tests stand up a feature root + seed
# its feature-status.yml first.


def _seed_feature(feature_root: Path, feature_id: str = "FEATURE-001") -> Path:
    """Lay down an initial feature-status.yml and return the status dir."""
    status_dir = feature_root / "status"
    write_initial_feature_status(status_dir, feature_id)
    return status_dir


def _feature_doc(feature_root: Path) -> dict:
    return yaml.safe_load((feature_root / "status" / FEATURE_STATUS_FILE).read_text())


def _audit_records(feature_root: Path) -> list[dict]:
    return json.loads((feature_root / AUDIT_LOG_JSON).read_text())


class TestWriteInitialFeatureStatus:
    def test_writes_initial_planning_state(self, tmp_path: Path) -> None:
        write_initial_feature_status(tmp_path, "FEATURE-001")

        doc = _load(tmp_path)
        assert doc["feature"]["id"] == "FEATURE-001"
        assert doc["feature"]["status"] == "planning"
        assert doc["feature"]["current_gate"] == "requirements_gate"
        assert doc["feature"]["verdict"] is None

    def test_all_four_frozen_flags_false(self, tmp_path: Path) -> None:
        write_initial_feature_status(tmp_path, "FEATURE-001")

        frozen = _load(tmp_path)["feature"]["frozen_artifacts"]
        assert set(frozen) == {"requirements", "design", "tasks", "lane_graph"}
        assert all(value is False for value in frozen.values())

    def test_id_matches_argument_not_hardcoded(self, tmp_path: Path) -> None:
        write_initial_feature_status(tmp_path, "FEATURE-007")
        assert _load(tmp_path)["feature"]["id"] == "FEATURE-007"

    def test_round_trips_as_plain_yaml(self, tmp_path: Path) -> None:
        # No anchors/aliases, no flow style surprises — a human and a fresh
        # loader both read the same plain mapping.
        write_initial_feature_status(tmp_path, "FEATURE-001")
        text = (tmp_path / "feature-status.yml").read_text()
        assert "status: planning" in text
        assert "current_gate: requirements_gate" in text
        # Spot-check one frozen flag renders as a literal false, not "False".
        assert "requirements: false" in text
        # The coherence-gate verdict field (renamed from final_verdict per
        # ADR-0003 D4) renders as a literal null at init, and the old name is
        # gone from the file entirely.
        assert "verdict: null" in text
        assert "final_verdict" not in text


class TestFreezeArtifact:
    """status.freeze_artifact — the §4.2 freeze operation (ticket 04).

    Flips a ``frozen_artifacts`` flag ``false → true`` deterministically and
    records it through the audit appender. Freeze is monotonic: re-freezing an
    already-frozen artifact is rejected, never silently re-applied.
    """

    def test_freeze_flips_flag_to_true(self, tmp_path: Path) -> None:
        _seed_feature(tmp_path)

        freeze_artifact(tmp_path, "requirements", timestamp="2026-07-19T10:00:00Z")

        assert _feature_doc(tmp_path)["feature"]["frozen_artifacts"]["requirements"] is True

    def test_freeze_persisted_as_literal_true_in_yaml(self, tmp_path: Path) -> None:
        _seed_feature(tmp_path)

        freeze_artifact(tmp_path, "requirements", timestamp="t")

        text = (tmp_path / "status" / FEATURE_STATUS_FILE).read_text()
        assert "requirements: true" in text

    @pytest.mark.parametrize("artifact", ["requirements", "design", "tasks", "lane_graph"])
    def test_each_of_the_four_artifacts_can_be_frozen(
        self, tmp_path: Path, artifact: str
    ) -> None:
        _seed_feature(tmp_path)

        freeze_artifact(tmp_path, artifact, timestamp="t")

        assert _feature_doc(tmp_path)["feature"]["frozen_artifacts"][artifact] is True

    def test_other_artifacts_stay_unfrozen(self, tmp_path: Path) -> None:
        _seed_feature(tmp_path)

        freeze_artifact(tmp_path, "requirements", timestamp="t")

        frozen = _feature_doc(tmp_path)["feature"]["frozen_artifacts"]
        assert frozen["requirements"] is True
        assert frozen["design"] is False
        assert frozen["tasks"] is False
        assert frozen["lane_graph"] is False

    def test_freeze_preserves_the_rest_of_the_document(self, tmp_path: Path) -> None:
        _seed_feature(tmp_path, "FEATURE-007")

        freeze_artifact(tmp_path, "design", timestamp="t")

        feature = _feature_doc(tmp_path)["feature"]
        assert feature["id"] == "FEATURE-007"
        assert feature["status"] == "planning"
        assert feature["current_gate"] == "requirements_gate"
        assert feature["verdict"] is None

    def test_freeze_appends_an_audit_record(self, tmp_path: Path) -> None:
        _seed_feature(tmp_path)

        freeze_artifact(tmp_path, "requirements", timestamp="2026-07-19T10:00:00Z")

        record = _audit_records(tmp_path)[-1]
        assert record["event"] == "freeze"
        assert record["timestamp"] == "2026-07-19T10:00:00Z"
        assert record["payload"] == {"artifact": "requirements", "frozen": True}

    def test_freeze_also_appears_in_audit_markdown(self, tmp_path: Path) -> None:
        _seed_feature(tmp_path)

        freeze_artifact(tmp_path, "requirements", timestamp="t")

        md = (tmp_path / AUDIT_LOG_MD).read_text()
        assert "freeze" in md
        assert "requirements" in md

    def test_refreezing_an_already_frozen_artifact_is_rejected(
        self, tmp_path: Path
    ) -> None:
        # §4.2: freeze is one-way. A second freeze of the same artifact is an
        # error, not an idempotent no-op — the frozen flag must never be cleared
        # or re-set by this writer (only a Change Proposal may touch the artifact).
        _seed_feature(tmp_path)
        freeze_artifact(tmp_path, "requirements", timestamp="t1")

        with pytest.raises(FrozenArtifactError):
            freeze_artifact(tmp_path, "requirements", timestamp="t2")

    def test_refreeze_rejection_is_a_value_error(self, tmp_path: Path) -> None:
        # FrozenArtifactError subclasses ValueError so callers may catch either.
        _seed_feature(tmp_path)
        freeze_artifact(tmp_path, "requirements", timestamp="t1")

        with pytest.raises(ValueError):
            freeze_artifact(tmp_path, "requirements", timestamp="t2")

    def test_refreeze_leaves_flag_and_audit_untouched(self, tmp_path: Path) -> None:
        # A rejected re-freeze must not append a second freeze record nor flip
        # anything — the writer fails loud and leaves state as it was.
        _seed_feature(tmp_path)
        freeze_artifact(tmp_path, "requirements", timestamp="t1")
        count_before = len(_audit_records(tmp_path))

        with pytest.raises(FrozenArtifactError):
            freeze_artifact(tmp_path, "requirements", timestamp="t2")

        assert len(_audit_records(tmp_path)) == count_before
        assert _feature_doc(tmp_path)["feature"]["frozen_artifacts"]["requirements"] is True

    def test_unknown_artifact_raises(self, tmp_path: Path) -> None:
        _seed_feature(tmp_path)

        with pytest.raises(ValueError):
            freeze_artifact(tmp_path, "bogus", timestamp="t")


class TestSetCurrentGate:
    """status.set_current_gate — advance current_gate to a known §18 gate.

    A low-level deterministic primitive: it records *that* the gate moved,
    validated against the §18 gate names. Gate sequencing is the orchestrator's
    concern, not the writer's.
    """

    def test_updates_current_gate(self, tmp_path: Path) -> None:
        _seed_feature(tmp_path)

        set_current_gate(tmp_path, "design_gate", timestamp="t")

        assert _feature_doc(tmp_path)["feature"]["current_gate"] == "design_gate"

    @pytest.mark.parametrize(
        "gate",
        [
            "requirements_gate",
            "design_gate",
            "task_gate",
            "lane_gate",
            "feature_coherence_gate",
        ],
    )
    def test_each_known_gate_is_accepted(self, tmp_path: Path, gate: str) -> None:
        _seed_feature(tmp_path)

        set_current_gate(tmp_path, gate, timestamp="t")

        assert _feature_doc(tmp_path)["feature"]["current_gate"] == gate

    def test_gate_constants_match_spec_section_18(self) -> None:
        # §18.1–§18.5 gate names, in pipeline order; initial gate is the head.
        assert GATES == (
            "requirements_gate",
            "design_gate",
            "task_gate",
            "lane_gate",
            "feature_coherence_gate",
        )
        assert GATES[0] == "requirements_gate"

    def test_unknown_gate_raises(self, tmp_path: Path) -> None:
        _seed_feature(tmp_path)

        with pytest.raises(ValueError):
            set_current_gate(tmp_path, "bogus_gate", timestamp="t")

    def test_preserves_frozen_flags_and_id(self, tmp_path: Path) -> None:
        _seed_feature(tmp_path, "FEATURE-042")
        freeze_artifact(tmp_path, "requirements", timestamp="t1")

        set_current_gate(tmp_path, "design_gate", timestamp="t2")

        feature = _feature_doc(tmp_path)["feature"]
        assert feature["id"] == "FEATURE-042"
        assert feature["frozen_artifacts"]["requirements"] is True

    def test_advance_is_audited(self, tmp_path: Path) -> None:
        _seed_feature(tmp_path)

        set_current_gate(tmp_path, "design_gate", timestamp="2026-07-19T11:00:00Z")

        record = _audit_records(tmp_path)[-1]
        assert record["event"] == "advance_gate"
        assert record["payload"] == {"current_gate": "design_gate"}
        assert record["timestamp"] == "2026-07-19T11:00:00Z"


class TestWriteInitialLaneStatus:
    """status.write_initial_lane_status — minimal §8.2 lane-status.yml.

    v0 is single-lane (§5.3), so the document holds exactly one lane in the
    schema-correct §8.2 shape, all run slots null at creation. The lane id is a
    parameter — it comes from the ticket-03 allocator at the call site, never a
    hardcoded string.
    """

    def test_writes_single_lane_with_section_8_2_schema(self, tmp_path: Path) -> None:
        write_initial_lane_status(tmp_path, "LANE-001")

        doc = yaml.safe_load((tmp_path / LANE_STATUS_FILE).read_text())
        assert list(doc) == ["lanes"]
        lane = doc["lanes"]["LANE-001"]
        assert lane == {
            "status": "pending",
            "current_phase": "not_started",
            "worktree": None,
            "implement_run": None,
            "review_run": None,
            "spec_gap_run": None,
            "verification_run": None,
            "gate_verdict": None,
        }

    def test_uses_provided_lane_id_not_hardcoded(self, tmp_path: Path) -> None:
        write_initial_lane_status(tmp_path, "LANE-007")

        doc = yaml.safe_load((tmp_path / LANE_STATUS_FILE).read_text())
        assert "LANE-007" in doc["lanes"]
        assert "LANE-001" not in doc["lanes"]

    def test_round_trips_as_plain_yaml(self, tmp_path: Path) -> None:
        write_initial_lane_status(tmp_path, "LANE-001")

        text = (tmp_path / LANE_STATUS_FILE).read_text()
        assert "status: pending" in text
        assert "current_phase: not_started" in text
        # Nulls render as literal null, not the Python string "None".
        assert "gate_verdict: null" in text


class TestWriteInitialTaskStatus:
    """status.write_initial_task_status — minimal §8.1 task-status.yml.

    No tasks exist at feature-run creation (the Planner elaborates them during
    the requirements phase, §9.1/§18.1), so the minimal schema-correct document
    is an empty ``tasks`` mapping. Task status is never derived from markdown
    checkboxes (§8.1) — rows are added later by this deterministic writer.
    """

    def test_writes_empty_tasks_mapping(self, tmp_path: Path) -> None:
        write_initial_task_status(tmp_path)

        doc = yaml.safe_load((tmp_path / TASK_STATUS_FILE).read_text())
        assert doc == {"tasks": {}}

    def test_round_trips_as_plain_yaml(self, tmp_path: Path) -> None:
        write_initial_task_status(tmp_path)

        text = (tmp_path / TASK_STATUS_FILE).read_text()
        assert "tasks:" in text
