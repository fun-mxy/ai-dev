"""``ai-dev`` console entry point — the deterministic Python runtime (spec §25.3).

v0.0 surface: ``ai-dev create-feature-run "<intent>"``. Structured as a
subcommand dispatcher so later tickets add commands (``allocate-id``,
``freeze``, ``append-audit``, …) without disturbing this one.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from ai_dev.feature_run import create_feature_run


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

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch a CLI invocation. Returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "create-feature-run":
        feature_id = create_feature_run(Path(args.repo_root), args.intent)
        print(feature_id)
        return 0

    # Unreachable: argparse rejects unknown/missing subcommands before we get
    # here (required=True). error() is NoReturn, so this ends the function.
    parser.error(f"unknown command: {args.command!r}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
