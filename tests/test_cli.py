"""cli.main — the ``ai-dev`` console entry (tickets 01 + 04).

Exercises the public CLI wiring (argparse + dispatch) without spawning a
subprocess; the real subprocess path is covered by the manual end-to-end run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pytest
import yaml

from ai_dev.cli import main
from ai_dev.profiles import AgentProfile
from ai_dev.run_wrapper import RunResult

INTENT = "export reports for sharing"


def _feature_status(repo_root: Path, fid: str) -> dict:
    return yaml.safe_load(
        (repo_root / ".ai-dev" / "features" / fid / "status" / "feature-status.yml").read_text()
    )


class TestCliCreateFeatureRun:
    def test_creates_run_and_prints_id(self, repo_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(["create-feature-run", INTENT, "--repo-root", str(repo_root)])

        assert code == 0
        out = capsys.readouterr().out
        assert "FEATURE-001" in out
        assert (repo_root / ".ai-dev" / "features" / "FEATURE-001" / "00-intent.md").is_file()

    def test_two_invocations_increment(self, repo_root: Path) -> None:
        assert main(["create-feature-run", INTENT, "--repo-root", str(repo_root)]) == 0
        assert main(["create-feature-run", "second", "--repo-root", str(repo_root)]) == 0
        ids = sorted(p.name for p in (repo_root / ".ai-dev" / "features").iterdir())
        assert ids == ["FEATURE-001", "FEATURE-002"]

    def test_default_repo_root_is_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        code = main(["create-feature-run", INTENT])
        assert code == 0
        assert "FEATURE-001" in capsys.readouterr().out
        assert (tmp_path / ".ai-dev" / "features" / "FEATURE-001").is_dir()

    def test_missing_intent_exits_nonzero(self, repo_root: Path) -> None:
        # argparse rejects a missing required positional with exit code 2.
        with pytest.raises(SystemExit) as exc:
            main(["create-feature-run", "--repo-root", str(repo_root)])
        assert exc.value.code == 2


class TestCliFreeze:
    """``ai-dev freeze`` — the deterministic, model-free freeze entry (§4.2/§4.3).

    This is the only sanctioned way an artifact's frozen flag flips; the CLI
    delegates to ``status.freeze_artifact`` and surfaces its monotonic rejection
    as a non-zero exit rather than a traceback.
    """

    def test_freeze_flips_flag_and_returns_zero(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["create-feature-run", INTENT, "--repo-root", str(repo_root)])

        code = main(
            ["freeze", "FEATURE-001", "requirements", "--repo-root", str(repo_root)]
        )

        assert code == 0
        assert _feature_status(repo_root, "FEATURE-001")["feature"]["frozen_artifacts"][
            "requirements"
        ] is True
        assert "FEATURE-001" in capsys.readouterr().out

    def test_refreezing_exits_nonzero(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["create-feature-run", INTENT, "--repo-root", str(repo_root)])
        main(["freeze", "FEATURE-001", "requirements", "--repo-root", str(repo_root)])

        code = main(
            ["freeze", "FEATURE-001", "requirements", "--repo-root", str(repo_root)]
        )

        assert code == 1
        err = capsys.readouterr().err
        assert "already frozen" in err

    def test_unknown_feature_exits_nonzero(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(
            ["freeze", "FEATURE-999", "requirements", "--repo-root", str(repo_root)]
        )

        assert code == 1
        assert "not found" in capsys.readouterr().err

    def test_unknown_artifact_rejected_by_argparse(self, repo_root: Path) -> None:
        # argparse ``choices`` rejects a bogus artifact with exit code 2 before
        # any state is touched.
        main(["create-feature-run", INTENT, "--repo-root", str(repo_root)])

        with pytest.raises(SystemExit) as exc:
            main(["freeze", "FEATURE-001", "bogus", "--repo-root", str(repo_root)])
        assert exc.value.code == 2


class TestCliShowProfile:
    """``ai-dev show-profile`` - the v0.1 run-adapter profile inspector (§10).

    Prints the resolved profile with the token value redacted (source name +
    set/unset only); exits non-zero when the profile is missing or its token
    source is unset (§24.2 fail loud). The canonical profile shape comes from the
    shared ``write_profiles`` fixture (conftest), so the CLI tests exercise the
    same env_strip_pattern / multi-entry extra_env the module tests do.
    """

    def test_prints_resolved_profile_and_returns_zero_when_token_set(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        write_profiles(repo_root)
        monkeypatch.setenv("CC_GLM52_TOKEN", "live-token-value")

        code = main(["show-profile", "cc-glm52", "--repo-root", str(repo_root)])

        assert code == 0
        out = capsys.readouterr().out
        assert "profile: cc-glm52" in out
        assert "cli: claude" in out
        assert "auth_env: CC_GLM52_TOKEN" in out
        assert "token_source: CC_GLM52_TOKEN" in out
        assert "token_set: true" in out

    def test_fallback_token_set_returns_zero(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # auth_env unset -> the fallback source satisfies the token requirement.
        write_profiles(repo_root)
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "fallback-token-value")

        code = main(["show-profile", "cc-glm52", "--repo-root", str(repo_root)])

        assert code == 0
        out = capsys.readouterr().out
        assert "token_source: ANTHROPIC_AUTH_TOKEN" in out
        assert "token_set: true" in out

    def test_token_value_redacted_in_all_output(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # §10.2 / invariant #11: the token value must not appear in stdout or
        # stderr under any branch. A distinctive sentinel makes a leak visible.
        sentinel = "tok-CLI-LEAK-CHECK-7d2a9f"
        write_profiles(repo_root)
        monkeypatch.setenv("CC_GLM52_TOKEN", sentinel)

        main(["show-profile", "cc-glm52", "--repo-root", str(repo_root)])
        captured_set = capsys.readouterr()

        monkeypatch.setenv("CC_GLM52_TOKEN", "")  # force unset path
        main(["show-profile", "cc-glm52", "--repo-root", str(repo_root)])
        captured_unset = capsys.readouterr()

        assert sentinel not in captured_set.out
        assert sentinel not in captured_set.err
        assert sentinel not in captured_unset.out
        assert sentinel not in captured_unset.err

    def test_missing_token_source_exits_nonzero(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # §24.2: profile is valid config but its token source is unset - the
        # command still prints the profile, then signals non-readiness.
        write_profiles(repo_root)

        code = main(["show-profile", "cc-glm52", "--repo-root", str(repo_root)])

        assert code == 1
        out = capsys.readouterr()
        assert "token_set: false" in out.out
        assert "token source not set" in out.err

    def test_missing_profile_exits_nonzero(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        write_profiles(repo_root)

        code = main(["show-profile", "no-such-profile", "--repo-root", str(repo_root)])

        assert code == 1
        assert "not found" in capsys.readouterr().err

    def test_missing_profiles_file_exits_nonzero(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # No .ai-dev/agent-profiles.yml at all -> fail loud (§24.2), not a
        # default/empty profile.
        code = main(["show-profile", "cc-glm52", "--repo-root", str(repo_root)])

        assert code == 1
        assert "agent-profiles.yml" in capsys.readouterr().err

    def test_malformed_yaml_exits_nonzero_with_clean_error(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # §24.2: a corrupt registry surfaces a ProfileError message (not a raw
        # yaml traceback) and exits non-zero.
        write_profiles(repo_root, "agent_profiles: {")

        code = main(["show-profile", "cc-glm52", "--repo-root", str(repo_root)])

        assert code == 1
        err = capsys.readouterr().err
        assert "not valid YAML" in err
        # No Python traceback leaks to the user.
        assert "Traceback" not in err


class TestCliPrepareRun:
    """``ai-dev prepare-run`` - the v0.1 run-scaffold entry (§12, ticket 02).

    Allocates RUN-NNN under the feature run's ``runs/`` and writes the §12.2
    input package. Exits non-zero when the feature run is missing or
    ``--role`` / ``--task`` is omitted (argparse) or empty (§24.2 fail loud).
    """

    def test_prints_run_id_and_returns_zero(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["create-feature-run", INTENT, "--repo-root", str(repo_root)])

        code = main(
            [
                "prepare-run",
                "FEATURE-001",
                "--role",
                "Implementer",
                "--task",
                "Create workspace/hello.py.",
                "--repo-root",
                str(repo_root),
            ]
        )

        assert code == 0
        assert "RUN-001" in capsys.readouterr().out
        # The input package landed on disk under the feature's runs/.
        pkg = (
            repo_root
            / ".ai-dev"
            / "features"
            / "FEATURE-001"
            / "runs"
            / "RUN-001"
            / "input"
        )
        assert (pkg / "system.md").is_file()
        assert (pkg / "output-schema.json").is_file()

    def test_two_invocations_increment(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["create-feature-run", INTENT, "--repo-root", str(repo_root)])

        main(
            [
                "prepare-run",
                "FEATURE-001",
                "--role",
                "Implementer",
                "--task",
                "first",
                "--repo-root",
                str(repo_root),
            ]
        )
        capsys.readouterr()  # drain
        code = main(
            [
                "prepare-run",
                "FEATURE-001",
                "--role",
                "Reviewer",
                "--task",
                "second",
                "--repo-root",
                str(repo_root),
            ]
        )

        assert code == 0
        assert "RUN-002" in capsys.readouterr().out

    def test_missing_feature_exits_nonzero(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(
            [
                "prepare-run",
                "FEATURE-999",
                "--role",
                "Implementer",
                "--task",
                "anything",
                "--repo-root",
                str(repo_root),
            ]
        )

        assert code == 1
        assert "FEATURE-999" in capsys.readouterr().err

    def test_missing_role_rejected_by_argparse(self, repo_root: Path) -> None:
        # argparse rejects a missing required --role with exit code 2 before any
        # state is touched.
        main(["create-feature-run", INTENT, "--repo-root", str(repo_root)])

        with pytest.raises(SystemExit) as exc:
            main(
                [
                    "prepare-run",
                    "FEATURE-001",
                    "--task",
                    "something",
                    "--repo-root",
                    str(repo_root),
                ]
            )
        assert exc.value.code == 2

    def test_missing_task_rejected_by_argparse(self, repo_root: Path) -> None:
        main(["create-feature-run", INTENT, "--repo-root", str(repo_root)])

        with pytest.raises(SystemExit) as exc:
            main(
                [
                    "prepare-run",
                    "FEATURE-001",
                    "--role",
                    "Implementer",
                    "--repo-root",
                    str(repo_root),
                ]
            )
        assert exc.value.code == 2

    def test_default_repo_root_is_cwd(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # create + prepare against the cwd, with no --repo-root on either.
        monkeypatch.chdir(tmp_path)
        main(["create-feature-run", INTENT])
        code = main(
            ["prepare-run", "FEATURE-001", "--role", "Implementer", "--task", "x"]
        )

        assert code == 0
        assert "RUN-001" in capsys.readouterr().out
        assert (
            tmp_path / ".ai-dev" / "features" / "FEATURE-001" / "runs" / "RUN-001"
        ).is_dir()

    def test_allowed_file_flag_appends_to_allow_list(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Ticket 05 seam: --allowed-file (repeatable) declares task-specific
        # workspace paths so validate-run's §14.2 boundary check passes on a
        # run that writes them.
        main(["create-feature-run", INTENT, "--repo-root", str(repo_root)])

        code = main(
            [
                "prepare-run",
                "FEATURE-001",
                "--role",
                "Implementer",
                "--task",
                "Create workspace/hello.py.",
                "--allowed-file",
                "workspace/hello.py",
                "--allowed-file",
                "workspace/util.py",
                "--repo-root",
                str(repo_root),
            ]
        )

        assert code == 0
        capsys.readouterr()  # drain the run id
        allowed = (
            repo_root
            / ".ai-dev"
            / "features"
            / "FEATURE-001"
            / "runs"
            / "RUN-001"
            / "input"
            / "allowed-files.txt"
        ).read_text()
        assert "workspace/hello.py" in allowed
        assert "workspace/util.py" in allowed
        # The mandatory outputs are still present alongside the extras.
        assert "output/result.json" in allowed
        assert "output/result.md" in allowed

    def test_blank_allowed_file_exits_nonzero(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # §24.2 fail loud: a blank --allowed-file is a config error.
        main(["create-feature-run", INTENT, "--repo-root", str(repo_root)])

        code = main(
            [
                "prepare-run",
                "FEATURE-001",
                "--role",
                "Implementer",
                "--task",
                "x",
                "--allowed-file",
                "   ",
                "--repo-root",
                str(repo_root),
            ]
        )

        assert code == 1
        assert "allowed_files" in capsys.readouterr().err


class TestCliRunHeadless:
    """``ai-dev run-headless`` - the v0.1 headless-wrapper entry (§11, ticket 03).

    Exercises the CLI wiring (argparse + dispatch + summary print) without
    spawning the real ``claude`` subprocess: ``run_headless`` is monkeypatched
    to a stub returning a fixed ``RunResult``, matching the convention that the
    real subprocess path is covered by the manual end-to-end run. Error paths
    (missing profile / token / run dir) fail loud with a non-zero exit (§24.2).
    """

    def _stub_result(self, repo_root: Path, exit_code: int = 0) -> RunResult:
        run_root = repo_root / ".ai-dev" / "features" / "FEATURE-001" / "runs" / "RUN-001"
        return RunResult(
            run_id="RUN-001",
            feature_id="FEATURE-001",
            profile="cc-glm52",
            exit_code=exit_code,
            changed_files=["output/result.json", "output/result.md"],
            started_at="2026-07-19T10:00:00Z",
            ended_at="2026-07-19T10:00:05Z",
            stdout_path=run_root / "output" / "stdout.log",
            stderr_path=run_root / "output" / "stderr.log",
            metadata_path=run_root / "output" / "metadata.json",
        )

    def test_prints_summary_and_returns_zero_on_capture(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # A captured run (claude exited 0) prints a summary and exits 0. The
        # wrapper captures; consistency with result.json is ticket 04's call.
        write_profiles(repo_root)
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok")
        main(["create-feature-run", INTENT, "--repo-root", str(repo_root)])
        main([
            "prepare-run", "FEATURE-001", "--role", "Implementer",
            "--task", "x", "--repo-root", str(repo_root),
        ])
        capsys.readouterr()  # drain prepare-run output
        captured = {"called": False}

        def _fake_run_headless(*args: object, **kwargs: object) -> RunResult:
            captured["called"] = True
            return self._stub_result(repo_root)

        monkeypatch.setattr("ai_dev.cli.run_headless", _fake_run_headless)

        code = main([
            "run-headless", "FEATURE-001", "RUN-001", "--repo-root", str(repo_root),
        ])

        assert code == 0
        assert captured["called"] is True
        out = capsys.readouterr().out
        assert "RUN-001" in out
        assert "exit_code=0" in out

    def test_returns_zero_even_when_claude_exits_nonzero(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # A captured claude failure is still a successful *capture* - the CLI
        # exits 0 and surfaces the non-zero exit_code; validate-run decides
        # PASS/FAIL. This keeps the create->prepare->run->validate chain running.
        write_profiles(repo_root)
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok")
        main(["create-feature-run", INTENT, "--repo-root", str(repo_root)])
        main([
            "prepare-run", "FEATURE-001", "--role", "Implementer",
            "--task", "x", "--repo-root", str(repo_root),
        ])
        capsys.readouterr()
        monkeypatch.setattr(
            "ai_dev.cli.run_headless",
            lambda *a, **k: self._stub_result(repo_root, exit_code=2),
        )

        code = main([
            "run-headless", "FEATURE-001", "RUN-001", "--repo-root", str(repo_root),
        ])

        assert code == 0
        assert "exit_code=2" in capsys.readouterr().out

    def test_passes_profile_and_max_turns_to_wrapper(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # ``--profile`` and ``--max-turns`` flow through to run_headless.
        write_profiles(repo_root)
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok")
        main(["create-feature-run", INTENT, "--repo-root", str(repo_root)])
        main([
            "prepare-run", "FEATURE-001", "--role", "Implementer",
            "--task", "x", "--repo-root", str(repo_root),
        ])
        capsys.readouterr()
        seen: dict[str, object] = {}

        def _capture(
            repo_root: Path,
            feature_id: str,
            run_id: str,
            profile: AgentProfile,
            **kwargs: object,
        ) -> RunResult:
            seen["feature_id"] = feature_id
            seen["run_id"] = run_id
            seen["profile_name"] = profile.name
            seen.update(kwargs)
            return self._stub_result(repo_root)

        monkeypatch.setattr("ai_dev.cli.run_headless", _capture)

        main([
            "run-headless", "FEATURE-001", "RUN-001",
            "--profile", "cc-glm52", "--max-turns", "7",
            "--repo-root", str(repo_root),
        ])

        assert seen["max_turns"] == 7
        assert seen["feature_id"] == "FEATURE-001"
        assert seen["run_id"] == "RUN-001"
        assert seen["profile_name"] == "cc-glm52"

    def test_missing_token_exits_nonzero(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # §24.2: token source unset -> run_headless raises ValueError -> exit 1.
        write_profiles(repo_root)
        main(["create-feature-run", INTENT, "--repo-root", str(repo_root)])
        main([
            "prepare-run", "FEATURE-001", "--role", "Implementer",
            "--task", "x", "--repo-root", str(repo_root),
        ])
        capsys.readouterr()

        code = main([
            "run-headless", "FEATURE-001", "RUN-001", "--repo-root", str(repo_root),
        ])

        assert code == 1
        assert "token" in capsys.readouterr().err

    def test_missing_profile_exits_nonzero(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        write_profiles(repo_root)

        code = main([
            "run-headless", "FEATURE-001", "RUN-001",
            "--profile", "no-such-profile", "--repo-root", str(repo_root),
        ])

        assert code == 1
        assert "not found" in capsys.readouterr().err

    def test_missing_profiles_file_exits_nonzero(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main([
            "run-headless", "FEATURE-001", "RUN-001", "--repo-root", str(repo_root),
        ])

        assert code == 1
        assert "agent-profiles.yml" in capsys.readouterr().err


# A schema-valid result.json for the validate-run CLI tests.
_VALID_RESULT = {
    "status": "proposed_done",
    "summary": "Wrote workspace/hello.py for the run.",
    "tasks": [
        {"id": "TASK-001", "status": "proposed_done", "evidence": ["workspace/hello.py"]}
    ],
}


def _write_run_outputs(
    repo_root: Path, feature_id: str, run_id: str, result: object, changed_files: list[str]
) -> None:
    """Write a result.json + a minimal metadata.json into a prepared run dir."""
    from ai_dev.paths import run_dir

    out = run_dir(repo_root, feature_id, run_id) / "output"
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.json").write_text(
        result if isinstance(result, str) else json.dumps(result, indent=2) + "\n"
    )
    (out / "metadata.json").write_text(
        json.dumps({"run_id": run_id, "changed_files": changed_files}) + "\n"
    )


class TestCliValidateRun:
    """``ai-dev validate-run`` - the §14 three-check entry (ticket 04).

    Exercises the CLI wiring (argparse + dispatch + PASS/FAIL print) against real
    on-disk run artifacts. ``validate_run`` itself is covered exhaustively in
    ``test_validate.py``; here we pin the exit codes and the readable output.
    """

    @staticmethod
    def _prepare_run(repo_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Create FEATURE-001 + prepare RUN-001 (draining intermediate stdout)."""
        main(["create-feature-run", INTENT, "--repo-root", str(repo_root)])
        main([
            "prepare-run", "FEATURE-001", "--role", "Implementer",
            "--task", "x", "--repo-root", str(repo_root),
        ])
        capsys.readouterr()  # drain create/prepare output

    def test_validate_pass_exits_zero(
        self,
        repo_root: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._prepare_run(repo_root, capsys)
        _write_run_outputs(
            repo_root, "FEATURE-001", "RUN-001", _VALID_RESULT,
            ["output/result.json", "output/result.md"],
        )

        code = main([
            "validate-run", "FEATURE-001", "RUN-001", "--repo-root", str(repo_root),
        ])

        assert code == 0
        out = capsys.readouterr().out
        assert "VALIDATE PASS" in out
        assert "RUN-001" in out

    def test_validate_fail_schema_exits_one(
        self,
        repo_root: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._prepare_run(repo_root, capsys)
        _write_run_outputs(
            repo_root, "FEATURE-001", "RUN-001",
            dict(_VALID_RESULT, status="done"),  # bad enum -> schema violation
            ["output/result.json", "output/result.md"],
        )

        code = main([
            "validate-run", "FEATURE-001", "RUN-001", "--repo-root", str(repo_root),
        ])

        assert code == 1
        out = capsys.readouterr().out
        assert "VALIDATE FAIL" in out
        # Readable issue line: severity + check + message.
        assert "[P1] schema:" in out

    def test_validate_fail_boundary_exits_one(
        self,
        repo_root: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._prepare_run(repo_root, capsys)
        # workspace/hello.py is not in the seeded allowed-files -> boundary breach.
        _write_run_outputs(
            repo_root, "FEATURE-001", "RUN-001", _VALID_RESULT,
            ["output/result.json", "workspace/hello.py"],
        )

        code = main([
            "validate-run", "FEATURE-001", "RUN-001", "--repo-root", str(repo_root),
        ])

        assert code == 1
        out = capsys.readouterr().out
        assert "VALIDATE FAIL" in out
        assert "[P0] boundary:" in out
        assert "workspace/hello.py" in out

    def test_missing_run_dir_exits_one_with_error(
        self,
        repo_root: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(["create-feature-run", INTENT, "--repo-root", str(repo_root)])
        capsys.readouterr()

        code = main([
            "validate-run", "FEATURE-001", "RUN-999", "--repo-root", str(repo_root),
        ])

        assert code == 1
        err = capsys.readouterr().err
        assert "error:" in err
        assert "RUN-999" in err


class TestCliErrorMessages:
    """v0.4 §26.5 - clean, actionable ``error:`` rendering (ticket 01).

    The exit-code contract is unchanged (0=success / 1=everything-else); the
    investment is in message quality: a top-level catch turns uncaught
    exceptions into one ``error:`` line (no traceback) unless ``--debug``
    re-raises, and every command's failure carries an actionable hint
    ("did you mean", legal values, the env var to export).
    """

    def test_uncaught_exception_becomes_clean_error_line(
        self,
        repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # An unexpected (non-ValueError) exception must not dump a traceback -
        # it renders as a single ``error:`` line and exits 1 (§26.5).
        def _boom(*args: object, **kwargs: object) -> str:
            raise RuntimeError("kaboom: internal widget fault")

        monkeypatch.setattr("ai_dev.cli.create_feature_run", _boom)

        code = main(["create-feature-run", INTENT, "--repo-root", str(repo_root)])

        assert code == 1
        err = capsys.readouterr().err
        assert "error:" in err
        assert "kaboom: internal widget fault" in err
        # No Python traceback leaks to the user by default.
        assert "Traceback" not in err

    def test_debug_reraises_uncaught_exception(
        self,
        repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ``--debug`` opts back into the full traceback for diagnosis.
        def _boom(*args: object, **kwargs: object) -> str:
            raise RuntimeError("kaboom: internal widget fault")

        monkeypatch.setattr("ai_dev.cli.create_feature_run", _boom)

        with pytest.raises(RuntimeError, match="kaboom"):
            main([
                "--debug", "create-feature-run", INTENT,
                "--repo-root", str(repo_root),
            ])

    def test_feature_not_found_hint_lists_candidates(
        self,
        repo_root: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Two features exist; freezing a third, unknown one surfaces the
        # existing ids as a "did you mean / existing" hint (§26.5).
        main(["create-feature-run", INTENT, "--repo-root", str(repo_root)])
        main(["create-feature-run", "second intent", "--repo-root", str(repo_root)])
        capsys.readouterr()

        code = main([
            "freeze", "FEATURE-099", "requirements", "--repo-root", str(repo_root),
        ])

        assert code == 1
        err = capsys.readouterr().err
        assert "error:" in err
        assert "FEATURE-099" in err
        # Actionable: the operator sees what does exist.
        assert "existing:" in err
        assert "FEATURE-001" in err
        assert "FEATURE-002" in err

    def test_run_not_found_hint_lists_candidates(
        self,
        repo_root: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # A feature with one prepared run; validating a missing run surfaces the
        # existing run id rather than just "not found".
        main(["create-feature-run", INTENT, "--repo-root", str(repo_root)])
        main([
            "prepare-run", "FEATURE-001", "--role", "Implementer",
            "--task", "x", "--repo-root", str(repo_root),
        ])
        capsys.readouterr()

        code = main([
            "validate-run", "FEATURE-001", "RUN-099", "--repo-root", str(repo_root),
        ])

        assert code == 1
        err = capsys.readouterr().err
        assert "error:" in err
        assert "existing:" in err
        assert "RUN-001" in err

    def test_triage_refusal_hint_names_legal_cells(
        self,
        repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # An illegal disposition x severity cell is refused at the write layer;
        # the CLI hint names the legal matrix so the operator can re-issue. The
        # triage library logic is covered by test_triage; here we stub the apply
        # to isolate the CLI's hint rendering.
        from ai_dev.triage import TriageRefusedError

        def _refuse(*args: object, **kwargs: object) -> None:
            raise TriageRefusedError(
                "triage refused for ISSUE-001: P0 cannot be waived by override"
            )

        monkeypatch.setattr("ai_dev.cli.apply_triage", _refuse)

        code = main([
            "triage", "FEATURE-001", "--issue", "ISSUE-001",
            "--disposition", "override", "--repo-root", str(repo_root),
        ])

        assert code == 1
        err = capsys.readouterr().err
        assert "error:" in err
        assert "triage refused" in err
        # Actionable: legal cells + the --reason flag reminder.
        assert "legal cells" in err
        assert "--reason" in err

    def test_triage_unknown_issue_hint_lists_candidates(
        self,
        repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # An unknown ISSUE-NNN (a ValueError, not a refusal) points at the
        # issue ids that do exist under the feature, so the operator can correct
        # the reference. ``apply_triage`` is stubbed to isolate CLI rendering.
        main(["create-feature-run", INTENT, "--repo-root", str(repo_root)])
        issues_dir = (
            repo_root / ".ai-dev" / "features" / "FEATURE-001" / "issues"
        )
        issues_dir.mkdir(parents=True, exist_ok=True)
        for iid in ("ISSUE-001", "ISSUE-002"):
            (issues_dir / f"{iid}.json").write_text('{"id": "%s"}' % iid)
        capsys.readouterr()

        def _not_found(*args: object, **kwargs: object) -> None:
            raise ValueError(
                "issue ISSUE-099 not found under FEATURE-001/issues (§24.2)"
            )

        monkeypatch.setattr("ai_dev.cli.apply_triage", _not_found)

        code = main([
            "triage", "FEATURE-001", "--issue", "ISSUE-099",
            "--disposition", "accept", "--repo-root", str(repo_root),
        ])

        assert code == 1
        err = capsys.readouterr().err
        assert "error:" in err
        assert "ISSUE-099" in err
        # Actionable: existing issue ids are surfaced.
        assert "existing:" in err
        assert "ISSUE-001" in err
        assert "ISSUE-002" in err

    def test_token_unset_hint_names_source_var(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Profile is valid config but its token source is unset; the hint names
        # the concrete env var to export (not just "not set").
        write_profiles(repo_root)

        code = main(["show-profile", "cc-glm52", "--repo-root", str(repo_root)])

        assert code == 1
        err = capsys.readouterr().err
        assert "error:" in err
        assert "CC_GLM52_TOKEN" in err
        assert "export" in err




