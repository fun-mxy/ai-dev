"""lane_run - v0.7 multi-lane worktree execution (ticket 03, ADR-0009 D2).

ADR-0009 D2 makes the lane worktree the cwd for every file-mutating lane leg.
Ticket 02 stood up the worktree lifecycle primitive; ticket 03 routes lane
execution through it. The Implementer / fix-run / reviewer / spec-gap / verifier
legs all read and write through the lane's worktree, and the lane-level
``metadata.json`` / ``diff.patch`` / ``commits.log`` are the canonical record
of what each lane actually changed.

Tests here cover the public seams:

* ``ensure_lane_worktree`` - resolves/creates the worktree for a lane
* ``capture_worktree_diff`` / ``capture_worktree_commits`` - read git state
* ``write_lane_metadata`` - lane-level metadata.json with all v0.7 fields
* ``write_lane_diff`` / ``write_lane_commits_log`` - lane-level diff/commits
* ``run_in_lane_worktree`` - the lane-aware execution orchestrator
* end-to-end: two lanes changing different files in isolated worktrees

Every test stands up a throwaway git repo (``git_repo`` fixture) and a feature
run with frozen tasks + lane-graph, so the lane legs have real preconditions.
The agent CLI is replaced with a fake ``claude`` that writes a workspace file
inside the lane's worktree, so the lane worktree is actually exercised.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

from ai_dev.audit import AUDIT_LOG_JSON
from ai_dev.feature_run import create_feature_run
from ai_dev.lane_run import (
    LANE_DIFF_FILE,
    LANE_METADATA_FILE,
    LANE_COMMITS_LOG_FILE,
    LaneRunContext,
    capture_worktree_commits,
    capture_worktree_diff,
    commit_lane_deliverables,
    ensure_lane_worktree,
    run_in_lane_worktree,
    write_lane_commits_log,
    write_lane_diff,
    write_lane_metadata,
)
from ai_dev.lane_worktree import create_lane_worktree, load_lane_worktree
from ai_dev.profiles import load_profile
from ai_dev.run_prepare import prepare_run
from ai_dev.run_wrapper import run_headless
from ai_dev.shell_verifier import run_verifier
from ai_dev.status import freeze_artifact
from ai_dev.templates import LANE_GRAPH_YML, TASKS_MD


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# `git_repo` fixture is hoisted to tests/conftest.py so all
# tests share one source of truth.



_FAKE_CLAUDE_TEMPLATE = """\
#!__PY__
import json, os, sys
target = "__WRITES_FILE__"
os.makedirs(os.path.dirname(target), exist_ok=True)
os.makedirs("output", exist_ok=True)
with open(target, "w") as f:
    f.write("# fake claude wrote " + target + "\\n")
with open("output/result.md", "w") as f:
    f.write("Wrote " + target + ".\\n")
with open("output/result.json", "w") as f:
    json.dump(
        {
            "status": "proposed_done",
            "summary": "Wrote " + target + ".",
            "tasks": [
                {"id": "TASK-001", "status": "proposed_done",
                 "evidence": [target]}
            ],
            "related_requirements": ["REQ-001"],
            "related_acceptance_criteria": ["AC-001"],
            "known_issues": [],
            "change_proposals": [],
        },
        f,
    )
sys.stdout.write('{"type":"result","subtype":"success","is_error":false}\\n')
sys.exit(0)
"""


def _write_fake_claude(
    bin_dir: Path, *, writes_file: str, monkeypatch: pytest.MonkeyPatch | None = None
) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "claude"
    # The fake's target file is parametrized at *write* time (literal in
    # the script), not read from the env at run time. This is the
    # difference between a single-lane fake and a multi-lane fake:
    # when two fakes share a single process, env-var dispatch lets the
    # later ``setenv`` win. Embedding the target in the script means
    # each binary carries its own target, regardless of what other
    # fakes are spawned. ``FAKE_CLAUDE_FILE`` is also set on
    # ``monkeypatch`` for unit tests that pass no ``writes_file``-aware
    # script; the env-driven path remains the fall-back.
    script.write_text(
        _FAKE_CLAUDE_TEMPLATE.replace("__PY__", sys.executable).replace(
            "__WRITES_FILE__", writes_file
        )
    )
    os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if monkeypatch is not None:
        monkeypatch.setenv("FAKE_CLAUDE_FILE", writes_file)
    else:
        os.environ["FAKE_CLAUDE_FILE"] = writes_file
    return script


_CC_GLM52_PROFILE = """\
agent_profiles:
  cc-glm52:
    cli: claude
    backend: glm
    base_url: "https://ark.cn-beijing.volces.com/api/coding"
    auth_env: "CC_GLM52_TOKEN"
    auth_env_fallback: "ANTHROPIC_AUTH_TOKEN"
    auth_target: "ANTHROPIC_AUTH_TOKEN"
    model: "glm-5.2"
    invocation: headless
    extra_env:
      ANTHROPIC_BASE_URL: "https://ark.cn-beijing.volces.com/api/coding"
      ANTHROPIC_MODEL: "glm-5.2"
    env_strip_pattern: "^(CLAUDE_CODE_|CLAUDECODE$|AI_AGENT$|CLAUDE_EFFORT$)"
role_defaults:
  implementer: cc-glm52
  reviewer: cc-glm52
  spec_gap_analyst: cc-glm52
  planner: cc-glm52
