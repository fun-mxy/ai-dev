"""status — canonical status writer (§8, tickets 01 + 04).

Ticket 01 wrote only the initial ``feature-status.yml``; ticket 04 extends the
deterministic writer with the freeze operation (§4.2), the ``current_gate``
advance, and the minimal ``lane-status.yml`` / ``task-status.yml`` seeds. These
are the only functions that mutate canonical status — models never call them
(§4.3 cardinal rule); the deterministic CLI / runtime does.

ADR-0003 (v0.3) wires the gate state machine into ticket 04's primitives:
``freeze_artifact`` atomically advances ``current_gate`` to the next stage on a
human-gate freeze (requirements/design/tasks), and ``feature.status`` becomes a
derived projection of ``(current_gate, verdict)`` - never an independent field,
recomputed inside every mutation. These tests pin both the derivation table
(ADR-0003 D3) and the freeze-driven advance (ADR-0003 D2).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ai_dev.audit import AUDIT_LOG_JSON, AUDIT_LOG_MD
from ai_dev.status import (
    FEATURE_STATUS_FILE,
    FROZEN_ARTIFACTS,
    GATES,
    LANE_STATUS_FILE,
    TASK_STATUS_FILE,
    FrozenArtifactError,
    agent_profiles,
    derive_feature_status,
    freeze_artifact,
    record_agent_profile,
    record_coherence_verdict,
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

    def test_initial_fix_loop_budget_starts_unused(self, tmp_path: Path) -> None:
        write_initial_feature_status(tmp_path, "FEATURE-001")

        assert _load(tmp_path)["feature"]["fix_loop_budget"] == {"used": 0, "max": 1}

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

    def test_initial_agent_profiles_is_empty_mapping(self, tmp_path: Path) -> None:
        # v0.5 ticket 06: the role->profile config starts empty (no legs run yet).
        write_initial_feature_status(tmp_path, "FEATURE-001")

        assert _load(tmp_path)["feature"]["agent_profiles"] == {}


class TestRecordAgentProfile:
    """status.record_agent_profile — the v0.5 ticket-06 feature-level profile record.

    Each agent leg records the profile it ran on into ``feature-status.yml``'s
    ``agent_profiles`` ``role -> profile`` dict, audited. This is the deterministic
    record ``compare-profiles`` reads to tell two parallel feature-runs apart.
    """

    def test_records_role_to_profile_and_audits(self, tmp_path: Path) -> None:
        feature_root = tmp_path
        _seed_feature(feature_root)

        record_agent_profile(feature_root, "implementer", "codex-default")

        doc = _feature_doc(feature_root)
        assert doc["feature"]["agent_profiles"] == {"implementer": "codex-default"}
        # The reader returns the same mapping.
        assert agent_profiles(feature_root) == {"implementer": "codex-default"}
        # Audited as a single agent_profile record carrying role + profile.
        records = _audit_records(feature_root)
        assert records[-1]["event"] == "agent_profile"
        assert records[-1]["payload"] == {"role": "implementer", "profile": "codex-default"}

    def test_records_each_role_independently_into_one_dict(self, tmp_path: Path) -> None:
        # A feature's legs may mix profiles (role_defaults); each role is its own slot.
        feature_root = tmp_path
        _seed_feature(feature_root)

        record_agent_profile(feature_root, "implementer", "codex-default")
        record_agent_profile(feature_root, "reviewer", "cc-glm52")
        record_agent_profile(feature_root, "spec_gap_analyst", "cc-glm52")

        assert agent_profiles(feature_root) == {
            "implementer": "codex-default",
            "reviewer": "cc-glm52",
            "spec_gap_analyst": "cc-glm52",
        }

    def test_rerun_same_profile_is_idempotent_in_value(self, tmp_path: Path) -> None:
        # Re-running a leg (e.g. fix loop re-implement) re-audits but leaves the
        # slot at the same value.
        feature_root = tmp_path
        _seed_feature(feature_root)

        record_agent_profile(feature_root, "implementer", "codex-default")
        record_agent_profile(feature_root, "implementer", "codex-default")

        assert agent_profiles(feature_root) == {"implementer": "codex-default"}

    def test_rerun_different_profile_updates_the_slot(self, tmp_path: Path) -> None:
        feature_root = tmp_path
        _seed_feature(feature_root)

        record_agent_profile(feature_root, "implementer", "cc-glm52")
        record_agent_profile(feature_root, "implementer", "codex-default")

        assert agent_profiles(feature_root) == {"implementer": "codex-default"}

    def test_does_not_advance_gate_or_verdict(self, tmp_path: Path) -> None:
        # Recording a profile is a pure record — it must not move gate state.
        feature_root = tmp_path
        _seed_feature(feature_root)
        before = _feature_doc(feature_root)["feature"]

        record_agent_profile(feature_root, "implementer", "codex-default")

        after = _feature_doc(feature_root)["feature"]
        assert after["current_gate"] == before["current_gate"]
        assert after["verdict"] == before["verdict"]
        assert after["status"] == before["status"]

    def test_empty_role_or_profile_fails_loud(self, tmp_path: Path) -> None:
        feature_root = tmp_path
        _seed_feature(feature_root)

        with pytest.raises(ValueError):
            record_agent_profile(feature_root, "", "codex-default")
        with pytest.raises(ValueError):
            record_agent_profile(feature_root, "implementer", "")

    def test_reader_returns_empty_for_pre_v0_5_feature(self, tmp_path: Path) -> None:
        # Older feature-runs have no agent_profiles mapping — legitimate history.
        feature_root = tmp_path
        _seed_feature(feature_root)
        path = feature_root / "status" / FEATURE_STATUS_FILE
        doc = yaml.safe_load(path.read_text())
        del doc["feature"]["agent_profiles"]
        path.write_text(yaml.safe_dump(doc, sort_keys=False))

        assert agent_profiles(feature_root) == {}


class TestDeriveFeatureStatus:
    """status.derive_feature_status - the ADR-0003 D3 projection table.

    ``feature.status`` is a *derived projection* of ``(current_gate, verdict)``,
    never an independent field. This pure function pins the four reachable
    values and fail-loud rejects the unreachable ``(feature_coherence_gate,
    null)`` cell - the coherence evaluator writes ``current_gate`` and
    ``verdict`` atomically (ticket 08), so that state is never observable on
    disk. ``blocked`` is strictly coherence-fail; the lane-gate verdict lives
    on ``lane-decision.json`` and is never written back here, so a lane-gate
    FAIL leaves ``feature.status`` at ``implementing`` (note a).
    """

    @pytest.mark.parametrize(
        "gate", ["requirements_gate", "design_gate", "task_gate"]
    )
    def test_null_verdict_at_planning_gates_projects_planning(
        self, gate: str
    ) -> None:
        assert derive_feature_status(gate, None) == "planning"

    def test_null_verdict_at_lane_gate_projects_implementing(self) -> None:
        assert derive_feature_status("lane_gate", None) == "implementing"

    @pytest.mark.parametrize("gate", GATES)
    def test_pass_verdict_projects_done_regardless_of_gate(
        self, gate: str
    ) -> None:
        assert derive_feature_status(gate, "pass") == "done"

    @pytest.mark.parametrize("gate", GATES)
    def test_fail_verdict_projects_blocked_regardless_of_gate(
        self, gate: str
    ) -> None:
        # blocked is coherence-fail only: it is reached solely via a fail
        # verdict, regardless of which gate current_gate sits at.
        assert derive_feature_status(gate, "fail") == "blocked"

    def test_coherence_gate_with_null_verdict_is_unreachable(self) -> None:
        # (feature_coherence_gate, null) is never observable on disk (D3 note
        # †): the coherence evaluator writes current_gate and verdict in one
        # atomic mutation. Deriving this cell is corruption - fail loud rather
        # than inventing a fifth status value.
        with pytest.raises(ValueError):
            derive_feature_status("feature_coherence_gate", None)

    def test_unknown_verdict_fails_loud(self) -> None:
        # A verdict that is neither pass/fail/null is corruption (§24.2).
        with pytest.raises(ValueError):
            derive_feature_status("lane_gate", "maybe")

    def test_unknown_gate_with_null_verdict_fails_loud(self) -> None:
        with pytest.raises(ValueError):
            derive_feature_status("bogus_gate", None)


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
        # freeze(design) atomically advances current_gate to task_gate
        # (ADR-0003 D2); verdict is still null and task_gate is a planning
        # gate, so the derived feature.status stays "planning" (ADR-0003 D3).
        assert feature["status"] == "planning"
        assert feature["current_gate"] == "task_gate"
        assert feature["verdict"] is None

    def test_freeze_appends_an_audit_record(self, tmp_path: Path) -> None:
        _seed_feature(tmp_path)

        freeze_artifact(tmp_path, "requirements", timestamp="2026-07-19T10:00:00Z")

        # freeze(requirements) atomically advances current_gate (ADR-0003 D2),
        # so the mutation appends a ``freeze`` record followed by an
        # ``advance_gate`` record - find the freeze record rather than assume it
        # is last.
        freeze_records = [
            r for r in _audit_records(tmp_path) if r["event"] == "freeze"
        ]
        assert len(freeze_records) == 1
        assert freeze_records[0]["timestamp"] == "2026-07-19T10:00:00Z"
        assert freeze_records[0]["payload"] == {"artifact": "requirements", "frozen": True}

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


class TestFreezeAdvancesGate:
    """ADR-0003 D2 - freeze_artifact atomically advances current_gate.

    Freezing a human-gate artifact (requirements/design/tasks) advances
    ``current_gate`` to the next stage inside the *same* mutation that flips the
    frozen flag, and re-derives ``feature.status`` (D3) from the resulting
    ``(current_gate, verdict)``. ``freeze(lane_graph)`` does **not** advance -
    lane_graph shares the task gate's freeze window (§4.2), so the advance
    already happened on ``freeze(tasks)``. The advance is monotonic:
    ``current_gate`` never regresses, so out-of-order freezes clamp forward.
    """

    @pytest.mark.parametrize(
        "artifact,expected_gate",
        [
            ("requirements", "design_gate"),
            ("design", "task_gate"),
            ("tasks", "lane_gate"),
        ],
    )
    def test_freeze_advances_current_gate_to_next_stage(
        self, tmp_path: Path, artifact: str, expected_gate: str
    ) -> None:
        _seed_feature(tmp_path)

        freeze_artifact(tmp_path, artifact, timestamp="t")

        assert _feature_doc(tmp_path)["feature"]["current_gate"] == expected_gate

    def test_freeze_tasks_derives_implementing_status(self, tmp_path: Path) -> None:
        # verdict=null + lane_gate -> implementing (D3). The advance and the
        # status derivation land in the same mutation as the flag flip.
        _seed_feature(tmp_path)

        freeze_artifact(tmp_path, "tasks", timestamp="t")

        feature = _feature_doc(tmp_path)["feature"]
        assert feature["current_gate"] == "lane_gate"
        assert feature["status"] == "implementing"

    def test_freeze_requirements_keeps_planning_status(self, tmp_path: Path) -> None:
        # verdict=null + design_gate -> planning (still in the planning gates).
        _seed_feature(tmp_path)

        freeze_artifact(tmp_path, "requirements", timestamp="t")

        feature = _feature_doc(tmp_path)["feature"]
        assert feature["current_gate"] == "design_gate"
        assert feature["status"] == "planning"

    def test_freeze_lane_graph_does_not_advance_current_gate(
        self, tmp_path: Path
    ) -> None:
        # lane_graph never advances current_gate: after freeze(tasks) the gate
        # is already lane_gate, and freeze(lane_graph) leaves it there.
        _seed_feature(tmp_path)
        freeze_artifact(tmp_path, "tasks", timestamp="t1")

        freeze_artifact(tmp_path, "lane_graph", timestamp="t2")

        feature = _feature_doc(tmp_path)["feature"]
        assert feature["current_gate"] == "lane_gate"

    def test_freeze_lane_graph_first_does_not_advance(self, tmp_path: Path) -> None:
        # Freezing lane_graph with no prior tasks freeze must not advance
        # current_gate past requirements_gate - lane_graph is a no-advance
        # artifact by definition.
        _seed_feature(tmp_path)

        freeze_artifact(tmp_path, "lane_graph", timestamp="t")

        assert _feature_doc(tmp_path)["feature"]["current_gate"] == "requirements_gate"

    def test_full_freeze_sequence_ends_at_lane_gate_implementing(
        self, tmp_path: Path
    ) -> None:
        _seed_feature(tmp_path)

        for artifact in ("requirements", "design", "tasks", "lane_graph"):
            freeze_artifact(tmp_path, artifact, timestamp="t")

        feature = _feature_doc(tmp_path)["feature"]
        assert feature["current_gate"] == "lane_gate"
        assert feature["status"] == "implementing"

    def test_freeze_never_produces_coherence_gate_state(
        self, tmp_path: Path
    ) -> None:
        # ADR-0003 D3 note b: assert the (feature_coherence_gate, null) cell is
        # never produced by any writer. freeze is structurally incapable of
        # reaching fcg - _FREEZE_ADVANCE_TARGET caps at lane_gate - so freezing
        # every artifact (in any order) leaves current_gate at lane_gate, never
        # fcg, and feature.status at implementing, never an unreachable value.
        _seed_feature(tmp_path)

        for artifact in FROZEN_ARTIFACTS:
            freeze_artifact(tmp_path, artifact, timestamp="t")
            feature = _feature_doc(tmp_path)["feature"]
            assert feature["current_gate"] != "feature_coherence_gate"
            assert feature["status"] in ("planning", "implementing")

        feature = _feature_doc(tmp_path)["feature"]
        assert feature["current_gate"] == "lane_gate"
        assert feature["status"] == "implementing"

    def test_freeze_advance_is_monotonic_no_regression(self, tmp_path: Path) -> None:
        # current_gate never regresses: freezing requirements *after* tasks are
        # already frozen (current_gate already lane_gate) must not move it back
        # to design_gate.
        _seed_feature(tmp_path)
        freeze_artifact(tmp_path, "tasks", timestamp="t1")
        assert _feature_doc(tmp_path)["feature"]["current_gate"] == "lane_gate"

        freeze_artifact(tmp_path, "requirements", timestamp="t2")

        assert _feature_doc(tmp_path)["feature"]["current_gate"] == "lane_gate"

    def test_freeze_advance_lands_in_same_atomic_mutation_as_flag(
        self, tmp_path: Path
    ) -> None:
        # One read-modify-write produces a freeze record then an advance_gate
        # record, sharing one timestamp - the flag flip and the gate advance
        # are atomic, not two separate operations.
        _seed_feature(tmp_path)

        freeze_artifact(tmp_path, "requirements", timestamp="2026-07-19T10:00:00Z")

        records = _audit_records(tmp_path)
        assert [r["event"] for r in records] == ["freeze", "advance_gate"]
        assert all(r["timestamp"] == "2026-07-19T10:00:00Z" for r in records)
        assert records[0]["payload"] == {"artifact": "requirements", "frozen": True}
        assert records[1]["payload"] == {"current_gate": "design_gate"}

    def test_freeze_lane_graph_emits_no_advance_gate_record(
        self, tmp_path: Path
    ) -> None:
        _seed_feature(tmp_path)

        freeze_artifact(tmp_path, "lane_graph", timestamp="t")

        assert [r["event"] for r in _audit_records(tmp_path)] == ["freeze"]

    def test_no_advance_gate_record_when_gate_does_not_move(
        self, tmp_path: Path
    ) -> None:
        # Monotonic clamp: freeze(requirements) after freeze(tasks) leaves
        # current_gate at lane_gate (no move), so only a freeze record is
        # appended - no spurious advance_gate for a non-advancing freeze.
        _seed_feature(tmp_path)
        freeze_artifact(tmp_path, "tasks", timestamp="t1")

        freeze_artifact(tmp_path, "requirements", timestamp="t2")

        records = _audit_records(tmp_path)
        assert [r["event"] for r in records] == [
            "freeze",
            "advance_gate",
            "freeze",
        ]
        assert records[-1]["payload"] == {"artifact": "requirements", "frozen": True}


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
        # Every gate except feature_coherence_gate is reachable with a null
        # verdict; derive the set from GATES so a new gate can't fall out of
        # sync here. (fcg, null) is unreachable - see the dedicated test below.
        [g for g in GATES if g != "feature_coherence_gate"],
    )
    def test_each_gate_reachable_with_null_verdict_is_accepted(
        self, tmp_path: Path, gate: str
    ) -> None:
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

    def test_set_lane_gate_derives_implementing_status(self, tmp_path: Path) -> None:
        # ADR-0003 D3: writing current_gate re-derives feature.status in the
        # same mutation. null verdict + lane_gate -> implementing.
        _seed_feature(tmp_path)

        set_current_gate(tmp_path, "lane_gate", timestamp="t")

        feature = _feature_doc(tmp_path)["feature"]
        assert feature["current_gate"] == "lane_gate"
        assert feature["status"] == "implementing"

    def test_set_planning_gate_derives_planning_status(self, tmp_path: Path) -> None:
        _seed_feature(tmp_path)

        set_current_gate(tmp_path, "task_gate", timestamp="t")

        feature = _feature_doc(tmp_path)["feature"]
        assert feature["current_gate"] == "task_gate"
        assert feature["status"] == "planning"

    def test_set_coherence_gate_with_null_verdict_is_rejected(
        self, tmp_path: Path
    ) -> None:
        # (feature_coherence_gate, null) is unreachable on disk (ADR-0003 D3
        # note †): the coherence evaluator must write current_gate and verdict
        # atomically (ticket 08). set_current_gate is a current_gate-only
        # writer, so setting fcg while verdict is still null fail-loud refuses
        # rather than producing the unreachable state.
        _seed_feature(tmp_path)

        with pytest.raises(ValueError):
            set_current_gate(tmp_path, "feature_coherence_gate", timestamp="t")

        # State untouched: the rejected mutation rewrote nothing and appended
        # no audit record - the audit log file was never created.
        feature = _feature_doc(tmp_path)["feature"]
        assert feature["current_gate"] == "requirements_gate"
        assert feature["status"] == "planning"
        assert not (tmp_path / AUDIT_LOG_JSON).exists()

    @pytest.mark.parametrize(
        "verdict,expected_status", [("pass", "done"), ("fail", "blocked")]
    )
    def test_set_coherence_gate_with_non_null_verdict_derives_terminal_status(
        self, tmp_path: Path, verdict: str, expected_status: str
    ) -> None:
        # When a verdict is already on disk (the post-coherence state ticket
        # 08's writer will produce), set_current_gate(fcg) is reachable: it sets
        # fcg and derives the terminal status. This pins the path the coherence
        # evaluator will use - (fcg, null) remains the only rejected case. The
        # verdict is injected directly because ticket 04 ships no verdict writer.
        _seed_feature(tmp_path)
        path = tmp_path / "status" / FEATURE_STATUS_FILE
        doc = yaml.safe_load(path.read_text())
        doc["feature"]["verdict"] = verdict
        path.write_text(
            yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, allow_unicode=True)
        )

        set_current_gate(tmp_path, "feature_coherence_gate", timestamp="t")

        feature = _feature_doc(tmp_path)["feature"]
        assert feature["current_gate"] == "feature_coherence_gate"
        assert feature["verdict"] == verdict
        assert feature["status"] == expected_status


class TestRecordCoherenceVerdict:
    """status.record_coherence_verdict - the coherence evaluator's sole
    canonical write (ADR-0003 D2/D4, ticket 08).

    Atomically sets ``current_gate = feature_coherence_gate`` and ``verdict``
    (pass/fail) in one mutation, re-derives ``feature.status`` (done/blocked,
    D3), and audits a ``coherence_gate`` event. The ``(fcg, null)`` transient is
    unreachable by construction: this primitive writes both fields together, so
    it can never be called with a null verdict.
    """

    def test_pass_verdict_writes_done_status(self, tmp_path: Path) -> None:
        _seed_feature(tmp_path)

        record_coherence_verdict(tmp_path, "pass", timestamp="t")

        feature = _feature_doc(tmp_path)["feature"]
        assert feature["current_gate"] == "feature_coherence_gate"
        assert feature["verdict"] == "pass"
        assert feature["status"] == "done"

    def test_fail_verdict_writes_blocked_status(self, tmp_path: Path) -> None:
        _seed_feature(tmp_path)

        record_coherence_verdict(tmp_path, "fail", timestamp="t")

        feature = _feature_doc(tmp_path)["feature"]
        assert feature["current_gate"] == "feature_coherence_gate"
        assert feature["verdict"] == "fail"
        assert feature["status"] == "blocked"

    def test_unknown_verdict_rejected(self, tmp_path: Path) -> None:
        _seed_feature(tmp_path)

        with pytest.raises(ValueError, match="verdict"):
            record_coherence_verdict(tmp_path, "maybe", timestamp="t")

        # State untouched: nothing written, no audit record.
        feature = _feature_doc(tmp_path)["feature"]
        assert feature["current_gate"] == "requirements_gate"
        assert feature["verdict"] is None
        assert not (tmp_path / AUDIT_LOG_JSON).exists()

    def test_re_coherence_overwrites_prior_verdict(self, tmp_path: Path) -> None:
        # ADR-0003 D4: verdict is mutable. A re-coherence overwrites a prior
        # verdict (fail -> pass), mirroring lane-decision.json's verdict.
        _seed_feature(tmp_path)
        record_coherence_verdict(tmp_path, "fail", timestamp="t1")
        assert _feature_doc(tmp_path)["feature"]["status"] == "blocked"

        record_coherence_verdict(tmp_path, "pass", timestamp="t2")

        feature = _feature_doc(tmp_path)["feature"]
        assert feature["current_gate"] == "feature_coherence_gate"
        assert feature["verdict"] == "pass"
        assert feature["status"] == "done"

    def test_verdict_write_is_audited(self, tmp_path: Path) -> None:
        _seed_feature(tmp_path)

        record_coherence_verdict(
            tmp_path,
            "fail",
            audit_payload={"failed_conditions": ["all_p0_p1_handled"]},
            timestamp="2026-07-21T09:00:00Z",
        )

        record = _audit_records(tmp_path)[-1]
        assert record["event"] == "coherence_gate"
        assert record["timestamp"] == "2026-07-21T09:00:00Z"
        # The primitive carries the verdict + terminal gate, plus the
        # evaluator-supplied condition breakdown merged into one record.
        assert record["payload"]["verdict"] == "fail"
        assert record["payload"]["current_gate"] == "feature_coherence_gate"
        assert record["payload"]["failed_conditions"] == ["all_p0_p1_handled"]

    def test_status_always_matches_derivation_after_verdict_write(
        self, tmp_path: Path
    ) -> None:
        _seed_feature(tmp_path)

        record_coherence_verdict(tmp_path, "fail", timestamp="t")

        feature = _feature_doc(tmp_path)["feature"]
        assert feature["status"] == derive_feature_status(
            feature["current_gate"], feature["verdict"]
        )


class TestFeatureStatusIsAlwaysDerived:
    """ADR-0003 D3 - feature.status is never an independent field.

    Every current_gate/verdict mutation re-derives feature.status atomically,
    so on disk it always equals ``derive_feature_status(current_gate,
    verdict)``. This is the single-source-of-truth guarantee: feature.status
    can never drift from the gate state.
    """

    def test_init_status_matches_derivation(self, tmp_path: Path) -> None:
        write_initial_feature_status(tmp_path, "FEATURE-001")

        feature = _load(tmp_path)["feature"]
        assert feature["status"] == derive_feature_status(
            feature["current_gate"], feature["verdict"]
        )

    def test_status_stays_consistent_across_freeze_sequence(
        self, tmp_path: Path
    ) -> None:
        _seed_feature(tmp_path)

        for artifact in ("requirements", "design", "tasks", "lane_graph"):
            freeze_artifact(tmp_path, artifact, timestamp="t")
            feature = _feature_doc(tmp_path)["feature"]
            assert feature["status"] == derive_feature_status(
                feature["current_gate"], feature["verdict"]
            )

    def test_status_stays_consistent_after_set_current_gate(
        self, tmp_path: Path
    ) -> None:
        _seed_feature(tmp_path)

        for gate in ("design_gate", "task_gate", "lane_gate", "task_gate"):
            set_current_gate(tmp_path, gate, timestamp="t")
            feature = _feature_doc(tmp_path)["feature"]
            assert feature["status"] == derive_feature_status(
                feature["current_gate"], feature["verdict"]
            )


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
