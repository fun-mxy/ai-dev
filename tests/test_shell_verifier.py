"""shell_verifier - v0.2 ticket 03, the shell Verifier leg (§9.5).

The Verifier is the third checking role in the v0.2 loop (§26.3), but unlike
the reviewer (§9.3) and gap (§9.4) it is a **non-agent run kind**: a
deterministic shell adapter that does NOT go through ``claude -p`` and does NOT
call a model (§9.5 MVP). Given an implement run's workspace changes, it runs the
lane's declared verify command set (pytest/mypy/build, source: the frozen
lane-graph's ``verification_commands``), captures each command's pass/fail +
stdout/stderr summary, and rolls them up into the lane-level
``verification-report.{md,json}`` §4.4 double product with an overall verdict.

These tests pin the seams the ticket names: command parsing (fail-loud on
missing/malformed), per-command execution (pass/fail/not-found/timeout),
the report rollup (field-complete, NO ``issues[]``, verdict pass iff all pass),
the multi-command pass/fail mix, fail-loud preconditions, the orchestration
(commands run in the implement workspace, report + audit written), and the CLI.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

from ai_dev.audit import AUDIT_LOG_JSON
from ai_dev.cli import main
from ai_dev.feature_run import create_feature_run
from ai_dev.paths import lane_dir, run_dir
from ai_dev.run_prepare import prepare_run
from ai_dev.shell_verifier import (
    DEFAULT_TIMEOUT,
    VERIFICATION_DIR,
    VERIFICATION_REPORT_JSON,
    VERIFICATION_REPORT_MD,
    CommandResult,
    VerifyCommand,
    _lane_worktree_root,
    read_implement_run_id,
    read_verification_commands,
    run_verify_command,
    run_verifier,
    write_verification_report,
)
from ai_dev.lane_worktree import create_lane_worktree
from ai_dev.status import freeze_artifact
from ai_dev.templates import LANE_GRAPH_YML, TASKS_MD
from ai_dev.validate import validate_run

# Reuse the implementer-leg test scaffolding (frozen-feature seeding, the
# implement run's result/metadata shapes) so the verifier tests stand up the
# same precondition the verifier consumes: a frozen feature with a real
# implement run + workspace to verify.
from test_implement_leg import (  # noqa: E402
    _METADATA_JSON as _IMPL_METADATA_JSON,
    _RESULT_JSON as _IMPL_RESULT_JSON,
    _feature_root,
)

_TASK_BODY = "Create workspace/hello.py defining answer() returning 42."
_IMPL_WORKSPACE_FILE = "workspace/hello.py"
_IMPL_WORKSPACE_CONTENT = "# throwaway prototype module\ndef answer():\n    return 42\n"

# Verify commands used across tests. ``_PY`` is swapped for the test interpreter
# so ``python -c`` resolves under ``uv run``. Bare ``import hello`` works because
# the verifier runs each command with cwd = the implement run's ``workspace/``
# (path[0] is '' for ``python -c``, i.e. the cwd), so the implemented module is
# importable.
_PY = sys.executable
# ``import hello`` resolves because the verifier runs each command with cwd =
# the implement run's ``workspace/`` (path[0] is '' for ``python -c``, i.e. the
# cwd), so the implemented module is importable. ``_CMD_NOOP_PASS`` has no
# workspace dependency - a plain exit-0 used as a second passing command.
_CMD_PASS = f'{_PY} -c "import hello; import sys; sys.exit(0 if hello.answer()==42 else 1)"'
_CMD_FAIL = f'{_PY} -c "import hello; import sys; sys.exit(0 if hello.answer()==99 else 1)"'
_CMD_NOOP_PASS = f'{_PY} -c "import sys; sys.exit(0)"'


def _fill_artifacts_with_verify(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    *,
    verification_commands: list[dict[str, Any]],
    task_body: str = _TASK_BODY,
    expected_files: list[str] | None = None,
    exclusive_files: list[str] | None = None,
    tasks: list[str] | None = None,
) -> None:
    """Overwrite 03-tasks.md + 04-lane-graph.yml with filled content, including
    the lane's ``verification_commands`` (the §9.5 source the verifier reads)."""
    if expected_files is None:
        expected_files = [_IMPL_WORKSPACE_FILE]
    if exclusive_files is None:
        exclusive_files = [_IMPL_WORKSPACE_FILE]
    root = _feature_root(repo_root, feature_id)
    (root / TASKS_MD).write_text(
        f"# Tasks - {feature_id}\n\nFrozen: false\n\n"
        f"> Canonical task state lives in `status/task-status.yml`.\n\n"
        f"## Tasks (TASK-NNN)\n\n{task_body}\n"
    )
    lane_graph = {
        "feature": feature_id,
        "frozen": False,
        "lanes": [
            {
                "id": lane_id,
                "purpose": "Implement the run contract",
                "tasks": tasks if tasks is not None else [],
                "depends_on": [],
                "expected_files": expected_files,
                "exclusive_files": exclusive_files,
                "provides": [],
                "consumes": [],
                "verification_scope": [],
                "verification_commands": verification_commands,
                "merge_policy": {
                    "auto_merge": False,
                    "allowed_mechanical_resolutions": [],
                    "semantic_conflict_policy": "human_triage",
                },
            }
        ],
    }
    with (root / LANE_GRAPH_YML).open("w") as f:
        yaml.safe_dump(
            lane_graph, f, sort_keys=False, default_flow_style=False, allow_unicode=True,
        )


