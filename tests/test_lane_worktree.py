"""lane_worktree — v0.7 multi-lane worktree lifecycle primitive (ticket 02).

ADR-0009 D2/D3 makes lane worktrees the isolation primitive for v0.7: each
lane gets a dedicated git worktree + branch, recorded in ``worktree.json``,
with explicit create/keep/remove lifecycle operations. This module is the
deterministic engine behind those operations.

Tests here cover the public seams:

* ``lane_worktree_path`` — pure path derivation (no I/O)
* ``create_lane_worktree`` — precondition + happy path + lane-status hook
* ``is_worktree_clean`` — clean/dirty detection
* ``keep_lane_worktree`` — explicit retention
* ``remove_lane_worktree`` — safe removal (refuses dirty unless ``force``)
* ``load_lane_worktree`` — worktree.json read

Every test stands up a throwaway git repo at ``tmp_path`` (via the
``git_repo`` fixture) so nothing on the real filesystem is touched.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from ai_dev.audit import AUDIT_LOG_JSON
from ai_dev.feature_ids import allocate_id
from ai_dev.feature_run import create_feature_run
from ai_dev.json_artifact import read_json_object
from ai_dev.lane_worktree import (
    LANE_WORKTREE_FILE,
    WORKTREE_LIFECYCLE_ACTIVE,
    WORKTREE_LIFECYCLE_KEPT,
    WORKTREE_LIFECYCLE_REMOVED,
    create_lane_worktree,
    is_worktree_clean,
    keep_lane_worktree,
    lane_worktree_path,
    load_lane_worktree,
    remove_lane_worktree,
)
from ai_dev.status import LANE_STATUS_FILE, write_initial_lane_statuses
from ai_dev.templates import LANE_GRAPH_YML


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A throwaway git repo at ``tmp_path`` with one initial commit on ``main``.

    Worktree creation requires a real working tree (with ``.git/`` and at least
    one commit so the default branch exists), so tests init git here rather
    than depending on the host's checkout. The initial commit gives
    ``create_lane_worktree`` a base ref to branch from.
    """
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    # Local identity so commits don't blow up in CI / sandboxes that lack one.
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "t@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "tester"],
        check=True,
        capture_output=True,
    )
    (tmp_path / "README.md").write_text("init\n")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "README.md"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    return tmp_path


def _seed_two_lane_feature(repo_root: Path) -> tuple[str, str, str]:
    """Create a feature run with two registered lanes, return (feature, lane1, lane2)."""
    feature_id = create_feature_run(repo_root, "test intent")
    # Add a second lane so we can also test the multi-lane registry hooks.
    allocate_id(repo_root / ".ai-dev" / "features" / feature_id, "LANE")
    graph = yaml.safe_load(
        (repo_root / ".ai-dev" / "features" / feature_id / LANE_GRAPH_YML).read_text()
    )
    graph["lanes"].append(
        {
            "id": "LANE-002",
            "purpose": None,
            "tasks": [],
            "depends_on": [],
            "expected_files": [],
            "exclusive_files": [],
            "provides": [],
            "consumes": [],
            "verification_scope": [],
            "merge_policy": {
                "auto_merge": False,
                "allowed_mechanical_resolutions": [],
                "semantic_conflict_policy": "human_triage",
            },
        }
    )
    (repo_root / ".ai-dev" / "features" / feature_id / LANE_GRAPH_YML).write_text(
        yaml.safe_dump(graph, sort_keys=False)
    )
    write_initial_lane_statuses(
        repo_root / ".ai-dev" / "features" / feature_id / "status",
        ["LANE-001", "LANE-002"],
    )
    return feature_id, "LANE-001", "LANE-002"


def _lane_status_row(repo_root: Path, feature_id: str, lane_id: str) -> dict:
    doc = yaml.safe_load(
        (
            repo_root
            / ".ai-dev"
            / "features"
            / feature_id
            / "status"
            / LANE_STATUS_FILE
        ).read_text()
    )
    return doc["lanes"][lane_id]


