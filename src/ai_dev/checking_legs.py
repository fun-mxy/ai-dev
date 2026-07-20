"""Checking legs - v0.2 ticket 02 (spec §9.3/§9.4/§15, §26.3).

The checking legs are the two review roles that run after an implement run:
the Code Reviewer (§9.3) and the Spec Gap Analyst (§9.4). Both emit
``issues[]`` under the shared §15 Issue Contract, so they share one
output-schema and one ticket. From a feature run whose tasks + lane-graph are
*frozen* (§4.2) AND whose lane has an ``implement-result`` (ticket 01), each
leg:

1. builds its input package by *reusing* the v0.1 ``prepare_run`` with the
   shared §15 issues output-schema (no new run mechanism) - the reviewer's
   task carries the implement run's ``changed_files`` + their contents (the
   "diff") + the task context; the gap's carries requirements/design/tasks +
   the implement diff;
2. runs it headless via ``run_headless`` and validates it with ``validate_run``
   (the §14 three checks, now against the issues schema);
3. rolls the run's ``issues[]`` + ``metadata`` + validation up into the
   lane-level ``review-report.{md,json}`` / ``spec-gap-report.{md,json}`` §4.4
   double product.

The §9.3/§9.4 responsibility boundary is held by instruction: each role's
task-package scopes what it checks and explicitly what it must NOT check (the
reviewer must not judge spec deviation; the gap must not do style review). The
shared §15 schema constrains each issue's ``source`` to the two checking values,
and each role's task-package instructs the correct one; the issues are carried
verbatim into the report (matching the implementer leg's verbatim carry of
``result`` fields), with the report's top-level ``source`` labelling which role
produced it.

Unlike the implementer leg, the checking legs write NO canonical status (§4.3):
they only read the implement run and produce a report. The ``proposed_done``
writeback is the implementer leg's single sanctioned canonical write (ticket 01);
the checking legs add none.

Path-space note (v0.2): per §6 the reports nest in role subdirs under the lane -
``lanes/<lane_id>/review/review-report.{md,json}`` and
``lanes/<lane_id>/spec-gap/spec-gap-report.{md,json}`` - alongside the flat
``implement-result.{md,json}`` (which §6 lists directly under ``LANE-001/``).
The issue ``source`` is carried verbatim from the run's ``result.json`` (the
schema's enum constrains it to ``code_review`` / ``spec_gap``, and each role's
task-package instructs the correct value); the report's top-level ``source``
field is the role's canonical label.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from ai_dev.json_artifact import read_json_object
from ai_dev.implement_leg import read_task_text
from ai_dev.paths import (
    LANES_DIR,
    METADATA_JSON,
    OUTPUT_DIR,
    RESULT_JSON,
    feature_dir,
    run_dir,
)
from ai_dev.profiles import AgentProfile
from ai_dev.run_prepare import prepare_run
from ai_dev.run_wrapper import (
    DEFAULT_MAX_TURNS,
    DEFAULT_PERMISSION_MODE,
    run_headless,
)
from ai_dev.status import frozen_artifacts_status
from ai_dev.templates import DESIGN_MD, REQUIREMENTS_MD, TASKS_MD
from ai_dev.validate import ValidationResult, validate_run

# ---------------------------------------------------------------------------
# The shared §15 Issue Contract as a JSON Schema (reviewer + gap共用).
# ---------------------------------------------------------------------------

# A valid ``result.json`` for a checking run is ``{"issues": [...]}`` where each
# issue carries the full §15 field set. ``source`` is constrained to the two
# checking sources; the runtime overrides it to the role's canonical value in
# the report (defense-in-depth: the schema validates the agent produced a
# plausible source, the rollup guarantees the authoritative one). ``evidence``
# is an array of ``{file, line?}`` - ``file`` required, ``line`` optional (a
# whole-file finding has no line). The ``related_*`` arrays may be empty (an
# issue need not map to a specific task/req/AC) but must be present, matching
# the §15 example's complete field set.
ISSUES_OUTPUT_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "IssuesReport",
    "type": "object",
    "required": ["issues"],
    "additionalProperties": True,
    "properties": {
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "id",
                    "source",
                    "severity",
                    "title",
                    "description",
                    "related_tasks",
                    "related_requirements",
                    "related_acceptance_criteria",
                    "evidence",
                    "recommendation",
                    "requires_change_proposal",
                ],
                "additionalProperties": True,
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "source": {
                        "type": "string",
                        "enum": ["code_review", "spec_gap"],
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["P0", "P1", "P2", "P3"],
                    },
                    "title": {"type": "string", "minLength": 1},
                    "description": {"type": "string", "minLength": 1},
                    "related_tasks": {"type": "array", "items": {"type": "string"}},
                    "related_requirements": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "related_acceptance_criteria": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["file"],
                            "additionalProperties": True,
                            "properties": {
                                "file": {"type": "string", "minLength": 1},
                                "line": {"type": "integer", "minimum": 1},
                            },
                        },
                    },
                    "recommendation": {"type": "string", "minLength": 1},
                    "requires_change_proposal": {"type": "boolean"},
                },
            },
        }
    },
}

# Lane-level §4.4 double-product filenames (public so later tickets / tests
# reference one source of truth for the on-disk layout, §6 ``lanes/LANE-001/``).
# §6 nests each checking role's report in its own subdir (``review/`` /
# ``spec-gap/``) under the lane - unlike ``implement-result`` which §6 lists
# flat. The subdir names are public so tests and later tickets resolve them.
REVIEW_DIR = "review"
REVIEW_REPORT_MD = "review-report.md"
REVIEW_REPORT_JSON = "review-report.json"
SPEC_GAP_DIR = "spec-gap"
SPEC_GAP_REPORT_MD = "spec-gap-report.md"
SPEC_GAP_REPORT_JSON = "spec-gap-report.json"

# The two roles this ticket prepares (§9.3/§9.4). Pinned, not caller-supplied:
# the reviewer leg is the Code Reviewer role by definition, the gap leg the
# Spec Gap Analyst. The §15 ``source`` each role's issues carry.
_REVIEWER_ROLE = "Code Reviewer"
_SPEC_GAP_ROLE = "Spec Gap Analyst"
_REVIEW_SOURCE = "code_review"
_GAP_SOURCE = "spec_gap"


# ---------------------------------------------------------------------------
# Reading the implement run a checking leg reviews (ticket 01 -> 02).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImplementRunFacts:
    """The implement run a checking leg reviews (ticket 01's lane rollup).

    Carries the implement run id, the wrapper-computed ``changed_files`` (the
    code under review), each changed file's content (the "diff" - v0.2 has no
    before-state, so the new content IS the change), and the task text the
    implementer executed (the reviewer's task context). Read-only facts gathered
    from the lane's ``implement-result.json`` + the implement run's
    ``metadata.json`` + the feature's ``03-tasks.md``.
    """

    run_id: str
    changed_files: list[str]
    file_contents: dict[str, str]
    task_text: str


def _read_changed_file_contents(
    implement_run_root: Path, changed_files: list[str]
) -> dict[str, str]:
    """Read each changed file's content from the implement run dir (the "diff").

    v0.2's empty-workspace model means there is no before-state to diff against,
    so the new content of each changed file IS the change the reviewer/gap
    inspects. A file that cannot be read (binary, permissions) is recorded as a
    placeholder rather than crashing the gatherer - the report still carries the
    ``changed_files`` list, and an unreadable blob is surfaced, not hidden.
    """
    contents: dict[str, str] = {}
    for rel in changed_files:
        try:
            contents[rel] = (implement_run_root / rel).read_text()
        except (OSError, UnicodeDecodeError):
            contents[rel] = f"<could not read {rel}>"
    return contents


def read_implement_run_facts(
    repo_root: Path, feature_id: str, lane_id: str
) -> ImplementRunFacts:
    """Gather the implement run facts a checking leg reviews (ticket 01 -> 02).

    Reads the lane's ``implement-result.json`` for the implement run id, the
    implement run's ``metadata.json`` for ``changed_files``, each changed file's
    content from the implement run dir (the "diff"), and the task text from
    ``03-tasks.md`` (the reviewer's task context, reusing the implementer leg's
    reader so the two legs agree on what "the task" is). Fail-loud (§24.2) when
    the lane has no ``implement-result`` (no implement run to check) or the
    implement run / its metadata are missing - a checking leg with nothing to
    review is a precondition breach, not a silent no-op.
    """
    feature_root = feature_dir(repo_root, feature_id)
    implement_result_path = (
        feature_root / LANES_DIR / lane_id / "implement-result.json"
    )
    implement_result = read_json_object(implement_result_path)
    if implement_result is None:
        raise ValueError(
            f"no implement-result.json under lanes/{lane_id}/ for feature "
            f"{feature_id}; run the implementer leg first (ticket 01) before "
            f"reviewing (§26.3)"
        )
    implement_run_id = implement_result.get("run")
    if not isinstance(implement_run_id, str) or not implement_run_id:
        raise ValueError(
            f"implement-result.json at {implement_result_path} has no 'run' id "
            f"(§24.2)"
        )

    implement_run_root = run_dir(repo_root, feature_id, implement_run_id)
    metadata = read_json_object(implement_run_root / OUTPUT_DIR / METADATA_JSON)
    if metadata is None:
        raise ValueError(
            f"implement run {implement_run_id} has no metadata.json at "
            f"{implement_run_root}; cannot determine changed_files (§24.2)"
        )
    raw_changed = metadata.get("changed_files")
    changed_files = (
        [str(c) for c in raw_changed]
        if isinstance(raw_changed, list)
        else []
    )
    file_contents = _read_changed_file_contents(implement_run_root, changed_files)
    task_text = read_task_text(feature_root)
    return ImplementRunFacts(
        run_id=implement_run_id,
        changed_files=changed_files,
        file_contents=file_contents,
        task_text=task_text,
    )


# ---------------------------------------------------------------------------
# Frozen precondition (shared with the implementer leg, §4.2).
# ---------------------------------------------------------------------------


def _require_frozen(feature_root: Path) -> None:
    """Reject an unfrozen precondition before reading facts or allocating a run.

    The checking legs run after the implementer leg, which already required
    frozen tasks + lane-graph; re-checking is cheap defense-in-depth and keeps
    the precondition shape identical to ``build_implementer_input_package``.
    The gap leg additionally reads requirements/design, but those are frozen at
    earlier gates (§18.1/§18.2) - the implementer leg did not gate on them
    either, so neither does this one.
    """
    frozen = frozen_artifacts_status(feature_root)
    if not (frozen.get("tasks") and frozen.get("lane_graph")):
        raise ValueError(
            "checking leg requires frozen tasks + lane_graph (§4.2); the "
            "implement run was built on frozen specs - freeze them at the task "
            "gate first"
        )


# ---------------------------------------------------------------------------
# Input-package assembly: the "diff" block + each role's task text.
# ---------------------------------------------------------------------------


def _diff_block(facts: ImplementRunFacts) -> str:
    """Render the implement run's changed files + contents as a review block.

    Lists every changed file (the implementer's footprint), then dumps the
    content of each *workspace* file (the actual code under review). The
    implement run's own ``output/result.json`` / ``result.md`` are the
    implementer's self-report, not code - they are listed in the footprint but
    their content is not dumped, so the reviewer/gap judge the implementation,
    not the implementer's claims (§13.2's ``changed_files`` is workspace files).
    A file that cannot be read is surfaced as a placeholder, not hidden.
    """
    lines = [f"changed_files: {facts.changed_files}", ""]
    for rel in facts.changed_files:
        if rel.startswith("output/"):
            # The implementer's report artifacts are listed but not dumped.
            continue
        content = facts.file_contents.get(rel, f"<could not read {rel}>")
        lines.append(f"### {rel}")
        lines.append("```")
        lines.append(content)
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def _reviewer_task_text(facts: ImplementRunFacts) -> str:
    """The Code Reviewer task: review the implement diff for quality (§9.3).

    Carries the task context (what was implemented) + the implement diff, then
    scopes the role: review code quality / bugs / boundaries / security, and
    explicitly NOT spec deviation (the gap analyst's job, §9.4). The agent is
    told to emit ``issues[]`` with ``source="code_review"`` (the runtime
    overrides it regardless, but the instruction keeps the agent's own output
    self-consistent).
    """
    return (
        "Review the implement run's code changes for quality (§9.3).\n"
        "\n"
        "## Task context (what was implemented)\n"
        "\n"
        f"{facts.task_text}\n"
        "\n"
        f"## Implement run changes ({facts.run_id})\n"
        "\n"
        f"{_diff_block(facts)}"
        "\n"
        "## Your role: Code Reviewer (§9.3)\n"
        "\n"
        "You review CODE QUALITY: bugs, boundary conditions, maintainability, "
        "and security of the changes above.\n"
        "You DO NOT judge spec deviation (whether the implementation deviates "
        "from the spec) - that is the Spec Gap Analyst's job (§9.4), not yours.\n"
        "Output `issues[]` in `output/result.json` conforming to "
        "`input/output-schema.json` (§15). Set `source: \"code_review\"` on "
        "every issue. An empty `issues: []` is a valid result (no findings).\n"
    )


def _spec_gap_task_text(
    feature_root: Path, facts: ImplementRunFacts
) -> str:
    """The Spec Gap Analyst task: compare the spec against the implement diff (§9.4).

    Carries the frozen requirements/design/tasks + the implement diff, then
    scopes the role: find missing requirements, over-scope implementation, and
    design deviation (places needing a Change Proposal), and explicitly NOT code
    style (the reviewer's job, §9.3). Reads the §7 spec artifacts from the
    feature root; fail-loud (§24.2) if any is missing - the gap cannot compare
    against a spec it cannot read.
    """
    specs: list[str] = []
    for label, fname in (
        ("Requirements", REQUIREMENTS_MD),
        ("Design", DESIGN_MD),
        ("Tasks", TASKS_MD),
    ):
        path = feature_root / fname
        if not path.is_file():
            raise ValueError(f"{fname} missing at {path} (§7 - gap leg needs it)")
        specs.append(f"## {label} ({fname})\n\n{path.read_text()}")
    return (
        "Compare the spec (requirements/design/tasks) against the implement "
        "run's changes; find gaps (§9.4).\n"
        "\n"
        + "\n\n".join(specs)
        + "\n"
        f"## Implement run changes ({facts.run_id})\n"
        "\n"
        f"{_diff_block(facts)}"
        "\n"
        "## Your role: Spec Gap Analyst (§9.4)\n"
        "\n"
        "You compare the SPEC above against the IMPLEMENTATION diff:\n"
        "- missing requirements (a REQ/AC not addressed by the changes);\n"
        "- over-scope implementation (changes beyond what the spec asks);\n"
        "- design deviation (changes that diverge from the design);\n"
        "- places that need a Change Proposal (§17).\n"
        "You DO NOT do code-style review - that is the Code Reviewer's job "
        "(§9.3), not yours.\n"
        "Output `issues[]` in `output/result.json` conforming to "
        "`input/output-schema.json` (§15). Set `source: \"spec_gap\"` on every "
        "issue. An empty `issues: []` is a valid result (no gaps).\n"
    )


def build_reviewer_input_package(
    repo_root: Path, feature_id: str, lane_id: str
) -> str:
    """Build the Code Reviewer input package from the lane's implement run (§9.3).

    Verifies the §4.2 frozen precondition, gathers the implement run facts, and
    delegates to the v0.1 ``prepare_run`` with the role pinned to ``Code
    Reviewer`` and the shared §15 issues output-schema. Returns the allocated
    ``RUN-NNN`` id. Reuses ``prepare_run`` unchanged (no new run mechanism): it
    allocates the run id, scaffolds the §12.2 input package, and appends the
    ``prepare_run`` audit record. The frozen + implement-run checks happen
    before any allocation so a rejected precondition leaves no partial run.
    """
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id} not found under {repo_root}")
    _require_frozen(feature_root)
    facts = read_implement_run_facts(repo_root, feature_id, lane_id)
    return prepare_run(
        repo_root,
        feature_id,
        _REVIEWER_ROLE,
        _reviewer_task_text(facts),
        output_schema=ISSUES_OUTPUT_SCHEMA,
    )


def build_spec_gap_input_package(
    repo_root: Path, feature_id: str, lane_id: str
) -> str:
    """Build the Spec Gap Analyst input package from the lane's implement run (§9.4).

    Same shape as ``build_reviewer_input_package`` but the task carries the
    frozen requirements/design/tasks + the implement diff, the role is pinned to
    ``Spec Gap Analyst``, and the §9.4 boundary (no style review) is stated.
    """
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id} not found under {repo_root}")
    _require_frozen(feature_root)
    facts = read_implement_run_facts(repo_root, feature_id, lane_id)
    return prepare_run(
        repo_root,
        feature_id,
        _SPEC_GAP_ROLE,
        _spec_gap_task_text(feature_root, facts),
        output_schema=ISSUES_OUTPUT_SCHEMA,
    )


# ---------------------------------------------------------------------------
# Lane-level report rollup (§4.4 double product).
# ---------------------------------------------------------------------------


def _serialize_validation(validation: ValidationResult) -> dict[str, Any]:
    """Render a ``ValidationResult`` as a JSON-serialisable dict for the rollup.

    ``asdict`` on each ``ValidationIssue`` yields exactly its six fields - the
    same shape the implementer leg's rollup uses, so the two reports' validation
    blocks are consistent.
    """
    return {
        "passed": validation.passed,
        "attempt": validation.attempt,
        "failed_check": validation.failed_check,
        "issues": [asdict(issue) for issue in validation.issues],
    }


def _extract_issues(result: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Best-effort extract of the ``issues[]`` list from the run's result.json.

    Tolerant: a missing/invalid result.json (validation already failed) yields
    ``[]`` - the report carries the validation verdict separately, so a failed
    run reports no trusted issues. Non-dict issue entries are dropped (a
    schema-valid run cannot produce them, but a best-effort read should not
    crash on a malformed one). The issues are carried verbatim (the schema's
    ``source`` enum + each role's task-package instruction hold the §15 source;
    the report's top-level ``source`` labels the role).
    """
    if not result:
        return []
    raw = result.get("issues")
    if not isinstance(raw, list):
        return []
    return [issue for issue in raw if isinstance(issue, dict)]


def _build_rollup(
    feature_id: str,
    lane_id: str,
    run_id: str,
    role: str,
    source: str,
    result: Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None,
    validation: ValidationResult,
) -> dict[str, Any]:
    """Assemble a checking-report JSON document from run facts.

    Field-complete against the run's ``result.json`` (§13.1 ``issues[]``) +
    ``metadata.json`` (§13.2) + the validation verdict: the agent-declared
    issues are carried verbatim, the wrapper-computed metadata is nested under
    ``run_metadata``, and the validation outcome under ``validation``. The
    top-level ``source`` is the role's canonical label (which role produced this
    report); the per-issue ``source`` is whatever the agent wrote (constrained by
    the schema enum).
    """
    metadata = metadata or {}
    issues = _extract_issues(result)
    return {
        "feature": feature_id,
        "lane": lane_id,
        "run": run_id,
        "role": role,
        "source": source,
        "issues": issues,
        "run_metadata": {
            "run_id": metadata.get("run_id"),
            "profile": metadata.get("profile"),
            "cli": metadata.get("cli"),
            "backend": metadata.get("backend"),
            "model": metadata.get("model"),
            "started_at": metadata.get("started_at"),
            "ended_at": metadata.get("ended_at"),
            "exit_code": metadata.get("exit_code"),
            "changed_files": metadata.get("changed_files", []),
        },
        "validation": _serialize_validation(validation),
    }


def _report_md(title: str, rollup: Mapping[str, Any]) -> str:
    """Render the human-readable mirror of a checking report (§4.4)."""
    meta = rollup.get("run_metadata") or {}
    val = rollup.get("validation") or {}
    issues = rollup.get("issues") or []
    issue_lines = (
        "\n".join(
            f"- [{i.get('severity')}] {i.get('title')} "
            f"(source={i.get('source')}, requires_cp={i.get('requires_change_proposal')})\n"
            f"  evidence: {i.get('evidence', [])} | related: "
            f"tasks={i.get('related_tasks', [])} "
            f"reqs={i.get('related_requirements', [])} "
            f"acs={i.get('related_acceptance_criteria', [])}\n"
            f"  recommendation: {i.get('recommendation')}"
            for i in issues
        )
        or "_none_"
    )
    val_issue_lines = (
        "\n".join(
            f"- [{i.get('severity')}] {i.get('check')}: {i.get('message')}"
            for i in val.get("issues", [])
        )
        or "_none_"
    )
    return (
        f"# {title} - {rollup.get('lane')}\n"
        f"\n"
        f"- feature: {rollup.get('feature')}\n"
        f"- lane: {rollup.get('lane')}\n"
        f"- run: {rollup.get('run')}\n"
        f"- role: {rollup.get('role')}\n"
        f"- source: {rollup.get('source')}\n"
        f"\n"
        f"## Issues ({len(issues)})\n"
        f"\n"
        f"{issue_lines}\n"
        f"\n"
        f"## Run metadata\n"
        f"\n"
        f"- profile: {meta.get('profile')}\n"
        f"- cli: {meta.get('cli')} / backend: {meta.get('backend')} / "
        f"model: {meta.get('model')}\n"
        f"- started_at: {meta.get('started_at')}\n"
        f"- ended_at: {meta.get('ended_at')}\n"
        f"- exit_code: {meta.get('exit_code')}\n"
        f"- changed_files: {meta.get('changed_files', [])}\n"
        f"\n"
        f"## Validation\n"
        f"\n"
        f"- passed: {val.get('passed')}\n"
        f"- attempt: {val.get('attempt')}\n"
        f"- failed_check: {val.get('failed_check')}\n"
        f"- issues:\n{val_issue_lines}\n"
    )


def _write_report(
    feature_root: Path,
    lane_id: str,
    *,
    role: str,
    source: str,
    report_dir_name: str,
    report_md_name: str,
    report_json_name: str,
    run_id: str,
    result: Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None,
    validation: ValidationResult,
) -> tuple[Path, Path]:
    """Write a lane-level checking report ``{md,json}`` rollup (§4.4, §6).

    Rolls the run's ``result.json`` (issues) + ``metadata.json`` + validation
    verdict up into the §4.4 double product under
    ``lanes/<lane_id>/<report_dir_name>/`` (§6 nests each checking role's report
    in its own subdir). The JSON is the canonical machine-readable rollup; the
    markdown is the human mirror. Returns ``(md_path, json_path)``. Pure writer:
    reads nothing from disk beyond what the caller passed in, so it is
    unit-testable from literals.
    """
    feature_id = feature_root.name
    rollup = _build_rollup(
        feature_id, lane_id, run_id, role, source, result, metadata, validation
    )
    title = (
        "Review Report" if source == _REVIEW_SOURCE else "Spec Gap Report"
    )
    report_dir = feature_root / LANES_DIR / lane_id / report_dir_name
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / report_json_name
    md_path = report_dir / report_md_name
    json_path.write_text(json.dumps(rollup, indent=2, ensure_ascii=False) + "\n")
    md_path.write_text(_report_md(title, rollup))
    return md_path, json_path


def write_review_report(
    feature_root: Path,
    lane_id: str,
    *,
    run_id: str,
    result: Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None,
    validation: ValidationResult,
) -> tuple[Path, Path]:
    """Write the lane-level ``review-report.{md,json}`` rollup (Code Reviewer)."""
    return _write_report(
        feature_root,
        lane_id,
        role=_REVIEWER_ROLE,
        source=_REVIEW_SOURCE,
        report_dir_name=REVIEW_DIR,
        report_md_name=REVIEW_REPORT_MD,
        report_json_name=REVIEW_REPORT_JSON,
        run_id=run_id,
        result=result,
        metadata=metadata,
        validation=validation,
    )


def write_spec_gap_report(
    feature_root: Path,
    lane_id: str,
    *,
    run_id: str,
    result: Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None,
    validation: ValidationResult,
) -> tuple[Path, Path]:
    """Write the lane-level ``spec-gap-report.{md,json}`` rollup (Spec Gap Analyst)."""
    return _write_report(
        feature_root,
        lane_id,
        role=_SPEC_GAP_ROLE,
        source=_GAP_SOURCE,
        report_dir_name=SPEC_GAP_DIR,
        report_md_name=SPEC_GAP_REPORT_MD,
        report_json_name=SPEC_GAP_REPORT_JSON,
        run_id=run_id,
        result=result,
        metadata=metadata,
        validation=validation,
    )


# ---------------------------------------------------------------------------
# Orchestration: build input package -> run -> validate -> rollup.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckingLegResult:
    """A checking leg's return: the captured run + the lane report outcome.

    Carries the run identity (``run_id`` / ``lane_id`` / ``feature_id`` /
    ``profile`` / ``role`` / ``source`` / ``exit_code``), the full ``validation``
    verdict, the ``issue_count`` (the agent-declared issues, pre-override), and
    the paths to the lane-level report products. No ``task_ids_marked`` - the
    checking legs write no canonical status (§4.3), unlike the implementer leg.
    """

    run_id: str
    lane_id: str
    feature_id: str
    profile: str
    role: str
    source: str
    exit_code: int
    validation: ValidationResult
    issue_count: int
    report_md: Path
    report_json: Path


def _run_checking_leg(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    profile: AgentProfile,
    *,
    role: str,
    source: str,
    report_dir_name: str,
    report_md_name: str,
    report_json_name: str,
    build_input_package: Callable[[Path, str, str], str],
    claude_path: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    started_at: str | None = None,
    ended_at: str | None = None,
) -> CheckingLegResult:
    """Run a checking leg end to end: build input -> run -> validate -> rollup.

    Composes the v0.1 seams unchanged: the role-specific ``build_input_package``
    (which reuses ``prepare_run`` with the issues schema), ``run_headless`` (env
    isolation + capture), and ``validate_run`` (the §14 three checks against the
    issues schema). Unlike the implementer leg there is no canonical writeback:
    the checking legs only read the implement run and produce a report, so a
    passing or failing validation both flow straight to the rollup. Returns a
    ``CheckingLegResult`` whether the run passed or failed validation (mirrors
    ``run_implementer_leg`` / ``run_headless`` returning verdicts rather than
    raising on a captured run failure).
    """
    run_id = build_input_package(repo_root, feature_id, lane_id)
    run_result = run_headless(
        repo_root,
        feature_id,
        run_id,
        profile,
        max_turns=max_turns,
        permission_mode=permission_mode,
        claude_path=claude_path,
        started_at=started_at,
        ended_at=ended_at,
    )
    validation = validate_run(repo_root, feature_id, run_id)

    feature_root = feature_dir(repo_root, feature_id)
    run_root = run_dir(repo_root, feature_id, run_id)
    result = read_json_object(run_root / OUTPUT_DIR / RESULT_JSON)
    metadata = read_json_object(run_root / OUTPUT_DIR / METADATA_JSON)
    issue_count = len(_extract_issues(result))

    md_path, json_path = _write_report(
        feature_root,
        lane_id,
        role=role,
        source=source,
        report_dir_name=report_dir_name,
        report_md_name=report_md_name,
        report_json_name=report_json_name,
        run_id=run_id,
        result=result,
        metadata=metadata,
        validation=validation,
    )

    return CheckingLegResult(
        run_id=run_id,
        lane_id=lane_id,
        feature_id=feature_id,
        profile=profile.name,
        role=role,
        source=source,
        exit_code=run_result.exit_code,
        validation=validation,
        issue_count=issue_count,
        report_md=md_path,
        report_json=json_path,
    )


def run_reviewer_leg(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    profile: AgentProfile,
    *,
    claude_path: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    started_at: str | None = None,
    ended_at: str | None = None,
) -> CheckingLegResult:
    """Run the Code Reviewer leg (§9.3): build -> run -> validate -> review-report."""
    return _run_checking_leg(
        repo_root,
        feature_id,
        lane_id,
        profile,
        role=_REVIEWER_ROLE,
        source=_REVIEW_SOURCE,
        report_dir_name=REVIEW_DIR,
        report_md_name=REVIEW_REPORT_MD,
        report_json_name=REVIEW_REPORT_JSON,
        build_input_package=build_reviewer_input_package,
        claude_path=claude_path,
        max_turns=max_turns,
        permission_mode=permission_mode,
        started_at=started_at,
        ended_at=ended_at,
    )


def run_spec_gap_leg(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    profile: AgentProfile,
    *,
    claude_path: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    started_at: str | None = None,
    ended_at: str | None = None,
) -> CheckingLegResult:
    """Run the Spec Gap Analyst leg (§9.4): build -> run -> validate -> spec-gap-report."""
    return _run_checking_leg(
        repo_root,
        feature_id,
        lane_id,
        profile,
        role=_SPEC_GAP_ROLE,
        source=_GAP_SOURCE,
        report_dir_name=SPEC_GAP_DIR,
        report_md_name=SPEC_GAP_REPORT_MD,
        report_json_name=SPEC_GAP_REPORT_JSON,
        build_input_package=build_spec_gap_input_package,
        claude_path=claude_path,
        max_turns=max_turns,
        permission_mode=permission_mode,
        started_at=started_at,
        ended_at=ended_at,
    )