def _seed_frozen_feature_with_verify(
    repo_root: Path,
    *,
    verification_commands: list[dict[str, Any]],
    task_body: str = _TASK_BODY,
    expected_files: list[str] | None = None,
    exclusive_files: list[str] | None = None,
    tasks: list[str] | None = None,
    freeze: bool = True,
) -> tuple[str, str]:
    """Create a feature run, fill tasks/lane-graph (with verify commands), freeze.

    Returns ``(feature_id, lane_id)``. ``freeze=False`` leaves the artifacts
    unfrozen to exercise the frozen-precondition guard.
    """
    feature_id = create_feature_run(repo_root, "verifier test")
    lane_id = "LANE-001"
    _fill_artifacts_with_verify(
        repo_root,
        feature_id,
        lane_id,
        verification_commands=verification_commands,
        task_body=task_body,
        expected_files=expected_files,
        exclusive_files=exclusive_files,
        tasks=tasks,
    )
    if freeze:
        root = _feature_root(repo_root, feature_id)
        freeze_artifact(root, "tasks")
        freeze_artifact(root, "lane_graph")
    return feature_id, lane_id


def _stage_implement_run_with_verify(
    repo_root: Path,
    *,
    verification_commands: list[dict[str, Any]],
    task_body: str = _TASK_BODY,
) -> tuple[str, str, str]:
    """Stage a real implement run (RUN-001) with a workspace file for the
    verifier to run commands against.

    Creates a frozen feature (with verify commands declared), prepares an
    Implementer run with the workspace file allowed, writes the implement run's
    outputs (result.json / result.md / metadata.json / workspace file) directly,
    and writes the lane ``implement-result.{md,json}`` rollup via the real
    ``write_implement_result``. Returns ``(feature_id, lane_id, implement_run_id)``.
    """
    from ai_dev.implement_leg import write_implement_result

    feature_id, lane_id = _seed_frozen_feature_with_verify(
        repo_root, verification_commands=verification_commands, task_body=task_body,
    )
    impl_run_id = prepare_run(
        repo_root, feature_id, "Implementer", task_body,
        allowed_files=[_IMPL_WORKSPACE_FILE],
    )
    impl_root = run_dir(repo_root, feature_id, impl_run_id)
    out = impl_root / "output"
    (out / "result.json").write_text(json.dumps(_IMPL_RESULT_JSON))
    (out / "result.md").write_text("Wrote workspace/hello.py for the run.\n")
    (out / "metadata.json").write_text(json.dumps(_IMPL_METADATA_JSON))
    ws = impl_root / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "hello.py").write_text(_IMPL_WORKSPACE_CONTENT)

    validation = validate_run(repo_root, feature_id, impl_run_id)
    assert validation.passed, f"staged implement run should validate: {validation.issues}"
    write_implement_result(
        _feature_root(repo_root, feature_id),
        lane_id,
        run_id=impl_run_id,
        result=_IMPL_RESULT_JSON,
        metadata=_IMPL_METADATA_JSON,
        validation=validation,
    )
    return feature_id, lane_id, impl_run_id


# ===========================================================================
# Seam 1: reading the declared verify command set (§9.5 source, fail-loud).
# ===========================================================================


