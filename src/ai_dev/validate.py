"""Deterministic three-check run validation (ticket 04, spec §14).

After every run the wrapper must execute three deterministic checks - schema
(§14.1), file boundary (§14.2), and frozen artifact (§14.3) - and decide
PASS/FAIL. This module is that decision: a pure, model-free reader of the
captured run artifacts (``output/result.json``, ``output/metadata.json``,
``input/output-schema.json``, ``input/allowed-files.txt``) plus the feature
run's frozen status. The prototype ``prototype/adapter/validate.py`` is the
seed; this ports its schema + boundary checks into the typed data plane and
adds the §14.3 frozen seam the prototype skipped.

Scope split with ``run_wrapper`` (ticket 03): the wrapper *captures* a run; it
does not *judge* it. Judgement lives here. ``validate_run`` reads what the
wrapper wrote and returns a ``ValidationResult`` - it spawns no subprocess and
mutates no canonical state (it appends one ``validate`` audit record, like every
other lifecycle op). The §14.1/§24.3 retry-once is NOT inside ``validate_run``;
it lives in ``validate_with_retry``, a pure seam that composes a validate
callable with a rerun callable so the retry semantics are unit-testable without
a real ``claude``.

Schema validation is hand-rolled (no ``jsonschema`` dependency), covering the
JSON Schema subset the project's ``output-schema.json`` uses (``type`` /
``required`` / ``enum`` / ``minLength`` / ``minItems`` / ``properties`` /
``items`` / ``additionalProperties`` plus a few common bounds). An asserting
keyword the validator does not support is rejected loudly (``SchemaValidatorError``,
§24.2) rather than silently ignored - a schema that grows beyond the supported
subset is caught, not quietly weakened.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from ai_dev.audit import append_audit_event
from ai_dev.paths import (
    INPUT_DIR,
    METADATA_JSON,
    OUTPUT_DIR,
    RESULT_JSON,
    feature_dir,
    run_dir,
)
from ai_dev.run_prepare import ALLOWED_FILES_FILE, OUTPUT_SCHEMA_FILE
from ai_dev.status import frozen_artifacts_status
from ai_dev.templates import (
    DESIGN_JSON,
    DESIGN_MD,
    LANE_GRAPH_YML,
    REQUIREMENTS_JSON,
    REQUIREMENTS_MD,
    TASKS_MD,
)

# The three §14 checks, as a closed set for static typing and for the
# ``failed_check`` priority ordering. A ``CheckName`` literal means mypy catches
# a typo at the call site (§24.2 fail-loud at type-check time) rather than
# silently constructing a bogus issue.
CheckName = Literal["schema", "boundary", "frozen"]
Severity = Literal["P0", "P1", "P2", "P3"]

# Severity-priority order for ``ValidationResult.failed_check`` (frozen outranks
# boundary outranks schema, matching §15.1).
_CHECKS_BY_PRIORITY: tuple[CheckName, ...] = ("frozen", "boundary", "schema")

# §24.3: only schema / output-format failures may auto-retry once. These are the
# agent-fixable schema failures (the agent can re-emit result.json correctly);
# a missing ``output-schema.json`` or a boundary/frozen breach is a config or
# contract failure retrying will not fix, so they are NOT retryable.
RETRYABLE_CODES: frozenset[str] = frozenset(
    {"result_missing", "result_invalid_json", "schema_violation"}
)

# §4.2 frozen artifact name -> the feature-root files that constitute it. Built
# from the ``templates`` filename constants so the §14.3 path set cannot drift
# from the §7 seeder. A changed file that resolves onto one of these (AND whose
# artifact is currently frozen) is a §14.3 violation.
_FROZEN_ARTIFACT_FILES: Mapping[str, tuple[str, ...]] = {
    "requirements": (REQUIREMENTS_MD, REQUIREMENTS_JSON),
    "design": (DESIGN_MD, DESIGN_JSON),
    "tasks": (TASKS_MD,),
    "lane_graph": (LANE_GRAPH_YML,),
}


class SchemaValidatorError(ValueError):
    """``output-schema.json`` uses a keyword/type this validator cannot check.

    Fail-loud (§24.2): an asserting keyword the hand-rolled validator does not
    support is rejected rather than silently ignored, so a schema that grows
    beyond the supported subset is caught at validation time instead of quietly
    weakening the §14.1 check. Subclasses ``ValueError`` so callers may catch
    either; ``validate_run`` converts it into a non-retryable schema issue so
    the CLI reports a clean FAIL instead of a traceback.
    """


@dataclass(frozen=True)
class ValidationIssue:
    """One finding from one §14 check.

    ``check`` is which of the three checks raised it (schema/boundary/frozen);
    ``code`` is a stable machine-readable identifier (the retry decision keys
    off it via ``RETRYABLE_CODES``); ``severity`` follows §15.1 (schema findings
    are P1 - blocking by default, overridable; boundary and frozen findings are
    P0 - non-overridable, a hard contract breach); ``path`` is the offending
    RUN-relative path for boundary/frozen findings; ``requires_change_proposal``
    marks frozen findings (§14.3: the only sanctioned fix is a CP, §17).
    """

    check: CheckName
    code: str
    message: str
    severity: Severity
    path: str | None = None
    requires_change_proposal: bool = False

    @property
    def retryable(self) -> bool:
        """§24.3: only the agent-fixable schema failures may trigger a retry."""
        return self.code in RETRYABLE_CODES


@dataclass(frozen=True)
class ValidationResult:
    """The outcome of validating one run: the issues found, plus attempt count.

    ``passed`` / ``failed_check`` / ``is_retryable`` are derived from ``issues``
    (a result is consistent by construction - never a passed result with issues,
    never a retryable result with a non-schema issue). ``attempt`` is ``1`` for
    a plain ``validate_run`` call and ``2`` when ``validate_with_retry`` re-ran
    the agent and re-validated.
    """

    run_id: str
    issues: list[ValidationIssue]
    attempt: int = 1

    @property
    def passed(self) -> bool:
        return not self.issues

    @property
    def failed_check(self) -> CheckName | None:
        """The primary failed check by severity priority (frozen > boundary > schema).

        ``None`` when the run passed. When several checks failed, the most
        severe is blamed first - a frozen breach outranks a boundary breach
        outranks a schema breach, matching the §15.1 severity ordering.
        """
        present = {issue.check for issue in self.issues}
        for check in _CHECKS_BY_PRIORITY:
            if check in present:
                return check
        return None

    @property
    def is_retryable(self) -> bool:
        """§24.3: retry once only when every failure is a schema/output-format failure.

        A boundary or frozen breach alongside a schema failure blocks the retry
        - re-running the agent will not fix a contract breach, so the run goes
        straight to Human Triage (§24.2).
        """
        return not self.passed and all(issue.retryable for issue in self.issues)


# ---------------------------------------------------------------------------
# §14.1 schema validation (hand-rolled JSON Schema subset).
# ---------------------------------------------------------------------------

# Asserting keywords the validator fully handles. Any keyword in a schema that
# is neither handled nor ignored-metadata trips a SchemaValidatorError (§24.2).
_HANDLED_KEYWORDS: frozenset[str] = frozenset(
    {
        "type",
        "enum",
        "const",
        "required",
        "properties",
        "additionalProperties",
        "items",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
    }
)
# Non-asserting metadata keywords: ignored per the JSON Schema spec (a schema
# may carry title/description/default/examples without affecting validity).
_IGNORED_KEYWORDS: frozenset[str] = frozenset(
    {
        "$schema",
        "$id",
        "title",
        "description",
        "default",
        "examples",
        "$defs",
        "definitions",
        "comment",
    }
)

_JSON_TYPES: frozenset[str] = frozenset(
    {"object", "array", "string", "integer", "number", "boolean", "null"}
)


def _type_name(value: Any) -> str:
    """JSON-Schema-style type name for ``value`` (bool distinct from int)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _matches_type(value: Any, type_spec: Any) -> bool:
    """Whether ``value`` matches a JSON Schema ``type`` (string or list of strings).

    Raises ``SchemaValidatorError`` on an unknown type name (§24.2 fail-loud) -
    the validator cannot check a type it does not understand, so it refuses
    rather than silently passing.
    """
    types = type_spec if isinstance(type_spec, list) else [type_spec]
    for t in types:
        if not isinstance(t, str) or t not in _JSON_TYPES:
            raise SchemaValidatorError(f"unsupported schema type {t!r}")
        if t == "object" and isinstance(value, dict):
            return True
        if t == "array" and isinstance(value, list):
            return True
        if t == "string" and isinstance(value, str):
            return True
        if t == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if (
            t == "number"
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            return True
        if t == "boolean" and isinstance(value, bool):
            return True
        if t == "null" and value is None:
            return True
    return False


def validate_against_schema(
    value: Any, schema: Any, path: str = ""
) -> list[str]:
    """Return human-readable validation errors for ``value`` against ``schema``.

    Empty list = valid. Recurses over the supported JSON Schema subset. Raises
    ``SchemaValidatorError`` on an unsupported asserting keyword or type
    (§24.2 fail-loud). ``path`` is the dotted path to ``value`` for messages
    (``""`` at root, ``".tasks[0].id"`` nested).
    """
    if not isinstance(schema, dict):
        raise SchemaValidatorError(
            f"schema at {path or '<root>'} is not an object"
        )
    # Fail-loud guard: an asserting keyword we don't handle must not be ignored.
    for key in schema:
        if key not in _HANDLED_KEYWORDS and key not in _IGNORED_KEYWORDS:
            raise SchemaValidatorError(
                f"unsupported schema keyword {key!r} at {path or '<root>'}"
            )

    errors: list[str] = []
    here = path or "<root>"

    if "type" in schema and not _matches_type(value, schema["type"]):
        errors.append(
            f"{here}: expected type {schema['type']}, got {_type_name(value)}"
        )

    if "const" in schema and value != schema["const"]:
        errors.append(f"{here}: expected const {schema['const']!r}, got {value!r}")

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{here}: {value!r} not in enum {schema['enum']}")

    if isinstance(value, dict):
        if "required" in schema:
            for name in schema["required"]:
                if name not in value:
                    errors.append(f"{here}: missing required property {name!r}")
        props = schema.get("properties", {})
        if isinstance(props, dict):
            for name, sub in props.items():
                if name in value:
                    errors.extend(
                        validate_against_schema(value[name], sub, f"{path}.{name}")
                    )
        ap = schema.get("additionalProperties")
        if ap is False:
            for name in sorted(set(value) - set(props)):
                errors.append(f"{here}: additional property {name!r} not allowed")
        elif isinstance(ap, dict):
            for name in sorted(set(value) - set(props)):
                errors.extend(validate_against_schema(value[name], ap, f"{path}.{name}"))

    if "items" in schema and isinstance(value, list):
        item_schema = schema["items"]
        for i, item in enumerate(value):
            errors.extend(validate_against_schema(item, item_schema, f"{path}[{i}]"))

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(
                f"{here}: string length {len(value)} < minLength {schema['minLength']}"
            )
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(
                f"{here}: string length {len(value)} > maxLength {schema['maxLength']}"
            )

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(
                f"{here}: array length {len(value)} < minItems {schema['minItems']}"
            )
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(
                f"{here}: array length {len(value)} > maxItems {schema['maxItems']}"
            )

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{here}: {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{here}: {value} > maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(
                f"{here}: {value} <= exclusiveMinimum {schema['exclusiveMinimum']}"
            )
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            errors.append(
                f"{here}: {value} >= exclusiveMaximum {schema['exclusiveMaximum']}"
            )

    return errors


def validate_schema(result_path: Path, schema_path: Path) -> list[ValidationIssue]:
    """§14.1: check result.json exists, is valid JSON, and conforms to the schema.

    Returns one ``ValidationIssue`` per problem (missing result, invalid JSON,
    schema violation, or an unreadable/unsupported schema file). The
    agent-fixable failures (``result_missing`` / ``result_invalid_json`` /
    ``schema_violation``) are retryable (§24.3); a broken ``output-schema.json``
    is a config error (``schema_file_missing`` / ``schema_file_invalid``) and is
    not retryable.
    """
    if not result_path.is_file():
        return [
            ValidationIssue(
                check="schema",
                code="result_missing",
                message=f"result.json missing at {result_path}",
                severity="P1",
            )
        ]
    try:
        result = json.loads(result_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return [
            ValidationIssue(
                check="schema",
                code="result_invalid_json",
                message=f"result.json is not valid JSON: {exc}",
                severity="P1",
            )
        ]

    if not schema_path.is_file():
        return [
            ValidationIssue(
                check="schema",
                code="schema_file_missing",
                message=f"output-schema.json missing at {schema_path}",
                severity="P1",
            )
        ]
    try:
        schema = json.loads(schema_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return [
            ValidationIssue(
                check="schema",
                code="schema_file_invalid",
                message=f"output-schema.json is not valid JSON: {exc}",
                severity="P1",
            )
        ]

    try:
        errors = validate_against_schema(result, schema)
    except SchemaValidatorError as exc:
        return [
            ValidationIssue(
                check="schema",
                code="schema_file_invalid",
                message=f"output-schema.json unsupported: {exc}",
                severity="P1",
            )
        ]

    return [
        ValidationIssue(check="schema", code="schema_violation", message=err, severity="P1")
        for err in errors
    ]


# ---------------------------------------------------------------------------
# §14.2 file-boundary validation.
# ---------------------------------------------------------------------------


def read_allowed_files(path: Path) -> set[str]:
    """Parse ``allowed-files.txt``: one RUN-relative path per line.

    ``#`` comments and blank lines are ignored, matching the prototype's
    parsing. A missing file yields an empty set - every changed file then
    counts as out-of-bounds, so a run whose input package was not prepared
    fails loudly rather than silently passing.
    """
    allowed: set[str] = set()
    if not path.is_file():
        return allowed
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            allowed.add(line)
    return allowed


def read_changed_files(metadata_path: Path) -> list[str] | None:
    """Return the ``changed_files`` list from ``metadata.json``, or ``None``.

    ``None`` means ``metadata.json`` is missing, invalid JSON, or lacks a list
    ``changed_files`` field - the boundary check treats that as "cannot validate"
    rather than guessing an empty list (which would silently pass).
    """
    if not metadata_path.is_file():
        return None
    try:
        md = json.loads(metadata_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    cf = md.get("changed_files") if isinstance(md, dict) else None
    if not isinstance(cf, list):
        return None
    return [str(c) for c in cf]


def validate_boundary(
    changed_files: list[str] | None,
    allowed: set[str],
) -> list[ValidationIssue]:
    """§14.2: every changed file must be within ``allowed-files.txt``.

    ``changed_files`` are RUN-relative repo files (the wrapper already
    subtracted its own artifacts and excluded out-of-band CC harness state,
    §14.2). A file not in the allow-list is a boundary violation (P0 - a hard
    contract breach). ``changed_files is None`` means ``metadata.json`` is
    missing/invalid - the boundary cannot be checked, which is itself a failure
    (the wrapper did not complete); the redundant ``metadata_missing`` flag was
    dropped because ``None`` already carries that signal.
    """
    if changed_files is None:
        return [
            ValidationIssue(
                check="boundary",
                code="metadata_missing",
                message="metadata.json missing or has no changed_files - "
                "cannot determine file boundary (§14.2)",
                severity="P0",
            )
        ]
    violations = [c for c in changed_files if c not in allowed]
    return [
        ValidationIssue(
            check="boundary",
            code="boundary_violation",
            message=f"changed file {c!r} is not in allowed-files.txt",
            severity="P0",
            path=c,
        )
        for c in violations
    ]


# ---------------------------------------------------------------------------
# §14.3 frozen-artifact validation (the seam).
# ---------------------------------------------------------------------------


def _resolve_run_relative(changed_path: str, run_root: Path) -> Path:
    """Resolve a RUN-relative changed path to an absolute path (normalising ``..``).

    ``Path.resolve()`` normalises ``..`` components lexically even for
    non-existent paths (``strict=False``), so a changed path like
    ``../../01-requirements.md`` from the run dir lands on the feature root.
    Both this and the frozen-artifact path are resolved the same way, so
    symlinked ``repo_root`` prefixes (e.g. macOS ``/tmp``) compare consistently.
    """
    return (run_root / changed_path).resolve()


def validate_frozen(
    changed_files: list[str],
    feature_root: Path,
    run_root: Path,
    frozen_status: Mapping[str, bool],
) -> list[ValidationIssue]:
    """§14.3: a changed file that resolves onto a *frozen* artifact file -> FAIL.

    Each changed file is resolved against ``run_root``; if the resolved path
    equals a frozen artifact's feature-root file AND that artifact is currently
    frozen, it is a frozen-artifact violation (P0, requires a Change Proposal,
    §4.2/§17). In v0.1 this never fires and never false-positives: changed_files
    are RUN-internal (the wrapper snapshots only the run dir, so they carry no
    ``..``), and nothing is frozen by default. The seam exists so that when a
    future runner can surface feature-root changes, the check is already correct.
    """
    if not changed_files:
        return []
    frozen_files: dict[Path, str] = {}
    for artifact, files in _FROZEN_ARTIFACT_FILES.items():
        if frozen_status.get(artifact):
            for fname in files:
                frozen_files[(feature_root / fname).resolve()] = artifact
    if not frozen_files:
        return []
    issues: list[ValidationIssue] = []
    for c in changed_files:
        resolved = _resolve_run_relative(c, run_root)
        hit = frozen_files.get(resolved)
        if hit is not None:
            issues.append(
                ValidationIssue(
                    check="frozen",
                    code="frozen_violation",
                    message=(
                        f"changed file {c!r} modifies frozen artifact "
                        f"{hit!r}; only a Change Proposal may (§4.2/§17)"
                    ),
                    severity="P0",
                    path=c,
                    requires_change_proposal=True,
                )
            )
    return issues


# ---------------------------------------------------------------------------
# validate_run: the pure three-check orchestrator.
# ---------------------------------------------------------------------------


def validate_run(
    repo_root: Path,
    feature_id: str,
    run_id: str,
    *,
    attempt: int = 1,
    origin: str | None = None,
) -> ValidationResult:
    """Run the §14 three deterministic checks against a captured run.

    Pure reader: loads ``output/result.json`` + ``output/metadata.json`` +
    ``input/output-schema.json`` + ``input/allowed-files.txt`` from the run
    directory and the frozen status from the feature run's
    ``status/feature-status.yml``, runs schema (§14.1) + boundary (§14.2) +
    frozen (§14.3), and returns every issue found. No subprocess, no retry -
    the §14.1 retry-once lives in ``validate_with_retry``, which drives this
    function with ``attempt=1`` then ``attempt=2``. Appends one ``validate``
    audit record (like every lifecycle op) carrying the verdict and the attempt
    number, so a retry leaves two distinguishable records (attempt-1-failed,
    attempt-2-passed/failed) rather than two identical-looking ones.

    Raises ``ValueError`` (§24.2 fail loud) if the run directory or the feature
    run's status file is missing/malformed - ``validate-run`` needs a real run
    on a sound feature run to check.
    """
    run_root = run_dir(repo_root, feature_id, run_id)
    if not run_root.is_dir():
        raise ValueError(
            f"run directory {run_id} not found under feature {feature_id} "
            f"(prepare and run it first)"
        )
    feature_root = feature_dir(repo_root, feature_id)
    input_dir = run_root / INPUT_DIR
    output_dir = run_root / OUTPUT_DIR

    schema_issues = validate_schema(
        output_dir / RESULT_JSON, input_dir / OUTPUT_SCHEMA_FILE
    )

    changed_files = read_changed_files(output_dir / METADATA_JSON)
    allowed = read_allowed_files(input_dir / ALLOWED_FILES_FILE)
    boundary_issues = validate_boundary(changed_files, allowed)

    frozen_status = frozen_artifacts_status(feature_root)
    frozen_issues = validate_frozen(
        changed_files or [], feature_root, run_root, frozen_status
    )

    issues = schema_issues + boundary_issues + frozen_issues
    result = ValidationResult(run_id=run_id, issues=issues, attempt=attempt)

    append_audit_event(
        feature_root,
        event="validate",
        payload={
            "run": run_id,
            "feature": feature_id,
            "attempt": attempt,
            "passed": result.passed,
            "failed_check": result.failed_check,
            "issue_count": len(result.issues),
        },
        origin=origin,
    )
    return result


# ---------------------------------------------------------------------------
# §14.1/§24.3 retry-once seam.
# ---------------------------------------------------------------------------


def validate_with_retry(
    validate: Callable[[int], ValidationResult],
    rerun: Callable[[], None],
) -> ValidationResult:
    """Validate; on a retryable failure, rerun once and re-validate (§14.1/§24.3).

    Pure function of two callables: ``validate(attempt)`` produces a
    ``ValidationResult`` tagged with that attempt number, and ``rerun``
    re-executes the agent run (returns nothing). Only schema / output-format
    failures (``result.is_retryable``) are retried, exactly once; a boundary or
    frozen breach (or a passed result) short-circuits to the caller unchanged -
    re-running the agent will not fix a contract breach, so the run goes
    straight to Human Triage (§24.2). The returned result carries ``attempt=2``
    when a retry happened, ``attempt=1`` otherwise.

    The attempt is passed *into* ``validate`` (rather than stamped on the
    result afterwards) so the audit record each ``validate_run`` writes already
    carries the right attempt - a retry leaves two distinguishable records
    (attempt-1-failed, attempt-2-passed/failed). Kept callable-injected so the
    retry semantics are unit-testable without a real ``claude`` subprocess.
    """
    first = validate(1)
    if first.passed or not first.is_retryable:
        return first
    rerun()
    return validate(2)
