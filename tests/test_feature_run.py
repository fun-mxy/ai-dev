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
