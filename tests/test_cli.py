"""cli.main — the ``ai-dev`` console entry (tickets 01 + 04).

Exercises the public CLI wiring (argparse + dispatch) without spawning a
subprocess; the real subprocess path is covered by the manual end-to-end run.
"""

from __future__ import annotations

from pathlib import Path

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

