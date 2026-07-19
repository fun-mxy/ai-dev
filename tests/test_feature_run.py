"""feature_run.create_feature_run — the ticket-01 tracer bullet.

One call turns an intent string into a persisted feature run: it allocates the
FEATURE-NNN id, lays down the §6 directory skeleton, records the intent, writes
the initial canonical status, seeds the final-report placeholders, and appends a
``create`` audit record.
"""

from __future__ import annotations

from pathlib import Path

import yaml

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
        import json

        json.loads(js.read_text())

    def test_audit_logs_create_event(self, repo_root: Path) -> None:
        create_feature_run(repo_root, INTENT)

        audit = _feature_path(repo_root, "FEATURE-001", "audit.log.md").read_text()
        assert "create" in audit
        assert "FEATURE-001" in audit

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
