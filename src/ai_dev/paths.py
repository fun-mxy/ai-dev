"""Filesystem path resolution for the ``.ai-dev/`` runtime state (spec §6).

These are thin helpers so the rest of the package never re-spells the
``.ai-dev/features/<id>/...`` layout by hand. ``repo_root`` is the directory the
orchestrator was invoked in; everything canonical lives beneath it (§4.1).
"""

from __future__ import annotations

from pathlib import Path

AI_DEV_DIR = ".ai-dev"
FEATURES_DIR = "features"


def ai_dev_root(repo_root: Path) -> Path:
    """``<repo_root>/.ai-dev``."""
    return repo_root / AI_DEV_DIR


def features_dir(repo_root: Path) -> Path:
    """``<repo_root>/.ai-dev/features``."""
    return ai_dev_root(repo_root) / FEATURES_DIR


def feature_dir(repo_root: Path, feature_id: str) -> Path:
    """``<repo_root>/.ai-dev/features/<feature_id>``."""
    return features_dir(repo_root) / feature_id
