"""checking_legs - v0.2 ticket 02, the Code Reviewer + Spec Gap Analyst legs.

The checking legs are the two review roles that run after an implement run
(§26.3): the Code Reviewer (§9.3) and the Spec Gap Analyst (§9.4). Both emit
``issues[]`` under the shared §15 Issue Contract, so they share one
output-schema and one ticket. From a feature run whose tasks + lane-graph are
frozen AND whose lane has an implement-result (ticket 01), each leg builds its
input package (reusing ``prepare_run`` with the §15 issues schema), runs it
headless, validates it (the §14 three checks against the issues schema), and
rolls the run's ``issues[]`` up into the lane-level
``review-report.{md,json}`` / ``spec-gap-report.{md,json}`` §4.4 double product.

These tests pin the four seams the ticket names: the §15 issues schema, the two
roles' input-package assembly (with the §9.3/§9.4 responsibility boundary), the
report rollup, and the orchestration - plus a CLI test driving each leg end to
end with a fake ``claude``.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

from ai_dev.audit import AUDIT_LOG_JSON
from ai_dev.checking_legs import (
    ISSUES_OUTPUT_SCHEMA,
    REVIEW_DIR,
    REVIEW_REPORT_JSON,
    REVIEW_REPORT_MD,
    SPEC_GAP_DIR,
    SPEC_GAP_REPORT_JSON,
    SPEC_GAP_REPORT_MD,
    CheckingLegResult,
    build_reviewer_input_package,
    build_spec_gap_input_package,
    read_implement_run_facts,
    run_reviewer_leg,
    run_spec_gap_leg,
    write_review_report,
    write_spec_gap_report,
)
from ai_dev.cli import main
from ai_dev.feature_run import create_feature_run
from ai_dev.implement_leg import (
    IMPLEMENT_RESULT_JSON,
    write_implement_result,
)
from ai_dev.paths import lane_dir, run_dir
from ai_dev.profiles import load_profile
from ai_dev.run_prepare import (
    OUTPUT_SCHEMA_FILE,
    ROLE_FILE,
    TASK_PACKAGE_FILE,
    prepare_run,
)
from ai_dev.status import freeze_artifact
from ai_dev.templates import DESIGN_MD, LANE_GRAPH_YML, REQUIREMENTS_MD, TASKS_MD
from ai_dev.validate import validate_against_schema, validate_run

# Reuse the implementer-leg test scaffolding (frozen-feature seeding, fake
# claude shape) so the checking-leg tests stand up the same precondition: a
# feature run with frozen tasks + lane-graph and a real implement run to review.
from test_implement_leg import (  # noqa: E402
    _METADATA_JSON as _IMPL_METADATA_JSON,
    _RESULT_JSON as _IMPL_RESULT_JSON,
    _feature_root,
    _fill_artifacts,
    _seed_frozen_feature,
)

_TASK_BODY = "Create workspace/hello.py defining answer() returning 42."
_IMPL_WORKSPACE_FILE = "workspace/hello.py"
_IMPL_WORKSPACE_CONTENT = "# throwaway prototype module\ndef answer():\n    return 42\n"


# ---------------------------------------------------------------------------
# A valid §15 issues result.json the checking-leg fake claude writes. The
# ``source`` is set per role by the test that stages the fake; the runtime
# overrides it to the role's canonical value in the rolled-up report anyway.
# ---------------------------------------------------------------------------
_ISSUES_PAYLOAD: dict[str, Any] = {
    "issues": [
        {
            "id": "ISSUE-001",
            "source": "code_review",
            "severity": "P2",
            "title": "answer() has no docstring",
            "description": "The answer() function does not document its return value.",
            "related_tasks": ["TASK-001"],
            "related_requirements": [],
            "related_acceptance_criteria": [],
            "evidence": [{"file": "workspace/hello.py", "line": 2}],
            "recommendation": "Add a one-line docstring describing the return value.",
            "requires_change_proposal": False,
        }
    ]
}


def _stage_implement_run(
    repo_root: Path,
    *,
    task_body: str = _TASK_BODY,
    expected_files: list[str] | None = None,
    exclusive_files: list[str] | None = None,
    tasks: list[str] | None = None,
) -> tuple[str, str, str]:
    """Stage a real implement run (RUN-001) for a checking leg to review.

    Creates a frozen feature, prepares an Implementer run with the lane's
    workspace file allowed, writes the implement run's outputs (result.json /
    result.md / metadata.json / workspace file) directly (no fake claude), and
    writes the lane ``implement-result.{md,json}`` rollup via the real
    ``write_implement_result``. Returns ``(feature_id, lane_id, implement_run_id)``.
    """
    if expected_files is None:
        expected_files = [_IMPL_WORKSPACE_FILE]
    if exclusive_files is None:
        exclusive_files = [_IMPL_WORKSPACE_FILE]
    feature_id, lane_id = _seed_frozen_feature(
        repo_root,
        task_body=task_body,
        expected_files=expected_files,
        exclusive_files=exclusive_files,
        tasks=tasks,
    )
    impl_run_id = prepare_run(
        repo_root,
        feature_id,
        "Implementer",
        task_body,
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


def _fill_specs(repo_root: Path, feature_id: str) -> None:
    """Overwrite the seeded requirements/design with identifiable content.

    The gap leg reads these frozen specs to compare against the implement diff;
    the seeded templates are placeholders, so tests stand up realistic content a
    Planner would have frozen at the requirements / design gates (§18.1/§18.2).
    """
    root = _feature_root(repo_root, feature_id)
    (root / REQUIREMENTS_MD).write_text(
        f"# Requirements - {feature_id}\n\nFrozen: true\n\n"
        f"## Requirements (REQ-NNN)\n\n"
        f"- REQ-001: The module must define answer() returning 42.\n"
        f"- REQ-002: The module must export a usage example. (intentionally unmet)\n"
    )
    (root / DESIGN_MD).write_text(
        f"# Design - {feature_id}\n\nFrozen: true\n\n"
        f"## Design elements (DES-NNN)\n\n"
        f"- DES-001: A single workspace/hello.py module with answer().\n"
    )


# ---------------------------------------------------------------------------
# Fake claude for the checking legs: writes an issues result.json (+ result.md)
# and exits 0. The payload is base64-encoded into the script (not embedded as a
# Python literal) so boolean/None values survive without quoting traps, and a
# non-conforming payload drives the schema-FAIL case.
# ---------------------------------------------------------------------------
_FAKE_CLAUDE_ISSUES = """\
#!__PY__
import json, os, sys, base64
os.makedirs("output", exist_ok=True)
with open("output/result.md", "w") as f:
    f.write("Checking run complete.\\n")
