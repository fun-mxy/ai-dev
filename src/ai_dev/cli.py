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
import sys
from pathlib import Path
from typing import Callable, Sequence

from ai_dev.checking_legs import CheckingLegResult, run_reviewer_leg, run_spec_gap_leg
from ai_dev.coherence_gate import CoherenceResult, evaluate_coherence_gate
from ai_dev.feature_run import create_feature_run
from ai_dev.final_report import FinalReportResult, generate_final_report
from ai_dev.fix_run import FixRunResult, run_fix_run
from ai_dev.implement_leg import run_implementer_leg
from ai_dev.issue_bundle import IssueBundleResult, collect_issue_bundle
from ai_dev.lane_gate import LaneDecisionResult, evaluate_lane_gate
from ai_dev.paths import feature_dir
from ai_dev.profiles import (
    ProfileError,
    load_profile,
    render_profile,
    token_source_var,
)
from ai_dev.run_prepare import prepare_run
from ai_dev.run_wrapper import DEFAULT_MAX_TURNS, DEFAULT_PERMISSION_MODE, run_headless
from ai_dev.shell_verifier import CommandResult, run_verifier
from ai_dev.status import FROZEN_ARTIFACTS, FrozenArtifactError, freeze_artifact
from ai_dev.triage import DISPOSITIONS, TriageRefusedError, TriageResult, apply_triage
from ai_dev.validate import ValidationIssue, validate_run


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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-dev",
        description="Multi-Agent Profile orchestrator (v0 walking skeleton).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser(
        "create-feature-run",
        help="Create a new feature run from an intent string (ticket 01).",
    )
    create.add_argument("intent", help="The original user intent text to record.")
    create.add_argument(
        "--repo-root",
        default=".",
        help="Repository root to create .ai-dev/ under (default: current directory).",
    )

    freeze = subparsers.add_parser(
        "freeze",
        help="Freeze a canonical artifact after its human gate passes (§4.2, ticket 04).",
    )
    freeze.add_argument("feature_id", help="The FEATURE-NNN id of the run to update.")
    freeze.add_argument(
        "artifact",
        choices=FROZEN_ARTIFACTS,
        help="Which frozen artifact to flip (one of the §4.2 four).",
    )
    freeze.add_argument(
        "--repo-root",
        default=".",
        help="Repository root holding .ai-dev/ (default: current directory).",
    )

    show = subparsers.add_parser(
        "show-profile",
        help="Load and display a resolved agent profile (§10.1, run-adapter ticket 01).",
    )
    show.add_argument("name", help="The profile name to resolve (e.g. cc-glm52).")
    show.add_argument(
        "--repo-root",
        default=".",
        help="Repository root holding .ai-dev/agent-profiles.yml (default: current dir).",
    )

    prepare = subparsers.add_parser(
        "prepare-run",
        help="Allocate RUN-NNN and scaffold its input package (§12, ticket 02).",
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
    prepare.add_argument(
        "--repo-root",
        default=".",
        help="Repository root holding .ai-dev/ (default: current directory).",
    )

    run = subparsers.add_parser(
        "run-headless",
        help="Run a prepared RUN-NNN headless via a profile and capture it (§11, ticket 03).",
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
    run.add_argument(
        "--repo-root",
        default=".",
        help="Repository root holding .ai-dev/ (default: current directory).",
    )

    validate = subparsers.add_parser(
        "validate-run",
        help="Run the §14 deterministic validation (schema + boundary + frozen) "
        "on a captured run (ticket 04).",
    )
    validate.add_argument("feature_id", help="The FEATURE-NNN id the run lives under.")
    validate.add_argument("run_id", help="The RUN-NNN id to validate.")
    validate.add_argument(
        "--repo-root",
        default=".",
        help="Repository root holding .ai-dev/ (default: current directory).",
    )

    implement = subparsers.add_parser(
        "implement",
        help="Run the Implementer leg: prepare -> run -> validate -> writeback -> "
        "rollup (v0.2 ticket 01, §9.2).",
    )
    implement.add_argument("feature_id", help="The FEATURE-NNN id whose tasks/lane-graph are frozen.")
    implement.add_argument("lane_id", help="The LANE-NNN id to implement (must be in 04-lane-graph.yml).")
    implement.add_argument(
        "--profile",
        default="cc-glm52",
        help="Agent profile to invoke (default: cc-glm52, the v0 recommended "
        "profile, §23.4).",
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
    implement.add_argument(
        "--repo-root",
        default=".",
        help="Repository root holding .ai-dev/ (default: current directory).",
    )

    review = subparsers.add_parser(
        "review",
        help="Run the Code Reviewer leg: build -> run -> validate -> "
        "review-report (v0.2 ticket 02, §9.3).",
    )
    review.add_argument(
        "feature_id", help="The FEATURE-NNN id whose lane has an implement-result."
    )
    review.add_argument(
        "lane_id", help="The LANE-NNN id to review (must have an implement-result)."
    )
    review.add_argument(
        "--profile",
        default="cc-glm52",
        help="Agent profile to invoke (default: cc-glm52, the v0 recommended "
        "profile, §23.4).",
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
    review.add_argument(
        "--repo-root",
        default=".",
        help="Repository root holding .ai-dev/ (default: current directory).",
    )

    spec_gap = subparsers.add_parser(
        "spec-gap",
        help="Run the Spec Gap Analyst leg: build -> run -> validate -> "
        "spec-gap-report (v0.2 ticket 02, §9.4).",
    )
    spec_gap.add_argument(
        "feature_id", help="The FEATURE-NNN id whose lane has an implement-result."
    )
    spec_gap.add_argument(
        "lane_id", help="The LANE-NNN id to gap-analyse (must have an implement-result)."
    )
    spec_gap.add_argument(
        "--profile",
        default="cc-glm52",
        help="Agent profile to invoke (default: cc-glm52, the v0 recommended "
        "profile, §23.4).",
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
    spec_gap.add_argument(
        "--repo-root",
        default=".",
        help="Repository root holding .ai-dev/ (default: current directory).",
    )

    verify = subparsers.add_parser(
        "verify",
        help="Run the shell Verifier leg: execute the lane's declared verify "
        "commands and roll up a verification-report (v0.2 ticket 03, §9.5).",
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
    verify.add_argument(
        "--repo-root",
        default=".",
        help="Repository root holding .ai-dev/ (default: current directory).",
    )

    collect = subparsers.add_parser(
        "collect-issues",
        help="Collect reviewer + spec-gap issues into feature issues and the "
        "lane issue-bundle (v0.2 ticket 04, §15).",
    )
    collect.add_argument(
        "feature_id",
        help="The FEATURE-NNN id whose lane has checking reports.",
    )
    collect.add_argument(
        "lane_id",
        help="The LANE-NNN id whose checking reports should be collected.",
    )
    collect.add_argument(
        "--repo-root",
        default=".",
        help="Repository root holding .ai-dev/ (default: current directory).",
    )

    lane_gate = subparsers.add_parser(
        "lane-gate",
        help="Evaluate the §18.4 lane gate and write lane-decision.{md,json} "
        "(v0.2 ticket 05).",
    )
    lane_gate.add_argument(
        "feature_id",
        help="The FEATURE-NNN id whose lane has implement/verify/bundle artifacts.",
    )
    lane_gate.add_argument(
        "lane_id",
        help="The LANE-NNN id whose gate should be evaluated.",
    )
    lane_gate.add_argument(
        "--repo-root",
        default=".",
        help="Repository root holding .ai-dev/ (default: current directory).",
    )

    coherence_gate = subparsers.add_parser(
        "coherence-gate",
        help="Evaluate the §18.5 feature coherence gate and write the terminal "
        "verdict on feature-status.yml (ADR-0003, v0.3 ticket 08).",
    )
    coherence_gate.add_argument(
        "feature_id",
        help="The FEATURE-NNN id whose lane gate has passed and is ready for "
        "the final coherence verdict.",
    )
    coherence_gate.add_argument(
        "--repo-root",
        default=".",
        help="Repository root holding .ai-dev/ (default: current directory).",
    )

    final_report = subparsers.add_parser(
        "final-report",
        help="Generate final-report.{json,md} from the coherence verdict "
        "(ADR-0003 D5/D6/D7, v0.3 ticket 09). Deterministic projection - no model.",
    )
    final_report.add_argument(
        "feature_id",
        help="The FEATURE-NNN id whose coherence verdict should be projected "
        "into the final report.",
    )
    final_report.add_argument(
        "--repo-root",
        default=".",
        help="Repository root holding .ai-dev/ (default: current directory).",
    )

    triage = subparsers.add_parser(
        "triage",
        help="Apply a Human-Triage disposition to one issue (ADR-0001, v0.3 "
        "ticket 05). Deterministic - no model.",
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
    triage.add_argument(
        "--repo-root",
        default=".",
        help="Repository root holding .ai-dev/ (default: current directory).",
    )

    fix_run = subparsers.add_parser(
        "fix-run",
        help="Run one bounded fix-loop bookend for active request_fix issues "
        "(ADR-0002, v0.3 ticket 07).",
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
        default="cc-glm52",
        help="Agent profile to invoke for implement/review/spec-gap (default: cc-glm52).",
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
    fix_run.add_argument(
        "--repo-root",
        default=".",
        help="Repository root holding .ai-dev/ (default: current directory).",
    )

    return parser


def _run_freeze(repo_root: Path, feature_id: str, artifact: str) -> int:
    """Resolve the feature run and delegate to ``freeze_artifact``.

    Returns a process exit code: ``0`` on a successful freeze, ``1`` if the run
    is missing or the artifact is already frozen (§4.2 monotonic — re-freezing
    is rejected, not silently reapplied). Other failures propagate (§24.2 fail
    loud).
    """
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        print(
            f"error: feature run {feature_id} not found under {repo_root}",
            file=sys.stderr,
        )
        return 1
    try:
        freeze_artifact(feature_root, artifact)
    except FrozenArtifactError as exc:
        print(f"error: {exc}", file=sys.stderr)
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
        print(f"error: {exc}", file=sys.stderr)
        return 1

    source = token_source_var(profile)
    print(render_profile(profile, source))

    if source is None:
        print(
            f"error: token source not set for profile {name!r} "
            f"({profile.token_source_description()} is unset); "
            f"set it before running (§24.2)",
            file=sys.stderr,
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
            repo_root, feature_id, role, task, allowed_files=allowed_files
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
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
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        result = run_headless(
            repo_root,
            feature_id,
            run_id,
            profile,
            max_turns=max_turns,
            permission_mode=permission_mode,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
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
        result = validate_run(repo_root, feature_id, run_id)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
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
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        result = run_implementer_leg(
            repo_root,
            feature_id,
            lane_id,
            profile,
            max_turns=max_turns,
            permission_mode=permission_mode,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
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
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        result = leg(
            repo_root,
            feature_id,
            lane_id,
            profile,
            max_turns=max_turns,
            permission_mode=permission_mode,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
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
            repo_root, feature_id, lane_id, timeout=timeout
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
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
        result: IssueBundleResult = collect_issue_bundle(repo_root, feature_id, lane_id)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
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
        result: LaneDecisionResult = evaluate_lane_gate(repo_root, feature_id, lane_id)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
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
        result: CoherenceResult = evaluate_coherence_gate(repo_root, feature_id)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
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
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"FINAL-REPORT - feature={result.feature_id} verdict={result.verdict} "
        f"failure_class={result.failure_class} report={result.report_json_path}"
    )
    return 0


def _run_fix_run(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    profile_name: str,
    max_turns: int,
    permission_mode: str,
    verify_timeout: float,
) -> int:
    """Run one bounded fix-loop bookend and stop before human re-triage."""
    try:
        profile = load_profile(repo_root, profile_name)
    except ProfileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        result: FixRunResult = run_fix_run(
            repo_root,
            feature_id,
            lane_id,
            profile,
            max_turns=max_turns,
            permission_mode=permission_mode,
            verify_timeout=verify_timeout,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
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
            repo_root, feature_id, issue_id, disposition, reason, by
        )
    except (TriageRefusedError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    decisions = ",".join(result.decision_ids) if result.decision_ids else "-"
    print(
        f"TRIAGE PASS - issue={result.issue_id} disposition={result.action} "
        f"severity={result.severity} decisions={decisions}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch a CLI invocation. Returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "create-feature-run":
        feature_id = create_feature_run(Path(args.repo_root), args.intent)
        print(feature_id)
        return 0

    if args.command == "freeze":
        return _run_freeze(Path(args.repo_root), args.feature_id, args.artifact)

    if args.command == "show-profile":
        return _run_show_profile(Path(args.repo_root), args.name)

    if args.command == "prepare-run":
        return _run_prepare_run(
            Path(args.repo_root),
            args.feature_id,
            args.role,
            args.task,
            args.allowed_file,
        )

    if args.command == "run-headless":
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
        return _run_implement(
            Path(args.repo_root),
            args.feature_id,
            args.lane_id,
            args.profile,
            args.max_turns,
            args.permission_mode,
        )

    if args.command == "review":
        return _run_checking(
            Path(args.repo_root),
            args.feature_id,
            args.lane_id,
            args.profile,
            args.max_turns,
            args.permission_mode,
            leg=run_reviewer_leg,
            label="REVIEW",
        )

    if args.command == "spec-gap":
        return _run_checking(
            Path(args.repo_root),
            args.feature_id,
            args.lane_id,
            args.profile,
            args.max_turns,
            args.permission_mode,
            leg=run_spec_gap_leg,
            label="SPEC-GAP",
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
        return _run_lane_gate(
            Path(args.repo_root),
            args.feature_id,
            args.lane_id,
        )

    if args.command == "coherence-gate":
        return _run_coherence_gate(Path(args.repo_root), args.feature_id)

    if args.command == "final-report":
        return _run_final_report(Path(args.repo_root), args.feature_id)

    if args.command == "fix-run":
        return _run_fix_run(
            Path(args.repo_root),
            args.feature_id,
            args.lane_id,
            args.profile,
            args.max_turns,
            args.permission_mode,
            args.verify_timeout,
        )

    if args.command == "triage":
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
