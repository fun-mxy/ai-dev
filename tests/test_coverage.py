"""coverage - the freeze-gate coverage precheck (v0.6 ticket 03, ADR-0008 D3).

Coverage-completeness is the freeze-gate half of ADR-0008's coverage split. These
tests pin the reusable helper: ``design_coverage`` (every frozen REQ in >=1
design ``requirement_mapping``, §18.2) and the ``freeze_gate_coverage`` dispatch.
The helper is pure over the canonical artifacts (no model, no subprocess).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_dev.coverage import (
    CoverageResult,
    design_coverage,
    freeze_gate_coverage,
    tasks_coverage,
)
from ai_dev.feature_run import create_feature_run
from ai_dev.promote import promote_design, promote_requirements, promote_tasks
from ai_dev.status import freeze_artifact

FEATURE_ID = "FEATURE-001"


def _feature_with_frozen_requirements(
    tmp_path: Path, n_reqs: int = 2
) -> tuple[Path, list[str]]:
    """Create a feature run, promote + freeze ``n_reqs`` requirements; return ids."""
    create_feature_run(tmp_path, "build the foo")
    root = tmp_path / ".ai-dev" / "features" / FEATURE_ID
    reqs = [
        {"key": f"r{i}", "statement": f"The system shall requirement {i}."}
        for i in range(1, n_reqs + 1)
    ]
    promote_requirements(
        root,
        FEATURE_ID,
        {"requirements": reqs, "acceptance_criteria": []},
        origin="test",
    )
    freeze_artifact(root, "requirements", origin="test")
    doc = json.loads((root / "01-requirements.json").read_text())
    return root, [r["id"] for r in doc["requirements"]]


def _design_proposal_covering(req_ids: list[str]) -> dict:
    """A design proposal whose requirement_mapping covers every REQ once."""
    return {
        "design_elements": [{"key": "d1", "name": "core"}],
        "requirement_mapping": [
            {"key": f"m{i}", "requirement": rid, "design_elements": ["d1"]}
            for i, rid in enumerate(req_ids)
        ],
    }


class TestDesignCoverage:
    def test_passes_when_all_reqs_mapped(self, tmp_path: Path) -> None:
        root, req_ids = _feature_with_frozen_requirements(tmp_path)
        promote_design(
            root, FEATURE_ID, _design_proposal_covering(req_ids), origin="test"
        )
        result = design_coverage(root)
        assert result.ok
        assert result.uncovered == ()
        assert result.covered == frozenset(req_ids)

    def test_detects_uncovered_req(self, tmp_path: Path) -> None:
        # Map only the first REQ; the second is uncovered -> refuse to freeze.
        root, req_ids = _feature_with_frozen_requirements(tmp_path)
        proposal = _design_proposal_covering(req_ids)
        proposal["requirement_mapping"] = [
            {"key": "m0", "requirement": req_ids[0], "design_elements": ["d1"]}
        ]
        promote_design(root, FEATURE_ID, proposal, origin="test")
        result = design_coverage(root)
        assert not result.ok
        assert result.uncovered == (req_ids[1],)

    def test_req_referenced_twice_still_covered(self, tmp_path: Path) -> None:
        root, req_ids = _feature_with_frozen_requirements(tmp_path)
        proposal = _design_proposal_covering(req_ids)
        # A second mapping also referencing REQ-001 (redundant coverage is fine).
        proposal["requirement_mapping"].append(
            {"key": "mX", "requirement": req_ids[0], "design_elements": ["d1"]}
        )
        promote_design(root, FEATURE_ID, proposal, origin="test")
        assert design_coverage(root).ok

    def test_empty_design_covers_nothing(self, tmp_path: Path) -> None:
        # A feature with frozen requirements but no design promoted: the seeded
        # 02-design.json has an empty requirement_mapping, so every REQ is
        # uncovered -> the freeze gate refuses.
        root, req_ids = _feature_with_frozen_requirements(tmp_path)
        result = design_coverage(root)
        assert not result.ok
        assert set(result.uncovered) == set(req_ids)

    def test_fails_loud_when_requirements_not_frozen(self, tmp_path: Path) -> None:
        create_feature_run(tmp_path, "build the foo")
        root = tmp_path / ".ai-dev" / "features" / FEATURE_ID
        with pytest.raises(ValueError, match="not frozen"):
            design_coverage(root)

    def test_coverage_counts_only_real_req_refs(self, tmp_path: Path) -> None:
        # A hand-edited mapping that references a non-existent REQ must not count
        # as coverage (defensive: promote's reference-integrity already prevents
        # this, but the precheck does not trust the file blindly).
        root, req_ids = _feature_with_frozen_requirements(tmp_path)
        promote_design(root, FEATURE_ID, _design_proposal_covering(req_ids), origin="test")
        # Surgically corrupt the canonical design: point one mapping at REQ-999.
        doc = json.loads((root / "02-design.json").read_text())
        doc["requirement_mapping"][0]["requirement"] = "REQ-999"
        (root / "02-design.json").write_text(json.dumps(doc))
        result = design_coverage(root)
        # REQ-999 is not a real frozen REQ, so it does not cover REQ-001; the
        # originally-covered REQ-001 is now uncovered.
        assert req_ids[0] in result.uncovered


class TestFreezeGateCoverageDispatch:
    def test_design_dispatches_to_design_coverage(self, tmp_path: Path) -> None:
        root, req_ids = _feature_with_frozen_requirements(tmp_path)
        promote_design(
            root, FEATURE_ID, _design_proposal_covering(req_ids), origin="test"
        )
        result = freeze_gate_coverage("design", root)
        assert isinstance(result, CoverageResult)
        assert result.artifact == "design"
        assert result.upstream_type == "REQ"

    def test_requirements_returns_none(self, tmp_path: Path) -> None:
        # Requirements is the root (no upstream) - no coverage precheck.
        root, _ = _feature_with_frozen_requirements(tmp_path)
        assert freeze_gate_coverage("requirements", root) is None

    def test_tasks_dispatches_to_tasks_coverage(self, tmp_path: Path) -> None:
        # The tasks coverage precheck (every REQ+DES in some task) lands in 04.
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        promote_tasks(
            root, FEATURE_ID, _tasks_proposal_covering(req_ids, des_ids), origin="test"
        )
        result = freeze_gate_coverage("tasks", root)
        assert isinstance(result, CoverageResult)
        assert result.artifact == "tasks"
        assert result.upstream_type == "REQ/DES"
        assert result.where == "task"

    def test_lane_graph_returns_none(self, tmp_path: Path) -> None:
        # lane_graph shares the task gate's freeze window - no separate precheck.
        root, _, _ = _feature_with_frozen_requirements_and_design(tmp_path)
        assert freeze_gate_coverage("lane_graph", root) is None



def _feature_with_frozen_requirements_and_design(
    tmp_path: Path, n_reqs: int = 2
) -> tuple[Path, list[str], list[str]]:
    """Create a feature run, promote+freeze requirements AND design; return ids.

    Tasks coverage spans two upstreams (REQ + DES), so its tests need both frozen.
    The design has one element per REQ (so there are ``n_reqs`` DES ids to cover,
    each mapped to its own REQ - satisfying the design gate too). Returns
    ``(root, req_ids, des_ids)``.
    """
    root, req_ids = _feature_with_frozen_requirements(tmp_path, n_reqs)
    design_elements = [
        {"key": f"d{i}", "name": f"element {i}"} for i in range(1, n_reqs + 1)
    ]
    requirement_mapping = [
        {
            "key": f"m{i}",
            "requirement": req_ids[i - 1],
            "design_elements": [f"d{i}"],
        }
        for i in range(1, n_reqs + 1)
    ]
    promote_design(
        root,
        FEATURE_ID,
        {
            "design_elements": design_elements,
            "requirement_mapping": requirement_mapping,
        },
        origin="test",
    )
    freeze_artifact(root, "design", origin="test")
    des_doc = json.loads((root / "02-design.json").read_text())
    des_ids = [el["id"] for el in des_doc["design_elements"]]
    return root, req_ids, des_ids


def _tasks_proposal_covering(req_ids: list[str], des_ids: list[str]) -> dict:
    """A tasks proposal whose tasks cover every REQ and DES exactly once."""
    return {
        "lane_purpose": "cover everything end to end",
        "tasks": [
            {
                "key": f"t{i}",
                "summary": f"task {i}",
                "related_requirements": [req_ids[i - 1]],
                "related_design": [des_ids[i - 1]],
                "expected_files": [f"src/t{i}.py"],
                "exclusive_files": [f"src/t{i}.py"],
            }
            for i in range(1, len(req_ids) + 1)
        ],
    }


class TestTasksCoverage:
    def test_passes_when_all_reqs_and_des_covered(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        promote_tasks(
            root, FEATURE_ID, _tasks_proposal_covering(req_ids, des_ids), origin="test"
        )
        result = tasks_coverage(root)
        assert result.ok
        assert result.uncovered == ()
        assert result.covered == frozenset(req_ids) | frozenset(des_ids)

    def test_detects_uncovered_req_and_des(self, tmp_path: Path) -> None:
        # Drop the task covering REQ-002/DES-002: both are uncovered -> refuse.
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        proposal = _tasks_proposal_covering(req_ids, des_ids)
        proposal["tasks"] = proposal["tasks"][:1]  # only t1 (REQ-001, DES-001)
        promote_tasks(root, FEATURE_ID, proposal, origin="test")
        result = tasks_coverage(root)
        assert not result.ok
        assert set(result.uncovered) == {req_ids[1], des_ids[1]}

    def test_detects_uncovered_des_with_req_covered(self, tmp_path: Path) -> None:
        # REQ-002 covered (via t2) but its DES-002 not referenced -> DES-002 gap
        # only (REQ-002 is covered, DES-001 is covered by t1).
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        proposal = _tasks_proposal_covering(req_ids, des_ids)
        proposal["tasks"][1]["related_design"] = [des_ids[0]]  # t2 -> DES-001 not DES-002
        promote_tasks(root, FEATURE_ID, proposal, origin="test")
        result = tasks_coverage(root)
        assert not result.ok
        assert result.uncovered == (des_ids[1],)

    def test_empty_tasks_covers_nothing(self, tmp_path: Path) -> None:
        # Seeded 03-tasks.json has empty tasks -> every REQ+DES uncovered.
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        result = tasks_coverage(root)
        assert not result.ok
        assert set(result.uncovered) == set(req_ids) | set(des_ids)

    def test_fails_loud_when_requirements_not_frozen(self, tmp_path: Path) -> None:
        create_feature_run(tmp_path, "build the foo")
        root = tmp_path / ".ai-dev" / "features" / FEATURE_ID
        with pytest.raises(ValueError, match="not frozen"):
            tasks_coverage(root)

    def test_fails_loud_when_design_not_frozen(self, tmp_path: Path) -> None:
        # Requirements frozen, design promoted but NOT frozen.
        root, req_ids = _feature_with_frozen_requirements(tmp_path)
        promote_design(root, FEATURE_ID, _design_proposal_covering(req_ids), origin="test")
        with pytest.raises(ValueError, match="not frozen"):
            tasks_coverage(root)

    def test_coverage_counts_only_real_refs(self, tmp_path: Path) -> None:
        # A hand-edited task referencing a non-existent REQ/DES must not count as
        # coverage (defensive: promote's reference-integrity already prevents this
        # for a real promote, but the precheck does not trust the file blindly).
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        promote_tasks(
            root, FEATURE_ID, _tasks_proposal_covering(req_ids, des_ids), origin="test"
        )
        doc = json.loads((root / "03-tasks.json").read_text())
        doc["tasks"][0]["related_requirements"] = ["REQ-999"]
        doc["tasks"][0]["related_design"] = ["DES-999"]
        (root / "03-tasks.json").write_text(json.dumps(doc))
        result = tasks_coverage(root)
        # REQ-001 + DES-001 no longer covered (their task now points at phantoms).
        assert req_ids[0] in result.uncovered
        assert des_ids[0] in result.uncovered

    def test_refusal_message_mentions_task_and_req_des(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        proposal = _tasks_proposal_covering(req_ids, des_ids)
        proposal["tasks"] = proposal["tasks"][:1]
        promote_tasks(root, FEATURE_ID, proposal, origin="test")
        result = tasks_coverage(root)
        msg = result.refusal_message("tasks")
        assert "REQ/DES" in msg
        assert "in any task" in msg
        assert "generate-tasks --feedback" in msg
