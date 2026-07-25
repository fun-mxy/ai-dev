"""Planner leg — ``generate-requirements`` (v0.6 ticket 02, spec §9.1, ADR-0008).

The Planner (§9.1) is the model role that authors the *id-free* planning
proposals. This module is the planning-leg analogue of ``implement_leg``:
where the implementer leg turns frozen specs into a ``proposed_done``
writeback, the Planner leg turns the feature intent into the canonical
**unfrozen** requirements artifact — through the three-state lifecycle
ADR-0008 D1 fixes:

    proposed (model ``output/``) --promote--> canonical-unfrozen --freeze--> frozen

``run_generate_requirements`` composes the v0.1/v0.2 seams unchanged:

1. **build** the Planner input package by reusing ``prepare_run`` — the role is
   pinned to ``Planner`` and the output-schema is the role-aware requirements
   *proposal* schema (``output_schema_for_role("Planner", stage="requirements")``,
   ticket 01), so the run's ``input/output-schema.json`` is the proposal
   contract ``validate-run`` checks (§14.1). The feature intent (read from
   ``00-intent.md``) is embedded in the task text, so the package is
   self-contained;
2. **run** it headless via ``run_headless`` and **validate** via
   ``validate_run`` (the §14 three-check — schema + boundary + frozen — applied
   unchanged; the §14.4 traceability check is Implementer-only, so the Planner
   role is exempt);
3. **promote** — the deterministic stitcher/renderer (ticket 01) — fires
   *automatically after the run*, **gated on a passing validation** (a
   schema-invalid proposal has no canonical form to promote). promote allocates
   the REQ/AC ids, stitches the AC local refs, and writes the canonical
   ``01-requirements.json`` + renders ``01-requirements.md``. This is the
   canonical-unfrozen state; freeze is the human's call.

Refinement is first-class (ADR-0008 D4): ``generate-requirements --feedback
"…"`` carries the human's note in the input package, and the generate→promote
loop repeats until the human is satisfied — each pass overwriting the unfrozen
artifact (promote targets the *unfrozen* state and refuses a frozen one, §4.2).
The human gate is then ``freeze`` (existing ``status.freeze_artifact``),
which advances ``current_gate`` from ``requirements_gate`` to ``design_gate``.
Requirements is the root (no upstream artifacts), so the freeze-gate coverage
precheck is trivial here; the non-trivial coverage machinery lands in ticket 03.

Scope: the **requirements** stage (ticket 02) and the **design** stage (ticket
03) are wired here. The seam is built so ``generate-tasks`` (ticket 04) reuses the prepare→run→validate
spine with their own proposal schema + promote
function + upstream id-set (frozen REQ+DES).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from ai_dev.json_artifact import read_json_object
from ai_dev.paths import OUTPUT_DIR, RESULT_JSON, feature_dir, run_dir
from ai_dev.promote import (
    PromoteResult,
    promote_design,
    promote_requirements,
    promote_tasks,
    read_frozen_design_doc,
    read_frozen_requirements_doc,
)
from ai_dev.profiles import AgentProfile
from ai_dev.run_prepare import prepare_run, output_schema_for_role
from ai_dev.run_wrapper import (
    DEFAULT_MAX_TURNS,
    DEFAULT_PERMISSION_MODE,
    run_headless,
)
from ai_dev.validate import ValidationResult, validate_run

# The model role this leg prepares (§9.1). Pinned, not caller-supplied: the
# requirements generator is the Planner role by definition. Mirrors the
# ``"You are the {role} for {run_id}."`` token ``prepare_run`` writes to
# ``role.md`` — and the §14.4 traceability check keys off this string, so it
# must match ``PLANNER_ROLE`` in ``planner_schemas``.
from ai_dev.planner_schemas import PLANNER_ROLE

# The §7.1 intent file (written by ``create_feature_run``). The Planner reads
# the feature's original intent to author requirements from; embedded into the
# task text at prepare time so the run stays self-contained (the intent lives
# at the feature root, outside the run's write boundary).
_INTENT_FILE = "00-intent.md"

# The §7.1 prose-section header the intent lives under. Extracted (not the
# whole file) so the Planner sees the raw user intent, not the captured-at
# timestamp metadata.
_INTENT_HEADER = "## Original intent"

# The planning stage this leg produces (one of ``PLANNING_STAGES``). Carried on
# the result and threaded into the proposal-schema lookup so ``generate-design``
# (03) / ``generate-tasks`` (04) swap one constant for their stage.
_STAGE_REQUIREMENTS = "requirements"

# The design stage (ticket 03). Carried on the design leg's result and threaded
# into the proposal-schema lookup (``output_schema_for_role(..., stage="design")``).
_STAGE_DESIGN = "design"

# The tasks stage (ticket 04). Carried on the tasks leg's result and threaded
# into the proposal-schema lookup (``output_schema_for_role(..., stage="tasks")``).
_STAGE_TASKS = "tasks"


def read_intent(feature_root: Path) -> str:
    """Extract the original user intent from ``00-intent.md`` (§7.1).

    The intent was recorded verbatim under the ``## Original intent (原始需求)``
    header at feature-run creation. The Planner authors requirements *from* that
    intent, so the generator embeds it in the task text. Returns the section
    body (stripped); fail-loud (§24.2) if the intent file or section is missing
    or empty — a requirements gate with no intent to elaborate from is a broken
    precondition, not something to hand the model silently.
    """
    path = feature_root / _INTENT_FILE
    if not path.is_file():
        raise ValueError(f"{_INTENT_FILE} missing at {path} (§7.1)")
    lines = path.read_text().splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        if line.startswith(_INTENT_HEADER):
            start = i + 1
            break
    if start is None:
        raise ValueError(
            f"no '{_INTENT_HEADER}' section in {_INTENT_FILE} at {path} (§7.1)"
        )
    body = "\n".join(lines[start:]).strip()
    if not body:
        raise ValueError(
            f"'{_INTENT_HEADER}' section in {_INTENT_FILE} at {path} is empty (§7.1)"
        )
    return body


def _planner_task_text(
    feature_id: str, intent: str, feedback: str | None
) -> str:
    """The Planner requirements task: author an id-free proposal from the intent.

    Carries the feature intent (what to elaborate) + the optional human feedback
    (ADR-0008 D4 refinement channel), then instructs the Planner to emit a
    requirements proposal conforming to ``input/output-schema.json`` —
    **id-free** content with *local* ``key`` refs (the model never assigns
    canonical REQ/AC ids; promote allocates them, D2). The schema
    (``REQUIREMENTS_PROPOSAL_SCHEMA``, ticket 01) is the contract; this text
    only makes the model's job and the id-free rule explicit so the proposal is
    promote-able on the first pass (the ticket-02 model de-risk).
    """
    blocks = [
        f"Author the requirements proposal for feature {feature_id} (§9.1, Planner).",
        "",
        "## Feature intent (原始需求, from 00-intent.md)",
        "",
        intent,
    ]
    if feedback is not None and feedback.strip():
        blocks += [
            "",
            "## Human feedback (refinement — revise the proposal accordingly)",
            "",
            feedback.strip(),
        ]
    blocks += [
        "",
        "## Your role: Planner (§9.1)",
        "",
        "You author the **requirements** proposal as structured JSON in "
        "`output/result.json` conforming to `input/output-schema.json`. This is "
        "the mandatory final step.",
        "",
        "Rules (ADR-0008 D2 — promote allocates the ids; you do NOT):",
        "- Do NOT assign canonical stable ids (no REQ-NNN / AC-NNN). Each "
        "requirement and acceptance criterion carries a *local* `key` (a short "
        "stable handle you invent, e.g. `\"r1\"`).",
        "- Each acceptance criterion's `requirement` is the *local key* of the "
        "requirement it traces to (e.g. `\"r1\"`), NOT a canonical id. promote "
        "resolves it.",
        "- `requirements[]` needs a non-empty `key` and `statement`; "
        "`acceptance_criteria[]` needs a non-empty `key`, `requirement` (local "
        "ref), and `criterion`.",
        "- A proposal is expected to be *incomplete while being refined* — emit "
        "your current best proposal; coverage-completeness is checked later at "
        "the freeze gate, not here. But every `requirement` ref you DO write must "
        "point at a real `key` you defined (reference-integrity, checked at "
        "promote).",
        "- Optional prose facets (`priority` / `scope` / `constraints` / "
        "`open_questions`) may be omitted.",
        "",
        "Write `output/result.md` (a short human-readable summary) and "
        "`output/result.json` (the proposal). Stop once result.json is written.",
    ]
    return "\n".join(blocks)


def build_requirements_input_package(
    repo_root: Path,
    feature_id: str,
    *,
    feedback: str | None = None,
    origin: str | None = None,
) -> str:
    """Build the Planner requirements input package (§9.1, ADR-0008 D2/D4).

    Reads the feature intent from ``00-intent.md`` (fail-loud if missing),
    renders the Planner task text (intent + optional feedback), and delegates
    to the v0.1 ``prepare_run`` with the role pinned to ``Planner`` and the
    output-schema pinned to the requirements *proposal* schema
    (``output_schema_for_role("Planner", stage="requirements")``). The Planner
    authors only ``output/result.{json,md}`` (no workspace files), so no
    task-specific allowed-files are declared — the ``prepare_run`` seed
    (``output/result.json`` + ``output/result.md``) is the whole boundary.

    Returns the allocated ``RUN-NNN`` id. Reuses ``prepare_run`` unchanged (no
    new run mechanism): it allocates the run id, scaffolds the §12.2 input
    package, and appends the ``prepare_run`` audit record. The intent read
    happens before any allocation so a missing-intent rejection leaves no
    partial run behind.
    """
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id} not found under {repo_root}")
    intent = read_intent(feature_root)
    task_text = _planner_task_text(feature_id, intent, feedback)
    return prepare_run(
        repo_root,
        feature_id,
        PLANNER_ROLE,
        task_text,
        # No task-specific allowed files: the Planner writes only the mandatory
        # result.{json,md} (the ``prepare_run`` seed). The proposal schema is the
        # role-aware §14.1 contract (ticket 01).
        output_schema=output_schema_for_role(PLANNER_ROLE, stage=_STAGE_REQUIREMENTS),
        origin=origin,
    )


# ---------------------------------------------------------------------------
# Orchestration: prepare -> run -> validate -> promote (gated on validation).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannerLegResult:
    """The Planner leg's return: the captured run + the promote outcome.

    Carries the run identity (``run_id`` / ``feature_id`` / ``profile`` /
    ``stage`` / ``exit_code``), the full ``validation`` verdict, and the
    ``promote`` result — ``None`` when validation failed (a schema-invalid
    proposal has no canonical form to promote, so no canonical write happens).
    ``promoted`` is the convenience boolean the CLI/deciders read.
    """

    run_id: str
    feature_id: str
    profile: str
    stage: str
    exit_code: int
    validation: ValidationResult
    promote: PromoteResult | None

    @property
    def promoted(self) -> bool:
        """Whether promote fired (validation passed + canonical artifact written)."""
        return self.promote is not None


def _run_planner_leg(
    repo_root: Path,
    feature_id: str,
    profile: AgentProfile,
    *,
    build_input_package: Callable[..., str],
    promote: Callable[..., PromoteResult],
    stage: str,
    feedback: str | None = None,
    claude_path: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    started_at: str | None = None,
    ended_at: str | None = None,
    origin: str | None = None,
) -> PlannerLegResult:
    """Run a Planner leg end to end: build input -> run -> validate -> promote.

    Composes the v0.1/v0.2 seams unchanged: ``build_input_package`` (which
    reuses ``prepare_run`` with the role-aware proposal schema), ``run_headless``
    (env isolation + capture), and ``validate_run`` (the §14 three-check).
    ``promote`` — the deterministic stitcher/renderer — then fires
    **automatically** — gated on a passing validation, exactly as the
    implementer leg gates its ``proposed_done`` writeback: a schema-invalid or
    boundary-breaching proposal never reaches the canonical artifact.

    Returns a ``PlannerLegResult`` whether the run passed or failed validation
    (mirrors ``run_implementer_leg`` / ``run_headless`` returning verdicts
    rather than raising on a captured run failure). promote errors
    (``UnresolvedRefError`` / ``FrozenArtifactWriteError``) propagate — a
    validation-passing run whose proposal promote cannot stitch is a malformed
    proposal (§24.2), reported loud rather than silently dropped.
    """
    run_id = build_input_package(
        repo_root, feature_id, feedback=feedback, origin=origin
    )
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
        origin=origin,
    )
    validation = validate_run(repo_root, feature_id, run_id, origin=origin)

    feature_root = feature_dir(repo_root, feature_id)
    run_root = run_dir(repo_root, feature_id, run_id)

    promote_result: PromoteResult | None = None
    # promote fires only on a passing validation — a schema-invalid or
    # boundary-breaching proposal has no canonical form. The proposal IS the
    # run's result.json (the §13.1 output, validated above against the proposal
    # schema). read_json_object returns None on a missing/malformed result.json;
    # validation-passed already guarantees a schema-valid object, but the guard
    # is belt-and-braces against a race.
    if validation.passed:
        proposal = read_json_object(run_root / OUTPUT_DIR / RESULT_JSON)
        if proposal is not None:
            promote_result = promote(
                feature_root,
                feature_id,
                proposal,
                origin=origin,
            )

    return PlannerLegResult(
        run_id=run_id,
        feature_id=feature_id,
        profile=profile.name,
        stage=stage,
        exit_code=run_result.exit_code,
        validation=validation,
        promote=promote_result,
    )


def run_generate_requirements(
    repo_root: Path,
    feature_id: str,
    profile: AgentProfile,
    *,
    feedback: str | None = None,
    claude_path: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    started_at: str | None = None,
    ended_at: str | None = None,
    origin: str | None = None,
) -> PlannerLegResult:
    """Run the Planner requirements leg (ticket 02): prepare -> run -> validate -> promote.

    promote allocates REQ/AC ids, stitches the AC local refs (reference-
    integrity, D3), and writes ``01-requirements.json`` + renders
    ``01-requirements.md`` — the canonical-unfrozen state. ``feedback`` threads
    the human's refinement note (ADR-0008 D4); re-running overwrites the
    unfrozen artifact (promote refuses a frozen one, §4.2). Requirements is the
    root stage (no upstream artifacts).
    """
    return _run_planner_leg(
        repo_root,
        feature_id,
        profile,
        build_input_package=build_requirements_input_package,
        promote=promote_requirements,
        stage=_STAGE_REQUIREMENTS,
        feedback=feedback,
        claude_path=claude_path,
        max_turns=max_turns,
        permission_mode=permission_mode,
        started_at=started_at,
        ended_at=ended_at,
        origin=origin,
    )


# ---------------------------------------------------------------------------
# Design stage (ticket 03): generate-design. The Planner authors the design
# *against* the frozen requirements (the upstream) - the first stage with a real
# frozen upstream, so the leg reads + freezes-checks the requirements before the
# run and promote_design stitches requirement_mapping against them.
# ---------------------------------------------------------------------------


def _render_frozen_requirements_summary(req_doc: Mapping[str, Any]) -> str:
    """Render the frozen requirements as a compact block for the design task text.

    The Planner authors the design *against* the frozen requirements, so it must
    see the canonical REQ/AC ids to reference: a design proposal's
    ``requirement_mapping`` references REQs by their canonical ``REQ-NNN`` (read
    here), which promote resolves against the frozen upstream. ACs are nested
    under their stitched REQ for readability. Returns ``"- (none)"`` for an empty
    (but frozen) requirements artifact.
    """
    reqs = req_doc.get("requirements", []) or []
    acs = req_doc.get("acceptance_criteria", []) or []
    acs_by_req: dict[str, list[str]] = {}
    for ac in acs:
        if isinstance(ac, Mapping):
            ref = str(ac.get("requirement", ""))
            acs_by_req.setdefault(ref, []).append(
                f"{ac.get('id', '?')}: {ac.get('criterion', '')}"
            )
    lines: list[str] = []
    for req in reqs:
        if not isinstance(req, Mapping):
            continue
        rid = req.get("id", "?")
        lines.append(f"- {rid}: {req.get('statement', '')}")
        for ac_line in acs_by_req.get(str(rid), []):
            lines.append(f"  - AC {ac_line}")
    return "\n".join(lines) if lines else "- (none)"


def _design_task_text(
    feature_id: str,
    intent: str,
    req_summary: str,
    feedback: str | None,
) -> str:
    """The Planner design task: author an id-free design proposal from the intent
    + frozen requirements.

    Carries the feature intent (the original goal) + the frozen requirements (the
    upstream REQ-NNN ids to map design elements against) + the optional human
    feedback (ADR-0008 D4 refinement channel), then instructs the Planner to emit
    a design proposal conforming to ``input/output-schema.json`` - **id-free**
    content: local ``key`` handles for design elements, canonical ``REQ-NNN``
    refs (from the frozen upstream) in ``requirement_mapping``. The schema
    (``DESIGN_PROPOSAL_SCHEMA``, ticket 03) is the contract; this text makes the
    model's job and the ref rules explicit so the proposal is promote-able on the
    first pass (mirrors the ticket-02 model de-risk).
    """
    blocks = [
        f"Author the design proposal for feature {feature_id} (§9.1, Planner).",
        "",
        "## Feature intent (原始需求, from 00-intent.md)",
        "",
        intent,
        "",
        "## Frozen requirements (the upstream - 01-requirements.json)",
        "",
        "The requirements below are FROZEN. Reference each requirement in your",
        "`requirement_mapping` by its canonical REQ-NNN id (e.g. `REQ-001`).",
        "",
        req_summary,
    ]
    if feedback is not None and feedback.strip():
        blocks += [
            "",
            "## Human feedback (refinement - revise the proposal accordingly)",
            "",
            feedback.strip(),
        ]
    blocks += [
        "",
        "## Your role: Planner (§9.1)",
        "",
        "You author the **design** proposal as structured JSON in "
        "`output/result.json` conforming to `input/output-schema.json`. This is "
        "the mandatory final step.",
        "",
        "Rules (ADR-0008 D2 - promote allocates the ids; you do NOT):",
        "- Do NOT assign canonical stable ids (no DES-NNN). Each design element "
        "carries a *local* `key` (a short stable handle you invent, e.g. "
        '`"d1"`) and a `name`.',
        "- Each `requirement_mapping` entry's `requirement` is the **canonical "
        "REQ-NNN** of a frozen requirement (read from the list above) - NOT a "
        "local key. promote resolves it against the frozen upstream.",
        "- Each `requirement_mapping` entry's `design_elements` is a list of the "
        "*local keys* of the design elements that realize that REQ "
        '(e.g. `["d1", "d2"]`).',
        "- `design_elements[]` needs a non-empty `key` and `name`; "
        "`requirement_mapping[]` needs a non-empty `key`, `requirement` (a real "
        "frozen REQ-NNN), and `design_elements` (a list of your local keys).",
        "- A proposal is expected to be *incomplete while being refined* - emit "
        "your current best proposal; coverage-completeness (every REQ mapped) is "
        "checked later at the freeze gate, not here. But every `requirement` ref "
        "you DO write must point at a real frozen REQ-NNN and every "
        "`design_elements` member at a real local `key` you defined "
        "(reference-integrity, checked at promote).",
        "- Optional prose facets (`architecture_decision` / `data_model` / "
        "`api_cli_contract` / `file_layout` / `invariants` / `risks` / "
        "`dependencies`) may be omitted or structured freely.",
        "",
        "Write `output/result.md` (a short human-readable summary) and "
        "`output/result.json` (the proposal). Stop once result.json is written.",
    ]
    return "\n".join(blocks)


def build_design_input_package(
    repo_root: Path,
    feature_id: str,
    *,
    feedback: str | None = None,
    origin: str | None = None,
) -> str:
    """Build the Planner design input package (§9.1, ADR-0008 D2/D4).

    Reads the feature intent from ``00-intent.md`` and the **frozen**
    requirements from ``01-requirements.json`` (fail-loud if requirements is not
    frozen - design may only stitch against a frozen upstream, ADR-0008 D2),
    renders the Planner design task text (intent + frozen-requirements summary +
    optional feedback), and delegates to ``prepare_run`` with the role pinned to
    ``Planner`` and the output-schema pinned to the design *proposal* schema
    (``output_schema_for_role("Planner", stage="design")``). The Planner authors
    only ``output/result.{json,md}`` (no workspace files), so no task-specific
    allowed-files are declared. Returns the allocated ``RUN-NNN`` id.

    The intent + frozen-requirements reads happen before any allocation so a
    missing-intent or not-frozen-requirements rejection leaves no partial run
    behind (mirrors ``build_requirements_input_package``).
    """
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id} not found under {repo_root}")
    intent = read_intent(feature_root)
    req_doc = read_frozen_requirements_doc(feature_root)
    req_summary = _render_frozen_requirements_summary(req_doc)
    task_text = _design_task_text(feature_id, intent, req_summary, feedback)
    return prepare_run(
        repo_root,
        feature_id,
        PLANNER_ROLE,
        task_text,
        # No task-specific allowed files: the Planner writes only the mandatory
        # result.{json,md} (the prepare_run seed). The design proposal schema is
        # the role-aware §14.1 contract (ticket 03).
        output_schema=output_schema_for_role(PLANNER_ROLE, stage=_STAGE_DESIGN),
        origin=origin,
    )


def run_generate_design(
    repo_root: Path,
    feature_id: str,
    profile: AgentProfile,
    *,
    feedback: str | None = None,
    claude_path: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    started_at: str | None = None,
    ended_at: str | None = None,
    origin: str | None = None,
) -> PlannerLegResult:
    """Run the Planner design leg (ticket 03): prepare -> run -> validate -> promote.

    promote allocates DES ids, stitches each ``requirement_mapping`` entry's
    ``requirement`` ref against the frozen REQ upstream and its
    ``design_elements`` local refs to allocated DES ids (reference-integrity,
    D3), and writes ``02-design.json`` + renders ``02-design.md``. ``feedback``
    threads the refinement note (ADR-0008 D4); re-running overwrites the
    unfrozen artifact. Requires the requirements artifact frozen.
    """
    return _run_planner_leg(
        repo_root,
        feature_id,
        profile,
        build_input_package=build_design_input_package,
        promote=promote_design,
        stage=_STAGE_DESIGN,
        feedback=feedback,
        claude_path=claude_path,
        max_turns=max_turns,
        permission_mode=permission_mode,
        started_at=started_at,
        ended_at=ended_at,
        origin=origin,
    )


# ---------------------------------------------------------------------------
# Tasks stage (ticket 04): generate-tasks. The Planner authors the tasks
# *against* the frozen requirements AND design (TWO upstreams - the first stage
# with two), so the leg reads + freeze-checks both before the run and
# promote_tasks stitches each task's related_requirements / related_design
# against them.
# ---------------------------------------------------------------------------


def _render_frozen_design_summary(des_doc: Mapping[str, Any]) -> str:
    """Render the frozen design as a compact block for the tasks task text.

    The Planner authors tasks *against* the frozen design, so it must see the
    canonical DES-NNN ids to reference in each task's ``related_design``. Renders
    the design elements (id + name), annotated with which frozen REQs each DES
    realizes (from ``requirement_mapping``) so the Planner can pick each task's
    ``related_design`` to match its ``related_requirements``. Returns ``"- (none)"``
    for an empty (but frozen) design artifact.
    """
    elements = des_doc.get("design_elements", []) or []
    mapping = des_doc.get("requirement_mapping", []) or []
    # Which frozen REQs each DES realizes (the mapping's design_elements are the
    # stitched canonical DES ids promote wrote).
    reqs_by_des: dict[str, list[str]] = {}
    for m in mapping:
        if not isinstance(m, Mapping):
            continue
        req = str(m.get("requirement", ""))
        for d in m.get("design_elements", []) or []:
            if isinstance(d, str):
                reqs_by_des.setdefault(d, []).append(req)
    lines: list[str] = []
    for el in elements:
        if not isinstance(el, Mapping):
            continue
        did = str(el.get("id", "?"))
        name = el.get("name", "")
        reqs = reqs_by_des.get(did, [])
        if reqs:
            lines.append(f"- {did}: {name} (realizes {', '.join(reqs)})")
        else:
            lines.append(f"- {did}: {name}")
    return "\n".join(lines) if lines else "- (none)"


def _tasks_task_text(
    feature_id: str,
    intent: str,
    req_summary: str,
    des_summary: str,
    feedback: str | None,
) -> str:
    """The Planner tasks task: author an id-free tasks proposal from the intent +
    frozen requirements + frozen design.

    Carries the feature intent (the original goal) + the frozen requirements (the
    upstream REQ-NNN ids) + the frozen design (the upstream DES-NNN ids) + the
    optional human feedback (ADR-0008 D4 refinement channel), then instructs the
    Planner to emit a tasks proposal conforming to ``input/output-schema.json`` -
    **id-free** content: a top-level ``lane_purpose`` (the single MVP lane's
    purpose) + local ``key`` handles per task, canonical ``REQ-NNN`` / ``DES-NNN``
    refs (from the frozen upstreams) in each task's ``related_requirements`` /
    ``related_design``. The schema (``TASKS_PROPOSAL_SCHEMA``, ticket 04) is the
    contract; this text makes the model's job and the ref rules explicit so the
    proposal is promote-able on the first pass (mirrors the ticket-02/03 de-risk).
    """
    blocks = [
        f"Author the tasks proposal for feature {feature_id} (§9.1, Planner).",
        "",
        "## Feature intent (原始需求, from 00-intent.md)",
        "",
        intent,
        "",
        "## Frozen requirements (upstream 1 - 01-requirements.json)",
        "",
        "The requirements below are FROZEN. Reference each requirement in a task's",
        "`related_requirements` by its canonical REQ-NNN id (e.g. `REQ-001`).",
        "",
        req_summary,
        "",
        "## Frozen design (upstream 2 - 02-design.json)",
        "",
        "The design elements below are FROZEN. Reference each design element in a",
        "task's `related_design` by its canonical DES-NNN id (e.g. `DES-001`).",
        "",
        des_summary,
    ]
    if feedback is not None and feedback.strip():
        blocks += [
            "",
            "## Human feedback (refinement - revise the proposal accordingly)",
            "",
            feedback.strip(),
        ]
    blocks += [
        "",
        "## Your role: Planner (§9.1)",
        "",
        "You author the **tasks** proposal as structured JSON in "
        "`output/result.json` conforming to `input/output-schema.json`. This is "
        "the mandatory final step.",
        "",
        "Rules (ADR-0008 D2 - promote allocates the ids; you do NOT):",
        "- Do NOT assign canonical stable ids (no TASK-NNN). Each task carries a "
        "*local* `key` (a short stable handle you invent, e.g. `\"t1\"`), a "
        "`summary`, and its REQ/DES refs.",
        "- Each task's `related_requirements` is a list of the **canonical "
        "REQ-NNN** ids of the frozen requirements it realizes (read from the list "
        "above) - NOT local keys. promote resolves them against the frozen "
        "upstream.",
        "- Each task's `related_design` is a list of the **canonical DES-NNN** ids "
        "of the frozen design elements it implements (read from the list above).",
        "- The top-level `lane_purpose` is a single sentence: the purpose of the "
        "one MVP lane all tasks run on (the lane itself is structural; you do NOT "
        "assign lanes).",
        "- Each task also declares `expected_files` and `exclusive_files` - the "
        "file paths it will touch. These are **RUN-relative paths under `workspace/`** "
        "(the Implementer writes there): e.g. `workspace/<pkg>/cli.py`, "
        "`workspace/tests/test_<x>.py`. The file-boundary check (§14.2) is an exact "
        "match against these, so the `workspace/` prefix is mandatory; an unprefixed "
        "path like `<pkg>/cli.py` will be rejected. List every file the task touches, "
        "including `workspace/<pkg>/__init__.py` when you create a package.",
        "- Declare the lane's top-level `verification_commands`: the shell "
        "commands the Verifier (§9.5) runs to prove the lane works. Each entry "
        "is `{\"name\": <label>, \"command\": <shell string>}`. The commands run "
        "with the implementer run's `workspace/` as the working directory, where "
        "the implemented package + `tests/` live - so emit **workspace-relative** "
        "commands. Use exactly this two-command set for a Python package "
        "`<pkg>` with a `tests/` dir (substitute the real package name):\n"
        "  - `{\"name\": \"pytest\", \"command\": \"PYTHONPATH=. python -m pytest "
        "-q -p no:cacheprovider -c /dev/null tests\"}`\n"
        "  - `{\"name\": \"mypy\", \"command\": \"python -m mypy <pkg>\"}`\n"
        "  These are the commands the Verifier executes; if you omit "
        "`verification_commands` the Verifier fails loud (no verify command set "
        "declared), so always emit them.",
        "- `tasks[]` needs a non-empty `key`, `summary`, `related_requirements` "
        "(real frozen REQ-NNN ids), `related_design` (real frozen DES-NNN ids), "
        "`expected_files`, and `exclusive_files`.",
        "- A proposal is expected to be *incomplete while being refined* - emit "
        "your current best proposal; coverage-completeness (every REQ+DES covered "
        "by some task) is checked later at the freeze gate, not here. But every "
        "`related_requirements` / `related_design` ref you DO write must point at "
        "a real frozen REQ-NNN / DES-NNN (reference-integrity, checked at promote).",
        "- Optional prose facets (`description` / `verification`) may be omitted "
        "per task.",
        "",
        "Write `output/result.md` (a short human-readable summary) and "
        "`output/result.json` (the proposal). Stop once result.json is written.",
    ]
    return "\n".join(blocks)


def build_tasks_input_package(
    repo_root: Path,
    feature_id: str,
    *,
    feedback: str | None = None,
    origin: str | None = None,
) -> str:
    """Build the Planner tasks input package (§9.1, ADR-0008 D2/D4).

    Reads the feature intent from ``00-intent.md`` and the **frozen** requirements
    AND design (fail-loud if either is not frozen - tasks may only stitch against
    frozen upstreams, ADR-0008 D2), renders the Planner tasks task text (intent +
    frozen-requirements summary + frozen-design summary + optional feedback), and
    delegates to ``prepare_run`` with the role pinned to ``Planner`` and the
    output-schema pinned to the tasks *proposal* schema
    (``output_schema_for_role("Planner", stage="tasks")``). The Planner authors
    only ``output/result.{json,md}`` (no workspace files), so no task-specific
    allowed-files are declared. Returns the allocated ``RUN-NNN`` id.

    The intent + frozen-upstream reads happen before any allocation so a
    missing-intent or not-frozen-upstream rejection leaves no partial run behind
    (mirrors ``build_design_input_package``).
    """
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id} not found under {repo_root}")
    intent = read_intent(feature_root)
    req_doc = read_frozen_requirements_doc(feature_root)
    des_doc = read_frozen_design_doc(feature_root)
    req_summary = _render_frozen_requirements_summary(req_doc)
    des_summary = _render_frozen_design_summary(des_doc)
    task_text = _tasks_task_text(
        feature_id, intent, req_summary, des_summary, feedback
    )
    return prepare_run(
        repo_root,
        feature_id,
        PLANNER_ROLE,
        task_text,
        # No task-specific allowed files: the Planner writes only the mandatory
        # result.{json,md} (the prepare_run seed). The tasks proposal schema is
        # the role-aware §14.1 contract (ticket 04).
        output_schema=output_schema_for_role(PLANNER_ROLE, stage=_STAGE_TASKS),
        origin=origin,
    )


def run_generate_tasks(
    repo_root: Path,
    feature_id: str,
    profile: AgentProfile,
    *,
    feedback: str | None = None,
    claude_path: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    started_at: str | None = None,
    ended_at: str | None = None,
    origin: str | None = None,
) -> PlannerLegResult:
    """Run the Planner tasks leg (ticket 04): prepare -> run -> validate -> promote.

    promote allocates TASK ids, stitches each task's ``related_requirements`` /
    ``related_design`` refs against the frozen REQ / DES upstreams (reference-
    integrity, D3), and writes ``03-tasks.json`` + renders ``03-tasks.md`` +
    seeds ``status/task-status.yml`` + populates ``04-lane-graph.yml``.
    ``feedback`` threads the refinement note (ADR-0008 D4); re-running
    overwrites the unfrozen artifact. Requires requirements AND design frozen.
    """
    return _run_planner_leg(
        repo_root,
        feature_id,
        profile,
        build_input_package=build_tasks_input_package,
        promote=promote_tasks,
        stage=_STAGE_TASKS,
        feedback=feedback,
        claude_path=claude_path,
        max_turns=max_turns,
        permission_mode=permission_mode,
        started_at=started_at,
        ended_at=ended_at,
        origin=origin,
    )
