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

Scope: the **requirements** stage only. The seam is built so ``generate-design``
(ticket 03) and ``generate-tasks`` (ticket 04) reuse the prepare→run→validate
spine with their own proposal schema + promote function + upstream id-set.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ai_dev.json_artifact import read_json_object
from ai_dev.paths import OUTPUT_DIR, RESULT_JSON, feature_dir, run_dir
from ai_dev.promote import PromoteResult, promote_requirements
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
_STAGE = "requirements"


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
        output_schema=output_schema_for_role(PLANNER_ROLE, stage=_STAGE),
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
    """Run the full Planner requirements leg: prepare -> run -> validate -> promote.

    Composes the v0.1/v0.2 seams unchanged: ``build_requirements_input_package``
    (which reuses ``prepare_run`` with the role-aware proposal schema),
    ``run_headless`` (env isolation + capture), and ``validate_run`` (the §14
    three-check). promote (the deterministic stitcher/renderer, ticket 01) then
    fires **automatically** — gated on a passing validation, exactly as the
    implementer leg gates its ``proposed_done`` writeback: a schema-invalid or
    boundary-breaching proposal never reaches the canonical artifact. promote
    allocates REQ/AC ids, stitches the AC local refs (reference-integrity, D3),
    and writes ``01-requirements.json`` + renders ``01-requirements.md`` — the
    canonical-unfrozen state. ``feedback`` threads the human's refinement note
    (ADR-0008 D4); re-running overwrites the unfrozen artifact (promote refuses
    a frozen one, §4.2).

    Returns a ``PlannerLegResult`` whether the run passed or failed validation
    (mirrors ``run_implementer_leg`` / ``run_headless`` returning verdicts
    rather than raising on a captured run failure). promote errors
    (``UnresolvedRefError`` / ``FrozenArtifactWriteError``) propagate — a
    validation-passing run whose proposal promote cannot stitch is a malformed
    proposal (§24.2), reported loud rather than silently dropped.
    """
    run_id = build_requirements_input_package(
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
            promote_result = promote_requirements(
                feature_root,
                feature_id,
                proposal,
                origin=origin,
            )

    return PlannerLegResult(
        run_id=run_id,
        feature_id=feature_id,
        profile=profile.name,
        stage=_STAGE,
        exit_code=run_result.exit_code,
        validation=validation,
        promote=promote_result,
    )
