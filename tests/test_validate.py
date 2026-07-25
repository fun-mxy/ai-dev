"""validate - the §14 deterministic three-check validator (ticket 04).

``validate_run`` reads a captured run (result.json / metadata.json /
output-schema.json / allowed-files.txt) and decides PASS/FAIL across schema
(§14.1), file boundary (§14.2), and frozen artifact (§14.3). Schema validation
is hand-rolled (no jsonschema dep) over the project's controlled schema subset;
an unsupported asserting keyword fails loud (§24.2). ``validate_with_retry``
carries the §14.1/§24.3 retry-once semantics as a pure callable-injected seam.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Callable

import pytest

from ai_dev.audit import AUDIT_LOG_JSON
from ai_dev.feature_run import create_feature_run
from ai_dev.json_artifact import write_json
from ai_dev.paths import feature_dir, run_dir
from ai_dev.run_prepare import prepare_run
from ai_dev.run_wrapper import write_metadata
from ai_dev.status import freeze_artifact, frozen_artifacts_status
from ai_dev.templates import REQUIREMENTS_JSON
from ai_dev.validate import (
    RESULT_JSON,
    RETRYABLE_CODES,
    CheckName,
    SchemaValidatorError,
    Severity,
    ValidationIssue,
    ValidationResult,
    read_allowed_files,
    read_changed_files,
    read_run_role,
    validate_against_schema,
    validate_boundary,
    validate_frozen,
    validate_run,
    validate_schema,
    validate_traceability,
    validate_with_retry,
)

# A schema matching the project's ``_OUTPUT_SCHEMA`` shape (the $schema/title
# metadata keywords are omitted - they are ignored by the validator anyway).
_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["status", "summary", "tasks"],
    "additionalProperties": True,
    "properties": {
        "status": {"type": "string", "enum": ["proposed_done", "failed"]},
        "summary": {"type": "string", "minLength": 1},
        "tasks": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "status", "evidence"],
                "additionalProperties": True,
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "status": {
                        "type": "string",
                        "enum": ["proposed_done", "failed"],
                    },
                    "evidence": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}

_VALID_RESULT: dict[str, object] = {
    "status": "proposed_done",
    "summary": "Wrote workspace/hello.py for the run.",
    "tasks": [
        {
            "id": "TASK-001",
            "status": "proposed_done",
            "evidence": ["workspace/hello.py"],
        }
    ],
}


# ---------------------------------------------------------------------------
# Test helpers.
# ---------------------------------------------------------------------------


def _make_run(
    repo_root: Path,
    role: str = "Implementer",
    task: str = "Create workspace/hello.py.",
) -> tuple[str, str]:
    """Create a feature run + prepare RUN-001; return (feature_id, run_id)."""
    feature_id = create_feature_run(repo_root, "de-risk the validator")
    run_id = prepare_run(repo_root, feature_id, role, task)
    return feature_id, run_id


def _run_root(repo_root: Path, feature_id: str, run_id: str) -> Path:
    return run_dir(repo_root, feature_id, run_id)


def _write_result(
    repo_root: Path, feature_id: str, run_id: str, result: object
) -> None:
    path = _run_root(repo_root, feature_id, run_id) / "output" / RESULT_JSON
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        result if isinstance(result, str) else json.dumps(result, indent=2) + "\n"
    )


def _write_metadata_with_profile(
    repo_root: Path,
    feature_id: str,
    run_id: str,
    changed_files: list[str],
    write_profiles: Callable[..., Path],
) -> None:
    """Write metadata.json via the real writer so its shape stays canonical."""
    write_profiles(repo_root)
    from ai_dev.profiles import load_profile

    profile = load_profile(repo_root, "cc-glm52")
    write_metadata(
        _run_root(repo_root, feature_id, run_id) / "output" / "metadata.json",
        run_id=run_id,
        profile=profile,
        started_at="2026-07-19T10:00:00Z",
        ended_at="2026-07-19T10:00:05Z",
        exit_code=0,
        changed_files=changed_files,
    )


def _allow(
    repo_root: Path, feature_id: str, run_id: str, *paths: str
) -> None:
    """Append ``paths`` to the run's allowed-files.txt (for boundary isolation)."""
    allowed_path = (
        _run_root(repo_root, feature_id, run_id) / "input" / "allowed-files.txt"
    )
    with allowed_path.open("a") as f:
        for p in paths:
            f.write(p + "\n")


def _audit_records(repo_root: Path, feature_id: str) -> list[dict[str, object]]:
    log = repo_root / ".ai-dev" / "features" / feature_id / AUDIT_LOG_JSON
    return json.loads(log.read_text())


