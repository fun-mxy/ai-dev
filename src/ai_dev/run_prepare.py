"""RUN-NNN scaffold + input-package builder (ticket 02, spec §12).

``prepare_run`` turns a ``(feature_id, role, task)`` triple into a persisted
``RUN-NNN`` directory under the feature run's ``runs/`` and writes the §12.2
input package. It is the deterministic seam that stands up an Agent Run before
the headless wrapper (ticket 03) invokes a profile against it.

RUN-NNN is allocated through the v0.0 stable-id allocator
(``allocate_id(feature_root, "RUN")``) - the same persisted per-type counter
that mints REQ/AC/DES/… ids - so run numbering is monotonic across process
restarts with no duplicates and every assignment is traceable through the
``allocate_id`` audit record. ``RUN`` is one of the twelve §5.2 counter types,
so no new allocation machinery is needed: ticket 02 reuses ticket 03 (v0.0)'s.

The input package shape seeds from the prototype's ``runs/RUN-001/input/``:
``role.md`` / ``system.md`` / ``task-package.md`` / ``output-schema.json`` /
``allowed-files.txt`` / ``context/run-context.md``. Two of those are
machine-readable contracts the downstream tickets depend on by path:

* ``output-schema.json`` - a valid JSON Schema the §14.1 validator (ticket 04)
  checks ``output/result.json`` against. Written via ``json.dumps`` so it is
  parseable by construction.
* ``allowed-files.txt`` - the §14.2 file-boundary allow-list (one RUN-relative
  path per line, ``#`` comments and blanks ignored). Seeded with the §13.1
  mandatory agent outputs (``output/result.json``, ``output/result.md``); a
  concrete task's workspace files are added by the Planner / caller.

The remaining files are human-readable guidance for the agent and carry no
machine contract - their content is fixed seed text parameterised by the run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from ai_dev.audit import append_audit_event
from ai_dev.feature_ids import allocate_id
from ai_dev.paths import INPUT_DIR, OUTPUT_DIR, WORKSPACE_DIR, feature_dir, run_dir
from ai_dev.planner_schemas import (
    PLANNER_ROLE,
    PLANNING_STAGES,
    planner_output_schema,
)

# §12.2 input-package file names (public so tests / later tickets reference one
# source of truth for the on-disk layout).
ROLE_FILE = "role.md"
SYSTEM_FILE = "system.md"
TASK_PACKAGE_FILE = "task-package.md"
OUTPUT_SCHEMA_FILE = "output-schema.json"
ALLOWED_FILES_FILE = "allowed-files.txt"
CONTEXT_DIR = "context"

# §12.1 run-directory subdirectories. §12.1 lists input/run.sh/output; the
# ticket and the prototype add workspace/ (where the agent does its task work),
# and run.sh is the wrapper's concern (ticket 03), not the prepare seam. The
# dir names live in ``paths`` (shared with ``run_wrapper``) so the layout has
# one source of truth.
_PREPARE_EVENT = "prepare_run"

# §12.2 global constraints, as the agent-facing system prompt. Every §12.2
# bullet is present (frozen / allowed-files / result.json / canonical status /
# close-issue / override-gate); result.md is added because §13.1 makes it
# mandatory alongside result.json.
_SYSTEM_MD = """\
# Global Constraints (apply to every Agent Run)

These constraints are non-negotiable and hold for every role, every run (spec §12.2).

