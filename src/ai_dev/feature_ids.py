"""Stable ID allocation (spec §5.2).

Two allocation strategies live here:

* Feature-run ids ``FEATURE-NNN`` (ticket 01) are derived deterministically
  from the feature directories that already exist on disk — one directory per
  feature, so the next id is ``max(existing) + 1``. No counter file is kept.

* The twelve §5.2 artifact ids — ``REQ``, ``AC``, ``DES``, ``TASK``, ``RUN``,
  ``REV``, ``GAP``, ``VER``, ``ISSUE``, ``DEC``, ``CP``, ``LANE`` (ticket 03)
  — are allocated from a persisted per-type counter held in the feature run's
  ``id-counters.yml``. Each allocation bumps that type's high-water mark,
  rewrites the file, and appends an ``allocate_id`` audit record (ticket 02),
  so numbering is monotonic across process restarts and every assignment is
  traceable. The counter lives at the feature-run root alongside the audit
  ledger — it is deterministic canonical state (§4.3) but not gate/freeze
  status, so it stays out of ``status/``.

v0 is single-writer with no crash recovery (§23.3): the counter file is
rewritten in place on each allocation, mirroring the audit JSON rewrite, which
is safe under the v0 concurrency model. Within one allocation the counter is
bumped and flushed *before* the audit record is appended: a crash in that
window would leave an allocated-but-unaudited id rather than a duplicate — the
preferred failure mode, since a duplicate corrupts traceability while an
unaudited id is recoverable. Joint atomicity of the two writes is deferred past
v0.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from ai_dev.audit import append_audit_event
from ai_dev.paths import features_dir

# §5.2 stable-id types, in spec order. FEATURE is handled separately above — it
# is dir-derived, not counter-derived, and is not one of the twelve.
ID_TYPES: tuple[str, ...] = (
    "REQ",
    "AC",
    "DES",
    "TASK",
    "RUN",
    "REV",
    "GAP",
    "VER",
    "ISSUE",
    "DEC",
    "CP",
    "LANE",
)

# Persisted per-type high-water mark: ``{TYPE: highest_allocated_number}``.
ID_COUNTERS_FILE = "id-counters.yml"

_ALLOCATE_EVENT = "allocate_id"
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


def _read_counters(feature_root: Path) -> dict[str, int]:
    """Load the per-type high-water map, or start empty when no file yet.

    The file is single-writer deterministic output (YAML string keys, int
    values); ``int(v)`` just enforces the declared ``dict[str, int]`` contract
    rather than returning loosely-typed parsed YAML.
    """
    path = feature_root / ID_COUNTERS_FILE
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return {k: int(v) for k, v in data.items()}


def _write_counters(feature_root: Path, counters: dict[str, int]) -> None:
    """Rewrite the counter file with sorted keys for diff-stable output."""
    path = feature_root / ID_COUNTERS_FILE
    with path.open("w") as f:
        yaml.safe_dump(
            counters,
            f,
            sort_keys=True,
            default_flow_style=False,
            allow_unicode=True,
        )


def allocate_id(
    feature_root: Path,
    id_type: str,
    *,
    timestamp: str | None = None,
    origin: str | None = None,
) -> str:
    """Allocate, persist, and audit the next stable id of ``id_type``.

    Reads the per-type high-water mark from ``<feature_root>/id-counters.yml``,
    assigns one greater (starting at ``001`` for a type's first allocation),
    writes the bumped counter back, and appends an ``allocate_id`` audit event
    (ticket 02). Because the counter is read from and written back to disk on
    every call, numbering survives process restarts without gaps or duplicates.

    The numeric suffix is zero-padded to a minimum width of three and grows
    naturally past 999 (``REQ-1000``). Raises ``ValueError`` if ``id_type`` is
    not one of the §5.2 types (``ID_TYPES``).
    """
    if id_type not in ID_TYPES:
        raise ValueError(
            f"unknown stable-id type {id_type!r}; expected one of {ID_TYPES}"
        )

    counters = _read_counters(feature_root)
    seq = counters.get(id_type, 0) + 1
    counters[id_type] = seq
    _write_counters(feature_root, counters)

    allocated = f"{id_type}-{seq:03d}"
    append_audit_event(
        feature_root,
        event=_ALLOCATE_EVENT,
        payload={"id": allocated, "type": id_type, "seq": seq},
        timestamp=timestamp,
        origin=origin,
    )
    return allocated


def preview_next_id(feature_root: Path, id_type: str) -> str:
    """Return the id ``allocate_id`` *would* mint next, without minting it.

    The dry-run companion to :func:`allocate_id` (ADR-0004): it reads the
    per-type high-water mark and returns one greater (starting at ``001`` for a
    type's first allocation), but writes no counter and appends no audit record
    — the never-mint-a-stable-id invariant (glossary pin ``dry-run``) holds.
    Also used by the ``allocate-id`` helper's ``--dry-run`` to show the would-be
    id for a human-added item (ADR-0008 D4 direct-edit channel). Raises
    ``ValueError`` if ``id_type`` is not one of the §5.2 types, exactly as
    :func:`allocate_id` does.

    The returned id is a *preview*: a real allocation that happens between this
    call and a later ``allocate_id`` changes what the next id actually is, since
    the counter is read fresh on every allocation.
    """
    if id_type not in ID_TYPES:
        raise ValueError(
            f"unknown stable-id type {id_type!r}; expected one of {ID_TYPES}"
        )
    counters = _read_counters(feature_root)
    seq = counters.get(id_type, 0) + 1
    return f"{id_type}-{seq:03d}"
