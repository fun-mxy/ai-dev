"""cli.main — the ``ai-dev`` console entry (ticket 01).

Exercises the public CLI wiring (argparse + dispatch) without spawning a
subprocess; the real subprocess path is covered by the manual end-to-end run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_dev.cli import main

INTENT = "export reports for sharing"


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