def _audit_events(repo_root: Path, feature_id: str) -> list[dict]:
    return json.loads(
        (
            repo_root
            / ".ai-dev"
            / "features"
            / feature_id
            / AUDIT_LOG_JSON
        ).read_text()
    )


# ---------------------------------------------------------------------------
# lane_worktree_path — pure path derivation
# ---------------------------------------------------------------------------


class TestLaneWorktreePath:
    """The deterministic path under which a lane worktree lives."""

    def test_lives_under_dot_ai_dev_worktrees(self, tmp_path: Path) -> None:
        path = lane_worktree_path(tmp_path, "FEATURE-001", "LANE-001")
        assert path == tmp_path / ".ai-dev" / "worktrees" / "FEATURE-001" / "LANE-001"

    def test_deterministic_for_same_inputs(self, tmp_path: Path) -> None:
        a = lane_worktree_path(tmp_path, "FEATURE-007", "LANE-003")
        b = lane_worktree_path(tmp_path, "FEATURE-007", "LANE-003")
        assert a == b

    def test_per_lane_paths_are_distinct(self, tmp_path: Path) -> None:
        a = lane_worktree_path(tmp_path, "FEATURE-001", "LANE-001")
        b = lane_worktree_path(tmp_path, "FEATURE-001", "LANE-002")
        assert a != b

    def test_rejects_blank_lane_id(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="lane_id"):
            lane_worktree_path(tmp_path, "FEATURE-001", "")

    def test_rejects_blank_feature_id(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="feature_id"):
            lane_worktree_path(tmp_path, "", "LANE-001")


# ---------------------------------------------------------------------------
# create_lane_worktree
# ---------------------------------------------------------------------------


