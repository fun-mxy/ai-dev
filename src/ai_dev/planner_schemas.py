"""Planner proposal output-schemas + role-aware §14.1 lookup (v0.6 ticket 01).

The Planner (spec §9.1) is a *model* role, so like every agent role its run
authors ``output/result.json`` and the wrapper validates it with the §14.1
schema check. What differs from the implementer is the *shape* of that result:
a Planner run produces an **id-free proposal** (structured content with *local*
references to upstream items), not the implementer's ``{status, summary,
tasks}`` rollup. ADR-0008 D2 / Consequences: "Output-schema validation (§14.1)
becomes **role-aware** (Planner proposal schemas differ from the implementer's
``result.json``); the 3-check validation otherwise applies unchanged."

So this module is the data home for the Planner proposal schemas plus the
role-aware lookup that picks one. Two consumers:

* **prepare-time** (ticket 02's ``generate-requirements``): the planning-leg
  driver calls ``output_schema_for_role("Planner", stage="requirements")`` and
  passes it to ``prepare_run(..., output_schema=...)``, so the run's
  ``input/output-schema.json`` is the proposal contract ``validate-run`` will
  check (§14.1) — reusing the run mechanism, not building a new one.
* **validate-time**: ``validate_run`` already reads whatever
  ``input/output-schema.json`` was written, so it needs no change — the
  role-awareness lives entirely in *which* schema gets written at prepare time.

Only the **requirements** and **design** proposal schemas are concretely defined here
(requirements in ticket 01, design in ticket 03). The tasks (ticket 04) proposal schema
is added to ``_PLANNER_PROPOSAL_SCHEMAS`` as a one-line data entry when that ticket
lands; the lookup mechanism and the promote seam (``promote``) need no rework then.

The schemas use only the JSON Schema subset ``validate.validate_against_schema``
hand-rolls (``type`` / ``required`` / ``enum`` / ``minLength`` / ``minItems`` /
``properties`` / ``items`` / ``additionalProperties``) — so the same validator
that checks an implementer ``result.json`` checks a Planner proposal, with no
new validation machinery.
"""

from __future__ import annotations

from typing import Any, Mapping

# The model role that authors planning proposals (§9.1). Mirrors the
# ``"You are the {role} for {run_id}."`` token written to ``role.md``.
PLANNER_ROLE = "Planner"

# The three planning stages (§23.5 steps 3/5/7), each producing one proposal
# artifact. Public so callers reference stage names from one source of truth.
PLANNING_STAGES: tuple[str, ...] = ("requirements", "design", "tasks")

# Each requirement entry in a requirements proposal. ``key`` is the proposal's
# *local* handle (the model never assigns the canonical REQ-NNN); acceptance
# criteria reference a requirement by that key, and ``promote`` resolves it
# (ADR-0008 D2). ``statement`` is the one field a requirement cannot lack.
_REQUIREMENT_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["key", "statement"],
    "additionalProperties": True,
    "properties": {
        "key": {"type": "string", "minLength": 1},
        "statement": {"type": "string", "minLength": 1},
        "priority": {"type": "string"},
        "rationale": {"type": "string"},
    },
}

# Each acceptance-criterion entry. ``requirement`` is a *local ref* to a
# requirement's ``key`` — required, because an AC with no traced REQ is a
# coverage hole (ADR-0007), and making it required means a missing trace fails
# the §14.1 schema check at validate-run before promote even runs. ``promote``
# then resolves that local ref to the allocated REQ-NNN (reference-integrity,
# ADR-0008 D3) and fails loud if it does not resolve.
_ACCEPTANCE_CRITERION_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["key", "requirement", "criterion"],
    "additionalProperties": True,
    "properties": {
        "key": {"type": "string", "minLength": 1},
        "requirement": {"type": "string", "minLength": 1},
        "criterion": {"type": "string", "minLength": 1},
    },
}

# ADR-0008 D2: the requirements proposal — id-free structured JSON. ``requirements``
# and ``acceptance_criteria`` are required arrays (the structure must be present),
# but neither carries a ``minItems``: a proposal is *expected to be incomplete*
# while being refined (D3), and coverage-completeness is a freeze-gate concern,
# not a §14.1 schema concern. The remaining §7.2 facets (priority / scope /
# constraints / open_questions) are optional prose a refinement draft may omit.
REQUIREMENTS_PROPOSAL_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "PlannerRequirementsProposal",
    "type": "object",
    "required": ["requirements", "acceptance_criteria"],
    "additionalProperties": True,
    "properties": {
        "requirements": {
            "type": "array",
            "items": _REQUIREMENT_ITEM_SCHEMA,
        },
        "acceptance_criteria": {
            "type": "array",
            "items": _ACCEPTANCE_CRITERION_ITEM_SCHEMA,
        },
        "priority": {},
        "scope": {},
        "constraints": {"type": "array"},
        "open_questions": {"type": "array"},
    },
}

