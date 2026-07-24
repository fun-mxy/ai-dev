"""The ``COMMANDS`` registry is the single source of truth for the cli command
surface (steps 3-4 of the command-registry migration).

These guard the invariants the ``_dispatch`` loop and ``_build_parser`` loop
assume - so a future contributor who adds a row but forgets to wire it (or
mismatches the dry-run / json flags) gets a loud, localized failure rather than
a silently-misbehaving command.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

from ai_dev.cli import COMMANDS, _agent_command, _build_parser
from ai_dev.profiles import AgentProfile


def _subparser_choices(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    """The ``{name: subparser}`` map argparse builds from ``COMMANDS``."""
    return parser._subparsers._group_actions[0].choices  # type: ignore[attr-defined]


def _dests(sub: argparse.ArgumentParser) -> set[str]:
    return {a.dest for a in sub._actions}


class TestRegistryCompleteness:
    def test_names_are_unique(self) -> None:
        names = [c.name for c in COMMANDS]
        assert len(names) == len(set(names))

    def test_every_row_is_wired_into_the_parser(self) -> None:
        choices = _subparser_choices(_build_parser())
        assert set(choices) == {c.name for c in COMMANDS}

    def test_dry_run_flag_iff_plan_not_none(self) -> None:
        """``--dry-run`` attaches exactly to side-effect commands (ADR-0004)."""
        choices = _subparser_choices(_build_parser())
        by_name = {c.name: c for c in COMMANDS}
        for name, sub in choices.items():
            has_dry = "dry_run" in _dests(sub)
            assert has_dry == (by_name[name].plan is not None), name

    def test_json_flag_iff_json_field(self) -> None:
        """``--json`` attaches exactly to the read-only ``json=True`` rows."""
        choices = _subparser_choices(_build_parser())
        by_name = {c.name: c for c in COMMANDS}
        for name, sub in choices.items():
            has_json = "json" in _dests(sub)
            assert has_json == by_name[name].json, name

    def test_every_row_carries_help_and_args(self) -> None:
        for cmd in COMMANDS:
            assert cmd.help_text, cmd.name
            assert callable(cmd.add_args), cmd.name
            assert callable(cmd.run), cmd.name


class TestAgentCommandFactory:
    """``_agent_command``: the resolve/record preamble shared by the six agent
    commands. Verifies the contract the dispatch loop relies on - the real path
    records the resolved name and hands it to ``real``; the dry path loads the
    profile and hands it to ``plan`` and never records."""

    def test_real_path_records_then_runs_with_name(
        self, repo_root: Path, write_profiles: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_profiles(repo_root)
        from ai_dev import cli

        recorded: dict[str, Any] = {}

        def fake_record(feature_root: Path, role: str, profile: str, *, origin: str | None = None) -> None:
            recorded["role"] = role
            recorded["profile"] = profile
            recorded["origin"] = origin

        monkeypatch.setattr(cli, "record_agent_profile", fake_record)

        def real(repo: Path, fid: str, name: str, a: argparse.Namespace) -> int:
            recorded["real_name"] = name
            return 7

        def plan(repo: Path, fid: str, prof: AgentProfile, a: argparse.Namespace) -> Any:
            raise AssertionError("plan must not run on the real path")

        cmd = _agent_command(
            "x",
            "help",
            lambda s: None,
            cli.ROLE_IMPLEMENTER,
            "ORIGIN_X",
            real=real,
            plan=plan,
        )
        args = argparse.Namespace(
            repo_root=str(repo_root), feature_id="FEATURE-1", profile=None
        )

        assert cmd.run(args) == 7
        # role_defaults[implementer] -> cc-glm52 (conftest's registry).
        assert recorded == {
            "role": cli.ROLE_IMPLEMENTER,
            "profile": "cc-glm52",
            "origin": "ORIGIN_X",
            "real_name": "cc-glm52",
        }

    def test_real_path_profile_override_skips_role_defaults(
        self, repo_root: Path, write_profiles: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_profiles(repo_root)
        from ai_dev import cli

        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            cli, "record_agent_profile", lambda fr, r, p, *, origin=None: seen.update(name=p)
        )

        def real(repo: Path, fid: str, name: str, a: argparse.Namespace) -> int:
            seen["real"] = name
            return 0

        cmd = _agent_command(
            "x", "help", lambda s: None, cli.ROLE_REVIEWER, "O",
            real=real,
            plan=lambda *a: None,
        )
        args = argparse.Namespace(
            repo_root=str(repo_root), feature_id="FEATURE-1", profile="codex-default"
        )
        assert cmd.run(args) == 0
        assert seen["name"] == "codex-default"
        assert seen["real"] == "codex-default"

    def test_dry_path_loads_and_plans_without_recording(
        self, repo_root: Path, write_profiles: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_profiles(repo_root)
        from ai_dev import cli

        recorded: list[Any] = []
        monkeypatch.setattr(
            cli, "record_agent_profile", lambda *a, **k: recorded.append(1)
        )
        plan_seen: dict[str, Any] = {}

        def plan(repo: Path, fid: str, prof: AgentProfile, a: argparse.Namespace) -> Any:
            plan_seen["profile"] = prof
            return "PLAN"

        def real(*a: Any, **k: Any) -> int:
            raise AssertionError("real must not run on the dry path")

        cmd = _agent_command(
            "x", "help", lambda s: None, cli.ROLE_PLANNER, "ORIGIN_X",
            real=real, plan=plan,
        )
        args = argparse.Namespace(
            repo_root=str(repo_root), feature_id="FEATURE-1", profile=None
        )

        assert cmd.plan(args) == "PLAN"
        assert recorded == []  # dry-run never records
        assert isinstance(plan_seen["profile"], AgentProfile)
