"""End-to-end integration - ticket 05, the v0.1 walking-skeleton proof.

Strings the v0.0 and v0.1 deterministic runtime together on one feature run:
``create_feature_run`` (v0.0) -> ``prepare_run`` (ticket 02) -> ``run_headless``
(ticket 03) -> ``validate_run`` (ticket 04), with no manual intervention between
steps. The artifact chain is asserted end to end - exit code, result.json schema
validity, changed_files within the allowed boundary, validate PASS, RUN-NNN under
the feature run's ``runs/``, a complete metadata.json, and the token never
persisted - exactly the ticket-05 checklist.

Two drive modes, covering two seams:

* **Library** (``TestEndToEndLibrary``) - calls the public functions directly,
  so the handoff between modules is the thing under test (the path / ID /
  interface alignment the ticket names). A fake ``claude`` binary stands in for
  the real CLI so the run is deterministic and token-free.
* **CLI** (``TestEndToEndCli``) - drives the ``ai-dev`` console entry for all
  four subcommands with the fake ``claude`` on ``PATH``, so the argparse +
  dispatch wiring is exercised across the whole pipeline (not just one command).

``TestAllowedFilesSeam`` pins the integration seam ticket 05 fixes: a run that
writes a workspace file must declare it via ``allowed_files`` (or
``--allowed-file``) or the §14.2 boundary check fails.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Callable

import pytest

from ai_dev.audit import AUDIT_LOG_JSON
from ai_dev.cli import main
from ai_dev.feature_run import create_feature_run
from ai_dev.paths import run_dir
from ai_dev.profiles import load_profile
from ai_dev.run_prepare import ALLOWED_FILES_FILE, prepare_run
from ai_dev.run_wrapper import run_headless
from ai_dev.validate import validate_run

# A fake ``claude`` binary: ignores argv, writes the §13.1 agent outputs
# (result.json / result.md / a workspace file) into its cwd, prints one
# stream-json ``result`` line to stdout, and exits 0. Stands in for the real CLI
# so the orchestrator is exercised end-to-end without network or token.
# ``__PY__`` is replaced with the test interpreter so the shebang resolves under
# ``uv run`` (string replace, not ``.format``, so the JSON braces are literal).
_FAKE_CLAUDE = """\
#!__PY__
import json, os, sys
os.makedirs("workspace", exist_ok=True)
os.makedirs("output", exist_ok=True)
with open("workspace/hello.py", "w") as f:
    f.write("# throwaway prototype module\\n")
    f.write("def answer():\\n    return 42\\n")
with open("output/result.md", "w") as f:
    f.write("Wrote workspace/hello.py for the run.\\n")
with open("output/result.json", "w") as f:
    json.dump(
        {
            "status": "proposed_done",
            "summary": "Wrote workspace/hello.py for the run.",
            "tasks": [
                {"id": "TASK-001", "status": "proposed_done",
                 "evidence": ["workspace/hello.py"]}
            ],
        },
        f,
    )
sys.stdout.write('{"type":"result","subtype":"success","is_error":false}\\n')
sys.exit(0)
"""

# The three files a real Implementer run (and the fake claude) writes: the two
# §13.1 mandatory outputs plus the declared workspace file.
_EXPECTED_CHANGED_FILES = [
    "output/result.json",
    "output/result.md",
    "workspace/hello.py",
]

_TASK = "Create workspace/hello.py defining answer() returning 42."


def _write_fake_claude(bin_dir: Path) -> Path:
    """Write the fake ``claude`` script into ``bin_dir`` and return its path.

    Named ``claude`` so ``shutil.which("claude")`` resolves it when ``bin_dir``
    is on ``PATH`` (the CLI e2e path); the library e2e passes the path directly
    via ``claude_path``.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "claude"
    script.write_text(_FAKE_CLAUDE.replace("__PY__", sys.executable))
    os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _audit_records(repo_root: Path, feature_id: str) -> list[dict[str, object]]:
    log = repo_root / ".ai-dev" / "features" / feature_id / AUDIT_LOG_JSON
    return json.loads(log.read_text())


def _read_allowed(repo_root: Path, feature_id: str, run_id: str) -> set[str]:
    allowed_path = (
        run_dir(repo_root, feature_id, run_id) / "input" / ALLOWED_FILES_FILE
    )
    entries: set[str] = set()
    for line in allowed_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            entries.add(line)
    return entries


def _assert_no_token_leak(run_root: Path, sentinel: str) -> None:
    """§10.2 / invariant #11: the token value must not appear in any file the
    wrapper wrote inside the run directory. A distinctive sentinel makes a leak
    visible across every wrapper-written artifact.
    """
    for path in run_root.rglob("*"):
        if path.is_file():
            assert sentinel not in path.read_text(errors="ignore"), (
                f"token leaked into {path}"
            )


