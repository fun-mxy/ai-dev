"""templates.seed_artifact_templates — the §7 artifact templates (ticket 05).

When a feature run is created, the four §7 artifacts are seeded as structured
templates for the v0.2 Planner to fill:

* ``01-requirements.md`` / ``.json``  (§7.2)
* ``02-design.md``       / ``.json``  (§7.3)
* ``03-tasks.md``                      (§7.4 — markdown only)
* ``04-lane-graph.yml``                (§7.5)

Every template carries a ``frozen: false`` marker (§4.2); stable-id slots
(REQ/AC/DES/TASK) are seeded empty as placeholders, while the lane graph is
seeded with a *real* lane id allocated upstream via ticket 03's allocator.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from ai_dev.templates import seed_artifact_templates

FEATURE_ID = "FEATURE-001"
LANE_ID = "LANE-001"


def _seed(tmp_path: Path) -> Path:
    """Seed templates into a feature root and return that root."""
    feature_root = tmp_path / FEATURE_ID
    feature_root.mkdir()
    seed_artifact_templates(feature_root, FEATURE_ID, LANE_ID)
    return feature_root


class TestSeedArtifactTemplates:
    def test_writes_all_six_template_files(self, tmp_path: Path) -> None:
        root = _seed(tmp_path)

        for name in (
            "01-requirements.md",
            "01-requirements.json",
            "02-design.md",
            "02-design.json",
            "03-tasks.md",
            "04-lane-graph.yml",
        ):
            assert (root / name).is_file(), name

    def test_returns_paths_written(self, tmp_path: Path) -> None:
        feature_root = tmp_path / FEATURE_ID
        feature_root.mkdir()
        paths = seed_artifact_templates(feature_root, FEATURE_ID, LANE_ID)

        names = {p.name for p in paths}
        assert names == {
            "01-requirements.md",
            "01-requirements.json",
            "02-design.md",
            "02-design.json",
            "03-tasks.md",
            "04-lane-graph.yml",
        }


class TestRequirementsTemplate:
    """§7.2 — requirements artifact: REQ/AC ids, priority, scope, constraints,
    open questions, frozen state."""

    def test_json_parses_and_has_all_section_7_2_fields(self, tmp_path: Path) -> None:
        root = _seed(tmp_path)
        doc = json.loads((root / "01-requirements.json").read_text())

        assert doc["feature"] == FEATURE_ID
        # §7.2 field set, including the frozen marker.
        assert set(doc) == {
            "feature",
            "frozen",
            "requirements",
            "acceptance_criteria",
            "priority",
            "scope",
            "constraints",
            "open_questions",
        }

    def test_frozen_false(self, tmp_path: Path) -> None:
        root = _seed(tmp_path)
        assert json.loads((root / "01-requirements.json").read_text())["frozen"] is False

    def test_stable_id_slots_are_empty_placeholders(self, tmp_path: Path) -> None:
        # REQ / AC ids are not allocated at creation — the Planner fills them.
        root = _seed(tmp_path)
        doc = json.loads((root / "01-requirements.json").read_text())
        assert doc["requirements"] == []
        assert doc["acceptance_criteria"] == []

    def test_markdown_mirrors_feature_id_and_frozen(self, tmp_path: Path) -> None:
        root = _seed(tmp_path)
        text = (root / "01-requirements.md").read_text()
        assert FEATURE_ID in text
        assert "Frozen: false" in text


class TestDesignTemplate:
    """§7.3 — design artifact: DES ids, architecture decision, data model,
    API/CLI contract, file layout, invariants, risk, dependency, REQ/AC mapping."""

    def test_json_has_all_section_7_3_fields(self, tmp_path: Path) -> None:
        root = _seed(tmp_path)
        doc = json.loads((root / "02-design.json").read_text())

        assert doc["feature"] == FEATURE_ID
        assert set(doc) == {
            "feature",
            "frozen",
            "design_elements",
            "architecture_decision",
            "data_model",
            "api_cli_contract",
            "file_layout",
            "invariants",
            "risks",
            "dependencies",
            "requirement_mapping",
        }

    def test_frozen_false(self, tmp_path: Path) -> None:
        root = _seed(tmp_path)
        assert json.loads((root / "02-design.json").read_text())["frozen"] is False

    def test_des_id_slot_is_empty_placeholder(self, tmp_path: Path) -> None:
        root = _seed(tmp_path)
        doc = json.loads((root / "02-design.json").read_text())
        assert doc["design_elements"] == []

    def test_markdown_mirrors_feature_id_and_frozen(self, tmp_path: Path) -> None:
        root = _seed(tmp_path)
        text = (root / "02-design.md").read_text()
        assert FEATURE_ID in text
        assert "Frozen: false" in text


class TestTasksTemplate:
    """§7.4 — markdown-only human task list. Checkboxes are not canonical state;
    canonical task state lives in status/task-status.yml."""

    def test_markdown_carries_feature_id_and_frozen(self, tmp_path: Path) -> None:
        root = _seed(tmp_path)
        text = (root / "03-tasks.md").read_text()
        assert FEATURE_ID in text
        assert "Frozen: false" in text

    def test_markdown_states_canonical_state_lives_elsewhere(self, tmp_path: Path) -> None:
        # §7.4: the markdown must not pretend to be canonical task state.
        root = _seed(tmp_path)
        text = (root / "03-tasks.md").read_text()
        assert "task-status.yml" in text

    def test_no_machine_mirror_is_written(self, tmp_path: Path) -> None:
        # §6 lists only 03-tasks.md — there is no 03-tasks.json counterpart.
        root = _seed(tmp_path)
        assert not (root / "03-tasks.json").exists()


class TestLaneGraphTemplate:
    """§7.5 — machine-readable lane DAG. MVP v0 is a single lane whose id is the
    one allocated upstream by ticket 03 (not a placeholder string)."""

    def test_yaml_parses_and_is_single_lane(self, tmp_path: Path) -> None:
        root = _seed(tmp_path)
        doc = yaml.safe_load((root / "04-lane-graph.yml").read_text())
        assert isinstance(doc, dict)
        assert len(doc["lanes"]) == 1

    def test_carries_feature_provenance_like_other_machine_artifacts(
        self, tmp_path: Path
    ) -> None:
        # All feature-run machine artifacts (req/design json, lane-graph yml,
        # and final-report.json) carry the owning feature id — kept consistent.
        root = _seed(tmp_path)
        graph = yaml.safe_load((root / "04-lane-graph.yml").read_text())
        req = json.loads((root / "01-requirements.json").read_text())
        design = json.loads((root / "02-design.json").read_text())
        assert graph["feature"] == FEATURE_ID
        assert req["feature"] == FEATURE_ID
        assert design["feature"] == FEATURE_ID

    def test_lane_id_is_the_allocated_one_not_hardcoded(self, tmp_path: Path) -> None:
        # Pass a different lane id; the graph must echo it verbatim. This proves
        # the id flows from the allocator rather than being a baked-in string.
        feature_root = tmp_path / FEATURE_ID
        feature_root.mkdir()
        seed_artifact_templates(feature_root, FEATURE_ID, "LANE-042")
        doc = yaml.safe_load((feature_root / "04-lane-graph.yml").read_text())
        assert doc["lanes"][0]["id"] == "LANE-042"

    def test_frozen_false(self, tmp_path: Path) -> None:
        root = _seed(tmp_path)
        doc = yaml.safe_load((root / "04-lane-graph.yml").read_text())
        assert doc["frozen"] is False

    def test_lane_has_full_section_7_5_shape(self, tmp_path: Path) -> None:
        # The format keeps the full §7.5 lane-entry shape (extensible), even
        # though MVP v0 only ever seeds one lane.
        root = _seed(tmp_path)
        lane = yaml.safe_load((root / "04-lane-graph.yml").read_text())["lanes"][0]
        assert set(lane) == {
            "id",
            "purpose",
            "tasks",
            "depends_on",
            "expected_files",
            "exclusive_files",
            "provides",
            "consumes",
            "verification_scope",
            "merge_policy",
        }

    def test_lane_content_fields_seed_empty(self, tmp_path: Path) -> None:
        # Content the Planner fills (tasks, files, scope…) starts empty.
        root = _seed(tmp_path)
        lane = yaml.safe_load((root / "04-lane-graph.yml").read_text())["lanes"][0]
        for key in (
            "tasks",
            "depends_on",
            "expected_files",
            "exclusive_files",
            "provides",
            "consumes",
            "verification_scope",
        ):
            assert lane[key] == [], key
        assert lane["purpose"] is None

    def test_merge_policy_has_section_7_5_fields(self, tmp_path: Path) -> None:
        root = _seed(tmp_path)
        mp = yaml.safe_load((root / "04-lane-graph.yml").read_text())["lanes"][0][
            "merge_policy"
        ]
        assert set(mp) == {
            "auto_merge",
            "allowed_mechanical_resolutions",
            "semantic_conflict_policy",
        }
        assert mp["semantic_conflict_policy"] == "human_triage"

    def test_lane_graph_is_valid_block_yaml(self, tmp_path: Path) -> None:
        # Reads like the §7.5 example for humans — no flow-style surprises.
        root = _seed(tmp_path)
        text = (root / "04-lane-graph.yml").read_text()
        assert "id: LANE-001" in text
        assert "frozen: false" in text