class TestReadVerificationCommands:
    """``read_verification_commands`` parses the lane-graph's
    ``verification_commands`` into typed ``VerifyCommand`` objects, fail-loud on
    a missing set (the §24.2 "缺失" case) or a malformed spec."""

    def test_reads_commands_from_lane_graph(self, repo_root: Path) -> None:
        cmds = [
            {"name": "pytest", "command": "pytest tests/"},
            {"name": "mypy", "command": "mypy src/"},
        ]
        feature_id, lane_id = _seed_frozen_feature_with_verify(
            repo_root, verification_commands=cmds
        )

        result = read_verification_commands(
            _feature_root(repo_root, feature_id), lane_id
        )

        assert result == [
            VerifyCommand(name="pytest", command="pytest tests/"),
            VerifyCommand(name="mypy", command="mypy src/"),
        ]

    def test_strips_surrounding_whitespace(self, repo_root: Path) -> None:
        feature_id, lane_id = _seed_frozen_feature_with_verify(
            repo_root,
            verification_commands=[{"name": "  pytest  ", "command": "  pytest  "}],
        )

        result = read_verification_commands(
            _feature_root(repo_root, feature_id), lane_id
        )

        assert result == [VerifyCommand(name="pytest", command="pytest")]

    def test_fails_loud_when_no_commands_declared(self, repo_root: Path) -> None:
        # §24.2 "缺失": a lane with no verification_commands cannot be verified.
        feature_id, lane_id = _seed_frozen_feature_with_verify(
            repo_root, verification_commands=[]
        )

        with pytest.raises(ValueError, match="no verification_commands"):
            read_verification_commands(
                _feature_root(repo_root, feature_id), lane_id
            )

    def test_fails_loud_on_missing_name(self, repo_root: Path) -> None:
        feature_id, lane_id = _seed_frozen_feature_with_verify(
            repo_root, verification_commands=[{"command": "pytest"}]
        )

        with pytest.raises(ValueError, match="name"):
            read_verification_commands(
                _feature_root(repo_root, feature_id), lane_id
            )

    def test_fails_loud_on_missing_command(self, repo_root: Path) -> None:
        feature_id, lane_id = _seed_frozen_feature_with_verify(
            repo_root, verification_commands=[{"name": "pytest"}]
        )

        with pytest.raises(ValueError, match="command"):
            read_verification_commands(
                _feature_root(repo_root, feature_id), lane_id
            )

    def test_fails_loud_on_empty_strings(self, repo_root: Path) -> None:
        feature_id, lane_id = _seed_frozen_feature_with_verify(
            repo_root, verification_commands=[{"name": "pytest", "command": "  "}]
        )

        with pytest.raises(ValueError, match="command"):
            read_verification_commands(
                _feature_root(repo_root, feature_id), lane_id
            )

    def test_fails_loud_when_lane_missing(self, repo_root: Path) -> None:
        feature_id, _ = _seed_frozen_feature_with_verify(
            repo_root, verification_commands=[{"name": "pytest", "command": "pytest"}]
        )

        with pytest.raises(ValueError, match="LANE-999"):
            read_verification_commands(
                _feature_root(repo_root, feature_id), "LANE-999"
            )


# ===========================================================================
# Seam 2: reading the implement run id backing the lane.
# ===========================================================================


class TestReadImplementRunId:
    """``read_implement_run_id`` resolves the implement run whose workspace is
    verified, fail-loud when the lane has no implement-result."""

    def test_reads_run_id_from_implement_result(self, repo_root: Path) -> None:
        feature_id, lane_id, impl_run_id = _stage_implement_run_with_verify(
            repo_root,
            verification_commands=[{"name": "pytest", "command": _CMD_PASS}],
        )

        result = read_implement_run_id(_feature_root(repo_root, feature_id), lane_id)

        assert result == impl_run_id

    def test_fails_loud_when_no_implement_result(self, repo_root: Path) -> None:
        # A frozen feature with verify commands but NO implement run has nothing
        # to verify.
        feature_id, lane_id = _seed_frozen_feature_with_verify(
            repo_root,
            verification_commands=[{"name": "pytest", "command": _CMD_PASS}],
        )

        with pytest.raises(ValueError, match="implement-result"):
            read_implement_run_id(_feature_root(repo_root, feature_id), lane_id)


# ===========================================================================
# Seam 3: per-command execution (deterministic shell, no model).
# ===========================================================================


