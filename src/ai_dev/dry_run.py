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
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ai_dev.checking_legs import (
    ISSUES_OUTPUT_SCHEMA,
    _REVIEWER_ROLE,
    _SPEC_GAP_ROLE,
    _require_frozen,
    _reviewer_task_text,
    _spec_gap_task_text,
    read_implement_run_facts,
)
from ai_dev.coherence_gate import COHERENCE_DECISION_JSON, compute_coherence
from ai_dev.final_report import FINAL_REPORT_JSON, FINAL_REPORT_MD, compute_final_report
from ai_dev.fix_run import _current_request_fix_targets
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
from ai_dev.profiles import AgentProfile, token_source_var
from ai_dev.run_prepare import (
    ALLOWED_FILES_FILE,
    TASK_PACKAGE_FILE,
    write_input_package_to,
)
from ai_dev.run_wrapper import (
    DEFAULT_MAX_TURNS,
    DEFAULT_PERMISSION_MODE,
    build_cli_flags,
    build_prompt,
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
) -> list[str]:
    """Build the exact ``claude -p`` argv a real run would spawn (§11.1).

    Reuses ``build_prompt`` + ``build_cli_flags`` verbatim, against the would-be
    run root (the temp dir for prepare-style commands, the real run dir for
    ``run-headless``). The binary is the profile-declared ``cli`` name — the
    wrapper's ``shutil.which`` resolution is the spawn-time concern, not the
    plan's.

    The ``--settings`` flag references the would-be ``.run-settings.json`` path
    (the path a real spawn writes, ``run_wrapper._RUN_SETTINGS``) — but the file
    is **not** materialised. Dry-run writes nothing (glossary pin ``dry-run``),
    so for ``run-headless`` (whose run root is the *real* ``runs/RUN-NNN/``) the
    feature tree stays byte-for-byte unchanged. The argv is still faithful: the
    path is what the spawn would pass.
    """
    settings_path = run_root / OUTPUT_DIR / ".run-settings.json"
    prompt = build_prompt(run_id, run_root)
    flags = build_cli_flags(settings_path, max_turns, permission_mode)
    return [profile.cli, "-p", prompt, *flags]


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


def _require_token_source(profile: AgentProfile) -> str:
    """§24.2: the token source name must be set before a run can be planned."""
    source = token_source_var(profile)
    if source is None:
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
        "invocation": invocation,
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


def _plan_checking(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    profile: AgentProfile,
    *,
    role: str,
    task_fn: Any,
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
    task_text = task_fn(facts)
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
        task_fn=_reviewer_task_text,
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
    profile: AgentProfile,
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
    _require_token_source(profile)
    budget = fix_loop_budget(feature_root)

    details: dict[str, Any] = {
        "lane_id": lane_id,
        "profile": profile.name,
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
    """Plan a ``freeze``: legality check (unknown / already-frozen), no write.

    Unknown artifact → ``ValueError`` (exit 1). Already frozen → reported as
    ``would be refused`` (exit 0), mirroring the real ``FrozenArtifactError``.
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
    if already:
        details["would_be_refused"] = True
        details["refusal_reason"] = (
            f"artifact {artifact!r} is already frozen; use a Change Proposal to "
            f"change it (§4.2)"
        )
        summary = (
            f"FREEZE DRY-RUN - would be REFUSED: {artifact} already frozen"
        )
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
