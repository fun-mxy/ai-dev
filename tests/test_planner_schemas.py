"""Planner proposal output-schemas + role-aware §14.1 lookup (v0.6 ticket 01).

The Planner (§9.1) is a model role, so its run authors ``output/result.json``
and the wrapper validates it with the §14.1 schema check — but the *shape*
differs from the implementer: an id-free proposal with local refs (ADR-0008 D2).
``planner_schemas`` is the data home for those schemas; ``run_prepare`` adds the
role-aware lookup that picks one. These tests pin both: the requirements schema
accepts a well-formed proposal and rejects a malformed one through the *same*
``validate_against_schema`` the implementer uses, and the lookup returns the
right contract per role/stage.
"""

from __future__ import annotations

import copy

import pytest

from ai_dev.planner_schemas import (
    PLANNER_ROLE,
    PLANNING_STAGES,
    REQUIREMENTS_PROPOSAL_SCHEMA,
    TASKS_PROPOSAL_SCHEMA,
    planner_output_schema,
)
from ai_dev.run_prepare import _OUTPUT_SCHEMA, output_schema_for_role
from ai_dev.validate import validate_against_schema

# A well-formed requirements proposal: two REQs (local keys r1/r2) and two ACs
# that reference them by those local keys. Id-free — no REQ-NNN anywhere.
VALID_PROPOSAL: dict = {
    "requirements": [
        {"key": "r1", "statement": "The system shall foo.", "priority": "must"},
        {"key": "r2", "statement": "The system shall bar.", "rationale": "because"},
    ],
    "acceptance_criteria": [
        {"key": "a1", "requirement": "r1", "criterion": "foo works"},
        {"key": "a2", "requirement": "r2", "criterion": "bar works"},
    ],
    "priority": "must-have-first",
    "scope": {"in_scope": ["foo"], "out_of_scope": ["baz"]},
    "constraints": ["no network"],
    "open_questions": ["how fast?"],
}


class TestRequirementsProposalSchema:
    def test_valid_proposal_passes(self) -> None:
        # The same validator that checks an implementer result.json checks a
        # Planner proposal — the role-awareness is in *which* schema, not a new
        # validator (ADR-0008 Consequences).
        assert validate_against_schema(VALID_PROPOSAL, REQUIREMENTS_PROPOSAL_SCHEMA) == []

    def test_requires_requirements_and_acceptance_criteria_arrays(self) -> None:
        assert validate_against_schema({}, REQUIREMENTS_PROPOSAL_SCHEMA) != []

        # Structure present but empty is fine — a refinement draft may lag on
        # ACs; coverage-completeness is a freeze-gate concern, not §14.1.
        empty = {"requirements": [], "acceptance_criteria": []}
        assert validate_against_schema(empty, REQUIREMENTS_PROPOSAL_SCHEMA) == []

    def test_requirement_missing_key_or_statement_rejected(self) -> None:
        bad = copy.deepcopy(VALID_PROPOSAL)
        bad["requirements"][0] = {"statement": "no key"}
        assert validate_against_schema(bad, REQUIREMENTS_PROPOSAL_SCHEMA) != []

        bad = copy.deepcopy(VALID_PROPOSAL)
        bad["requirements"][0] = {"key": "r1"}  # no statement
        assert validate_against_schema(bad, REQUIREMENTS_PROPOSAL_SCHEMA) != []

    def test_ac_must_trace_a_requirement(self) -> None:
        # An AC without a `requirement` local ref fails the schema — an AC with
        # no traced REQ is a coverage hole (ADR-0007), caught at validate-run
        # before promote even runs.
        bad = copy.deepcopy(VALID_PROPOSAL)
        bad["acceptance_criteria"][0] = {"key": "a1", "criterion": "no req ref"}
        assert validate_against_schema(bad, REQUIREMENTS_PROPOSAL_SCHEMA) != []

    def test_extra_fields_allowed(self) -> None:
        # additionalProperties is true — the model may carry rationale/notes/etc.
        proposal = copy.deepcopy(VALID_PROPOSAL)
        proposal["requirements"][0]["note"] = "anything"
        assert validate_against_schema(proposal, REQUIREMENTS_PROPOSAL_SCHEMA) == []