class TestRunVerifyCommand:
    """``run_verify_command`` executes one command in ``cwd`` and captures its
    outcome - pass / fail / not-found / timeout - never raising (a verification
    failure is a captured result, §24.1/§24.2)."""

    def test_passing_command(self, tmp_path: Path) -> None:
        # A workspace with hello.py so the import resolves.
        (tmp_path / "hello.py").write_text(_IMPL_WORKSPACE_CONTENT)

        result = run_verify_command(
            VerifyCommand(name="pytest", command=_CMD_PASS), tmp_path
        )

        assert result.exit_code == 0
        assert result.passed is True
        assert result.name == "pytest"
        assert result.command == _CMD_PASS

    def test_failing_command_captures_nonzero_exit(self, tmp_path: Path) -> None:
        (tmp_path / "hello.py").write_text(_IMPL_WORKSPACE_CONTENT)

        result = run_verify_command(
            VerifyCommand(name="pytest", command=_CMD_FAIL), tmp_path
        )

        assert result.exit_code == 1
        assert result.passed is False

    def test_command_not_found_captured_not_raised(self, tmp_path: Path) -> None:
        # shell=True: a missing binary is reported by the shell as exit 127, not
        # raised - so the verifier records it as a (failed) command result.
        result = run_verify_command(
            VerifyCommand(name="missing", command="no_such_binary_xyz_123_456"),
            tmp_path,
        )

        assert result.exit_code == 127
        assert result.passed is False
        assert "not found" in result.stderr.lower() or result.exit_code != 0

    def test_stdout_stderr_captured(self, tmp_path: Path) -> None:
        cmd = (
            f"{_PY} -c \"import sys; print('out-line', file=sys.stdout); "
            f"print('err-line', file=sys.stderr); sys.exit(0)\""
        )

        result = run_verify_command(
            VerifyCommand(name="echo", command=cmd), tmp_path
        )

        assert result.passed is True
        assert "out-line" in result.stdout
        assert "err-line" in result.stderr

    def test_timeout_captured_as_failure(self, tmp_path: Path) -> None:
        # §24.1 timeout: a hung command is bounded and recorded as a failure,
        # not raised. Sleep far longer than the tiny timeout.
        cmd = f"{_PY} -c \"import time; time.sleep(30)\""

        result = run_verify_command(
            VerifyCommand(name="slow", command=cmd), tmp_path, timeout=0.5
        )

        assert result.passed is False
        assert result.exit_code == -1  # _TIMEOUT_EXIT sentinel
        assert "timed out" in result.stderr.lower()


# ===========================================================================
# Seam 4: the verification-report rollup (§4.4 md+json double product, no issues[]).
# ===========================================================================


_PASSING_RESULTS = [
    CommandResult(name="pytest", command="pytest tests/", exit_code=0, stdout="3 passed", stderr=""),
    CommandResult(name="mypy", command="mypy src/", exit_code=0, stdout="", stderr=""),
]
_MIXED_RESULTS = [
    CommandResult(name="pytest", command="pytest tests/", exit_code=0, stdout="ok", stderr=""),
    CommandResult(name="mypy", command="mypy src/", exit_code=2, stdout="", stderr="type error"),
]


