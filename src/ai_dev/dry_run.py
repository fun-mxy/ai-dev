"""``--dry-run`` planner — run a command's checks, skip its side effects (ADR-0004).

v0.4 polish §26.5: the only *new capability* of v0.4. Every side-effect command
(spawn-a-subprocess agent command, or write-canonical-state deterministic
command) gets a ``--dry-run`` flag that runs the command's full **planning +
§24.2 precondition + legality check** but skips the expensive/irreversible
step. This module is the planning half: each ``plan_*`` helper reuses the
existing pure read/compute helpers and returns a ``DryRunPlan`` describing what
the command *would* do. The CLI prints it and exits 0.

The critical invariant (glossary pin ``dry-run``): **dry-run never mints a
stable id.** Agent commands render their would-be input package into a
``tempfile.mkdtemp()`` directory — never under ``runs/`` — so the monotonic
``RUN`` counter (and the ``DEC`` counter for triage) and the feature-run tree
are left byte-for-byte unchanged. ADR-0004 records why dry-run does **not** flow
through ``prepare_run`` (allocate-and-skip would burn a monotonic id and orphan
a directory, violating both monotonic allocation and "dry" semantics).

Audit (ticket 04 lists ``origin=dry-run``): dry-run writes **nothing** —
including no ``audit.log`` append — the strongest reading of "feature 树零改动
/ 不写任何 canonical state". The ``origin=dry-run`` tag lands together with
ticket 02's ``origin`` audit field; ADR-0004 records the deferral.

Precondition failures (missing feature/lane/issue, unknown disposition,
unfrozen artifacts, bad gate sequencing) raise ``ValueError`` exactly as the
real command does — the CLI surfaces them as a clean ``error:`` line + exit 1.
A **legality refusal** the real command would raise (illegal triage cell,
exhausted fix-loop budget, already-frozen artifact) is *reported in the plan*
(``would be refused: …``) and exits 0: dry-run is a preview, and "this would be
refused" is a successful answer to "what would happen?".
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from ai_dev.checking_legs import (
    ISSUES_OUTPUT_SCHEMA,
    _REVIEWER_ROLE,
    _SPEC_GAP_ROLE,
    _require_frozen,
    _reviewer_task_text,
    _spec_gap_task_text,
    ImplementRunFacts,
    read_implement_run_facts,
)
from ai_dev.coherence_gate import COHERENCE_DECISION_JSON, compute_coherence
from ai_dev.final_report import FINAL_REPORT_JSON, FINAL_REPORT_MD, compute_final_report
from ai_dev.fix_run import _current_request_fix_targets
from ai_dev.github_projection import (
    GITHUB_TOKEN_ENV,
    compute_github_plan,
)
from ai_dev.implement_leg import (
    _IMPLEMENTER_ROLE,
    lane_allowed_files,
    read_lane_entry,
    read_task_text,
)
from ai_dev.lane_gate import (
    LANE_DECISION_JSON,
    LANE_DECISION_MD,
    compute_lane_decision,
)
from ai_dev.paths import (
    INPUT_DIR,
    OUTPUT_DIR,
    feature_dir,
    lane_dir,
    run_dir,
)
from ai_dev.planner_leg import (
    _design_task_text,
    _planner_task_text,
    _render_frozen_design_summary,
    _render_frozen_requirements_summary,
    _tasks_task_text,
    read_intent,
)
# The model-role token lives in planner_schemas (planner_leg only re-exports it);
# import from its home rather than through the leg (avoid a Middle Man hop).
from ai_dev.planner_schemas import PLANNER_ROLE as _PLANNER_ROLE
# The frozen-requirements precondition the design leg stitches against lives in
# promote (the canonical reader); the freeze-gate coverage precheck lives in
# coverage. Both are pure read/compute helpers - no canonical write - so importing
# them here keeps dry-run free of side effects (ADR-0004).
from ai_dev.promote import read_frozen_design_doc, read_frozen_requirements_doc
from ai_dev.coverage import freeze_gate_coverage
from ai_dev.profile_comparison import (
    PROFILE_COMPARISON_JSON,
    PROFILE_COMPARISON_MD,
    compute_profile_comparison,
)
from ai_dev.profiles import AgentProfile, token_source_var
from ai_dev.run_prepare import (
    ALLOWED_FILES_FILE,
    TASK_PACKAGE_FILE,
    output_schema_for_role,
    write_input_package_to,
)
from ai_dev.run_wrapper import (
    DEFAULT_MAX_TURNS,
    DEFAULT_PERMISSION_MODE,
    Invocation,
    build_prompt,
    get_runner,
)
from ai_dev.status import (
    FROZEN_ARTIFACTS,
    frozen_artifacts_status,
    fix_loop_budget,
    fix_loop_budget_exhausted,
)
from ai_dev.triage import (
    DISPOSITIONS,
    SEVERITIES,
    _matrix_cell,
)
from ai_dev.issue_bundle import ISSUES_DIR
from ai_dev.json_artifact import read_json_object

# Display placeholder for a run id that dry-run does NOT mint. Never a real
# allocated id (the RUN counter is untouched); appears only inside the temp dir
# and the printed plan so the operator can see "this is where RUN-NNN would go".
_DRY_RUN_RUN_ID = "RUN-DRYRUN"

# The agent-output artifacts the wrapper would write on a real run (§13.2).
_WRAPPER_OUTPUTS = ["metadata.json", "stdout.log", "stderr.log", "env-snapshot.txt"]


@dataclass(frozen=True)
class DryRunPlan:
    """What a command *would* do if run for real.

    ``summary`` is the one-line headline (``IMPLEMENT DRY-RUN would prepare
    Implementer run + spawn claude``); ``details`` carries the structured preview
    (would-be ids / writes / spawn / invocation / allowed-files / refusal). The
    CLI renders ``details`` as ``key: value`` lines under the summary.
    """

    command: str
    feature_id: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


def _read_allowed_files(run_input_dir: Path) -> list[str]:
    """Read a rendered ``allowed-files.txt`` back as a sorted path list.

    Comments / blanks are dropped, matching the §14.2 boundary file convention
    so the plan shows the exact paths a real run would be confined to.
    """
    paths: list[str] = []
    allowed = run_input_dir / ALLOWED_FILES_FILE
    if allowed.is_file():
        for line in allowed.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                paths.append(stripped)
    return sorted(paths)


def _build_invocation(
    profile: AgentProfile,
    run_id: str,
    run_root: Path,
    max_turns: int,
    permission_mode: str,
) -> Invocation:
    """Build the exact argv (+ stdin) a real run would spawn (§11.1), per ``cli``.

    Dispatches through ``get_runner`` (ADR-0005 D1) so each adapter owns its
    invocation shape: claude gets ``-p <prompt>`` (prompt in argv) and codex gets
    ``exec -`` (prompt on stdin, ``-s workspace-write`` sandbox). The binary is
    the profile-declared ``cli`` *name* — the wrapper's ``shutil.which`` resolution
    is the spawn-time concern, not the plan's (a dry-run must not require the
    binary on ``PATH``).

    ``run_wrapper`` builds the prompt verbatim (``build_prompt``); the adapter's
    ``build_invocation`` wires it into the per-CLI argv. The claude ``--settings``
    flag references the would-be ``.run-settings.json`` path (the path a real
    spawn writes, ``run_wrapper._RUN_SETTINGS``) — the file is **not** materialised.
    Dry-run writes nothing (glossary pin ``dry-run``), so for ``run-headless``
    (whose run root is the *real* ``runs/RUN-NNN/``) the feature tree stays
    byte-for-byte unchanged. The argv is still faithful: the path is what the
    spawn would pass.
    """
    prompt = build_prompt(run_id, run_root)
    runner = get_runner(profile)
    return runner.build_invocation(
        profile=profile,
        output_dir=run_root / OUTPUT_DIR,
        binary=profile.cli,
        max_turns=max_turns,
        permission_mode=permission_mode,
        prompt=prompt,
    )


def _env_target_names(profile: AgentProfile) -> dict[str, str | None]:
    """The §10.3 env var *names* a real spawn would set (never values).

    Dry-run must not read the token value (§10.2); this reports only the variable
    names the operator can verify before committing to a real run.
    """
    return {
        "token_source": token_source_var(profile),
        "base_url_var": "ANTHROPIC_BASE_URL" if profile.base_url is not None else None,
        "model_var": "ANTHROPIC_MODEL" if profile.model is not None else None,
    }


def _require_token_source(profile: AgentProfile) -> str | None:
    """§24.2: the token source name must be set before a run can be planned.

    A token-required adapter (claude - ``ClaudeRunner.token_required``) fails loud
    when the source is unset: it has no non-env auth path. A codex profile
    (``token_required=False``) may proceed without an env token via codex's native
    stored ``~/.codex/auth.json`` (ADR-0005 D3 amended), so its source may be
    ``None`` - the plan reports ``token_source: None`` and the run spawns anyway.
    Either way the value is never read (§10.2): only the name is resolved.
    """
    source = token_source_var(profile)
    if source is None and get_runner(profile).token_required:
        raise ValueError(
            f"token source not set for profile {profile.name!r} "
            f"({profile.token_source_description()} is unset); set it before "
            f"running (§24.2)"
        )
    return source


def _agent_invocation_details(
    profile: AgentProfile,
    run_root: Path,
    allowed_files: list[str],
    max_turns: int,
    permission_mode: str,
) -> dict[str, Any]:
    """Shared details block for an agent dry-run: invocation + boundary + env."""
    invocation = _build_invocation(
        profile, _DRY_RUN_RUN_ID, run_root, max_turns, permission_mode
    )
    return {
        "profile": profile.name,
        "role_cli": profile.cli,
        "allowed_files": allowed_files,
        "env_target_names": _env_target_names(profile),
        "max_turns": max_turns,
        "permission_mode": permission_mode,
        "invocation": invocation.argv,
        # claude rides the prompt in argv (``-p <prompt>``); codex pipes it on
        # stdin (``exec -``), so the argv carries no prompt - flag the difference
        # so the plan is faithful about where the prompt goes (ADR-0005 D1).
        "prompt_on_stdin": invocation.stdin is not None,
        "would_spawn": True,
        "would_write_run_outputs": _WRAPPER_OUTPUTS,
    }


# ---------------------------------------------------------------------------
# Agent commands.
# ---------------------------------------------------------------------------


def _render_temp_package(
    feature_id: str,
    role: str,
    task: str,
    allowed_files: list[str],
    output_schema: Mapping[str, Any] | None,
) -> tuple[Path, Path]:
    """Render a would-be §12.2 input package into a fresh temp dir.

    The temp dir stands in for ``runs/RUN-NNN/`` so the monotonic RUN counter and
    the feature-run tree stay untouched. Returns ``(temp_run_root, input_dir)``.
    """
    temp_root = Path(tempfile.mkdtemp(prefix="ai-dev-dryrun-"))
    input_dir = temp_root / INPUT_DIR
    write_input_package_to(
        feature_id,
        _DRY_RUN_RUN_ID,
        role,
        task,
        input_dir,
        allowed_files=allowed_files,
        output_schema=output_schema,
    )
    (temp_root / OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
    return temp_root, input_dir


def plan_run_headless(
    repo_root: Path,
    feature_id: str,
    run_id: str,
    profile: AgentProfile,
    *,
    max_turns: int = DEFAULT_MAX_TURNS,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
) -> DryRunPlan:
    """Plan a ``run-headless``: verify preconditions + compute the spawn, no spawn.

    The run is already prepared (``RUN-NNN`` exists from a prior ``prepare-run``),
    so there is no temp dir — the plan builds the exact claude argv against the
    real run directory and reports the §14.2 boundary + the env target names.
    """
    run_root = run_dir(repo_root, feature_id, run_id)
    if not run_root.is_dir():
        raise ValueError(
            f"run directory {run_id} not found under feature {feature_id} "
            f"(prepare it with `ai-dev prepare-run` first)"
        )
    _require_token_source(profile)
    allowed_files = _read_allowed_files(run_root / INPUT_DIR)
    details = _agent_invocation_details(
        profile, run_root, allowed_files, max_turns, permission_mode
    )
    details.update(
        {
            "run_id": run_id,
            "would_mint_ids": [],
            "would_write": [f"runs/{run_id}/output/{n}" for n in _WRAPPER_OUTPUTS],
        }
    )
    return DryRunPlan(
        command="run-headless",
        feature_id=feature_id,
        summary=(
            f"RUN-HEADLESS DRY-RUN - would spawn {profile.cli} against {run_id} "
            f"(profile={profile.name}, {len(allowed_files)} allowed file(s))"
        ),
        details=details,
    )


def plan_implement(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    profile: AgentProfile,
    *,
    max_turns: int = DEFAULT_MAX_TURNS,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
) -> DryRunPlan:
    """Plan an ``implement`` leg: frozen precondition + temp-dir package, no spawn.

    Reuses the implementer leg's precondition reads (frozen tasks + lane_graph,
    task text, lane entry, lane allowed-files) but renders the would-be input
    package into a temp dir instead of minting ``RUN-NNN``. Reports the would-be
    run + the lane rollup paths.
    """
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id} not found under {repo_root}")
    frozen = frozen_artifacts_status(feature_root)
    if not (frozen.get("tasks") and frozen.get("lane_graph")):
        raise ValueError(
            "implementer leg requires frozen tasks + lane_graph (§4.2); "
            "freeze them at the task gate first"
        )
    task_text = read_task_text(feature_root)
    lane = read_lane_entry(feature_root, lane_id)
    allowed = lane_allowed_files(lane)
    _require_token_source(profile)

    temp_root, input_dir = _render_temp_package(
        feature_id, _IMPLEMENTER_ROLE, task_text, allowed, None
    )
    details = _agent_invocation_details(
        profile, temp_root, _read_allowed_files(input_dir), max_turns, permission_mode
    )
    details.update(
        {
            "lane_id": lane_id,
            "role": _IMPLEMENTER_ROLE,
            "task_package": str(input_dir / TASK_PACKAGE_FILE),
            "temp_dir": str(temp_root),
            "would_mint_ids": ["RUN-NNN (next monotonic)"],
            "would_write": [
                "runs/RUN-NNN/input/* (package)",
                "runs/RUN-NNN/output/{result.json,result.md,metadata.json,...}",
                f"lanes/{lane_id}/implement-result.md",
                f"lanes/{lane_id}/implement-result.json",
            ],
            "would_writeback_task_status": "task-status.yml (proposed_done, gated on validation)",
        }
    )
    return DryRunPlan(
        command="implement",
        feature_id=feature_id,
        summary=(
            f"IMPLEMENT DRY-RUN - would prepare {_IMPLEMENTER_ROLE} run for lane "
            f"{lane_id} + spawn {profile.cli} (no id minted, no spawn)"
        ),
        details=details,
    )


def plan_generate_requirements(
    repo_root: Path,
    feature_id: str,
    profile: AgentProfile,
    *,
    feedback: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
) -> DryRunPlan:
    """Plan a ``generate-requirements`` leg: intent read + temp-dir package, no spawn.

    Reuses the Planner leg's precondition reads (feature exists, intent present
    in ``00-intent.md``) and renders the would-be Planner input package into a
    temp dir instead of minting ``RUN-NNN``. The proposal schema
    (``output_schema_for_role("Planner", stage="requirements")``) is the §14.1
    contract the real run writes; the plan reports it as the would-be output
    schema. Reports the would-be run + the promote targets
    (``01-requirements.{json,md}``) so the operator sees the full generate→
    promote slice before committing to a real run. ``feedback`` (when given) is
    surfaced in the plan so the refinement channel is visible.
    """
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id} not found under {repo_root}")
    # Precondition: the intent must exist (the Planner elaborates from it). Reads
    # the same helper the real leg uses, so a missing/empty intent fails loud
    # here exactly as it would at run time.
    intent = read_intent(feature_root)
    task_text = _planner_task_text(feature_id, intent, feedback)
    schema = output_schema_for_role(_PLANNER_ROLE, stage="requirements")
    _require_token_source(profile)

    temp_root, input_dir = _render_temp_package(
        feature_id, _PLANNER_ROLE, task_text, [], schema
    )
    details = _agent_invocation_details(
        profile, temp_root, _read_allowed_files(input_dir), max_turns, permission_mode
    )
    details.update(
        {
            "role": _PLANNER_ROLE,
            "stage": "requirements",
            "task_package": str(input_dir / TASK_PACKAGE_FILE),
            "output_schema": "requirements proposal (id-free; promote allocates ids)",
            "feedback": feedback if (feedback is not None and feedback.strip()) else None,
            "temp_dir": str(temp_root),
            "would_mint_ids": ["RUN-NNN (next monotonic)"],
            "would_write": [
                "runs/RUN-NNN/input/* (proposal-schema package)",
                "runs/RUN-NNN/output/{result.json,result.md,metadata.json,...}",
                "01-requirements.json (canonical-unfrozen, via promote)",
                "01-requirements.md (rendered mirror, via promote)",
            ],
            # promote is gated on a passing validation in the real leg; surfaced
            # so the plan is honest that a schema-invalid proposal promotes nothing.
            "would_promote": "gated on validate-run PASS (proposal schema-valid)",
        }
    )
    return DryRunPlan(
        command="generate-requirements",
        feature_id=feature_id,
        summary=(
            f"GENERATE-REQUIREMENTS DRY-RUN - would prepare {_PLANNER_ROLE} run "
            f"for {feature_id} + spawn {profile.cli} (no id minted, no spawn)"
        ),
        details=details,
    )


def plan_generate_design(
    repo_root: Path,
    feature_id: str,
    profile: AgentProfile,
    *,
    feedback: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
) -> DryRunPlan:
    """Plan a ``generate-design`` leg: intent + frozen-requirements read, no spawn.

    Mirrors ``plan_generate_requirements`` but adds the design leg's second
    precondition: the requirements artifact must be **frozen** (design may only
    stitch against a frozen upstream, ADR-0008 D2). Reuses the Planner leg's
    precondition reads (feature exists, intent present, requirements frozen) and
    renders the would-be Planner design input package into a temp dir instead of
    minting ``RUN-NNN``. The design proposal schema
    (``output_schema_for_role("Planner", stage="design")``) is the §14.1 contract
    the real run writes; the plan reports it as the would-be output schema.
    Reports the would-be run + the promote targets (``02-design.{json,md}``) so
    the operator sees the full generate->promote slice before committing to a
    real run. ``feedback`` (when given) is surfaced in the plan so the refinement
    channel (ADR-0008 D4) is visible.
    """
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id} not found under {repo_root}")
    # Preconditions mirror the real leg exactly (so a missing intent or an
    # unfrozen-requirements rejection fails loud here as it would at run time).
    # ``read_frozen_requirements_doc`` raises ValueError when requirements is not
    # frozen - the design gate's gating precondition (ADR-0008 D2).
    intent = read_intent(feature_root)
    req_doc = read_frozen_requirements_doc(feature_root)
    req_summary = _render_frozen_requirements_summary(req_doc)
    task_text = _design_task_text(feature_id, intent, req_summary, feedback)
    schema = output_schema_for_role(_PLANNER_ROLE, stage="design")
    _require_token_source(profile)

    temp_root, input_dir = _render_temp_package(
        feature_id, _PLANNER_ROLE, task_text, [], schema
    )
    details = _agent_invocation_details(
        profile, temp_root, _read_allowed_files(input_dir), max_turns, permission_mode
    )
    details.update(
        {
            "role": _PLANNER_ROLE,
            "stage": "design",
            "task_package": str(input_dir / TASK_PACKAGE_FILE),
            "output_schema": "design proposal (id-free; promote allocates DES ids)",
            "upstream": "01-requirements.json (frozen REQ ids; stitched via add_upstream)",
            "feedback": feedback if (feedback is not None and feedback.strip()) else None,
            "temp_dir": str(temp_root),
            "would_mint_ids": ["RUN-NNN (next monotonic)"],
            "would_write": [
                "runs/RUN-NNN/input/* (design-proposal-schema package)",
                "runs/RUN-NNN/output/{result.json,result.md,metadata.json,...}",
                "02-design.json (canonical-unfrozen, via promote)",
                "02-design.md (rendered mirror, via promote)",
            ],
            # promote is gated on a passing validation in the real leg; surfaced
            # so the plan is honest that a schema-invalid proposal promotes nothing.
            "would_promote": "gated on validate-run PASS (proposal schema-valid)",
        }
    )
    return DryRunPlan(
        command="generate-design",
        feature_id=feature_id,
        summary=(
            f"GENERATE-DESIGN DRY-RUN - would prepare {_PLANNER_ROLE} run "
            f"for {feature_id} + spawn {profile.cli} (no id minted, no spawn)"
        ),
        details=details,
    )


def plan_generate_tasks(
    repo_root: Path,
    feature_id: str,
    profile: AgentProfile,
    *,
    feedback: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
) -> DryRunPlan:
    """Plan a ``generate-tasks`` leg: intent + frozen-reqs + frozen-design read, no spawn.

    Mirrors ``plan_generate_design`` but adds the tasks leg's third precondition:
    the design artifact must also be **frozen** (tasks may only stitch against
    frozen upstreams - requirements AND design, ADR-0008 D2 - the first stage with
    two). Reuses the Planner leg's precondition reads (feature exists, intent
    present, requirements frozen, design frozen) and renders the would-be Planner
    tasks input package into a temp dir instead of minting ``RUN-NNN``. The tasks
    proposal schema (``output_schema_for_role("Planner", stage="tasks")``) is the
    §14.1 contract the real run writes; the plan reports it as the would-be output
    schema. Reports the would-be run + the promote targets (``03-tasks.{json,md}``
    + the seeded ``task-status.yml`` + the populated ``04-lane-graph.yml``) so the
    operator sees the full generate->promote slice (the four-file write) before
    committing to a real run. ``feedback`` (when given) is surfaced so the
    refinement channel (ADR-0008 D4) is visible.
    """
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id} not found under {repo_root}")
    # Preconditions mirror the real leg exactly: tasks stitches against TWO frozen
    # upstreams (REQ + DES), so both must be frozen (ADR-0008 D2). The
    # ``read_frozen_*_doc`` helpers raise ValueError when either is not frozen -
    # the task gate's gating precondition.
    intent = read_intent(feature_root)
    req_doc = read_frozen_requirements_doc(feature_root)
    des_doc = read_frozen_design_doc(feature_root)
    req_summary = _render_frozen_requirements_summary(req_doc)
    des_summary = _render_frozen_design_summary(des_doc)
    task_text = _tasks_task_text(feature_id, intent, req_summary, des_summary, feedback)
    schema = output_schema_for_role(_PLANNER_ROLE, stage="tasks")
    _require_token_source(profile)

    temp_root, input_dir = _render_temp_package(
        feature_id, _PLANNER_ROLE, task_text, [], schema
    )
    details = _agent_invocation_details(
        profile, temp_root, _read_allowed_files(input_dir), max_turns, permission_mode
    )
    details.update(
        {
            "role": _PLANNER_ROLE,
            "stage": "tasks",
            "task_package": str(input_dir / TASK_PACKAGE_FILE),
            "output_schema": "tasks proposal (id-free; promote allocates TASK ids)",
            "upstream": (
                "01-requirements.json + 02-design.json (frozen REQ+DES ids; "
                "stitched via add_upstream)"
            ),
            "feedback": feedback if (feedback is not None and feedback.strip()) else None,
            "temp_dir": str(temp_root),
            "would_mint_ids": ["RUN-NNN (next monotonic)"],
            "would_write": [
                "runs/RUN-NNN/input/* (tasks-proposal-schema package)",
                "runs/RUN-NNN/output/{result.json,result.md,metadata.json,...}",
                "03-tasks.json (canonical-unfrozen, via promote)",
                "03-tasks.md (rendered mirror, via promote)",
                "status/task-status.yml (seeded, all pending, via promote)",
                "04-lane-graph.yml (single lane populated, via promote)",
            ],
            # promote is gated on a passing validation in the real leg; surfaced
            # so the plan is honest that a schema-invalid proposal promotes nothing.
            "would_promote": "gated on validate-run PASS (proposal schema-valid)",
        }
    )
    return DryRunPlan(
        command="generate-tasks",
        feature_id=feature_id,
        summary=(
            f"GENERATE-TASKS DRY-RUN - would prepare {_PLANNER_ROLE} run "
            f"for {feature_id} + spawn {profile.cli} (no id minted, no spawn)"
        ),
        details=details,
    )


def _plan_checking(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    profile: AgentProfile,
    *,
    role: str,
    task_fn: Callable[[Path, ImplementRunFacts], str],
    command: str,
    label: str,
    max_turns: int,
    permission_mode: str,
) -> DryRunPlan:
    """Shared planner for the ``review`` / ``spec-gap`` checking legs (§9.3/§9.4)."""
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id} not found under {repo_root}")
    _require_frozen(feature_root)
    facts = read_implement_run_facts(repo_root, feature_id, lane_id)
    # ``task_fn`` takes ``(feature_root, facts)`` so the spec-gap task gets the
    # root it reads the frozen spec artifacts from; the reviewer task ignores
    # the root (it builds from the implement-run facts alone), so plan_review
    # adapts via a thunk. (Previously this called ``task_fn(facts)`` with one
    # arg, which crashed every spec-gap dry-run: _spec_gap_task_text needs the
    # root too.)
    task_text = task_fn(feature_root, facts)
    _require_token_source(profile)

    temp_root, input_dir = _render_temp_package(
        feature_id, role, task_text, [], ISSUES_OUTPUT_SCHEMA
    )
    details = _agent_invocation_details(
        profile, temp_root, _read_allowed_files(input_dir), max_turns, permission_mode
    )
    details.update(
        {
            "lane_id": lane_id,
            "role": role,
            "implement_run_id": facts.run_id,
            "temp_dir": str(temp_root),
            "would_mint_ids": ["RUN-NNN (next monotonic)"],
            "would_write": [
                "runs/RUN-NNN/input/* (issues-schema package)",
                "runs/RUN-NNN/output/{result.json,result.md,metadata.json,...}",
                f"lanes/{lane_id}/{command}-report.md",
                f"lanes/{lane_id}/{command}-report.json",
            ],
        }
    )
    return DryRunPlan(
        command=command,
        feature_id=feature_id,
        summary=(
            f"{label} DRY-RUN - would prepare {role} run on implement run "
            f"{facts.run_id} + spawn {profile.cli} (no id minted, no spawn)"
        ),
        details=details,
    )


def plan_review(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    profile: AgentProfile,
    *,
    max_turns: int = DEFAULT_MAX_TURNS,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
) -> DryRunPlan:
    """Plan a ``review`` (Code Reviewer, §9.3) leg without spawning."""
    return _plan_checking(
        repo_root,
        feature_id,
        lane_id,
        profile,
        role=_REVIEWER_ROLE,
        task_fn=lambda _feature_root, facts: _reviewer_task_text(facts),
        command="review",
        label="REVIEW",
        max_turns=max_turns,
        permission_mode=permission_mode,
    )


def plan_spec_gap(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    profile: AgentProfile,
    *,
    max_turns: int = DEFAULT_MAX_TURNS,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
) -> DryRunPlan:
    """Plan a ``spec-gap`` (Spec Gap Analyst, §9.4) leg without spawning."""
    return _plan_checking(
        repo_root,
        feature_id,
        lane_id,
        profile,
        role=_SPEC_GAP_ROLE,
        task_fn=_spec_gap_task_text,
        command="spec-gap",
        label="SPEC-GAP",
        max_turns=max_turns,
        permission_mode=permission_mode,
    )


def plan_fix_run(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    implement_profile: AgentProfile,
    reviewer_profile: AgentProfile,
    spec_gap_profile: AgentProfile,
    *,
    max_turns: int = DEFAULT_MAX_TURNS,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    verify_timeout: float = 300,
) -> DryRunPlan:
    """Plan a ``fix-run`` bookend: preflight the targets/budget, plan the chain.

    The preflight (feature exists, fix-loop budget not exhausted, ≥1 active
    ``request_fix`` issue) is the valuable check; the implement[fix] leg is then
    planned as a temp-dir package (``plan_implement`` shape) and the
    review/spec-gap/verify/collect chain is listed as ``would_run``. No leg
    actually runs, no id is minted, no budget is consumed.

    v0.5 ticket 03: each leg's token source is preflighted (one per role
    default), and the plan reports all three profile names so the operator can
    see the per-leg routing before committing to a real run.
    """
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id} not found under {repo_root}")
    if fix_loop_budget_exhausted(feature_root):
        raise ValueError(
            "fix_loop_budget exhausted; cannot run another request_fix loop "
            "(ADR-0002 D5)"
        )
    targets = _current_request_fix_targets(feature_root)
    if not targets:
        raise ValueError(
            f"no active request_fix issues found under {feature_id}/{ISSUES_DIR}; "
            "fix-run has nothing to target"
        )
    # Each leg's token source must be set - preflight all three role defaults.
    for leg_label, leg_profile in (
        ("implement", implement_profile),
        ("review", reviewer_profile),
        ("spec-gap", spec_gap_profile),
    ):
        _require_token_source(leg_profile)
    budget = fix_loop_budget(feature_root)

    details: dict[str, Any] = {
        "lane_id": lane_id,
        "profile": implement_profile.name,
        "profiles": {
            "implementer": implement_profile.name,
            "reviewer": reviewer_profile.name,
            "spec_gap_analyst": spec_gap_profile.name,
        },
        "target_issue_ids": [t.issue_id for t in targets],
        "fix_loop_budget": dict(budget),
        "verify_timeout": verify_timeout,
        "max_turns": max_turns,
        "would_consume_budget": False,
        "would_mint_ids": ["RUN-NNN x2 (implement[fix] + review) at most"],
        "would_run": [
            "implement[fix] (temp-dir package + spawn)",
            "review (spawn)",
            "spec-gap (spawn)",
            "verify (shell, no model)",
            "collect-issues (deterministic)",
        ],
        "would_write": [
            f"lanes/{lane_id}/implement-result.* (fix run)",
            "feature-status.yml fix_loop_budget.used (only after implement validates)",
        ],
    }
    return DryRunPlan(
        command="fix-run",
        feature_id=feature_id,
        summary=(
            f"FIX-RUN DRY-RUN - would run implement[fix]->review->spec-gap->verify"
            f"->collect for {len(targets)} request_fix issue(s) "
            f"(budget {budget['used']}/{budget['max']}; no run, no budget consumed)"
        ),
        details=details,
    )


# ---------------------------------------------------------------------------
# Deterministic commands.
# ---------------------------------------------------------------------------


def plan_freeze(repo_root: Path, feature_id: str, artifact: str) -> DryRunPlan:
    """Plan a ``freeze``: legality check (unknown / already-frozen / coverage gap), no write.

    Unknown artifact -> ``ValueError`` (exit 1). Already frozen -> reported as
    ``would be refused`` (exit 0), mirroring the real ``FrozenArtifactError``. A
    freeze-gate coverage gap (ADR-0008 D3 - an upstream id not referenced, e.g. a
    REQ missing from every design ``requirement_mapping``) is also reported as
    ``would be refused`` (exit 0). A corrupt precondition (design freeze before
    requirements frozen, or no design promoted to freeze) raises ``ValueError``
    (exit 1), mirroring the real CLI.
    """
    if artifact not in FROZEN_ARTIFACTS:
        raise ValueError(
            f"unknown frozen artifact {artifact!r}; expected one of {FROZEN_ARTIFACTS}"
        )
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id} not found under {repo_root}")
    frozen = frozen_artifacts_status(feature_root)
    already = bool(frozen.get(artifact))
    advance = {"requirements": "design_gate", "design": "task_gate", "tasks": "lane_gate"}
    target = advance.get(artifact)
    details: dict[str, Any] = {
        "artifact": artifact,
        "currently_frozen": already,
    }
    # ADR-0008 D3: the freeze-gate coverage precheck runs BEFORE the already-frozen
    # short-circuit, mirroring the real CLI (which runs coverage before the
    # already-frozen guard inside ``freeze_artifact``) - so dry-run reports the
    # same refusal *reason* the real command would for every case (a hand-edited
    # frozen design with a gap reports "coverage gap", not "already frozen").
    # Stages with no upstream invariant (requirements, lane_graph) return None -
    # no precheck. A corrupt precondition (design freeze before requirements
    # frozen, or no 02-design.json to freeze) raises ValueError - which propagates
    # as exit 1, mirroring the real CLI (a precondition error, not a refusal). A
    # coverage *gap* (uncovered upstream ids) is a legality refusal the real
    # command would reject - reported here as ``would be refused`` and exits 0,
    # the dry-run convention for "this would be refused" (§18.2).
    coverage = freeze_gate_coverage(artifact, feature_root)
    if coverage is not None and not coverage.ok:
        details["would_be_refused"] = True
        details["refusal_reason"] = coverage.refusal_message(artifact, would=True)
        summary = (
            f"FREEZE DRY-RUN - would be REFUSED: {artifact} coverage gap "
            f"({len(coverage.uncovered)} {coverage.upstream_type} uncovered)"
        )
    elif already:
        details["would_be_refused"] = True
        details["refusal_reason"] = (
            f"artifact {artifact!r} is already frozen; use a Change Proposal to "
            f"change it (§4.2)"
        )
        summary = f"FREEZE DRY-RUN - would be REFUSED: {artifact} already frozen"
    else:
        details["would_be_refused"] = False
        details["would_advance_current_gate_to"] = target
        details["would_write"] = ["status/feature-status.yml", "audit.log.{md,json}"]
        summary = (
            f"FREEZE DRY-RUN - would freeze {artifact}"
            + (f" and advance current_gate -> {target}" if target else "")
        )
    return DryRunPlan(
        command="freeze",
        feature_id=feature_id,
        summary=summary,
        details=details,
    )


def plan_triage(
    repo_root: Path,
    feature_id: str,
    issue_id: str,
    action: str,
    reason: str | None,
    by: str,
) -> DryRunPlan:
    """Plan a ``triage``: full legality matrix + reason + budget, no write.

    Precondition failures (unknown disposition, missing feature/issue, unknown
    severity) raise ``ValueError`` (exit 1). A legality refusal (illegal cell,
    disarming-without-reason, exhausted fix-loop budget) is *reported* in the plan
    (``would be refused``) and exits 0 — dry-run triage validates a disposition
    without recording it.
    """
    if action not in DISPOSITIONS:
        raise ValueError(
            f"unknown disposition {action!r}; expected one of {DISPOSITIONS}"
        )
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id} not found under {repo_root}")
    issue_path = feature_root / ISSUES_DIR / f"{issue_id}.json"
    issue = read_json_object(issue_path)
    if issue is None:
        raise ValueError(
            f"issue {issue_id} not found under {feature_id}/{ISSUES_DIR} (§24.2)"
        )
    severity = issue.get("severity")
    if severity not in SEVERITIES:
        raise ValueError(
            f"issue {issue_id} has unknown severity {severity!r}; "
            f"expected one of {SEVERITIES}"
        )

    cell = _matrix_cell(action, severity)
    details: dict[str, Any] = {
        "issue_id": issue_id,
        "severity": severity,
        "action": action,
        "by": by,
    }

    # Legality matrix (ADR-0001 #4).
    if not cell.legal:
        details["would_be_refused"] = True
        details["refusal_reason"] = cell.refusal_reason or "illegal disposition x severity cell"
        return DryRunPlan(
            command="triage",
            feature_id=feature_id,
            summary=f"TRIAGE DRY-RUN - would be REFUSED: {cell.refusal_reason}",
            details=details,
        )
    # Reason-presence for disarming dispositions (ADR-0001 #6).
    if cell.disarms and (reason is None or not reason.strip()):
        msg = (
            f"{action} on {severity} requires a reason (ADR-0001 #6); without "
            "one it is a Decision-free escape hatch"
        )
        details["would_be_refused"] = True
        details["refusal_reason"] = msg
        return DryRunPlan(
            command="triage",
            feature_id=feature_id,
            summary=f"TRIAGE DRY-RUN - would be REFUSED: {msg}",
            details=details,
        )
    # Fix-loop budget (ADR-0002 D5).
    if action == "request_fix" and fix_loop_budget_exhausted(feature_root):
        budget = fix_loop_budget(feature_root)
        msg = (
            f"request_fix is refused because fix_loop_budget is exhausted "
            f"(used={budget['used']}, max={budget['max']}; ADR-0002 D5)"
        )
        details["would_be_refused"] = True
        details["refusal_reason"] = msg
        return DryRunPlan(
            command="triage",
            feature_id=feature_id,
            summary=f"TRIAGE DRY-RUN - would be REFUSED: {msg}",
            details=details,
        )

    # Would succeed.
    details["would_be_refused"] = False
    details["would_mint_ids"] = (
        [f"DEC-NNN ({cell.decision_kind})"] if cell.disarms else []
    )
    details["would_write"] = [
        f"issues/{issue_id}.json (triage state + status -> triaged)",
        f"decisions/DEC-NNN.{{json,md}}" if cell.disarms else "(no Decision)",
        "audit.log.{md,json}",
    ]
    return DryRunPlan(
        command="triage",
        feature_id=feature_id,
        summary=(
            f"TRIAGE DRY-RUN - would apply {action} on {issue_id} "
            f"({severity})"
            + (f" + mint DEC-NNN" if cell.disarms else "")
            + " (no write)"
        ),
        details=details,
    )


def plan_coherence_gate(repo_root: Path, feature_id: str) -> DryRunPlan:
    """Plan a ``coherence-gate``: compute the verdict, write nothing (ADR-0003 D1)."""
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id} not found under {repo_root}")
    compute = compute_coherence(feature_root)
    failed = [str(c["name"]) for c in compute.conditions if not c.get("passed")]
    details = {
        "verdict": compute.verdict,
        "derived_feature_status": "done" if compute.verdict == "pass" else "blocked",
        "conditions": compute.conditions,
        "failed_conditions": failed,
        "issue_count": compute.issue_count,
        "would_mint_ids": [],
        "would_write": [
            "status/feature-status.yml (current_gate=feature_coherence_gate + verdict + feature.status)",
            f"{COHERENCE_DECISION_JSON}",
            "coherence-decision.md",
            "audit.log.{md,json}",
        ],
    }
    return DryRunPlan(
        command="coherence-gate",
        feature_id=feature_id,
        summary=(
            f"COHERENCE-GATE DRY-RUN - would write verdict={compute.verdict} "
            f"(status={details['derived_feature_status']}, "
            f"{len(failed)} failed condition(s))"
        ),
        details=details,
    )


def plan_lane_gate(repo_root: Path, feature_id: str, lane_id: str) -> DryRunPlan:
    """Plan a ``lane-gate``: compute the decision, write nothing (§18.4)."""
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id} not found under {repo_root}")
    if not lane_dir(repo_root, feature_id, lane_id).is_dir():
        raise ValueError(f"lane {lane_id} not found under feature {feature_id}")
    compute = compute_lane_decision(repo_root, feature_id, lane_id)
    decision = compute.decision["decision"]
    failed = [str(c["name"]) for c in compute.conditions if not c.get("passed")]
    details = {
        "decision": decision,
        "conditions": compute.conditions,
        "failed_conditions": failed,
        "blocking_issues": compute.blocking_issues,
        "would_mint_ids": [],
        "would_write": [
            f"lanes/{lane_id}/{LANE_DECISION_JSON}",
            f"lanes/{lane_id}/{LANE_DECISION_MD}",
            "audit.log.{md,json}",
        ],
    }
    return DryRunPlan(
        command="lane-gate",
        feature_id=feature_id,
        summary=(
            f"LANE-GATE DRY-RUN - would write decision={decision} for lane "
            f"{lane_id} ({len(failed)} failed condition(s))"
        ),
        details=details,
    )


def plan_final_report(repo_root: Path, feature_id: str) -> DryRunPlan:
    """Plan a ``final-report``: compute the projection, write nothing (§23.5)."""
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id} not found under {repo_root}")
    compute = compute_final_report(feature_root)
    details = {
        "verdict": compute.verdict,
        "failure_class": compute.failure_class,
        "would_mint_ids": [],
        "would_write": [
            f"{FINAL_REPORT_JSON}",
            f"{FINAL_REPORT_MD}",
        ],
        "audited": False,
    }
    return DryRunPlan(
        command="final-report",
        feature_id=feature_id,
        summary=(
            f"FINAL-REPORT DRY-RUN - would render verdict={compute.verdict} "
            f"failure_class={compute.failure_class}"
        ),
        details=details,
    )


def plan_compare_profiles(
    repo_root: Path, feature_id: str, profile_names: list[str]
) -> DryRunPlan:
    """Plan a ``compare-profiles``: compute the projection, write nothing (§23.5).

    Reuses the pure ``compute_profile_comparison`` so the dry-run exercises the
    full discovery + metric projection (and its §24.2 preconditions) without
    writing the non-canonical products.
    """
    compute = compute_profile_comparison(repo_root, feature_id, profile_names)
    meta = compute.report.get("meta", {})
    profiles_compared = meta.get("profiles_compared", profile_names)
    details = {
        "profiles_compared": profiles_compared,
        "feature_ids": meta.get("feature_ids", []),
        "would_mint_ids": [],
        "would_write": [
            f"projections/{PROFILE_COMPARISON_JSON}",
            f"projections/{PROFILE_COMPARISON_MD}",
        ],
        "audited": False,
    }
    return DryRunPlan(
        command="compare-profiles",
        feature_id=feature_id,
        summary=(
            f"COMPARE-PROFILES DRY-RUN - would project "
            f"profiles={','.join(profiles_compared)}"
        ),
        details=details,
    )


def plan_project_github(
    repo_root: Path, feature_id: str, pr_number: int | None
) -> DryRunPlan:
    """Plan a ``project-github``: non-network preflight + plan, no push (ADR-0006).

    Runs the *non-network* preconditions the operator can fix without touching
    GitHub (feature exists, ``GITHUB_TOKEN`` env name set, ``gh`` on ``PATH``)
    and reports what the run would push — which issues would be *created* vs
    *edited* (read from the mapping), whether the PR comment would post, and the
    mapping it would write. The *network* pre-flight (rate-limit probe, PR
    existence) and the pushes themselves are the expensive/irreversible steps
    dry-run skips, so they are listed as ``would_run`` rather than executed.
    """
    # The pure seam: create-vs-edit split + effective PR (read from the mapping),
    # so the dry-run faithfully previews D2 idempotency (already-pushed issues
    # show as edits, not creates). Raises ValueError on a missing feature or a
    # corrupt mapping (fail loud, §24.2) -> surfaced as a clean error: + exit 1.
    plan = compute_github_plan(repo_root, feature_id, pr_number)
    # Non-network preflight (ADR-0004: skip the network steps).
    token_set = os.environ.get(GITHUB_TOKEN_ENV) not in (None, "")
    gh_on_path = _which_gh()
    preflight_ok = token_set and gh_on_path
    has_pr = plan.pr_number is not None
    would_write = [
        "projections/github/mapping.json "
        "(ISSUE-NNN -> GH number; feature -> PR; non-deterministic canonical write)",
    ]
    if has_pr:
        would_write.append("PR comment (final-report.md) — create or update")
    details: dict[str, Any] = {
        "pr_number": plan.pr_number,
        "issues_total": plan.issues_total,
        "would_create": plan.would_create,
        "would_edit": plan.would_edit,
        "has_pr_comment": has_pr,
        "github_token_set": token_set,
        "gh_on_path": gh_on_path,
        "preflight_non_network_ok": preflight_ok,
        "would_run": [
            "preflight: GITHUB_TOKEN set, gh on PATH, rate-limit probe, "
            "PR exists (if --pr) — note: PR existence is a network check this "
            "dry-run does NOT perform; a bad --pr surfaces only on the real run",
            "gh issue create/edit per ISSUE-NNN",
            "gh pr comment (if --pr)",
        ],
        "would_mint_ids": [],
        "would_write": would_write,
        "audited": False,
    }
    if not preflight_ok:
        missing = []
        if not token_set:
            missing.append(f"{GITHUB_TOKEN_ENV} env var")
        if not gh_on_path:
            missing.append("`gh` on PATH")
        details["would_be_refused"] = True
        details["refusal_reason"] = (
            "pre-flight would fail: missing " + " + ".join(missing)
        )
        summary = (
            f"PROJECT-GITHUB DRY-RUN - would be REFUSED: pre-flight missing "
            + " + ".join(missing)
        )
    else:
        details["would_be_refused"] = False
        summary = (
            f"PROJECT-GITHUB DRY-RUN - would push {plan.issues_total} issue(s) "
            f"({len(plan.would_create)} create, {len(plan.would_edit)} edit; "
            f"{'with' if has_pr else 'no'} PR comment; no network call)"
        )
    return DryRunPlan(
        command="project-github",
        feature_id=feature_id,
        summary=summary,
        details=details,
    )


def _which_gh() -> bool:
    """Whether ``gh`` is on ``PATH`` (no spawn). Local so dry_run imports no
    private name from ``github_projection`` (the compute seam is the boundary)."""
    from shutil import which

    return which("gh") is not None


# ---------------------------------------------------------------------------
# Rendering.
# ---------------------------------------------------------------------------


def _render_value(value: Any) -> str:
    """Render a detail value for stdout (strings bare, else compact JSON)."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def render_plan(plan: DryRunPlan) -> str:
    """Render a ``DryRunPlan`` as the human-readable stdout block.

    One headline line, then ``key: value`` per detail (compact JSON for
    non-strings), matching the audit-log markdown rendering convention so the
    plan reads like the rest of the tool's output.
    """
    lines = [plan.summary, ""]
    for key, value in plan.details.items():
        lines.append(f"- {key}: {_render_value(value)}")
    lines.append("")
    return "\n".join(lines)
