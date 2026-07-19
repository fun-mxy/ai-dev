"""``ai-dev`` console entry point — the deterministic Python runtime (spec §25.3).

v0.0 surface:

* ``ai-dev create-feature-run "<intent>"`` (ticket 01)
* ``ai-dev freeze <FEATURE-NNN> <artifact>`` (ticket 04) — the deterministic,
  model-free entry point for the §4.2 freeze operation. Because the only way to
  flip a frozen flag is this command (or an equivalent runtime call into
  ``status.freeze_artifact``), no model ever writes canonical freeze state
  (§4.3 cardinal rule).

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

    # Unreachable: argparse rejects unknown/missing subcommands before we get
    # here (required=True). error() is NoReturn, so this ends the function.
    parser.error(f"unknown command: {args.command!r}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
