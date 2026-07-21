"""fix_run - v0.3 ticket 07 bounded fix-loop driver."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from ai_dev.checking_legs import CheckingLegResult, REVIEW_DIR, REVIEW_REPORT_JSON, SPEC_GAP_DIR, SPEC_GAP_REPORT_JSON
from ai_dev.feature_run import create_feature_run
from ai_dev.fix_run import run_fix_run
from ai_dev.implement_leg import ImplementerLegResult
from ai_dev.issue_bundle import ISSUE_BUNDLE_JSON
from ai_dev.json_artifact import write_json
from ai_dev.paths import lane_dir, run_dir
from ai_dev.profiles import AgentProfile
from ai_dev.shell_verifier import CommandResult, VerifierResult
from ai_dev.validate import ValidationIssue, ValidationResult
from test_implement_leg import _feature_root, _seed_frozen_feature

_FIXED_PROFILE = AgentProfile(
    name="cc-glm52",
    cli="claude",
    backend="glm",
    base_url="https://ark.example.invalid",
    auth_env="CC_GLM52_TOKEN",
    auth_env_fallback=None,
    auth_target="ANTHROPIC_AUTH_TOKEN",
    model="glm-5.2",
    invocation="headless",
    extra_env={},
    env_strip_pattern=None,
)

_ISSUE_REPORT: dict[str, Any] = {
    "id": "LOCAL-001",
    "source": "code_review",
    "severity": "P1",
    "title": "answer() still lacks a docstring",
    "description": "The function remains undocumented.",
    "related_tasks": ["TASK-001"],
    "related_requirements": ["REQ-001"],
    "related_acceptance_criteria": ["AC-001"],
    "evidence": [{"file": "workspace/hello.py", "line": 1}],
    "recommendation": "Add a docstring.",
    "requires_change_proposal": False,
}


def _status_doc(repo_root: Path, feature_id: str) -> dict[str, Any]:
    return yaml.safe_load(
        (_feature_root(repo_root, feature_id) / "status" / "feature-status.yml").read_text()
    )


def _stage_request_fix_issue(repo_root: Path, feature_id: str) -> Path:
    issue_path = _feature_root(repo_root, feature_id) / "issues" / "ISSUE-001.json"
    issue = {
        **_ISSUE_REPORT,
        "id": "ISSUE-001",
        "status": "triaged",
        "triage": {
            "action": "request_fix",
            "reason": "Fix once before the gate.",
            "by": "human",
            "ts": "2026-07-21T09:00:00Z",
        },
    }
    write_json(issue_path, issue)
    return issue_path


def _passing_validation(run_id: str) -> ValidationResult:
    return ValidationResult(run_id=run_id, issues=[])


def _failing_validation(run_id: str) -> ValidationResult:
    return ValidationResult(
        run_id=run_id,
        issues=[
            ValidationIssue(
                check="boundary",
                code="file_out_of_bounds",
                message="workspace/hello.py is outside the allow-list",
                severity="P1",
                path="workspace/hello.py",
            )
        ],
    )


def _implement_result(feature_id: str, lane_id: str, validation: ValidationResult) -> ImplementerLegResult:
    root = _feature_root(Path.cwd(), feature_id)
    # Tests assert fields and budget behavior, not the rollup path contents.
    return ImplementerLegResult(
        run_id=validation.run_id,
        lane_id=lane_id,
        feature_id=feature_id,
        profile="cc-glm52",
        exit_code=0,
        result_status="proposed_done",
        validation=validation,
        task_ids_marked=["TASK-001"] if validation.passed else [],
        implement_result_md=root / "lanes" / lane_id / "implement-result.md",
        implement_result_json=root / "lanes" / lane_id / "implement-result.json",
    )


def _checking_result(repo_root: Path, feature_id: str, lane_id: str, run_id: str, source: str) -> CheckingLegResult:
    report_dir = lane_dir(repo_root, feature_id, lane_id) / (REVIEW_DIR if source == "code_review" else SPEC_GAP_DIR)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_json = report_dir / (REVIEW_REPORT_JSON if source == "code_review" else SPEC_GAP_REPORT_JSON)
    issues = [_ISSUE_REPORT] if source == "code_review" else []
    report_json.write_text(json.dumps({"issues": issues}, indent=2) + "\n")
    return CheckingLegResult(
        run_id=run_id,
        lane_id=lane_id,
        feature_id=feature_id,
        profile="cc-glm52",
        role="Code Reviewer" if source == "code_review" else "Spec Gap Analyst",
        source=source,
        exit_code=0,
        validation=_passing_validation(run_id),
        issue_count=len(issues),
        report_md=report_dir / "report.md",
        report_json=report_json,
    )


def _verifier_result(repo_root: Path, feature_id: str, lane_id: str) -> VerifierResult:
    return VerifierResult(
        lane_id=lane_id,
        feature_id=feature_id,
        implement_run_id="RUN-001",
        verdict="pass",
        command_results=[CommandResult("pytest", "pytest", 0, "", "")],
        report_md=lane_dir(repo_root, feature_id, lane_id) / "verification" / "verification-report.md",
        report_json=lane_dir(repo_root, feature_id, lane_id) / "verification" / "verification-report.json",
    )

captured_context: dict[str, str] = {}


def _capturing_implement(feature_id: str, lane_id: str) -> Any:
    def _inner(*args: Any, **kwargs: Any) -> ImplementerLegResult:
        captured_context["text"] = str(kwargs.get("task_context_append"))
        return _implement_result(feature_id, lane_id, _passing_validation("RUN-001"))

    return _inner


class TestFixRunBudget:
    def test_validated_implement_consumes_budget_and_marks_targets(self, repo_root: Path) -> None:
        feature_id, lane_id = _seed_frozen_feature(repo_root, tasks=["TASK-001"])
        issue_path = _stage_request_fix_issue(repo_root, feature_id)

        captured_context.clear()
        result = run_fix_run(
            repo_root,
            feature_id,
            lane_id,
            _FIXED_PROFILE,
            max_turns=12,
            permission_mode="bypassPermissions",
            verify_timeout=300,
            implement_leg=_capturing_implement(feature_id, lane_id),
            reviewer_leg=lambda *a, **kw: _checking_result(repo_root, feature_id, lane_id, "RUN-002", "code_review"),
            spec_gap_leg=lambda *a, **kw: _checking_result(repo_root, feature_id, lane_id, "RUN-003", "spec_gap"),
            verifier_leg=lambda *a, **kw: _verifier_result(repo_root, feature_id, lane_id),
        )

        assert result.budget_used == 1
        assert "ISSUE-001" in captured_context["text"]
        assert "Fix once before the gate." in captured_context["text"]
        assert "answer() still lacks a docstring" in captured_context["text"]
        assert _status_doc(repo_root, feature_id)["feature"]["fix_loop_budget"] == {"used": 1, "max": 1}
        assert json.loads(issue_path.read_text())["fix_targeted_in_run"] == "RUN-001"

    def test_failed_implement_validation_does_not_consume_budget(self, repo_root: Path) -> None:
        feature_id, lane_id = _seed_frozen_feature(repo_root, tasks=["TASK-001"])
        issue_path = _stage_request_fix_issue(repo_root, feature_id)

        with pytest.raises(ValueError, match="validation failed"):
            run_fix_run(
                repo_root,
                feature_id,
                lane_id,
                _FIXED_PROFILE,
                max_turns=12,
                permission_mode="bypassPermissions",
                verify_timeout=300,
                implement_leg=lambda *a, **kw: _implement_result(feature_id, lane_id, _failing_validation("RUN-001")),
            )

        assert _status_doc(repo_root, feature_id)["feature"]["fix_loop_budget"] == {"used": 0, "max": 1}
        assert "fix_targeted_in_run" not in json.loads(issue_path.read_text())

    def test_exhausted_budget_refuses_before_launch(self, repo_root: Path) -> None:
        feature_id, lane_id = _seed_frozen_feature(repo_root, tasks=["TASK-001"])
        _stage_request_fix_issue(repo_root, feature_id)
        status_path = _feature_root(repo_root, feature_id) / "status" / "feature-status.yml"
        doc = yaml.safe_load(status_path.read_text())
        doc["feature"]["fix_loop_budget"] = {"used": 1, "max": 1}
        status_path.write_text(yaml.safe_dump(doc, sort_keys=False))
        launched = False

        def _launch(*args: Any, **kwargs: Any) -> ImplementerLegResult:
            nonlocal launched
            launched = True
            return _implement_result(feature_id, lane_id, _passing_validation("RUN-001"))

        with pytest.raises(ValueError, match="fix_loop_budget exhausted"):
            run_fix_run(
                repo_root,
                feature_id,
                lane_id,
                _FIXED_PROFILE,
                max_turns=12,
                permission_mode="bypassPermissions",
                verify_timeout=300,
                implement_leg=_launch,
            )

        assert launched is False

    def test_recollected_target_reappears_and_requires_retriage(self, repo_root: Path) -> None:
        feature_id, lane_id = _seed_frozen_feature(repo_root, tasks=["TASK-001"])
        issue_path = _stage_request_fix_issue(repo_root, feature_id)
        # Seed the prior lane bundle so collector diffing has the old fingerprint.
        bundle_path = lane_dir(repo_root, feature_id, lane_id) / ISSUE_BUNDLE_JSON
        write_json(bundle_path, {"feature": feature_id, "lane": lane_id, "issue_count": 1, "issues": [json.loads(issue_path.read_text())]})

        run_fix_run(
            repo_root,
            feature_id,
            lane_id,
            _FIXED_PROFILE,
            max_turns=12,
            permission_mode="bypassPermissions",
            verify_timeout=300,
            implement_leg=lambda *a, **kw: _implement_result(feature_id, lane_id, _passing_validation("RUN-001")),
            reviewer_leg=lambda *a, **kw: _checking_result(repo_root, feature_id, lane_id, "RUN-002", "code_review"),
            spec_gap_leg=lambda *a, **kw: _checking_result(repo_root, feature_id, lane_id, "RUN-003", "spec_gap"),
            verifier_leg=lambda *a, **kw: _verifier_result(repo_root, feature_id, lane_id),
        )

        reappeared = json.loads(issue_path.read_text())
        assert reappeared["status"] == "reappeared"
        assert reappeared["triage"] is None
        assert reappeared["triage_history"][-1]["action"] == "request_fix"


def test_fix_run_cli_dispatches_and_prints_summary(
    repo_root: Path, write_profiles: Any, clean_token_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from ai_dev import cli

    write_profiles(repo_root)
    monkeypatch.setenv("CC_GLM52_TOKEN", "tok-fix-run")
    feature_id = create_feature_run(repo_root, "fix-run cli")

    def _fake_run_fix_run(*args: Any, **kwargs: Any) -> Any:
        class _Result:
            lane_id = "LANE-001"
            implement_run_id = "RUN-001"
            target_issue_ids = ["ISSUE-001"]
            budget_used = 1
            budget_max = 1
            verification = type("V", (), {"verdict": "pass"})()
            collection = type("C", (), {"issue_count": 1})()

        return _Result()

    monkeypatch.setattr(cli, "run_fix_run", _fake_run_fix_run)

    rc = cli.main(["fix-run", feature_id, "LANE-001", "--repo-root", str(repo_root)])

    assert rc == 0
    assert "FIX-RUN PASS" in capsys.readouterr().out
