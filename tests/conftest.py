"""Shared pytest fixtures.

Tests never touch a real ``.ai-dev/`` - every feature run is created inside a
throwaway tmp directory that stands in for a repo root.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

# The canonical cc-glm52 profile shape - §10.1 plus the §10.3 env_strip_pattern,
# matching the prototype ``prototype/adapter/agent-profiles.yml``. Shared by the
# profiles-module and CLI tests so the two cannot drift apart on what "a valid
# profile" looks like (a previous split copy had already dropped env_strip_pattern
# and the ANTHROPIC_BASE_URL extra_env entry).
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
