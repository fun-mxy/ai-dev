"""Shared pytest fixtures.

Tests never touch a real ``.ai-dev/`` — every feature run is created inside a
throwaway tmp directory that stands in for a repo root.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """A clean directory that behaves like a repo root for ``.ai-dev`` writes."""
    return tmp_path
