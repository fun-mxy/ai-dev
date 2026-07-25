"""``ai-dev`` console entry point — the deterministic Python runtime (spec §25.3).

v0.0 surface:

* ``ai-dev create-feature-run "<intent>"`` (ticket 01)
* ``ai-dev freeze <FEATURE-NNN> <artifact>`` (ticket 04) — the deterministic,
  model-free entry point for the §4.2 freeze operation. Because the only way to
  flip a frozen flag is this command (or an equivalent runtime call into
  ``status.freeze_artifact``), no model ever writes canonical freeze state
  (§4.3 cardinal rule).

v0.1 additions:

* ``ai-dev show-profile <name>`` (run-adapter ticket 01) - load and display a
  resolved agent profile (§10.1). The token value is never printed: the output
  carries the source/target variable *names* plus a redacted placeholder
  (§10.2, invariant #11). Exits non-zero when the profile is missing or its
  token source is unset (§24.2 fail loud).

* ``ai-dev prepare-run <FEATURE> --role <role> --task <task> [--allowed-file PATH ...]``
  (run-adapter ticket 02, extended by ticket 05) - allocate ``RUN-NNN`` under the
  feature run's ``runs/`` and write the §12.2 input package. The run id is minted
  by the deterministic stable-id allocator (no model involvement), and printed to
  stdout. ``--allowed-file`` (repeatable) declares the task-specific RUN-relative
  paths the run may write, appended to the mandatory-output seed in
  ``allowed-files.txt`` so the §14.2 boundary check passes on a real run that
  writes workspace files (ticket 05 integration seam). Exits non-zero when the
  feature run is missing, ``--role`` / ``--task`` is empty, or an
  ``--allowed-file`` entry is blank (§24.2).

* ``ai-dev run-headless <FEATURE> <RUN-ID> [--profile cc-glm52]`` (run-adapter
  ticket 03) - run a prepared ``RUN-NNN`` headless via a profile and capture it:
  isolate the child env (§10.3), invoke ``claude -p`` with the §11.1 hard flags,
  capture stdout/stderr, compute ``changed_files`` (§13.2), and write
  ``metadata.json``. The token is read from the environment by source name and
  never persisted (§10.2). Exits ``0`` on a successful capture - including a
  non-zero claude exit, which is a captured run failure, not a wrapper failure
  (the §14 ``validate-run`` decides PASS/FAIL) - and ``1`` when the profile
  cannot load or the run cannot start (missing token / run directory, §24.2).

* ``ai-dev validate-run <FEATURE> <RUN-ID>`` (run-adapter ticket 04) - run the
  §14 deterministic three-check validation (schema §14.1 + file boundary §14.2
  + frozen artifact §14.3) against a captured run and print ``VALIDATE PASS`` /
  ``VALIDATE FAIL`` with a readable issue list. Pure and side-effect-free beyond
  one ``validate`` audit record: it reads what ``run-headless`` wrote and judges
  it, spawning no subprocess. Exits ``0`` on PASS, ``1`` on FAIL or when the run
  directory is missing (§24.2 fail loud). The §14.1/§24.3 retry-once is a
  library seam (``validate_with_retry``), not this command - the standalone
  ``validate-run`` re-checks an already-captured run.

v0.2 additions (the implement -> review/gap -> verify -> bundle -> lane-gate
loop, §26.3):

* ``ai-dev implement <FEATURE> <LANE> [--profile cc-glm52]`` (ticket 01) - run
  the Implementer leg: prepare from frozen artifacts -> run headless -> validate
  -> ``proposed_done`` writeback gated on validation -> lane ``implement-result``.

* ``ai-dev review <FEATURE> <LANE>`` / ``ai-dev spec-gap <FEATURE> <LANE>``
  (ticket 02) - run the Code Reviewer (§9.3) / Spec Gap Analyst (§9.4) legs:
  build the issues-schema input package -> run headless -> validate -> lane
  ``review-report`` / ``spec-gap-report``. The checking legs write no canonical
  status (§4.3).

* ``ai-dev verify <FEATURE> <LANE> [--timeout 300]`` (ticket 03) - run the shell
  Verifier leg (§9.5): execute the lane's declared verify command set
  (pytest/mypy/build, from the frozen lane-graph's ``verification_commands``)
  against the implement run's workspace, one by one, and roll up a lane
  ``verification-report.{md,json}`` with a pass/fail verdict. **Deterministic
  shell - no ``--profile``, no token, no model** (a non-agent run kind, §9.5).
  The verifier emits a report, NOT ``issues[]`` (§9.5 vs §15); the verdict is an
  independent condition the §18.4 lane gate consumes. Exits ``0`` on verdict
  pass, ``1`` when any command failed or the leg cannot start (unfrozen, no
  verify commands declared, no implement-result - §24.2 fail loud).

v0.3 additions (Human Triage + fix loop, ADR-0001/0002):

* ``ai-dev triage <FEATURE> --issue ISSUE-NNN --disposition <d> [--reason ...]
  [--by human]`` (ticket 05) - the deterministic Human-Triage write chokepoint
  (ADR-0001 #8). Pure - no profile, no token, no model. Writes the disposition
  as the ``triage`` state object on ``issues/ISSUE-NNN.json`` (not the bundle,
  not a Decision), enforcing the disposition x severity legality matrix, the
  reason-presence rule for disarming dispositions, the promotion rule
  (``override`` x P1 / ``reject`` x {P0, P1} -> ``DEC-NNN``), the P0 ``override``
  write-layer refusal, the fix-loop budget guard for ``request_fix``, and the
  ``request_change_proposal`` clean deferral. Drives the issue ``status`` to
  ``triaged`` via the ticket-03 helper. Exits ``0`` on a successful apply, ``1``
  when the disposition is refused (illegal cell, missing reason, or exhausted
  fix-loop budget - a clean ``error:`` line, issue stays untriaged) or a
  precondition is missing (§24.2 fail loud).

* ``ai-dev fix-run <FEATURE> <LANE> [--profile cc-glm52]`` (ticket 07) - run one
  bounded fix-loop bookend for all active ``request_fix`` issues: implement[fix]
  -> review -> spec-gap -> verify -> collect. The feature ``fix_loop_budget`` is
  consumed only after the implement leg produces a §14-validated result; the
  driver marks targeted issues with ``fix_targeted_in_run`` and stops before the
  mandatory human re-triage step.

* ``ai-dev coherence-gate <FEATURE>`` (ticket 08) - the terminal §18.5 gate
  (ADR-0003 D1/D2/D4). Deterministic - no profile, no token, no model. Checks
  the three D1 input conditions (status consistency; all P0/P1 resolved or
  disarmed with the lane gate PASSed; every disarmed blocker has a DEC-NNN
  file), then atomically writes ``current_gate=feature_coherence_gate`` +
  ``verdict`` (pass/fail) + derived ``feature.status`` (done/blocked) on
  ``feature-status.yml`` and a ``coherence-decision.{md,json}`` double product.
  ``verdict`` is mutable (re-coherence overwrites). Exits ``0`` on pass, ``1``
  on fail or fail-loud missing/corrupt prerequisites (§24.2).

* ``ai-dev final-report <FEATURE>`` (ticket 09) - the §23.5 step-21 projection
  writer (ADR-0003 D5/D6/D7). Deterministic - no profile, no token, no model.
  Reads the coherence ``verdict`` (never writes it) and the feature-run
  artifacts and renders ``final-report.json`` (canonical, keyed by §2.1's five
  audit questions + ``meta`` + failure-shape) and a deterministic
  ``final-report.md`` skeleton from that JSON. A pure, non-audited render: it
  touches no canonical state and appends no audit event, so it is independently
  re-runnable. ``verdict == null`` (coherence has not run) fail-loud refuses
  (§24.2 / D7-c). Exits ``0`` on a successful render for either verdict, ``1``
  on fail-loud missing/corrupt prerequisites.

Structured as a subcommand dispatcher so later tickets add commands
(``allocate-id``, ``append-audit``, …) without disturbing the existing ones.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from ai_dev.checking_legs import CheckingLegResult, run_reviewer_leg, run_spec_gap_leg
from ai_dev.coherence_gate import CoherenceResult, evaluate_coherence_gate
from ai_dev.dry_run import (
    DryRunPlan,
    plan_allocate_id,
    plan_coherence_gate,
    plan_compare_profiles,
    plan_final_report,
    plan_fix_run,
    plan_freeze,
    plan_project_github,
    plan_implement,
    plan_generate_design,
    plan_generate_requirements,
    plan_generate_tasks,
    plan_lane_gate,
    plan_render,
    plan_review,
    plan_run_headless,
    plan_spec_gap,
    plan_triage,
    render_plan,
)
from ai_dev.feature_run import create_feature_run
from ai_dev.feature_ids import ID_TYPES, allocate_id
from ai_dev.final_report import FinalReportResult, generate_final_report
from ai_dev.fix_run import FixRunResult, run_fix_run
from ai_dev.github_projection import (
    GithubProjectionResult,
    project_github,
)
from ai_dev.implement_leg import run_implementer_leg
from ai_dev.issue_bundle import ISSUES_DIR, IssueBundleResult, collect_issue_bundle
from ai_dev.lane_gate import LaneDecisionResult, evaluate_lane_gate
from ai_dev.coverage import freeze_gate_coverage
from ai_dev.paths import feature_dir, features_dir, require_feature_root, run_dir, runs_dir
from ai_dev.promote import (
    FrozenArtifactWriteError,
    RENDERABLE_ARTIFACTS,
    RenderResult,
    render_artifact,
)
from ai_dev.planner_leg import (
    PlannerLegResult,
    run_generate_design,
    run_generate_requirements,
    run_generate_tasks,
)
from ai_dev.profile_comparison import (
    PROFILE_COMPARISON_JSON,
    PROFILE_COMPARISON_MD,
    ProfileComparisonResult,
    generate_profile_comparison,
)
from ai_dev.profiles import (
    AgentProfile,
    ProfileError,
    ROLE_IMPLEMENTER,
    ROLE_PLANNER,
    ROLE_REVIEWER,
    ROLE_SPEC_GAP_ANALYST,
    load_profile,
    render_profile,
    resolve_profile_name,
    token_source_var,
)
from ai_dev.query import (
    AuditRecordView,
    FeatureStatusView,
    FeatureSummary,
    LaneDecisionSummary,
    list_features,
    read_audit_timeline,
    show_feature_status,
)
from ai_dev.run_prepare import prepare_run
from ai_dev.run_wrapper import DEFAULT_MAX_TURNS, DEFAULT_PERMISSION_MODE, run_headless
from ai_dev.shell_verifier import CommandResult, run_verifier
from ai_dev.status import (
    FROZEN_ARTIFACTS,
    FrozenArtifactError,
    freeze_artifact,
    record_agent_profile,
)
from ai_dev.triage import DISPOSITIONS, TriageRefusedError, TriageResult, apply_triage
from ai_dev.validate import ValidationIssue, validate_run

# v0.4 ticket 02: the canonical ``origin`` driver tag stamped on every audit
# event this CLI emits (threaded explicitly into each leg/driver below). A direct
# primitive invocation is ``cli``; the agent commands carry their leg identity so
# the audit log answers "which driver triggered this run" without inference.
ORIGIN_CLI = "cli"
ORIGIN_IMPLEMENT_LEG = "implement-leg"
ORIGIN_REVIEW_LEG = "review-leg"
ORIGIN_SPEC_GAP_LEG = "spec-gap-leg"
ORIGIN_VERIFIER = "verifier"
ORIGIN_FIX_RUN_DRIVER = "fix-run-driver"
ORIGIN_PLANNER_LEG = "planner-leg"


def _print_validation_issues(issues: Sequence[ValidationIssue]) -> None:
    """Print one readable line per §14 validation issue.

    Shared by ``validate-run`` and ``implement`` so the issue format stays in
    one place (``[severity] check: message (path=...)`` per issue).
    """
    for issue in issues:
        line = f"  - [{issue.severity}] {issue.check}: {issue.message}"
        if issue.path:
            line += f" (path={issue.path})"
        print(line)


def _render_error(exc: BaseException, *, hint: str | None = None) -> None:
    """Render one clean, actionable ``error:`` line to stderr (§26.5).

    Every ``_run_*`` failure surfaces through this helper so the ``error:``
    prefix, stderr destination, and optional ``hint:`` line stay identical
    across commands (文案同构). Takes the exception itself (per the ticket's
    ``_render_error(exc, *, hint=None)`` contract) so callers do not each
    re-spell ``str(exc)`` and the helper may later derive hints by type. The
    hint is the "next step" / "did you mean" guidance that turns a bare problem
    statement into an actionable one; it is omitted when there is nothing useful
    to add.
    """
    message = str(exc) or exc.__class__.__name__
    print(f"error: {message}", file=sys.stderr)
    if hint:
        print(f"  hint: {hint}", file=sys.stderr)


def _load_profile_or_render(repo_root: Path, name: str) -> AgentProfile | None:
    """Load a profile by name; on ``ProfileError``, render it and return ``None``.

    The cli's shared profile-load tail: handlers that need a loaded
    ``AgentProfile`` wrap ``load_profile`` in the same try/except (no hint - a
    missing profile file is not a did-you-mean situation). Returning ``None``
    lets the caller exit 1 without re-spelling the except block. Callers that
    load several profiles in one try (``fix-run``) keep their own block - this
    helper is for the single-load case.
    """
    try:
        return load_profile(repo_root, name)
    except ProfileError as exc:
        _render_error(exc)
        return None


def _exit_value_error(
    repo_root: Path, feature_id: str, exc: BaseException, *, run_id: str | None = None
) -> int:
    """Render a precondition ``ValueError`` with a did-you-mean hint; return 1.

    The cli's shared exit-1 tail: a §24.2 precondition failure (missing
    feature/lane/run, unfrozen artifact, unknown id) surfaces as one
    ``error:`` line plus a ``_lookup_hint`` did-you-mean, exit 1. Centralising
    it keeps the hint's call shape in one place across the handler except
    blocks. ``run_id`` is passed only by run-scoped commands (``validate-run``)
    so their hint can point at a missing run rather than a missing feature.
    """
    _render_error(exc, hint=_lookup_hint(repo_root, feature_id, run_id))
    return 1


def _existing_ids(parent: Path, prefix: str) -> list[str]:
    """Sorted names of existing ``<prefix>-NNN`` children under ``parent``.

    Matches dirs first (features, runs) and falls back to ``<prefix>*.json``
    files (issues live as ``ISSUE-NNN.json`` under ``issues/``). Returns ``[]``
    when the parent does not exist yet (a fresh repo).
    """
    if not parent.is_dir():
        return []
    names = {
        p.name
        for p in parent.iterdir()
        if (p.is_dir() or p.suffix == ".json") and p.name.startswith(prefix)
    }
    return sorted(n.removesuffix(".json") for n in names)


def _candidate_hint(requested: str, candidates: list[str]) -> str | None:
    """A "did you mean ..." hint over candidate ids, or ``None`` when empty.

    A close match (difflib) is named first; the full candidate list always
    follows so the operator sees what actually exists.
    """
    if not candidates:
        return None
    matches = difflib.get_close_matches(requested, candidates, n=3, cutoff=0.4)
    if matches:
        return f"did you mean {', '.join(matches)}? existing: {', '.join(candidates)}"
    return f"existing: {', '.join(candidates)}"


def _lookup_hint(
    repo_root: Path, feature_id: str | None, run_id: str | None = None
) -> str | None:
    """Actionable not-found hint for a feature/run lookup failure, else ``None``.

    Fires only when the referenced feature (or, given a feature that exists, the
    referenced run) genuinely does not exist on disk - so an unrelated
    ``ValueError`` (e.g. "lane not frozen") does not get a misleading candidate
    list appended.
    """
    if feature_id is None:
        return None
    if not feature_dir(repo_root, feature_id).is_dir():
        return _candidate_hint(
            feature_id, _existing_ids(features_dir(repo_root), "FEATURE-")
        )
    if run_id is not None and not run_dir(repo_root, feature_id, run_id).is_dir():
        return _candidate_hint(
            run_id, _existing_ids(runs_dir(repo_root, feature_id), "RUN-")
        )
    return None


def _lookup_hint_from_args(args: argparse.Namespace) -> str | None:
    """``_lookup_hint`` driven by a parsed CLI namespace (the top-level handler).

    Reads ``feature_id`` / ``run_id`` / ``repo_root`` off the namespace so the
    top-level ``except`` can attach a not-found hint to any uncaught error
    without each ``_run_*`` repeating the wiring.
    """
    feature_id = getattr(args, "feature_id", None)
    if feature_id is None:
        return None
    run_id = getattr(args, "run_id", None)
    return _lookup_hint(Path(getattr(args, "repo_root", ".")), feature_id, run_id)


# The ADR-0001 #4 disposition x severity legality matrix, in one readable line
# for triage refusal hints (P0 only reject disarms; P1 override/reject need a
# reason; P2/P3 accept everything). The single source of truth for which cells
# are legal lives in ``triage._matrix_cell``; this is the operator-facing gloss.
_TRIAGE_LEGAL_MATRIX = (
    "legal cells: P0 -> reject (needs --reason) or request_fix; "
    "P1 -> override/reject (need --reason), request_fix, defer(n/a); "
    "P2/P3 -> any disposition"
)


def _triage_hint(
    exc: BaseException,
    *,
    repo_root: Path,
    feature_id: str,
    issue_id: str,
    reason: str | None,
) -> str | None:
    """Actionable hint for a triage refusal / bad input (§26.5).

    A ``TriageRefusedError`` names the legal disposition x severity cells and
    reminds the operator of ``--reason``; a plain ``ValueError`` (unknown issue
    / unknown feature) points at what does exist - issue ids when the feature
    is present, feature ids when it is not.
    """
    if isinstance(exc, TriageRefusedError):
        hint = _TRIAGE_LEGAL_MATRIX
        if reason is None or not reason.strip():
            hint += "; re-run with --reason \"...\" for a disarming disposition"
        return hint
    if feature_dir(repo_root, feature_id).is_dir():
        # Feature exists -> the bad reference is the issue id.
        return _candidate_hint(
            issue_id,
            _existing_ids(feature_dir(repo_root, feature_id) / ISSUES_DIR, "ISSUE-"),
        )
    return _lookup_hint(repo_root, feature_id)


# --- per-command argument declarations -------------------------------------
# Each ``_args_*`` adds one subcommand's positional/optional args to the parser
# built from its ``Command`` row. ``_build_parser`` loops over ``COMMANDS`` and
# calls the row's ``add_args`` - so the arg surface lives next to the run/plan
# handlers, not in a separate hand-maintained block. Help strings are the
# user-facing contract; keep them verbatim when editing.


def _add_run_flags(
    sub: argparse.ArgumentParser,
    *,
    profile_default: str | None,
    profile_help: str,
    per_call: bool = False,
) -> None:
    """Add the ``--profile``/``--max-turns``/``--permission-mode`` trio shared by
    ``run-headless`` and the agent/``fix-run`` commands. ``per_call`` switches the
    max-turns/permission-mode help to ``fix-run``'s "each headless agent call"
    wording (it spawns three legs, not one)."""
    sub.add_argument("--profile", default=profile_default, help=profile_help)
    if per_call:
        sub.add_argument(
            "--max-turns",
            type=int,
            default=DEFAULT_MAX_TURNS,
            help="Bounded --max-turns for each headless agent call (default: 12).",
        )
        sub.add_argument(
            "--permission-mode",
            default=DEFAULT_PERMISSION_MODE,
            help="claude --permission-mode for each headless agent call "
            "(default: bypassPermissions).",
        )
    else:
        sub.add_argument(
            "--max-turns",
            type=int,
            default=DEFAULT_MAX_TURNS,
            help="Bounded --max-turns for the headless call (default: 12).",
        )
        sub.add_argument(
            "--permission-mode",
            default=DEFAULT_PERMISSION_MODE,
            help="claude --permission-mode (default: bypassPermissions; the wrapper "
            "enforces the file boundary post-hoc, §14.2).",
        )


def _args_create_feature_run(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("intent", help="The original user intent text to record.")


def _args_freeze(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("feature_id", help="The FEATURE-NNN id of the run to update.")
    sub.add_argument(
        "artifact",
        choices=FROZEN_ARTIFACTS,
        help="Which frozen artifact to flip (one of the §4.2 four).",
    )


def _args_render(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "feature_id",
        help="The FEATURE-NNN whose canonical .json a human directly edited.",
    )
    sub.add_argument(
        "artifact",
        choices=RENDERABLE_ARTIFACTS,
        help="Which artifact's mirror to re-render (requirements / design / "
        "tasks; lane_graph has no md mirror).",
    )


def _args_allocate_id(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "feature_id", help="The FEATURE-NNN whose per-type id counter to bump."
    )
    sub.add_argument(
        "id_type",
        choices=ID_TYPES,
        help="Which §5.2 stable-id type to allocate (REQ / AC / DES / TASK / …).",
    )


def _args_show_profile(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("name", help="The profile name to resolve (e.g. cc-glm52).")


def _args_list_features(sub: argparse.ArgumentParser) -> None:
    # No command-specific args; the shared --repo-root/--json parents suffice.
    pass


def _args_show_status(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("feature_id", help="The FEATURE-NNN id to inspect.")


def _args_log(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "feature_id", help="The FEATURE-NNN id whose audit timeline to print."
    )


def _args_prepare_run(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("feature_id", help="The FEATURE-NNN id to prepare the run under.")
    sub.add_argument(
        "--role",
        required=True,
        help="The role for this run (e.g. Implementer, Reviewer, Spec-Gap).",
    )
    sub.add_argument(
        "--task",
        required=True,
        help="The task text for this run (written verbatim into task-package.md).",
    )
    sub.add_argument(
        "--allowed-file",
        action="append",
        default=[],
        metavar="PATH",
        help="A RUN-relative path the run may create or modify (§14.2), in "
        "addition to output/result.json and output/result.md. Repeatable: "
        "--allowed-file workspace/hello.py --allowed-file workspace/util.py. "
        "Declare every task-specific workspace file so validate-run's boundary "
        "check passes (ticket 05 integration seam).",
    )


def _args_run_headless(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("feature_id", help="The FEATURE-NNN id the run lives under.")
    sub.add_argument("run_id", help="The RUN-NNN id to invoke.")
    _add_run_flags(
        sub,
        profile_default="cc-glm52",
        profile_help="Agent profile to invoke (default: cc-glm52, the v0 "
        "recommended profile, §23.4).",
    )


def _args_validate_run(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("feature_id", help="The FEATURE-NNN id the run lives under.")
    sub.add_argument("run_id", help="The RUN-NNN id to validate.")


def _args_generate(
    sub: argparse.ArgumentParser, *, feature_help: str, feedback_help: str
) -> None:
    """Shared argsetup for the planner generate-* commands (v0.6 tickets 02-04).

    Each planner leg takes the same surface: a ``feature_id`` positional, an
    optional ``--feedback`` refinement note (ADR-0008 D4), and the shared run
    flags. Only the two help strings differ per stage, so they are the parameters;
    the planner ``profile_help`` is one source here, not three copies.
    """
    sub.add_argument("feature_id", help=feature_help)
    sub.add_argument("--feedback", default=None, help=feedback_help)
    _add_run_flags(
        sub,
        profile_default=None,
        profile_help="Agent profile to invoke (default: role_defaults[planner] in "
        "agent-profiles.yml; --profile always overrides, no allowed-set, no "
        "refusal).",
    )


def _args_generate_requirements(sub: argparse.ArgumentParser) -> None:
    _args_generate(
        sub,
        feature_help="The FEATURE-NNN whose intent (00-intent.md) the Planner "
        "elaborates into a requirements proposal.",
        feedback_help="Human refinement note carried into the Planner input package "
        "(ADR-0008 D4). Re-run with --feedback to refine; promote overwrites the "
        "unfrozen 01-requirements until you freeze it.",
    )


def _args_generate_design(sub: argparse.ArgumentParser) -> None:
    _args_generate(
        sub,
        feature_help="The FEATURE-NNN whose frozen requirements "
        "(01-requirements.json) the Planner designs against. Requirements must be "
        "frozen first.",
        feedback_help="Human refinement note carried into the Planner input package "
        "(ADR-0008 D4). Re-run with --feedback to refine; promote overwrites the "
        "unfrozen 02-design until you freeze it.",
    )


def _args_generate_tasks(sub: argparse.ArgumentParser) -> None:
    _args_generate(
        sub,
        feature_help="The FEATURE-NNN whose frozen requirements "
        "(01-requirements.json) AND design (02-design.json) the Planner tasks "
        "against. Both must be frozen first.",
        feedback_help="Human refinement note carried into the Planner input package "
        "(ADR-0008 D4). Re-run with --feedback to refine; promote overwrites the "
        "unfrozen 03-tasks until you freeze it.",
    )


def _args_implement(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "feature_id", help="The FEATURE-NNN id whose tasks/lane-graph are frozen."
    )
    sub.add_argument(
        "lane_id", help="The LANE-NNN id to implement (must be in 04-lane-graph.yml)."
    )
    _add_run_flags(
        sub,
        profile_default=None,
        profile_help="Agent profile to invoke (default: role_defaults[implementer] "
        "in agent-profiles.yml, ticket 03; --profile always overrides, no "
        "allowed-set, no refusal).",
    )


def _args_review(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "feature_id", help="The FEATURE-NNN id whose lane has an implement-result."
    )
    sub.add_argument(
        "lane_id", help="The LANE-NNN id to review (must have an implement-result)."
    )
    _add_run_flags(
        sub,
        profile_default=None,
        profile_help="Agent profile to invoke (default: role_defaults[reviewer] in "
        "agent-profiles.yml, ticket 03; --profile always overrides, no "
        "allowed-set, no refusal).",
    )


def _args_spec_gap(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "feature_id", help="The FEATURE-NNN id whose lane has an implement-result."
    )
    sub.add_argument(
        "lane_id",
        help="The LANE-NNN id to gap-analyse (must have an implement-result).",
    )
    _add_run_flags(
        sub,
        profile_default=None,
        profile_help="Agent profile to invoke (default: role_defaults[spec_gap_analyst] "
        "in agent-profiles.yml, ticket 03; --profile always overrides, no "
        "allowed-set, no refusal).",
    )


def _args_verify(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "feature_id",
        help="The FEATURE-NNN id whose lane has an implement-result to verify.",
    )
    sub.add_argument(
        "lane_id",
        help="The LANE-NNN id to verify (must declare verification_commands).",
    )
    sub.add_argument(
        "--timeout",
        type=float,
        default=300,
        help="Per-command timeout in seconds (default: 300; a hung command is "
        "recorded as a verification failure, not raised, §24.1).",
    )


def _args_collect_issues(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "feature_id", help="The FEATURE-NNN id whose lane has checking reports."
    )
    sub.add_argument(
        "lane_id", help="The LANE-NNN id whose checking reports should be collected."
    )


def _args_lane_gate(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "feature_id",
        help="The FEATURE-NNN id whose lane has implement/verify/bundle artifacts.",
    )
    sub.add_argument("lane_id", help="The LANE-NNN id whose gate should be evaluated.")


def _args_coherence_gate(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "feature_id",
        help="The FEATURE-NNN id whose lane gate has passed and is ready for "
        "the final coherence verdict.",
    )


def _args_final_report(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "feature_id",
        help="The FEATURE-NNN id whose coherence verdict should be projected "
        "into the final report.",
    )


def _args_triage(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "feature_id",
        help="The FEATURE-NNN id whose issues/ holds the issue to triage.",
    )
    sub.add_argument(
        "--issue",
        required=True,
        metavar="ISSUE-NNN",
        help="The issue id whose disposition is being written (e.g. ISSUE-001).",
    )
    sub.add_argument(
        "--disposition",
        required=True,
        choices=DISPOSITIONS,
        help="The Human-Triage disposition (§16): accept | reject | defer | "
        "override | request_fix | request_change_proposal.",
    )
    sub.add_argument(
        "--reason",
        default=None,
        help="Recorded rationale. Required for override (P1) and reject on "
        "P0/P1 (ADR-0001 #6); optional otherwise.",
    )
    sub.add_argument(
        "--by",
        default="human",
        help="Who applied the triage (default: human; models may only propose).",
    )


def _args_fix_run(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "feature_id",
        help="The FEATURE-NNN id whose request_fix issues should be targeted.",
    )
    sub.add_argument(
        "lane_id",
        help="The LANE-NNN id to run through implement/review/spec-gap/verify/collect.",
    )
    _add_run_flags(
        sub,
        profile_default=None,
        profile_help="Agent profile to invoke for implement/review/spec-gap "
        "(default: each leg's role_defaults entry, ticket 03; --profile, if "
        "given, overrides all three legs - no allowed-set, no refusal).",
        per_call=True,
    )
    sub.add_argument(
        "--verify-timeout",
        type=float,
        default=300,
        help="Per-command verifier timeout in seconds (default: 300).",
    )


def _args_compare_profiles(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "feature_id",
        help="The anchor FEATURE-NNN (one of the two compared runs; the "
        "projection lands in its projections/ dir).",
    )
    sub.add_argument(
        "--profiles",
        required=True,
        help="Exactly two comma-separated profile names to compare, e.g. "
        "cc-glm52,codex-default. Each is matched to the intent-sibling "
        "feature-run whose implementer used it.",
    )


def _args_project_github(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "feature_id",
        help="The FEATURE-NNN whose canonical issues/ + final-report to project.",
    )
    sub.add_argument(
        "--pr",
        type=int,
        default=None,
        metavar="N",
        help="A PR number to comment the final-report on (ADR-0006 D3). Stored "
        "as feature -> PR on first projection; without it projection is "
        "issues-only. The orchestrator never creates the PR - a human does.",
    )


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse parser from the ``COMMANDS`` registry.

    The command surface is ``COMMANDS`` (one row per subcommand). Each row
    carries its ``help_text``, ``add_args`` callback, and ``json`` flag, so
    adding a command is one registry entry - not a separate ``add_parser`` block
    plus a ``_dispatch`` branch plus a ``_DRY_RUN_COMMANDS`` entry. The
    ``--dry-run`` flag attaches automatically when ``plan is not None``
    (side-effect command); read-only commands (``plan is None``) skip it
    (ADR-0004 - a dry-run flag on a no-side-effect command is noise). The table
    is ordered to match the historical ``--help`` listing so the refactor is
    output-neutral.
    """
    parser = argparse.ArgumentParser(
        prog="ai-dev",
        description="Multi-Agent Profile orchestrator (v0 walking skeleton).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print a full traceback on an uncaught error (default: a one-line "
        "`error:` message). Opt-in diagnostics - scripted consumers get stable, "
        "clean output by default (§26.5).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ``--repo-root`` is declared once on this parent and attached to every
    # subparser via ``parents=[...]``: one source of truth, unchanged invocation
    # syntax (every command still takes ``<command> ... --repo-root X``). A
    # second parent carries the read-only commands' shared ``--json`` flag.
    repo_root_parent = argparse.ArgumentParser(add_help=False)
    repo_root_parent.add_argument(
        "--repo-root",
        default=".",
        help="Repository root holding .ai-dev/ (default: current directory).",
    )
    json_parent = argparse.ArgumentParser(add_help=False)
    json_parent.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the human-readable form "
        "(read-only commands only; default: human-readable).",
    )

    for cmd in COMMANDS:
        parents = [repo_root_parent]
        if cmd.json:
            parents.append(json_parent)
        sub = subparsers.add_parser(cmd.name, help=cmd.help_text, parents=parents)
        cmd.add_args(sub)
        if cmd.plan is not None:
            _add_dry_run(sub)

    return parser



