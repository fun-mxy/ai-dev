"""Stable ID allocation — v0.0 minimal slice (ticket 01).

Spec §5.2 defines twelve stable-id types. Ticket 01 only needs the feature-run
id (``FEATURE-NNN``), derived deterministically from the feature directories that
already exist on disk. Ticket 03 will generalize this to all types with a
persisted counter and per-type audit; the name and behaviour here are the seed.
"""

from __future__ import annotations

import re
from pathlib import Path

from ai_dev.paths import features_dir

_FEATURE_RE = re.compile(r"^FEATURE-(\d+)$")


def next_feature_id(repo_root: Path) -> str:
    """Return the next ``FEATURE-NNN`` id, one greater than the highest existing.

    Numbering is monotonic over the *maximum* seen number (not the count), so
    deleted or skipped ids never recycle. The numeric suffix is zero-padded to a
    minimum width of three (``FEATURE-001``) and grows naturally past 999.
    """
    directory = features_dir(repo_root)
    highest = 0
    if directory.is_dir():
        for entry in directory.iterdir():
            match = _FEATURE_RE.match(entry.name)
            if match and entry.is_dir():
                highest = max(highest, int(match.group(1)))
    return f"FEATURE-{highest + 1:03d}"
