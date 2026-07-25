"""Direct-edit refinement channel — ``render`` + ``allocate-id`` (v0.6 ticket 06).

ADR-0008 D4's optional second refinement channel: alongside the model-mediated
feedback loop (the primary path), a human may directly edit the canonical
*unfrozen* ``.json`` of a planning artifact for a surgical fix, then run
``render`` to re-render the ``.md`` mirror (single source of truth, D2) and
``allocate-id`` to mint the next counter id for a human-added item. These tests
exercise the seam with synthetic artifacts; no model runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_dev.coverage import freeze_gate_coverage
from ai_dev.feature_ids import allocate_id, preview_next_id
from ai_dev.feature_run import create_feature_run
from ai_dev.promote import (
    FrozenArtifactWriteError,
    RENDERABLE_ARTIFACTS,
    RenderResult,
    UnresolvedRefError,
    promote_design,
    promote_requirements,
    promote_tasks,
    render_artifact,
    validate_artifact_refs,
)
from ai_dev.status import freeze_artifact
from ai_dev.templates import (
    DESIGN_JSON,
    DESIGN_MD,
    REQUIREMENTS_JSON,
    REQUIREMENTS_MD,
    TASKS_JSON,
)

FEATURE_ID = "FEATURE-001"


def _feature_root(tmp_path: Path) -> Path:
    """Create a real feature run (status + counters + seeded templates)."""
    create_feature_run(tmp_path, "build the foo")
    return tmp_path / ".ai-dev" / "features" / FEATURE_ID


def _audit_records(root: Path) -> list[dict]:
    return json.loads((root / "audit.log.json").read_text())


def _allocate_events(root: Path) -> list[dict]:
    """Only the ``allocate_id`` audit events (create_feature_run emits others)."""
    return [r for r in _audit_records(root) if r["event"] == "allocate_id"]


def _write_json(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def _promote_requirements(root: Path) -> None:
    """Promote a 2-REQ / 1-AC requirements proposal (the root artifact)."""
    promote_requirements(
        root,
        FEATURE_ID,
        {
            "requirements": [
                {"key": "r1", "statement": "The system shall foo."},
                {"key": "r2", "statement": "The system shall bar."},
            ],
            "acceptance_criteria": [
                {"key": "a1", "requirement": "r1", "criterion": "foo observable"}
            ],
        },
        origin="test",
    )


# ---------------------------------------------------------------------------
# render_artifact — re-render the .md mirror from the edited .json.
# ---------------------------------------------------------------------------


class TestRenderArtifact:
    def test_renders_mirror_reflecting_a_direct_content_edit(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        _promote_requirements(root)
        before = (root / REQUIREMENTS_MD).read_text()
        assert "The system shall foo." in before

        # A human directly edits the canonical unfrozen JSON (a typo fix).
        doc = json.loads((root / REQUIREMENTS_JSON).read_text())
        doc["requirements"][0]["statement"] = "The system shall FOO (edited)."
        _write_json(root / REQUIREMENTS_JSON, doc)

        result = render_artifact(root, FEATURE_ID, "requirements", origin="test")

        assert isinstance(result, RenderResult)
        assert result.artifact == "requirements"
        assert result.json_path == root / REQUIREMENTS_JSON
        assert result.md_path == root / REQUIREMENTS_MD
        mirror = result.md_path.read_text()
        # The mirror now reflects the edited content (single source of truth).
        assert "The system shall FOO (edited)." in mirror
        assert "The system shall foo." not in mirror

    def test_render_is_a_deterministic_fixed_point(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        _promote_requirements(root)
        doc = json.loads((root / REQUIREMENTS_JSON).read_text())
        # render produces byte-identical output to the mirror promote wrote.
        rendered = render_artifact(root, FEATURE_ID, "requirements", origin="test")
        # A second render of unchanged content is a fixed point.
        again = render_artifact(root, FEATURE_ID, "requirements", origin="test")
        assert rendered.md_path.read_text() == again.md_path.read_text()
        assert doc["requirements"][0]["statement"] in again.md_path.read_text()

    def test_appends_render_audit_event(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        _promote_requirements(root)
        before = len(_audit_records(root))
        render_artifact(root, FEATURE_ID, "requirements", origin="test")
        records = _audit_records(root)
        assert len(records) == before + 1
        event = records[-1]
        assert event["event"] == "render"
        assert event["origin"] == "test"
        assert event["payload"]["artifact"] == "requirements"
        assert event["payload"]["source"] == REQUIREMENTS_JSON
        assert event["payload"]["mirror"] == REQUIREMENTS_MD

    def test_refuses_a_frozen_artifact(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        _promote_requirements(root)
        freeze_artifact(root, "requirements", origin="test")
        with pytest.raises(FrozenArtifactWriteError, match="frozen"):
            render_artifact(root, FEATURE_ID, "requirements", origin="test")
        # The frozen mirror is left untouched (no resync of frozen content).
        records = [r for r in _audit_records(root) if r["event"] == "render"]
        assert records == []

    def test_fails_loud_when_nothing_promoted_to_render(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        # Simulate the canonical JSON being absent (never promoted / deleted).
        (root / REQUIREMENTS_JSON).unlink()
        with pytest.raises(ValueError, match="missing or unreadable"):
            render_artifact(root, FEATURE_ID, "requirements", origin="test")

    def test_fails_loud_on_corrupt_json(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        _promote_requirements(root)
        (root / REQUIREMENTS_JSON).write_text("{ not valid json")
        with pytest.raises(ValueError, match="missing or unreadable"):
            render_artifact(root, FEATURE_ID, "requirements", origin="test")

    def test_unknown_artifact_rejected(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        with pytest.raises(ValueError, match="not renderable"):
            render_artifact(root, FEATURE_ID, "lane_graph", origin="test")

    def test_renders_design_and_tasks_mirrors(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        _promote_requirements(root)
        freeze_artifact(root, "requirements", origin="test")
        promote_design(
            root,
            FEATURE_ID,
            {
                "design_elements": [
                    {"key": "d1", "name": "Greeting module"},
                ],
                "requirement_mapping": [
                    {"key": "m1", "requirement": "REQ-001", "design_elements": ["d1"]},
                    {"key": "m2", "requirement": "REQ-002", "design_elements": ["d1"]},
                ],
            },
            origin="test",
        )
        # Direct-edit the design JSON (reword the DES name), then re-render.
        doc = json.loads((root / DESIGN_JSON).read_text())
        doc["design_elements"][0]["name"] = "Greeting module (reworded)"
        _write_json(root / DESIGN_JSON, doc)
        result = render_artifact(root, FEATURE_ID, "design", origin="test")
        assert "Greeting module (reworded)" in result.md_path.read_text()

    def test_renderable_artifacts_are_the_three_json_md_pairs(self) -> None:
        assert RENDERABLE_ARTIFACTS == ("requirements", "design", "tasks")


# ---------------------------------------------------------------------------
# allocate-id + preview_next_id — ids stay in the counter, out of human hands.
# ---------------------------------------------------------------------------


class TestAllocateIdHelper:
    def test_allocate_id_mints_next_counter_id(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        assert allocate_id(root, "REQ", origin="test") == "REQ-001"
        assert allocate_id(root, "REQ", origin="test") == "REQ-002"
        # A different type has its own counter namespace.
        assert allocate_id(root, "AC", origin="test") == "AC-001"

    def test_preview_next_id_does_not_mint(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        before = len(_audit_records(root))
        # Preview agrees with the would-be allocation (REQ counter is fresh)...
        assert preview_next_id(root, "REQ") == "REQ-001"
        # ...but mints nothing: no counter write, no new audit record of any kind.
        assert len(_audit_records(root)) == before
        # The real allocation still starts at 001 (the preview consumed nothing).
        assert allocate_id(root, "REQ", origin="test") == "REQ-001"

    def test_preview_tracks_real_allocations(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        req_before = len(_allocate_events(root))
        allocate_id(root, "REQ", origin="test")
        allocate_id(root, "REQ", origin="test")
        assert preview_next_id(root, "REQ") == "REQ-003"
        # The preview appended no allocate_id record (only the two real ones).
        assert len(_allocate_events(root)) == req_before + 2

    def test_preview_rejects_unknown_type(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        with pytest.raises(ValueError, match="unknown stable-id type"):
            preview_next_id(root, "BOGUS")


# ---------------------------------------------------------------------------
# Criterion 4: the freeze-gate coverage precheck still runs after a direct edit,
# reading the edited canonical JSON (reference-integrity is preserved by a
# content-only edit; coverage reflects the edit at the subsequent freeze).
# ---------------------------------------------------------------------------


class TestFreezeChecksSurviveDirectEdit:
    def _design_feature(self, tmp_path: Path) -> Path:
        root = _feature_root(tmp_path)
        _promote_requirements(root)
        freeze_artifact(root, "requirements", origin="test")
        promote_design(
            root,
            FEATURE_ID,
            {
                "design_elements": [{"key": "d1", "name": "Greeting module"}],
                "requirement_mapping": [
                    {"key": "m1", "requirement": "REQ-001", "design_elements": ["d1"]},
                    {"key": "m2", "requirement": "REQ-002", "design_elements": ["d1"]},
                ],
            },
            origin="test",
        )
        return root

    def test_coverage_passes_after_a_clean_content_edit(self, tmp_path: Path) -> None:
        root = self._design_feature(tmp_path)
        # A surgical content edit (reword a DES name) leaves coverage intact.
        doc = json.loads((root / DESIGN_JSON).read_text())
        doc["design_elements"][0]["name"] = "Greeting module (edited)"
        _write_json(root / DESIGN_JSON, doc)
        render_artifact(root, FEATURE_ID, "design", origin="test")
        coverage = freeze_gate_coverage("design", root)
        assert coverage is not None and coverage.ok

    def test_coverage_catches_a_gap_introduced_by_direct_edit(self, tmp_path: Path) -> None:
        root = self._design_feature(tmp_path)
        # A human deletes the mapping covering REQ-002 -> a coverage gap.
        doc = json.loads((root / DESIGN_JSON).read_text())
        doc["requirement_mapping"] = [
            m for m in doc["requirement_mapping"] if m["requirement"] != "REQ-002"
        ]
        _write_json(root / DESIGN_JSON, doc)
        render_artifact(root, FEATURE_ID, "design", origin="test")
        coverage = freeze_gate_coverage("design", root)
        assert coverage is not None
        assert not coverage.ok
        assert "REQ-002" in coverage.uncovered

    def test_content_edit_preserves_stitched_refs(self, tmp_path: Path) -> None:
        root = self._design_feature(tmp_path)
        # Edit only prose content; the stitched REQ/DES refs must survive.
        doc = json.loads((root / DESIGN_JSON).read_text())
        doc["design_elements"][0]["name"] = "Renamed module"
        _write_json(root / DESIGN_JSON, doc)
        result = render_artifact(root, FEATURE_ID, "design", origin="test")
        mirror = result.md_path.read_text()
        # The stitched refs (REQ-001/REQ-002, DES-001) are still rendered.
        assert "REQ-001" in mirror and "REQ-002" in mirror
        assert "DES-001" in mirror


# ---------------------------------------------------------------------------
# Criterion 4 (reference-integrity half): render re-validates refs on the
# direct-edited doc — the direct-edit channel bypasses promote, so render is the
# bookend that catches a dangling ref a human introduced (ADR-0008 D3).
# ---------------------------------------------------------------------------


class TestRenderReferenceIntegrity:
    def _design_feature(self, tmp_path: Path) -> Path:
        root = _feature_root(tmp_path)
        _promote_requirements(root)
        freeze_artifact(root, "requirements", origin="test")
        promote_design(
            root,
            FEATURE_ID,
            {
                "design_elements": [{"key": "d1", "name": "Greeting module"}],
                "requirement_mapping": [
                    {"key": "m1", "requirement": "REQ-001", "design_elements": ["d1"]},
                    {"key": "m2", "requirement": "REQ-002", "design_elements": ["d1"]},
                ],
            },
            origin="test",
        )
        return root

    def test_render_catches_a_dangling_ac_ref(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        _promote_requirements(root)
        # A human retypes an AC's requirement ref to a non-existent REQ.
        doc = json.loads((root / REQUIREMENTS_JSON).read_text())
        doc["acceptance_criteria"][0]["requirement"] = "REQ-999"
        _write_json(root / REQUIREMENTS_JSON, doc)
        with pytest.raises(UnresolvedRefError, match="REQ-999"):
            render_artifact(root, FEATURE_ID, "requirements", origin="test")

    def test_render_catches_a_dangling_design_element_ref(self, tmp_path: Path) -> None:
        root = self._design_feature(tmp_path)
        doc = json.loads((root / DESIGN_JSON).read_text())
        # Point a mapping at a DES that was never allocated.
        doc["requirement_mapping"][0]["design_elements"] = ["DES-999"]
        _write_json(root / DESIGN_JSON, doc)
        with pytest.raises(UnresolvedRefError, match="DES-999"):
            render_artifact(root, FEATURE_ID, "design", origin="test")

    def test_render_catches_a_dangling_upstream_req_ref(self, tmp_path: Path) -> None:
        root = self._design_feature(tmp_path)
        doc = json.loads((root / DESIGN_JSON).read_text())
        # Point a mapping's requirement at a REQ not in the frozen upstream.
        doc["requirement_mapping"][0]["requirement"] = "REQ-999"
        _write_json(root / DESIGN_JSON, doc)
        with pytest.raises(UnresolvedRefError, match="REQ-999"):
            render_artifact(root, FEATURE_ID, "design", origin="test")

    def _tasks_feature(self, tmp_path: Path) -> Path:
        # Tasks stitch against FROZEN requirements AND design (two upstreams),
        # so both must be promoted then frozen before tasks is promoted.
        root = self._design_feature(tmp_path)
        freeze_artifact(root, "design", origin="test")
        promote_tasks(
            root,
            FEATURE_ID,
            {
                "lane_purpose": "Implement the greet CLI end to end.",
                "tasks": [
                    {
                        "key": "t1",
                        "summary": "Implement greeting formatter",
                        "related_requirements": ["REQ-001"],
                        "related_design": ["DES-001"],
                        "expected_files": ["src/greet.py"],
                        "exclusive_files": ["src/greet.py"],
                    },
                ],
            },
            origin="test",
        )
        return root

    def test_render_catches_a_dangling_task_req_ref(self, tmp_path: Path) -> None:
        root = self._tasks_feature(tmp_path)
        doc = json.loads((root / TASKS_JSON).read_text())
        # Point a task's related_requirements at a REQ not in the frozen upstream.
        doc["tasks"][0]["related_requirements"] = ["REQ-999"]
        _write_json(root / TASKS_JSON, doc)
        with pytest.raises(UnresolvedRefError, match="REQ-999"):
            render_artifact(root, FEATURE_ID, "tasks", origin="test")

    def test_render_catches_a_dangling_task_des_ref(self, tmp_path: Path) -> None:
        root = self._tasks_feature(tmp_path)
        doc = json.loads((root / TASKS_JSON).read_text())
        # Point a task's related_design at a DES not in the frozen upstream.
        doc["tasks"][0]["related_design"] = ["DES-999"]
        _write_json(root / TASKS_JSON, doc)
        with pytest.raises(UnresolvedRefError, match="DES-999"):
            render_artifact(root, FEATURE_ID, "tasks", origin="test")

    def test_render_does_not_resync_mirror_over_a_malformed_edit(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        _promote_requirements(root)
        mirror_before = (root / REQUIREMENTS_MD).read_text()
        doc = json.loads((root / REQUIREMENTS_JSON).read_text())
        doc["acceptance_criteria"][0]["requirement"] = "REQ-999"
        _write_json(root / REQUIREMENTS_JSON, doc)
        with pytest.raises(UnresolvedRefError):
            render_artifact(root, FEATURE_ID, "requirements", origin="test")
        # The mirror is left untouched (no resync over a malformed artifact),
        # and no render audit event was appended.
        assert (root / REQUIREMENTS_MD).read_text() == mirror_before
        assert [r for r in _audit_records(root) if r["event"] == "render"] == []

    def test_validate_artifact_refs_passes_a_clean_doc(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        _promote_requirements(root)
        doc = json.loads((root / REQUIREMENTS_JSON).read_text())
        # No exception for a doc whose refs all resolve.
        validate_artifact_refs("requirements", doc, root)

