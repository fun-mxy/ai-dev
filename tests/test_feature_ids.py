"""feature_ids.next_feature_id — minimal FEATURE-NNN allocation (§5.2, ticket 01).

Ticket 03 will generalize this to all 12 stable-id types; here we only need the
feature-run id, allocated off the existing ``.ai-dev/features/`` directories.
"""

from __future__ import annotations

from pathlib import Path

from ai_dev.feature_ids import next_feature_id


class TestNextFeatureId:
    def test_empty_features_dir_yields_001(self, repo_root: Path) -> None:
        assert next_feature_id(repo_root) == "FEATURE-001"

    def test_increments_past_existing(self, repo_root: Path) -> None:
        (repo_root / ".ai-dev" / "features" / "FEATURE-001").mkdir(parents=True)
        assert next_feature_id(repo_root) == "FEATURE-002"

    def test_two_consecutive_allocations(self, repo_root: Path) -> None:
        # The defining ticket-01 scenario: 001 then 002.
        assert next_feature_id(repo_root) == "FEATURE-001"
        # Simulate 001 having been created on disk before the next allocation.
        (repo_root / ".ai-dev" / "features" / "FEATURE-001").mkdir(parents=True)
        assert next_feature_id(repo_root) == "FEATURE-002"

    def test_uses_max_not_count(self, repo_root: Path) -> None:
        # Gaps must not reset numbering: max wins, not length.
        features = repo_root / ".ai-dev" / "features"
        (features / "FEATURE-001").mkdir(parents=True)
        (features / "FEATURE-005").mkdir()
        assert next_feature_id(repo_root) == "FEATURE-006"

    def test_ignores_non_matching_entries(self, repo_root: Path) -> None:
        features = repo_root / ".ai-dev" / "features"
        (features / "FEATURE-002").mkdir(parents=True)
        (features / "notes.md").write_text("noise")
        (features / "draft").mkdir()
        # Only FEATURE-002 counts → next is 003, unaffected by the noise.
        assert next_feature_id(repo_root) == "FEATURE-003"

    def test_zero_pads_to_three(self, repo_root: Path) -> None:
        features = repo_root / ".ai-dev" / "features"
        (features / "FEATURE-999").mkdir(parents=True)
        assert next_feature_id(repo_root) == "FEATURE-1000"