payload = json.loads(base64.b64decode("__B64__"))
with open("output/result.json", "w") as f:
    json.dump(payload, f)
sys.stdout.write('{"type":"result","subtype":"success","is_error":false}\\n')
sys.exit(0)
"""


def _write_fake_claude_issues(
    bin_dir: Path, payload: dict[str, Any]
) -> Path:
    import base64

    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "claude"
    b64 = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    script.write_text(
        _FAKE_CLAUDE_ISSUES.replace("__PY__", sys.executable).replace("__B64__", b64)
    )
    os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _read_allowed(path: Path) -> set[str]:
    entries: set[str] = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            entries.add(line)
    return entries


# ===========================================================================
# Seam 1: the shared §15 issues output-schema.
# ===========================================================================


class TestIssuesOutputSchema:
    """The §15 Issue Contract as a JSON Schema, shared by reviewer + gap. A
    valid ``{"issues": [...]}`` result.json conforms; a malformed one is
    rejected - so ``validate-run`` (§14.1) gates the checking runs on the same
    contract the implementer run is gated on."""

    def test_valid_issues_payload_conforms(self) -> None:
        errors = validate_against_schema(_ISSUES_PAYLOAD, ISSUES_OUTPUT_SCHEMA)
        assert errors == []

    def test_empty_issues_list_conforms(self) -> None:
        # A checking run that finds NO issues is a valid result (issues: []).
        errors = validate_against_schema({"issues": []}, ISSUES_OUTPUT_SCHEMA)
        assert errors == []

    def test_missing_issues_rejected(self) -> None:
        errors = validate_against_schema({"status": "proposed_done"}, ISSUES_OUTPUT_SCHEMA)
        assert errors
        assert any("issues" in e for e in errors)

    def test_issue_missing_required_field_rejected(self) -> None:
        bad = {
            "issues": [
                {
                    "id": "ISSUE-001",
                    "source": "code_review",
                    "severity": "P2",
                    # title, description, evidence, recommendation,
                    # requires_change_proposal, related_* all missing.
                }
            ]
        }
        errors = validate_against_schema(bad, ISSUES_OUTPUT_SCHEMA)
        assert errors
        joined = " ".join(errors)
        assert "title" in joined
        assert "recommendation" in joined

    def test_bad_severity_rejected(self) -> None:
        bad = {
            "issues": [
                {
                    "id": "ISSUE-001",
                    "source": "code_review",
                    "severity": "P9",  # not in §15.1
                    "title": "t",
                    "description": "d",
                    "related_tasks": [],
                    "related_requirements": [],
                    "related_acceptance_criteria": [],
                    "evidence": [],
                    "recommendation": "r",
                    "requires_change_proposal": False,
                }
            ]
        }
        errors = validate_against_schema(bad, ISSUES_OUTPUT_SCHEMA)
        assert errors
        assert any("P9" in e or "enum" in e for e in errors)

    def test_bad_source_rejected(self) -> None:
        # The schema constrains source to the two checking sources (§15).
        bad = json.loads(json.dumps(_ISSUES_PAYLOAD))
        bad["issues"][0]["source"] = "random_source"
        errors = validate_against_schema(bad, ISSUES_OUTPUT_SCHEMA)
        assert errors
        assert any("random_source" in e or "enum" in e for e in errors)

    def test_evidence_item_requires_file(self) -> None:
        bad = json.loads(json.dumps(_ISSUES_PAYLOAD))
        bad["issues"][0]["evidence"] = [{"line": 5}]  # no file
        errors = validate_against_schema(bad, ISSUES_OUTPUT_SCHEMA)
        assert errors
        assert any("file" in e for e in errors)

    def test_requires_change_proposal_must_be_boolean(self) -> None:
        bad = json.loads(json.dumps(_ISSUES_PAYLOAD))
        bad["issues"][0]["requires_change_proposal"] = "no"
        errors = validate_against_schema(bad, ISSUES_OUTPUT_SCHEMA)
        assert errors


# ===========================================================================
# Seam 2: reading the implement run a checking leg reviews.
# ===========================================================================


class TestReadImplementRunFacts:
    """``read_implement_run_facts`` gathers the implement run id (from the lane
    implement-result), its changed_files (from metadata), each changed file's
    content (the 'diff'), and the task text (the reviewer's context)."""

    def test_reads_run_id_changed_files_and_contents(self, repo_root: Path) -> None:
        feature_id, lane_id, impl_run_id = _stage_implement_run(repo_root)

        facts = read_implement_run_facts(repo_root, feature_id, lane_id)

        assert facts.run_id == impl_run_id
        assert _IMPL_WORKSPACE_FILE in facts.changed_files
        assert "output/result.json" in facts.changed_files
        # The workspace file content flows through as the 'diff'.
        assert facts.file_contents[_IMPL_WORKSPACE_FILE] == _IMPL_WORKSPACE_CONTENT
        # The task text (03-tasks.md body) is the reviewer's context.
        assert _TASK_BODY in facts.task_text

    def test_fails_loud_when_no_implement_result(self, repo_root: Path) -> None:
        # A frozen feature with NO implement run has nothing to check.
        feature_id, lane_id = _seed_frozen_feature(repo_root)

        with pytest.raises(ValueError, match="implement-result"):
            read_implement_run_facts(repo_root, feature_id, lane_id)


# ===========================================================================
# Seam 3: reviewer + spec-gap input-package assembly (with §9.3/§9.4 boundary).
# ===========================================================================


class TestBuildReviewerInputPackage:
    """The reviewer input package reuses ``prepare_run`` with the §15 issues
    schema, role pinned to Code Reviewer, and a task-package carrying the
    implement run's changed_files + their contents + the task context."""

    def test_role_pinned_to_code_reviewer(self, repo_root: Path) -> None:
        feature_id, lane_id, _ = _stage_implement_run(repo_root)

        run_id = build_reviewer_input_package(repo_root, feature_id, lane_id)

        role = (run_dir(repo_root, feature_id, run_id) / "input" / ROLE_FILE).read_text()
        assert role == f"You are the Code Reviewer for {run_id}.\n"

    def test_issues_schema_written(self, repo_root: Path) -> None:
        feature_id, lane_id, _ = _stage_implement_run(repo_root)

        run_id = build_reviewer_input_package(repo_root, feature_id, lane_id)

        schema = json.loads(
            (run_dir(repo_root, feature_id, run_id) / "input" / OUTPUT_SCHEMA_FILE).read_text()
        )
        assert schema["title"] == "IssuesReport"
        assert "issues" in schema["required"]

    def test_task_package_carries_changed_files_and_diff(self, repo_root: Path) -> None:
        feature_id, lane_id, _ = _stage_implement_run(repo_root)

        run_id = build_reviewer_input_package(repo_root, feature_id, lane_id)

        task_pkg = (
            run_dir(repo_root, feature_id, run_id) / "input" / TASK_PACKAGE_FILE
        ).read_text()
        # The implement run's changed files and their content (the 'diff') flow
        # into the reviewer's task-package.
        assert _IMPL_WORKSPACE_FILE in task_pkg
        assert "def answer" in task_pkg  # the workspace file content
        # The implement run's output/ report artifacts are listed in the
        # changed_files footprint but their content is NOT dumped (they are the
        # implementer's self-report, not code - §13.2).
        assert "output/result.json" in task_pkg  # listed in changed_files
        assert '"status": "proposed_done"' not in task_pkg  # content not dumped
        # The task context (what was implemented) is present.
        assert _TASK_BODY in task_pkg

    def test_boundary_says_no_spec_deviation(self, repo_root: Path) -> None:
        # §9.3: the reviewer reviews code quality, NOT spec deviation. The
        # task-package must make that boundary explicit.
        feature_id, lane_id, _ = _stage_implement_run(repo_root)

        run_id = build_reviewer_input_package(repo_root, feature_id, lane_id)

        task_pkg = (
            run_dir(repo_root, feature_id, run_id) / "input" / TASK_PACKAGE_FILE
        ).read_text()
        assert "spec deviation" in task_pkg.lower() or "spec-gap" in task_pkg.lower()
        assert "code_review" in task_pkg

    def test_allowed_files_are_only_mandatory_outputs(self, repo_root: Path) -> None:
        # The reviewer writes only result.json + result.md - no workspace files.
        feature_id, lane_id, _ = _stage_implement_run(repo_root)

        run_id = build_reviewer_input_package(repo_root, feature_id, lane_id)

        allowed = _read_allowed(
            run_dir(repo_root, feature_id, run_id) / "input" / "allowed-files.txt"
        )
        assert allowed == {"output/result.json", "output/result.md"}

    def test_requires_frozen_artifacts(self, repo_root: Path) -> None:
        # An unfrozen feature (no freeze called) is rejected before the
        # implement-run check - the specs must be stable when reviewed (§4.2).
        feature_id, lane_id = _seed_frozen_feature(repo_root, freeze=False)
        _fill_artifacts(repo_root, feature_id, lane_id)  # ensure artifacts present
        with pytest.raises(ValueError, match="frozen"):
            build_reviewer_input_package(repo_root, feature_id, lane_id)

    def test_fails_loud_when_no_implement_result(self, repo_root: Path) -> None:
        feature_id, lane_id = _seed_frozen_feature(repo_root)
        with pytest.raises(ValueError, match="implement-result"):
            build_reviewer_input_package(repo_root, feature_id, lane_id)


class TestBuildSpecGapInputPackage:
    """The spec-gap input package carries requirements/design/tasks + the
    implement diff, role pinned to Spec Gap Analyst, issues schema."""

    def test_role_pinned_to_spec_gap_analyst(self, repo_root: Path) -> None:
        feature_id, lane_id, _ = _stage_implement_run(repo_root)
        _fill_specs(repo_root, feature_id)

        run_id = build_spec_gap_input_package(repo_root, feature_id, lane_id)

        role = (run_dir(repo_root, feature_id, run_id) / "input" / ROLE_FILE).read_text()
        assert role == f"You are the Spec Gap Analyst for {run_id}.\n"

    def test_task_package_carries_specs_and_diff(self, repo_root: Path) -> None:
        feature_id, lane_id, _ = _stage_implement_run(repo_root)
        _fill_specs(repo_root, feature_id)

        run_id = build_spec_gap_input_package(repo_root, feature_id, lane_id)

        task_pkg = (
            run_dir(repo_root, feature_id, run_id) / "input" / TASK_PACKAGE_FILE
        ).read_text()
        # The frozen specs flow into the gap's task-package.
        assert "REQ-001" in task_pkg
        assert "REQ-002" in task_pkg
        assert "DES-001" in task_pkg
        # The implement diff flows in too.
        assert _IMPL_WORKSPACE_FILE in task_pkg
        assert "def answer" in task_pkg

    def test_boundary_says_no_style_review(self, repo_root: Path) -> None:
        # §9.4: the gap compares spec vs implementation, NOT code style.
        feature_id, lane_id, _ = _stage_implement_run(repo_root)
        _fill_specs(repo_root, feature_id)

        run_id = build_spec_gap_input_package(repo_root, feature_id, lane_id)

        task_pkg = (
            run_dir(repo_root, feature_id, run_id) / "input" / TASK_PACKAGE_FILE
        ).read_text()
        assert "style" in task_pkg.lower()
        assert "spec_gap" in task_pkg

    def test_issues_schema_written(self, repo_root: Path) -> None:
        feature_id, lane_id, _ = _stage_implement_run(repo_root)
        _fill_specs(repo_root, feature_id)

        run_id = build_spec_gap_input_package(repo_root, feature_id, lane_id)

        schema = json.loads(
            (run_dir(repo_root, feature_id, run_id) / "input" / OUTPUT_SCHEMA_FILE).read_text()
        )
        assert schema["title"] == "IssuesReport"


# ===========================================================================
# Seam 4: review-report + spec-gap-report rollup (§4.4 md+json double product).
# ===========================================================================


_REVIEW_RUN_METADATA = {
    "run_id": "RUN-002",
    "profile": "cc-glm52",
    "cli": "claude",
    "backend": "glm",
    "model": "glm-5.2",
    "started_at": "2026-07-20T11:00:00Z",
    "ended_at": "2026-07-20T11:00:05Z",
    "exit_code": 0,
    "changed_files": ["output/result.json", "output/result.md"],
    "commits": [],
    "checks": [],
}


def _stage_passing_checking_run(
    repo_root: Path, role: str, payload: dict[str, Any]
) -> tuple[str, str, str, "object"]:
    """Stage an implement run + a checking run with a passing issues result.

    Returns ``(feature_id, lane_id, checking_run_id, validation)``.
    """
    feature_id, lane_id, _ = _stage_implement_run(repo_root)
    _fill_specs(repo_root, feature_id)
    run_id = prepare_run(
        repo_root, feature_id, role, "checking task",
        output_schema=ISSUES_OUTPUT_SCHEMA,
    )
    out = run_dir(repo_root, feature_id, run_id) / "output"
    (out / "result.json").write_text(json.dumps(payload))
    (out / "result.md").write_text("Checking run complete.\n")
    (out / "metadata.json").write_text(json.dumps(_REVIEW_RUN_METADATA))
    validation = validate_run(repo_root, feature_id, run_id)
    assert validation.passed, f"staging checking run should validate: {validation.issues}"
    return feature_id, lane_id, run_id, validation


class TestWriteReviewReport:
    """``write_review_report`` rolls the run's issues + metadata + validation
    into lane-level ``review-report.{md,json}``, with each issue's ``source``
    overridden to the reviewer's canonical ``code_review``."""

    def test_writes_md_and_json_under_lane_dir(self, repo_root: Path) -> None:
        feature_id, lane_id, run_id, validation = _stage_passing_checking_run(
            repo_root, "Code Reviewer", _ISSUES_PAYLOAD
        )
        root = _feature_root(repo_root, feature_id)

        md_path, json_path = write_review_report(
            root, lane_id, run_id=run_id, result=_ISSUES_PAYLOAD,
            metadata=_REVIEW_RUN_METADATA, validation=validation,  # type: ignore[arg-type]
        )

        assert md_path == root / "lanes" / lane_id / REVIEW_DIR / REVIEW_REPORT_MD
        assert json_path == root / "lanes" / lane_id / REVIEW_DIR / REVIEW_REPORT_JSON
        assert md_path.is_file() and json_path.is_file()

    def test_json_rollup_carries_issues_verbatim(
        self, repo_root: Path
    ) -> None:
        # The issues are carried verbatim from result.json (matching the
        # implementer leg's verbatim carry of result fields). The report's
        # top-level ``source`` is the reviewer's canonical label.
        feature_id, lane_id, run_id, validation = _stage_passing_checking_run(
            repo_root, "Code Reviewer", _ISSUES_PAYLOAD
        )
        root = _feature_root(repo_root, feature_id)

        _, json_path = write_review_report(
            root, lane_id, run_id=run_id, result=_ISSUES_PAYLOAD,
            metadata=_REVIEW_RUN_METADATA, validation=validation,  # type: ignore[arg-type]
        )
        rollup = json.loads(json_path.read_text())

        assert rollup["feature"] == feature_id
        assert rollup["lane"] == lane_id
        assert rollup["run"] == run_id
        assert rollup["role"] == "Code Reviewer"
        assert rollup["source"] == "code_review"  # top-level role label
        assert len(rollup["issues"]) == 1
        # The agent's issue is carried verbatim (source, severity, evidence).
        assert rollup["issues"][0] == _ISSUES_PAYLOAD["issues"][0]
        assert rollup["issues"][0]["source"] == "code_review"
        assert rollup["issues"][0]["severity"] == "P2"
        assert rollup["issues"][0]["evidence"] == [{"file": "workspace/hello.py", "line": 2}]
        # Wrapper metadata nested (§13.2).
        assert rollup["run_metadata"]["profile"] == "cc-glm52"
        assert rollup["run_metadata"]["changed_files"] == _REVIEW_RUN_METADATA["changed_files"]
        # Validation verdict carried.
        assert rollup["validation"]["passed"] is True
        assert rollup["validation"]["issues"] == []

    def test_md_mirror_carries_key_fields(self, repo_root: Path) -> None:
        feature_id, lane_id, run_id, validation = _stage_passing_checking_run(
            repo_root, "Code Reviewer", _ISSUES_PAYLOAD
        )
        root = _feature_root(repo_root, feature_id)

        md_path, _ = write_review_report(
            root, lane_id, run_id=run_id, result=_ISSUES_PAYLOAD,
            metadata=_REVIEW_RUN_METADATA, validation=validation,  # type: ignore[arg-type]
        )
        md = md_path.read_text()

        assert "Code Reviewer" in md
        assert f"lane: {lane_id}" in md
        assert f"run: {run_id}" in md
        assert "P2" in md  # severity
        assert "answer() has no docstring" in md  # issue title

    def test_rollup_reports_failed_validation_with_no_issues(
        self, repo_root: Path
    ) -> None:
        # A checking run whose result.json is schema-invalid fails validation;
        # the report records the failure and carries no trusted issues.
        feature_id, lane_id, _ = _stage_implement_run(repo_root)
        run_id = prepare_run(
            repo_root, feature_id, "Code Reviewer", "checking task",
            output_schema=ISSUES_OUTPUT_SCHEMA,
        )
        out = run_dir(repo_root, feature_id, run_id) / "output"
        (out / "result.json").write_text(json.dumps({"status": "proposed_done"}))  # no issues
        (out / "result.md").write_text("bad\n")
        (out / "metadata.json").write_text(json.dumps(_REVIEW_RUN_METADATA))
        validation = validate_run(repo_root, feature_id, run_id)
        assert not validation.passed
        assert validation.failed_check == "schema"

        root = _feature_root(repo_root, feature_id)
        _, json_path = write_review_report(
            root, lane_id, run_id=run_id, result=None,
            metadata=_REVIEW_RUN_METADATA, validation=validation,
        )
        rollup = json.loads(json_path.read_text())

        assert rollup["validation"]["passed"] is False
        assert rollup["validation"]["failed_check"] == "schema"
        assert rollup["issues"] == []  # no trusted issues on a failed run


class TestWriteSpecGapReport:
    """``write_spec_gap_report`` mirrors ``write_review_report`` for the gap,
    carrying issues verbatim with the report's top-level ``source`` = spec_gap."""

    def test_json_rollup_carries_gap_issues_verbatim(self, repo_root: Path) -> None:
        gap_payload = json.loads(json.dumps(_ISSUES_PAYLOAD))
        gap_payload["issues"][0]["source"] = "spec_gap"  # correct for a gap
        gap_payload["issues"][0]["title"] = "REQ-002 not implemented"
        gap_payload["issues"][0]["severity"] = "P1"
        feature_id, lane_id, run_id, validation = _stage_passing_checking_run(
            repo_root, "Spec Gap Analyst", gap_payload
        )
        root = _feature_root(repo_root, feature_id)

        _, json_path = write_spec_gap_report(
            root, lane_id, run_id=run_id, result=gap_payload,
            metadata=_REVIEW_RUN_METADATA, validation=validation,  # type: ignore[arg-type]
        )
        rollup = json.loads(json_path.read_text())

        assert rollup["role"] == "Spec Gap Analyst"
        assert rollup["source"] == "spec_gap"  # top-level role label
        # The agent's gap issue is carried verbatim.
        assert rollup["issues"][0] == gap_payload["issues"][0]
        assert rollup["issues"][0]["source"] == "spec_gap"
        assert rollup["issues"][0]["severity"] == "P1"

    def test_writes_md_and_json_under_lane_dir(self, repo_root: Path) -> None:
        feature_id, lane_id, run_id, validation = _stage_passing_checking_run(
            repo_root, "Spec Gap Analyst", _ISSUES_PAYLOAD
        )
        root = _feature_root(repo_root, feature_id)

        md_path, json_path = write_spec_gap_report(
            root, lane_id, run_id=run_id, result=_ISSUES_PAYLOAD,
            metadata=_REVIEW_RUN_METADATA, validation=validation,  # type: ignore[arg-type]
        )

        assert md_path == root / "lanes" / lane_id / SPEC_GAP_DIR / SPEC_GAP_REPORT_MD
        assert json_path == root / "lanes" / lane_id / SPEC_GAP_DIR / SPEC_GAP_REPORT_JSON


# ===========================================================================
# Seam 5: orchestration (build input package -> run -> validate -> rollup).
# ===========================================================================


class TestRunReviewerLeg:
    """``run_reviewer_leg`` wires the v0.1 seams: a passing reviewer run
    (issues result.json) validates and rolls up into the lane review-report.
    No canonical status is written (unlike the implementer leg)."""

    def test_passing_run_validates_and_rolls_up(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-review")
        fake = _write_fake_claude_issues(tmp_path / "bin", _ISSUES_PAYLOAD)
        feature_id, lane_id, _ = _stage_implement_run(repo_root)

        result = run_reviewer_leg(
            repo_root, feature_id, lane_id, profile,
            claude_path=str(fake),
            started_at="2026-07-20T11:00:00Z",
            ended_at="2026-07-20T11:00:05Z",
        )

        assert result.run_id == "RUN-002"  # RUN-001 was the implement run
        assert result.role == "Code Reviewer"
        assert result.source == "code_review"
        assert result.exit_code == 0
        assert result.validation.passed
        assert result.issue_count == 1
        # The lane review-report landed.
        lane_root = lane_dir(repo_root, feature_id, lane_id)
        rollup = json.loads((lane_root / REVIEW_DIR / REVIEW_REPORT_JSON).read_text())
        assert rollup["role"] == "Code Reviewer"
        assert rollup["source"] == "code_review"
        assert len(rollup["issues"]) == 1
        assert rollup["issues"][0]["source"] == "code_review"
        # No canonical task-status writeback (only the implementer leg writes
        # that - the audit test below asserts mark_task_proposed_done is absent).

    def test_failed_validation_still_rolls_up(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # A reviewer run whose result.json does not conform to the issues schema
        # fails validation; the report records the failure, no trusted issues.
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-review-fail")
        bad_payload = {"status": "proposed_done"}  # no issues[] -> schema FAIL
        fake = _write_fake_claude_issues(tmp_path / "bin", bad_payload)
        feature_id, lane_id, _ = _stage_implement_run(repo_root)

        result = run_reviewer_leg(
            repo_root, feature_id, lane_id, profile, claude_path=str(fake),
            started_at="2026-07-20T11:00:00Z", ended_at="2026-07-20T11:00:05Z",
        )

        assert not result.validation.passed
        assert result.validation.failed_check == "schema"
        assert result.issue_count == 0
        rollup = json.loads(
            (lane_dir(repo_root, feature_id, lane_id) / REVIEW_DIR / REVIEW_REPORT_JSON).read_text()
        )
        assert rollup["validation"]["passed"] is False
        assert rollup["issues"] == []

    def test_audit_log_records_leg_lifecycle(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-review-audit")
        fake = _write_fake_claude_issues(tmp_path / "bin", _ISSUES_PAYLOAD)
        feature_id, lane_id, _ = _stage_implement_run(repo_root)

        result = run_reviewer_leg(
            repo_root, feature_id, lane_id, profile, claude_path=str(fake),
            started_at="2026-07-20T11:00:00Z", ended_at="2026-07-20T11:00:05Z",
        )

        root = _feature_root(repo_root, feature_id)
        records = json.loads((root / AUDIT_LOG_JSON).read_text())
        # Filter to the checking run's lifecycle (the staged implement run RUN-001
        # also has prepare_run/validate records, so filter by run id).
        checking = [
            str(r["event"])
            for r in records
            if r.get("payload", {}).get("run") == result.run_id
        ]
        # The v0.1 lifecycle (prepare_run -> run -> validate) for the checking
        # run, in order. No mark_task_proposed_done anywhere (checking legs write
        # no canonical status).
        assert checking.index("prepare_run") < checking.index("run")
        assert checking.index("run") < checking.index("validate")
        assert "mark_task_proposed_done" not in [str(r["event"]) for r in records]

    def test_fails_loud_when_no_implement_result(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-review-nopre")
        fake = _write_fake_claude_issues(tmp_path / "bin", _ISSUES_PAYLOAD)
        feature_id, lane_id = _seed_frozen_feature(repo_root)  # no implement run

        with pytest.raises(ValueError, match="implement-result"):
            run_reviewer_leg(
                repo_root, feature_id, lane_id, profile, claude_path=str(fake),
                started_at="2026-07-20T11:00:00Z", ended_at="2026-07-20T11:00:05Z",
            )


class TestRunSpecGapLeg:
    """``run_spec_gap_leg`` mirrors the reviewer leg for the gap role."""

    def test_passing_run_validates_and_rolls_up(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-gap")
        gap_payload = json.loads(json.dumps(_ISSUES_PAYLOAD))
        gap_payload["issues"][0]["source"] = "spec_gap"
        gap_payload["issues"][0]["title"] = "REQ-002 not implemented"
        gap_payload["issues"][0]["severity"] = "P1"
        fake = _write_fake_claude_issues(tmp_path / "bin", gap_payload)
        feature_id, lane_id, _ = _stage_implement_run(repo_root)
        _fill_specs(repo_root, feature_id)

        result = run_spec_gap_leg(
            repo_root, feature_id, lane_id, profile, claude_path=str(fake),
            started_at="2026-07-20T11:00:00Z", ended_at="2026-07-20T11:00:05Z",
        )

        assert result.role == "Spec Gap Analyst"
        assert result.source == "spec_gap"
        assert result.validation.passed
        assert result.issue_count == 1
        rollup = json.loads(
            (lane_dir(repo_root, feature_id, lane_id) / SPEC_GAP_DIR / SPEC_GAP_REPORT_JSON).read_text()
        )
        assert rollup["source"] == "spec_gap"
        assert rollup["issues"][0]["source"] == "spec_gap"
        assert rollup["issues"][0]["severity"] == "P1"


# ===========================================================================
# CLI: `ai-dev review <FEATURE> <LANE>` and `ai-dev spec-gap <FEATURE> <LANE>`.
# ===========================================================================


class TestCheckingCli:
    """The ``review`` and ``spec-gap`` console commands drive each leg end to
    end - argparse + dispatch + profile load + the leg - with the fake claude
    on PATH, mirroring the v0.1 / implementer CLI tests."""

    def test_review_pass_exit_zero_and_report(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        write_profiles(repo_root)
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-cli-review")
        fake_bin = _write_fake_claude_issues(tmp_path / "bin", _ISSUES_PAYLOAD)
        monkeypatch.setenv("PATH", f"{fake_bin.parent}{os.pathsep}{os.environ['PATH']}")
        feature_id, lane_id, _ = _stage_implement_run(repo_root)

        rc = main(
            ["review", feature_id, lane_id, "--profile", "cc-glm52",
             "--repo-root", str(repo_root)]
        )

        assert rc == 0
        out = capsys.readouterr().out
        assert "REVIEW PASS" in out
        assert "RUN-002" in out
        assert "issues=1" in out
        assert (lane_dir(repo_root, feature_id, lane_id) / REVIEW_DIR / REVIEW_REPORT_JSON).is_file()

    def test_spec_gap_pass_exit_zero_and_report(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        write_profiles(repo_root)
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-cli-gap")
        gap_payload = json.loads(json.dumps(_ISSUES_PAYLOAD))
        gap_payload["issues"][0]["source"] = "spec_gap"
        fake_bin = _write_fake_claude_issues(tmp_path / "bin", gap_payload)
        monkeypatch.setenv("PATH", f"{fake_bin.parent}{os.pathsep}{os.environ['PATH']}")
        feature_id, lane_id, _ = _stage_implement_run(repo_root)
        _fill_specs(repo_root, feature_id)

        rc = main(
            ["spec-gap", feature_id, lane_id, "--profile", "cc-glm52",
             "--repo-root", str(repo_root)]
        )

        assert rc == 0
        out = capsys.readouterr().out
        assert "SPEC-GAP PASS" in out
        assert (lane_dir(repo_root, feature_id, lane_id) / SPEC_GAP_DIR / SPEC_GAP_REPORT_JSON).is_file()

    def test_review_missing_implement_run_exits_one(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        write_profiles(repo_root)
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-cli-review-nopre")
        fake_bin = _write_fake_claude_issues(tmp_path / "bin", _ISSUES_PAYLOAD)
        monkeypatch.setenv("PATH", f"{fake_bin.parent}{os.pathsep}{os.environ['PATH']}")
        feature_id, lane_id = _seed_frozen_feature(repo_root)  # no implement run

        rc = main(["review", feature_id, lane_id, "--repo-root", str(repo_root)])

        assert rc == 1
        err = capsys.readouterr().err
        assert "implement-result" in err