# ---------------------------------------------------------------------------
# validate_against_schema - the hand-rolled JSON Schema subset validator.
# ---------------------------------------------------------------------------


class TestValidateAgainstSchema:
    """The validator covers the project schema's subset; fail-loud on the rest."""

    def test_valid_result_yields_no_errors(self) -> None:
        assert validate_against_schema(_VALID_RESULT, _SCHEMA) == []

    def test_missing_required_field(self) -> None:
        result = {"summary": "x", "tasks": []}
        errors = validate_against_schema(result, _SCHEMA)
        assert any("missing required property 'status'" in e for e in errors)

    def test_status_not_in_enum(self) -> None:
        result = dict(_VALID_RESULT, status="done")
        errors = validate_against_schema(result, _SCHEMA)
        assert any("'done' not in enum" in e for e in errors)

    def test_summary_empty_string_fails_min_length(self) -> None:
        result = dict(_VALID_RESULT, summary="")
        errors = validate_against_schema(result, _SCHEMA)
        assert any("minLength" in e for e in errors)

    def test_tasks_empty_array_fails_min_items(self) -> None:
        result = dict(_VALID_RESULT, tasks=[])
        errors = validate_against_schema(result, _SCHEMA)
        assert any("minItems" in e for e in errors)

    def test_task_item_missing_required(self) -> None:
        result = {
            "status": "proposed_done",
            "summary": "x",
            "tasks": [{"status": "proposed_done", "evidence": []}],
        }
        errors = validate_against_schema(result, _SCHEMA)
        assert any("missing required property 'id'" in e for e in errors)

    def test_task_item_status_not_in_enum(self) -> None:
        result = {
            "status": "proposed_done",
            "summary": "x",
            "tasks": [
                {"id": "TASK-001", "status": "done", "evidence": ["x"]}
            ],
        }
        errors = validate_against_schema(result, _SCHEMA)
        assert any("'done' not in enum" in e for e in errors)

    def test_wrong_type_for_status(self) -> None:
        result = dict(_VALID_RESULT, status=42)
        errors = validate_against_schema(result, _SCHEMA)
        assert any("expected type string" in e and "status" in e for e in errors)

    def test_additional_properties_false_rejects_extras(self) -> None:
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "additionalProperties": False,
        }
        errors = validate_against_schema({"a": "x", "b": "y"}, schema)
        assert any("additional property 'b'" in e for e in errors)

    def test_additional_properties_true_allows_extras(self) -> None:
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "additionalProperties": True,
        }
        assert validate_against_schema({"a": "x", "b": "y"}, schema) == []

    def test_nested_path_in_error_messages(self) -> None:
        result = {
            "status": "proposed_done",
            "summary": "x",
            "tasks": [
                {"id": "", "status": "proposed_done", "evidence": []}
            ],
        }
        errors = validate_against_schema(result, _SCHEMA)
        assert any(".tasks[0].id" in e for e in errors)

    def test_fail_loud_on_unsupported_keyword(self) -> None:
        # §24.2: an asserting keyword the validator cannot check is rejected,
        # not silently ignored (which would weaken validation).
        schema = {"type": "string", "pattern": "^[a-z]+$"}
        with pytest.raises(SchemaValidatorError, match="pattern"):
            validate_against_schema("abc", schema)

    def test_fail_loud_on_unsupported_type(self) -> None:
        schema = {"type": "frobnicate"}
        with pytest.raises(SchemaValidatorError, match="unsupported schema type"):
            validate_against_schema("abc", schema)

    def test_ignores_metadata_keywords(self) -> None:
        # $schema / title / description are non-asserting metadata: their
        # presence must not trip the fail-loud guard or affect validity.
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "T",
            "description": "D",
            "type": "object",
            "required": ["a"],
        }
        assert validate_against_schema({"a": 1}, schema) == []
        errors = validate_against_schema({}, schema)
        assert any("missing required property 'a'" in e for e in errors)

    def test_union_type_list(self) -> None:
        # ``type`` as a list (union) is supported.
        schema = {"type": ["string", "null"]}
        assert validate_against_schema("x", schema) == []
        assert validate_against_schema(None, schema) == []
        errors = validate_against_schema(42, schema)
        assert len(errors) == 1


# ---------------------------------------------------------------------------
# validate_schema - §14.1 against on-disk files (retryable vs not).
# ---------------------------------------------------------------------------


