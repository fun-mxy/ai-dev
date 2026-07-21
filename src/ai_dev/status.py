"""Canonical status writer — v0.0 (tickets 01 + 04).

This module is the *only* place canonical status files are written (§4.3: models
never write canonical state; only deterministic code does). Ticket 01 wrote the
initial ``feature-status.yml``; ticket 04 extends the writer with:

* the freeze operation (§4.2) — flips a ``frozen_artifacts`` flag ``false → true``
  monotonically, audited, and rejects re-freezing an already-frozen artifact;
* ``set_current_gate`` — moves ``current_gate`` to a known §18 gate, audited;
* the minimal §8.2 ``lane-status.yml`` (single lane) and §8.1 ``task-status.yml``
  (empty) writers, seeded at feature-run creation.

Two scopes, by design:

* The **initial writers** (``write_initial_feature_status`` /
  ``write_initial_lane_status`` / ``write_initial_task_status``) take the
  ``status/`` directory — they write one status file and emit no audit record.
* The **mutating operations** (``freeze_artifact``, ``set_current_gate``) take
  the **feature-run root**, because they both rewrite ``status/feature-status.yml``
  *and* append to the run-level audit log at the feature root — the same scope
  as the ticket-03 ``allocate_id`` allocator. ``status_dir = feature_root/"status"``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import yaml

from ai_dev.audit import append_audit_event

FEATURE_STATUS_FILE = "feature-status.yml"
LANE_STATUS_FILE = "lane-status.yml"
TASK_STATUS_FILE = "task-status.yml"

# §18.1–§18.5 gates in pipeline order; a new feature starts at the head.
GATES: tuple[str, ...] = (
    "requirements_gate",
    "design_gate",
    "task_gate",
    "lane_gate",
    "feature_coherence_gate",
)
_INITIAL_GATE = GATES[0]
# §8.3 — the four artifacts that freezing toggles, all unfrozen at creation.
# Public so the CLI's argparse ``choices`` (and future callers) share one source
# of truth for the canonical artifact names.
FROZEN_ARTIFACTS: tuple[str, ...] = ("requirements", "design", "tasks", "lane_graph")

_FREEZE_EVENT = "freeze"
_ADVANCE_GATE_EVENT = "advance_gate"
_MARK_TASK_PROPOSED_DONE_EVENT = "mark_task_proposed_done"


class FrozenArtifactError(ValueError):
    """An already-frozen artifact was written again (§4.2 monotonic freeze).

    Freezing is one-way: once an artifact's ``frozen_artifacts`` flag is true,
    the only sanctioned path to change the underlying artifact is a Change
    Proposal (§4.2) — not a second call to this writer. Subclasses ``ValueError``
    so callers may catch either.
    """


def _dump_yaml(path: Path, doc: dict[str, Any]) -> None:
    """Dump ``doc`` to ``path`` with stable, human-friendly formatting.

    Sorted insertion order (spec field order) + block style, so the file reads
    identically for humans and machines across read-modify-write cycles.
    """
    with path.open("w") as f:
        yaml.safe_dump(
            doc,
            f,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )


def _initial_feature_status(feature_id: str) -> dict[str, Any]:
    """Build the §8.3 initial feature-status document, in spec field order."""
    return {
        "feature": {
            "id": feature_id,
            "status": "planning",
            "frozen_artifacts": {name: False for name in FROZEN_ARTIFACTS},
            "current_gate": _INITIAL_GATE,
            "verdict": None,
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
    _dump_yaml(path, _initial_feature_status(feature_id))
    return path


def _feature_status_path(feature_root: Path) -> Path:
    return feature_root / "status" / FEATURE_STATUS_FILE


def _load_feature_status(feature_root: Path) -> dict[str, Any]:
    """Load and parse the feature-status document from the feature run root."""
    return yaml.safe_load(_feature_status_path(feature_root).read_text())


def frozen_artifacts_status(feature_root: Path) -> Mapping[str, bool]:
    """Return the ``frozen_artifacts`` map read from ``feature-status.yml``.

    Maps each §4.2 artifact name (``requirements`` / ``design`` / ``tasks`` /
    ``lane_graph``) to its frozen bool - the read-side complement to
    ``freeze_artifact``. Used by the §14.3 frozen-artifact validator (ticket 04)
    to decide whether touching a given artifact is a violation: only *frozen*
    artifacts are protected (§14.3: "如果这些 artifact 已冻结，则任何直接修改都失败").

    Fails loud (§24.2) if the status file is missing or malformed: a real
    feature run always has a valid status file (``create_feature_run`` writes
    it, ``freeze_artifact`` mutates it deterministically), so an unreadable file
    is genuine corruption the caller must surface - not silently treat as
    "nothing frozen", which could hide a broken run. This mirrors the sibling
    ``_load_feature_status`` (which raises via ``read_text``) and preserves the
    §14.3 no-false-positive guarantee: the frozen check never *asserts* a
    violation it cannot verify - it refuses to run rather than guessing.
    """
    path = _feature_status_path(feature_root)
    if not path.is_file():
        raise ValueError(
            f"feature-status.yml missing at {path} (broken feature run, §24.2)"
        )
    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ValueError(
            f"feature-status.yml at {path} is not valid YAML: {exc} (§24.2)"
        ) from exc
    if not isinstance(doc, dict):
        raise ValueError(
            f"feature-status.yml at {path} is not a mapping (§24.2)"
        )
    feature = doc.get("feature")
    if not isinstance(feature, dict):
        raise ValueError(
            f"feature-status.yml at {path} has no 'feature' mapping (§24.2)"
        )
    frozen = feature.get("frozen_artifacts")
    if not isinstance(frozen, dict):
        raise ValueError(
            f"feature-status.yml at {path} has no 'frozen_artifacts' mapping (§24.2)"
        )
    return frozen


def _mutate_feature_status(
    feature_root: Path,
    mutate: Any,
    *,
    event: str,
    payload: Mapping[str, Any],
    timestamp: str | None,
) -> None:
    """Shared load → mutate → dump → audit spine for feature-status edits.

    ``mutate`` receives the parsed ``feature`` mapping and applies the edit in
    place; if it raises, the file is *not* rewritten and no audit record is
    appended — so a rejected mutation (e.g. re-freezing) leaves state untouched
    by construction, not just by convention. On a clean mutation the document is
    flushed and the ``event``/``payload`` audit record appended.
    """
    doc = _load_feature_status(feature_root)
    mutate(doc["feature"])
    _dump_yaml(_feature_status_path(feature_root), doc)
    append_audit_event(
        feature_root, event=event, payload=payload, timestamp=timestamp
    )


def freeze_artifact(
    feature_root: Path, artifact: str, *, timestamp: str | None = None
) -> None:
    """Flip ``artifact``'s frozen flag ``false → true`` and audit it (§4.2).

    Reads ``status/feature-status.yml``, sets
    ``frozen_artifacts[artifact] = True``, writes it back, and appends a
    ``freeze`` audit record. Freeze is monotonic: if the artifact is already
    frozen this raises ``FrozenArtifactError`` (a ``ValueError``) — re-freezing
    is not an idempotent no-op, it is rejected, because the frozen flag must
    never be cleared or re-set by this writer (only a Change Proposal may change
    a frozen artifact). ``ValueError`` is raised for an unknown artifact name.
    """
    if artifact not in FROZEN_ARTIFACTS:
        raise ValueError(
            f"unknown frozen artifact {artifact!r}; expected one of {FROZEN_ARTIFACTS}"
        )

    def _flip(feature: dict[str, Any]) -> None:
        if feature["frozen_artifacts"][artifact]:
            raise FrozenArtifactError(
                f"artifact {artifact!r} is already frozen; use a Change Proposal "
                f"to change it (§4.2)"
            )
        feature["frozen_artifacts"][artifact] = True

    _mutate_feature_status(
        feature_root,
        _flip,
        event=_FREEZE_EVENT,
        payload={"artifact": artifact, "frozen": True},
        timestamp=timestamp,
    )


def set_current_gate(
    feature_root: Path, gate: str, *, timestamp: str | None = None
) -> None:
    """Move ``current_gate`` to a known §18 gate and audit the advance.

    Validates ``gate`` against ``GATES`` (raises ``ValueError`` otherwise), then
    rewrites ``status/feature-status.yml`` and appends an ``advance_gate`` audit
    record. This is a low-level deterministic primitive: it records *that* the
    gate moved, not *whether* the move is sequenced — gate ordering is the
    orchestrator's concern (later tickets), not the writer's.
    """
    if gate not in GATES:
        raise ValueError(f"unknown gate {gate!r}; expected one of {GATES}")

    def _set_gate(feature: dict[str, Any]) -> None:
        feature["current_gate"] = gate

    _mutate_feature_status(
        feature_root,
        _set_gate,
        event=_ADVANCE_GATE_EVENT,
        payload={"current_gate": gate},
        timestamp=timestamp,
    )


def write_initial_lane_status(status_dir: Path, lane_id: str) -> Path:
    """Write the minimal §8.2 ``lane-status.yml`` with one lane, ``lane_id``.

    The single lane starts ``pending`` / ``not_started`` with every run slot
    null — a schema-correct initial state. v0 is single-lane (§5.3), so one lane
    is the whole document; the mapping keeps the structure for the multi-lane
    future. ``lane_id`` is expected to come from the ticket-03 allocator
    (``allocate_id(feature_root, "LANE")``) at the call site, not a hardcoded
    string, so the seeded lane references a real allocated id.
    """
    status_dir.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = {
        "lanes": {
            lane_id: {
                "status": "pending",
                "current_phase": "not_started",
                "worktree": None,
                "implement_run": None,
                "review_run": None,
                "spec_gap_run": None,
                "verification_run": None,
                "gate_verdict": None,
            }
        }
    }
    path = status_dir / LANE_STATUS_FILE
    _dump_yaml(path, doc)
    return path


def write_initial_task_status(status_dir: Path) -> Path:
    """Write the minimal §8.1 ``task-status.yml`` with an empty task mapping.

    No tasks exist at feature-run creation (the Planner elaborates them during
    the requirements phase, §9.1/§18.1), so the minimal schema-correct document
    is ``tasks: {}``. Task rows are added later by this deterministic writer as
    tasks are generated — never derived from markdown checkboxes (§8.1).
    """
    status_dir.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = {"tasks": {}}
    path = status_dir / TASK_STATUS_FILE
    _dump_yaml(path, doc)
    return path


# ---------------------------------------------------------------------------
# §8.1 task-status mutation: the §9.2 Implementer writeback (v0.2 ticket 01).
# ---------------------------------------------------------------------------


def _task_status_path(feature_root: Path) -> Path:
    return feature_root / "status" / TASK_STATUS_FILE


def _load_task_status(feature_root: Path) -> dict[str, Any]:
    """Load and parse the task-status document, fail-loud on corruption (§24.2).

    Mirrors ``frozen_artifacts_status``: a real feature run always has a valid
    ``task-status.yml`` (``create_feature_run`` writes it), so an unreadable or
    mis-shaped file is genuine corruption the caller must surface - not silently
    treated as "no tasks", which could hide a broken run.
    """
    path = _task_status_path(feature_root)
    if not path.is_file():
        raise ValueError(
            f"task-status.yml missing at {path} (broken feature run, §24.2)"
        )
    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ValueError(
            f"task-status.yml at {path} is not valid YAML: {exc} (§24.2)"
        ) from exc
    if not isinstance(doc, dict):
        raise ValueError(f"task-status.yml at {path} is not a mapping (§24.2)")
    tasks = doc.get("tasks")
    if not isinstance(tasks, dict):
        raise ValueError(
            f"task-status.yml at {path} has no 'tasks' mapping (§24.2)"
        )
    return doc


def mark_task_proposed_done(
    feature_root: Path,
    task_id: str,
    *,
    lane_id: str,
    run_id: str,
    timestamp: str | None = None,
) -> None:
    """Write ``task_id``'s status back to ``proposed_done`` (§9.2, v0.2 ticket 01).

    The Implementer's ``result.json`` declares a task ``proposed_done``; this is
    the deterministic runtime that makes it canonical (§4.3 - models never write
    canonical state, only this code does). It is the *one* sanctioned canonical
    write of the implementer leg: every other §9.2 limit (no final done, no
    frozen edits, no boundary breach) is enforced by ``validate-run`` gating this
    call, not by the model's good behaviour.

    Loads ``status/task-status.yml``, registers the task row with the full §8.1
    shape if it is not already present (the Planner would normally do this at the
    task gate; the runtime registers on first ``proposed_done`` so the leg is
    self-contained), then sets ``status: proposed_done`` and
    ``proposed_done_by: <run_id>``. ``accepted_done`` is forced to ``false`` -
    invariant #7: the Implementer can only ever propose, never accept final done.
    Existing ``lane`` / ``related_requirements`` / ``related_acceptance_criteria``
    on a Planner-registered row are preserved (only the proposal fields move).
    Audited as ``mark_task_proposed_done`` so the canonical change is traceable.
    """
    if not task_id:
        raise ValueError("task_id must be a non-empty string")
    if not lane_id:
        raise ValueError("lane_id must be a non-empty string")
    if not run_id:
        raise ValueError("run_id must be a non-empty string")

    doc = _load_task_status(feature_root)
    tasks: dict[str, Any] = doc["tasks"]
    if task_id not in tasks:
        # §8.1 row shape, seeded pending. The runtime flips it to proposed_done
        # below; accepted_done starts and stays false (invariant #7).
        tasks[task_id] = {
            "status": "pending",
            "lane": lane_id,
            "owner_run": None,
            "proposed_done_by": None,
            "accepted_done": False,
            "related_requirements": [],
            "related_acceptance_criteria": [],
        }
    row = tasks[task_id]
    if not isinstance(row, dict):
        raise ValueError(
            f"task-status.yml row {task_id!r} is not a mapping (§24.2)"
        )
    row["status"] = "proposed_done"
    row["proposed_done_by"] = run_id
    row["owner_run"] = run_id
    # §9.2 / invariant #7: the Implementer may only propose, never accept final
    # done. Forced here (not just defaulted) so a row left accepted_done=true by
    # some other path cannot survive an Implementer writeback.
    row["accepted_done"] = False

    _dump_yaml(_task_status_path(feature_root), doc)
    append_audit_event(
        feature_root,
        event=_MARK_TASK_PROPOSED_DONE_EVENT,
        payload={
            "task": task_id,
            "lane": lane_id,
            "run": run_id,
            "status": "proposed_done",
        },
        timestamp=timestamp,
    )
