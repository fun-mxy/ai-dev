"""Canonical status writer — v0.0 minimal slice (ticket 01).

This module is the *only* place canonical status files are written (§4.3: models
never write canonical state; only deterministic code does). Ticket 01 writes just
the initial ``feature-status.yml`` for a freshly created feature run. Ticket 04
will extend this with the freeze operation and the ``lane-status.yml`` /
``task-status.yml`` writers; the initial-state writer here is the seed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

FEATURE_STATUS_FILE = "feature-status.yml"

# §18 gate order; a new feature starts at the front of the pipeline.
_INITIAL_GATE = "requirements_gate"
# §8.3 — the four artifacts that freezing toggles, all unfrozen at creation.
_FROZEN_ARTIFACTS = ("requirements", "design", "tasks", "lane_graph")


def _initial_feature_status(feature_id: str) -> dict[str, Any]:
    """Build the §8.3 initial feature-status document, in spec field order."""
    return {
        "feature": {
            "id": feature_id,
            "status": "planning",
            "frozen_artifacts": {name: False for name in _FROZEN_ARTIFACTS},
            "current_gate": _INITIAL_GATE,
            "final_verdict": None,
        }
    }


def write_initial_feature_status(status_dir: Path, feature_id: str) -> Path:
    """Write the initial ``feature-status.yml`` and return its path.

    ``status_dir`` is the feature run's ``status/`` directory. The file is
    dumped with sorted insertion order (spec field order) and block style so it
    reads identically to the §8.3 example for humans and machines alike.
    """
    status_dir.mkdir(parents=True, exist_ok=True)
    path = status_dir / FEATURE_STATUS_FILE
    with path.open("w") as f:
        yaml.safe_dump(
            _initial_feature_status(feature_id),
            f,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )
    return path