class TestWriteVerificationReport:
    """``write_verification_report`` rolls the per-command results up into the
    lane-level ``verification-report.{md,json}`` - field-complete, with an
    overall verdict, and deliberately NO ``issues[]`` (§9.5 vs §15)."""

    def test_writes_md_and_json_under_verification_dir(
        self, repo_root: Path
    ) -> None:
        feature_id, lane_id, _ = _stage_implement_run_with_verify(
            repo_root,
            verification_commands=[{"name": "pytest", "command": _CMD_PASS}],
        )
        root = _feature_root(repo_root, feature_id)

        md_path, json_path = write_verification_report(
            root,
            lane_id,
            implement_run_id="RUN-001",
            results=_PASSING_RESULTS,
            started_at="2026-07-20T12:00:00Z",
            ended_at="2026-07-20T12:00:01Z",
        )

        assert md_path == root / "lanes" / lane_id / VERIFICATION_DIR / VERIFICATION_REPORT_MD
        assert json_path == root / "lanes" / lane_id / VERIFICATION_DIR / VERIFICATION_REPORT_JSON
        assert md_path.is_file() and json_path.is_file()

    def test_json_rollup_is_field_complete(self, repo_root: Path) -> None:
        feature_id, lane_id, _ = _stage_implement_run_with_verify(
            repo_root,
            verification_commands=[{"name": "pytest", "command": _CMD_PASS}],
        )
        root = _feature_root(repo_root, feature_id)

        _, json_path = write_verification_report(
            root,
            lane_id,
            implement_run_id="RUN-001",
            results=_PASSING_RESULTS,
            started_at="2026-07-20T12:00:00Z",
            ended_at="2026-07-20T12:00:01Z",
        )
        rollup = json.loads(json_path.read_text())

        # Provenance.
        assert rollup["feature"] == feature_id
        assert rollup["lane"] == lane_id
        assert rollup["implement_run"] == "RUN-001"
        assert rollup["role"] == "Verifier"
        assert rollup["kind"] == "shell"  # §9.5 shell adapter
        # Overall verdict + precomputed counts.
        assert rollup["verdict"] == "pass"
        assert rollup["command_count"] == 2
        assert rollup["passed_count"] == 2
        # Each command's facts carried verbatim.
        assert len(rollup["commands"]) == 2
        assert rollup["commands"][0]["name"] == "pytest"
        assert rollup["commands"][0]["exit_code"] == 0
        assert rollup["commands"][0]["passed"] is True
        assert rollup["commands"][0]["stdout"] == "3 passed"
        assert rollup["commands"][0]["stderr"] == ""
        # Timestamps.
        assert rollup["started_at"] == "2026-07-20T12:00:00Z"
        assert rollup["ended_at"] == "2026-07-20T12:00:01Z"

    def test_rollup_has_no_issues_field(self, repo_root: Path) -> None:
        # §9.5 vs §15: the verifier emits a report, NOT issues[]. The checking
        # roles that emit issues[] are reviewer + gap; verification pass/fail is
        # a separate §18.4 gate condition.
        feature_id, lane_id, _ = _stage_implement_run_with_verify(
            repo_root,
            verification_commands=[{"name": "pytest", "command": _CMD_PASS}],
        )
        root = _feature_root(repo_root, feature_id)

        _, json_path = write_verification_report(
            root,
            lane_id,
            implement_run_id="RUN-001",
            results=_PASSING_RESULTS,
            started_at="2026-07-20T12:00:00Z",
            ended_at="2026-07-20T12:00:01Z",
        )
        rollup = json.loads(json_path.read_text())

        assert "issues" not in rollup

    def test_verdict_pass_iff_all_commands_pass(self, repo_root: Path) -> None:
        feature_id, lane_id, _ = _stage_implement_run_with_verify(
            repo_root,
            verification_commands=[{"name": "pytest", "command": _CMD_PASS}],
        )
        root = _feature_root(repo_root, feature_id)

        _, passing_json = write_verification_report(
            root, lane_id, implement_run_id="RUN-001", results=_PASSING_RESULTS,
            started_at="2026-07-20T12:00:00Z", ended_at="2026-07-20T12:00:01Z",
        )
        assert json.loads(passing_json.read_text())["verdict"] == "pass"

        _, mixed_json = write_verification_report(
            root, lane_id, implement_run_id="RUN-001", results=_MIXED_RESULTS,
            started_at="2026-07-20T12:00:00Z", ended_at="2026-07-20T12:00:01Z",
        )
        mixed = json.loads(mixed_json.read_text())
        assert mixed["verdict"] == "fail"
        assert mixed["passed_count"] == 1
        assert mixed["command_count"] == 2
        # The failing command's stderr is carried through.
        failing = [c for c in mixed["commands"] if not c["passed"]][0]
        assert failing["exit_code"] == 2
        assert failing["stderr"] == "type error"

    def test_md_mirror_carries_key_fields(self, repo_root: Path) -> None:
        feature_id, lane_id, _ = _stage_implement_run_with_verify(
            repo_root,
            verification_commands=[{"name": "pytest", "command": _CMD_PASS}],
        )
        root = _feature_root(repo_root, feature_id)

        md_path, _ = write_verification_report(
            root, lane_id, implement_run_id="RUN-001", results=_MIXED_RESULTS,
            started_at="2026-07-20T12:00:00Z", ended_at="2026-07-20T12:00:01Z",
        )
        md = md_path.read_text()

        assert f"lane: {lane_id}" in md
        assert "implement_run: RUN-001" in md
        assert "role: Verifier" in md
        assert "kind: shell" in md
        assert "verdict: **fail**" in md
        assert "pytest: PASS" in md
        assert "mypy: FAIL" in md
        # The §9.5 vs §15 reminder that this is a report, not issues[].
        assert "issues[]" in md


# ===========================================================================
# Seam 5: orchestration (read commands -> find workspace -> run each -> rollup).
# ===========================================================================


