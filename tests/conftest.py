"""Shared pytest fixtures.

Tests never touch a real ``.ai-dev/`` - every feature run is created inside a
throwaway tmp directory that stands in for a repo root.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

import pytest

# The canonical cc-glm52 profile shape - §10.1 plus the §10.3 env_strip_pattern,
# matching the prototype ``prototype/adapter/agent-profiles.yml``. Shared by the
# profiles-module and CLI tests so the two cannot drift apart on what "a valid
# profile" looks like (a previous split copy had already dropped env_strip_pattern
# and the ANTHROPIC_BASE_URL extra_env entry).
#
# v0.5 ticket 03: carries ``role_defaults`` mapping every role to cc-glm52 so the
# many CLI/dry-run tests that omit ``--profile`` resolve it through the policy
# table (the new default path) rather than a hardcoded argparse default.
CC_GLM52_PROFILE_YAML = """\
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
def repo_root(tmp_path: Path) -> Path:
    """A clean directory that behaves like a repo root for ``.ai-dev`` writes."""
    return tmp_path


@pytest.fixture
def write_profiles() -> Callable[..., Path]:
    """Return a callable that writes an ``agent-profiles.yml`` registry.

    Defaults to the canonical cc-glm52 shape above; pass ``text`` to write a
    different registry. Shared so the profiles and CLI tests exercise one shape.
    """

    def _write(repo_root: Path, text: str = CC_GLM52_PROFILE_YAML) -> Path:
        path = repo_root / ".ai-dev" / "agent-profiles.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    return _write


# Env vars the cc-glm52 profile reads as its token source (§10.1). The dev shell
# that runs the prototype often has ANTHROPIC_AUTH_TOKEN set, so profile
# token-resolution tests delete these first to start from a known-empty state.
TOKEN_ENV_VARS = ("CC_GLM52_TOKEN", "ANTHROPIC_AUTH_TOKEN")


@pytest.fixture
def clean_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the cc-glm52 token env vars so a test controls token presence.

    Token values are secret (§10.2/invariant #11); this fixture only deletes
    names, it never reads or asserts on a value.
    """
    for var in TOKEN_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Throwaway git repo at ``tmp_path`` with an initial commit + identity.

    v0.7 lane worktree tests need a real git working tree to call
    ``git worktree add``. This fixture inits a repo, sets a commit identity,
    and makes a single README commit so the worktree machinery has a
    non-empty base ref. Mirrors the v0.7 ``tests/test_lane_run.py``
    ``git_repo`` fixture - now hoisted to conftest so v0.2 / v0.3 / v0.5
    / e2e tests can opt in by replacing ``repo_root`` with ``git_repo``
    in the test signature.
    """
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(tmp_path)],
        check=True,
        capture_output=True,
    )
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