class TestValidateSchema:
    """§14.1: result.json exists / valid JSON / conforms to schema."""

    def test_valid_result_no_issues(
        self, repo_root: Path, tmp_path: Path
    ) -> None:
        result_path = tmp_path / "result.json"
        schema_path = tmp_path / "output-schema.json"
        result_path.write_text(json.dumps(_VALID_RESULT))
        schema_path.write_text(json.dumps(_SCHEMA))

        assert validate_schema(result_path, schema_path) == []

    def test_result_missing_is_retryable(self, tmp_path: Path) -> None:
        issues = validate_schema(tmp_path / "missing.json", tmp_path / "schema.json")
        assert len(issues) == 1
        assert issues[0].code == "result_missing"
        assert issues[0].retryable is True

    def test_result_invalid_json_is_retryable(self, tmp_path: Path) -> None:
        result_path = tmp_path / "result.json"
        schema_path = tmp_path / "output-schema.json"
        result_path.write_text("{not valid json")
        schema_path.write_text(json.dumps(_SCHEMA))

        issues = validate_schema(result_path, schema_path)
        assert len(issues) == 1
        assert issues[0].code == "result_invalid_json"
        assert issues[0].retryable is True

    def test_schema_violation_is_retryable(self, tmp_path: Path) -> None:
        result_path = tmp_path / "result.json"
        schema_path = tmp_path / "output-schema.json"
        result_path.write_text(json.dumps(dict(_VALID_RESULT, status="done")))
        schema_path.write_text(json.dumps(_SCHEMA))

        issues = validate_schema(result_path, schema_path)
        assert issues
        assert all(i.code == "schema_violation" for i in issues)
        assert all(i.retryable for i in issues)

    def test_schema_file_missing_not_retryable(self, tmp_path: Path) -> None:
        result_path = tmp_path / "result.json"
        result_path.write_text(json.dumps(_VALID_RESULT))

        issues = validate_schema(result_path, tmp_path / "missing-schema.json")
        assert len(issues) == 1
        assert issues[0].code == "schema_file_missing"
        assert issues[0].retryable is False

    def test_schema_file_unsupported_keyword_not_retryable(
        self, tmp_path: Path
    ) -> None:
        # A schema using an unsupported keyword is a config error (§24.2) -
        # surfaced as a non-retryable schema_file_invalid issue, not a crash.
        result_path = tmp_path / "result.json"
        schema_path = tmp_path / "output-schema.json"
        result_path.write_text(json.dumps(_VALID_RESULT))
        schema_path.write_text(json.dumps({"type": "string", "pattern": "^x$"}))

        issues = validate_schema(result_path, schema_path)
        assert len(issues) == 1
        assert issues[0].code == "schema_file_invalid"
        assert issues[0].retryable is False


# ---------------------------------------------------------------------------
# validate_boundary - §14.2.
# ---------------------------------------------------------------------------


class TestValidateBoundary:
    """§14.2: changed_files must all be within allowed-files.txt."""

    def test_all_in_allowed_no_issues(self) -> None:
        allowed = {"output/result.json", "output/result.md"}
        changed = ["output/result.json", "output/result.md"]

        assert validate_boundary(changed, allowed) == []

    def test_out_of_bounds_is_p0_violation(self) -> None:
        allowed = {"output/result.json"}
        changed = ["output/result.json", "workspace/hello.py"]

        issues = validate_boundary(changed, allowed)
        assert len(issues) == 1
        assert issues[0].code == "boundary_violation"
        assert issues[0].severity == "P0"
        assert issues[0].path == "workspace/hello.py"
        assert issues[0].retryable is False

    def test_metadata_missing_is_failure(self) -> None:
        # changed_files is None -> metadata missing/invalid -> boundary cannot
        # be checked, which is itself a P0 failure.
        issues = validate_boundary(None, set())
        assert len(issues) == 1
        assert issues[0].code == "metadata_missing"
        assert issues[0].severity == "P0"
        assert issues[0].retryable is False


class TestReadAllowedFiles:
    """``allowed-files.txt`` parsing: comments + blanks ignored."""

    def test_strips_comments_and_blanks(self, tmp_path: Path) -> None:
        path = tmp_path / "allowed-files.txt"
        path.write_text(
            "# comment line\n"
            "\n"
            "output/result.json\n"
            "  output/result.md  \n"
            "# another comment\n"
            "workspace/hello.py\n"
        )
        assert read_allowed_files(path) == {
            "output/result.json",
            "output/result.md",
            "workspace/hello.py",
        }

    def test_missing_file_yields_empty_set(self, tmp_path: Path) -> None:
        # Missing allowed-files -> empty set -> every changed file is a
        # violation (fail loud, not silent pass).
        assert read_allowed_files(tmp_path / "nope.txt") == set()