class TestRunVerifier:
    """``run_verifier`` wires the seams: a passing command set yields
    ``verdict: pass`` + the lane report; a mixed set yields ``verdict: fail``
    with every command's outcome captured. The commands run in the implement
    run's workspace (proven by an import that only resolves there)."""

    def test_passing_commands_verdict_pass_and_rolls_up(
        self, repo_root: Path
    ) -> None:
        feature_id, lane_id, impl_run_id = _stage_implement_run_with_verify(
            repo_root,
            verification_commands=[
                {"name": "pytest", "command": _CMD_PASS},
                {"name": "noop", "command": _CMD_NOOP_PASS},
            ],
        )

        result = run_verifier(
            repo_root, feature_id, lane_id,
            started_at="2026-07-20T12:00:00Z", ended_at="2026-07-20T12:00:01Z",
        )

        assert result.lane_id == lane_id
        assert result.feature_id == feature_id
        assert result.implement_run_id == impl_run_id
        assert result.verdict == "pass"
        assert len(result.command_results) == 2
        assert all(r.passed for r in result.command_results)
        # The lane-level report landed.
        lane_root = lane_dir(repo_root, feature_id, lane_id)
        assert (lane_root / VERIFICATION_DIR / VERIFICATION_REPORT_MD).is_file()
        rollup = json.loads(
            (lane_root / VERIFICATION_DIR / VERIFICATION_REPORT_JSON).read_text()
        )
        assert rollup["verdict"] == "pass"
        assert rollup["implement_run"] == impl_run_id

    def test_commands_run_in_implement_workspace(self, repo_root: Path) -> None:
        # The verify command imports `hello` - which only resolves because the
        # verifier runs it with cwd = the implement run's workspace/. A wrong
        # cwd would make `import hello` fail (ModuleNotFoundError -> non-zero).
        feature_id, lane_id, _ = _stage_implement_run_with_verify(
            repo_root,
            verification_commands=[{"name": "import-hello", "command": _CMD_PASS}],
        )

        result = run_verifier(
            repo_root, feature_id, lane_id,
            started_at="2026-07-20T12:00:00Z", ended_at="2026-07-20T12:00:01Z",
        )

        assert result.verdict == "pass"
        assert result.command_results[0].passed is True

    def test_mixed_pass_fail_verdict_fail_captures_each(
        self, repo_root: Path
    ) -> None:
        # The multi-command pass/fail mix: pytest passes, mypy fails -> overall
        # verdict fail, but BOTH outcomes are captured (the verifier does not
        # stop at the first failure).
        feature_id, lane_id, _ = _stage_implement_run_with_verify(
            repo_root,
            verification_commands=[
                {"name": "pytest", "command": _CMD_PASS},
                {"name": "mypy", "command": _CMD_FAIL},
            ],
        )

        result = run_verifier(
            repo_root, feature_id, lane_id,
            started_at="2026-07-20T12:00:00Z", ended_at="2026-07-20T12:00:01Z",
        )

        assert result.verdict == "fail"
        assert len(result.command_results) == 2
        names = {r.name: r.passed for r in result.command_results}
        assert names == {"pytest": True, "mypy": False}
        rollup = json.loads(
            (lane_dir(repo_root, feature_id, lane_id) / VERIFICATION_DIR / VERIFICATION_REPORT_JSON).read_text()
        )
        assert rollup["verdict"] == "fail"
        assert rollup["passed_count"] == 1
        assert rollup["command_count"] == 2

    def test_audit_log_records_verify_event(self, repo_root: Path) -> None:
        feature_id, lane_id, impl_run_id = _stage_implement_run_with_verify(
            repo_root,
            verification_commands=[{"name": "pytest", "command": _CMD_PASS}],
        )

        run_verifier(
            repo_root, feature_id, lane_id,
            started_at="2026-07-20T12:00:00Z", ended_at="2026-07-20T12:00:01Z",
        )

        root = _feature_root(repo_root, feature_id)
        records = json.loads((root / AUDIT_LOG_JSON).read_text())
        verify_events = [r for r in records if r["event"] == "verify"]
        assert len(verify_events) == 1
        payload = verify_events[0]["payload"]
        assert payload["lane"] == lane_id
        assert payload["feature"] == feature_id
        assert payload["implement_run"] == impl_run_id
        assert payload["verdict"] == "pass"
        assert payload["command_count"] == 1
        assert payload["passed_count"] == 1

    def test_fails_loud_when_unfrozen(self, repo_root: Path) -> None:
        # §4.2: the verifier reads verify commands from the frozen lane-graph;
        # an unfrozen precondition is rejected before any command runs.
        feature_id, lane_id = _seed_frozen_feature_with_verify(
            repo_root,
            verification_commands=[{"name": "pytest", "command": _CMD_PASS}],
            freeze=False,
        )

        with pytest.raises(ValueError, match="frozen"):
            run_verifier(repo_root, feature_id, lane_id)

    def test_fails_loud_when_no_verify_commands(self, repo_root: Path) -> None:
        # A lane declaring NO verify commands is the §24.2 "缺失" case.
        feature_id, lane_id = _seed_frozen_feature_with_verify(
            repo_root, verification_commands=[]
        )

        with pytest.raises(ValueError, match="no verification_commands"):
            run_verifier(repo_root, feature_id, lane_id)

    def test_fails_loud_when_no_implement_result(self, repo_root: Path) -> None:
        feature_id, lane_id = _seed_frozen_feature_with_verify(
            repo_root,
            verification_commands=[{"name": "pytest", "command": _CMD_PASS}],
        )

        with pytest.raises(ValueError, match="implement-result"):
            run_verifier(repo_root, feature_id, lane_id)

    def test_fails_loud_when_feature_missing(self, repo_root: Path) -> None:
        with pytest.raises(ValueError, match="not found"):
            run_verifier(repo_root, "FEATURE-999", "LANE-001")

    def test_no_token_persisted_anywhere(self, repo_root: Path) -> None:
        # The verifier is deterministic shell - no profile, no token. Nothing
        # token-like should land in the lane report or the implement run dir.
        feature_id, lane_id, _ = _stage_implement_run_with_verify(
            repo_root,
            verification_commands=[{"name": "pytest", "command": _CMD_PASS}],
        )

        run_verifier(
            repo_root, feature_id, lane_id,
            started_at="2026-07-20T12:00:00Z", ended_at="2026-07-20T12:00:01Z",
        )

        root = _feature_root(repo_root, feature_id)
        report = (lane_dir(repo_root, feature_id, lane_id) / VERIFICATION_DIR / VERIFICATION_REPORT_JSON).read_text()
        assert "token" not in report.lower()
        # No profile field either (deterministic shell, no agent).
        rollup = json.loads(report)
        assert "profile" not in rollup
        assert "run_metadata" not in rollup