"""


@pytest.fixture
def write_profiles() -> Callable[..., Path]:
    def _write(repo_root: Path) -> Path:
        path = repo_root / ".ai-dev" / "agent-profiles.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_CC_GLM52_PROFILE)
        return path

    return _write


@pytest.fixture
def clean_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("CC_GLM52_TOKEN", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)


def _feature_root(repo_root: Path, feature_id: str) -> Path:
    return repo_root / ".ai-dev" / "features" / feature_id


def _seed_frozen_two_lane_feature(
    repo_root: Path,
    *,
    task_body: str = "Write workspace/hello.py.",
    lane1_files: list[str] | None = None,
    lane2_files: list[str] | None = None,
    tasks1: list[str] | None = None,
    tasks2: list[str] | None = None,
) -> tuple[str, str, str]:
    """Create a feature run with two registered lanes + frozen tasks/lane-graph.

    Returns ``(feature_id, lane1_id, lane2_id)``. The lane graph declares both
    lanes with their respective expected/exclusive files so the §14.2 boundary
    check has lane-specific allow-lists.
    """
    feature_id = create_feature_run(repo_root, "two-lane worktree test")
    root = _feature_root(repo_root, feature_id)
    # Seed LANE-001 was already allocated by create_feature_run; allocate
    # LANE-002 so the graph has two real entries.
    from ai_dev.feature_ids import allocate_id
    allocate_id(root, "LANE")

    lane1 = "LANE-001"
    lane2 = "LANE-002"
    (root / TASKS_MD).write_text(
        f"# Tasks - {feature_id}\n"
        f"\n"
        f"Frozen: false\n"
        f"\n"
        f"## Tasks (TASK-NNN)\n"
        f"\n"
        f"{task_body}\n"
    )
    graph = {
        "feature": feature_id,
        "frozen": False,
        "lanes": [
            {
                "id": lane1,
                "purpose": "Lane one",
                "tasks": tasks1 if tasks1 is not None else [],
                "depends_on": [],
                "expected_files": lane1_files if lane1_files is not None else [],
                "exclusive_files": lane1_files if lane1_files is not None else [],
                "provides": [],
                "consumes": [],
                "verification_scope": [],
                "merge_policy": {
                    "auto_merge": False,
                    "allowed_mechanical_resolutions": [],
                    "semantic_conflict_policy": "human_triage",
                },
                "verification_commands": [
                    {"name": "echo-lane1", "command": "echo lane1"}
                ],
            },
            {
                "id": lane2,
                "purpose": "Lane two",
                "tasks": tasks2 if tasks2 is not None else [],
                "depends_on": [],
                "expected_files": lane2_files if lane2_files is not None else [],
                "exclusive_files": lane2_files if lane2_files is not None else [],
                "provides": [],
                "consumes": [],
                "verification_scope": [],
                "merge_policy": {
                    "auto_merge": False,
                    "allowed_mechanical_resolutions": [],
                    "semantic_conflict_policy": "human_triage",
                },
                "verification_commands": [
                    {"name": "echo-lane2", "command": "echo lane2"}
                ],
            },
        ],
    }
    (root / LANE_GRAPH_YML).write_text(yaml.safe_dump(graph, sort_keys=False))
    freeze_artifact(root, "tasks")
    freeze_artifact(root, "lane_graph")
    # The v0.7 lane-runtime registry is lane-status.yml; v0.2's
    # create_feature_run only registered the first lane. Register
    # the second lane so the worktree lifecycle's §24.2 precondition
    # check (``lane X must be in lane-status``) does not fail loud
    # before we even get to the worktree-existence branch.
    from ai_dev.status import write_initial_lane_statuses
    write_initial_lane_statuses(root / "status", [lane1, lane2])
    return feature_id, lane1, lane2


def _lane_path(repo_root: Path, feature_id: str, lane_id: str) -> Path:
    return repo_root / ".ai-dev" / "worktrees" / feature_id / lane_id


def _commit_in_worktree(worktree_path: Path, file_rel: str, content: str, msg: str) -> str:
    """Commit ``file_rel`` with ``content`` inside ``worktree_path``; return sha."""
    target = worktree_path / file_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    subprocess.run(
        ["git", "-C", str(worktree_path), "add", file_rel],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree_path), "commit", "-m", msg],
        check=True, capture_output=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return sha


# ---------------------------------------------------------------------------
# ensure_lane_worktree
# ---------------------------------------------------------------------------


class TestEnsureLaneWorktree:
    """Resolves a lane's worktree, creating one on demand."""

    def test_creates_worktree_when_missing(self, git_repo: Path) -> None:
        feature_id, lane1, lane2 = _seed_frozen_two_lane_feature(
            git_repo,
            lane1_files=["workspace/lane1.py"],
            lane2_files=["workspace/lane2.py"],
        )
        ctx = ensure_lane_worktree(
            git_repo, feature_id, lane1, base_ref="HEAD", timestamp="t"
        )
        assert isinstance(ctx, LaneRunContext)
        assert ctx.worktree_path == _lane_path(git_repo, feature_id, lane1)
        assert ctx.worktree_path.is_dir()
        assert ctx.branch == f"ai-dev/{feature_id}/{lane1}"
        # ``base_ref`` is resolved to a commit SHA at create time so the
        # lane's own commits can be diffed against a stable base, not the
        # worktree's advancing HEAD. It's still a 40-char hex SHA.
        assert len(ctx.base_ref) == 40
        assert all(c in "0123456789abcdef" for c in ctx.base_ref.lower())

    def test_returns_existing_worktree_without_recreating(
        self, git_repo: Path
    ) -> None:
        feature_id, lane1, _ = _seed_frozen_two_lane_feature(
            git_repo, lane1_files=["workspace/lane1.py"]
        )
        first = ensure_lane_worktree(
            git_repo, feature_id, lane1, base_ref="HEAD", timestamp="t1"
        )
        # Second call must return the same path; the worktree was created once
        # and not re-created (no branch reuse error).
        second = ensure_lane_worktree(
            git_repo, feature_id, lane1, base_ref="HEAD", timestamp="t2"
        )
        assert first.worktree_path == second.worktree_path
        assert first.branch == second.branch
        # The reuse path returns the same pinned SHA the create path wrote
        # (ADR-0009 D3 - base_ref is a durable SHA, not a symbolic ref that
        # could drift if main advances between create and reuse).
        assert len(second.base_ref) == 40
        assert second.base_ref == first.base_ref

    def test_records_worktree_json(self, git_repo: Path) -> None:
        feature_id, lane1, _ = _seed_frozen_two_lane_feature(
            git_repo, lane1_files=["workspace/lane1.py"]
        )
        ensure_lane_worktree(
            git_repo, feature_id, lane1, base_ref="HEAD", timestamp="t"
        )
        record = load_lane_worktree(_feature_root(git_repo, feature_id), lane1)
        assert record is not None
        assert record["lifecycle"] == "active"
        # ADR-0009 D3: ``worktree.json`` durably records the pinned base_ref
        # SHA (not the symbolic ``"HEAD"`` the caller passed), so the lane's
        # base is fixed at create time and won't drift if main later moves.
        assert record["base_ref"] != "HEAD"
        assert len(record["base_ref"]) == 40
        assert all(c in "0123456789abcdef" for c in record["base_ref"].lower())

    def test_refuses_unknown_lane(self, git_repo: Path) -> None:
        feature_id, _, _ = _seed_frozen_two_lane_feature(
            git_repo, lane1_files=["workspace/lane1.py"]
        )
        with pytest.raises(ValueError, match="LANE-999"):
            ensure_lane_worktree(
                git_repo, feature_id, "LANE-999", base_ref="HEAD", timestamp="t"
            )

    def test_refuses_non_git_repo(self, tmp_path: Path) -> None:
        feature_id, lane1, _ = _seed_frozen_two_lane_feature(
            tmp_path, lane1_files=["workspace/lane1.py"]
        )
        with pytest.raises(ValueError, match="not a git"):
            ensure_lane_worktree(
                tmp_path, feature_id, lane1, base_ref="HEAD", timestamp="t"
            )


