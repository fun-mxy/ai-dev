"""feature_run.create_feature_run — the ticket-01 tracer bullet.

One call turns an intent string into a persisted feature run: it allocates the
FEATURE-NNN id, lays down the §6 directory skeleton, records the intent, writes
the initial canonical status, seeds the final-report placeholders, and appends a
``create`` audit record.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from ai_dev.audit import AUDIT_LOG_JSON, AUDIT_LOG_MD
from ai_dev.feature_run import create_feature_run

INTENT = "As a user I want to export reports so I can share them."


def _feature_path(root: Path, fid: str, *parts: str) -> Path:
    return root / ".ai-dev" / "features" / fid / Path(*parts)


class TestCreateFeatureRun:
    def test_allocates_001_then_002(self, repo_root: Path) -> None:
        first = create_feature_run(repo_root, INTENT)
        second = create_feature_run(repo_root, "a different intent")

        assert first == "FEATURE-001"
        assert second == "FEATURE-002"

    def test_creates_skeleton_directories(self, repo_root: Path) -> None:
        create_feature_run(repo_root, INTENT)

        for sub in ("status", "lanes", "runs", "issues", "decisions", "projections"):
            assert _feature_path(repo_root, "FEATURE-001", sub).is_dir(), sub

    def test_intent_recorded_verbatim(self, repo_root: Path) -> None:
        create_feature_run(repo_root, INTENT)

        text = _feature_path(repo_root, "FEATURE-001", "00-intent.md").read_text()
        assert INTENT in text
        assert "FEATURE-001" in text

    def test_initial_feature_status_written(self, repo_root: Path) -> None:
        create_feature_run(repo_root, INTENT)

        status_path = _feature_path(repo_root, "FEATURE-001", "status", "feature-status.yml")
        doc = yaml.safe_load(status_path.read_text())
        assert doc["feature"]["id"] == "FEATURE-001"
        assert doc["feature"]["status"] == "planning"
        assert doc["feature"]["current_gate"] == "requirements_gate"

    def test_final_report_placeholders_exist(self, repo_root: Path) -> None:
        create_feature_run(repo_root, INTENT)

        md = _feature_path(repo_root, "FEATURE-001", "final-report.md")
        js = _feature_path(repo_root, "FEATURE-001", "final-report.json")
        assert md.is_file()
        assert js.is_file()
        # JSON placeholder must still parse.
        json.loads(js.read_text())

    def test_audit_logs_create_event(self, repo_root: Path) -> None:
        create_feature_run(repo_root, INTENT)

        audit = _feature_path(repo_root, "FEATURE-001", AUDIT_LOG_MD).read_text()
        assert "create" in audit
        assert "FEATURE-001" in audit
        # §4.4 double product: the machine-readable mirror lands alongside.
        records = json.loads(
            _feature_path(repo_root, "FEATURE-001", AUDIT_LOG_JSON).read_text()
        )
        assert records[0]["event"] == "create"
        assert records[0]["payload"]["feature"] == "FEATURE-001"

    def test_second_run_gets_own_isolated_skeleton(self, repo_root: Path) -> None:
        create_feature_run(repo_root, INTENT)
        create_feature_run(repo_root, "second intent")

        # Each feature run owns a complete, independent skeleton.
        for fid in ("FEATURE-001", "FEATURE-002"):
            assert _feature_path(repo_root, fid, "00-intent.md").is_file()
            assert _feature_path(repo_root, fid, "status", "feature-status.yml").is_file()
            assert _feature_path(repo_root, fid, "audit.log.md").is_file()

    def test_intent_with_newlines_preserved(self, repo_root: Path) -> None:
        multi = "Line one.\nLine two.\n- a bullet"
        create_feature_run(repo_root, multi)
        text = _feature_path(repo_root, "FEATURE-001", "00-intent.md").read_text()
        assert multi in text

    def test_seeds_all_four_artifact_templates(self, repo_root: Path) -> None:
        # Ticket 05: create-time seeding of the §7 templates.
        create_feature_run(repo_root, INTENT)

        for name in (
            "01-requirements.md",
            "01-requirements.json",
            "02-design.md",
            "02-design.json",
            "03-tasks.md",
            "04-lane-graph.yml",
        ):
            assert _feature_path(repo_root, "FEATURE-001", name).is_file(), name

    def test_lane_graph_references_allocated_lane_id(self, repo_root: Path) -> None:
        # Ticket 05: the lane-graph's LANE-001 is the id allocated by ticket 03,
        # not a placeholder — the per-type counter records LANE: 1, and the audit
        # log carries the allocate_id event.
        create_feature_run(repo_root, INTENT)

        graph = yaml.safe_load(
            _feature_path(repo_root, "FEATURE-001", "04-lane-graph.yml").read_text()
        )
        assert graph["lanes"][0]["id"] == "LANE-001"

        counters = yaml.safe_load(
            _feature_path(repo_root, "FEATURE-001", "id-counters.yml").read_text()
        )
        assert counters == {"LANE": 1}

        records = json.loads(
            _feature_path(repo_root, "FEATURE-001", AUDIT_LOG_JSON).read_text()
        )
        # create precedes the lane allocation in audit order.
        assert [r["event"] for r in records] == ["create", "allocate_id"]
        assert records[1]["payload"] == {"id": "LANE-001", "type": "LANE", "seq": 1}


class TestCreateFeatureRunSeedsLaneAndTaskStatus:
    """Ticket 04: create_feature_run seeds the §8.2/§8.1 status files too.

    A freshly created feature run carries the full §6 ``status/`` set —
    ``feature-status.yml`` (ticket 01) plus ``lane-status.yml`` and
    ``task-status.yml`` (ticket 04). The lane-status references the *same*
    allocated ``LANE-001`` the lane-graph (ticket 05) uses, not a second id.
    """

    def test_status_dir_has_all_three_status_files(self, repo_root: Path) -> None:
        create_feature_run(repo_root, INTENT)

        status = _feature_path(repo_root, "FEATURE-001", "status")
        for name in ("feature-status.yml", "lane-status.yml", "task-status.yml"):
            assert (status / name).is_file(), name

    def test_lane_status_references_the_allocated_lane_id(
        self, repo_root: Path
    ) -> None:
        create_feature_run(repo_root, INTENT)

        doc = yaml.safe_load(
            _feature_path(repo_root, "FEATURE-001", "status", "lane-status.yml").read_text()
        )
        assert list(doc["lanes"]) == ["LANE-001"]
        assert doc["lanes"]["LANE-001"]["status"] == "pending"

    def test_lane_status_reuses_lane_graphs_lane_id_not_a_second_one(
        self, repo_root: Path
    ) -> None:
        # Both the lane-graph (ticket 05) and the lane-status (ticket 04) point
        # at the same single LANE-001 allocation; the counter was bumped once.
        create_feature_run(repo_root, INTENT)

        graph = yaml.safe_load(
            _feature_path(repo_root, "FEATURE-001", "04-lane-graph.yml").read_text()
        )["lanes"][0]["id"]
        lane_status = list(
            yaml.safe_load(
                _feature_path(
                    repo_root, "FEATURE-001", "status", "lane-status.yml"
                ).read_text()
            )["lanes"]
        )
        assert graph == "LANE-001"
        assert lane_status == ["LANE-001"]

        counters = yaml.safe_load(
            _feature_path(repo_root, "FEATURE-001", "id-counters.yml").read_text()
        )
        assert counters == {"LANE": 1}

    def test_task_status_starts_empty(self, repo_root: Path) -> None:
        create_feature_run(repo_root, INTENT)

        doc = yaml.safe_load(
            _feature_path(repo_root, "FEATURE-001", "status", "task-status.yml").read_text()
        )
        assert doc == {"tasks": {}}

    def test_lane_status_seeding_adds_no_audit_records(
        self, repo_root: Path
    ) -> None:
        # lane/task writers are pure (no audit); the audit order ticket 05
        # pins (create → allocate_id) is unchanged by ticket 04's seeding.
        create_feature_run(repo_root, INTENT)

        records = json.loads(
            _feature_path(repo_root, "FEATURE-001", AUDIT_LOG_JSON).read_text()
        )
        assert [r["event"] for r in records] == ["create", "allocate_id"]

    def test_each_feature_run_gets_their_own_lane_001(self, repo_root: Path) -> None:
        # Lane ids are scoped per feature run (§5.3 single-lane v0), so both
        # feature runs independently own a LANE-001 in their lane-status.
        create_feature_run(repo_root, INTENT)
        create_feature_run(repo_root, "second intent")

        for fid in ("FEATURE-001", "FEATURE-002"):
            doc = yaml.safe_load(
                _feature_path(repo_root, fid, "status", "lane-status.yml").read_text()
            )
            assert list(doc["lanes"]) == ["LANE-001"]


class TestCreateFeatureRunMultiLane:
    """v0.7 capstone: ``create_feature_run(..., lanes=N)`` seeds N lanes.

    The lane graph and lane-status both carry every allocated lane id so a
    Planner tasks proposal may assign tasks across them (ADR-0009 D1). The
    degenerate ``lanes=1`` (default) case stays byte-identical to the v0.6
    single-lane shape - multi-lane is an additive superset.
    """

    def test_default_lanes_is_one_single_lane_shape_unchanged(
        self, repo_root: Path
    ) -> None:
        create_feature_run(repo_root, INTENT)

        graph = yaml.safe_load(
            _feature_path(repo_root, "FEATURE-001", "04-lane-graph.yml").read_text()
        )
        assert [lane["id"] for lane in graph["lanes"]] == ["LANE-001"]
        lane_status = yaml.safe_load(
            _feature_path(
                repo_root, "FEATURE-001", "status", "lane-status.yml"
            ).read_text()
        )
        assert list(lane_status["lanes"]) == ["LANE-001"]
        counters = yaml.safe_load(
            _feature_path(repo_root, "FEATURE-001", "id-counters.yml").read_text()
        )
        assert counters == {"LANE": 1}

    def test_lanes_two_seeds_two_lane_entries_and_status_rows(
        self, repo_root: Path
    ) -> None:
        create_feature_run(repo_root, INTENT, lanes=2)

        graph = yaml.safe_load(
            _feature_path(repo_root, "FEATURE-001", "04-lane-graph.yml").read_text()
        )
        # Both lanes carry the full §7.5 entry shape; only the id differs.
        assert [lane["id"] for lane in graph["lanes"]] == ["LANE-001", "LANE-002"]
        for lane in graph["lanes"]:
            assert set(lane) >= {
                "id",
                "purpose",
                "tasks",
                "depends_on",
                "expected_files",
                "exclusive_files",
                "verification_scope",
                "merge_policy",
            }
            assert lane["purpose"] is None
            assert lane["tasks"] == []
        lane_status = yaml.safe_load(
            _feature_path(
                repo_root, "FEATURE-001", "status", "lane-status.yml"
            ).read_text()
        )
        assert list(lane_status["lanes"]) == ["LANE-001", "LANE-002"]
        assert lane_status["lanes"]["LANE-002"]["status"] == "pending"
        counters = yaml.safe_load(
            _feature_path(repo_root, "FEATURE-001", "id-counters.yml").read_text()
        )
        assert counters == {"LANE": 2}

    def test_lanes_two_allocates_both_lanes_in_audit_order(
        self, repo_root: Path
    ) -> None:
        create_feature_run(repo_root, INTENT, lanes=2)

        records = json.loads(
            _feature_path(repo_root, "FEATURE-001", AUDIT_LOG_JSON).read_text()
        )
        # create precedes the two lane allocations, in id order.
        assert [r["event"] for r in records] == [
            "create",
            "allocate_id",
            "allocate_id",
        ]
        assert records[1]["payload"] == {"id": "LANE-001", "type": "LANE", "seq": 1}
        assert records[2]["payload"] == {"id": "LANE-002", "type": "LANE", "seq": 2}

    def test_lanes_zero_or_negative_raises(self, repo_root: Path) -> None:
        import pytest

        for bad in (0, -1):
            with pytest.raises(ValueError):
                create_feature_run(repo_root, INTENT, lanes=bad)
