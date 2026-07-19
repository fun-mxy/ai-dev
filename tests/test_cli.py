"""cli.main — the ``ai-dev`` console entry (tickets 01 + 04).

Exercises the public CLI wiring (argparse + dispatch) without spawning a
subprocess; the real subprocess path is covered by the manual end-to-end run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest
import yaml

from ai_dev.cli import main

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