class TestReadChangedFiles:
    """``metadata.json`` -> changed_files extraction."""

    def test_reads_changed_files(self, tmp_path: Path) -> None:
        path = tmp_path / "metadata.json"
        path.write_text(json.dumps({"changed_files": ["a.py", "b.py"]}))

        changed = read_changed_files(path)
        assert changed == ["a.py", "b.py"]

    def test_missing_metadata(self, tmp_path: Path) -> None:
        assert read_changed_files(tmp_path / "nope.json") is None

    def test_invalid_json_metadata(self, tmp_path: Path) -> None:
        path = tmp_path / "metadata.json"
        path.write_text("{broken")
        assert read_changed_files(path) is None

    def test_no_changed_files_field(self, tmp_path: Path) -> None:
        path = tmp_path / "metadata.json"
        path.write_text(json.dumps({"run_id": "RUN-001"}))
        assert read_changed_files(path) is None


# ---------------------------------------------------------------------------
# validate_frozen - §14.3 (the seam: must not false-positive in v0.1).
# ---------------------------------------------------------------------------


class TestValidateFrozen:
    """§14.3: touching a *frozen* artifact fails; v0.1 must not false-positive."""

    def test_run_internal_changes_no_false_positive_when_all_frozen(
        self, repo_root: Path
    ) -> None:
        # Even with every artifact frozen, RUN-internal changed files never
        # resolve onto a feature-root frozen file -> no frozen issue.
        feature_id, run_id = _make_run(repo_root)
        feature_root = feature_dir(repo_root, feature_id)
        rroot = _run_root(repo_root, feature_id, run_id)
        for artifact in ("requirements", "design", "tasks", "lane_graph"):
            freeze_artifact(feature_root, artifact)
        frozen_status = frozen_artifacts_status(feature_root)
        assert all(frozen_status.values())

        issues = validate_frozen(
            ["output/result.json", "workspace/hello.py"],
            feature_root,
            rroot,
            frozen_status,
        )
        assert issues == []

    def test_touch_frozen_artifact_via_parent_path_fails(
        self, repo_root: Path
    ) -> None:
        # A changed path that climbs out of the run dir (../../) and lands on a
        # frozen artifact file is a §14.3 violation. v0.1's wrapper never
        # produces such a path, but the seam must catch it when present.
        feature_id, run_id = _make_run(repo_root)
        feature_root = feature_dir(repo_root, feature_id)
        rroot = _run_root(repo_root, feature_id, run_id)
        freeze_artifact(feature_root, "requirements")

        issues = validate_frozen(
            ["../../01-requirements.md"],
            feature_root,
            rroot,
            frozen_artifacts_status(feature_root),
        )
        assert len(issues) == 1
        assert issues[0].code == "frozen_violation"
        assert issues[0].severity == "P0"
        assert issues[0].requires_change_proposal is True
        assert issues[0].path == "../../01-requirements.md"

    def test_touch_artifact_when_not_frozen_no_frozen_issue(
        self, repo_root: Path
    ) -> None:
        # Touching the requirements file when requirements is NOT frozen is not
        # a §14.3 violation (it is a boundary issue, tested elsewhere). The
        # frozen check must stay silent here.
        feature_id, run_id = _make_run(repo_root)
        feature_root = feature_dir(repo_root, feature_id)
        rroot = _run_root(repo_root, feature_id, run_id)

        issues = validate_frozen(
            ["../../01-requirements.md"],
            feature_root,
            rroot,
            frozen_artifacts_status(feature_root),  # nothing frozen
        )
        assert issues == []

    def test_nothing_frozen_no_issues(self, repo_root: Path) -> None:
        feature_id, run_id = _make_run(repo_root)
        feature_root = feature_dir(repo_root, feature_id)
        rroot = _run_root(repo_root, feature_id, run_id)

        issues = validate_frozen(
            ["../../01-requirements.md", "../../02-design.json"],
            feature_root,
            rroot,
            {},  # nothing frozen
        )
        assert issues == []