class TestEndToEndLibrary:
    """The full pipeline through the public functions, driven by a fake claude.

    This is the walking-skeleton proof: every ticket-05 checkbox asserted against
    one integrated run, with the path / ID / interface alignment exercised for
    real (create returns FEATURE-NNN -> prepare takes it and returns RUN-NNN ->
    run takes both + profile -> validate takes both).
    """

    def test_full_pipeline_passes_validation(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        # A distinctive token sentinel: §10.2/invariant #11 says it must never
        # land on disk inside the run directory.
        sentinel = "tok-E2E-LEAK-CHECK-7d2a9e"
        monkeypatch.setenv("CC_GLM52_TOKEN", sentinel)
        fake = _write_fake_claude(tmp_path / "bin")

        # STEP 1 - create (v0.0): intent -> FEATURE-001.
        feature_id = create_feature_run(repo_root, "de-risk the v0.1 run adapter")
        assert feature_id == "FEATURE-001"

        # STEP 2 - prepare (ticket 02): feature + role + task + allowed_files ->
        # RUN-001. The workspace file is declared here (ticket 05 seam).
        run_id = prepare_run(
            repo_root,
            feature_id,
            "Implementer",
            _TASK,
            allowed_files=["workspace/hello.py"],
        )
        assert run_id == "RUN-001"

        # RUN-NNN lives under THIS feature run's runs/ (v0.0 skeleton <-> v0.1
        # run path integration).
        assert (
            repo_root / ".ai-dev" / "features" / feature_id / "runs" / run_id
        ).is_dir()

        # STEP 3 - run (ticket 03): headless capture via the profile.
        result = run_headless(
            repo_root,
            feature_id,
            run_id,
            profile,
            claude_path=str(fake),
            started_at="2026-07-20T10:00:00Z",
            ended_at="2026-07-20T10:00:05Z",
        )

        # exit_code 0 (the captured run succeeded).
        assert result.exit_code == 0
        # changed_files == the actual workspace changes (§13.2).
        assert result.changed_files == _EXPECTED_CHANGED_FILES

        # STEP 4 - validate (ticket 04): the §14 three checks -> PASS.
        verdict = validate_run(repo_root, feature_id, run_id)
        assert verdict.passed, f"validation failed: {verdict.issues}"
        assert verdict.failed_check is None

        # changed_files all within allowed-files.txt (§14.2).
        allowed = _read_allowed(repo_root, feature_id, run_id)
        assert set(result.changed_files).issubset(allowed)

        # result.json is schema-valid (§14.1) - re-check independently of the
        # verdict to pin the schema check specifically.
        run_root = run_dir(repo_root, feature_id, run_id)
        result_json = json.loads((run_root / "output" / "result.json").read_text())
        assert result_json["status"] == "proposed_done"
        assert result_json["tasks"][0]["evidence"] == ["workspace/hello.py"]

        # metadata.json field-complete + changed_files consistent with workspace.
        md = json.loads((run_root / "output" / "metadata.json").read_text())
        assert md["run_id"] == run_id
        assert md["profile"] == "cc-glm52"
        assert md["cli"] == "claude"
        assert md["backend"] == "glm"
        assert md["model"] == "glm-5.2"
        assert md["exit_code"] == 0
        assert md["changed_files"] == _EXPECTED_CHANGED_FILES
        assert md["commits"] == []
        assert md["checks"] == []

        # token never persisted anywhere in the run directory (§10.2/#11).
        _assert_no_token_leak(run_root, sentinel)

    def test_audit_log_records_full_lifecycle(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # §2.1: every lifecycle op flows through the audit log, in order.
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok")
        fake = _write_fake_claude(tmp_path / "bin")

        feature_id = create_feature_run(repo_root, "e2e audit trail")
        prepare_run(
            repo_root, feature_id, "Implementer", _TASK,
            allowed_files=["workspace/hello.py"],
        )
        run_headless(
            repo_root, feature_id, "RUN-001", profile, claude_path=str(fake),
            started_at="2026-07-20T10:00:00Z", ended_at="2026-07-20T10:00:05Z",
        )
        validate_run(repo_root, feature_id, "RUN-001")

        events = [str(r["event"]) for r in _audit_records(repo_root, feature_id)]
        # The four user-visible lifecycle ops, in pipeline order (allocate_id
        # mints the RUN id inside prepare, so it precedes prepare_run).
        assert "create" in events
        assert "prepare_run" in events
        assert "run" in events
        assert "validate" in events
        assert events.index("create") < events.index("prepare_run")
        assert events.index("prepare_run") < events.index("run")
        assert events.index("run") < events.index("validate")


class TestEndToEndCli:
    """The full pipeline through the ``ai-dev`` console entry, fake claude on PATH.

    Exercises argparse + dispatch across all four subcommands in one run, so the
    CLI wiring (not just the library seam) is proven end to end. The fake
    ``claude`` is placed on ``PATH`` so ``run-headless`` resolves it via
    ``shutil.which`` like the real binary.
    """

    def test_four_commands_in_sequence_exit_zero_and_validate_passes(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        write_profiles(repo_root)
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-cli-e2e")
        fake_bin = _write_fake_claude(tmp_path / "bin")
        # Prepend the fake bin dir to PATH so shutil.which("claude") finds it.
        monkeypatch.setenv("PATH", f"{fake_bin.parent}{os.pathsep}{os.environ['PATH']}")

        # 1. create-feature-run -> prints FEATURE-001.
        assert main(
            ["create-feature-run", "e2e via CLI", "--repo-root", str(repo_root)]
        ) == 0
        assert "FEATURE-001" in capsys.readouterr().out

        # 2. prepare-run with --allowed-file -> prints RUN-001.
        assert main(
            [
                "prepare-run", "FEATURE-001",
                "--role", "Implementer",
                "--task", _TASK,
                "--allowed-file", "workspace/hello.py",
                "--repo-root", str(repo_root),
            ]
        ) == 0
        assert "RUN-001" in capsys.readouterr().out

        # 3. run-headless -> captures, prints a summary, exits 0 (capture, not
        # verdict - even a non-zero claude exit returns 0 from run-headless).
        assert main(
            [
                "run-headless", "FEATURE-001", "RUN-001",
                "--profile", "cc-glm52",
                "--repo-root", str(repo_root),
            ]
        ) == 0
        summary = capsys.readouterr().out
        assert "RUN-001" in summary
        assert "exit_code=0" in summary

        # 4. validate-run -> VALIDATE PASS, exit 0.
        assert main(
            [
                "validate-run", "FEATURE-001", "RUN-001",
                "--repo-root", str(repo_root),
            ]
        ) == 0
        out = capsys.readouterr().out
        assert "VALIDATE PASS" in out

        # The artifact chain landed on disk through the CLI path too.
        run_root = run_dir(repo_root, "FEATURE-001", "RUN-001")
        md = json.loads((run_root / "output" / "metadata.json").read_text())
        assert md["changed_files"] == _EXPECTED_CHANGED_FILES

    def test_token_not_on_disk_through_cli_path(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # §10.2/invariant #11 through the CLI: the resolved token value never
        # lands on disk inside the run directory.
        write_profiles(repo_root)
        sentinel = "tok-CLI-LEAK-CHECK-3b1f8c"
        monkeypatch.setenv("CC_GLM52_TOKEN", sentinel)
        fake_bin = _write_fake_claude(tmp_path / "bin")
        monkeypatch.setenv("PATH", f"{fake_bin.parent}{os.pathsep}{os.environ['PATH']}")

        main(["create-feature-run", "token safety", "--repo-root", str(repo_root)])
        main(
            ["prepare-run", "FEATURE-001", "--role", "Implementer",
             "--task", _TASK, "--allowed-file", "workspace/hello.py",
             "--repo-root", str(repo_root)]
        )
        main(
            ["run-headless", "FEATURE-001", "RUN-001", "--profile", "cc-glm52",
             "--repo-root", str(repo_root)]
        )
        capsys.readouterr()

        run_root = run_dir(repo_root, "FEATURE-001", "RUN-001")
        _assert_no_token_leak(run_root, sentinel)


class TestAllowedFilesSeamE2E:
    """The integration seam ticket 05 fixes: declaring task-specific workspace files.

    A run that writes a workspace file must declare it via ``allowed_files`` (or
    ``--allowed-file``) - otherwise the §14.2 boundary check fails even though
    schema and exit code are clean. Before the seam there was no API to declare
    them, so every workspace-writing run failed validation.
    """

    def test_undeclared_workspace_file_fails_boundary(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # The same run as the passing e2e, but workspace/hello.py is NOT declared
        # -> the §14.2 boundary check rejects it.
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok")
        fake = _write_fake_claude(tmp_path / "bin")

        feature_id = create_feature_run(repo_root, "seam: undeclared workspace")
        # No allowed_files -> allowed-files.txt has only result.json + result.md.
        prepare_run(repo_root, feature_id, "Implementer", _TASK)
        run_headless(
            repo_root, feature_id, "RUN-001", profile, claude_path=str(fake),
            started_at="2026-07-20T10:00:00Z", ended_at="2026-07-20T10:00:05Z",
        )

        verdict = validate_run(repo_root, feature_id, "RUN-001")

        assert not verdict.passed
        assert verdict.failed_check == "boundary"
        boundary_issues = [i for i in verdict.issues if i.check == "boundary"]
        assert len(boundary_issues) == 1
        assert boundary_issues[0].path == "workspace/hello.py"

    def test_declared_workspace_file_passes_boundary(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Declaring workspace/hello.py via the seam flips the same run to PASS.
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok")
        fake = _write_fake_claude(tmp_path / "bin")

        feature_id = create_feature_run(repo_root, "seam: declared workspace")
        prepare_run(
            repo_root, feature_id, "Implementer", _TASK,
            allowed_files=["workspace/hello.py"],
        )
        run_headless(
            repo_root, feature_id, "RUN-001", profile, claude_path=str(fake),
            started_at="2026-07-20T10:00:00Z", ended_at="2026-07-20T10:00:05Z",
        )

        verdict = validate_run(repo_root, feature_id, "RUN-001")

        assert verdict.passed, f"expected PASS with declared file: {verdict.issues}"
