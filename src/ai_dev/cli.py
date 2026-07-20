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

Structured as a subcommand dispatcher so later tickets add commands
(``allocate-id``, ``append-audit``, …) without disturbing the existing ones.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from ai_dev.feature_run import create_feature_run
from ai_dev.implement_leg import run_implementer_leg
from ai_dev.paths import feature_dir
from ai_dev.profiles import (
    ProfileError,
    load_profile,
    render_profile,
    token_source_var,
)
from ai_dev.run_prepare import prepare_run
from ai_dev.run_wrapper import DEFAULT_MAX_TURNS, DEFAULT_PERMISSION_MODE, run_headless
from ai_dev.status import FROZEN_ARTIFACTS, FrozenArtifactError, freeze_artifact
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

    # Unreachable: argparse rejects unknown/missing subcommands before we get
    # here (required=True). error() is NoReturn, so this ends the function.
    parser.error(f"unknown command: {args.command!r}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