# ---------------------------------------------------------------------------
# Design proposal (ticket 03). The Planner authors the design *against* the
# frozen requirements (the upstream), so the two arrays a design proposal
# cannot lack are ``design_elements`` (the local DES slots) and
# ``requirement_mapping`` (the REQ-coverage the freeze gate checks, §18.2).
# ---------------------------------------------------------------------------

# Each design-element entry. ``key`` is the proposal's *local* handle (the model
# never assigns the canonical DES-NNN; promote allocates it, ADR-0008 D2). A
# ``requirement_mapping`` entry references a design element by that key, and
# ``promote`` resolves it. ``name`` is the one field a design element cannot
# lack (the rendered-md heading, the analogue of a requirement's ``statement``);
# ``description`` / ``rationale`` / ``type`` are optional prose a refinement draft
# may omit (promote carries exactly these through to the canonical doc + md).
_DESIGN_ELEMENT_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["key", "name"],
    "additionalProperties": True,
    "properties": {
        "key": {"type": "string", "minLength": 1},
        "name": {"type": "string", "minLength": 1},
        "description": {"type": "string"},
        "rationale": {"type": "string"},
        "type": {"type": "string"},
    },
}

# Each requirement-mapping entry. ``requirement`` is a ref to a *frozen* upstream
# REQ-NNN (the model reads the frozen ``01-requirements.json`` in its input
# package and references REQs by their canonical id; promote resolves it against
# the frozen upstream set via ``RefResolver.add_upstream`` - reference-integrity,
# ADR-0008 D3 - the first live use of the generic resolver's upstream path).
# ``design_elements`` are *local* refs to this proposal's design-element ``key``s
# (resolved via ``register_local`` as DES ids are allocated). A mapping entry
# with no design elements carries no coverage information, so the list is
# required - but it carries no ``minItems``: a draft may map a REQ to one element
# and refine later. ``requirement`` is required because an entry that traces to
# no REQ is a coverage hole (ADR-0007), and making it required means a missing
# trace fails the §14.1 schema check at validate-run before promote runs.
_REQUIREMENT_MAPPING_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["key", "requirement", "design_elements"],
    "additionalProperties": True,
    "properties": {
        "key": {"type": "string", "minLength": 1},
        "requirement": {"type": "string", "minLength": 1},
        "design_elements": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
        "rationale": {"type": "string"},
    },
}

# ADR-0008 D2: the design proposal - id-free structured JSON. ``design_elements``
# and ``requirement_mapping`` are required arrays (the structure must be present),
# but neither carries a ``minItems``: a proposal is *expected to be incomplete*
# while being refined (D3), and coverage-completeness is a freeze-gate concern,
# not a §14.1 schema concern. The remaining §7.3 facets (architecture decision /
# data model / API-CLI contract / file layout / invariants / risks / dependencies)
# are optional prose a refinement draft may omit; their shapes are unconstrained
# (``{}``) so the model may structure them freely, carried through verbatim by
# promote.
DESIGN_PROPOSAL_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "PlannerDesignProposal",
    "type": "object",
    "required": ["design_elements", "requirement_mapping"],
    "additionalProperties": True,
    "properties": {
        "design_elements": {
            "type": "array",
            "items": _DESIGN_ELEMENT_ITEM_SCHEMA,
        },
        "requirement_mapping": {
            "type": "array",
            "items": _REQUIREMENT_MAPPING_ITEM_SCHEMA,
        },
        "architecture_decision": {},
        "data_model": {},
        "api_cli_contract": {},
        "file_layout": {},
        "invariants": {"type": "array"},
        "risks": {"type": "array"},
        "dependencies": {"type": "array"},
    },
}

# Stage -> proposal schema. ``requirements`` wired in ticket 01; ``design`` wired
# here (ticket 03); ``tasks`` (ticket 04) adds its entry with no other change.
_PLANNER_PROPOSAL_SCHEMAS: dict[str, Mapping[str, Any]] = {
    "requirements": REQUIREMENTS_PROPOSAL_SCHEMA,
    "design": DESIGN_PROPOSAL_SCHEMA,
}


def planner_output_schema(stage: str) -> Mapping[str, Any]:
    """Return the Planner proposal output-schema for ``stage`` (role-aware §14.1).

    The single lookup a planning-leg driver uses to pick the proposal contract:
    ``planner_output_schema("requirements")`` / ``planner_output_schema("design")``
    are the schemas written to a run's ``input/output-schema.json`` and thus the
    ones ``validate-run`` checks (§14.1). Fails loud (§24.2) for an unknown stage —
    a stage not yet wired (e.g. ``"tasks"`` before ticket 04) is a config error
    the caller must surface, not a silent fallback to the implementer schema
    (which would validate the wrong contract and let a malformed proposal pass).
    """
    try:
        return _PLANNER_PROPOSAL_SCHEMAS[stage]
    except KeyError as exc:
        raise ValueError(
            f"no Planner proposal schema wired for stage {stage!r}; "
            f"wired stages: {tuple(_PLANNER_PROPOSAL_SCHEMAS)}"
        ) from exc