class TestFrozenArtifactsStatus:
    """``frozen_artifacts_status`` fails loud on a broken feature-status.yml.

    §24.2: a real feature run always has a valid status file (``create-feature-run``
    writes it, ``freeze_artifact`` mutates it), so a missing/malformed file is
    corruption the caller must surface - not silently treat as "nothing frozen".
    """

    def test_returns_frozen_map_for_real_feature_run(self, repo_root: Path) -> None:
        feature_id, _ = _make_run(repo_root)
        feature_root = feature_dir(repo_root, feature_id)

        status = frozen_artifacts_status(feature_root)

        assert set(status) == {"requirements", "design", "tasks", "lane_graph"}
        assert all(v is False for v in status.values())

    def test_missing_status_file_raises(self, repo_root: Path) -> None:
        feature_id, _ = _make_run(repo_root)
        feature_root = feature_dir(repo_root, feature_id)
        (feature_root / "status" / "feature-status.yml").unlink()

        with pytest.raises(ValueError, match="feature-status.yml missing"):
            frozen_artifacts_status(feature_root)

    def test_malformed_status_file_raises(self, repo_root: Path) -> None:
        feature_id, _ = _make_run(repo_root)
        feature_root = feature_dir(repo_root, feature_id)
        # An unclosed flow sequence is unparseable YAML -> YAMLError, caught
        # and re-raised as ValueError so the CLI surfaces a clean error.
        (feature_root / "status" / "feature-status.yml").write_text(
            "feature: [unclosed\n"
        )

        with pytest.raises(ValueError, match="not valid YAML"):
            frozen_artifacts_status(feature_root)


# ---------------------------------------------------------------------------
# validate_run - end-to-end pure validator.
# ---------------------------------------------------------------------------


class TestValidateRun:
    """``validate_run`` runs all three checks and returns the verdict."""

    def test_well_formed_run_passes(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
    ) -> None:
        feature_id, run_id = _make_run(repo_root)
        _write_result(repo_root, feature_id, run_id, _VALID_RESULT)
        _write_metadata_with_profile(
            repo_root, feature_id, run_id,
            ["output/result.json", "output/result.md"], write_profiles,
        )

        result = validate_run(repo_root, feature_id, run_id)

        assert result.passed is True
        assert result.failed_check is None
        assert result.issues == []
        assert result.attempt == 1

    def test_schema_broken_fails_retryable(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
    ) -> None:
        feature_id, run_id = _make_run(repo_root)
        _write_result(repo_root, feature_id, run_id, dict(_VALID_RESULT, status="done"))
        _write_metadata_with_profile(
            repo_root, feature_id, run_id,
            ["output/result.json", "output/result.md"], write_profiles,
        )

        result = validate_run(repo_root, feature_id, run_id)

        assert result.passed is False
        assert result.failed_check == "schema"
        assert result.is_retryable is True

    def test_boundary_violation_fails_not_retryable(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
    ) -> None:
        feature_id, run_id = _make_run(repo_root)
        _write_result(repo_root, feature_id, run_id, _VALID_RESULT)
        # workspace/hello.py is not in the seeded allowed-files -> boundary breach.
        _write_metadata_with_profile(
            repo_root, feature_id, run_id,
            ["output/result.json", "workspace/hello.py"], write_profiles,
        )

        result = validate_run(repo_root, feature_id, run_id)

        assert result.passed is False
        assert result.failed_check == "boundary"
        assert result.is_retryable is False

    def test_frozen_violation_outranks_boundary(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
    ) -> None:
        # A frozen-artifact touch is also a boundary breach (the path is not in
        # allowed-files); the frozen check is more severe, so failed_check is
        # "frozen" and the run is not retryable.
        feature_id, run_id = _make_run(repo_root)
        feature_root = feature_dir(repo_root, feature_id)
        freeze_artifact(feature_root, "requirements")
        _write_result(repo_root, feature_id, run_id, _VALID_RESULT)
        _write_metadata_with_profile(
            repo_root, feature_id, run_id,
            ["../../01-requirements.md"], write_profiles,
        )

        result = validate_run(repo_root, feature_id, run_id)

        assert result.passed is False
        assert result.failed_check == "frozen"
        assert result.is_retryable is False
        assert any(i.code == "frozen_violation" for i in result.issues)

    def test_missing_run_dir_raises(self, repo_root: Path) -> None:
        # §24.2 fail loud: no run directory -> ValueError, not a silent PASS.
        feature_id = create_feature_run(repo_root, "intent")
        with pytest.raises(ValueError, match="RUN-999"):
            validate_run(repo_root, feature_id, "RUN-999")

    def test_audits_validate_event(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
    ) -> None:
        # §2.1: a validate lifecycle event flows through the audit log.
        feature_id, run_id = _make_run(repo_root)
        _write_result(repo_root, feature_id, run_id, _VALID_RESULT)
        _write_metadata_with_profile(
            repo_root, feature_id, run_id,
            ["output/result.json", "output/result.md"], write_profiles,
        )

        validate_run(repo_root, feature_id, run_id)

        records = _audit_records(repo_root, feature_id)
        validate_events = [r for r in records if r.get("event") == "validate"]
        assert len(validate_events) == 1
        payload = validate_events[0]["payload"]
        assert isinstance(payload, dict)
        assert payload.get("run") == run_id
        assert payload.get("attempt") == 1
        assert payload.get("passed") is True
        assert payload.get("failed_check") is None

    def test_audit_records_attempt_distinguishes_retry(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
    ) -> None:
        # §14.1/§24.3: a retry leaves two audit records carrying attempt=1 then
        # attempt=2, so the log can distinguish attempt-1-failed from
        # attempt-2-passed (rather than two identical-looking validations).
        feature_id, run_id = _make_run(repo_root)
        _write_result(repo_root, feature_id, run_id, dict(_VALID_RESULT, status="done"))
        _write_metadata_with_profile(
            repo_root, feature_id, run_id,
            ["output/result.json", "output/result.md"], write_profiles,
        )

        def validate(attempt: int) -> ValidationResult:
            return validate_run(repo_root, feature_id, run_id, attempt=attempt)

        def rerun() -> None:
            _write_result(repo_root, feature_id, run_id, _VALID_RESULT)

        result = validate_with_retry(validate, rerun)
        assert result.passed is True
        assert result.attempt == 2

        records = _audit_records(repo_root, feature_id)
        attempts = [
            r["payload"]["attempt"]
            for r in records
            if r.get("event") == "validate"
        ]
        assert attempts == [1, 2]
        passed = [
            r["payload"]["passed"]
            for r in records
            if r.get("event") == "validate"
        ]
        assert passed == [False, True]


