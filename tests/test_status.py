"""status.write_initial_feature_status — initial feature-status.yml (§8.3, ticket 01).

Ticket 04 owns the full deterministic writer (freeze flips, lane/task status);
here we only assert the exact initial document a brand-new feature run gets.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ai_dev.status import write_initial_feature_status


def _load(status_dir: Path) -> dict:
    with (status_dir / "feature-status.yml").open() as f:
        return yaml.safe_load(f)


class TestWriteInitialFeatureStatus:
    def test_writes_initial_planning_state(self, tmp_path: Path) -> None:
        write_initial_feature_status(tmp_path, "FEATURE-001")

        doc = _load(tmp_path)
        assert doc["feature"]["id"] == "FEATURE-001"
        assert doc["feature"]["status"] == "planning"
        assert doc["feature"]["current_gate"] == "requirements_gate"
        assert doc["feature"]["final_verdict"] is None

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
