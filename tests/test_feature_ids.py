"""Stable ID allocation (spec §5.2).

Two allocation strategies live in ``ai_dev.feature_ids``:

* ``next_feature_id`` (ticket 01) derives ``FEATURE-NNN`` from the feature
  directories already on disk — tested in ``TestNextFeatureId``.
* ``allocate_id`` (ticket 03) hands out the twelve §5.2 artifact ids (REQ, AC,
  DES, TASK, RUN, REV, GAP, VER, ISSUE, DEC, CP, LANE) from a persisted
  per-type counter inside the feature run, auditing each allocation — tested in
  ``TestAllocateId``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ai_dev.audit import AUDIT_LOG_JSON, AUDIT_LOG_MD
from ai_dev.feature_ids import (
    ID_COUNTERS_FILE,
    ID_TYPES,
    allocate_id,
    next_feature_id,
)


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


class TestAllocateId:
    """feature_ids.allocate_id — persisted per-type counter for the §5.2 types.

    ``allocate_id`` is the ticket-03 stable-id allocator: every call bumps a
    per-type counter persisted in the feature run, so numbering is monotonic
    across process restarts, and every allocation is recorded by the ticket-02
    audit appender.
    """

    def test_all_twelve_types_are_supported(self, tmp_path: Path) -> None:
        # §5.2 lists exactly these twelve; each type's first allocation is -001.
        assert ID_TYPES == (
            "REQ", "AC", "DES", "TASK", "RUN", "REV", "GAP", "VER",
            "ISSUE", "DEC", "CP", "LANE",
        )
        for id_type in ID_TYPES:
            assert (
                allocate_id(tmp_path, id_type, timestamp="2026-07-19T10:00:00Z")
                == f"{id_type}-001"
            )

    def test_same_type_consecutive_allocations_increment(self, tmp_path: Path) -> None:
        assert allocate_id(tmp_path, "REQ", timestamp="t1") == "REQ-001"
        assert allocate_id(tmp_path, "REQ", timestamp="t2") == "REQ-002"
        assert allocate_id(tmp_path, "REQ", timestamp="t3") == "REQ-003"

    def test_defining_scenario_two_req_then_restart(self, tmp_path: Path) -> None:
        # Ticket-03 acceptance: two REQ → 001, 002; after a process "restart"
        # the next is 003, never a duplicate. The persisted on-disk counter —
        # not any in-memory state — is what the third allocation reads, so we
        # assert that high-water mark is on disk between the two processes.
        assert allocate_id(tmp_path, "REQ", timestamp="t1") == "REQ-001"
        assert allocate_id(tmp_path, "REQ", timestamp="t2") == "REQ-002"
        assert yaml.safe_load((tmp_path / ID_COUNTERS_FILE).read_text()) == {"REQ": 2}
        assert allocate_id(tmp_path, "REQ", timestamp="t3") == "REQ-003"

    def test_resumes_from_persisted_counter(self, tmp_path: Path) -> None:
        # Proves restart-safety concretely: a counter file written out-of-band
        # is honoured, so a fresh process picks up where the last one left off.
        (tmp_path / ID_COUNTERS_FILE).write_text("REQ: 5\n")
        assert allocate_id(tmp_path, "REQ", timestamp="t1") == "REQ-006"

    def test_types_allocate_independently(self, tmp_path: Path) -> None:
        assert allocate_id(tmp_path, "REQ", timestamp="t1") == "REQ-001"
        assert allocate_id(tmp_path, "AC", timestamp="t2") == "AC-001"
        assert allocate_id(tmp_path, "REQ", timestamp="t3") == "REQ-002"
        assert allocate_id(tmp_path, "AC", timestamp="t4") == "AC-002"

    def test_zero_pads_to_three_and_grows_past_999(self, tmp_path: Path) -> None:
        (tmp_path / ID_COUNTERS_FILE).write_text("REQ: 999\n")
        assert allocate_id(tmp_path, "REQ", timestamp="t1") == "REQ-1000"

    def test_unknown_type_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            allocate_id(tmp_path, "BOGUS", timestamp="t1")

    def test_persists_counter_file_monotonically(self, tmp_path: Path) -> None:
        allocate_id(tmp_path, "REQ", timestamp="t1")
        allocate_id(tmp_path, "REQ", timestamp="t2")
        allocate_id(tmp_path, "AC", timestamp="t3")
        data = yaml.safe_load((tmp_path / ID_COUNTERS_FILE).read_text())
        assert data == {"AC": 1, "REQ": 2}

    def test_each_allocation_appends_an_audit_record(self, tmp_path: Path) -> None:
        # Ticket-03: every allocation flows through the ticket-02 audit appender.
        allocate_id(tmp_path, "REQ", timestamp="2026-07-19T10:00:00Z")
        allocate_id(tmp_path, "AC", timestamp="2026-07-19T10:00:01Z")

        records = json.loads((tmp_path / AUDIT_LOG_JSON).read_text())
        assert [r["event"] for r in records] == ["allocate_id", "allocate_id"]
        assert records[0] == {
            "timestamp": "2026-07-19T10:00:00Z",
            "event": "allocate_id",
            "payload": {"id": "REQ-001", "type": "REQ", "seq": 1},
        }
        assert records[1]["payload"] == {"id": "AC-001", "type": "AC", "seq": 1}

    def test_allocation_also_appears_in_audit_markdown(self, tmp_path: Path) -> None:
        # §4.4 double product: the human-readable audit log carries it too.
        allocate_id(tmp_path, "REQ", timestamp="2026-07-19T10:00:00Z")
        md = (tmp_path / AUDIT_LOG_MD).read_text()
        assert "allocate_id" in md
        assert "REQ-001" in md