class TestCreateLaneWorktree:
    """The v0.7 minimal worktree engine (ADR-0009 D2/D3)."""

    def test_creates_branch_and_worktree_directory(self, git_repo: Path) -> None:
        feature_id, lane_id, _ = _seed_two_lane_feature(git_repo)

        wt_path = create_lane_worktree(
            git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="t"
        )

        assert wt_path.is_dir()
        assert (wt_path / "README.md").is_file()
        # The worktree is on its own branch.
        result = subprocess.run(
            ["git", "-C", str(wt_path), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == _expected_branch_name(feature_id, lane_id)

    def test_writes_worktree_json_with_required_fields(self, git_repo: Path) -> None:
        feature_id, lane_id, _ = _seed_two_lane_feature(git_repo)

        create_lane_worktree(
            git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="2026-07-25T00:00:00Z"
        )

        doc = read_json_object(
            git_repo / ".ai-dev" / "features" / feature_id / "lanes" / lane_id / LANE_WORKTREE_FILE
        )
        assert doc is not None
        assert doc["lane_id"] == lane_id
        assert doc["feature_id"] == feature_id
        assert doc["branch"] == _expected_branch_name(feature_id, lane_id)
        assert doc["base_ref"] == "HEAD"
        assert doc["path"] == str(lane_worktree_path(git_repo, feature_id, lane_id))
        assert doc["created_at"] == "2026-07-25T00:00:00Z"
        assert doc["updated_at"] == "2026-07-25T00:00:00Z"
        assert doc["lifecycle"] == WORKTREE_LIFECYCLE_ACTIVE
        assert doc["clean"] is True

    def test_uses_deterministic_branch_naming(self, git_repo: Path) -> None:
        feature_id, lane_id, _ = _seed_two_lane_feature(git_repo)
        create_lane_worktree(git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="t")
        doc = load_lane_worktree(git_repo / ".ai-dev" / "features" / feature_id, lane_id)
        assert doc is not None
        assert doc["branch"] == f"ai-dev/{feature_id}/{lane_id}"

    def test_updates_lane_status_worktree_pointer(self, git_repo: Path) -> None:
        feature_id, lane_id, _ = _seed_two_lane_feature(git_repo)

        create_lane_worktree(git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="t")

        row = _lane_status_row(git_repo, feature_id, lane_id)
        assert row["worktree"] == str(lane_worktree_path(git_repo, feature_id, lane_id))

    def test_does_not_touch_sibling_lane_status(self, git_repo: Path) -> None:
        feature_id, lane1, lane2 = _seed_two_lane_feature(git_repo)
        create_lane_worktree(git_repo, feature_id, lane1, base_ref="HEAD", timestamp="t")
        # lane2's status is untouched (still worktree: null).
        row = _lane_status_row(git_repo, feature_id, lane2)
        assert row["worktree"] is None

    def test_records_create_audit_event(self, git_repo: Path) -> None:
        feature_id, lane_id, _ = _seed_two_lane_feature(git_repo)
        create_lane_worktree(
            git_repo, feature_id, lane_id, base_ref="HEAD",
            timestamp="2026-07-25T01:00:00Z", origin="cli",
        )
        events = _audit_events(git_repo, feature_id)
        creates = [e for e in events if e["event"] == "lane_worktree_create"]
        assert len(creates) == 1
        rec = creates[0]
        assert rec["timestamp"] == "2026-07-25T01:00:00Z"
        assert rec["origin"] == "cli"
        assert rec["payload"]["lane"] == lane_id
        assert rec["payload"]["branch"] == f"ai-dev/{feature_id}/{lane_id}"
        assert rec["payload"]["path"] == str(lane_worktree_path(git_repo, feature_id, lane_id))
        assert rec["payload"]["base_ref"] == "HEAD"

    # --- preconditions ------------------------------------------------------

    def test_refuses_non_git_repo_root(self, tmp_path: Path) -> None:
        feature_id, lane_id, _ = _seed_two_lane_feature(tmp_path)
        with pytest.raises(ValueError, match="not a git"):
            create_lane_worktree(tmp_path, feature_id, lane_id, base_ref="HEAD", timestamp="t")

    def test_refuses_unknown_lane(self, git_repo: Path) -> None:
        feature_id, _, _ = _seed_two_lane_feature(git_repo)
        with pytest.raises(ValueError, match="LANE-999"):
            create_lane_worktree(git_repo, feature_id, "LANE-999", base_ref="HEAD", timestamp="t")

    def test_refuses_already_active_lane(self, git_repo: Path) -> None:
        feature_id, lane_id, _ = _seed_two_lane_feature(git_repo)
        create_lane_worktree(git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="t")
        with pytest.raises(ValueError, match="active"):
            create_lane_worktree(git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="t")

    def test_refuses_existing_branch(self, git_repo: Path) -> None:
        feature_id, lane_id, _ = _seed_two_lane_feature(git_repo)
        branch = _expected_branch_name(feature_id, lane_id)
        subprocess.run(
            ["git", "-C", str(git_repo), "branch", branch],
            check=True, capture_output=True,
        )
        with pytest.raises(ValueError, match="branch"):
            create_lane_worktree(git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="t")

    def test_refuses_unknown_base_ref(self, git_repo: Path) -> None:
        feature_id, lane_id, _ = _seed_two_lane_feature(git_repo)
        with pytest.raises(ValueError):
            create_lane_worktree(
                git_repo, feature_id, lane_id, base_ref="no-such-ref", timestamp="t"
            )

    def test_uses_explicit_base_ref_sha(self, git_repo: Path) -> None:
        feature_id, lane_id, _ = _seed_two_lane_feature(git_repo)
        head_sha = subprocess.run(
            ["git", "-C", str(git_repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        wt_path = create_lane_worktree(
            git_repo, feature_id, lane_id, base_ref=head_sha, timestamp="t"
        )
        assert wt_path.is_dir()
        # worktree.json records the literal base_ref the caller asked for.
        doc = load_lane_worktree(
            git_repo / ".ai-dev" / "features" / feature_id, lane_id
        )
        assert doc is not None
        assert doc["base_ref"] == head_sha


# ---------------------------------------------------------------------------
# is_worktree_clean
# ---------------------------------------------------------------------------


class TestIsWorktreeClean:
    """clean/dirty detection is the gate before destructive lifecycle ops."""

    def test_returns_true_on_freshly_created_worktree(self, git_repo: Path) -> None:
        feature_id, lane_id, _ = _seed_two_lane_feature(git_repo)
        wt_path = create_lane_worktree(
            git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="t"
        )
        assert is_worktree_clean(wt_path) is True

    def test_returns_false_when_uncommitted_change(self, git_repo: Path) -> None:
        feature_id, lane_id, _ = _seed_two_lane_feature(git_repo)
        wt_path = create_lane_worktree(
            git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="t"
        )
        (wt_path / "draft.txt").write_text("uncommitted\n")
        assert is_worktree_clean(wt_path) is False

    def test_returns_false_when_untracked_file_present(self, git_repo: Path) -> None:
        feature_id, lane_id, _ = _seed_two_lane_feature(git_repo)
        wt_path = create_lane_worktree(
            git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="t"
        )
        (wt_path / "scratch.txt").write_text("scratch\n")
        assert is_worktree_clean(wt_path) is False

    def test_refuses_non_worktree_path(self, git_repo: Path) -> None:
        # A path that is not a directory at all (and therefore not a
        # git worktree) — refuse loud rather than silently claim clean.
        missing = git_repo / "does-not-exist-anywhere"
        with pytest.raises(ValueError, match="not a directory"):
            is_worktree_clean(missing)


# ---------------------------------------------------------------------------
# remove_lane_worktree
# ---------------------------------------------------------------------------


class TestRemoveLaneWorktree:
    """Explicit removal; refuses dirty trees unless force=True."""

    def test_removes_clean_worktree_and_updates_metadata(self, git_repo: Path) -> None:
        feature_id, lane_id, _ = _seed_two_lane_feature(git_repo)
        wt_path = create_lane_worktree(
            git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="t"
        )
        assert wt_path.is_dir()

        remove_lane_worktree(
            git_repo, feature_id, lane_id, timestamp="2026-07-25T02:00:00Z", origin="cli"
        )

        assert not wt_path.exists()
        doc = load_lane_worktree(
            git_repo / ".ai-dev" / "features" / feature_id, lane_id
        )
        assert doc is not None
        assert doc["lifecycle"] == WORKTREE_LIFECYCLE_REMOVED
        assert doc["updated_at"] == "2026-07-25T02:00:00Z"

    def test_clears_lane_status_worktree_pointer(self, git_repo: Path) -> None:
        feature_id, lane_id, _ = _seed_two_lane_feature(git_repo)
        create_lane_worktree(git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="t")
        remove_lane_worktree(git_repo, feature_id, lane_id, timestamp="t")
        row = _lane_status_row(git_repo, feature_id, lane_id)
        assert row["worktree"] is None

    def test_does_not_touch_sibling_lane(self, git_repo: Path) -> None:
        feature_id, lane1, lane2 = _seed_two_lane_feature(git_repo)
        create_lane_worktree(git_repo, feature_id, lane1, base_ref="HEAD", timestamp="t")
        create_lane_worktree(git_repo, feature_id, lane2, base_ref="HEAD", timestamp="t2")
        remove_lane_worktree(git_repo, feature_id, lane1, timestamp="t3")
        # lane2's worktree stays put, lane2's worktree pointer remains set.
        row = _lane_status_row(git_repo, feature_id, lane2)
        assert row["worktree"] == str(
            lane_worktree_path(git_repo, feature_id, lane2)
        )

    def test_refuses_when_worktree_is_dirty(self, git_repo: Path) -> None:
        feature_id, lane_id, _ = _seed_two_lane_feature(git_repo)
        wt_path = create_lane_worktree(
            git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="t"
        )
        (wt_path / "draft.txt").write_text("dirty\n")

        with pytest.raises(ValueError, match="dirty"):
            remove_lane_worktree(git_repo, feature_id, lane_id, timestamp="t")

        # Refusing leaves the worktree on disk AND marks it not-removed.
        assert wt_path.is_dir()
        doc = load_lane_worktree(
            git_repo / ".ai-dev" / "features" / feature_id, lane_id
        )
        assert doc is not None
        assert doc["lifecycle"] == WORKTREE_LIFECYCLE_ACTIVE
        assert doc["clean"] is False

    def test_force_removes_dirty_worktree(self, git_repo: Path) -> None:
        feature_id, lane_id, _ = _seed_two_lane_feature(git_repo)
        wt_path = create_lane_worktree(
            git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="t"
        )
        (wt_path / "draft.txt").write_text("dirty\n")

        remove_lane_worktree(
            git_repo, feature_id, lane_id, force=True, timestamp="t"
        )

        assert not wt_path.exists()
        doc = load_lane_worktree(
            git_repo / ".ai-dev" / "features" / feature_id, lane_id
        )
        assert doc is not None
        assert doc["lifecycle"] == WORKTREE_LIFECYCLE_REMOVED

    def test_refuses_when_no_metadata(self, git_repo: Path) -> None:
        feature_id, lane_id, _ = _seed_two_lane_feature(git_repo)
        with pytest.raises(ValueError, match="no lane worktree"):
            remove_lane_worktree(git_repo, feature_id, lane_id, timestamp="t")

    def test_records_remove_audit_event(self, git_repo: Path) -> None:
        feature_id, lane_id, _ = _seed_two_lane_feature(git_repo)
        create_lane_worktree(git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="t")
        remove_lane_worktree(
            git_repo, feature_id, lane_id, timestamp="2026-07-25T03:00:00Z", origin="cli"
        )
        events = _audit_events(git_repo, feature_id)
        removes = [e for e in events if e["event"] == "lane_worktree_remove"]
        assert len(removes) == 1
        assert removes[0]["payload"]["force"] is False


# ---------------------------------------------------------------------------
# keep_lane_worktree
# ---------------------------------------------------------------------------


class TestKeepLaneWorktree:
    """``keep`` is the explicit retention signal — refuses silently-deleting
    dirty worktrees, but lets a human choose to keep a dirty one."""

    def test_marks_kept_status(self, git_repo: Path) -> None:
        feature_id, lane_id, _ = _seed_two_lane_feature(git_repo)
        create_lane_worktree(git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="t")
        keep_lane_worktree(
            git_repo, feature_id, lane_id, timestamp="2026-07-25T04:00:00Z", origin="cli"
        )
        doc = load_lane_worktree(
            git_repo / ".ai-dev" / "features" / feature_id, lane_id
        )
        assert doc is not None
        assert doc["lifecycle"] == WORKTREE_LIFECYCLE_KEPT
        assert doc["updated_at"] == "2026-07-25T04:00:00Z"

    def test_preserves_dirty_worktree(self, git_repo: Path) -> None:
        feature_id, lane_id, _ = _seed_two_lane_feature(git_repo)
        wt_path = create_lane_worktree(
            git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="t"
        )
        (wt_path / "draft.txt").write_text("dirty\n")
        keep_lane_worktree(git_repo, feature_id, lane_id, timestamp="t")
        # Keep does NOT remove the worktree — it just marks it for retention.
        assert wt_path.is_dir()

    def test_refuses_when_no_metadata(self, git_repo: Path) -> None:
        feature_id, lane_id, _ = _seed_two_lane_feature(git_repo)
        with pytest.raises(ValueError, match="no lane worktree"):
            keep_lane_worktree(git_repo, feature_id, lane_id, timestamp="t")

    def test_records_keep_audit_event(self, git_repo: Path) -> None:
        feature_id, lane_id, _ = _seed_two_lane_feature(git_repo)
        create_lane_worktree(git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="t")
        keep_lane_worktree(git_repo, feature_id, lane_id, timestamp="t", origin="cli")
        events = _audit_events(git_repo, feature_id)
        keeps = [e for e in events if e["event"] == "lane_worktree_keep"]
        assert len(keeps) == 1
        assert keeps[0]["origin"] == "cli"


# ---------------------------------------------------------------------------
# Lifecycle composition
# ---------------------------------------------------------------------------


class TestWorktreeLifecycleComposition:
    """Repeated create/run/keep/remove attempts must be loud, not silent."""

    def test_create_then_keep_then_remove_then_recreate(self, git_repo: Path) -> None:
        feature_id, lane_id, _ = _seed_two_lane_feature(git_repo)

        create_lane_worktree(git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="t1")
        # Second create on an active lane refuses.
        with pytest.raises(ValueError):
            create_lane_worktree(git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="t2")

        keep_lane_worktree(git_repo, feature_id, lane_id, timestamp="t3")
        # Re-creating a kept lane also refuses (not active but not absent either).
        with pytest.raises(ValueError):
            create_lane_worktree(git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="t4")

        remove_lane_worktree(git_repo, feature_id, lane_id, force=True, timestamp="t5")
        # After explicit removal, recreate works clean.
        create_lane_worktree(git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="t6")
        doc = load_lane_worktree(
            git_repo / ".ai-dev" / "features" / feature_id, lane_id
        )
        assert doc is not None
        assert doc["lifecycle"] == WORKTREE_LIFECYCLE_ACTIVE

    def test_create_then_force_remove_then_recreate(self, git_repo: Path) -> None:
        feature_id, lane_id, _ = _seed_two_lane_feature(git_repo)
        create_lane_worktree(git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="t1")
        remove_lane_worktree(git_repo, feature_id, lane_id, force=True, timestamp="t2")
        # Force-remove frees the path; the next create should succeed.
        create_lane_worktree(git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="t3")

    def test_recreate_after_keep_surfaces_lifecycle_message(self, git_repo: Path) -> None:
        """The kept lifecycle refuses re-create with a message that names
        ``lifecycle=kept`` rather than the misleading "unrelated worktree"
        error the on-disk path check would otherwise raise."""
        feature_id, lane_id, _ = _seed_two_lane_feature(git_repo)
        create_lane_worktree(git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="t1")
        keep_lane_worktree(git_repo, feature_id, lane_id, timestamp="t2")
        with pytest.raises(ValueError, match="lifecycle=kept"):
            create_lane_worktree(
                git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="t3"
            )

    def test_remove_after_remove_surfaces_consistent_error(self, git_repo: Path) -> None:
        """Removing an already-removed lane raises the same "no lane
        worktree record" error a missing record would, not the
        "not a directory" exception that ``is_worktree_clean`` would
        otherwise raise on a reaped path."""
        feature_id, lane_id, _ = _seed_two_lane_feature(git_repo)
        create_lane_worktree(git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="t1")
        remove_lane_worktree(git_repo, feature_id, lane_id, force=True, timestamp="t2")
        with pytest.raises(ValueError, match="no lane worktree"):
            remove_lane_worktree(git_repo, feature_id, lane_id, timestamp="t3")

    def test_keep_and_remove_refuse_unknown_lane(self, git_repo: Path) -> None:
        """The lane-registration precondition is symmetric: ``create``,
        ``keep``, and ``remove`` all refuse lanes that the lane graph
        does not declare (ADR-0009 D2 — lane registry owns the lane set)."""
        feature_id, _, _ = _seed_two_lane_feature(git_repo)
        with pytest.raises(ValueError, match="LANE-999"):
            keep_lane_worktree(git_repo, feature_id, "LANE-999", timestamp="t")
        with pytest.raises(ValueError, match="LANE-999"):
            remove_lane_worktree(git_repo, feature_id, "LANE-999", timestamp="t")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _expected_branch_name(feature_id: str, lane_id: str) -> str:
    """Mirror the deterministic branch scheme documented in ``lane_worktree``."""
    return f"ai-dev/{feature_id}/{lane_id}"
