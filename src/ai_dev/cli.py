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

Structured as a subcommand dispatcher so later tickets add commands
(``allocate-id``, ``append-audit``, …) without disturbing the existing ones.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from ai_dev.feature_run import create_feature_run
from ai_dev.paths import feature_dir
from ai_dev.profiles import (
    ProfileError,
    load_profile,
    render_profile,
    token_source_var,
)
from ai_dev.status import FROZEN_ARTIFACTS, FrozenArtifactError, freeze_artifact


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

    # Unreachable: argparse rejects unknown/missing subcommands before we get
    # here (required=True). error() is NoReturn, so this ends the function.
    parser.error(f"unknown command: {args.command!r}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
