"""Small JSON artifact helpers shared by deterministic writers/readers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


def read_json_object(path: Path) -> dict[str, Any] | None:
    """Return a JSON object from ``path``, or ``None`` if missing/invalid."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write a formatted JSON artifact, creating its parent directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