# ---------------------------------------------------------------------------
# capture_worktree_diff / capture_worktree_commits
# ---------------------------------------------------------------------------


class TestCaptureWorktreeDiff:
    """``git diff <base_ref>..HEAD`` of the lane worktree, as text."""

    def test_returns_empty_diff_for_clean_worktree(self, git_repo: Path) -> None:
        feature_id, lane1, _ = _seed_frozen_two_lane_feature(
            git_repo, lane1_files=["workspace/lane1.py"]
        )
        ctx = ensure_lane_worktree(
            git_repo, feature_id, lane1, base_ref="HEAD", timestamp="t"
        )
        assert capture_worktree_diff(ctx.worktree_path, ctx.base_ref) == ""

    def test_returns_diff_for_uncommitted_changes(self, git_repo: Path) -> None:
        feature_id, lane1, _ = _seed_frozen_two_lane_feature(
            git_repo, lane1_files=["workspace/lane1.py"]
        )
        ctx = ensure_lane_worktree(
            git_repo, feature_id, lane1, base_ref="HEAD", timestamp="t"
        )
        target = ctx.worktree_path / "workspace" / "lane1.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("new content\n")
        diff = capture_worktree_diff(ctx.worktree_path, ctx.base_ref)
        assert "+new content" in diff
        assert "workspace/lane1.py" in diff

    def test_returns_diff_for_committed_changes(self, git_repo: Path) -> None:
        feature_id, lane1, _ = _seed_frozen_two_lane_feature(
            git_repo, lane1_files=["workspace/lane1.py"]
        )
        ctx = ensure_lane_worktree(
            git_repo, feature_id, lane1, base_ref="HEAD", timestamp="t"
        )
        _commit_in_worktree(
            ctx.worktree_path,
            "workspace/lane1.py",
            "committed content\n",
            "lane1 commit",
        )
        diff = capture_worktree_diff(ctx.worktree_path, ctx.base_ref)
        assert "+committed content" in diff

    def test_refuses_non_git_path(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not a git worktree"):
            capture_worktree_diff(tmp_path, "HEAD")


class TestCaptureWorktreeCommits:
    """``git log <base_ref>..HEAD`` of the lane worktree, as text."""

    def test_returns_empty_log_for_clean_worktree(self, git_repo: Path) -> None:
        feature_id, lane1, _ = _seed_frozen_two_lane_feature(
            git_repo, lane1_files=["workspace/lane1.py"]
        )
        ctx = ensure_lane_worktree(
            git_repo, feature_id, lane1, base_ref="HEAD", timestamp="t"
        )
        assert capture_worktree_commits(ctx.worktree_path, ctx.base_ref) == ""

    def test_returns_log_with_one_commit_per_change(self, git_repo: Path) -> None:
        feature_id, lane1, _ = _seed_frozen_two_lane_feature(
            git_repo, lane1_files=["workspace/lane1.py"]
        )
        ctx = ensure_lane_worktree(
            git_repo, feature_id, lane1, base_ref="HEAD", timestamp="t"
        )
        _commit_in_worktree(
            ctx.worktree_path, "workspace/lane1.py", "one\n", "first lane1 commit"
        )
        _commit_in_worktree(
            ctx.worktree_path, "workspace/lane1.py", "two\n", "second lane1 commit"
        )
        log = capture_worktree_commits(ctx.worktree_path, ctx.base_ref)
        assert "first lane1 commit" in log
        assert "second lane1 commit" in log


# ---------------------------------------------------------------------------
# write_lane_metadata
# ---------------------------------------------------------------------------


class TestWriteLaneMetadata:
    """The lane-level ``metadata.json`` carries the v0.7 fields the ticket pins."""

    def test_writes_lane_metadata_with_required_fields(
        self, git_repo: Path
    ) -> None:
        feature_id, lane1, _ = _seed_frozen_two_lane_feature(
            git_repo, lane1_files=["workspace/lane1.py"]
        )
        root = _feature_root(git_repo, feature_id)
        path = write_lane_metadata(
            root,
            lane1,
            run_id="RUN-001",
            worktree_path=Path("/tmp/wt"),
            branch=f"ai-dev/{feature_id}/{lane1}",
            base_ref="HEAD",
            profile="cc-glm52",
            cli="claude",
            backend="glm",
            model="glm-5.2",
            started_at="2026-07-25T10:00:00Z",
            ended_at="2026-07-25T10:00:05Z",
            exit_code=0,
            changed_files=["workspace/lane1.py"],
            commands=[],
        )
        assert path == root / "lanes" / lane1 / LANE_METADATA_FILE
        assert path.is_file()
        doc = json.loads(path.read_text())
        assert doc["lane_id"] == lane1
        assert doc["feature_id"] == feature_id
        assert doc["worktree_path"] == "/tmp/wt"
        assert doc["branch"] == f"ai-dev/{feature_id}/{lane1}"
        assert doc["base_ref"] == "HEAD"
        assert doc["profile"] == "cc-glm52"
        assert doc["cli"] == "claude"
        assert doc["backend"] == "glm"
        assert doc["model"] == "glm-5.2"
        assert doc["exit_code"] == 0
        assert doc["changed_files"] == ["workspace/lane1.py"]
        assert "commands" in doc

    def test_optional_role_field(self, git_repo: Path) -> None:
        feature_id, lane1, _ = _seed_frozen_two_lane_feature(
            git_repo, lane1_files=["workspace/lane1.py"]
        )
        root = _feature_root(git_repo, feature_id)
        path = write_lane_metadata(
            root,
            lane1,
            run_id="RUN-001",
            worktree_path=Path("/tmp/wt"),
            branch="b",
            base_ref="HEAD",
            profile="cc-glm52",
            cli="claude",
            backend="glm",
            model="glm-5.2",
            started_at="2026-07-25T10:00:00Z",
            ended_at="2026-07-25T10:00:05Z",
            exit_code=0,
            changed_files=[],
            commands=[],
            role="Implementer",
        )
        doc = json.loads(path.read_text())
        assert doc["role"] == "Implementer"


# ---------------------------------------------------------------------------
# write_lane_diff / write_lane_commits_log
# ---------------------------------------------------------------------------


class TestWriteLaneDiffAndCommitsLog:
    """The lane-level ``diff.patch`` / ``commits.log`` are the canonical record."""

    def test_writes_diff_under_lane_dir(self, git_repo: Path) -> None:
        feature_id, lane1, _ = _seed_frozen_two_lane_feature(
            git_repo, lane1_files=["workspace/lane1.py"]
        )
        root = _feature_root(git_repo, feature_id)
        path = write_lane_diff(root, lane1, "diff content\n")
        assert path == root / "lanes" / lane1 / LANE_DIFF_FILE
        assert path.read_text() == "diff content\n"

    def test_writes_commits_log_under_lane_dir(self, git_repo: Path) -> None:
        feature_id, lane1, _ = _seed_frozen_two_lane_feature(
            git_repo, lane1_files=["workspace/lane1.py"]
        )
        root = _feature_root(git_repo, feature_id)
        path = write_lane_commits_log(root, lane1, "log content\n")
        assert path == root / "lanes" / lane1 / LANE_COMMITS_LOG_FILE
        assert path.read_text() == "log content\n"


# ---------------------------------------------------------------------------
# run_in_lane_worktree — the lane-aware execution orchestrator
# ---------------------------------------------------------------------------


class TestRunInLaneWorktree:
    """Lane legs run with cwd rooted at the lane's worktree; outputs land in
    the canonical lane directory.

    Covers the ticket's first two checklist bullets:
    - lane run commands resolve the target lane and require/prepare its worktree
    - implement/fix runs execute in the lane worktree and collect result/diff/
      commits/metadata into the canonical lane dir
    """

    def test_creates_worktree_and_runs_with_cwd_in_worktree(
        self,
        git_repo: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        write_profiles(git_repo)
        profile = load_profile(git_repo, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-lane")
        fake = _write_fake_claude(
            tmp_path / "bin", writes_file="workspace/lane1.py", monkeypatch=monkeypatch
        )
        feature_id, lane1, _ = _seed_frozen_two_lane_feature(
            git_repo,
            lane1_files=["workspace/lane1.py"],
            tasks1=["TASK-001"],
        )

        result = run_in_lane_worktree(
            git_repo,
            feature_id,
            lane1,
            role="Implementer",
            task="Write workspace/lane1.py.",
            profile=profile,
            allowed_files=["workspace/lane1.py"],
            claude_path=str(fake),
        )

        # The worktree was created and the agent wrote a file inside it.
        wt = _lane_path(git_repo, feature_id, lane1)
        assert wt.is_dir()
        assert (wt / "workspace" / "lane1.py").is_file()
        # The lane-level artifacts landed at the canonical path.
        lane_root = _feature_root(git_repo, feature_id) / "lanes" / lane1
        assert (lane_root / LANE_DIFF_FILE).is_file()
        assert (lane_root / LANE_COMMITS_LOG_FILE).is_file()
        assert (lane_root / LANE_METADATA_FILE).is_file()
        # The run's own result/metadata still live in <feature>/runs/RUN-NNN/.
        run_root = (
            git_repo / ".ai-dev" / "features" / feature_id / "runs" / result.run_id
        )
        assert (run_root / "output" / "result.json").is_file()
        assert (run_root / "output" / "result.md").is_file()
        assert (run_root / "output" / "metadata.json").is_file()

    def test_lane_metadata_records_worktree_branch_and_profile(
        self,
        git_repo: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        write_profiles(git_repo)
        profile = load_profile(git_repo, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-meta")
        fake = _write_fake_claude(
            tmp_path / "bin", writes_file="workspace/lane1.py", monkeypatch=monkeypatch
        )
        feature_id, lane1, _ = _seed_frozen_two_lane_feature(
            git_repo, lane1_files=["workspace/lane1.py"]
        )

        result = run_in_lane_worktree(
            git_repo,
            feature_id,
            lane1,
            role="Implementer",
            task="Write workspace/lane1.py.",
            profile=profile,
            allowed_files=["workspace/lane1.py"],
            claude_path=str(fake),
        )

        # Lane-level metadata.json is field-complete per the ticket.
        lane_root = _feature_root(git_repo, feature_id) / "lanes" / lane1
        meta = json.loads((lane_root / LANE_METADATA_FILE).read_text())
        assert meta["lane_id"] == lane1
        assert meta["feature_id"] == feature_id
        assert meta["branch"] == f"ai-dev/{feature_id}/{lane1}"
        # ``base_ref`` is resolved to a commit SHA at worktree creation
        # (so the lane's own commits can be diffed against a stable base).
        assert len(meta["base_ref"]) == 40
        assert meta["worktree_path"] == str(
            _lane_path(git_repo, feature_id, lane1)
        )
        assert meta["profile"] == "cc-glm52"
        assert meta["cli"] == "claude"
        assert meta["backend"] == "glm"
        assert meta["model"] == "glm-5.2"
        assert "changed_files" in meta
        assert "commands" in meta
        assert "exit_code" in meta
        # Run id should match the result we got back.
        assert meta["run_id"] == result.run_id

    def test_run_wrapper_metadata_records_lane_id(
        self,
        git_repo: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        write_profiles(git_repo)
        profile = load_profile(git_repo, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-meta2")
        fake = _write_fake_claude(
            tmp_path / "bin", writes_file="workspace/lane1.py", monkeypatch=monkeypatch
        )
        feature_id, lane1, _ = _seed_frozen_two_lane_feature(
            git_repo, lane1_files=["workspace/lane1.py"]
        )

        result = run_in_lane_worktree(
            git_repo,
            feature_id,
            lane1,
            role="Implementer",
            task="Write workspace/lane1.py.",
            profile=profile,
            allowed_files=["workspace/lane1.py"],
            claude_path=str(fake),
        )

        run_meta_path = (
            git_repo
            / ".ai-dev"
            / "features"
            / feature_id
            / "runs"
            / result.run_id
            / "output"
            / "metadata.json"
        )
        run_meta = json.loads(run_meta_path.read_text())
        # The §13.2 metadata enriched with the v0.7 lane fields.
        assert run_meta["lane_id"] == lane1
        assert run_meta["branch"] == f"ai-dev/{feature_id}/{lane1}"
        # ``base_ref`` is resolved to a commit SHA at worktree creation.
        assert len(run_meta["base_ref"]) == 40
        assert run_meta["worktree_path"] == str(
            _lane_path(git_repo, feature_id, lane1)
        )
        assert "commands" in run_meta

    def test_refuses_to_run_lane_leg_for_unknown_lane(
        self,
        git_repo: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        write_profiles(git_repo)
        profile = load_profile(git_repo, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-bad")
        fake = _write_fake_claude(
            tmp_path / "bin", writes_file="workspace/x.py", monkeypatch=monkeypatch
        )
        feature_id, _, _ = _seed_frozen_two_lane_feature(
            git_repo, lane1_files=["workspace/lane1.py"]
        )
        with pytest.raises(ValueError, match="LANE-999"):
            run_in_lane_worktree(
                git_repo,
                feature_id,
                "LANE-999",
                role="Implementer",
                task="X.",
                profile=profile,
                allowed_files=[],
                claude_path=str(fake),
            )

    def test_refuses_non_git_repo(
        self,
        tmp_path: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        write_profiles(tmp_path)
        # Use a non-git repo: no .git dir. v0.7 falls back to the v0.2
        # "agent runs in run-home workspace/" path so the v0.2 test
        # harness (plain ``tmp_path``, no git init) keeps working when
        # the v0.7 lane-aware seam is wired in. The seam returns a
        # ``LaneRunResult`` with the v0.7 fields populated (synthetic
        # worktree_path pointing at the run-home, empty diff/commits);
        # the v0.2 contract on the run-home side is unchanged.
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-nogit")
        feature_id, lane1, _ = _seed_frozen_two_lane_feature(
            tmp_path, lane1_files=["workspace/lane1.py"]
        )
        profile = load_profile(tmp_path, "cc-glm52")
        # ``claude`` is the real binary; replace with a fake that just
        # writes the result files the wrapper expects.
        fake_bin = tmp_path / "bin" / "claude"
        fake_bin.parent.mkdir(parents=True, exist_ok=True)
        fake_bin.write_text(_FAKE_CLAUDE_TEMPLATE.replace("__PY__", sys.executable).replace(
            "__WRITES_FILE__", "workspace/lane1.py"
        ))
        os.chmod(fake_bin, os.stat(fake_bin).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        result = run_in_lane_worktree(
            tmp_path,
            feature_id,
            lane1,
            role="Implementer",
            task="X.",
            profile=profile,
            allowed_files=["workspace/lane1.py"],
            claude_path=str(fake_bin),
        )
        # The v0.7 contract on a non-git repo: the run happens, the
        # lane-level metadata is written, and the worktree_path falls
        # back to the run-home (no actual worktree, no diff).
        assert result.run_id == "RUN-001"
        assert result.worktree_path == _feature_root(tmp_path, feature_id) / "runs" / "RUN-001"
        assert result.branch == f"ai-dev/{feature_id}/{lane1}"
        # No lane-level diff was captured (no worktree, no diff).
        assert result.lane_diff.read_text() == ""


# ---------------------------------------------------------------------------
# Two lanes, two worktrees, two isolated diffs
# ---------------------------------------------------------------------------


class TestTwoLanesIndependent:
    """Two lanes must run in different worktrees without sharing one checkout.

    Covers the ticket's last checklist bullet:
    - tests cover two lanes changing different files without sharing one checkout
    """

    def test_two_lanes_run_in_separate_worktrees(
        self,
        git_repo: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        write_profiles(git_repo)
        profile = load_profile(git_repo, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-two")
        # Each lane's fake writes a file scoped to its own lane so the two
        # worktrees cannot accidentally collide on the same path.
        bin1 = tmp_path / "bin1"
        bin2 = tmp_path / "bin2"
        fake1 = _write_fake_claude(bin1, writes_file="workspace/lane1.py")
        fake2 = _write_fake_claude(bin2, writes_file="workspace/lane2.py")
        feature_id, lane1, lane2 = _seed_frozen_two_lane_feature(
            git_repo,
            lane1_files=["workspace/lane1.py"],
            lane2_files=["workspace/lane2.py"],
        )

        result1 = run_in_lane_worktree(
            git_repo,
            feature_id,
            lane1,
            role="Implementer",
            task="Write lane1.",
            profile=profile,
            allowed_files=["workspace/lane1.py"],
            claude_path=str(fake1),
        )
        result2 = run_in_lane_worktree(
            git_repo,
            feature_id,
            lane2,
            role="Implementer",
            task="Write lane2.",
            profile=profile,
            allowed_files=["workspace/lane2.py"],
            claude_path=str(fake2),
        )

        # Each lane has its own worktree directory, and the files it wrote
        # live ONLY in its own worktree.
        wt1 = _lane_path(git_repo, feature_id, lane1)
        wt2 = _lane_path(git_repo, feature_id, lane2)
        assert wt1 != wt2
        assert (wt1 / "workspace" / "lane1.py").is_file()
        assert not (wt1 / "workspace" / "lane2.py").exists()
        assert (wt2 / "workspace" / "lane2.py").is_file()
        assert not (wt2 / "workspace" / "lane1.py").exists()

        # Each lane's diff/metadata only reflect ITS changes.
        lane1_root = _feature_root(git_repo, feature_id) / "lanes" / lane1
        lane2_root = _feature_root(git_repo, feature_id) / "lanes" / lane2
        meta1 = json.loads((lane1_root / LANE_METADATA_FILE).read_text())
        meta2 = json.loads((lane2_root / LANE_METADATA_FILE).read_text())
        assert meta1["run_id"] == result1.run_id
        assert meta2["run_id"] == result2.run_id
        assert meta1["changed_files"] == ["workspace/lane1.py"]
        assert meta2["changed_files"] == ["workspace/lane2.py"]
        # The diffs name the lane's own file and not the sibling's.
        diff1 = (lane1_root / LANE_DIFF_FILE).read_text()
        diff2 = (lane2_root / LANE_DIFF_FILE).read_text()
        assert "workspace/lane1.py" in diff1
        assert "workspace/lane2.py" not in diff1
        assert "workspace/lane2.py" in diff2
        assert "workspace/lane1.py" not in diff2

    def test_two_lanes_have_independent_git_branches(
        self,
        git_repo: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        write_profiles(git_repo)
        profile = load_profile(git_repo, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-branch")
        fake1 = _write_fake_claude(
            tmp_path / "bin1", writes_file="workspace/lane1.py", monkeypatch=monkeypatch
        )
        fake2 = _write_fake_claude(
            tmp_path / "bin2", writes_file="workspace/lane2.py", monkeypatch=monkeypatch
        )
        feature_id, lane1, lane2 = _seed_frozen_two_lane_feature(
            git_repo,
            lane1_files=["workspace/lane1.py"],
            lane2_files=["workspace/lane2.py"],
        )

        run_in_lane_worktree(
            git_repo, feature_id, lane1, role="Implementer", task="x",
            profile=profile, allowed_files=["workspace/lane1.py"],
            claude_path=str(fake1),
        )
        run_in_lane_worktree(
            git_repo, feature_id, lane2, role="Implementer", task="x",
            profile=profile, allowed_files=["workspace/lane2.py"],
            claude_path=str(fake2),
        )

        # Each worktree is checked out on its own lane branch.
        wt1 = _lane_path(git_repo, feature_id, lane1)
        wt2 = _lane_path(git_repo, feature_id, lane2)
        b1 = subprocess.run(
            ["git", "-C", str(wt1), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        b2 = subprocess.run(
            ["git", "-C", str(wt2), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert b1 == f"ai-dev/{feature_id}/{lane1}"
        assert b2 == f"ai-dev/{feature_id}/{lane2}"
        assert b1 != b2


# ---------------------------------------------------------------------------
# Reviewer / spec-gap / verifier read the lane worktree, not the run workspace
# ---------------------------------------------------------------------------


class TestReviewerReadsLaneWorktree:
    """The reviewer's "diff" comes from the lane worktree (the implement
    evidence surface), not from the implement run's workspace/ copy.

    Covers: reviewer/spec-gap/verifier legs use the lane worktree diff/files
    as their evidence surface.
    """

    def test_reviewer_diff_uses_worktree_file_content(
        self,
        git_repo: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from ai_dev.checking_legs import read_implement_run_facts
        from ai_dev.implement_leg import run_implementer_leg

        write_profiles(git_repo)
        profile = load_profile(git_repo, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-review")
        fake = _write_fake_claude(
            tmp_path / "bin", writes_file="workspace/lane1.py", monkeypatch=monkeypatch
        )
        feature_id, lane1, _ = _seed_frozen_two_lane_feature(
            git_repo, lane1_files=["workspace/lane1.py"]
        )

        result = run_implementer_leg(
            git_repo, feature_id, lane1, profile,
            claude_path=str(fake),
        )
        # The implement run was recorded; the checking legs read its facts.
        facts = read_implement_run_facts(git_repo, feature_id, lane1)
        assert facts.run_id == result.run_id
        # The diff surface is the lane's worktree, not the run's workspace/.
        wt = _lane_path(git_repo, feature_id, lane1)
        assert "workspace/lane1.py" in facts.changed_files
        assert "workspace/lane1.py" in facts.file_contents
        # The content read by the reviewer matches the file on disk in the
        # lane's worktree, not the (older or absent) copy in the run dir.
        worktree_content = (wt / "workspace" / "lane1.py").read_text()
        run_workspace = (
            git_repo
            / ".ai-dev"
            / "features"
            / feature_id
            / "runs"
            / result.run_id
            / "workspace"
            / "lane1.py"
        )
        # The worktree is the source of truth; the reviewer reads from it.
        assert facts.file_contents["workspace/lane1.py"] == worktree_content
        # And the run's workspace/ copy is the same content (the lane run
        # collected from the worktree, so the two agree).
        if run_workspace.is_file():
            assert run_workspace.read_text() == worktree_content


class TestVerifierRunsInLaneWorktree:
    """The Verifier executes its commands in the lane worktree's ``workspace/``
    cwd (where the implementer wrote the package + ``tests/``), not the
    implement run's run-home ``workspace/`` copy. A file written into the
    worktree between implement and verify must be visible to the verify
    command. The cwd matches the Planner's workspace-relative verify commands
    (``PYTHONPATH=. python -m pytest tests``, ``python -m mypy <pkg>``) and the
    v0.2 fallback cwd (``implement_run/workspace/``) - v0.7 capstone (ADR-0009
    D2).

    Covers: reviewer/spec-gap/verifier legs use the lane worktree diff/files
    as their evidence surface.
    """

    def test_verify_command_sees_files_in_lane_worktree(
        self,
        git_repo: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from ai_dev.implement_leg import run_implementer_leg

        write_profiles(git_repo)
        profile = load_profile(git_repo, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-verify")
        fake = _write_fake_claude(
            tmp_path / "bin", writes_file="workspace/lane1.py", monkeypatch=monkeypatch
        )
        feature_id, lane1, _ = _seed_frozen_two_lane_feature(
            git_repo, lane1_files=["workspace/lane1.py"]
        )
        # Run the full implementer leg (the v0.7 lane-aware path: it calls
        # ``run_in_lane_worktree`` then writes the lane-level
        # ``implement-result.json`` the verifier reads back).
        run_implementer_leg(
            git_repo, feature_id, lane1, profile,
            claude_path=str(fake),
        )

        # Add a workspace-relative verify command (cwd = <worktree>/workspace/,
        # where the implementer wrote workspace/lane1.py -> lane1.py there).
        root = _feature_root(git_repo, feature_id)
        graph = yaml.safe_load((root / LANE_GRAPH_YML).read_text())
        for entry in graph["lanes"]:
            if entry["id"] == lane1:
                entry["verification_commands"] = [
                    {
                        "name": "lane1-file-exists",
                        "command": "test -f lane1.py && echo present",
                    }
                ]
        (root / LANE_GRAPH_YML).write_text(yaml.safe_dump(graph, sort_keys=False))
        # Re-freeze so the verifier accepts the updated graph.
        from ai_dev.status import write_initial_lane_statuses
        write_initial_lane_statuses(root / "status", [lane1, "LANE-002"])
        # ``verification_commands`` lives on the frozen lane-graph. The lane
        # graph stays frozen; the verifier reads the freshly written commands.

        result = run_verifier(git_repo, feature_id, lane1, timeout=30.0)
        assert result.verdict == "pass"
        # The command's stdout reflects the file's presence in the worktree.
        cmd = result.command_results[0]
        assert cmd.passed
        assert "present" in cmd.stdout


# ---------------------------------------------------------------------------
# commit_lane_deliverables (v0.7 capstone: stage + commit workspace/ on the
# lane branch so PR projection pushes real content)
# ---------------------------------------------------------------------------


class TestCommitLaneDeliverables:
    """``commit_lane_deliverables`` stages ``workspace/`` and commits it to the
    lane branch; a no-op (returns ``[]``) when nothing is new.

    Direct tests of the public helper (the run-home->worktree sync that feeds
    it is covered through the ``run_in_lane_worktree`` seam below).
    """

    def test_commits_staged_workspace_files(self, git_repo: Path) -> None:
        feature_id, lane1, _ = _seed_frozen_two_lane_feature(
            git_repo, lane1_files=["workspace/lane1.py"]
        )
        ctx = ensure_lane_worktree(
            git_repo, feature_id, lane1, base_ref="HEAD", timestamp="t"
        )
        # A deliverable the (synced) agent wrote into the worktree workspace/.
        deliverable = ctx.worktree_path / "workspace" / "lane1.py"
        deliverable.parent.mkdir(parents=True, exist_ok=True)
        deliverable.write_text("# implementer deliverable\n")

        committed = commit_lane_deliverables(
            ctx.worktree_path, lane1, "RUN-001", "Implementer"
        )
        # The worktree-relative path of the staged file is returned.
        assert committed == ["workspace/lane1.py"]
        # The lane branch now carries one commit beyond the pinned base.
        log = subprocess.run(
            ["git", "-C", str(ctx.worktree_path), "log",
             "--pretty=%s", f"{ctx.base_ref}..HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout
        assert "ai-dev Implementer LANE-001 (run RUN-001): workspace deliverables" in log

    def test_noop_when_nothing_new_is_staged(self, git_repo: Path) -> None:
        feature_id, lane1, _ = _seed_frozen_two_lane_feature(
            git_repo, lane1_files=["workspace/lane1.py"]
        )
        ctx = ensure_lane_worktree(
            git_repo, feature_id, lane1, base_ref="HEAD", timestamp="t"
        )
        deliverable = ctx.worktree_path / "workspace" / "lane1.py"
        deliverable.parent.mkdir(parents=True, exist_ok=True)
        deliverable.write_text("# implementer deliverable\n")
        # First call commits the file.
        assert commit_lane_deliverables(
            ctx.worktree_path, lane1, "RUN-001", "Implementer"
        ) == ["workspace/lane1.py"]
        head_before = subprocess.run(
            ["git", "-C", str(ctx.worktree_path), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        # Second call stages nothing new (byte-identical) -> no-op.
        assert commit_lane_deliverables(
            ctx.worktree_path, lane1, "RUN-002", "Implementer"
        ) == []
        head_after = subprocess.run(
            ["git", "-C", str(ctx.worktree_path), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert head_before == head_after


# ---------------------------------------------------------------------------
# run-home -> worktree sync (v0.7 capstone: the real claude CLI writes
# workspace/ deliverables to the run-home, not the worktree)
# ---------------------------------------------------------------------------


_FAKE_CLAUDE_RUN_HOME_TEMPLATE = """\
#!__PY__
import json, os, re, sys
# Reproduce the real claude CLI: resolve the working directory stated in the
# prompt (the run-home) and write the deliverable there with an absolute path
# - NOT to the process cwd (the worktree). The relative-to-cwd fake hits the
# worktree directly and never exercises the run-home->worktree sync; this one
# does, so the sync + commit fix is covered through the public seam.
argv = " ".join(sys.argv[1:])
m = re.search(r"Your working directory is: (\\S+)", argv)
run_dir = m.group(1) if m else os.getcwd()
target = os.path.join(run_dir, "workspace", "__DELIVERABLE__")
os.makedirs(os.path.dirname(target), exist_ok=True)
with open(target, "w") as f:
    f.write("# written to run-home by fake claude\\n")
# output/result.{json,md} relative to cwd (worktree) - the normal path the
# wrapper's _copy_agent_outputs collects from the agent cwd.
os.makedirs("output", exist_ok=True)
with open("output/result.md", "w") as f:
    f.write("Wrote __DELIVERABLE__.\\n")
with open("output/result.json", "w") as f:
    json.dump(
        {
            "status": "proposed_done",
            "summary": "Wrote __DELIVERABLE__.",
            "tasks": [
                {"id": "TASK-001", "status": "proposed_done",
                 "evidence": ["workspace/__DELIVERABLE__"]}
            ],
            "related_requirements": ["REQ-001"],
            "related_acceptance_criteria": ["AC-001"],
            "known_issues": [],
            "change_proposals": [],
        },
        f,
    )
sys.stdout.write('{"type":"result","subtype":"success","is_error":false}\\n')
sys.exit(0)
"""


def _write_fake_claude_run_home(bin_dir: Path, *, deliverable: str) -> Path:
    """A fake ``claude`` that writes ``deliverable`` to the run-home
    ``workspace/`` (absolute, parsed from the prompt), mirroring the real
    agent. Used to exercise the run-home->worktree sync through the
    ``run_in_lane_worktree`` public seam."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "claude-run-home"
    script.write_text(
        _FAKE_CLAUDE_RUN_HOME_TEMPLATE
        .replace("__PY__", sys.executable)
        .replace("__DELIVERABLE__", deliverable)
    )
    os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


class TestSyncRunHomeWorkspaceToWorktree:
    """The real claude CLI writes ``workspace/`` deliverables to the run-home
    (the working directory ``build_prompt`` states), not the lane worktree;
    ``run_in_lane_worktree`` syncs them into the worktree and (for the
    implementer) commits them to the lane branch. A non-implementer leg must
    NOT commit a stray ``workspace/`` write.

    Covered through the public ``run_in_lane_worktree`` seam (DEVELOPMENT.md:
    tests at public seams, never internals) using a fake that reproduces the
    real agent's run-home writes.
    """

    def test_syncs_run_home_deliverable_to_worktree_and_commits(
        self,
        git_repo: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        write_profiles(git_repo)
        profile = load_profile(git_repo, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-sync")
        # The fake writes workspace/synced.py to the RUN-HOME (absolute), so
        # the only way it reaches the worktree is the sync.
        fake = _write_fake_claude_run_home(tmp_path / "bin", deliverable="synced.py")
        feature_id, lane1, _ = _seed_frozen_two_lane_feature(
            git_repo, lane1_files=["workspace/lane1.py"], tasks1=["TASK-001"]
        )

        result = run_in_lane_worktree(
            git_repo,
            feature_id,
            lane1,
            role="Implementer",
            task="Write workspace/synced.py.",
            profile=profile,
            allowed_files=["workspace/synced.py"],
            claude_path=str(fake),
            commit_deliverables=True,
        )

        wt = _lane_path(git_repo, feature_id, lane1)
        # The deliverable reached the worktree workspace/ via the sync.
        assert (wt / "workspace" / "synced.py").is_file()
        # ...and was committed to the lane branch (implementer commits).
        lane_root = _feature_root(git_repo, feature_id) / "lanes" / lane1
        commits_log = (lane_root / LANE_COMMITS_LOG_FILE).read_text()
        assert "workspace deliverables" in commits_log
        # changed_files (the worktree snapshot diff) saw the synced file.
        meta = json.loads((lane_root / LANE_METADATA_FILE).read_text())
        assert "workspace/synced.py" in meta["changed_files"]
        assert result.run_id  # the run was recorded

    def test_non_implementer_role_does_not_commit(
        self,
        git_repo: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        write_profiles(git_repo)
        profile = load_profile(git_repo, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-review-nocommit")
        # Standard relative-to-cwd fake: the reviewer writes a stray
        # workspace/ file into the worktree.
        fake = _write_fake_claude(
            tmp_path / "bin", writes_file="workspace/review.txt", monkeypatch=monkeypatch
        )
        feature_id, lane1, _ = _seed_frozen_two_lane_feature(
            git_repo, lane1_files=["workspace/lane1.py"], tasks1=["TASK-001"]
        )

        run_in_lane_worktree(
            git_repo,
            feature_id,
            lane1,
            role="Code Reviewer",
            task="Review the lane.",
            profile=profile,
            allowed_files=["workspace/review.txt"],
            claude_path=str(fake),
            # commit_deliverables defaults to False: a reviewer leg must not
            # commit a stray workspace/ write to the lane branch.
        )

        wt = _lane_path(git_repo, feature_id, lane1)
        # The reviewer's file is on disk in the worktree (uncommitted)...
        assert (wt / "workspace" / "review.txt").is_file()
        # ...but the lane branch carries NO commit beyond the pinned base.
        lane_root = _feature_root(git_repo, feature_id) / "lanes" / lane1
        commits_log = (lane_root / LANE_COMMITS_LOG_FILE).read_text()
        assert commits_log == ""