- Do NOT modify any frozen artifact (01-requirements, 02-design, 03-tasks, 04-lane-graph). The only sanctioned way to change a frozen artifact is a Change Proposal (§4.2).
- ONLY create or modify files listed in `input/allowed-files.txt`. Any file not on that list is a file-boundary violation (§14.2).
- You MUST produce `output/result.json` conforming to `input/output-schema.json` (§13.1). This is the mandatory final step.
- You MUST also produce `output/result.md` - a short human-readable summary (§13.1).
- Do NOT write canonical status anywhere (§4.3 - only deterministic code writes canonical state).
- Do NOT close issues on your own initiative (§12.2).
- Do NOT override a gate on your own initiative (§12.2).
- Stay within this RUN directory; do not reach outside it.
"""

# The structured-output schema for result.json (§13.1). Seeds from the
# prototype's output-schema.json verbatim - a JSON Schema (draft 2020-12)
# requiring status / summary / tasks, with status in {proposed_done, failed}.
# v0.1 uses one schema for every role; per-role schemas are a later concern.
# Private: only the written file is a contract; ticket 04's validator reads the
# file, not this dict, so nothing else imports it.
_OUTPUT_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "AgentRunResult",
    "type": "object",
    "required": ["status", "summary", "tasks"],
    "additionalProperties": True,
    "properties": {
        "status": {
            "type": "string",
            "enum": ["proposed_done", "failed"],
        },
        "summary": {
            "type": "string",
            "minLength": 1,
        },
        "tasks": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "status", "evidence"],
                "additionalProperties": True,
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "status": {
                        "type": "string",
                        "enum": ["proposed_done", "failed"],
                    },
                    "evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
    },
}

# §14.2 allow-list seed: the §13.1 mandatory agent outputs (exact RUN-relative
# paths, matching the prototype's exact-match convention). Non-empty by
# construction. Task-specific workspace files are NOT in the seed - the caller
# declares them via ``prepare_run(..., allowed_files=...)`` (ticket 05 seam) so
# the §14.2 boundary check passes on a run that writes workspace files.
_ALLOWED_FILES_SEED: tuple[str, ...] = ("output/result.json", "output/result.md")


def output_schema_for_role(
    role: str, *, stage: str | None = None
) -> Mapping[str, Any]:
    """Role-aware §14.1 schema lookup: the output-schema a ``role``'s run writes.

    ADR-0008 Consequences: Planner proposal schemas differ from the implementer's
    ``result.json``, so §14.1 validation becomes role-aware. This is the one
    lookup a leg driver calls to pick the contract passed to
    ``prepare_run(..., output_schema=...)`` — and because ``validate-run`` reads
    whatever ``input/output-schema.json`` was written, role-awareness lives
    entirely in *which* schema this returns:

    * ``PLANNER_ROLE`` (the Planner, §9.1) -> the proposal schema for ``stage``
      (``stage`` required; fails loud for an unwired stage, §24.2). The model
      authors id-free content; promote (ticket 01) allocates the ids.
    * any other role (Implementer / Reviewer / Spec-Gap, §9.2-9.4) -> the
      implementer ``result.json`` schema (``_OUTPUT_SCHEMA``), the v0.1 default.

    The Planner branch fails loud when ``stage`` is omitted: a Planner run
    without a stage is a config error (the driver forgot which artifact it is
    producing), not a silent fallback to the implementer schema — which would
    validate the wrong contract and let a malformed proposal pass.
    """
    if role == PLANNER_ROLE:
        if stage is None:
            raise ValueError(
                f"role {role!r} requires a --stage (one of {PLANNING_STAGES}); "
                f"a Planner run must declare which artifact it produces (§24.2)"
            )
        return planner_output_schema(stage)
    return _OUTPUT_SCHEMA

# Header comment block for allowed-files.txt (the prototype's seed header).
_ALLOWED_FILES_HEADER = (
    "# Agent may ONLY create or modify these paths (relative to the RUN directory).\n"
    "# Anything else is a file-boundary violation (spec §14.2).\n"
    "# Task-specific workspace files must be added here before the run.\n"
)


def _allowed_files_md(extra: Sequence[str] = ()) -> str:
    """Render ``allowed-files.txt``: the seed + caller-declared extras (sorted).

    The two §13.1 mandatory outputs are always present. ``extra`` carries the
    task-specific RUN-relative paths the Planner / caller declared for this run
    (ticket 05 seam); they are deduped against the seed and each other, then the
    whole list is sorted so two prepares of the same task produce byte-identical
    output (diff-stable, matching the changed_files sort convention). Blank /
    whitespace entries are rejected by ``prepare_run`` before reaching here.
    """
    combined: list[str] = list(_ALLOWED_FILES_SEED)
    seen = set(combined)
    for path in extra:
        if path not in seen:
            seen.add(path)
            combined.append(path)
    combined.sort()
    return _ALLOWED_FILES_HEADER + "\n".join(combined) + "\n"


def _role_md(role: str, run_id: str) -> str:
    """§12.2 role.md: a one-line definition of this run's role.

    Seeds from the prototype's ``You are the Implementer for RUN-001.`` - the
    role is parameterised and scoped to the run id (prepare-run does not take a
    lane; v0.1 is single-lane, and the prototype scopes the role to the run).
    """
    return f"You are the {role} for {run_id}.\n"


def _task_package_md(
    feature_id: str, run_id: str, role: str, task: str
) -> str:
    """§12.2 task-package.md: the task for this run, in spec section order.

    The verbatim ``task`` text is the contract; the structured sections
    (expected outputs / done criteria / verification / forbidden) reference the
    contract files (``output-schema.json``, ``allowed-files.txt``) and the §14
    validation the wrapper runs, so the agent knows how its output is judged.

    §12.2 also names ``lane id`` and ``task ids`` as task-package elements.
    v0.1 is single-lane (§5.3) and prepare-run does not take a lane or allocate
    TASK-NNN ids, so the lane is recorded as the single-lane MVP context and
    task ids are noted as Planner-allocated (§7.4) rather than invented empty -
    matching the prototype seed, which carries a ``lane:`` line and no
    pre-assigned task ids.
    """
    return (
        f"# Task Package - {run_id}\n"
        f"\n"
        f"- run_id: {run_id}\n"
        f"- feature: {feature_id}\n"
        f"- role: {role}\n"
        f"- lane: (single-lane MVP, §5.3)\n"
        f"\n"
        f"## Task\n"
        f"\n"
        f"{task}\n"
        f"\n"
        f"> Stable task IDs (TASK-NNN) are allocated by the Planner during the "
        f"task gate (§7.4); this run executes the task above as a single unit.\n"
        f"\n"
        f"## Expected outputs\n"
        f"\n"
        f"- `output/result.json` conforming to `input/output-schema.json` "
        f"(mandatory, §13.1).\n"
        f"- `output/result.md` - a short human-readable summary "
        f"(mandatory, §13.1).\n"
        f"- Any task-specific files under `workspace/`, each listed in "
        f"`input/allowed-files.txt`.\n"
        f"\n"
        f"## Done criteria\n"
        f"\n"
        f"- `output/result.json` conforms to `input/output-schema.json`.\n"
        f"- Every changed file is listed in `input/allowed-files.txt` (§14.2).\n"
        f"- No frozen artifact was modified (§14.3).\n"
        f"\n"
        f"## Verification\n"
        f"\n"
        f"The wrapper runs the §14 deterministic validation (schema + "
        f"file-boundary + frozen) after the run completes; the role does not "
        f"self-verify.\n"
        f"\n"
        f"## Forbidden\n"
        f"\n"
        f"- Touching any path not in `input/allowed-files.txt`.\n"
        f"- Modifying any frozen artifact (01-requirements, 02-design, "
        f"03-tasks, 04-lane-graph).\n"
        f"- Writing canonical status, closing issues, or overriding gates.\n"
        f"- Running git commands.\n"
    )


def _context_md(feature_id: str, run_id: str, role: str) -> str:
    """§12.2 context/run-context.md: pointers to the artifacts the role reads.

    The feature run's §7 artifacts (requirements/design/tasks/lane-graph) are
    seeded templates at this stage; this file points the agent at them by path
    rather than copying content, so the run stays small and the source of truth
    stays canonical.
    """
    return (
        f"# Run Context - {run_id}\n"
        f"\n"
        f"- feature: {feature_id}\n"
        f"- run: {run_id}\n"
        f"- role: {role}\n"
        f"\n"
        f"This run executes against feature `{feature_id}`. The canonical "
        f"artifacts the role may need to read live at the feature-run root "
        f"(above this RUN directory):\n"
        f"\n"
        f"- `01-requirements.md` / `01-requirements.json` (§7.2)\n"
        f"- `02-design.md` / `02-design.json` (§7.3)\n"
        f"- `03-tasks.md` (§7.4)\n"
        f"- `04-lane-graph.yml` (§7.5)\n"
        f"\n"
        f"These are seeded templates at this stage; the Planner elaborates them "
        f"during the requirements / design / task gates. Prior decisions live "
        f"under `decisions/` and prior runs under sibling `RUN-*` directories "
        f"in `runs/`.\n"
        f"\n"
        f"See `../task-package.md` for the specific task of this run.\n"
    )


def write_input_package_to(
    feature_id: str,
    run_id: str,
    role: str,
    task: str,
    input_dir: Path,
    *,
    allowed_files: Sequence[str] = (),
    output_schema: Mapping[str, Any] | None = None,
) -> None:
    """Write the six §12.2 input-package files under an arbitrary ``input_dir``.

    Public entry over ``_write_input_package`` for callers that render a package
    **without minting a RUN-NNN** - notably ``--dry-run`` (ticket 04 / ADR-0004),
    which renders the would-be package into a temp dir so the monotonic RUN
    counter and the feature-run tree stay untouched. ``run_id`` here is a display
    placeholder (e.g. ``RUN-DRYRUN``), never a real allocated id.
    """
    _write_input_package(
        feature_id, run_id, role, task, input_dir, allowed_files,
        output_schema=output_schema,
    )


def _write_input_package(
    feature_id: str,
    run_id: str,
    role: str,
    task: str,
    input_dir: Path,
    allowed_files: Sequence[str] = (),
    output_schema: Mapping[str, Any] | None = None,
) -> None:
    """Write the six §12.2 input-package files under ``input_dir``."""
    input_dir.mkdir(parents=True, exist_ok=True)

    (input_dir / ROLE_FILE).write_text(_role_md(role, run_id))
    (input_dir / SYSTEM_FILE).write_text(_SYSTEM_MD)
    (input_dir / TASK_PACKAGE_FILE).write_text(
        _task_package_md(feature_id, run_id, role, task)
    )
    # json.dumps guarantees a parseable file (ticket 02: output-schema 可解析).
    # ``output_schema`` (v0.2 seam) lets a checking leg substitute the §15 issues
    # schema for the implementer default, so ``validate-run`` checks the role's
    # own result.json contract (§14.1) without a new run mechanism.
    schema = output_schema if output_schema is not None else _OUTPUT_SCHEMA
    (input_dir / OUTPUT_SCHEMA_FILE).write_text(
        json.dumps(schema, indent=2, ensure_ascii=False) + "\n"
    )
    (input_dir / ALLOWED_FILES_FILE).write_text(_allowed_files_md(allowed_files))

    context_dir = input_dir / CONTEXT_DIR
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "run-context.md").write_text(
        _context_md(feature_id, run_id, role)
    )


def prepare_run(
    repo_root: Path,
    feature_id: str,
    role: str,
    task: str,
    *,
    allowed_files: Sequence[str] = (),
    output_schema: Mapping[str, Any] | None = None,
    origin: str | None = None,
) -> str:
    """Allocate ``RUN-NNN`` and scaffold its directory + input package.

    Reuses the v0.0 stable-id allocator to mint ``RUN-NNN`` (monotonic across
    restarts, audited as ``allocate_id``), then creates the ``input/``,
    ``output/`` and ``workspace/`` directories under the feature run's
    ``runs/RUN-NNN/`` and writes the §12.2 input package seeded from the
    prototype. Appends a ``prepare_run`` audit record associating the run with
    its role, and returns the allocated run id.

    ``allowed_files`` is the ticket-05 integration seam: the task-specific
    RUN-relative paths (e.g. ``workspace/hello.py``) the run is permitted to
    write, appended to the mandatory-output seed in ``allowed-files.txt``. The
    Planner / caller declares them at prepare time so the §14.2 boundary check
    passes on a real run that writes workspace files. Defaults to empty - the
    ticket-02 behaviour (mandatory outputs only).

    ``output_schema`` is the v0.2 checking-leg seam: a JSON Schema written to
    ``input/output-schema.json`` in place of the implementer default
    (``_OUTPUT_SCHEMA``). The reviewer / spec-gap legs pass the shared §15
    issues schema so their ``result.json`` (``{"issues": [...]}``) is the
    contract ``validate-run`` checks (§14.1), reusing the run mechanism rather
    than building a new one. Defaults to ``None`` = the implementer schema, so
    v0.1 behaviour is unchanged.

    Raises ``ValueError`` (§24.2 fail loud) if the feature run does not exist,
    if ``role`` / ``task`` is empty, or if an ``allowed_files`` entry is blank -
    and does so before allocating an id or creating any directory, so a failed
    prepare leaves no partial run behind.
    """
    if not role:
        raise ValueError("role must be a non-empty string")
    if not task:
        raise ValueError("task must be a non-empty string")
    # §24.2 fail loud: a blank allowed_files entry is a config error (it would
    # imply "any path" if silently coerced to empty). Reject before allocating -
    # same shape as the role/task checks above (typed API, trust the contract).
    normalised_allowed: list[str] = []
    for entry in allowed_files:
        stripped = entry.strip()
        if not stripped:
            raise ValueError(
                "allowed_files entries must be non-empty strings (§14.2)"
            )
        normalised_allowed.append(stripped)

    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(
            f"feature run {feature_id} not found under {repo_root}"
        )

    # Mint the run id via the v0.0 allocator (audits allocate_id, persists the
    # RUN counter) before touching the filesystem, so a crash here leaves an
    # allocated-but-empty id rather than a partial directory.
    run_id = allocate_id(feature_root, "RUN", origin=origin)
    run_root = run_dir(repo_root, feature_id, run_id)

    for sub in (INPUT_DIR, OUTPUT_DIR, WORKSPACE_DIR):
        (run_root / sub).mkdir(parents=True, exist_ok=True)

    _write_input_package(
        feature_id, run_id, role, task, run_root / INPUT_DIR, normalised_allowed,
        output_schema=output_schema,
    )

    append_audit_event(
        feature_root,
        event=_PREPARE_EVENT,
        payload={"run": run_id, "feature": feature_id, "role": role},
        origin=origin,
    )
    return run_id