# ---------------------------------------------------------------------------
# §14.4 traceability-declaration validation (ADR-0007 D2).
# ---------------------------------------------------------------------------


def _seed_requirements(
    repo_root: Path, feature_id: str, *, reqs: list[str], acs: list[str]
) -> None:
    """Overwrite 01-requirements.json with the given REQ/AC ids (frozen)."""
    root = feature_dir(repo_root, feature_id)
    write_json(
        root / REQUIREMENTS_JSON,
        {
            "feature": feature_id,
            "frozen": True,
            "requirements": [{"id": r, "statement": f"{r} statement"} for r in reqs],
            "acceptance_criteria": [
                {"id": a, "statement": f"{a} statement"} for a in acs
            ],
            "priority": None,
            "scope": None,
            "constraints": [],
            "open_questions": [],
        },
    )


def _declaring_result() -> dict[str, object]:
    """A valid result.json that declares REQ-001 / AC-001."""
    return dict(
        _VALID_RESULT,
        related_requirements=["REQ-001"],
        related_acceptance_criteria=["AC-001"],
    )


class TestValidateTraceabilityUnit:
    """``validate_traceability`` + ``read_run_role`` - the §14.4 seam in isolation."""

    def test_read_run_role_parses_implementer(self, repo_root: Path) -> None:
        feature_id, run_id = _make_run(repo_root)
        assert read_run_role(run_dir(repo_root, feature_id, run_id)) == "Implementer"

    def test_read_run_role_none_when_role_md_missing(self, tmp_path: Path) -> None:
        assert read_run_role(tmp_path) is None

    def test_non_implementer_role_is_skipped(self) -> None:
        # A Code Reviewer run never declares coverage; the check is a no-op.
        issues = validate_traceability(
            {"related_requirements": []}, "Code Reviewer", ["REQ-001"], ["AC-001"]
        )
        assert issues == []

    def test_empty_spec_is_noop(self) -> None:
        # No REQ/AC allocated -> nothing to cover -> the check does not fire even
        # for an implementer that declares nothing (matches the "honestly empty
        # when there is no spec" baseline, ADR-0007).
        assert validate_traceability({}, "Implementer", [], []) == []

    def test_missing_declaration_fails(self) -> None:
        issues = validate_traceability({}, "Implementer", ["REQ-001"], ["AC-001"])
        assert len(issues) == 2  # both fields required when both id-sets exist
        assert {i.code for i in issues} == {"traceability_missing"}
        assert all(i.check == "traceability" and i.severity == "P1" for i in issues)

    def test_unknown_id_fails(self) -> None:
        result = {
            "related_requirements": ["REQ-999"],  # not allocated
            "related_acceptance_criteria": ["AC-001"],
        }
        issues = validate_traceability(result, "Implementer", ["REQ-001"], ["AC-001"])
        assert len(issues) == 1
        assert issues[0].code == "traceability_unknown_id"
        assert "REQ-999" in issues[0].message

    def test_malformed_non_list_fails(self) -> None:
        result = {"related_requirements": "REQ-001", "related_acceptance_criteria": []}
        issues = validate_traceability(result, "Implementer", ["REQ-001"], ["AC-001"])
        assert any(i.code == "traceability_malformed" for i in issues)

    def test_partial_scope_declaration_passes(self) -> None:
        # ADR-0007 D2: a lane declares only its own subset, not every req. With
        # REQ-001/REQ-002 allocated, declaring just REQ-001 is well-formed.
        result = {"related_requirements": ["REQ-001"], "related_acceptance_criteria": []}
        assert (
            validate_traceability(result, "Implementer", ["REQ-001", "REQ-002"], ["AC-001"])
            == []
        )

    def test_reqs_without_acs_do_not_require_ac_field(self) -> None:
        # Only AC-001 allocated, no REQs: related_requirements is not required.
        result = {"related_acceptance_criteria": ["AC-001"]}
        assert validate_traceability(result, "Implementer", [], ["AC-001"]) == []