class TestPlannerOutputSchemaLookup:
    def test_requirements_stage_returns_requirements_schema(self) -> None:
        assert planner_output_schema("requirements") is REQUIREMENTS_PROPOSAL_SCHEMA

    def test_unknown_stage_fails_loud(self) -> None:
        # requirements (ticket 01), design (ticket 03), and tasks (ticket 04) are
        # all wired. Any other stage is a config error, not a silent fallback to
        # the implementer schema.
        with pytest.raises(ValueError, match="no Planner proposal schema"):
            planner_output_schema("bogus")

    def test_tasks_stage_returns_tasks_schema(self) -> None:
        # ticket 04 wires the tasks stage -> the id-free tasks proposal schema
        # (lane_purpose + tasks with REQ+DES refs).
        assert planner_output_schema("tasks") is TASKS_PROPOSAL_SCHEMA

    def test_all_declared_stages_are_documented(self) -> None:
        # The three §23.5 planning stages (requirements/design/tasks all wired).
        assert PLANNING_STAGES == ("requirements", "design", "tasks")


class TestTasksProposalSchema:
    """The tasks proposal schema (ticket 04) + the verify-command facet (ticket 05)."""

    _VALID_TASKS: dict = {
        "lane_purpose": "Deliver the greet CLI.",
        "tasks": [
            {
                "key": "t1",
                "summary": "Greeting formatter",
                "related_requirements": ["REQ-001"],
                "related_design": ["DES-001"],
                "expected_files": ["src/greet.py"],
                "exclusive_files": ["src/greet.py"],
            }
        ],
    }

    def test_valid_proposal_passes(self) -> None:
        assert validate_against_schema(self._VALID_TASKS, TASKS_PROPOSAL_SCHEMA) == []

    def test_proposal_with_verification_commands_passes(self) -> None:
        # v0.6 capstone (ticket 05): the optional top-level lane verify command
        # set - pytest + mypy, the contract the Verifier (§9.5) runs.
        proposal = copy.deepcopy(self._VALID_TASKS)
        proposal["verification_commands"] = [
            {
                "name": "pytest",
                "command": "PYTHONPATH=. python -m pytest -q -p no:cacheprovider -c /dev/null tests",
            },
            {"name": "mypy", "command": "python -m mypy greet"},
        ]
        assert validate_against_schema(proposal, TASKS_PROPOSAL_SCHEMA) == []

    def test_verification_command_missing_name_rejected(self) -> None:
        proposal = copy.deepcopy(self._VALID_TASKS)
        proposal["verification_commands"] = [{"command": "python -m pytest"}]
        assert validate_against_schema(proposal, TASKS_PROPOSAL_SCHEMA) != []

    def test_verification_command_missing_command_rejected(self) -> None:
        proposal = copy.deepcopy(self._VALID_TASKS)
        proposal["verification_commands"] = [{"name": "pytest"}]
        assert validate_against_schema(proposal, TASKS_PROPOSAL_SCHEMA) != []

    def test_verification_commands_optional(self) -> None:
        # A refinement draft may omit the verify command set entirely.
        assert "verification_commands" not in self._VALID_TASKS
        assert validate_against_schema(self._VALID_TASKS, TASKS_PROPOSAL_SCHEMA) == []


class TestOutputSchemaForRole:
    def test_planner_requirements_returns_proposal_schema(self) -> None:
        assert (
            output_schema_for_role(PLANNER_ROLE, stage="requirements")
            is REQUIREMENTS_PROPOSAL_SCHEMA
        )

    def test_planner_without_stage_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="requires a --stage"):
            output_schema_for_role(PLANNER_ROLE)

    def test_non_planner_role_returns_implementer_schema(self) -> None:
        # Implementer / Reviewer / Spec-Gap keep the v0.1 implementer result.json
        # schema — the default branch.
        assert output_schema_for_role("Implementer") is _OUTPUT_SCHEMA
        assert output_schema_for_role("Reviewer") is _OUTPUT_SCHEMA
