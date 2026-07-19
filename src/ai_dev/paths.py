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

# §12.1 run-directory subdirectories. Shared by ``run_prepare`` (creates them)
# and ``run_wrapper`` (writes capture artifacts / reads the workspace) so the
# on-disk layout has one source of truth.
INPUT_DIR = "input"
OUTPUT_DIR = "output"
WORKSPACE_DIR = "workspace"

# §13 output-contract filenames: the agent-written result (§13.1) and the
# wrapper-written metadata (§13.2). Centralised here so the writer
# (``run_wrapper``) and the reader (``validate``, ticket 04) share one source of
# truth for the on-disk contract names - previously the wrapper held
# ``metadata.json`` as a private constant and the validator re-spelled it.
RESULT_JSON = "result.json"
METADATA_JSON = "metadata.json"


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