class TestValidateRunTraceability:
    """``validate_run`` enforces §14.4 end-to-end on an implementer run."""

    def test_declaring_run_passes_when_spec_allocated(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
    ) -> None:
        feature_id, run_id = _make_run(repo_root)
        _seed_requirements(repo_root, feature_id, reqs=["REQ-001"], acs=["AC-001"])
        _write_result(repo_root, feature_id, run_id, _declaring_result())
        _write_metadata_with_profile(
            repo_root, feature_id, run_id,
            ["output/result.json", "output/result.md"], write_profiles,
        )

        result = validate_run(repo_root, feature_id, run_id)

        assert result.passed is True
        assert result.failed_check is None

    def test_non_declaring_run_fails_when_spec_allocated(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
    ) -> None:
        # The implementer was required to declare but result.json omits both
        # fields -> §14.4 fails the run (ADR-0007 D2).
        feature_id, run_id = _make_run(repo_root)
        _seed_requirements(repo_root, feature_id, reqs=["REQ-001"], acs=["AC-001"])
        _write_result(repo_root, feature_id, run_id, _VALID_RESULT)  # no declaration
        _write_metadata_with_profile(
            repo_root, feature_id, run_id,
            ["output/result.json", "output/result.md"], write_profiles,
        )

        result = validate_run(repo_root, feature_id, run_id)

        assert result.passed is False
        assert result.failed_check == "traceability"
        assert result.is_retryable is False
        codes = {i.code for i in result.issues}
        assert codes == {"traceability_missing"}

    def test_empty_spec_run_passes_without_declaration(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
    ) -> None:
        # No REQ/AC allocated (the seeded de-risking feature) -> §14.4 is a
        # no-op, so a result.json without the declaration still validates. This
        # is what keeps the v0.1-era de-risking fixtures green.
        feature_id, run_id = _make_run(repo_root)
        _write_result(repo_root, feature_id, run_id, _VALID_RESULT)
        _write_metadata_with_profile(
            repo_root, feature_id, run_id,
            ["output/result.json", "output/result.md"], write_profiles,
        )

        result = validate_run(repo_root, feature_id, run_id)
        assert result.passed is True

    def test_non_implementer_run_not_checked(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
    ) -> None:
        # A Code Reviewer run over an allocated spec: the §13 issues-schema
        # result.json carries no declaration and must NOT trip §14.4.
        feature_id, run_id = _make_run(repo_root, role="Code Reviewer")
        _seed_requirements(repo_root, feature_id, reqs=["REQ-001"], acs=["AC-001"])
        # The reviewer's result.json is an issues payload, not a task payload;
        # it still passes the (role-agnostic) schema check because
        # additionalProperties is true, and §14.4 skips non-implementer roles.
        _write_result(
            repo_root, feature_id, run_id, {"status": "proposed_done",
                                            "summary": "reviewed", "tasks": [
                                                {"id": "TASK-001",
                                                 "status": "proposed_done",
                                                 "evidence": []}]}
        )
        _write_metadata_with_profile(
            repo_root, feature_id, run_id,
            ["output/result.json", "output/result.md"], write_profiles,
        )

        result = validate_run(repo_root, feature_id, run_id)
        assert result.passed is True

    def test_corrupt_requirements_doc_fails_loud(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
    ) -> None:
        # §24.2: a present-but-corrupt 01-requirements.json is not a silent
        # "empty spec" -> it fails loud for an implementer run (the honesty gate
        # must not be bypassed by corruption).
        feature_id, run_id = _make_run(repo_root)
        req_path = feature_dir(repo_root, feature_id) / REQUIREMENTS_JSON
        req_path.write_text("{ not valid json")
        _write_result(repo_root, feature_id, run_id, _VALID_RESULT)
        _write_metadata_with_profile(
            repo_root, feature_id, run_id,
            ["output/result.json", "output/result.md"], write_profiles,
        )

        result = validate_run(repo_root, feature_id, run_id)

        assert result.passed is False
        assert any(i.code == "traceability_requirements_corrupt" for i in result.issues)