# ===========================================================================
# CLI: `ai-dev verify <FEATURE> <LANE>`.
# ===========================================================================


class TestVerifyCli:
    """The ``verify`` console command drives the leg end to end - argparse +
    dispatch to ``run_verifier`` - with no profile (deterministic shell, no
    model). Prints VERIFY PASS / VERIFY FAIL and exits 0 / 1 accordingly."""

    def test_verify_pass_exit_zero(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        feature_id, lane_id, impl_run_id = _stage_implement_run_with_verify(
            repo_root,
            verification_commands=[{"name": "pytest", "command": _CMD_PASS}],
        )

        rc = main(["verify", feature_id, lane_id, "--repo-root", str(repo_root)])

        assert rc == 0
        out = capsys.readouterr().out
        assert "VERIFY PASS" in out
        assert f"lane={lane_id}" in out
        assert f"implement_run={impl_run_id}" in out
        # The lane report landed through the CLI path.
        assert (
            lane_dir(repo_root, feature_id, lane_id) / VERIFICATION_DIR / VERIFICATION_REPORT_JSON
        ).is_file()

    def test_verify_fail_exit_one(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        feature_id, lane_id, _ = _stage_implement_run_with_verify(
            repo_root,
            verification_commands=[
                {"name": "pytest", "command": _CMD_PASS},
                {"name": "mypy", "command": _CMD_FAIL},
            ],
        )

        rc = main(["verify", feature_id, lane_id, "--repo-root", str(repo_root)])

        assert rc == 1
        out = capsys.readouterr().out
        assert "VERIFY FAIL" in out
        # The failing command is named in the per-command summary.
        assert "mypy" in out

    def test_verify_unfrozen_exits_one_with_error(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        feature_id, lane_id = _seed_frozen_feature_with_verify(
            repo_root,
            verification_commands=[{"name": "pytest", "command": _CMD_PASS}],
            freeze=False,
        )

        rc = main(["verify", feature_id, lane_id, "--repo-root", str(repo_root)])

        assert rc == 1
        err = capsys.readouterr().err
        assert "frozen" in err

    def test_verify_no_commands_exits_one_with_error(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        feature_id, lane_id = _seed_frozen_feature_with_verify(
            repo_root, verification_commands=[]
        )

        rc = main(["verify", feature_id, lane_id, "--repo-root", str(repo_root)])

        assert rc == 1
        err = capsys.readouterr().err
        assert "verification_commands" in err

    def test_verify_needs_no_profile_flag(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The deterministic shell verifier takes no --profile (no model). Passing
        # one is rejected by argparse (unknown arg) -> exit non-zero.
        feature_id, lane_id, _ = _stage_implement_run_with_verify(
            repo_root,
            verification_commands=[{"name": "pytest", "command": _CMD_PASS}],
        )

        with pytest.raises(SystemExit):
            main([
                "verify", feature_id, lane_id,
                "--profile", "cc-glm52", "--repo-root", str(repo_root),
            ])


# ===========================================================================
# Seam 6: v0.7 lane-worktree verify cwd (ADR-0009 D2 capstone).
# ===========================================================================


class TestVerifyLaneWorktreeCwd:
    """v0.7 capstone: when a lane has an active worktree, the verifier runs
    each command with ``cwd=<worktree>/workspace/`` - the same place the
    implementer leg wrote the package + ``tests/`` (the Planner emits
    workspace-relative verify commands: ``PYTHONPATH=. python -m pytest tests``,
    ``python -m mypy <pkg>``). A wrong cwd (the bare worktree root) would leave
    the implemented files invisible to the commands and turn every verify into a
    spurious failure - so this is the load-bearing cwd resolution for the real
    two-lane dogfood."""

    def test_worktree_cwd_is_workspace_subdir(self, git_repo: Path) -> None:
        # The verify command imports `hello` - which only resolves because the
        # verifier runs it with cwd = <worktree>/workspace/. The implement
        # run's run-home workspace/ also has hello.py, but the worktree is the
        # active cwd; with the fix the import resolves against the worktree's
        # own workspace/, proving cwd = <worktree>/workspace/ (not the bare
        # worktree root, where `import hello` would ModuleNotFoundError).
        feature_id, lane_id, _ = _stage_implement_run_with_verify(
            git_repo,
            verification_commands=[{"name": "import-hello", "command": _CMD_PASS}],
        )
        worktree = create_lane_worktree(
            git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="t"
        )
        ws = worktree / "workspace"
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "hello.py").write_text(_IMPL_WORKSPACE_CONTENT)

        result = run_verifier(
            git_repo, feature_id, lane_id,
            started_at="2026-07-20T12:00:00Z", ended_at="2026-07-20T12:00:01Z",
        )

        assert result.verdict == "pass"
        assert result.command_results[0].passed is True

    def test_lane_worktree_root_returns_workspace_when_present(
        self, git_repo: Path
    ) -> None:
        # An active worktree that the implementer populated with a workspace/
        # subdir resolves to <worktree>/workspace/ (the cwd the Planner's
        # workspace-relative verify commands assume).
        feature_id, lane_id, _ = _stage_implement_run_with_verify(
            git_repo,
            verification_commands=[{"name": "import-hello", "command": _CMD_PASS}],
        )
        worktree = create_lane_worktree(
            git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="t"
        )
        (worktree / "workspace").mkdir(parents=True, exist_ok=True)

        resolved = _lane_worktree_root(git_repo, feature_id, lane_id)
        assert resolved == worktree / "workspace"

    def test_lane_worktree_root_falls_back_to_worktree_when_no_workspace(
        self, git_repo: Path
    ) -> None:
        # An active worktree with no workspace/ subdir yet resolves to the bare
        # worktree root (the else-branch; e.g. a worktree created but not yet
        # populated by the implementer).
        feature_id, lane_id, _ = _stage_implement_run_with_verify(
            git_repo,
            verification_commands=[{"name": "import-hello", "command": _CMD_PASS}],
        )
        worktree = create_lane_worktree(
            git_repo, feature_id, lane_id, base_ref="HEAD", timestamp="t"
        )

        resolved = _lane_worktree_root(git_repo, feature_id, lane_id)
        assert resolved == worktree

    def test_lane_worktree_root_none_without_active_worktree(
        self, repo_root: Path
    ) -> None:
        # No worktree.json -> None, so run_verifier falls back to the implement
        # run's workspace/ (the v0.1-v0.6 cwd). This is the non-worktree path
        # the v0.2 tests exercise.
        feature_id, lane_id, _ = _stage_implement_run_with_verify(
            repo_root,
            verification_commands=[{"name": "import-hello", "command": _CMD_PASS}],
        )

        assert _lane_worktree_root(repo_root, feature_id, lane_id) is None
