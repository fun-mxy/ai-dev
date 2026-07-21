"""v0.4 ticket 03 — the read-only CLI observability commands (§26.5 CLI UX).

``list-features`` / ``show-status`` / ``log`` turn the CLI from write-only into
observable. They read canonical state other commands already write, render a
human-readable form by default, and emit machine-readable JSON with ``--json``.
``log`` consumes ticket 02's ``origin`` / ``elapsed_ms`` audit fields. Read-only
commands exit ``0`` when there is data and ``1`` with a clean ``error:`` when a
referenced feature does not exist (§24.2 fail loud).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_dev.cli import main
from ai_dev.lane_gate import LANE_DECISION_JSON, evaluate_lane_gate
from ai_dev.paths import lane_dir

from test_lane_gate import _stage_lane_gate_inputs  # noqa: E402


INTENT = "export reports for sharing"


class TestListFeatures:
    def test_empty_repo_exits_zero(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["list-features", "--repo-root", str(repo_root)])
        assert code == 0
        assert "(no features yet)" in capsys.readouterr().out

    def test_lists_feature_with_status_and_gate(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["create-feature-run", INTENT, "--repo-root", str(repo_root)])
        main(["create-feature-run", "second", "--repo-root", str(repo_root)])
        capsys.readouterr()  # discard the create-command stdout

        code = main(["list-features", "--repo-root", str(repo_root)])
        assert code == 0
        out = capsys.readouterr().out
        lines = [ln for ln in out.splitlines() if ln]
        assert len(lines) == 2
        assert lines[0].startswith("FEATURE-001")
        assert "status=planning" in lines[0]
        assert "gate=requirements_gate" in lines[0]
        assert "verdict=-" in lines[0]
        assert lines[1].startswith("FEATURE-002")

    def test_json_output_is_parseable(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["create-feature-run", INTENT, "--repo-root", str(repo_root)])
        capsys.readouterr()  # discard the create-command stdout

        code = main(["list-features", "--repo-root", str(repo_root), "--json"])
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload == [
            {
                "feature_id": "FEATURE-001",
                "status": "planning",
                "current_gate": "requirements_gate",
                "verdict": None,
            }
        ]

    def test_reflects_freeze_advancing_gate(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["create-feature-run", INTENT, "--repo-root", str(repo_root)])
        main(["freeze", "FEATURE-001", "requirements", "--repo-root", str(repo_root)])
        main(["freeze", "FEATURE-001", "design", "--repo-root", str(repo_root)])

        main(["list-features", "--repo-root", str(repo_root)])
        out = capsys.readouterr().out
        # freezing requirements -> design_gate, design -> task_gate; still planning
        assert "gate=task_gate" in out
        assert "status=planning" in out


class TestShowStatus:
    def test_missing_feature_exits_nonzero(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["show-status", "FEATURE-999", "--repo-root", str(repo_root)])
        assert code == 1
        err = capsys.readouterr().err
        assert "error:" in err
        assert "not found" in err

    def test_fresh_feature_shows_lane_without_decision(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["create-feature-run", INTENT, "--repo-root", str(repo_root)])

        code = main(["show-status", "FEATURE-001", "--repo-root", str(repo_root)])
        assert code == 0
        out = capsys.readouterr().out
        assert "FEATURE-001" in out
        assert "status: planning" in out
        assert "current_gate: requirements_gate" in out
        assert "verdict: (none)" in out
        # The lane exists in lane-status.yml even before any lane dir is built.
        assert "LANE-001: (no lane-decision yet)" in out

    def test_json_output_parseable(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["create-feature-run", INTENT, "--repo-root", str(repo_root)])
        capsys.readouterr()  # discard the create-command stdout

        code = main(
            ["show-status", "FEATURE-001", "--repo-root", str(repo_root), "--json"]
        )
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["feature_id"] == "FEATURE-001"
        assert payload["status"] == "planning"
        assert payload["verdict"] is None
        assert payload["lanes"] == [
            {
                "lane_id": "LANE-001",
                "decision": None,
                "failed_conditions": [],
                "blocking_issue_count": None,
            }
        ]

    def test_reflects_lane_decision(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Stage a full lane-gate PASS so a real lane-decision.json is written,
        # then confirm show-status surfaces its verdict.
        feature_id, lane_id = _stage_lane_gate_inputs(repo_root)
        evaluate_lane_gate(repo_root, feature_id, lane_id)

        code = main(["show-status", feature_id, "--repo-root", str(repo_root)])
        assert code == 0
        out = capsys.readouterr().out
        assert f"{lane_id}: decision=pass" in out

    def test_lane_decision_json_carries_failed_conditions(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Force a FAIL decision by writing a lane-decision.json directly, then
        # confirm show-status surfaces the failed condition names.
        feature_id, lane_id = _stage_lane_gate_inputs(repo_root)
        decision = {
            "feature": feature_id,
            "lane": lane_id,
            "decision": "fail",
            "conditions": [
                {"name": "proposed_done", "passed": True, "reason": "ok"},
                {"name": "verification_passed", "passed": False, "reason": "bad"},
            ],
            "blocking_issue_count": 0,
            "blocking_issues": [],
        }
        (lane_dir(repo_root, feature_id, lane_id) / LANE_DECISION_JSON).write_text(
            json.dumps(decision) + "\n"
        )

        code = main(["show-status", feature_id, "--repo-root", str(repo_root), "--json"])
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        lane = payload["lanes"][0]
        assert lane["decision"] == "fail"
        assert lane["failed_conditions"] == ["verification_passed"]


class TestLog:
    def test_missing_feature_exits_nonzero(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["log", "FEATURE-999", "--repo-root", str(repo_root)])
        assert code == 1
        assert "error:" in capsys.readouterr().err

    def test_renders_create_events_with_origin(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["create-feature-run", INTENT, "--repo-root", str(repo_root)])

        code = main(["log", "FEATURE-001", "--repo-root", str(repo_root)])
        assert code == 0
        out = capsys.readouterr().out
        # The create + allocate_id events, both stamped origin=cli (ticket 02).
        assert "· create · origin=cli" in out
        assert "· allocate_id · origin=cli" in out
        assert "- feature: FEATURE-001" in out

    def test_json_output_is_the_record_array(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["create-feature-run", INTENT, "--repo-root", str(repo_root)])
        capsys.readouterr()  # discard the create-command stdout

        code = main(["log", "FEATURE-001", "--repo-root", str(repo_root), "--json"])
        assert code == 0
        records = json.loads(capsys.readouterr().out)
        assert isinstance(records, list)
        assert records[0]["event"] == "create"
        assert records[0]["origin"] == "cli"
        assert records[0]["payload"] == {"feature": "FEATURE-001"}
        # allocate_id is the second event in creation order.
        assert records[1]["event"] == "allocate_id"

    def test_consumes_elapsed_ms_from_audit(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A freeze appends a real audit record; lane-gate writes elapsed_ms
        # (ticket 02). Render the timeline and confirm elapsed_ms surfaces.
        feature_id, lane_id = _stage_lane_gate_inputs(repo_root)
        evaluate_lane_gate(repo_root, feature_id, lane_id)

        code = main(["log", feature_id, "--repo-root", str(repo_root)])
        assert code == 0
        out = capsys.readouterr().out
        assert "· lane_gate" in out
        assert "elapsed_ms:" in out


class TestReadOnlyExcludesDryRun:
    """Read-only commands have no side effects, so ``--dry-run`` is not offered."""

    @pytest.mark.parametrize("cmd", ["list-features", "show-status", "log"])
    def test_dry_run_rejected_by_argparse(
        self, cmd: str, repo_root: Path
    ) -> None:
        main(["create-feature-run", INTENT, "--repo-root", str(repo_root)])
        argv = (
            [cmd, "FEATURE-001", "--repo-root", str(repo_root), "--dry-run"]
            if cmd != "list-features"
            else [cmd, "--repo-root", str(repo_root), "--dry-run"]
        )
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 2