# ---------------------------------------------------------------------------
# validate_with_retry - §14.1/§24.3 retry-once seam.
# ---------------------------------------------------------------------------


def _passing() -> ValidationResult:
    return ValidationResult(run_id="RUN-001", issues=[])


def _failing(check: CheckName, code: str) -> ValidationResult:
    severity: Severity = "P1" if check == "schema" else "P0"
    return ValidationResult(
        run_id="RUN-001",
        issues=[
            ValidationIssue(
                check=check, code=code, message="x", severity=severity
            )
        ],
    )


class _CountingRerun:
    """A rerun callable that counts how many times it was invoked."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


class _SeqValidate:
    """A validate callable that returns a fixed sequence of results.

    Applies the ``attempt`` argument onto each returned result (the way the real
    ``validate_run(..., attempt=n)`` would), so retry tests can assert the
    final attempt number without constructing two distinct result objects.
    """

    def __init__(self, results: list[ValidationResult]) -> None:
        self._results = list(results)
        self.calls = 0

    def __call__(self, attempt: int) -> ValidationResult:
        result = self._results[self.calls]
        self.calls += 1
        return replace(result, attempt=attempt)


class TestValidateWithRetry:
    """§14.1/§24.3: retry once only on a schema/output-format failure."""

    def test_pass_first_try_no_rerun(self) -> None:
        validate = _SeqValidate([_passing()])
        rerun = _CountingRerun()

        result = validate_with_retry(validate, rerun)

        assert result.passed is True
        assert result.attempt == 1
        assert rerun.calls == 0
        assert validate.calls == 1

    def test_retryable_then_pass(self) -> None:
        # First validation fails with a schema violation (retryable); the rerun
        # fixes it; the second validation passes. Attempt becomes 2.
        validate = _SeqValidate([_failing("schema", "schema_violation"), _passing()])
        rerun = _CountingRerun()

        result = validate_with_retry(validate, rerun)

        assert result.passed is True
        assert result.attempt == 2
        assert rerun.calls == 1
        assert validate.calls == 2

    def test_retryable_then_fail_rerun_once(self) -> None:
        # Both attempts fail with a schema violation -> rerun exactly once (not
        # twice), attempt 2, still failed, still retryable-in-spirit.
        validate = _SeqValidate(
            [_failing("schema", "schema_violation"), _failing("schema", "result_missing")]
        )
        rerun = _CountingRerun()

        result = validate_with_retry(validate, rerun)

        assert result.passed is False
        assert result.attempt == 2
        assert rerun.calls == 1
        assert validate.calls == 2

    def test_non_retryable_no_rerun(self) -> None:
        # A boundary breach is not retryable -> no rerun, attempt 1.
        validate = _SeqValidate([_failing("boundary", "boundary_violation")])
        rerun = _CountingRerun()

        result = validate_with_retry(validate, rerun)

        assert result.passed is False
        assert result.attempt == 1
        assert result.is_retryable is False
        assert rerun.calls == 0

    def test_retryable_codes_set_pins_section_24_3(self) -> None:
        # §24.3 lists exactly: missing result.json / malformed result.json /
        # schema violation. Pin the set so a refactor cannot widen it silently.
        assert RETRYABLE_CODES == frozenset(
            {"result_missing", "result_invalid_json", "schema_violation"}
        )

    def test_retry_with_real_validate_run_and_rerun_that_fixes_result(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
    ) -> None:
        # End-to-end against real files: validate_run sees a schema-broken
        # result.json (retryable); the rerun callable rewrites result.json to a
        # valid one; the second validate_run passes. Demonstrates the retry seam
        # works with real validation, not just fake callables.
        feature_id, run_id = _make_run(repo_root)
        _write_result(repo_root, feature_id, run_id, dict(_VALID_RESULT, status="done"))
        _write_metadata_with_profile(
            repo_root, feature_id, run_id,
            ["output/result.json", "output/result.md"], write_profiles,
        )

        def validate(attempt: int) -> ValidationResult:
            return validate_run(repo_root, feature_id, run_id, attempt=attempt)

        fixed = {"called": False}

        def rerun() -> None:
            fixed["called"] = True
            _write_result(repo_root, feature_id, run_id, _VALID_RESULT)

        result = validate_with_retry(validate, rerun)

        assert result.passed is True
        assert result.attempt == 2
        assert fixed["called"] is True

