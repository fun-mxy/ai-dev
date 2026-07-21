"""``create-feature-run`` — the ticket-01 tracer bullet.

Turns an intent string into a persisted feature run under
``.ai-dev/features/<FEATURE-NNN>/``: allocates the id, lays down the §6
directory skeleton, records the intent, writes the initial canonical status,
seeds the final-report placeholders, and appends a ``create`` audit record.

This is deliberately a thin slice — it minimally touches directory generation,
id allocation, status, templates and audit (the five concerns tickets 02–05
each build out for real). Ticket 05 extends it to also seed the four §7
artifact templates: after the ``create`` record, the single MVP lane is
allocated through ticket 03's allocator (``LANE-001``) and the templates are
seeded with that id so the lane-graph references a real allocated id. Ticket 04
extends it once more: that same allocated lane id seeds the §8.2
``lane-status.yml`` (and the empty §8.1 ``task-status.yml``), so the full §6
``status/`` set is present at creation and the lane-status references the same
real id as the lane-graph.
"""

from __future__ import annotations

import json
from pathlib import Path

from ai_dev.audit import append_audit_event
from ai_dev.feature_ids import allocate_id, next_feature_id
from ai_dev.paths import feature_dir
from ai_dev.status import (
    write_initial_feature_status,
    write_initial_lane_status,
    write_initial_task_status,
)
from ai_dev.templates import seed_artifact_templates
from ai_dev.timeutil import utc_now_iso

# §6 skeleton subdirectories that start empty (ticket 01 lists them as "空的").
_EMPTY_SKELETON_DIRS = ("lanes", "runs", "issues", "decisions", "projections")


def _write_intent(feature_root: Path, feature_id: str, intent: str) -> None:
    """Record the verbatim user intent under the §7.1 ``原始需求`` slot.

    §7.1 also names 背景 / 业务目标 / 非目标 / 约束 / 初始假设; those facets are
    elaborated by the Planner during the requirements phase (§9.1, §18.1), not
    invented empty at run creation. At creation we only have the raw intent.
    """
    (feature_root / "00-intent.md").write_text(
        f"# Intent — {feature_id}\n"
        f"\n"
        f"Captured: {utc_now_iso()}\n"
        f"\n"
        f"## Original intent (原始需求)\n"
        f"\n"
        f"{intent}\n"
    )


def _write_final_report_placeholders(feature_root: Path, feature_id: str) -> None:
    """Seed ``final-report.md``/``.json`` placeholders (filled at the feature gate).

    The spec does not define ``final-report.json``'s schema (that lands with the
    §18.5 final-report work), so the placeholder only echoes the owning feature
    id — no speculative field names.
    """
    (feature_root / "final-report.md").write_text(
        f"# Final Report — {feature_id}\n"
        f"\n"
        f"_Pending: feature run not yet complete._\n"
    )
    payload = {"feature": feature_id}
    (feature_root / "final-report.json").write_text(json.dumps(payload, indent=2) + "\n")


def _seed_empty_dirs(feature_root: Path) -> None:
    for name in _EMPTY_SKELETON_DIRS:
        (feature_root / name).mkdir(parents=True, exist_ok=True)


def create_feature_run(repo_root: Path, intent: str, *, origin: str | None = None) -> str:
    """Create a new feature run for ``intent`` and return its ``FEATURE-NNN`` id.

    Lays down the §6 directory skeleton, records the intent, writes the initial
    canonical status and final-report placeholders, appends a ``create`` audit
    record, then allocates the single MVP lane (§5.3) and seeds the four §7
    artifact templates against it (ticket 05).

    Idempotent over re-invocation: each call allocates the next id from the
    directories already on disk, so consecutive calls produce FEATURE-001,
    FEATURE-002, …
    """
    feature_id = next_feature_id(repo_root)
    feature_root = feature_dir(repo_root, feature_id)
    feature_root.mkdir(parents=True, exist_ok=True)

    _seed_empty_dirs(feature_root)
    _write_intent(feature_root, feature_id, intent)
    write_initial_feature_status(feature_root / "status", feature_id)
    _write_final_report_placeholders(feature_root, feature_id)
    append_audit_event(
        feature_root,
        event="create",
        payload={"feature": feature_id},
        origin=origin,
    )
    # §5.3 MVP: every feature run owns exactly one lane. Allocate it through
    # ticket 03's allocator so the lane-graph references a real, persisted,
    # audited id rather than a placeholder string (ticket 05).
    lane_id = allocate_id(feature_root, "LANE", origin=origin)
    seed_artifact_templates(feature_root, feature_id, lane_id)
    # Ticket 04: seed the §8.2 lane-status with that same allocated lane id and
    # the empty §8.1 task-status, completing the §6 ``status/`` set. These are
    # pure writers (no audit) so the audit order is unchanged: create, allocate.
    write_initial_lane_status(feature_root / "status", lane_id)
    write_initial_task_status(feature_root / "status")
    return feature_id
