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

Only the **requirements** proposal schema is concretely defined here (this
ticket). The design (ticket 03) and tasks (ticket 04) proposal schemas are
added to ``_PLANNER_PROPOSAL_SCHEMAS`` as one-line data entries when those
tickets land; the lookup mechanism and the promote seam (``promote``) are built
generically now so they need no rework then.

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

# Stage -> proposal schema. Only ``requirements`` is wired in ticket 01; design
# (ticket 03) and tasks (ticket 04) add their entries here with no other change.
_PLANNER_PROPOSAL_SCHEMAS: dict[str, Mapping[str, Any]] = {
    "requirements": REQUIREMENTS_PROPOSAL_SCHEMA,
}


def planner_output_schema(stage: str) -> Mapping[str, Any]:
    """Return the Planner proposal output-schema for ``stage`` (role-aware §14.1).

    The single lookup a planning-leg driver uses to pick the proposal contract:
    ``planner_output_schema("requirements")`` is the schema written to a
    requirements run's ``input/output-schema.json`` and thus the one
    ``validate-run`` checks (§14.1). Fails loud (§24.2) for an unknown stage —
    a stage not yet wired (e.g. ``"design"`` before ticket 03) is a config error
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
