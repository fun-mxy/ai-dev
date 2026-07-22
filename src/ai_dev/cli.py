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
from pathlib import Path
from typing import Any, Callable, Sequence

from ai_dev.checking_legs import CheckingLegResult, run_reviewer_leg, run_spec_gap_leg
from ai_dev.coherence_gate import CoherenceResult, evaluate_coherence_gate
from ai_dev.dry_run import (
    DryRunPlan,
    plan_coherence_gate,
    plan_compare_profiles,
    plan_final_report,
    plan_fix_run,
    plan_freeze,
    plan_implement,
    plan_lane_gate,
    plan_review,
    plan_run_headless,
    plan_spec_gap,
    plan_triage,
    render_plan,
)
from ai_dev.feature_run import create_feature_run
from ai_dev.final_report import FinalReportResult, generate_final_report
from ai_dev.fix_run import FixRunResult, run_fix_run
from ai_dev.implement_leg import run_implementer_leg
from ai_dev.issue_bundle import ISSUES_DIR, IssueBundleResult, collect_issue_bundle
from ai_dev.lane_gate import LaneDecisionResult, evaluate_lane_gate
from ai_dev.paths import feature_dir, features_dir, run_dir, runs_dir
from ai_dev.profile_comparison import (
    PROFILE_COMPARISON_JSON,
    PROFILE_COMPARISON_MD,
    ProfileComparisonResult,
    generate_profile_comparison,
)
from ai_dev.profiles import (
    ProfileError,
    ROLE_IMPLEMENTER,
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


def _build_parser() -> argparse.ArgumentParser:
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

    # v0.4 ticket 03: ``--repo-root`` is declared once on this parent parser and
    # attached to every subparser via ``parents=[...]``. The declaration is
    # deduplicated (one source of truth) without changing the invocation syntax
    # — every command still accepts ``<command> ... --repo-root X`` exactly as
    # before, so existing calls and scripts are unaffected. A second parent
    # carries the read-only commands' shared ``--json`` flag.
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

    create = subparsers.add_parser(
        "create-feature-run",
        help="Create a new feature run from an intent string (ticket 01).",
        parents=[repo_root_parent],
    )
    create.add_argument("intent", help="The original user intent text to record.")

    freeze = subparsers.add_parser(
        "freeze",
        help="Freeze a canonical artifact after its human gate passes (§4.2, ticket 04).",
        parents=[repo_root_parent],
    )
    freeze.add_argument("feature_id", help="The FEATURE-NNN id of the run to update.")
    freeze.add_argument(
        "artifact",
        choices=FROZEN_ARTIFACTS,
        help="Which frozen artifact to flip (one of the §4.2 four).",
    )

    show = subparsers.add_parser(
        "show-profile",
        help="Load and display a resolved agent profile (§10.1, run-adapter ticket 01).",
        parents=[repo_root_parent],
    )
    show.add_argument("name", help="The profile name to resolve (e.g. cc-glm52).")

    prepare = subparsers.add_parser(
        "prepare-run",
        help="Allocate RUN-NNN and scaffold its input package (§12, ticket 02).",
        parents=[repo_root_parent],
    )
    prepare.add_argument(
        "feature_id", help="The FEATURE-NNN id to prepare the run under."
    )
    prepare.add_argument(
        "--role",
        required=True,
        help="The role for this run (e.g. Implementer, Reviewer, Spec-Gap).",
    )
    prepare.add_argument(
        "--task",
        required=True,
        help="The task text for this run (written verbatim into task-package.md).",
    )
    prepare.add_argument(
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

    run = subparsers.add_parser(
        "run-headless",
        help="Run a prepared RUN-NNN headless via a profile and capture it (§11, ticket 03).",
        parents=[repo_root_parent],
    )
    run.add_argument("feature_id", help="The FEATURE-NNN id the run lives under.")
    run.add_argument("run_id", help="The RUN-NNN id to invoke.")
    run.add_argument(
        "--profile",
        default="cc-glm52",
        help="Agent profile to invoke (default: cc-glm52, the v0 recommended "
        "profile, §23.4).",
    )
    run.add_argument(
        "--max-turns",
        type=int,
        default=DEFAULT_MAX_TURNS,
        help="Bounded --max-turns for the headless call (default: 12).",
    )
    run.add_argument(
        "--permission-mode",
        default=DEFAULT_PERMISSION_MODE,
        help="claude --permission-mode (default: bypassPermissions; the wrapper "
        "enforces the file boundary post-hoc, §14.2).",
    )

    validate = subparsers.add_parser(
        "validate-run",
        help="Run the §14 deterministic validation (schema + boundary + frozen) "
        "on a captured run (ticket 04).",
        parents=[repo_root_parent],
    )
    validate.add_argument("feature_id", help="The FEATURE-NNN id the run lives under.")
    validate.add_argument("run_id", help="The RUN-NNN id to validate.")

    implement = subparsers.add_parser(
        "implement",
        help="Run the Implementer leg: prepare -> run -> validate -> writeback -> "
        "rollup (v0.2 ticket 01, §9.2).",
        parents=[repo_root_parent],
    )
    implement.add_argument("feature_id", help="The FEATURE-NNN id whose tasks/lane-graph are frozen.")
    implement.add_argument("lane_id", help="The LANE-NNN id to implement (must be in 04-lane-graph.yml).")
    implement.add_argument(
        "--profile",
        default=None,
        help="Agent profile to invoke (default: role_defaults[implementer] in "
        "agent-profiles.yml, ticket 03; --profile always overrides, no "
        "allowed-set, no refusal).",
    )
    implement.add_argument(
        "--max-turns",
        type=int,
        default=DEFAULT_MAX_TURNS,
        help="Bounded --max-turns for the headless call (default: 12).",
    )
    implement.add_argument(
        "--permission-mode",
        default=DEFAULT_PERMISSION_MODE,
        help="claude --permission-mode (default: bypassPermissions; the wrapper "
        "enforces the file boundary post-hoc, §14.2).",
    )

    review = subparsers.add_parser(
        "review",
        help="Run the Code Reviewer leg: build -> run -> validate -> "
        "review-report (v0.2 ticket 02, §9.3).",
        parents=[repo_root_parent],
    )
    review.add_argument(
        "feature_id", help="The FEATURE-NNN id whose lane has an implement-result."
    )
    review.add_argument(
        "lane_id", help="The LANE-NNN id to review (must have an implement-result)."
    )
    review.add_argument(
        "--profile",
        default=None,
        help="Agent profile to invoke (default: role_defaults[reviewer] in "
        "agent-profiles.yml, ticket 03; --profile always overrides, no "
        "allowed-set, no refusal).",
    )
    review.add_argument(
        "--max-turns",
        type=int,
        default=DEFAULT_MAX_TURNS,
        help="Bounded --max-turns for the headless call (default: 12).",
    )
    review.add_argument(
        "--permission-mode",
        default=DEFAULT_PERMISSION_MODE,
        help="claude --permission-mode (default: bypassPermissions; the wrapper "
        "enforces the file boundary post-hoc, §14.2).",
    )

    spec_gap = subparsers.add_parser(
        "spec-gap",
        help="Run the Spec Gap Analyst leg: build -> run -> validate -> "
        "spec-gap-report (v0.2 ticket 02, §9.4).",
        parents=[repo_root_parent],
    )
    spec_gap.add_argument(
        "feature_id", help="The FEATURE-NNN id whose lane has an implement-result."
    )
    spec_gap.add_argument(
        "lane_id", help="The LANE-NNN id to gap-analyse (must have an implement-result)."
    )
    spec_gap.add_argument(
        "--profile",
        default=None,
        help="Agent profile to invoke (default: role_defaults[spec_gap_analyst] "
        "in agent-profiles.yml, ticket 03; --profile always overrides, no "
        "allowed-set, no refusal).",
    )
    spec_gap.add_argument(
        "--max-turns",
        type=int,
        default=DEFAULT_MAX_TURNS,
        help="Bounded --max-turns for the headless call (default: 12).",
    )
    spec_gap.add_argument(
        "--permission-mode",
        default=DEFAULT_PERMISSION_MODE,
        help="claude --permission-mode (default: bypassPermissions; the wrapper "
        "enforces the file boundary post-hoc, §14.2).",
    )

    verify = subparsers.add_parser(
        "verify",
        help="Run the shell Verifier leg: execute the lane's declared verify "
        "commands and roll up a verification-report (v0.2 ticket 03, §9.5).",
        parents=[repo_root_parent],
    )
    verify.add_argument(
        "feature_id",
        help="The FEATURE-NNN id whose lane has an implement-result to verify.",
    )
    verify.add_argument(
        "lane_id",
        help="The LANE-NNN id to verify (must declare verification_commands).",
    )
    verify.add_argument(
        "--timeout",
        type=float,
        default=300,
        help="Per-command timeout in seconds (default: 300; a hung command is "
        "recorded as a verification failure, not raised, §24.1).",
    )

    collect = subparsers.add_parser(
        "collect-issues",
        help="Collect reviewer + spec-gap issues into feature issues and the "
        "lane issue-bundle (v0.2 ticket 04, §15).",
        parents=[repo_root_parent],
    )
    collect.add_argument(
        "feature_id",
        help="The FEATURE-NNN id whose lane has checking reports.",
    )
    collect.add_argument(
        "lane_id",
        help="The LANE-NNN id whose checking reports should be collected.",
    )

    lane_gate = subparsers.add_parser(
        "lane-gate",
        help="Evaluate the §18.4 lane gate and write lane-decision.{md,json} "
        "(v0.2 ticket 05).",
        parents=[repo_root_parent],
    )
    lane_gate.add_argument(
        "feature_id",
        help="The FEATURE-NNN id whose lane has implement/verify/bundle artifacts.",
    )
    lane_gate.add_argument(
        "lane_id",
        help="The LANE-NNN id whose gate should be evaluated.",
    )

    coherence_gate = subparsers.add_parser(
        "coherence-gate",
        help="Evaluate the §18.5 feature coherence gate and write the terminal "
        "verdict on feature-status.yml (ADR-0003, v0.3 ticket 08).",
        parents=[repo_root_parent],
    )
    coherence_gate.add_argument(
        "feature_id",
        help="The FEATURE-NNN id whose lane gate has passed and is ready for "
        "the final coherence verdict.",
    )

    final_report = subparsers.add_parser(
        "final-report",
        help="Generate final-report.{json,md} from the coherence verdict "
        "(ADR-0003 D5/D6/D7, v0.3 ticket 09). Deterministic projection - no model.",
        parents=[repo_root_parent],
    )
    final_report.add_argument(
        "feature_id",
        help="The FEATURE-NNN id whose coherence verdict should be projected "
        "into the final report.",
    )

    triage = subparsers.add_parser(
        "triage",
        help="Apply a Human-Triage disposition to one issue (ADR-0001, v0.3 "
        "ticket 05). Deterministic - no model.",
        parents=[repo_root_parent],
    )
    triage.add_argument(
        "feature_id",
        help="The FEATURE-NNN id whose issues/ holds the issue to triage.",
    )
    triage.add_argument(
        "--issue",
        required=True,
        metavar="ISSUE-NNN",
        help="The issue id whose disposition is being written (e.g. ISSUE-001).",
    )
    triage.add_argument(
        "--disposition",
        required=True,
        choices=DISPOSITIONS,
        help="The Human-Triage disposition (§16): accept | reject | defer | "
        "override | request_fix | request_change_proposal.",
    )
    triage.add_argument(
        "--reason",
        default=None,
        help="Recorded rationale. Required for override (P1) and reject on "
        "P0/P1 (ADR-0001 #6); optional otherwise.",
    )
    triage.add_argument(
        "--by",
        default="human",
        help="Who applied the triage (default: human; models may only propose).",
    )

    fix_run = subparsers.add_parser(
        "fix-run",
        help="Run one bounded fix-loop bookend for active request_fix issues "
        "(ADR-0002, v0.3 ticket 07).",
        parents=[repo_root_parent],
    )
    fix_run.add_argument(
        "feature_id",
        help="The FEATURE-NNN id whose request_fix issues should be targeted.",
    )
    fix_run.add_argument(
        "lane_id",
        help="The LANE-NNN id to run through implement/review/spec-gap/verify/collect.",
    )
    fix_run.add_argument(
        "--profile",
        default=None,
        help="Agent profile to invoke for implement/review/spec-gap (default: "
        "each leg's role_defaults entry, ticket 03; --profile, if given, "
        "overrides all three legs - no allowed-set, no refusal).",
    )
    fix_run.add_argument(
        "--max-turns",
        type=int,
        default=DEFAULT_MAX_TURNS,
        help="Bounded --max-turns for each headless agent call (default: 12).",
    )
    fix_run.add_argument(
        "--permission-mode",
        default=DEFAULT_PERMISSION_MODE,
        help="claude --permission-mode for each headless agent call (default: bypassPermissions).",
    )
    fix_run.add_argument(
        "--verify-timeout",
        type=float,
        default=300,
        help="Per-command verifier timeout in seconds (default: 300).",
    )

    # v0.4 ticket 03: the three read-only observability commands (§26.5 CLI UX).
    # They carry the shared ``--json`` flag (human-readable by default, JSON
    # opt-in) on top of the shared ``--repo-root`` parent. They are deliberately
    # absent from ``_DRY_RUN_COMMANDS`` below — a dry-run flag on a command with
    # no side effects is noise.
    subparsers.add_parser(
        "list-features",
        help="List every FEATURE-NNN with its derived status + current gate "
        "(v0.4 ticket 03). Read-only.",
        parents=[repo_root_parent, json_parent],
    )

    show_status = subparsers.add_parser(
        "show-status",
        help="Show a feature's gate/verdict/derived status + each lane's "
        "lane-decision (v0.4 ticket 03). Read-only.",
        parents=[repo_root_parent, json_parent],
    )
    show_status.add_argument(
        "feature_id", help="The FEATURE-NNN id to inspect."
    )

    log_cmd = subparsers.add_parser(
        "log",
        help="Pretty-print a feature's audit timeline (v0.4 ticket 03). "
        "Read-only; renders audit.log.json (consumes ticket 02's "
        "origin/elapsed_ms).",
        parents=[repo_root_parent, json_parent],
    )
    log_cmd.add_argument(
        "feature_id", help="The FEATURE-NNN id whose audit timeline to print."
    )

    # v0.5 ticket 06: non-canonical side-by-side projection of two parallel
    # feature-runs (same intent, one profile each). Read-only over canonical
    # state; writes only the non-canonical projection. Supports the global
    # ``--json`` flag (stdout form) and ``--dry-run`` (plan only).
    compare_profiles = subparsers.add_parser(
        "compare-profiles",
        help="Project a side-by-side comparison of two parallel feature-runs "
             "(same intent, one profile each) into "
             "projections/profile-comparison.{json,md} (v0.5 ticket 06, "
             "ADR-0003-style non-canonical projection). Read-only.",
        parents=[repo_root_parent, json_parent],
    )
    compare_profiles.add_argument(
        "feature_id",
        help="The anchor FEATURE-NNN (one of the two compared runs; the "
             "projection lands in its projections/ dir).",
    )
    compare_profiles.add_argument(
        "--profiles",
        required=True,
        help="Exactly two comma-separated profile names to compare, e.g. "
             "cc-glm52,codex-default. Each is matched to the intent-sibling "
             "feature-run whose implementer used it.",
    )

    # ADR-0004: attach ``--dry-run`` to every side-effect subparser in one place
    # rather than repeating the add_argument per command. Read-only commands are
    # excluded (a dry-run flag on a command with no side effects is noise).
    for name, sub in subparsers.choices.items():
        if name in _DRY_RUN_COMMANDS:
            _add_dry_run(sub)

    return parser


# The side-effect commands that accept ``--dry-run`` (ADR-0004). Agent commands
# spawn a claude subprocess; deterministic commands write canonical state.
# Already-pure/read-only commands (show-profile, validate-run, the v0.4
# read-only commands) are deliberately excluded - a dry-run flag on a command
# with no side effects is noise.
_DRY_RUN_COMMANDS: frozenset[str] = frozenset(
    {
        "run-headless",
        "implement",
        "review",
        "spec-gap",
        "fix-run",
        "freeze",
        "triage",
        "coherence-gate",
        "final-report",
        "lane-gate",
        "compare-profiles",
    }
)


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
    """Resolve the feature run and delegate to ``freeze_artifact``.

    Returns a process exit code: ``0`` on a successful freeze, ``1`` if the run
    is missing or the artifact is already frozen (§4.2 monotonic — re-freezing
    is rejected, not silently reapplied). Other failures propagate (§24.2 fail
    loud).
    """
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        _render_error(
            ValueError(f"feature run {feature_id} not found under {repo_root}"),
            hint=_lookup_hint(repo_root, feature_id),
        )
        return 1
    try:
        freeze_artifact(feature_root, artifact, origin=ORIGIN_CLI)
    except FrozenArtifactError as exc:
        _render_error(exc)
        return 1
    print(f"{feature_id}: froze {artifact}")
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
    try:
        profile = load_profile(repo_root, name)
    except ProfileError as exc:
        _render_error(exc)
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
        _render_error(exc, hint=_lookup_hint(repo_root, feature_id))
        return 1
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
    try:
        profile = load_profile(repo_root, profile_name)
    except ProfileError as exc:
        _render_error(exc)
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
        _render_error(exc, hint=_lookup_hint(repo_root, feature_id, run_id))
        return 1
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
        _render_error(exc, hint=_lookup_hint(repo_root, feature_id, run_id))
        return 1
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
    try:
        profile = load_profile(repo_root, profile_name)
    except ProfileError as exc:
        _render_error(exc)
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
        _render_error(exc, hint=_lookup_hint(repo_root, feature_id))
        return 1
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
    try:
        profile = load_profile(repo_root, profile_name)
    except ProfileError as exc:
        _render_error(exc)
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
        _render_error(exc, hint=_lookup_hint(repo_root, feature_id))
        return 1
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
        _render_error(exc, hint=_lookup_hint(repo_root, feature_id))
        return 1
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
        _render_error(exc, hint=_lookup_hint(repo_root, feature_id))
        return 1
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
        _render_error(exc, hint=_lookup_hint(repo_root, feature_id))
        return 1
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
        _render_error(exc, hint=_lookup_hint(repo_root, feature_id))
        return 1
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
        _render_error(exc, hint=_lookup_hint(repo_root, feature_id))
        return 1
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
        _render_error(exc, hint=_lookup_hint(repo_root, feature_id))
        return 1
    if as_json:
        _print_json(json.loads(result.projection_json_path.read_text()))
    else:
        print(
            f"COMPARE-PROFILES - feature={result.feature_id} "
            f"profiles={','.join(profile_names)} "
            f"projection={result.projection_json_path}"
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
        _render_error(exc, hint=_lookup_hint(repo_root, feature_id))
        return 1
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
        _render_error(exc, hint=_lookup_hint(repo_root, feature_id))
        return 1
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
        _render_error(exc, hint=_lookup_hint(repo_root, feature_id))
        return 1
    if as_json:
        _print_json([record.to_dict() for record in records])
    else:
        _render_log(records)
    return 0


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

    Expected failures (missing feature/run, refused triage, bad input) are
    surfaced by each ``_run_*`` through ``_render_error`` + ``return 1``; this
    function lets anything else propagate to ``main``'s top-level handler.
    """
    if args.command == "create-feature-run":
        feature_id = create_feature_run(Path(args.repo_root), args.intent, origin=ORIGIN_CLI)
        print(feature_id)
        return 0

    if args.command == "freeze":
        if args.dry_run:
            return _run_dry_plan(
                lambda: plan_freeze(
                    Path(args.repo_root), args.feature_id, args.artifact
                )
            )
        return _run_freeze(Path(args.repo_root), args.feature_id, args.artifact)

    if args.command == "show-profile":
        return _run_show_profile(Path(args.repo_root), args.name)

    if args.command == "list-features":
        return _run_list_features(Path(args.repo_root), args.json)

    if args.command == "show-status":
        return _run_show_status(Path(args.repo_root), args.feature_id, args.json)

    if args.command == "log":
        return _run_log(Path(args.repo_root), args.feature_id, args.json)

    if args.command == "prepare-run":
        return _run_prepare_run(
            Path(args.repo_root),
            args.feature_id,
            args.role,
            args.task,
            args.allowed_file,
        )

    if args.command == "run-headless":
        if args.dry_run:
            return _run_dry_plan(
                lambda: plan_run_headless(
                    Path(args.repo_root),
                    args.feature_id,
                    args.run_id,
                    load_profile(Path(args.repo_root), args.profile),
                    max_turns=args.max_turns,
                    permission_mode=args.permission_mode,
                )
            )
        return _run_run_headless(
            Path(args.repo_root),
            args.feature_id,
            args.run_id,
            args.profile,
            args.max_turns,
            args.permission_mode,
        )

    if args.command == "validate-run":
        return _run_validate_run(Path(args.repo_root), args.feature_id, args.run_id)

    if args.command == "implement":
        repo_root = Path(args.repo_root)
        try:
            profile_name = resolve_profile_name(
                repo_root, ROLE_IMPLEMENTER, args.profile
            )
        except ProfileError as exc:
            _render_error(exc)
            return 1
        if args.dry_run:
            return _run_dry_plan(
                lambda: plan_implement(
                    repo_root,
                    args.feature_id,
                    args.lane_id,
                    load_profile(repo_root, profile_name),
                    max_turns=args.max_turns,
                    permission_mode=args.permission_mode,
                )
            )
        # v0.5 ticket 06: record the resolved implementer profile on the feature's
        # agent_profiles config (the record compare-profiles reads). Dry-run skips
        # it — recording is a canonical status write.
        record_agent_profile(
            feature_dir(repo_root, args.feature_id),
            ROLE_IMPLEMENTER,
            profile_name,
            origin=ORIGIN_IMPLEMENT_LEG,
        )
        return _run_implement(
            repo_root,
            args.feature_id,
            args.lane_id,
            profile_name,
            args.max_turns,
            args.permission_mode,
        )

    if args.command == "review":
        repo_root = Path(args.repo_root)
        try:
            profile_name = resolve_profile_name(
                repo_root, ROLE_REVIEWER, args.profile
            )
        except ProfileError as exc:
            _render_error(exc)
            return 1
        if args.dry_run:
            return _run_dry_plan(
                lambda: plan_review(
                    repo_root,
                    args.feature_id,
                    args.lane_id,
                    load_profile(repo_root, profile_name),
                    max_turns=args.max_turns,
                    permission_mode=args.permission_mode,
                )
            )
        record_agent_profile(
            feature_dir(repo_root, args.feature_id),
            ROLE_REVIEWER,
            profile_name,
            origin=ORIGIN_REVIEW_LEG,
        )
        return _run_checking(
            repo_root,
            args.feature_id,
            args.lane_id,
            profile_name,
            args.max_turns,
            args.permission_mode,
            leg=run_reviewer_leg,
            label="REVIEW",
            origin=ORIGIN_REVIEW_LEG,
        )

    if args.command == "spec-gap":
        repo_root = Path(args.repo_root)
        try:
            profile_name = resolve_profile_name(
                repo_root, ROLE_SPEC_GAP_ANALYST, args.profile
            )
        except ProfileError as exc:
            _render_error(exc)
            return 1
        if args.dry_run:
            return _run_dry_plan(
                lambda: plan_spec_gap(
                    repo_root,
                    args.feature_id,
                    args.lane_id,
                    load_profile(repo_root, profile_name),
                    max_turns=args.max_turns,
                    permission_mode=args.permission_mode,
                )
            )
        record_agent_profile(
            feature_dir(repo_root, args.feature_id),
            ROLE_SPEC_GAP_ANALYST,
            profile_name,
            origin=ORIGIN_SPEC_GAP_LEG,
        )
        return _run_checking(
            repo_root,
            args.feature_id,
            args.lane_id,
            profile_name,
            args.max_turns,
            args.permission_mode,
            leg=run_spec_gap_leg,
            label="SPEC-GAP",
            origin=ORIGIN_SPEC_GAP_LEG,
        )

    if args.command == "verify":
        return _run_verify(
            Path(args.repo_root),
            args.feature_id,
            args.lane_id,
            args.timeout,
        )

    if args.command == "collect-issues":
        return _run_collect_issues(
            Path(args.repo_root),
            args.feature_id,
            args.lane_id,
        )

    if args.command == "lane-gate":
        if args.dry_run:
            return _run_dry_plan(
                lambda: plan_lane_gate(
                    Path(args.repo_root), args.feature_id, args.lane_id
                )
            )
        return _run_lane_gate(
            Path(args.repo_root),
            args.feature_id,
            args.lane_id,
        )

    if args.command == "coherence-gate":
        if args.dry_run:
            return _run_dry_plan(
                lambda: plan_coherence_gate(Path(args.repo_root), args.feature_id)
            )
        return _run_coherence_gate(Path(args.repo_root), args.feature_id)

    if args.command == "final-report":
        if args.dry_run:
            return _run_dry_plan(
                lambda: plan_final_report(Path(args.repo_root), args.feature_id)
            )
        return _run_final_report(Path(args.repo_root), args.feature_id)

    if args.command == "compare-profiles":
        profile_names = [p.strip() for p in args.profiles.split(",") if p.strip()]
        if args.dry_run:
            return _run_dry_plan(
                lambda: plan_compare_profiles(
                    Path(args.repo_root), args.feature_id, profile_names
                )
            )
        return _run_compare_profiles(
            Path(args.repo_root), args.feature_id, profile_names, args.json
        )

    if args.command == "fix-run":
        repo_root = Path(args.repo_root)
        # --profile (override) applies to all three legs; when absent each leg
        # resolves its own role default (ticket 03: fix-run uses per-leg role
        # defaults). No allowed-set, no refusal - a bad name surfaces at load.
        try:
            implement_name = resolve_profile_name(
                repo_root, ROLE_IMPLEMENTER, args.profile
            )
            reviewer_name = resolve_profile_name(
                repo_root, ROLE_REVIEWER, args.profile
            )
            spec_gap_name = resolve_profile_name(
                repo_root, ROLE_SPEC_GAP_ANALYST, args.profile
            )
        except ProfileError as exc:
            _render_error(exc)
            return 1
        if args.dry_run:
            return _run_dry_plan(
                lambda: plan_fix_run(
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
            )
        # v0.5 ticket 06: fix-run drives all three legs — record each resolved
        # profile onto the feature's agent_profiles config (compare-profiles record).
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

    if args.command == "triage":
        if args.dry_run:
            return _run_dry_plan(
                lambda: plan_triage(
                    Path(args.repo_root),
                    args.feature_id,
                    args.issue,
                    args.disposition,
                    args.reason,
                    args.by,
                )
            )
        return _run_triage(
            Path(args.repo_root),
            args.feature_id,
            args.issue,
            args.disposition,
            args.reason,
            args.by,
        )

    # Unreachable: argparse rejects unknown/missing subcommands before we get
    # here (required=True). error() is NoReturn, so this ends the function.
    parser.error(f"unknown command: {args.command!r}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
