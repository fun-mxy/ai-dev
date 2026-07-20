"""Implementer leg - v0.2 ticket 01 (spec §9.2, §26.3).

The Implementer leg is the first half of the v0.2 implement -> review/gap ->
verify -> bundle -> lane-gate loop. From a feature run whose tasks + lane-graph
are *frozen* (§4.2), it:

1. builds an Implementer input package - task text read from ``03-tasks.md``,
   allowed-files read from ``04-lane-graph.yml``'s expected/exclusive files for
   the lane - by *reusing* the v0.1 ``prepare_run`` (no new run mechanism);
2. runs it headless via the v0.1 ``run_headless`` and validates it with the v0.1
   ``validate_run`` (the §14 three checks);
3. writes the run's ``result.json`` task status back to canonical
   ``task-status.yml`` as ``proposed_done`` - via the deterministic
   ``mark_task_proposed_done`` writer, never the model (§4.3);
4. rolls the run's ``result`` + ``metadata`` + validation up into the lane-level
   ``implement-result.{md,json}`` double product (§4.4).

The §9.2 limits are enforced by composition, not re-implemented here: the
boundary is the existing ``validate-run`` (reused unchanged), and the writeback
is gated on a passing validation so a boundary-breaching or schema-invalid run
never reaches canonical status. The Implementer can only ever ``proposed_done``
- ``accepted_done`` is structurally false (invariant #7, enforced in
``mark_task_proposed_done``); final done is a later gate's call (§18.4).

Path-space note (v0.2): the lane-graph's ``expected_files`` / ``exclusive_files``
are taken verbatim as the run's RUN-relative allowed paths. The v0.1 boundary
check is exact-match (no glob expansion - reusing ``validate-run`` unchanged
means no glob support this ticket), so a v0.2 lane declares exact workspace
paths (e.g. ``workspace/hello.py``); the spec's ``src/adapter/**`` glob form is
the multi-lane repo-relative future (§27.2) and is out of scope here.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

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
from ai_dev.status import frozen_artifacts_status, mark_task_proposed_done
from ai_dev.templates import LANE_GRAPH_YML, TASKS_MD
from ai_dev.validate import ValidationResult, validate_run

# Lane-level §4.4 double-product filenames (public so later tickets / tests
# reference one source of truth for the on-disk layout, §6 ``lanes/LANE-001/``).
IMPLEMENT_RESULT_MD = "implement-result.md"
IMPLEMENT_RESULT_JSON = "implement-result.json"

# The single role this leg prepares (§9.2). Pinned, not caller-supplied: the
# implementer leg is the Implementer role by definition.
_IMPLEMENTER_ROLE = "Implementer"

# The §9.2/§13.1 result status that triggers the proposed_done writeback. Any
# other status (``failed``) leaves canonical task status untouched.
_PROPOSED_DONE = "proposed_done"


@dataclass(frozen=True)
class LaneEntry:
    """One parsed lane entry from ``04-lane-graph.yml`` (§7.5).

    Carries the full §7.5 lane-entry shape so the format stays extensible to more
    lanes later; the implementer leg consumes ``expected_files`` /
    ``exclusive_files`` / ``tasks``, the rest is preserved for downstream
    tickets (review/gap/verify consume ``verification_scope``, etc.).
    """

    id: str
    purpose: str | None
    tasks: list[str]
    depends_on: list[str]
    expected_files: list[str]
    exclusive_files: list[str]
    provides: list[str]
    consumes: list[str]
    verification_scope: list[str]
    merge_policy: dict[str, Any] | None
    # §9.5/§7.5: the lane's declared verify commands (pytest/mypy/build), the
    # source the shell Verifier (ticket 03) executes. Carried as raw dicts
    # (``[{"name": ..., "command": ...}, ...]``) so the lane-graph parser only
    # validates *shape* here; the verifier module parses each dict into a typed
    # ``VerifyCommand`` and validates the name/command strings (keeping the
    # semantic validation next to the role that consumes it, and avoiding an
    # import cycle - this module must not import the verifier). Defaults to
    # empty so existing lanes / direct constructions stay valid.
    verification_commands: list[dict[str, Any]] = field(default_factory=list)


def _require_str_list(raw: Any, name: str, lane_id: str) -> list[str]:
    """Coerce a lane-entry list field to ``list[str]``, fail-loud on bad shape.

    §7.5 list fields (``tasks`` / ``expected_files`` / ...) are sequences of
    strings; a missing field is an empty list, but a present non-list or
    non-string element is a config error (§24.2) - silently coercing would hide
    a malformed lane-graph.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(
            f"lane {lane_id!r} field {name!r} in {LANE_GRAPH_YML} must be a list"
        )
    return [str(item) for item in raw]


