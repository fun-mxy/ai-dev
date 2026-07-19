"""Filesystem path resolution for the ``.ai-dev/`` runtime state (spec §6).

These are thin helpers so the rest of the package never re-spells the
``.ai-dev/features/<id>/...`` layout by hand. ``repo_root`` is the directory the
orchestrator was invoked in; everything canonical lives beneath it (§4.1).
"""

from __future__ import annotations

from pathlib import Path

AI_DEV_DIR = ".ai-dev"
FEATURES_DIR = "features"
AGENT_PROFILES_FILE = "agent-profiles.yml"
RUNS_DIR = "runs"


def ai_dev_root(repo_root: Path) -> Path:
    """``<repo_root>/.ai-dev``."""
    return repo_root / AI_DEV_DIR


def agent_profiles_path(repo_root: Path) -> Path:
    """``<repo_root>/.ai-dev/agent-profiles.yml`` - the §10.1 profile registry."""
    return ai_dev_root(repo_root) / AGENT_PROFILES_FILE


def features_dir(repo_root: Path) -> Path:
    """``<repo_root>/.ai-dev/features``."""
    return ai_dev_root(repo_root) / FEATURES_DIR


def feature_dir(repo_root: Path, feature_id: str) -> Path:
    """``<repo_root>/.ai-dev/features/<feature_id>``."""
    return features_dir(repo_root) / feature_id


def runs_dir(repo_root: Path, feature_id: str) -> Path:
    """``<repo_root>/.ai-dev/features/<feature_id>/runs`` (§6 skeleton, §12.1).

    One ``RUN-NNN`` directory per agent invocation lives beneath this; the v0.0
    ``create-feature-run`` seeds it empty, and the v0.1 ``prepare-run`` command
    fills it one run at a time.
    """
    return feature_dir(repo_root, feature_id) / RUNS_DIR


def run_dir(repo_root: Path, feature_id: str, run_id: str) -> Path:
    """``<repo_root>/.ai-dev/features/<feature_id>/runs/<run_id>`` (§12.1)."""
    return runs_dir(repo_root, feature_id) / run_id