def _add_dry_run(subparser: argparse.ArgumentParser) -> None:
    """Add the ``--dry-run`` flag to a side-effect subparser (ADR-0004).

    ``--dry-run`` runs the command's full planning + §24.2 precondition + legality
    check but skips the expensive/irreversible step (claude spawn for agent
    commands; canonical-state write for deterministic commands). Dry-run never
    mints a stable id and writes nothing - including no audit append.
    """
    subparser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan the run without spawning claude or writing canonical state "
        "(ADR-0004). Prints what the command would do and exits 0; never mints "
        "a stable id. Legality refusals are reported (would be refused) rather "
        "than raised; precondition failures still exit 1.",
    )


def _run_dry_plan(planner: "Callable[[], DryRunPlan]") -> int:
    """Run a dry-run planner and print its plan (ADR-0004).

    Wraps the ``plan_*`` call so every dry-run dispatch shares one error shape:
    a §24.2 precondition ``ValueError`` surfaces as a clean ``error:`` line +
    exit 1 (same as the real commands), while a successful plan prints and exits
    0. A *legality refusal* is returned inside the plan (``would be refused``),
    not raised, so it exits 0 - dry-run answers "what would happen?" and "this
    would be refused" is a successful answer.
    """
    try:
        plan = planner()
    except (ValueError, ProfileError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(render_plan(plan), end="")
    return 0


def _run_freeze(repo_root: Path, feature_id: str, artifact: str) -> int:
    """Resolve the feature run, run the freeze-gate coverage precheck, then freeze.

    Returns a process exit code: ``0`` on a successful freeze; ``1`` if the run
    is missing, the artifact is already frozen (§4.2 monotonic), or the
    freeze-gate coverage precheck refuses (ADR-0008 D3 - a planning artifact
    with an upstream coverage invariant may not freeze until every upstream id is
    referenced, e.g. every REQ in some design ``requirement_mapping`` for the
    design gate, §18.2). Other failures propagate (§24.2 fail loud).
    """
    try:
        feature_root = require_feature_root(repo_root, feature_id)
    except ValueError as exc:
        return _exit_value_error(repo_root, feature_id, exc)
    # ADR-0008 D3: coverage-completeness is checked at the freeze action. Stages
    # with no upstream coverage invariant (requirements = root, lane_graph)
    # return None - no precheck. A gap refuses to freeze (no self-heal): the
    # human refines (generate-X --feedback) or routes to Triage. The precheck
    # gates the freeze here (the CLI layer), not inside ``freeze_artifact``: the
    # primitive is a pure low-level writer with no artifact-reading dependency,
    # and the CLI is the sole production freeze path.
    try:
        coverage = freeze_gate_coverage(artifact, feature_root)
    except ValueError as exc:
        # A corrupt precondition (e.g. design freeze before requirements frozen)
        # surfaces as a clean error rather than a traceback.
        return _exit_value_error(repo_root, feature_id, exc)
    if coverage is not None and not coverage.ok:
        _render_error(ValueError(coverage.refusal_message(artifact)))
        return 1
    try:
        freeze_artifact(feature_root, artifact, origin=ORIGIN_CLI)
    except FrozenArtifactError as exc:
        _render_error(exc)
        return 1
    print(f"{feature_id}: froze {artifact}")
    return 0


def _run_render(repo_root: Path, feature_id: str, artifact: str) -> int:
    """Re-render an unfrozen artifact's ``.md`` mirror from its ``.json``
    (v0.6 ticket 06, ADR-0008 D4).

    Deterministic bookend of the direct-edit channel - no profile, no token, no
    model. Delegates to ``render_artifact`` (refuses a frozen artifact, fails
    loud on a missing/unreadable canonical ``.json``, re-renders the mirror via
    the sole stage renderer, audits). Returns ``0`` on a successful re-render;
    ``1`` when the artifact is frozen (the direct-edit channel is closed past
    freeze - surfaced as a clean ``error:`` line, not a traceback) or when a
    precondition is missing (unknown/non-renderable artifact, no feature run,
    nothing promoted to render - §24.2 fail loud).
    """
    try:
        feature_root = require_feature_root(repo_root, feature_id)
    except ValueError as exc:
        return _exit_value_error(repo_root, feature_id, exc)
    try:
        result: RenderResult = render_artifact(
            feature_root, feature_id, artifact, origin=ORIGIN_CLI
        )
    except FrozenArtifactWriteError as exc:
        _render_error(exc)
        return 1
    except ValueError as exc:
        return _exit_value_error(repo_root, feature_id, exc)
    print(
        f"{feature_id}: re-rendered {result.md_path.name} from "
        f"{result.json_path.name} ({artifact}, unfrozen)"
    )
    return 0


def _run_allocate_id(repo_root: Path, feature_id: str, id_type: str) -> int:
    """Allocate the next stable id of ``id_type`` from the counter (v0.6 ticket
    06, ADR-0008 D4).

    Deterministic - no profile, no token, no model. Delegates to ``allocate_id``
    (bumps the per-type high-water mark, appends an ``allocate_id`` audit
    record) and prints the minted id — the sanctioned way for a human adding an
    item to a direct-edited unfrozen artifact to get a counter-tracked id, so ids
    stay in the counter and out of human hands (§4.3). Returns ``0`` on a
    successful mint; ``1`` on an unknown id type or missing feature run (§24.2).
    """
    try:
        feature_root = require_feature_root(repo_root, feature_id)
    except ValueError as exc:
        return _exit_value_error(repo_root, feature_id, exc)
    try:
        allocated_id = allocate_id(feature_root, id_type, origin=ORIGIN_CLI)
    except ValueError as exc:
        return _exit_value_error(repo_root, feature_id, exc)
    print(allocated_id)
    return 0


def _run_show_profile(repo_root: Path, name: str) -> int:
    """Load and display a resolved profile (§10.1); fail loud on missing
    profile or missing token source (§24.2).

    Prints the profile config and token-source status to stdout. The token
    *value* is never printed - ``render_profile`` takes only the source NAME
    (§10.2, invariant #11). Returns ``0`` when the profile loads and its token
    source is set; ``1`` if the profile/file is missing or the token source is
    unset (the latter still prints the profile so the operator can see what is
    configured, then signals non-readiness via the exit code).
    """
    profile = _load_profile_or_render(repo_root, name)
    if profile is None:
        return 1

    source = token_source_var(profile)
    print(render_profile(profile, source))

    if source is None:
        # Point at the concrete source variable name so the operator knows which
        # env var to export (§26.5 actionable message - not just "not set").
        _render_error(
            ValueError(
                f"token source not set for profile {name!r} "
                f"({profile.token_source_description()} is unset); "
                f"set it before running (§24.2)"
            ),
            hint=f"export {profile.auth_env}=<token>"
            + (
                f" (or {profile.auth_env_fallback}=<token>)"
                if profile.auth_env_fallback
                else ""
            ),
        )
        return 1
    return 0


def _run_prepare_run(
    repo_root: Path,
    feature_id: str,
    role: str,
    task: str,
    allowed_files: list[str],
) -> int:
    """Allocate RUN-NNN and scaffold its input package (§12); print the run id.

    Returns ``0`` on success, ``1`` if the feature run is missing, ``role`` /
    ``task`` is empty, or an ``allowed_files`` entry is blank (§24.2 fail loud)
    - all surface as a clean ``ValueError`` message rather than a traceback.
    """
    try:
        run_id = prepare_run(
            repo_root, feature_id, role, task, allowed_files=allowed_files,
            origin=ORIGIN_CLI,
        )
    except ValueError as exc:
        return _exit_value_error(repo_root, feature_id, exc)
    print(run_id)
    return 0


def _run_run_headless(
    repo_root: Path,
    feature_id: str,
    run_id: str,
    profile_name: str,
    max_turns: int,
    permission_mode: str,
) -> int:
    """Run a prepared RUN-NNN headless via a profile and capture it (§11/§13).

    Loads the profile (fail loud on a missing file/profile, §24.2), delegates to
    ``run_headless`` (env isolation, invocation, capture, metadata), and prints a
    one-line summary. Returns ``0`` on a successful *capture* - including when
    the claude subprocess itself exited non-zero, since a captured run failure is
    not a wrapper failure (the §14 ``validate-run`` decides PASS/FAIL). Returns
    ``1`` when the profile cannot load or the run cannot start (missing token /
    run directory), surfacing the message rather than a traceback.
    """
    profile = _load_profile_or_render(repo_root, profile_name)
    if profile is None:
        return 1
    try:
        result = run_headless(
            repo_root,
            feature_id,
            run_id,
            profile,
            max_turns=max_turns,
            permission_mode=permission_mode,
            origin=ORIGIN_CLI,
        )
    except ValueError as exc:
        return _exit_value_error(repo_root, feature_id, exc, run_id=run_id)
    print(
        f"{result.run_id}: profile={result.profile} exit_code={result.exit_code} "
        f"changed_files={len(result.changed_files)}"
    )
    return 0


def _run_validate_run(repo_root: Path, feature_id: str, run_id: str) -> int:
    """Run the §14 three checks and print VALIDATE PASS/FAIL (ticket 04).

    Delegates to ``validate_run`` (pure: reads the captured artifacts, judges
    them, appends one audit record). Prints ``VALIDATE PASS`` on a clean run and
    ``VALIDATE FAIL`` with one readable line per issue otherwise. Returns ``0``
    on PASS, ``1`` on FAIL - and ``1`` with an ``error:`` line when the run
    directory is missing (§24.2 fail loud), so a missing run is distinguishable
    from a failed run in the message even though both exit ``1`` (the ticket
    fixes the exit code at 0=PASS / 1=FAIL).
    """
    try:
        result = validate_run(repo_root, feature_id, run_id, origin=ORIGIN_CLI)
    except ValueError as exc:
        return _exit_value_error(repo_root, feature_id, exc, run_id=run_id)
    if result.passed:
        print(
            f"VALIDATE PASS - {run_id} (schema + boundary + frozen OK)"
        )
        return 0
    print(f"VALIDATE FAIL - {run_id} ({len(result.issues)} problem(s)):")
    _print_validation_issues(result.issues)
    return 1


def _run_implement(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    profile_name: str,
    max_turns: int,
    permission_mode: str,
) -> int:
    """Run the Implementer leg end to end (v0.2 ticket 01, §9.2).

    Loads the profile (fail loud on a missing file/profile, §24.2), delegates to
    ``run_implementer_leg`` (prepare from frozen artifacts -> run headless ->
    validate -> ``proposed_done`` writeback gated on validation -> lane rollup),
    and prints a one-line summary. Returns ``0`` when the run validated and the
    ``proposed_done`` writeback landed; ``1`` when validation failed (a captured
    run failure is reported, not raised - the rollup still records it) or when
    the leg cannot start (missing feature/lane, unfrozen artifacts, missing
    token). The §9.2 limits are enforced inside the leg (``validate-run`` gates
    the writeback), so this command never writes canonical status for a failed
    run.
    """
    profile = _load_profile_or_render(repo_root, profile_name)
    if profile is None:
        return 1
    try:
        result = run_implementer_leg(
            repo_root,
            feature_id,
            lane_id,
            profile,
            max_turns=max_turns,
            permission_mode=permission_mode,
            origin=ORIGIN_IMPLEMENT_LEG,
        )
    except ValueError as exc:
        return _exit_value_error(repo_root, feature_id, exc)
    status = (
        f"IMPLEMENT PASS - {result.run_id} lane={result.lane_id} "
        f"status={result.result_status} tasks_marked={result.task_ids_marked}"
    )
    if result.validation.passed:
        print(status)
        return 0
    print(f"IMPLEMENT FAIL - {result.run_id} lane={result.lane_id} "
          f"({len(result.validation.issues)} problem(s)):")
    _print_validation_issues(result.validation.issues)
    return 1


def _run_generate(
    repo_root: Path,
    feature_id: str,
    profile_name: str,
    feedback: str | None,
    max_turns: int,
    permission_mode: str,
    *,
    leg: Callable[..., PlannerLegResult],
    label: str,
    id_keys: Sequence[str],
) -> int:
    """Run a Planner generate leg end to end (v0.6 tickets 02-04, §9.1, ADR-0008 D2).

    Shared by the ``generate-requirements``/``generate-design``/``generate-tasks``
    commands: loads the profile (fail loud on a missing file/profile, §24.2),
    delegates to the leg (build the Planner input package from the feature intent
    [+ frozen requirements [+ frozen design]] -> run headless -> validate ->
    promote, gated on validation), and prints a one-line summary. Returns ``0``
    when the run validated and promote wrote the canonical-unfrozen artifact;
    ``1`` when validation failed (a captured run failure is reported, not raised
    — no canonical artifact is written for a schema-invalid proposal) or when the
    leg cannot start (missing feature/intent, upstream not frozen, missing token).
    promote errors (malformed proposal / unresolved ref / frozen artifact)
    propagate as a clean ``error:`` line via the top-level handler.
    """
    profile = _load_profile_or_render(repo_root, profile_name)
    if profile is None:
        return 1
    try:
        result: PlannerLegResult = leg(
            repo_root,
            feature_id,
            profile,
            feedback=feedback,
            max_turns=max_turns,
            permission_mode=permission_mode,
            origin=ORIGIN_PLANNER_LEG,
        )
    except ValueError as exc:
        return _exit_value_error(repo_root, feature_id, exc)
    # ``result.promote`` narrows to ``PromoteResult`` here (no type: ignore): the
    # ``is not None`` guard is what mypy follows, unlike the ``promoted`` property.
    promote = result.promote
    if result.validation.passed and promote is not None:
        ids_part = " ".join(
            f"{key}={list(promote.allocated.get(key, []))}" for key in id_keys
        )
        print(
            f"{label} PASS - {result.run_id} feature={result.feature_id} "
            f"stage={result.stage} promoted={promote.json_path.name} {ids_part}"
        )
        return 0
    # Distinguish the two no-promote causes honestly. Validation failing is the
    # expected one (a captured run failure / §14 breach → no canonical write). The
    # belt-and-braces race where validation passed but no readable result.json
    # reached promote is reported as the unexpected case it is — NOT as a schema
    # failure, since validation already attested the proposal is schema-valid.
    if result.validation.passed:
        print(
            f"{label} FAIL - {result.run_id} feature={result.feature_id} "
            f"stage={result.stage}; validation passed but no result.json proposal "
            f"was readable to promote (unexpected):"
        )
        return 1
    print(
        f"{label} FAIL - {result.run_id} feature={result.feature_id} "
        f"({len(result.validation.issues)} problem(s)); no promote (proposal failed "
        f"§14 validation):"
    )
    _print_validation_issues(result.validation.issues)
    return 1


def _run_generate_requirements(
    repo_root: Path,
    feature_id: str,
    profile_name: str,
    feedback: str | None,
    max_turns: int,
    permission_mode: str,
) -> int:
    """Run the Planner requirements leg (v0.6 ticket 02, §9.1, ADR-0008 D2).

    promote allocates REQ/AC ids, stitches the AC local refs (D3), and writes the
    canonical-unfrozen ``01-requirements.{json,md}``; no upstream frozen
    precondition (the requirements leg is the head of the chain).
    """
    return _run_generate(
        repo_root,
        feature_id,
        profile_name,
        feedback,
        max_turns,
        permission_mode,
        leg=run_generate_requirements,
        label="GENERATE-REQUIREMENTS",
        id_keys=("REQ", "AC"),
    )


def _run_generate_design(
    repo_root: Path,
    feature_id: str,
    profile_name: str,
    feedback: str | None,
    max_turns: int,
    permission_mode: str,
) -> int:
    """Run the Planner design leg (v0.6 ticket 03, §9.1, ADR-0008 D2).

    Requires ``01-requirements.json`` frozen first. promote allocates DES ids,
    resolves refs against the frozen requirements, and writes the
    canonical-unfrozen ``02-design.{json,md}``.
    """
    return _run_generate(
        repo_root,
        feature_id,
        profile_name,
        feedback,
        max_turns,
        permission_mode,
        leg=run_generate_design,
        label="GENERATE-DESIGN",
        id_keys=("DES",),
    )


def _run_generate_tasks(
    repo_root: Path,
    feature_id: str,
    profile_name: str,
    feedback: str | None,
    max_turns: int,
    permission_mode: str,
) -> int:
    """Run the Planner tasks leg (v0.6 ticket 04, §9.1, ADR-0008 D2).

    Requires ``01-requirements.json`` and ``02-design.json`` frozen first. promote
    allocates TASK ids, resolves refs against the frozen requirements + design, and
    writes the canonical-unfrozen ``03-tasks.{json,md}`` (plus seeded
    ``task-status.yml`` and populated ``04-lane-graph.yml``).
    """
    return _run_generate(
        repo_root,
        feature_id,
        profile_name,
        feedback,
        max_turns,
        permission_mode,
        leg=run_generate_tasks,
        label="GENERATE-TASKS",
        id_keys=("TASK",),
    )


def _run_checking(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    profile_name: str,
    max_turns: int,
    permission_mode: str,
    *,
    leg: Callable[..., CheckingLegResult],
    label: str,
    origin: str,
) -> int:
    """Run a checking leg (Code Reviewer or Spec Gap Analyst) end to end
    (v0.2 ticket 02, §9.3/§9.4).

    Shared by the ``review`` and ``spec-gap`` commands: loads the profile (fail
    loud on a missing file/profile, §24.2), delegates to the leg (build input
    package from the lane's implement run -> run headless -> validate against the
    §15 issues schema -> roll up the lane report), and prints a one-line summary.
    Returns ``0`` when the run validated; ``1`` when validation failed (a
    captured run failure is reported, not raised - the report still records it)
    or when the leg cannot start (missing feature/lane, unfrozen artifacts, no
    implement-result, missing token). The checking legs write no canonical
    status (§4.3), so this command never mutates ``task-status.yml``.
    """
    profile = _load_profile_or_render(repo_root, profile_name)
    if profile is None:
        return 1
    try:
        result = leg(
            repo_root,
            feature_id,
            lane_id,
            profile,
            max_turns=max_turns,
            permission_mode=permission_mode,
            origin=origin,
        )
    except ValueError as exc:
        return _exit_value_error(repo_root, feature_id, exc)
    status = (
        f"{label} PASS - {result.run_id} lane={result.lane_id} "
        f"role={result.role} issues={result.issue_count}"
    )
    if result.validation.passed:
        print(status)
        return 0
    print(f"{label} FAIL - {result.run_id} lane={result.lane_id} "
          f"({len(result.validation.issues)} problem(s)):")
    _print_validation_issues(result.validation.issues)
    return 1


def _print_command_results(results: Sequence[CommandResult]) -> None:
    """Print one readable line per verify command (the verifier's analog of
    ``_print_validation_issues``). Shows the verdict + exit code per command,
    with a compact stderr excerpt where the command wrote one (the failure
    detail)."""
    for r in results:
        line = f"  - {r.name}: {'PASS' if r.passed else 'FAIL'} (exit_code={r.exit_code})"
        if r.stderr.strip():
            line += f" :: {r.stderr.strip()[:200]}"
        print(line)


def _run_verify(
    repo_root: Path, feature_id: str, lane_id: str, timeout: float
) -> int:
    """Run the shell Verifier leg end to end (v0.2 ticket 03, §9.5).

    Deterministic shell - no profile, no token (unlike the agent legs). Delegates
    to ``run_verifier`` (read declared commands from the frozen lane-graph ->
    find the implement workspace -> run each command -> roll up the lane
    ``verification-report.{md,json}`` -> ``verify`` audit), and prints a one-line
    summary. Returns ``0`` when every command passed (verdict pass); ``1`` when
    any command failed (a captured verification failure is reported, not raised
    - the report still records it, §24.1) or when the leg cannot start (missing
    feature/lane, unfrozen artifacts, no verify commands declared, no
    implement-result - all §24.2 fail-loud preconditions surfaced as a clean
    ``error:`` line rather than a traceback)."""
    try:
        result = run_verifier(
            repo_root, feature_id, lane_id, timeout=timeout, origin=ORIGIN_VERIFIER
        )
    except ValueError as exc:
        return _exit_value_error(repo_root, feature_id, exc)
    passed = sum(1 for r in result.command_results if r.passed)
    total = len(result.command_results)
    if result.verdict == "pass":
        print(
            f"VERIFY PASS - lane={result.lane_id} "
            f"implement_run={result.implement_run_id} "
            f"commands={passed}/{total} passed"
        )
        return 0
    print(
        f"VERIFY FAIL - lane={result.lane_id} "
        f"implement_run={result.implement_run_id} "
        f"commands={passed}/{total} passed:"
    )
    _print_command_results(result.command_results)
    return 1


def _run_collect_issues(
    repo_root: Path, feature_id: str, lane_id: str
) -> int:
    """Collect reviewer + spec-gap findings into stable issue artifacts.

    Deterministic collector - no profile, no token, no verifier ingestion. It
    delegates to ``collect_issue_bundle`` (review-report + spec-gap-report ->
    feature ``issues/ISSUE-NNN`` files + lane ``issue-bundle``) and prints a
    compact summary. Returns ``1`` for missing feature/lane/report preconditions
    with a clean ``error:`` line (§24.2).
    """
    try:
        result: IssueBundleResult = collect_issue_bundle(
            repo_root, feature_id, lane_id, origin=ORIGIN_CLI
        )
    except ValueError as exc:
        return _exit_value_error(repo_root, feature_id, exc)
    print(
        f"COLLECT-ISSUES PASS - lane={result.lane_id} "
        f"issues={result.issue_count} bundle={result.bundle_json_path}"
    )
    return 0


def _run_lane_gate(
    repo_root: Path, feature_id: str, lane_id: str
) -> int:
    """Evaluate the deterministic §18.4 lane gate and print PASS/FAIL.

    Delegates to ``evaluate_lane_gate`` (implement-result + verification-report +
    issue-bundle -> ``lane-decision.{md,json}``) and returns the process-level
    contract the ticket names: ``0`` for PASS, ``1`` for FAIL or fail-loud missing
    prerequisite artifacts.
    """
    try:
        result: LaneDecisionResult = evaluate_lane_gate(
            repo_root, feature_id, lane_id, origin=ORIGIN_CLI
        )
    except ValueError as exc:
        return _exit_value_error(repo_root, feature_id, exc)
    if result.passed:
        print(
            f"LANE-GATE PASS - lane={result.lane_id} "
            f"conditions={result.passed_condition_count}/{result.condition_count} "
            f"decision={result.decision_json_path}"
        )
        return 0
    failed = ",".join(result.failed_conditions)
    print(
        f"LANE-GATE FAIL - lane={result.lane_id} "
        f"failed_conditions={failed} decision={result.decision_json_path}"
    )
    return 1


def _run_coherence_gate(repo_root: Path, feature_id: str) -> int:
    """Evaluate the deterministic §18.5 coherence gate and print PASS/FAIL.

    Delegates to ``evaluate_coherence_gate`` (lane-decision + issues/ +
    decisions/ + feature-status -> ``coherence-decision.{md,json}`` + the atomic
    ``current_gate=feature_coherence_gate`` + ``verdict`` + derived
    ``feature.status`` write) and returns the process-level contract: ``0`` for
    PASS, ``1`` for FAIL or fail-loud missing/corrupt prerequisites (§24.2). A
    FAIL still writes the verdict (status=blocked); a fail-loud precondition
    writes nothing.
    """
    try:
        result: CoherenceResult = evaluate_coherence_gate(
            repo_root, feature_id, origin=ORIGIN_CLI
        )
    except ValueError as exc:
        return _exit_value_error(repo_root, feature_id, exc)
    if result.passed:
        print(
            f"COHERENCE-GATE PASS - feature={result.feature_id} verdict=pass "
            f"status=done decision={result.decision_json_path}"
        )
        return 0
    failed = ",".join(result.failed_conditions)
    print(
        f"COHERENCE-GATE FAIL - feature={result.feature_id} verdict=fail "
        f"status=blocked failed_conditions={failed} "
        f"decision={result.decision_json_path}"
    )
    return 1


def _run_final_report(repo_root: Path, feature_id: str) -> int:
    """Generate ``final-report.{json,md}`` and print a one-line summary.

    Delegates to ``generate_final_report`` (a pure projection: reads the
    coherence verdict + artifacts, writes the two report files, touches no
    canonical state, appends no audit event - ADR-0003 D7 supplement b). Returns
    ``0`` on a successful render for either verdict (the report exists for both
    pass and fail, D6) and ``1`` with a clean ``error:`` line when a
    required artifact is missing/corrupt or the verdict is null (coherence has
    not run - §24.2 / D7 supplement c). Re-running is idempotent and non-audited.
    """
    try:
        result: FinalReportResult = generate_final_report(repo_root, feature_id)
    except ValueError as exc:
        return _exit_value_error(repo_root, feature_id, exc)
    print(
        f"FINAL-REPORT - feature={result.feature_id} verdict={result.verdict} "
        f"failure_class={result.failure_class} report={result.report_json_path}"
    )
    return 0


def _run_compare_profiles(
    repo_root: Path,
    feature_id: str,
    profile_names: list[str],
    as_json: bool,
) -> int:
    """``compare-profiles``: project two parallel feature-runs (v0.5 ticket 06).

    Always writes the non-canonical ``projections/profile-comparison.{json,md}``
    under the anchor feature; ``--json`` additionally emits the full report as
    JSON to stdout, otherwise a one-line human summary. Non-canonical: no audit
    append, no canonical-state mutation. Returns ``1`` with a clean ``error:``
    line on any §24.2 precondition miss (missing sibling, missing final-report).
    """
    try:
        result: ProfileComparisonResult = generate_profile_comparison(
            repo_root, feature_id, profile_names
        )
    except ValueError as exc:
        return _exit_value_error(repo_root, feature_id, exc)
    if as_json:
        _print_json(json.loads(result.projection_json_path.read_text()))
    else:
        print(
            f"COMPARE-PROFILES - feature={result.feature_id} "
            f"profiles={','.join(profile_names)} "
            f"projection={result.projection_json_path}"
        )
    return 0


def _run_project_github(
    repo_root: Path, feature_id: str, pr_number: int | None
) -> int:
    """``project-github``: push issues + PR comment to GitHub (v0.5 ticket 07).

    Delegates to ``project_github`` (preflight -> per-issue gh create/edit ->
    optional PR comment, all idempotent via ``projections/github/mapping.json``).
    Returns ``0`` on a complete projection, ``1`` on a pre-flight failure or a
    mid-stream push failure (D4: successes + their mapping entries are kept
    either way; re-running resumes from the mapping). A missing feature run is a
    fail-loud §24.2 precondition surfaced as a clean ``error:`` line.
    """
    try:
        result: GithubProjectionResult = project_github(
            repo_root, feature_id, pr_number
        )
    except ValueError as exc:
        return _exit_value_error(repo_root, feature_id, exc)
    if result.failure_reason is not None:
        _render_error(
            ValueError(result.failure_reason),
            hint=(
                "re-run `ai-dev project-github` to resume from the mapping "
                "(already-pushed items are edited, not re-created)"
            ),
        )
        return 1
    created = [i.issue_id for i in result.issues if i.action == "created"]
    updated = [i.issue_id for i in result.issues if i.action == "updated"]
    pr = f" pr={result.pr_number} comment={result.pr_comment_action}" if result.pr_number else ""
    print(
        f"PROJECT-GITHUB - feature={result.feature_id} "
        f"issues_created={created} issues_updated={updated}{pr} "
        f"mapping={result.mapping_path}"
    )
    return 0


def _run_fix_run(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    implement_profile_name: str,
    reviewer_profile_name: str,
    spec_gap_profile_name: str,
    max_turns: int,
    permission_mode: str,
    verify_timeout: float,
) -> int:
    """Run one bounded fix-loop bookend and stop before human re-triage.

    v0.5 ticket 03: loads one profile per leg (per-leg role defaults resolved by
    the dispatcher); a missing profile surfaces as a clean ``error:`` + exit 1
    before any leg runs. ``--profile``, when given, was applied to all three
    names upstream so a single override covers the whole chain.
    """
    try:
        implement_profile = load_profile(repo_root, implement_profile_name)
        reviewer_profile = load_profile(repo_root, reviewer_profile_name)
        spec_gap_profile = load_profile(repo_root, spec_gap_profile_name)
    except ProfileError as exc:
        _render_error(exc)
        return 1
    try:
        result: FixRunResult = run_fix_run(
            repo_root,
            feature_id,
            lane_id,
            implement_profile,
            reviewer_profile,
            spec_gap_profile,
            max_turns=max_turns,
            permission_mode=permission_mode,
            verify_timeout=verify_timeout,
            origin=ORIGIN_FIX_RUN_DRIVER,
        )
    except ValueError as exc:
        return _exit_value_error(repo_root, feature_id, exc)
    print(
        f"FIX-RUN PASS - lane={result.lane_id} implement_run={result.implement_run_id} "
        f"targets={result.target_issue_ids} budget={result.budget_used}/{result.budget_max} "
        f"verification={result.verification.verdict} collected={result.collection.issue_count}"
    )
    return 0


def _run_triage(
    repo_root: Path,
    feature_id: str,
    issue_id: str,
    disposition: str,
    reason: str | None,
    by: str,
) -> int:
    """Apply one Human-Triage disposition to an issue (v0.3 ticket 05, §16).

    Deterministic - no profile, no token, no model (ADR-0001 #8). Delegates to
    ``apply_triage`` (writes the ``triage`` state object on
    ``issues/ISSUE-NNN.json``, enforces the legality matrix + reason + promotion,
    drives ``status -> triaged``, audits). Returns ``0`` on a successful apply,
    ``1`` when the disposition is refused at the write layer (illegal cell or a
    disarming disposition missing its reason - ADR-0001 #7: surfaced as a clean
    ``error:`` line, not a traceback, and the issue stays untriaged) or when a
    precondition is missing (unknown issue / disposition, §24.2 fail loud).
    """
    try:
        result: TriageResult = apply_triage(
            repo_root, feature_id, issue_id, disposition, reason, by, origin=ORIGIN_CLI
        )
    except (TriageRefusedError, ValueError) as exc:
        _render_error(
            exc,
            hint=_triage_hint(
                exc,
                repo_root=repo_root,
                feature_id=feature_id,
                issue_id=issue_id,
                reason=reason,
            ),
        )
        return 1
    decisions = ",".join(result.decision_ids) if result.decision_ids else "-"
    print(
        f"TRIAGE PASS - issue={result.issue_id} disposition={result.action} "
        f"severity={result.severity} decisions={decisions}"
    )
    return 0


# ---------------------------------------------------------------------------
# v0.4 ticket 03: read-only observability commands (§26.5 CLI UX).
# ---------------------------------------------------------------------------


def _print_json(payload: Any) -> None:
    """Print ``payload`` as indented JSON to stdout (the ``--json`` form)."""
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _render_list_features(rows: Sequence[FeatureSummary]) -> None:
    """Human-readable form of ``list-features`` (one line per feature)."""
    if not rows:
        print("(no features yet)")
        return
    for row in rows:
        verdict = row.verdict if row.verdict is not None else "-"
        print(
            f"{row.feature_id}\tstatus={row.status}\t"
            f"gate={row.current_gate}\tverdict={verdict}"
        )


def _run_list_features(repo_root: Path, as_json: bool) -> int:
    """``ai-dev list-features`` — list every feature + derived status + gate.

    Read-only: it reads each feature's ``feature-status.yml`` and prints one row
    per ``FEATURE-NNN`` (human-readable by default, JSON with ``--json``). Empty
    is a valid observable state, so it exits ``0`` even with zero features. A
    corrupt feature-status file propagates as a clean ``error:`` + exit ``1``
    (§24.2 fail loud) rather than silently dropping the broken feature.
    """
    try:
        rows = list_features(repo_root)
    except ValueError as exc:
        _render_error(exc)
        return 1
    if as_json:
        _print_json([row.to_dict() for row in rows])
    else:
        _render_list_features(rows)
    return 0


def _render_show_status(view: FeatureStatusView) -> None:
    """Human-readable form of ``show-status``."""
    verdict = view.verdict if view.verdict is not None else "(none)"
    print(f"{view.feature_id}")
    print(f"  status: {view.status}")
    print(f"  current_gate: {view.current_gate}")
    print(f"  verdict: {verdict}")
    print("  lanes:")
    if not view.lanes:
        print("    (no lanes)")
        return
    for lane in view.lanes:
        if lane.decision is None:
            print(f"    {lane.lane_id}: (no lane-decision yet)")
            continue
        detail = ""
        if lane.failed_conditions:
            detail = f" failed=[{','.join(lane.failed_conditions)}]"
        blockers = (
            f" blockers={lane.blocking_issue_count}"
            if lane.blocking_issue_count
            else ""
        )
        print(f"    {lane.lane_id}: decision={lane.decision}{detail}{blockers}")


def _run_show_status(repo_root: Path, feature_id: str, as_json: bool) -> int:
    """``ai-dev show-status <FEATURE>`` — gate/verdict/status + per-lane decisions.

    Read-only. Exits ``0`` on a readable feature, ``1`` with a clean ``error:``
    when the feature does not exist (§24.2 fail loud).
    """
    try:
        view = show_feature_status(repo_root, feature_id)
    except ValueError as exc:
        return _exit_value_error(repo_root, feature_id, exc)
    if as_json:
        _print_json(view.to_dict())
    else:
        _render_show_status(view)
    return 0


def _render_log(records: Sequence[AuditRecordView]) -> None:
    """Human-readable chronological audit timeline (from ``audit.log.json``).

    One block per event: ``<timestamp> · <event> · origin=<origin>`` followed by
    the payload pairs. ``origin`` / ``elapsed_ms`` (ticket 02) are surfaced
    inline so the timeline answers "which driver ran this" and "how long did it
    take" without re-reading the raw log.
    """
    if not records:
        print("(no audit events)")
        return
    for record in records:
        header = f"{record.timestamp} · {record.event}"
        if record.origin is not None:
            header += f" · origin={record.origin}"
        print(header)
        for key, value in record.payload.items():
            display = value if isinstance(value, str) else json.dumps(
                value, ensure_ascii=False
            )
            print(f"  - {key}: {display}")
        print()


def _run_log(repo_root: Path, feature_id: str, as_json: bool) -> int:
    """``ai-dev log <FEATURE>`` — pretty-print the audit timeline.

    Read-only. Renders ``audit.log.json`` (the machine product — never
    ``audit.log.md``), consuming ticket 02's ``origin`` / ``elapsed_ms``. Exits
    ``0`` on a readable feature, ``1`` with a clean ``error:`` when the feature
    does not exist or its audit log is missing/corrupt (§24.2 fail loud).
    """
    try:
        records = read_audit_timeline(repo_root, feature_id)
    except ValueError as exc:
        return _exit_value_error(repo_root, feature_id, exc)
    if as_json:
        _print_json([record.to_dict() for record in records])
    else:
        _render_log(records)
    return 0


@dataclass(frozen=True)
class Command:
    """One row in the cli command registry - the single source of truth for the
    command surface. ``_build_parser`` declares the subcommand from this row
    (``help_text`` + ``add_args`` + ``json`` + the ``--dry-run`` flag when
    ``plan is not None``); ``_dispatch`` routes to ``run`` (or ``plan`` under
    ``--dry-run``). Adding a command is one entry here - not three hand-maintained
    lists (parser block + dispatch branch + dry-run set).
    """

    name: str
    help_text: str
    add_args: "Callable[[argparse.ArgumentParser], None]"
    run: "Callable[[argparse.Namespace], int]"
    plan: "Callable[[argparse.Namespace], DryRunPlan] | None"
    json: bool = False


def _agent_command(
    name: str,
    help_text: str,
    add_args: "Callable[[argparse.ArgumentParser], None]",
    role: str,
    origin: str,
    real: "Callable[[Path, str, str, argparse.Namespace], int]",
    plan: "Callable[[Path, str, AgentProfile, argparse.Namespace], DryRunPlan]",
    json: bool = False,
) -> Command:
    """Build an agent Command: resolve profile, then (dry) plan with a loaded
    ``AgentProfile`` or (real) record + run with the name.

    The resolve/record preamble - previously copy-pasted for the six agent
    commands in ``_dispatch`` - lives once here. ``real`` takes the profile
    *name* (records it; the leg loads internally); ``plan`` takes the *loaded*
    Profile (dry-run never records). The dry-vs-real branch is the dispatch
    loop's job, not the closure's: this factory builds two clean callables and
    the loop picks. ``ProfileError`` from resolve renders on the real path
    (``_render_error``) and propagates to ``_run_dry_plan``'s catch on the dry
    path - identical ``error:`` shape either way.
    """

    def run(args: argparse.Namespace) -> int:
        repo_root = Path(args.repo_root)
        try:
            profile_name = resolve_profile_name(repo_root, role, args.profile)
        except ProfileError as exc:
            _render_error(exc)
            return 1
        record_agent_profile(
            feature_dir(repo_root, args.feature_id), role, profile_name, origin=origin
        )
        return real(repo_root, args.feature_id, profile_name, args)

    def dry(args: argparse.Namespace) -> DryRunPlan:
        repo_root = Path(args.repo_root)
        profile_name = resolve_profile_name(repo_root, role, args.profile)
        return plan(
            repo_root, args.feature_id, load_profile(repo_root, profile_name), args
        )

    return Command(
        name=name,
        help_text=help_text,
        add_args=add_args,
        run=run,
        plan=dry,
        json=json,
    )


def _run_create_feature_run_cmd(args: argparse.Namespace) -> int:
    """``create-feature-run`` - prints the minted id and exits 0 (no ``_run_*``)."""
    feature_id = create_feature_run(Path(args.repo_root), args.intent, origin=ORIGIN_CLI)
    print(feature_id)
    return 0


def _profile_names(args: argparse.Namespace) -> list[str]:
    """Parse ``compare-profiles``' comma-separated ``--profiles`` into a list."""
    return [p.strip() for p in args.profiles.split(",") if p.strip()]


def _resolve_fix_profiles(
    repo_root: Path, profile_override: str | None
) -> tuple[str, str, str]:
    """Resolve fix-run's three role profiles from one ``--profile`` override.

    Raises ``ProfileError`` on a bad name - the caller decides whether to render
    (real path) or let it propagate to ``_run_dry_plan``'s catch (dry path).
    """
    return (
        resolve_profile_name(repo_root, ROLE_IMPLEMENTER, profile_override),
        resolve_profile_name(repo_root, ROLE_REVIEWER, profile_override),
        resolve_profile_name(repo_root, ROLE_SPEC_GAP_ANALYST, profile_override),
    )


def _run_fix_run_cmd(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root)
    try:
        implement_name, reviewer_name, spec_gap_name = _resolve_fix_profiles(
            repo_root, args.profile
        )
    except ProfileError as exc:
        _render_error(exc)
        return 1
    fix_feature_root = feature_dir(repo_root, args.feature_id)
    record_agent_profile(
        fix_feature_root, ROLE_IMPLEMENTER, implement_name, origin=ORIGIN_FIX_RUN_DRIVER
    )
    record_agent_profile(
        fix_feature_root, ROLE_REVIEWER, reviewer_name, origin=ORIGIN_FIX_RUN_DRIVER
    )
    record_agent_profile(
        fix_feature_root,
        ROLE_SPEC_GAP_ANALYST,
        spec_gap_name,
        origin=ORIGIN_FIX_RUN_DRIVER,
    )
    return _run_fix_run(
        repo_root,
        args.feature_id,
        args.lane_id,
        implement_name,
        reviewer_name,
        spec_gap_name,
        args.max_turns,
        args.permission_mode,
        args.verify_timeout,
    )


def _plan_fix_run_cmd(args: argparse.Namespace) -> DryRunPlan:
    repo_root = Path(args.repo_root)
    implement_name, reviewer_name, spec_gap_name = _resolve_fix_profiles(
        repo_root, args.profile
    )
    return plan_fix_run(
        repo_root,
        args.feature_id,
        args.lane_id,
        load_profile(repo_root, implement_name),
        load_profile(repo_root, reviewer_name),
        load_profile(repo_root, spec_gap_name),
        max_turns=args.max_turns,
        permission_mode=args.permission_mode,
        verify_timeout=args.verify_timeout,
    )


# The command registry: one row per subcommand - the single source of
# truth for the command surface. ``_build_parser`` declares the subcommand
# (help + args + --dry-run when plan is not None); ``_dispatch`` routes to
# run (or plan under --dry-run). Adding a command is one entry here - not
# three hand-maintained lists (parser block + dispatch branch + dry-run set).
COMMANDS: list[Command] = [
    Command(
        "create-feature-run",
        help_text="Create a new feature run from an intent string (ticket 01).",
        add_args=_args_create_feature_run,
        run=_run_create_feature_run_cmd,
        plan=None,
    ),
    Command(
        "freeze",
        help_text="Freeze a canonical artifact after its human gate passes (§4.2, ticket 04).",
        add_args=_args_freeze,
        run=lambda a: _run_freeze(Path(a.repo_root), a.feature_id, a.artifact),
        plan=lambda a: plan_freeze(Path(a.repo_root), a.feature_id, a.artifact),
    ),
    Command(
        "render",
        help_text="Re-render an unfrozen artifact's .md mirror from its (hand-edited) "
        ".json (v0.6 ticket 06, ADR-0008 D4). Deterministic - no model.",
        add_args=_args_render,
        run=lambda a: _run_render(Path(a.repo_root), a.feature_id, a.artifact),
        plan=lambda a: plan_render(Path(a.repo_root), a.feature_id, a.artifact),
    ),
    Command(
        "allocate-id",
        help_text="Allocate the next stable id of a type from the counter "
        "(v0.6 ticket 06, ADR-0008 D4). Deterministic - no model. For human-"
        "added items in a direct-edited unfrozen artifact, so ids stay in the "
        "counter and out of human hands (§4.3).",
        add_args=_args_allocate_id,
        run=lambda a: _run_allocate_id(Path(a.repo_root), a.feature_id, a.id_type),
        plan=lambda a: plan_allocate_id(Path(a.repo_root), a.feature_id, a.id_type),
    ),
    Command(
        "show-profile",
        help_text="Load and display a resolved agent profile (§10.1, run-adapter ticket 01).",
        add_args=_args_show_profile,
        run=lambda a: _run_show_profile(Path(a.repo_root), a.name),
        plan=None,
    ),
    Command(
        "prepare-run",
        help_text="Allocate RUN-NNN and scaffold its input package (§12, ticket 02).",
        add_args=_args_prepare_run,
        run=lambda a: _run_prepare_run(
            Path(a.repo_root),
            a.feature_id,
            a.role,
            a.task,
            a.allowed_file,
        ),
        plan=None,
    ),
    Command(
        "run-headless",
        help_text="Run a prepared RUN-NNN headless via a profile and capture it (§11, ticket 03).",
        add_args=_args_run_headless,
        run=lambda a: _run_run_headless(
            Path(a.repo_root),
            a.feature_id,
            a.run_id,
            a.profile,
            a.max_turns,
            a.permission_mode,
        ),
        plan=lambda a: plan_run_headless(
            Path(a.repo_root),
            a.feature_id,
            a.run_id,
            load_profile(Path(a.repo_root), a.profile),
            max_turns=a.max_turns,
            permission_mode=a.permission_mode,
        ),
    ),
    Command(
        "validate-run",
        help_text="Run the §14 deterministic validation (schema + boundary + frozen) "
        "on a captured run (ticket 04).",
        add_args=_args_validate_run,
        run=lambda a: _run_validate_run(Path(a.repo_root), a.feature_id, a.run_id),
        plan=None,
    ),
    _agent_command(
        "generate-requirements",
        "Run the Planner requirements leg: generate -> validate -> auto promote "
        "the canonical-unfrozen 01-requirements (v0.6 ticket 02, ADR-0008).",
        _args_generate_requirements,
        ROLE_PLANNER,
        ORIGIN_PLANNER_LEG,
        real=lambda repo, fid, name, a: _run_generate_requirements(
            repo, fid, name, a.feedback, a.max_turns, a.permission_mode
        ),
        plan=lambda repo, fid, prof, a: plan_generate_requirements(
            repo,
            fid,
            prof,
            feedback=a.feedback,
            max_turns=a.max_turns,
            permission_mode=a.permission_mode,
        ),
    ),
    _agent_command(
        "generate-design",
        "Run the Planner design leg: generate -> validate -> auto promote "
        "the canonical-unfrozen 02-design against the frozen requirements "
        "(v0.6 ticket 03, ADR-0008).",
        _args_generate_design,
        ROLE_PLANNER,
        ORIGIN_PLANNER_LEG,
        real=lambda repo, fid, name, a: _run_generate_design(
            repo, fid, name, a.feedback, a.max_turns, a.permission_mode
        ),
        plan=lambda repo, fid, prof, a: plan_generate_design(
            repo,
            fid,
            prof,
            feedback=a.feedback,
            max_turns=a.max_turns,
            permission_mode=a.permission_mode,
        ),
    ),
    _agent_command(
        "generate-tasks",
        "Run the Planner tasks leg: generate -> validate -> auto promote "
        "the canonical-unfrozen 03-tasks (+ task-status.yml + 04-lane-graph.yml) "
        "against the frozen requirements and design (v0.6 ticket 04, ADR-0008).",
        _args_generate_tasks,
        ROLE_PLANNER,
        ORIGIN_PLANNER_LEG,
        real=lambda repo, fid, name, a: _run_generate_tasks(
            repo, fid, name, a.feedback, a.max_turns, a.permission_mode
        ),
        plan=lambda repo, fid, prof, a: plan_generate_tasks(
            repo,
            fid,
            prof,
            feedback=a.feedback,
            max_turns=a.max_turns,
            permission_mode=a.permission_mode,
        ),
    ),
    _agent_command(
        "implement",
        "Run the Implementer leg: prepare -> run -> validate -> writeback -> "
        "rollup (v0.2 ticket 01, §9.2).",
        _args_implement,
        ROLE_IMPLEMENTER,
        ORIGIN_IMPLEMENT_LEG,
        real=lambda repo, fid, name, a: _run_implement(
            repo, fid, a.lane_id, name, a.max_turns, a.permission_mode
        ),
        plan=lambda repo, fid, prof, a: plan_implement(
            repo,
            fid,
            a.lane_id,
            prof,
            max_turns=a.max_turns,
            permission_mode=a.permission_mode,
        ),
    ),
    _agent_command(
        "review",
        "Run the Code Reviewer leg: build -> run -> validate -> "
        "review-report (v0.2 ticket 02, §9.3).",
        _args_review,
        ROLE_REVIEWER,
        ORIGIN_REVIEW_LEG,
        real=lambda repo, fid, name, a: _run_checking(
            repo,
            fid,
            a.lane_id,
            name,
            a.max_turns,
            a.permission_mode,
            leg=run_reviewer_leg,
            label="REVIEW",
            origin=ORIGIN_REVIEW_LEG,
        ),
        plan=lambda repo, fid, prof, a: plan_review(
            repo,
            fid,
            a.lane_id,
            prof,
            max_turns=a.max_turns,
            permission_mode=a.permission_mode,
        ),
    ),
    _agent_command(
        "spec-gap",
        "Run the Spec Gap Analyst leg: build -> run -> validate -> "
        "spec-gap-report (v0.2 ticket 02, §9.4).",
        _args_spec_gap,
        ROLE_SPEC_GAP_ANALYST,
        ORIGIN_SPEC_GAP_LEG,
        real=lambda repo, fid, name, a: _run_checking(
            repo,
            fid,
            a.lane_id,
            name,
            a.max_turns,
            a.permission_mode,
            leg=run_spec_gap_leg,
            label="SPEC-GAP",
            origin=ORIGIN_SPEC_GAP_LEG,
        ),
        plan=lambda repo, fid, prof, a: plan_spec_gap(
            repo,
            fid,
            a.lane_id,
            prof,
            max_turns=a.max_turns,
            permission_mode=a.permission_mode,
        ),
    ),
    Command(
        "verify",
        help_text="Run the shell Verifier leg: execute the lane's declared verify "
        "commands and roll up a verification-report (v0.2 ticket 03, §9.5).",
        add_args=_args_verify,
        run=lambda a: _run_verify(
            Path(a.repo_root), a.feature_id, a.lane_id, a.timeout
        ),
        plan=None,
    ),
    Command(
        "collect-issues",
        help_text="Collect reviewer + spec-gap issues into feature issues and the "
        "lane issue-bundle (v0.2 ticket 04, §15).",
        add_args=_args_collect_issues,
        run=lambda a: _run_collect_issues(Path(a.repo_root), a.feature_id, a.lane_id),
        plan=None,
    ),
    Command(
        "lane-gate",
        help_text="Evaluate the §18.4 lane gate and write lane-decision.{md,json} "
        "(v0.2 ticket 05).",
        add_args=_args_lane_gate,
        run=lambda a: _run_lane_gate(Path(a.repo_root), a.feature_id, a.lane_id),
        plan=lambda a: plan_lane_gate(Path(a.repo_root), a.feature_id, a.lane_id),
    ),
    Command(
        "coherence-gate",
        help_text="Evaluate the §18.5 feature coherence gate and write the terminal "
        "verdict on feature-status.yml (ADR-0003, v0.3 ticket 08).",
        add_args=_args_coherence_gate,
        run=lambda a: _run_coherence_gate(Path(a.repo_root), a.feature_id),
        plan=lambda a: plan_coherence_gate(Path(a.repo_root), a.feature_id),
    ),
    Command(
        "final-report",
        help_text="Generate final-report.{json,md} from the coherence verdict "
        "(ADR-0003 D5/D6/D7, v0.3 ticket 09). Deterministic projection - no model.",
        add_args=_args_final_report,
        run=lambda a: _run_final_report(Path(a.repo_root), a.feature_id),
        plan=lambda a: plan_final_report(Path(a.repo_root), a.feature_id),
    ),
    Command(
        "triage",
        help_text="Apply a Human-Triage disposition to one issue (ADR-0001, v0.3 "
        "ticket 05). Deterministic - no model.",
        add_args=_args_triage,
        run=lambda a: _run_triage(
            Path(a.repo_root),
            a.feature_id,
            a.issue,
            a.disposition,
            a.reason,
            a.by,
        ),
        plan=lambda a: plan_triage(
            Path(a.repo_root),
            a.feature_id,
            a.issue,
            a.disposition,
            a.reason,
            a.by,
        ),
    ),
    Command(
        "fix-run",
        help_text="Run one bounded fix-loop bookend for active request_fix issues "
        "(ADR-0002, v0.3 ticket 07).",
        add_args=_args_fix_run,
        run=_run_fix_run_cmd,
        plan=_plan_fix_run_cmd,
    ),
    Command(
        "list-features",
        help_text="List every FEATURE-NNN with its derived status + current gate "
        "(v0.4 ticket 03). Read-only.",
        add_args=_args_list_features,
        run=lambda a: _run_list_features(Path(a.repo_root), a.json),
        plan=None,
        json=True,
    ),
    Command(
        "show-status",
        help_text="Show a feature's gate/verdict/derived status + each lane's "
        "lane-decision (v0.4 ticket 03). Read-only.",
        add_args=_args_show_status,
        run=lambda a: _run_show_status(Path(a.repo_root), a.feature_id, a.json),
        plan=None,
        json=True,
    ),
    Command(
        "log",
        help_text="Pretty-print a feature's audit timeline (v0.4 ticket 03). "
        "Read-only; renders audit.log.json (consumes ticket 02's "
        "origin/elapsed_ms).",
        add_args=_args_log,
        run=lambda a: _run_log(Path(a.repo_root), a.feature_id, a.json),
        plan=None,
        json=True,
    ),
    Command(
        "compare-profiles",
        help_text="Project a side-by-side comparison of two parallel feature-runs "
        "(same intent, one profile each) into "
        "projections/profile-comparison.{json,md} (v0.5 ticket 06, "
        "ADR-0003-style non-canonical projection). Read-only.",
        add_args=_args_compare_profiles,
        run=lambda a: _run_compare_profiles(
            Path(a.repo_root), a.feature_id, _profile_names(a), a.json
        ),
        plan=lambda a: plan_compare_profiles(
            Path(a.repo_root), a.feature_id, _profile_names(a)
        ),
        json=True,
    ),
    Command(
        "project-github",
        help_text="Push canonical issues to GitHub issues + post the final-report as "
        "a PR comment (v0.5 ticket 07, ADR-0006). Network-bound; idempotent via "
        "projections/github/mapping.json.",
        add_args=_args_project_github,
        run=lambda a: _run_project_github(Path(a.repo_root), a.feature_id, a.pr),
        plan=lambda a: plan_project_github(Path(a.repo_root), a.feature_id, a.pr),
    ),
]

_COMMAND_BY_NAME: dict[str, Command] = {c.name: c for c in COMMANDS}


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch a CLI invocation. Returns a process exit code.

    Parses args (argparse's own errors - bad flags / missing subcommand - exit
    ``2`` via ``SystemExit`` before this returns), then runs the subcommand. Any
    exception the subcommand did not itself catch is rendered here as a single
    clean ``error:`` line and exit ``1`` (§26.5): a scripted consumer never sees
    a Python traceback by default. ``--debug`` re-raises the original exception
    so an operator can read the full stack (exit code stays ``1``-or-crash,
    never a new band - the 0=success / 1=everything-else contract is unchanged).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(parser, args)
    except Exception as exc:
        if getattr(args, "debug", False):
            raise
        _render_error(exc, hint=_lookup_hint_from_args(args))
        return 1


def _dispatch(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Run the parsed subcommand and return its exit code (no catch-all here).

    The command surface is the ``COMMANDS`` registry - one row per subcommand,
    each carrying its real ``run`` and (for dry-capable commands) a ``plan``.
    The dry-vs-real branch lives here once, not in a per-command if/elif: a
    dry run defers ``cmd.plan(args)`` into ``_run_dry_plan`` so its
    precondition ``ValueError``/``ProfileError`` surface as one ``error:``
    line. Expected precondition failures exit 1; anything else propagates
    to ``main``'s top-level handler.
    """
    cmd = _COMMAND_BY_NAME[args.command]
    if getattr(args, "dry_run", False) and cmd.plan is not None:
        planner = cmd.plan
        return _run_dry_plan(lambda: planner(args))
    return cmd.run(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