def _require_dict_list(
    raw: Any, name: str, lane_id: str
) -> list[dict[str, Any]]:
    """Return a lane-entry list-of-mappings field, fail-loud on bad shape.

    ``verification_commands`` (§9.5/§7.5) is a list of ``{name, command}``
    mappings; a missing field is an empty list (the lane declares no verify
    commands), but a present non-list or non-mapping element is a config error
    (§24.2). Only shape is validated here - the verifier module parses each
    mapping into a typed ``VerifyCommand`` and checks the name/command strings.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(
            f"lane {lane_id!r} field {name!r} in {LANE_GRAPH_YML} must be a list"
        )
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(
                f"lane {lane_id!r} field {name!r}[{i}] in {LANE_GRAPH_YML} "
                f"must be a mapping (§9.5)"
            )
    return [dict(item) for item in raw]


def read_lane_entry(feature_root: Path, lane_id: str) -> LaneEntry:
    """Parse ``04-lane-graph.yml`` and return the entry for ``lane_id`` (§7.5).

    Fail-loud (§24.2) if the lane-graph is missing, not a mapping, has no
    ``lanes`` list, or does not contain ``lane_id`` - the implementer leg cannot
    proceed without a real lane to implement.
    """
    path = feature_root / LANE_GRAPH_YML
    if not path.is_file():
        raise ValueError(f"{LANE_GRAPH_YML} missing at {path} (§7.5)")
    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ValueError(f"{LANE_GRAPH_YML} at {path} is not valid YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"{LANE_GRAPH_YML} at {path} is not a mapping (§7.5)")
    lanes = doc.get("lanes")
    if not isinstance(lanes, list):
        raise ValueError(f"{LANE_GRAPH_YML} at {path} has no 'lanes' list (§7.5)")
    for raw in lanes:
        if not isinstance(raw, dict):
            continue
        if raw.get("id") == lane_id:
            merge_policy = raw.get("merge_policy")
            return LaneEntry(
                id=str(raw.get("id")),
                purpose=raw.get("purpose"),
                tasks=_require_str_list(raw.get("tasks"), "tasks", lane_id),
                depends_on=_require_str_list(raw.get("depends_on"), "depends_on", lane_id),
                expected_files=_require_str_list(
                    raw.get("expected_files"), "expected_files", lane_id
                ),
                exclusive_files=_require_str_list(
                    raw.get("exclusive_files"), "exclusive_files", lane_id
                ),
                provides=_require_str_list(raw.get("provides"), "provides", lane_id),
                consumes=_require_str_list(raw.get("consumes"), "consumes", lane_id),
                verification_scope=_require_str_list(
                    raw.get("verification_scope"), "verification_scope", lane_id
                ),
                merge_policy=merge_policy if isinstance(merge_policy, dict) else None,
                verification_commands=_require_dict_list(
                    raw.get("verification_commands"),
                    "verification_commands",
                    lane_id,
                ),
            )
    raise ValueError(
        f"lane {lane_id!r} not found in {LANE_GRAPH_YML}; "
        f"available: {[str(r.get('id')) for r in lanes if isinstance(r, dict) and r.get('id')]}"
    )


def read_task_text(feature_root: Path) -> str:
    """Extract the ``## Tasks`` section body from ``03-tasks.md`` (§7.4).

    The task *text* lives only in the human-readable ``03-tasks.md`` (§7.4 is
    markdown-only, no machine mirror); the machine-readable task list is the
    lane-graph's ``tasks: [TASK-NNN]`` (just ids) and ``task-status.yml`` (just
    status). The implementer leg reads the section body verbatim as the run's
    task - the agent reads it from ``task-package.md`` (where ``prepare_run``
    writes it) and implements it.

    Fail-loud (§24.2) if ``03-tasks.md`` is missing, has no ``## Tasks`` header,
    or the section body is empty - a frozen tasks artifact with no task content
    is a broken precondition, not something to silently pass to the agent.
    """
    path = feature_root / TASKS_MD
    if not path.is_file():
        raise ValueError(f"{TASKS_MD} missing at {path} (§7.4)")
    lines = path.read_text().splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        if line.startswith("## Tasks"):
            start = i + 1
            break
    if start is None:
        raise ValueError(f"no '## Tasks' section in {TASKS_MD} at {path} (§7.4)")
    body = "\n".join(lines[start:]).strip()
    if not body:
        raise ValueError(f"'## Tasks' section in {TASKS_MD} at {path} is empty (§7.4)")
    return body


def lane_allowed_files(lane: LaneEntry) -> list[str]:
    """The run's allowed-files: lane ``expected_files`` + ``exclusive_files``.

    Deduped (a file in both lists is declared once) and sorted, matching the
    v0.1 ``allowed-files.txt`` sort convention so two prepares of the same lane
    produce byte-identical output. The v0.1 ``prepare_run`` seed
    (``output/result.json`` / ``output/result.md``) is added by ``prepare_run``
    itself - this returns only the lane-declared paths.
    """
    seen: set[str] = set()
    combined: list[str] = []
    for entry in list(lane.expected_files) + list(lane.exclusive_files):
        if entry not in seen:
            seen.add(entry)
            combined.append(entry)
    return sorted(combined)


def build_implementer_input_package(
    repo_root: Path, feature_id: str, lane_id: str
) -> str:
    """Build the Implementer input package from frozen artifacts (ticket 01).

    Verifies the §4.2 frozen precondition (tasks + lane-graph both frozen) - the
    implementer leg builds on *frozen* specs, never editable ones - then reads
    the task text from ``03-tasks.md`` and the lane's allowed-files from
    ``04-lane-graph.yml``, and delegates to the v0.1 ``prepare_run`` with the
    role pinned to ``Implementer``. Returns the allocated ``RUN-NNN`` id.

    Reuses ``prepare_run`` unchanged (no new run mechanism): it allocates the
    run id, scaffolds the §12.2 input package, and appends the ``prepare_run``
    audit record. The frozen check happens before any allocation so a rejected
    precondition leaves no partial run behind.
    """
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(
            f"feature run {feature_id} not found under {repo_root}"
        )
    # §4.2: the implementer leg consumes frozen tasks + lane-graph. An unfrozen
    # precondition is rejected before reading anything or allocating a run.
    frozen = frozen_artifacts_status(feature_root)
    if not (frozen.get("tasks") and frozen.get("lane_graph")):
        raise ValueError(
            "implementer leg requires frozen tasks + lane_graph (§4.2); "
            "freeze them at the task gate first"
        )
    task_text = read_task_text(feature_root)
    lane = read_lane_entry(feature_root, lane_id)
    return prepare_run(
        repo_root,
        feature_id,
        _IMPLEMENTER_ROLE,
        task_text,
        allowed_files=lane_allowed_files(lane),
    )


# ---------------------------------------------------------------------------
# Lane-level implement-result rollup (§4.4 double product).
# ---------------------------------------------------------------------------


def _read_json_object(path: Path) -> dict[str, Any] | None:
    """Read a JSON object from ``path``, or ``None`` if missing/invalid.

    Tolerant on purpose: a failed run may have no ``result.json`` (schema check
    already failed in ``validate_run``). The rollup reports what it can rather
    than crashing - the validation verdict already carries the failure.
    """
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _serialize_validation(validation: ValidationResult) -> dict[str, Any]:
    """Render a ``ValidationResult`` as a JSON-serialisable dict for the rollup."""
    return {
        "passed": validation.passed,
        "attempt": validation.attempt,
        "failed_check": validation.failed_check,
        "issues": [asdict(issue) for issue in validation.issues],
    }


def _build_rollup(
    feature_id: str,
    lane_id: str,
    run_id: str,
    result: Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None,
    validation: ValidationResult,
) -> dict[str, Any]:
    """Assemble the ``implement-result.json`` document from run facts.

    Field-complete against the run's ``result.json`` (§13.1) + ``metadata.json``
    (§13.2) + the validation verdict: the agent-declared result fields are
    carried verbatim (status / summary / tasks / related_* / known_issues /
    change_proposals), the wrapper-computed metadata is nested under
    ``run_metadata``, and the validation outcome under ``validation``.
    ``accepted_done`` is structurally ``false`` - the Implementer only proposes
    (§9.2 / invariant #7); final done is the lane gate's verdict (§18.4).
    """
    result = result or {}
    metadata = metadata or {}
    return {
        "feature": feature_id,
        "lane": lane_id,
        "run": run_id,
        "role": _IMPLEMENTER_ROLE,
        "status": result.get("status"),
        "summary": result.get("summary"),
        "tasks": result.get("tasks", []),
        "related_requirements": result.get("related_requirements", []),
        "related_acceptance_criteria": result.get(
            "related_acceptance_criteria", []
        ),
        "known_issues": result.get("known_issues", []),
        "change_proposals": result.get("change_proposals", []),
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
        # §9.2 / invariant #7: the Implementer never announces final done. This
        # is the lane-gate's call (§18.4), not the implementer leg's.
        "accepted_done": False,
    }


def _implement_result_md(rollup: Mapping[str, Any]) -> str:
    """Render the ``implement-result.md`` human-readable mirror (§4.4)."""
    meta = rollup.get("run_metadata") or {}
    val = rollup.get("validation") or {}
    tasks = rollup.get("tasks") or []
    task_lines = "\n".join(
        f"- {t.get('id')}: {t.get('status')} (evidence: {t.get('evidence', [])})"
        for t in tasks
    ) or "_none_"
    issue_lines = (
        "\n".join(
            f"- [{i.get('severity')}] {i.get('check')}: {i.get('message')}"
            for i in val.get("issues", [])
        )
        or "_none_"
    )
    return (
        f"# Implement Result - {rollup.get('lane')}\n"
        f"\n"
        f"- feature: {rollup.get('feature')}\n"
        f"- lane: {rollup.get('lane')}\n"
        f"- run: {rollup.get('run')}\n"
        f"- role: {rollup.get('role')}\n"
        f"- status: {rollup.get('status')}\n"
        f"- accepted_done: {rollup.get('accepted_done')} "
        f"(Implementer only proposes; final done is the lane gate's call, §9.2)\n"
        f"\n"
        f"## Summary\n"
        f"\n"
        f"{rollup.get('summary') or '_none_'}\n"
        f"\n"
        f"## Tasks\n"
        f"\n"
        f"{task_lines}\n"
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
        f"- issues:\n{issue_lines}\n"
        f"\n"
        f"## Related requirements / acceptance criteria\n"
        f"\n"
        f"- requirements: {rollup.get('related_requirements', [])}\n"
        f"- acceptance_criteria: {rollup.get('related_acceptance_criteria', [])}\n"
        f"\n"
        f"## Known issues / change proposals\n"
        f"\n"
        f"- known_issues: {rollup.get('known_issues', [])}\n"
        f"- change_proposals: {rollup.get('change_proposals', [])}\n"
    )


def write_implement_result(
    feature_root: Path,
    lane_id: str,
    *,
    run_id: str,
    result: Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None,
    validation: ValidationResult,
) -> tuple[Path, Path]:
    """Write the lane-level ``implement-result.{md,json}`` rollup (§4.4, §6).

    Rolls the run's ``result.json`` + ``metadata.json`` + validation verdict up
    into the §4.4 double product under ``lanes/<lane_id>/``. The JSON is the
    canonical machine-readable rollup; the markdown is the human mirror. Returns
    ``(md_path, json_path)``. Pure writer: reads nothing from disk beyond what
    the caller passed in (the caller - ``run_implementer_leg`` - already read
    result/metadata), so it is unit-testable from literals.
    """
    feature_id = feature_root.name
    rollup = _build_rollup(feature_id, lane_id, run_id, result, metadata, validation)
    lane_root = feature_root / LANES_DIR / lane_id
    lane_root.mkdir(parents=True, exist_ok=True)
    json_path = lane_root / IMPLEMENT_RESULT_JSON
    md_path = lane_root / IMPLEMENT_RESULT_MD
    json_path.write_text(json.dumps(rollup, indent=2, ensure_ascii=False) + "\n")
    md_path.write_text(_implement_result_md(rollup))
    return md_path, json_path


# ---------------------------------------------------------------------------
# Orchestration: prepare -> run -> validate -> writeback -> rollup.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImplementerLegResult:
    """The implementer leg's return: the captured run + the lane rollup outcome.

    Carries the run identity (``run_id`` / ``lane_id`` / ``feature_id`` /
    ``profile`` / ``exit_code``), the agent-declared ``result_status`` (the
    ``result.json`` status, or ``None`` if the run produced no valid result),
    the full ``validation`` verdict, the ``task_ids_marked`` list (the tasks
    written back to ``proposed_done`` - empty when validation failed, §9.2
    gating), and the paths to the lane-level rollup products.
    """

    run_id: str
    lane_id: str
    feature_id: str
    profile: str
    exit_code: int
    result_status: str | None
    validation: ValidationResult
    task_ids_marked: list[str]
    implement_result_md: Path
    implement_result_json: Path


def _proposed_done_task_ids(result: Mapping[str, Any] | None) -> list[str]:
    """The task ids the run declared ``proposed_done`` (§13.1 ``tasks[]``)."""
    if not result:
        return []
    tasks = result.get("tasks")
    if not isinstance(tasks, list):
        return []
    ids: list[str] = []
    for task in tasks:
        if isinstance(task, dict) and task.get("status") == _PROPOSED_DONE:
            tid = task.get("id")
            if isinstance(tid, str) and tid:
                ids.append(tid)
    return ids


def run_implementer_leg(
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
) -> ImplementerLegResult:
    """Run the full Implementer leg: prepare -> run -> validate -> writeback -> rollup.

    Composes the v0.1 seams unchanged: ``build_implementer_input_package`` (which
    reuses ``prepare_run``), ``run_headless`` (env isolation + capture), and
    ``validate_run`` (the §14 three checks). The §9.2 limits fall out of this
    composition: the boundary is the existing ``validate-run`` (reused, not
    rebuilt), and the ``proposed_done`` writeback is *gated on a passing
    validation* - a boundary-breaching or schema-invalid run never reaches
    canonical ``task-status.yml``. The writeback is also lane-scoped: when the
    lane declares its tasks, a ``proposed_done`` task the model declares outside
    that set is rejected (§24.2) rather than written to canonical status, so the
    model cannot propose work outside its lane. Finally the run's result +
    metadata + verdict are rolled up into
    ``lanes/<lane_id>/implement-result.{md,json}``.

    Returns an ``ImplementerLegResult`` whether the run passed or failed
    validation - the caller (CLI, ticket 06 e2e) decides what to do with a
    failed leg; this function does not raise on a captured run failure (mirrors
    ``run_headless`` / ``validate_run`` returning verdicts rather than raising).
    It *does* raise ``ValueError`` (§24.2) when a passing run declares
    ``proposed_done`` for a task outside the lane's declared task set - that is a
    contract breach, not a captured run failure.
    """
    run_id = build_implementer_input_package(repo_root, feature_id, lane_id)
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
    result = _read_json_object(run_root / OUTPUT_DIR / RESULT_JSON)
    metadata = _read_json_object(run_root / OUTPUT_DIR / METADATA_JSON)
    result_status = result.get("status") if result else None

    # §9.2: the proposed_done writeback happens ONLY when validation passed AND
    # the run declared proposed_done. A failed validation (boundary breach,
    # schema violation, frozen edit) never writes canonical status - the model's
    # claim is not trusted on its own, the deterministic checks gate it.
    task_ids_marked: list[str] = []
    if validation.passed and result_status == _PROPOSED_DONE:
        proposed_ids = _proposed_done_task_ids(result)
        # §9.2 lane scoping: the Implementer may only propose the lane's own
        # tasks. When the lane declares its task set, a proposed_done task
        # outside it is a contract breach (the model declared work outside its
        # lane) -> fail loud (§24.2) rather than silently writing an out-of-lane
        # task to canonical status. A lane with no declared tasks (the Planner
        # has not yet filled ``tasks:``) cannot be scoped, so the check is
        # skipped - the writeback trusts the model's declaration in that case.
        lane = read_lane_entry(feature_root, lane_id)
        if lane.tasks:
            out_of_lane = [t for t in proposed_ids if t not in lane.tasks]
            if out_of_lane:
                raise ValueError(
                    f"run {run_id} declared proposed_done for task(s) "
                    f"{out_of_lane} not in lane {lane_id!r} tasks {lane.tasks} "
                    f"(§9.2 - implementer may only propose the lane's own tasks)"
                )
        for task_id in proposed_ids:
            mark_task_proposed_done(
                feature_root, task_id, lane_id=lane_id, run_id=run_id
            )
            task_ids_marked.append(task_id)

    md_path, json_path = write_implement_result(
        feature_root,
        lane_id,
        run_id=run_id,
        result=result,
        metadata=metadata,
        validation=validation,
    )

    return ImplementerLegResult(
        run_id=run_id,
        lane_id=lane_id,
        feature_id=feature_id,
        profile=profile.name,
        exit_code=run_result.exit_code,
        result_status=result_status,
        validation=validation,
        task_ids_marked=task_ids_marked,
        implement_result_md=md_path,
        implement_result_json=json_path,
    )
