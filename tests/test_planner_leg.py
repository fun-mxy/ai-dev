"""planner_leg - v0.6 ticket 02, the Planner requirements leg (generate→promote).

ADR-0008 D1/D2/D4: the first live planning gate — a complete
generate → promote → review → freeze vertical slice for requirements. The
Planner role (cc-glm52 via role_defaults) runs through the existing
run_wrapper; the run emits an id-free requirements proposal (ticket-01 schema)
in output/; promote fires automatically after the run, writing the
canonical-unfrozen 01-requirements.{json,md}; --feedback carries the human's
refinement note; freeze is the human gate (advances current_gate
requirements_gate → design_gate).

These tests pin the seams: input-package assembly from the feature intent
(role pinned to Planner, role-aware proposal schema, feedback carried),
the auto-promote gated on validation, the refinement overwrite, the CLI
wiring (fake claude on PATH), and the freeze gate advance. A fake ``claude``
stands in for the real CLI so the orchestrator is exercised end-to-end
without network or token; the genuine cc-glm52/Ark evidence lives in
.scratch/ai-dev-v0-6-planner/evidence/.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from ai_dev.cli import main
from ai_dev.coverage import design_coverage, freeze_gate_coverage, tasks_coverage
from ai_dev.feature_run import create_feature_run
from ai_dev.paths import run_dir
from ai_dev.planner_leg import (
    build_design_input_package,
    build_requirements_input_package,
    build_tasks_input_package,
    read_intent,
    run_generate_design,
    run_generate_requirements,
    run_generate_tasks,
)
from ai_dev.planner_schemas import (
    DESIGN_PROPOSAL_SCHEMA,
    PLANNER_ROLE,
    REQUIREMENTS_PROPOSAL_SCHEMA,
    TASKS_PROPOSAL_SCHEMA,
)
from ai_dev.promote import promote_design, promote_requirements
from ai_dev.profiles import load_profile
from ai_dev.run_prepare import (
    ALLOWED_FILES_FILE,
    OUTPUT_SCHEMA_FILE,
    ROLE_FILE,
    TASK_PACKAGE_FILE,
)
from ai_dev.status import (
    FROZEN_ARTIFACTS,
    freeze_artifact,
    load_feature_status,
)
from ai_dev.templates import (
    DESIGN_JSON,
    DESIGN_MD,
    LANE_GRAPH_YML,
    REQUIREMENTS_JSON,
    REQUIREMENTS_MD,
    TASKS_JSON,
    TASKS_MD,
)

FEATURE_ID = "FEATURE-001"
_INTENT = "Build a CLI that greets a named user."


def _feature_root(repo_root: Path, feature_id: str = FEATURE_ID) -> Path:
    return repo_root / ".ai-dev" / "features" / feature_id


@pytest.fixture
def feature(repo_root: Path, write_profiles) -> str:
    """A created feature run (intent + status + counters + seeded templates)."""
    write_profiles(repo_root)
    create_feature_run(repo_root, _INTENT)
    return FEATURE_ID


@pytest.fixture
def feature_with_frozen_reqs(repo_root: Path, write_profiles, feature: str) -> str:
    """A feature run whose requirements are promoted + frozen (REQ-001/002).

    The design leg (ticket 03) may only run against a *frozen* requirements
    upstream (ADR-0008 D2), so the design tests start here. Requirements are
    promoted + frozen directly (not via the requirements leg) to keep the design
    seam the unit under test. REQ-001/002 + AC-001/002 are allocated; the design
    fake-claude references REQ-001/REQ-002 by those canonical ids.
    """
    write_profiles(repo_root)
    root = _feature_root(repo_root)
    promote_requirements(
        root,
        FEATURE_ID,
        {
            "requirements": [
                {"key": "r1", "statement": "The CLI shall greet a named user."},
                {"key": "r2", "statement": "The CLI shall exit 0 on success."},
            ],
            "acceptance_criteria": [
                {"key": "a1", "requirement": "r1", "criterion": "greeting contains name"},
                {"key": "a2", "requirement": "r2", "criterion": "exit 0 on valid name"},
            ],
        },
        origin="test",
    )
    freeze_artifact(root, "requirements", origin="test")
    return FEATURE_ID


@pytest.fixture
def feature_with_frozen_reqs_and_design(
    repo_root: Path, feature_with_frozen_reqs: str
) -> str:
    """A feature run whose requirements AND design are promoted + frozen.

    The tasks leg (ticket 04) may only run against *frozen* requirements AND
    design (TWO upstreams, ADR-0008 D2), so the tasks tests start here. Design is
    promoted + frozen directly (not via the design leg) to keep the tasks seam the
    unit under test. DES-001 (Greeting module) + DES-002 (Exit handling) are
    allocated, mapped REQ-001->[DES-001], REQ-002->[DES-001,DES-002]; the tasks
    fake-claude references REQ-001/REQ-002 + DES-001/DES-002 by those canonical
    ids. Starts at ``task_gate`` (requirements + design frozen).
    """
    root = _feature_root(repo_root)
    promote_design(
        root,
        FEATURE_ID,
        {
            "design_elements": [
                {"key": "d1", "name": "Greeting module",
                 "description": "formats the greeting"},
                {"key": "d2", "name": "Exit handling", "type": "module"},
            ],
            "requirement_mapping": [
                {"key": "m1", "requirement": "REQ-001", "design_elements": ["d1"]},
                {"key": "m2", "requirement": "REQ-002", "design_elements": ["d1", "d2"]},
            ],
            "architecture_decision": "single module",
            "invariants": ["deterministic greeting"],
        },
        origin="test",
    )
    freeze_artifact(root, "design", origin="test")
    return FEATURE_ID


# A fake ``claude`` that writes a schema-valid id-free design proposal
# (ticket-03 schema) referencing the frozen REQ-001/REQ-002 upstream + local DES
# keys. Stands in for the real CLI so the design generate->promote slice is
# exercised end-to-end without network or token.
_FAKE_CLAUDE_DESIGN = """\
#!__PY__
import json, os, sys
os.makedirs("output", exist_ok=True)
with open("output/result.md", "w") as f:
    f.write("Authored a design proposal.\\n")
with open("output/result.json", "w") as f:
    json.dump(
        {
            "design_elements": [
                {"key": "d1", "name": "Greeting module",
                 "description": "formats the greeting"},
                {"key": "d2", "name": "Exit handling", "type": "module"}
            ],
            "requirement_mapping": [
                {"key": "m1", "requirement": "REQ-001", "design_elements": ["d1"]},
                {"key": "m2", "requirement": "REQ-002", "design_elements": ["d1", "d2"]}
            ],
            "architecture_decision": "single module",
            "invariants": ["deterministic greeting"]
        },
        f,
    )
sys.stdout.write('{"type":"result","subtype":"success","is_error":false}\\n')
sys.exit(0)
"""

# A fake ``claude`` whose design result.json violates the proposal schema (a
# design element missing the required ``name``) - exercises the validation-fail
# -> no-promote path for the design leg.
_FAKE_CLAUDE_DESIGN_INVALID = """\
#!__PY__
import json, os, sys
os.makedirs("output", exist_ok=True)
with open("output/result.md", "w") as f:
    f.write("bad design proposal\\n")
with open("output/result.json", "w") as f:
    json.dump(
        {"design_elements": [{"key": "d1"}], "requirement_mapping": []},
        f,
    )
sys.stdout.write('{"type":"result","subtype":"success","is_error":false}\\n')
sys.exit(0)
"""

# A fake ``claude`` whose design proposal maps only REQ-001 - leaving REQ-002
# uncovered. The proposal is schema-valid and promotes fine (reference-integrity
# holds), but the freeze-gate coverage precheck (§18.2) refuses the freeze.
_FAKE_CLAUDE_DESIGN_GAP = """\
#!__PY__
import json, os, sys
os.makedirs("output", exist_ok=True)
with open("output/result.md", "w") as f:
    f.write("Partial design - REQ-002 unmapped.\\n")
with open("output/result.json", "w") as f:
    json.dump(
        {
            "design_elements": [{"key": "d1", "name": "Greeting module"}],
            "requirement_mapping": [
                {"key": "m1", "requirement": "REQ-001", "design_elements": ["d1"]}
            ]
        },
        f,
    )
sys.stdout.write('{"type":"result","subtype":"success","is_error":false}\\n')
sys.exit(0)
"""
# generate→promote slice is exercised end-to-end without network or token.
# ``__PY__`` is replaced with the test interpreter so the shebang resolves under
# ``uv run`` (string replace, not ``.format``, so the JSON braces are literal).
# A fake ``claude`` that writes a schema-valid id-free tasks proposal (ticket-04
# schema) referencing the frozen REQ-001/REQ-002 + DES-001/DES-002 upstreams.
# Covers every REQ+DES, so the task-gate coverage precheck passes.
_FAKE_CLAUDE_TASKS = """\
#!__PY__
import json, os, sys
os.makedirs("output", exist_ok=True)
with open("output/result.md", "w") as f:
    f.write("Authored a tasks proposal.\\n")
with open("output/result.json", "w") as f:
    json.dump(
        {
            "lane_purpose": "Implement the greet CLI end to end.",
            "tasks": [
                {
                    "key": "t1",
                    "summary": "Implement greeting module",
                    "related_requirements": ["REQ-001"],
                    "related_design": ["DES-001"],
                    "expected_files": ["src/greet.py"],
                    "exclusive_files": ["src/greet.py"],
                    "description": "Formats the greeting string."
                },
                {
                    "key": "t2",
                    "summary": "Wire exit handling",
                    "related_requirements": ["REQ-002"],
                    "related_design": ["DES-001", "DES-002"],
                    "expected_files": ["src/cli.py"],
                    "exclusive_files": ["src/cli.py"]
                }
            ],
            "verification_commands": [
                {"name": "pytest", "command": "PYTHONPATH=. python -m pytest -q tests"},
                {"name": "mypy", "command": "python -m mypy src"},
            ],
        },
        f,
    )
sys.stdout.write('{"type":"result","subtype":"success","is_error":false}\\n')
sys.exit(0)
"""

# A fake ``claude`` whose tasks result.json violates the proposal schema (a task
# missing the required ``summary``) - exercises the validation-fail -> no-promote
# path for the tasks leg.
_FAKE_CLAUDE_TASKS_INVALID = """\
#!__PY__
import json, os, sys
os.makedirs("output", exist_ok=True)
with open("output/result.md", "w") as f:
    f.write("bad tasks proposal\\n")
with open("output/result.json", "w") as f:
    json.dump(
        {
            "lane_purpose": "bad",
            "tasks": [
                {"key": "t1", "related_requirements": ["REQ-001"],
                 "related_design": ["DES-001"],
                 "expected_files": ["src/greet.py"],
                 "exclusive_files": ["src/greet.py"]}
            ]
        },
        f,
    )
sys.stdout.write('{"type":"result","subtype":"success","is_error":false}\\n')
sys.exit(0)
"""

# A fake ``claude`` whose tasks proposal covers only REQ-001/DES-001 (t1) -
# leaving REQ-002 + DES-002 uncovered. Schema-valid + promotes fine, but the
# task-gate coverage precheck (§18.2, REQ+DES) refuses the freeze.
_FAKE_CLAUDE_TASKS_GAP = """\
#!__PY__
import json, os, sys
os.makedirs("output", exist_ok=True)
with open("output/result.md", "w") as f:
    f.write("Partial tasks - REQ-002 + DES-002 uncovered.\\n")
with open("output/result.json", "w") as f:
    json.dump(
        {
            "lane_purpose": "Partial implementation.",
            "tasks": [
                {
                    "key": "t1",
                    "summary": "Implement greeting module only",
                    "related_requirements": ["REQ-001"],
                    "related_design": ["DES-001"],
                    "expected_files": ["src/greet.py"],
                    "exclusive_files": ["src/greet.py"]
                }
            ]
        },
        f,
    )
sys.stdout.write('{"type":"result","subtype":"success","is_error":false}\\n')
sys.exit(0)
"""

# A fake ``claude`` whose tasks proposal uses the v0.7 multi-lane `lanes` array
# form: one entry per seeded lane (LANE-001/002) with its own purpose +
# verification_commands, and every task carrying a `lane` assignment. promote's
# `_proposal_lanes` prefers the `lanes` array over the (still-required,
# schema-wise) top-level `lane_purpose`, so the canonical lane-graph ends up
# with two populated lane entries. Stands in for the real CLI on the 2-lane
# tasks leg (v0.7 capstone ticket 07).
_FAKE_CLAUDE_TASKS_TWO_LANE = """\
#!__PY__
import json, os, sys
os.makedirs("output", exist_ok=True)
with open("output/result.md", "w") as f:
    f.write("Authored a two-lane tasks proposal.\\n")
with open("output/result.json", "w") as f:
    json.dump(
        {
            "lane_purpose": "Implement greet + exit across two lanes.",
            "lanes": [
                {"id": "LANE-001", "purpose": "Greeting module lane",
                 "verification_commands": [
                     {"name": "pytest", "command": "PYTHONPATH=. python -m pytest -q tests"},
                     {"name": "mypy", "command": "python -m mypy src"}]},
                {"id": "LANE-002", "purpose": "Exit handling lane",
                 "verification_commands": [
                     {"name": "pytest", "command": "PYTHONPATH=. python -m pytest -q tests"},
                     {"name": "mypy", "command": "python -m mypy src"}]}
            ],
            "tasks": [
                {
                    "key": "t1",
                    "summary": "Implement greeting module",
                    "related_requirements": ["REQ-001"],
                    "related_design": ["DES-001"],
                    "expected_files": ["src/greet.py"],
                    "exclusive_files": ["src/greet.py"],
                    "lane": "LANE-001"
                },
                {
                    "key": "t2",
                    "summary": "Wire exit handling",
                    "related_requirements": ["REQ-002"],
                    "related_design": ["DES-001", "DES-002"],
                    "expected_files": ["src/cli.py"],
                    "exclusive_files": ["src/cli.py"],
                    "lane": "LANE-002"
                }
            ],
            "verification_commands": [
                {"name": "pytest", "command": "PYTHONPATH=. python -m pytest -q tests"},
                {"name": "mypy", "command": "python -m mypy src"}
            ]
        },
        f,
    )
sys.stdout.write('{"type":"result","subtype":"success","is_error":false}\\n')
sys.exit(0)
"""

_FAKE_CLAUDE = """\
#!__PY__
import json, os, sys
os.makedirs("output", exist_ok=True)
with open("output/result.md", "w") as f:
    f.write("Authored a requirements proposal.\\n")
with open("output/result.json", "w") as f:
    json.dump(
        {
            "requirements": [
                {"key": "r1", "statement": "The CLI shall greet a named user.",
                 "priority": "must"},
                {"key": "r2", "statement": "The CLI shall exit 0 on success."}
            ],
            "acceptance_criteria": [
                {"key": "a1", "requirement": "r1",
                 "criterion": "greeting contains the given name"},
                {"key": "a2", "requirement": "r2",
                 "criterion": "exit code is 0 on a valid name"}
            ],
            "priority": "P0",
            "open_questions": ["should it localize the greeting?"]
        },
        f,
    )
sys.stdout.write('{"type":"result","subtype":"success","is_error":false}\\n')
sys.exit(0)
"""

# A fake ``claude`` whose result.json violates the proposal schema (a
# requirement missing the required ``statement``) — exercises the validation-fail
# → no-promote path.
_FAKE_CLAUDE_INVALID = """\
#!__PY__
import json, os, sys
os.makedirs("output", exist_ok=True)
with open("output/result.md", "w") as f:
    f.write("bad proposal\\n")
with open("output/result.json", "w") as f:
    json.dump({"requirements": [{"key": "r1"}], "acceptance_criteria": []}, f)
sys.stdout.write('{"type":"result","subtype":"success","is_error":false}\\n')
sys.exit(0)
"""


def _write_fake_claude(bin_dir: Path, *, variant: str = "valid") -> Path:
    """Write the fake ``claude`` script into ``bin_dir`` and return its path.

    ``variant`` picks which canned proposal the fake writes: the requirements
    proposals (``valid`` / ``invalid``) or the design proposals (``design`` /
    ``design_invalid`` / ``design_gap``). The design variants stand in for the
    real CLI on the design leg (ticket 03).
    """
    sources = {
        "valid": _FAKE_CLAUDE,
        "invalid": _FAKE_CLAUDE_INVALID,
        "design": _FAKE_CLAUDE_DESIGN,
        "design_invalid": _FAKE_CLAUDE_DESIGN_INVALID,
        "design_gap": _FAKE_CLAUDE_DESIGN_GAP,
        "tasks": _FAKE_CLAUDE_TASKS,
        "tasks_invalid": _FAKE_CLAUDE_TASKS_INVALID,
        "tasks_gap": _FAKE_CLAUDE_TASKS_GAP,
        "tasks_two_lane": _FAKE_CLAUDE_TASKS_TWO_LANE,
    }
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "claude"
    src = sources[variant]
    script.write_text(src.replace("__PY__", sys.executable))
    os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _load_profile(repo_root: Path) -> Any:
    return load_profile(repo_root, "cc-glm52")


# ===========================================================================
# Seam 1: input-package assembly from the feature intent.
# ===========================================================================


class TestBuildRequirementsInputPackage:
    """The Planner input package is assembled from the feature intent, with the
    role-aware proposal schema and the optional feedback refinement note."""

    def test_intent_flows_into_task_package(self, repo_root: Path, feature: str) -> None:
        run_id = build_requirements_input_package(repo_root, feature)

        assert run_id == "RUN-001"
        task_pkg = (
            run_dir(repo_root, feature, run_id) / "input" / TASK_PACKAGE_FILE
        ).read_text()
        assert _INTENT in task_pkg

    def test_feedback_carried_into_task_package(
        self, repo_root: Path, feature: str
    ) -> None:
        run_id = build_requirements_input_package(
            repo_root, feature, feedback="please split greeting into its own REQ"
        )

        task_pkg = (
            run_dir(repo_root, feature, run_id) / "input" / TASK_PACKAGE_FILE
        ).read_text()
        assert "please split greeting into its own REQ" in task_pkg
        assert "Human feedback" in task_pkg

    def test_role_pinned_to_planner(self, repo_root: Path, feature: str) -> None:
        run_id = build_requirements_input_package(repo_root, feature)

        role_md = (
            run_dir(repo_root, feature, run_id) / "input" / ROLE_FILE
        ).read_text()
        assert PLANNER_ROLE in role_md

    def test_output_schema_is_the_requirements_proposal(
        self, repo_root: Path, feature: str
    ) -> None:
        run_id = build_requirements_input_package(repo_root, feature)

        schema_path = run_dir(repo_root, feature, run_id) / "input" / OUTPUT_SCHEMA_FILE
        written = json.loads(schema_path.read_text())
        # The role-aware §14.1 contract is the ticket-01 requirements proposal
        # schema (id-free), not the implementer result.json schema.
        assert written["title"] == REQUIREMENTS_PROPOSAL_SCHEMA["title"]
        assert "requirements" in written["required"]
        assert "acceptance_criteria" in written["required"]

    def test_allowed_files_are_only_the_mandatory_outputs(
        self, repo_root: Path, feature: str
    ) -> None:
        # The Planner authors only output/result.{json,md} — no workspace files.
        run_id = build_requirements_input_package(repo_root, feature)

        allowed = (
            run_dir(repo_root, feature, run_id) / "input" / ALLOWED_FILES_FILE
        ).read_text()
        paths = {ln.strip() for ln in allowed.splitlines() if ln.strip() and not ln.startswith("#")}
        assert paths == {"output/result.json", "output/result.md"}

    def test_fails_loud_when_feature_missing(self, repo_root: Path) -> None:
        with pytest.raises(ValueError, match="not found"):
            build_requirements_input_package(repo_root, "FEATURE-999")


class TestReadIntent:
    """``read_intent`` extracts the original intent from 00-intent.md."""

    def test_extracts_intent_section(self, repo_root: Path, feature: str) -> None:
        assert read_intent(_feature_root(repo_root)) == _INTENT

    def test_fails_loud_without_intent_section(self, repo_root: Path, feature: str) -> None:
        # Overwrite 00-intent.md with no intent section.
        (_feature_root(repo_root) / "00-intent.md").write_text("# Intent — X\n\nNo section.\n")
        with pytest.raises(ValueError, match="Original intent"):
            read_intent(_feature_root(repo_root))


# ===========================================================================
# Seam 2: orchestration — prepare -> run -> validate -> promote (gated).
# ===========================================================================


class TestRunGenerateRequirements:
    """The full Planner leg with a fake claude: a passing run promotes; a
    failing run does not."""

    def test_passing_run_promotes_canonical_artifact(
        self, repo_root: Path, feature: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-planner")
        fake = _write_fake_claude(repo_root / "bin")
        profile = _load_profile(repo_root)
        root = _feature_root(repo_root)

        result = run_generate_requirements(
            repo_root, feature, profile, claude_path=str(fake)
        )

        assert result.validation.passed
        assert result.promoted
        assert result.stage == "requirements"
        # promote wrote the canonical-unfrozen artifact + rendered mirror.
        doc = json.loads((root / REQUIREMENTS_JSON).read_text())
        assert doc["frozen"] is False
        assert [r["id"] for r in doc["requirements"]] == ["REQ-001", "REQ-002"]
        # AC local refs stitched to allocated REQ ids (reference-integrity, D3).
        assert [ac["requirement"] for ac in doc["acceptance_criteria"]] == [
            "REQ-001",
            "REQ-002",
        ]
        assert [ac["id"] for ac in doc["acceptance_criteria"]] == ["AC-001", "AC-002"]
        assert (root / REQUIREMENTS_MD).is_file()

    def test_passing_run_allocates_ids_from_counter(
        self, repo_root: Path, feature: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-planner")
        fake = _write_fake_claude(repo_root / "bin")
        profile = _load_profile(repo_root)

        result = run_generate_requirements(
            repo_root, feature, profile, claude_path=str(fake)
        )

        assert result.promote is not None
        assert list(result.promote.allocated["REQ"]) == ["REQ-001", "REQ-002"]
        assert list(result.promote.allocated["AC"]) == ["AC-001", "AC-002"]

    def test_failed_validation_skips_promote(
        self, repo_root: Path, feature: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-planner-fail")
        fake = _write_fake_claude(repo_root / "bin", variant="invalid")
        profile = _load_profile(repo_root)
        root = _feature_root(repo_root)

        result = run_generate_requirements(
            repo_root, feature, profile, claude_path=str(fake)
        )

        assert not result.validation.passed
        assert not result.promoted
        # No canonical write happened — the seeded placeholder is untouched (no
        # allocated REQ/AC ids).
        doc = json.loads((root / REQUIREMENTS_JSON).read_text())
        assert doc["requirements"] == []
        assert doc["acceptance_criteria"] == []

    def test_refinement_overwrites_unfrozen_artifact(
        self, repo_root: Path, feature: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # First pass: the valid fake (2 REQs). Second pass: the same fake again
        # with feedback — promote overwrites the unfrozen 01-requirements.
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-planner-refine")
        fake = _write_fake_claude(repo_root / "bin")
        profile = _load_profile(repo_root)
        root = _feature_root(repo_root)

        first = run_generate_requirements(
            repo_root, feature, profile, claude_path=str(fake)
        )
        second = run_generate_requirements(
            repo_root, feature, profile, feedback="tighten scope", claude_path=str(fake)
        )

        assert first.promoted and second.promoted
        # Two distinct runs allocated; the canonical artifact is the second pass.
        assert first.run_id == "RUN-001"
        assert second.run_id == "RUN-002"
        doc = json.loads((root / REQUIREMENTS_JSON).read_text())
        # The counter advanced across the two passes (ids are not reused); the
        # artifact reflects the latest promote (REQ-003/004, AC-003/004).
        assert [r["id"] for r in doc["requirements"]] == ["REQ-003", "REQ-004"]
        assert [ac["id"] for ac in doc["acceptance_criteria"]] == ["AC-003", "AC-004"]
        # Feedback reached the second run's task package.
        task_pkg = (
            run_dir(repo_root, feature, second.run_id) / "input" / TASK_PACKAGE_FILE
        ).read_text()
        assert "tighten scope" in task_pkg

    def test_refuses_to_promote_over_frozen_artifact(
        self, repo_root: Path, feature: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Freeze the requirements artifact (the human gate), then attempt
        # another generate — promote must refuse (§4.2): a frozen artifact is
        # immutable; only a Change Proposal may change it.
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-planner-frozen")
        fake = _write_fake_claude(repo_root / "bin")
        profile = _load_profile(repo_root)
        root = _feature_root(repo_root)

        run_generate_requirements(repo_root, feature, profile, claude_path=str(fake))
        freeze_artifact(root, "requirements")

        with pytest.raises(ValueError, match="frozen"):
            run_generate_requirements(
                repo_root, feature, profile, claude_path=str(fake)
            )

    def test_audit_log_records_leg_lifecycle(
        self, repo_root: Path, feature: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-planner-audit")
        fake = _write_fake_claude(repo_root / "bin")
        profile = _load_profile(repo_root)

        run_generate_requirements(repo_root, feature, profile, claude_path=str(fake))

        from ai_dev.audit import AUDIT_LOG_JSON

        log = json.loads((_feature_root(repo_root) / AUDIT_LOG_JSON).read_text())
        events = [rec["event"] for rec in log]
        # prepare_run -> run -> validate -> promote, in order.
        assert "prepare_run" in events
        assert "run" in events
        assert "validate" in events
        assert "promote" in events
        assert events.index("promote") > events.index("validate")


# ===========================================================================
# Seam 3: CLI wiring — generate-requirements end to end (fake claude on PATH).
# ===========================================================================


class TestGenerateRequirementsCLI:
    """The ``ai-dev generate-requirements`` command through the console entry,
    fake claude on PATH."""

    def _invoke(
        self, repo_root: Path, monkeypatch: pytest.MonkeyPatch, *extra: str
    ) -> int:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-cli")
        fake_bin = _write_fake_claude(repo_root / "bin")
        monkeypatch.setenv(
            "PATH", f"{fake_bin.parent}{os.pathsep}{os.environ['PATH']}"
        )
        return main(
            ["generate-requirements", FEATURE_ID, "--repo-root", str(repo_root), *extra]
        )

    def test_cli_passes_and_promotes(
        self, repo_root: Path, feature: str, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        rc = self._invoke(repo_root, monkeypatch)
        assert rc == 0
        out = capsys.readouterr().out
        assert "GENERATE-REQUIREMENTS PASS" in out
        assert "REQ=['REQ-001', 'REQ-002']" in out
        # The canonical artifact was written.
        assert (_feature_root(repo_root) / REQUIREMENTS_JSON).is_file()

    def test_cli_carries_feedback(
        self, repo_root: Path, feature: str, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        rc = self._invoke(
            repo_root, monkeypatch, "--feedback", "add a non-goal REQ"
        )
        assert rc == 0
        # Feedback reached the run task package.
        task_pkg = (
            run_dir(repo_root, feature, "RUN-001") / "input" / TASK_PACKAGE_FILE
        ).read_text()
        assert "add a non-goal REQ" in task_pkg

    def test_cli_resolves_planner_role_default(
        self, repo_root: Path, feature: str, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        # No --profile: resolves through role_defaults[planner] = cc-glm52.
        rc = self._invoke(repo_root, monkeypatch)
        assert rc == 0
        # The resolved planner profile is recorded on the feature config.
        slots = load_feature_status(_feature_root(repo_root))["feature"]["agent_profiles"]
        assert slots.get("planner") == "cc-glm52"

    def test_cli_dry_run_mints_nothing(
        self, repo_root: Path, feature: str, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        # Dry-run needs no token and mints no run id; the feature tree is
        # untouched (no runs/ dir populated, no canonical write).
        monkeypatch.delenv("CC_GLM52_TOKEN", raising=False)
        rc = main(
            [
                "generate-requirements",
                FEATURE_ID,
                "--dry-run",
                "--repo-root",
                str(repo_root),
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "GENERATE-REQUIREMENTS DRY-RUN" in out
        assert "would_promote" in out
        # No run was allocated: dry-run renders into a temp dir (ADR-0004), so
        # no RUN-NNN exists under the feature's runs/ tree.
        runs = list((repo_root / ".ai-dev" / "features" / FEATURE_ID / "runs").glob("RUN-*"))
        assert runs == []


# ===========================================================================
# Seam 4: the human freeze gate advances current_gate requirements→design.
# ===========================================================================


class TestFreezeAdvancesGate:
    """Freezing the requirements artifact (the human gate) advances
    current_gate requirements_gate → design_gate (§18)."""

    def test_freeze_advances_requirements_to_design_gate(
        self, repo_root: Path, feature: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-freeze")
        fake = _write_fake_claude(repo_root / "bin")
        profile = _load_profile(repo_root)
        root = _feature_root(repo_root)

        # Start at the requirements gate.
        assert load_feature_status(root)["feature"]["current_gate"] == "requirements_gate"

        run_generate_requirements(repo_root, feature, profile, claude_path=str(fake))
        freeze_artifact(root, "requirements")

        status = load_feature_status(root)["feature"]
        assert status["frozen_artifacts"]["requirements"] is True
        assert status["current_gate"] == "design_gate"

    def test_freeze_via_cli_advances_gate(
        self, repo_root: Path, feature: str, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-freeze-cli")
        fake_bin = _write_fake_claude(repo_root / "bin")
        monkeypatch.setenv(
            "PATH", f"{fake_bin.parent}{os.pathsep}{os.environ['PATH']}"
        )
        root = _feature_root(repo_root)

        assert main(
            ["generate-requirements", FEATURE_ID, "--repo-root", str(repo_root)]
        ) == 0
        assert main(["freeze", FEATURE_ID, "requirements", "--repo-root", str(repo_root)]) == 0

        assert load_feature_status(root)["feature"]["current_gate"] == "design_gate"


# ===========================================================================
# Ticket 03: the Planner design leg (generate -> promote -> freeze) + the
# freeze-gate coverage precheck (every REQ in >=1 requirement_mapping, §18.2).
# ===========================================================================


class TestBuildDesignInputPackage:
    """The Planner design input package is assembled from the intent + the frozen
    requirements (the upstream), with the design proposal schema and feedback."""

    def test_intent_and_frozen_requirements_flow_into_task_package(
        self, repo_root: Path, feature_with_frozen_reqs: str
    ) -> None:
        run_id = build_design_input_package(repo_root, feature_with_frozen_reqs)

        task_pkg = (
            run_dir(repo_root, feature_with_frozen_reqs, run_id) / "input" / TASK_PACKAGE_FILE
        ).read_text()
        # The original intent AND the frozen REQ ids both reach the Planner.
        assert _INTENT in task_pkg
        assert "REQ-001" in task_pkg and "REQ-002" in task_pkg

    def test_feedback_carried_into_task_package(
        self, repo_root: Path, feature_with_frozen_reqs: str
    ) -> None:
        run_id = build_design_input_package(
            repo_root, feature_with_frozen_reqs, feedback="split greeting into its own DES"
        )
        task_pkg = (
            run_dir(repo_root, feature_with_frozen_reqs, run_id) / "input" / TASK_PACKAGE_FILE
        ).read_text()
        assert "split greeting into its own DES" in task_pkg
        assert "Human feedback" in task_pkg

    def test_role_pinned_to_planner(
        self, repo_root: Path, feature_with_frozen_reqs: str
    ) -> None:
        run_id = build_design_input_package(repo_root, feature_with_frozen_reqs)
        role_md = (
            run_dir(repo_root, feature_with_frozen_reqs, run_id) / "input" / ROLE_FILE
        ).read_text()
        assert PLANNER_ROLE in role_md

    def test_output_schema_is_the_design_proposal(
        self, repo_root: Path, feature_with_frozen_reqs: str
    ) -> None:
        run_id = build_design_input_package(repo_root, feature_with_frozen_reqs)
        schema_path = (
            run_dir(repo_root, feature_with_frozen_reqs, run_id) / "input" / OUTPUT_SCHEMA_FILE
        )
        written = json.loads(schema_path.read_text())
        assert written["title"] == DESIGN_PROPOSAL_SCHEMA["title"]
        assert "design_elements" in written["required"]
        assert "requirement_mapping" in written["required"]

    def test_fails_loud_when_requirements_not_frozen(
        self, repo_root: Path, feature: str
    ) -> None:
        # feature (not feature_with_frozen_reqs): requirements exist as a seeded
        # template but are NOT frozen - design may only stitch against a frozen
        # upstream (ADR-0008 D2).
        with pytest.raises(ValueError, match="not frozen"):
            build_design_input_package(repo_root, feature)

    def test_fails_loud_when_feature_missing(self, repo_root: Path) -> None:
        with pytest.raises(ValueError, match="not found"):
            build_design_input_package(repo_root, "FEATURE-999")


class TestRunGenerateDesign:
    """The full Planner design leg with a fake claude: a passing run promotes; a
    failing run does not."""

    def test_passing_run_promotes_canonical_design_artifact(
        self, repo_root: Path, feature_with_frozen_reqs: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-design")
        fake = _write_fake_claude(repo_root / "bin", variant="design")
        profile = _load_profile(repo_root)
        root = _feature_root(repo_root)

        result = run_generate_design(
            repo_root, feature_with_frozen_reqs, profile, claude_path=str(fake)
        )

        assert result.validation.passed
        assert result.promoted
        assert result.stage == "design"
        doc = json.loads((root / DESIGN_JSON).read_text())
        assert doc["frozen"] is False
        assert [el["id"] for el in doc["design_elements"]] == ["DES-001", "DES-002"]
        # requirement_mapping refs stitched: REQ refs against frozen upstream,
        # design_elements local refs against allocated DES ids (reference-integrity).
        req_of = {m["key"]: m["requirement"] for m in doc["requirement_mapping"]}
        assert req_of["m1"] == "REQ-001"
        assert req_of["m2"] == "REQ-002"
        des_of = {m["key"]: m["design_elements"] for m in doc["requirement_mapping"]}
        assert des_of["m1"] == ["DES-001"]
        assert des_of["m2"] == ["DES-001", "DES-002"]
        assert (root / DESIGN_MD).is_file()

    def test_passing_run_allocates_des_ids_from_counter(
        self, repo_root: Path, feature_with_frozen_reqs: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-design-ids")
        fake = _write_fake_claude(repo_root / "bin", variant="design")
        profile = _load_profile(repo_root)

        result = run_generate_design(
            repo_root, feature_with_frozen_reqs, profile, claude_path=str(fake)
        )
        assert result.promote is not None
        assert list(result.promote.allocated["DES"]) == ["DES-001", "DES-002"]

    def test_failed_validation_skips_promote(
        self, repo_root: Path, feature_with_frozen_reqs: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-design-fail")
        fake = _write_fake_claude(repo_root / "bin", variant="design_invalid")
        profile = _load_profile(repo_root)
        root = _feature_root(repo_root)

        result = run_generate_design(
            repo_root, feature_with_frozen_reqs, profile, claude_path=str(fake)
        )
        assert not result.validation.passed
        assert not result.promoted
        # The seeded placeholder design is untouched (no allocated DES ids).
        doc = json.loads((root / DESIGN_JSON).read_text())
        assert doc["design_elements"] == []

    def test_refinement_overwrites_unfrozen_design(
        self, repo_root: Path, feature_with_frozen_reqs: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-design-refine")
        fake = _write_fake_claude(repo_root / "bin", variant="design")
        profile = _load_profile(repo_root)
        root = _feature_root(repo_root)

        first = run_generate_design(
            repo_root, feature_with_frozen_reqs, profile, claude_path=str(fake)
        )
        second = run_generate_design(
            repo_root, feature_with_frozen_reqs, profile, feedback="tighten", claude_path=str(fake)
        )
        assert first.promoted and second.promoted
        assert first.run_id == "RUN-001"
        assert second.run_id == "RUN-002"
        doc = json.loads((root / DESIGN_JSON).read_text())
        # The counter advanced across passes (DES-003/004); the artifact is the 2nd.
        assert [el["id"] for el in doc["design_elements"]] == ["DES-003", "DES-004"]
        task_pkg = (
            run_dir(repo_root, feature_with_frozen_reqs, second.run_id) / "input" / TASK_PACKAGE_FILE
        ).read_text()
        assert "tighten" in task_pkg

    def test_refuses_to_promote_over_frozen_design(
        self, repo_root: Path, feature_with_frozen_reqs: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-design-frozen")
        fake = _write_fake_claude(repo_root / "bin", variant="design")
        profile = _load_profile(repo_root)
        root = _feature_root(repo_root)

        run_generate_design(repo_root, feature_with_frozen_reqs, profile, claude_path=str(fake))
        freeze_artifact(root, "design")
        with pytest.raises(ValueError, match="frozen"):
            run_generate_design(
                repo_root, feature_with_frozen_reqs, profile, claude_path=str(fake)
            )

    def test_refuses_if_requirements_not_frozen(
        self, repo_root: Path, feature: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-design-noupstream")
        fake = _write_fake_claude(repo_root / "bin", variant="design")
        profile = _load_profile(repo_root)
        # feature (not frozen reqs): design cannot stitch against an unfrozen upstream.
        with pytest.raises(ValueError, match="not frozen"):
            run_generate_design(repo_root, feature, profile, claude_path=str(fake))

    def test_audit_log_records_leg_lifecycle(
        self, repo_root: Path, feature_with_frozen_reqs: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-design-audit")
        fake = _write_fake_claude(repo_root / "bin", variant="design")
        profile = _load_profile(repo_root)

        run_generate_design(repo_root, feature_with_frozen_reqs, profile, claude_path=str(fake))

        from ai_dev.audit import AUDIT_LOG_JSON

        log = json.loads((_feature_root(repo_root) / AUDIT_LOG_JSON).read_text())
        events = [rec["event"] for rec in log]
        assert "prepare_run" in events
        assert "run" in events
        assert "validate" in events
        # The fixture's requirements promote already wrote one promote record, so
        # pin to the design-stage promote and assert it follows the design run's
        # validate (the only validate - the fixture promotes requirements directly,
        # without a run/validate).
        design_promote = next(
            i
            for i, rec in enumerate(log)
            if rec["event"] == "promote" and rec["payload"].get("stage") == "design"
        )
        assert design_promote > events.index("validate")


class TestGenerateDesignCLI:
    """The ``ai-dev generate-design`` command through the console entry, fake
    claude on PATH."""

    def _invoke(
        self,
        repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        feature_id: str,
        *extra: str,
    ) -> int:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-cli-design")
        fake_bin = _write_fake_claude(repo_root / "bin", variant="design")
        monkeypatch.setenv(
            "PATH", f"{fake_bin.parent}{os.pathsep}{os.environ['PATH']}"
        )
        return main(
            ["generate-design", feature_id, "--repo-root", str(repo_root), *extra]
        )

    def test_cli_passes_and_promotes(
        self, repo_root: Path, feature_with_frozen_reqs: str, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        rc = self._invoke(repo_root, monkeypatch, feature_with_frozen_reqs)
        assert rc == 0
        out = capsys.readouterr().out
        assert "GENERATE-DESIGN PASS" in out
        assert "DES=['DES-001', 'DES-002']" in out
        assert (_feature_root(repo_root) / DESIGN_JSON).is_file()

    def test_cli_carries_feedback(
        self, repo_root: Path, feature_with_frozen_reqs: str, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        rc = self._invoke(
            repo_root, monkeypatch, feature_with_frozen_reqs, "--feedback", "add a risk"
        )
        assert rc == 0
        task_pkg = (
            run_dir(repo_root, feature_with_frozen_reqs, "RUN-001") / "input" / TASK_PACKAGE_FILE
        ).read_text()
        assert "add a risk" in task_pkg

    def test_cli_resolves_planner_role_default(
        self, repo_root: Path, feature_with_frozen_reqs: str, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        rc = self._invoke(repo_root, monkeypatch, feature_with_frozen_reqs)
        assert rc == 0
        slots = load_feature_status(_feature_root(repo_root))["feature"]["agent_profiles"]
        assert slots.get("planner") == "cc-glm52"

    def test_cli_dry_run_mints_nothing(
        self, repo_root: Path, feature_with_frozen_reqs: str, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        monkeypatch.delenv("CC_GLM52_TOKEN", raising=False)
        rc = main(
            [
                "generate-design",
                feature_with_frozen_reqs,
                "--dry-run",
                "--repo-root",
                str(repo_root),
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "GENERATE-DESIGN DRY-RUN" in out
        assert "would_promote" in out
        runs = list(
            (repo_root / ".ai-dev" / "features" / feature_with_frozen_reqs / "runs").glob("RUN-*")
        )
        assert runs == []


# ===========================================================================
# Seam: the human freeze gate advances design_gate -> task_gate, AND the
# freeze-gate coverage precheck refuses a freeze on a REQ-coverage gap (§18.2).
# ===========================================================================


class TestFreezeDesignGateAndCoverage:
    """Freezing design advances current_gate design_gate -> task_gate (§18); a
    coverage gap (a REQ not in any requirement_mapping) refuses the freeze."""

    def test_freeze_advances_design_to_task_gate(
        self, repo_root: Path, feature_with_frozen_reqs: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-freeze-design")
        fake = _write_fake_claude(repo_root / "bin", variant="design")
        profile = _load_profile(repo_root)
        root = _feature_root(repo_root)

        # feature_with_frozen_reqs starts at design_gate (requirements frozen).
        assert load_feature_status(root)["feature"]["current_gate"] == "design_gate"

        run_generate_design(repo_root, feature_with_frozen_reqs, profile, claude_path=str(fake))
        # Coverage passes: both REQ-001 and REQ-002 are mapped.
        assert design_coverage(root).ok
        freeze_artifact(root, "design")

        status = load_feature_status(root)["feature"]
        assert status["frozen_artifacts"]["design"] is True
        assert status["current_gate"] == "task_gate"

    def test_freeze_refused_on_coverage_gap(
        self, repo_root: Path, feature_with_frozen_reqs: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A design proposal mapping only REQ-001 leaves REQ-002 uncovered; the
        # freeze-gate coverage precheck (§18.2) refuses to freeze.
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-freeze-gap")
        fake = _write_fake_claude(repo_root / "bin", variant="design_gap")
        profile = _load_profile(repo_root)
        root = _feature_root(repo_root)

        result = run_generate_design(
            repo_root, feature_with_frozen_reqs, profile, claude_path=str(fake)
        )
        assert result.promoted  # the proposal is schema-valid + promotes fine
        gap = design_coverage(root)
        assert not gap.ok
        assert gap.uncovered == ("REQ-002",)
        # The freeze primitive itself does NOT enforce coverage (it is a pure
        # low-level writer); the CLI layer gates on the precheck. A direct
        # freeze_artifact call would still flip the flag - the gate is the CLI's
        # job, exercised via the CLI test below.

    def test_freeze_via_cli_refuses_coverage_gap(
        self, repo_root: Path, feature_with_frozen_reqs: str, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-freeze-gap-cli")
        fake_bin = _write_fake_claude(repo_root / "bin", variant="design_gap")
        monkeypatch.setenv(
            "PATH", f"{fake_bin.parent}{os.pathsep}{os.environ['PATH']}"
        )
        root = _feature_root(repo_root)

        assert main(
            ["generate-design", feature_with_frozen_reqs, "--repo-root", str(repo_root)]
        ) == 0
        # Freeze is REFUSED: REQ-002 is uncovered; exit 1, design stays unfrozen,
        # current_gate stays design_gate (no advance).
        rc = main(
            ["freeze", feature_with_frozen_reqs, "design", "--repo-root", str(repo_root)]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "REQ-002" in err
        status = load_feature_status(root)["feature"]
        assert status["frozen_artifacts"]["design"] is False
        assert status["current_gate"] == "design_gate"

    def test_freeze_via_cli_advances_when_coverage_passes(
        self, repo_root: Path, feature_with_frozen_reqs: str, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-freeze-ok-cli")
        fake_bin = _write_fake_claude(repo_root / "bin", variant="design")
        monkeypatch.setenv(
            "PATH", f"{fake_bin.parent}{os.pathsep}{os.environ['PATH']}"
        )
        root = _feature_root(repo_root)

        assert main(
            ["generate-design", feature_with_frozen_reqs, "--repo-root", str(repo_root)]
        ) == 0
        assert main(
            ["freeze", feature_with_frozen_reqs, "design", "--repo-root", str(repo_root)]
        ) == 0
        assert load_feature_status(root)["feature"]["current_gate"] == "task_gate"


# ===========================================================================
# Ticket 04: the Planner tasks leg (generate -> promote -> freeze) + the
# task/lane-gate coverage precheck (every REQ+DES in some task, §18.2).
# ===========================================================================


class TestBuildTasksInputPackage:
    """The Planner tasks input package is assembled from the intent + the frozen
    requirements AND design (two upstreams), with the tasks proposal schema."""

    def test_intent_and_frozen_upstreams_flow_into_task_package(
        self, repo_root: Path, feature_with_frozen_reqs_and_design: str
    ) -> None:
        run_id = build_tasks_input_package(repo_root, feature_with_frozen_reqs_and_design)
        task_pkg = (
            run_dir(repo_root, feature_with_frozen_reqs_and_design, run_id)
            / "input"
            / TASK_PACKAGE_FILE
        ).read_text()
        # The original intent AND both frozen upstream id sets reach the Planner.
        assert _INTENT in task_pkg
        assert "REQ-001" in task_pkg and "REQ-002" in task_pkg
        assert "DES-001" in task_pkg and "DES-002" in task_pkg

    def test_feedback_carried_into_task_package(
        self, repo_root: Path, feature_with_frozen_reqs_and_design: str
    ) -> None:
        run_id = build_tasks_input_package(
            repo_root,
            feature_with_frozen_reqs_and_design,
            feedback="split greeting into its own task",
        )
        task_pkg = (
            run_dir(repo_root, feature_with_frozen_reqs_and_design, run_id)
            / "input"
            / TASK_PACKAGE_FILE
        ).read_text()
        assert "split greeting into its own task" in task_pkg
        assert "Human feedback" in task_pkg

    def test_role_pinned_to_planner(
        self, repo_root: Path, feature_with_frozen_reqs_and_design: str
    ) -> None:
        run_id = build_tasks_input_package(repo_root, feature_with_frozen_reqs_and_design)
        role_md = (
            run_dir(repo_root, feature_with_frozen_reqs_and_design, run_id)
            / "input"
            / ROLE_FILE
        ).read_text()
        assert PLANNER_ROLE in role_md

    def test_output_schema_is_the_tasks_proposal(
        self, repo_root: Path, feature_with_frozen_reqs_and_design: str
    ) -> None:
        run_id = build_tasks_input_package(repo_root, feature_with_frozen_reqs_and_design)
        schema_path = (
            run_dir(repo_root, feature_with_frozen_reqs_and_design, run_id)
            / "input"
            / OUTPUT_SCHEMA_FILE
        )
        written = json.loads(schema_path.read_text())
        assert written["title"] == TASKS_PROPOSAL_SCHEMA["title"]
        assert "lane_purpose" in written["required"]
        assert "tasks" in written["required"]

    def test_fails_loud_when_design_not_frozen(
        self, repo_root: Path, feature_with_frozen_reqs: str
    ) -> None:
        # feature_with_frozen_reqs: requirements frozen but design is NOT - tasks
        # may only stitch against frozen upstreams (ADR-0008 D2).
        with pytest.raises(ValueError, match="not frozen"):
            build_tasks_input_package(repo_root, feature_with_frozen_reqs)

    def test_fails_loud_when_requirements_not_frozen(
        self, repo_root: Path, feature: str
    ) -> None:
        # feature: requirements exist as a seeded template but are NOT frozen.
        with pytest.raises(ValueError, match="not frozen"):
            build_tasks_input_package(repo_root, feature)

    def test_fails_loud_when_feature_missing(self, repo_root: Path) -> None:
        with pytest.raises(ValueError, match="not found"):
            build_tasks_input_package(repo_root, "FEATURE-999")


class TestRunGenerateTasks:
    """The full Planner tasks leg with a fake claude: a passing run promotes
    (writing all four files); a failing run does not."""

    def test_passing_run_promotes_canonical_tasks_artifact(
        self, repo_root: Path, feature_with_frozen_reqs_and_design: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-tasks")
        fake = _write_fake_claude(repo_root / "bin", variant="tasks")
        profile = _load_profile(repo_root)
        root = _feature_root(repo_root)

        result = run_generate_tasks(
            repo_root, feature_with_frozen_reqs_and_design, profile, claude_path=str(fake)
        )

        assert result.validation.passed
        assert result.promoted
        assert result.stage == "tasks"
        doc = json.loads((root / TASKS_JSON).read_text())
        assert doc["frozen"] is False
        assert [t["id"] for t in doc["tasks"]] == ["TASK-001", "TASK-002"]
        assert doc["lane_purpose"] == "Implement the greet CLI end to end."
        # REQ/DES refs stitched against the frozen upstreams (reference-integrity).
        reqs_of = {t["key"]: t["related_requirements"] for t in doc["tasks"]}
        des_of = {t["key"]: t["related_design"] for t in doc["tasks"]}
        assert reqs_of["t1"] == ["REQ-001"]
        assert reqs_of["t2"] == ["REQ-002"]
        assert des_of["t1"] == ["DES-001"]
        assert des_of["t2"] == ["DES-001", "DES-002"]
        # The single seeded lane is assigned to every task.
        assert all(t["lane"] == "LANE-001" for t in doc["tasks"])
        assert (root / TASKS_MD).is_file()
        # promote also seeded task-status.yml (all pending) + populated the lane.
        status = yaml.safe_load((root / "status" / "task-status.yml").read_text())
        assert set(status["tasks"]) == {"TASK-001", "TASK-002"}
        for row in status["tasks"].values():
            assert row["status"] == "pending"
        # Derived ACs: TASK-001 -> REQ-001 -> [AC-001]; TASK-002 -> REQ-002 -> [AC-002].
        assert status["tasks"]["TASK-001"]["related_acceptance_criteria"] == ["AC-001"]
        assert status["tasks"]["TASK-002"]["related_acceptance_criteria"] == ["AC-002"]
        graph = yaml.safe_load((root / LANE_GRAPH_YML).read_text())
        lane = graph["lanes"][0]
        assert lane["purpose"] == "Implement the greet CLI end to end."
        assert lane["tasks"] == ["TASK-001", "TASK-002"]
        assert lane["expected_files"] == ["src/cli.py", "src/greet.py"]
        assert lane["exclusive_files"] == ["src/cli.py", "src/greet.py"]
        # v0.6 capstone (ticket 05): the Planner-generated verify command set is
        # promoted onto the lane so the Verifier runs model-generated commands
        # (zero hand-authored planning).
        assert [vc["name"] for vc in lane["verification_commands"]] == ["pytest", "mypy"]
        assert lane["verification_scope"] == ["pytest", "mypy"]

    def test_passing_run_allocates_task_ids_from_counter(
        self, repo_root: Path, feature_with_frozen_reqs_and_design: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-tasks-ids")
        fake = _write_fake_claude(repo_root / "bin", variant="tasks")
        profile = _load_profile(repo_root)

        result = run_generate_tasks(
            repo_root, feature_with_frozen_reqs_and_design, profile, claude_path=str(fake)
        )
        assert result.promote is not None
        assert list(result.promote.allocated["TASK"]) == ["TASK-001", "TASK-002"]

    def test_failed_validation_skips_promote(
        self, repo_root: Path, feature_with_frozen_reqs_and_design: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-tasks-fail")
        fake = _write_fake_claude(repo_root / "bin", variant="tasks_invalid")
        profile = _load_profile(repo_root)
        root = _feature_root(repo_root)

        result = run_generate_tasks(
            repo_root, feature_with_frozen_reqs_and_design, profile, claude_path=str(fake)
        )
        assert not result.validation.passed
        assert not result.promoted
        # The seeded placeholder tasks artifact is untouched (no allocated TASK ids).
        doc = json.loads((root / TASKS_JSON).read_text())
        assert doc["tasks"] == []

    def test_refinement_overwrites_unfrozen_tasks(
        self, repo_root: Path, feature_with_frozen_reqs_and_design: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-tasks-refine")
        fake = _write_fake_claude(repo_root / "bin", variant="tasks")
        profile = _load_profile(repo_root)
        root = _feature_root(repo_root)

        first = run_generate_tasks(
            repo_root, feature_with_frozen_reqs_and_design, profile, claude_path=str(fake)
        )
        second = run_generate_tasks(
            repo_root, feature_with_frozen_reqs_and_design, profile,
            feedback="tighten", claude_path=str(fake),
        )
        assert first.promoted and second.promoted
        assert first.run_id == "RUN-001"
        assert second.run_id == "RUN-002"
        doc = json.loads((root / TASKS_JSON).read_text())
        # The counter advanced across passes (TASK-003/004); artifact is the 2nd.
        assert [t["id"] for t in doc["tasks"]] == ["TASK-003", "TASK-004"]
        task_pkg = (
            run_dir(repo_root, feature_with_frozen_reqs_and_design, second.run_id)
            / "input"
            / TASK_PACKAGE_FILE
        ).read_text()
        assert "tighten" in task_pkg

    def test_refuses_to_promote_over_frozen_tasks(
        self, repo_root: Path, feature_with_frozen_reqs_and_design: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-tasks-frozen")
        fake = _write_fake_claude(repo_root / "bin", variant="tasks")
        profile = _load_profile(repo_root)
        root = _feature_root(repo_root)

        run_generate_tasks(
            repo_root, feature_with_frozen_reqs_and_design, profile, claude_path=str(fake)
        )
        freeze_artifact(root, "tasks")
        with pytest.raises(ValueError, match="frozen"):
            run_generate_tasks(
                repo_root, feature_with_frozen_reqs_and_design, profile, claude_path=str(fake)
            )

    def test_refuses_if_design_not_frozen(
        self, repo_root: Path, feature_with_frozen_reqs: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-tasks-nodesign")
        fake = _write_fake_claude(repo_root / "bin", variant="tasks")
        profile = _load_profile(repo_root)
        # feature_with_frozen_reqs: design not frozen -> tasks cannot stitch.
        with pytest.raises(ValueError, match="not frozen"):
            run_generate_tasks(
                repo_root, feature_with_frozen_reqs, profile, claude_path=str(fake)
            )

    def test_refuses_if_requirements_not_frozen(
        self, repo_root: Path, feature: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-tasks-noreqs")
        fake = _write_fake_claude(repo_root / "bin", variant="tasks")
        profile = _load_profile(repo_root)
        with pytest.raises(ValueError, match="not frozen"):
            run_generate_tasks(repo_root, feature, profile, claude_path=str(fake))

    def test_audit_log_records_leg_lifecycle(
        self, repo_root: Path, feature_with_frozen_reqs_and_design: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-tasks-audit")
        fake = _write_fake_claude(repo_root / "bin", variant="tasks")
        profile = _load_profile(repo_root)

        run_generate_tasks(
            repo_root, feature_with_frozen_reqs_and_design, profile, claude_path=str(fake)
        )

        from ai_dev.audit import AUDIT_LOG_JSON

        log = json.loads((_feature_root(repo_root) / AUDIT_LOG_JSON).read_text())
        events = [rec["event"] for rec in log]
        assert "prepare_run" in events
        assert "run" in events
        assert "validate" in events
        # The fixture promoted requirements + design directly (no run/validate),
        # so the only validate is the tasks leg's - pin the tasks-stage promote to
        # follow it.
        tasks_promote = next(
            i
            for i, rec in enumerate(log)
            if rec["event"] == "promote" and rec["payload"].get("stage") == "tasks"
        )
        assert tasks_promote > events.index("validate")


class TestGenerateTasksCLI:
    """The ``ai-dev generate-tasks`` command through the console entry, fake
    claude on PATH."""

    def _invoke(
        self,
        repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        feature_id: str,
        *extra: str,
    ) -> int:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-cli-tasks")
        fake_bin = _write_fake_claude(repo_root / "bin", variant="tasks")
        monkeypatch.setenv(
            "PATH", f"{fake_bin.parent}{os.pathsep}{os.environ['PATH']}"
        )
        return main(
            ["generate-tasks", feature_id, "--repo-root", str(repo_root), *extra]
        )

    def test_cli_passes_and_promotes(
        self, repo_root: Path, feature_with_frozen_reqs_and_design: str,
        monkeypatch: pytest.MonkeyPatch, capsys,
    ) -> None:
        rc = self._invoke(repo_root, monkeypatch, feature_with_frozen_reqs_and_design)
        assert rc == 0
        out = capsys.readouterr().out
        assert "GENERATE-TASKS PASS" in out
        assert "TASK=['TASK-001', 'TASK-002']" in out
        assert (_feature_root(repo_root) / TASKS_JSON).is_file()

    def test_cli_carries_feedback(
        self, repo_root: Path, feature_with_frozen_reqs_and_design: str,
        monkeypatch: pytest.MonkeyPatch, capsys,
    ) -> None:
        rc = self._invoke(
            repo_root, monkeypatch, feature_with_frozen_reqs_and_design,
            "--feedback", "add a verification task",
        )
        assert rc == 0
        task_pkg = (
            run_dir(repo_root, feature_with_frozen_reqs_and_design, "RUN-001")
            / "input"
            / TASK_PACKAGE_FILE
        ).read_text()
        assert "add a verification task" in task_pkg

    def test_cli_resolves_planner_role_default(
        self, repo_root: Path, feature_with_frozen_reqs_and_design: str,
        monkeypatch: pytest.MonkeyPatch, capsys,
    ) -> None:
        rc = self._invoke(repo_root, monkeypatch, feature_with_frozen_reqs_and_design)
        assert rc == 0
        slots = load_feature_status(_feature_root(repo_root))["feature"]["agent_profiles"]
        assert slots.get("planner") == "cc-glm52"

    def test_cli_dry_run_mints_nothing(
        self, repo_root: Path, feature_with_frozen_reqs_and_design: str,
        monkeypatch: pytest.MonkeyPatch, capsys,
    ) -> None:
        monkeypatch.delenv("CC_GLM52_TOKEN", raising=False)
        rc = main(
            [
                "generate-tasks",
                feature_with_frozen_reqs_and_design,
                "--dry-run",
                "--repo-root",
                str(repo_root),
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "GENERATE-TASKS DRY-RUN" in out
        assert "would_promote" in out
        runs = list(
            (repo_root / ".ai-dev" / "features" / feature_with_frozen_reqs_and_design / "runs").glob("RUN-*")
        )
        assert runs == []


class TestFreezeTasksGateAndCoverage:
    """The task/lane gate: ``freeze tasks`` runs the REQ+DES coverage precheck
    and advances current_gate task_gate -> lane_gate (§18); a coverage gap refuses
    the freeze. ``freeze lane_graph`` (the lane-graph half of the task-gate pair)
    carries no precheck and does not advance."""

    def test_freeze_tasks_advances_to_lane_gate(
        self, repo_root: Path, feature_with_frozen_reqs_and_design: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-freeze-tasks")
        fake = _write_fake_claude(repo_root / "bin", variant="tasks")
        profile = _load_profile(repo_root)
        root = _feature_root(repo_root)

        # feature_with_frozen_reqs_and_design starts at task_gate.
        assert load_feature_status(root)["feature"]["current_gate"] == "task_gate"

        run_generate_tasks(
            repo_root, feature_with_frozen_reqs_and_design, profile, claude_path=str(fake)
        )
        # Coverage passes: every REQ+DES is referenced by a task.
        assert tasks_coverage(root).ok
        freeze_artifact(root, "tasks")

        status = load_feature_status(root)["feature"]
        assert status["frozen_artifacts"]["tasks"] is True
        assert status["current_gate"] == "lane_gate"
        # lane_graph is the other half of the task-gate pair - not frozen yet.
        assert status["frozen_artifacts"]["lane_graph"] is False

    def test_freeze_lane_graph_no_precheck_no_advance(
        self, repo_root: Path, feature_with_frozen_reqs_and_design: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-freeze-lanegraph")
        fake = _write_fake_claude(repo_root / "bin", variant="tasks")
        profile = _load_profile(repo_root)
        root = _feature_root(repo_root)

        run_generate_tasks(
            repo_root, feature_with_frozen_reqs_and_design, profile, claude_path=str(fake)
        )
        freeze_artifact(root, "tasks")  # advances to lane_gate
        # lane_graph shares the task gate's window: no coverage precheck of its own
        # (freeze_gate_coverage returns None) and no current_gate advance.
        assert freeze_gate_coverage("lane_graph", root) is None
        freeze_artifact(root, "lane_graph")
        status = load_feature_status(root)["feature"]
        assert status["frozen_artifacts"]["lane_graph"] is True
        # Still lane_gate - freezing lane_graph does not advance.
        assert status["current_gate"] == "lane_gate"

    def test_freeze_refused_on_coverage_gap(
        self, repo_root: Path, feature_with_frozen_reqs_and_design: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A tasks proposal covering only REQ-001/DES-001 leaves REQ-002 + DES-002
        # uncovered; the task-gate coverage precheck (§18.2) refuses to freeze.
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-freeze-tasks-gap")
        fake = _write_fake_claude(repo_root / "bin", variant="tasks_gap")
        profile = _load_profile(repo_root)
        root = _feature_root(repo_root)

        result = run_generate_tasks(
            repo_root, feature_with_frozen_reqs_and_design, profile, claude_path=str(fake)
        )
        assert result.promoted  # schema-valid + promotes fine
        gap = tasks_coverage(root)
        assert not gap.ok
        assert set(gap.uncovered) == {"REQ-002", "DES-002"}

    def test_freeze_tasks_via_cli_refuses_coverage_gap(
        self, repo_root: Path, feature_with_frozen_reqs_and_design: str,
        monkeypatch: pytest.MonkeyPatch, capsys,
    ) -> None:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-freeze-tasks-gap-cli")
        fake_bin = _write_fake_claude(repo_root / "bin", variant="tasks_gap")
        monkeypatch.setenv(
            "PATH", f"{fake_bin.parent}{os.pathsep}{os.environ['PATH']}"
        )
        root = _feature_root(repo_root)

        assert main(
            ["generate-tasks", feature_with_frozen_reqs_and_design, "--repo-root", str(repo_root)]
        ) == 0
        # Freeze is REFUSED: REQ-002 + DES-002 uncovered; exit 1, tasks stay
        # unfrozen, current_gate stays task_gate (no advance).
        rc = main(
            ["freeze", feature_with_frozen_reqs_and_design, "tasks", "--repo-root", str(repo_root)]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "REQ-002" in err
        assert "DES-002" in err
        status = load_feature_status(root)["feature"]
        assert status["frozen_artifacts"]["tasks"] is False
        assert status["current_gate"] == "task_gate"

    def test_freeze_tasks_via_cli_advances_when_coverage_passes(
        self, repo_root: Path, feature_with_frozen_reqs_and_design: str,
        monkeypatch: pytest.MonkeyPatch, capsys,
    ) -> None:
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-freeze-tasks-ok-cli")
        fake_bin = _write_fake_claude(repo_root / "bin", variant="tasks")
        monkeypatch.setenv(
            "PATH", f"{fake_bin.parent}{os.pathsep}{os.environ['PATH']}"
        )
        root = _feature_root(repo_root)

        assert main(
            ["generate-tasks", feature_with_frozen_reqs_and_design, "--repo-root", str(repo_root)]
        ) == 0
        assert main(
            ["freeze", feature_with_frozen_reqs_and_design, "tasks", "--repo-root", str(repo_root)]
        ) == 0
        status = load_feature_status(root)["feature"]
        assert status["frozen_artifacts"]["tasks"] is True
        assert status["current_gate"] == "lane_gate"


# ---------------------------------------------------------------------------
# v0.7 capstone (ticket 07): multi-lane Planner tasks generation.
# ---------------------------------------------------------------------------


def _seed_two_lane_frozen_feature(repo_root: Path, write_profiles) -> str:
    """A 2-lane feature run with requirements + design promoted + frozen.

    Mirrors the ``feature_with_frozen_reqs_and_design`` fixture but seeds two
    lanes (``create_feature_run(..., lanes=2)``) so the tasks leg's
    multi-lane path is the unit under test. REQ-001/002 + AC-001/002 and
    DES-001/002 are allocated exactly as the single-lane fixture, so the same
    fake-claude refs resolve.
    """
    write_profiles(repo_root)
    create_feature_run(repo_root, _INTENT, lanes=2)
    root = _feature_root(repo_root)
    promote_requirements(
        root,
        FEATURE_ID,
        {
            "requirements": [
                {"key": "r1", "statement": "The CLI shall greet a named user."},
                {"key": "r2", "statement": "The CLI shall exit 0 on success."},
            ],
            "acceptance_criteria": [
                {"key": "a1", "requirement": "r1", "criterion": "greeting contains name"},
                {"key": "a2", "requirement": "r2", "criterion": "exit 0 on valid name"},
            ],
        },
        origin="test",
    )
    freeze_artifact(root, "requirements", origin="test")
    promote_design(
        root,
        FEATURE_ID,
        {
            "design_elements": [
                {"key": "d1", "name": "Greeting module",
                 "description": "formats the greeting"},
                {"key": "d2", "name": "Exit handling", "type": "module"},
            ],
            "requirement_mapping": [
                {"key": "m1", "requirement": "REQ-001", "design_elements": ["d1"]},
                {"key": "m2", "requirement": "REQ-002", "design_elements": ["d1", "d2"]},
            ],
            "architecture_decision": "single module",
            "invariants": ["deterministic greeting"],
        },
        origin="test",
    )
    freeze_artifact(root, "design", origin="test")
    return FEATURE_ID


class TestBuildTasksInputPackageMultiLane:
    """v0.7: when the feature has >1 seeded lane, the tasks package instructs
    the Planner to emit a ``lanes`` array + per-task ``lane`` assignment; the
    single-lane package keeps the v0.6 ``one MVP lane`` phrasing."""

    def test_two_lane_package_carries_both_lane_ids_and_lanes_instruction(
        self, repo_root: Path, write_profiles
    ) -> None:
        feature_id = _seed_two_lane_frozen_feature(repo_root, write_profiles)
        run_id = build_tasks_input_package(repo_root, feature_id)
        task_pkg = (
            run_dir(repo_root, feature_id, run_id) / "input" / TASK_PACKAGE_FILE
        ).read_text()
        # Both seeded lane ids reach the Planner, plus the multi-lane `lanes`
        # array contract and the per-task `lane` assignment instruction.
        assert "LANE-001" in task_pkg
        assert "LANE-002" in task_pkg
        assert "lanes" in task_pkg
        assert "exactly one lane" in task_pkg

    def test_single_lane_package_keeps_one_lane_phrasing(
        self, repo_root: Path, feature_with_frozen_reqs_and_design: str
    ) -> None:
        run_id = build_tasks_input_package(
            repo_root, feature_with_frozen_reqs_and_design
        )
        task_pkg = (
            run_dir(repo_root, feature_with_frozen_reqs_and_design, run_id)
            / "input"
            / TASK_PACKAGE_FILE
        ).read_text()
        # Backward compat: the v0.6 single-lane prompt is unchanged - the model
        # is told there is one MVP lane and it does NOT assign lanes.
        assert "one MVP lane" in task_pkg or "do NOT assign lanes" in task_pkg
        # The multi-lane contract is NOT surfaced for one lane: no second lane
        # id and no per-task lane-assignment instruction.
        assert "LANE-002" not in task_pkg
        assert "exactly one lane" not in task_pkg


class TestRunGenerateTasksMultiLane:
    """A 2-lane ``lanes``-array proposal promotes into a 2-lane lane-graph:
    each lane gets its own purpose / tasks / files / verify commands."""

    def test_two_lane_proposal_promotes_two_lane_graph(
        self, repo_root: Path, write_profiles, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        feature_id = _seed_two_lane_frozen_feature(repo_root, write_profiles)
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok-tasks-two-lane")
        fake = _write_fake_claude(repo_root / "bin", variant="tasks_two_lane")
        profile = _load_profile(repo_root)
        root = _feature_root(repo_root)

        result = run_generate_tasks(
            repo_root, feature_id, profile, claude_path=str(fake)
        )

        assert result.validation.passed
        assert result.promoted
        doc = json.loads((root / TASKS_JSON).read_text())
        # Tasks are assigned to different lanes by the Planner's `lane` field.
        lane_of = {t["key"]: t["lane"] for t in doc["tasks"]}
        assert lane_of["t1"] == "LANE-001"
        assert lane_of["t2"] == "LANE-002"
        graph = yaml.safe_load((root / LANE_GRAPH_YML).read_text())
        assert [lane["id"] for lane in graph["lanes"]] == ["LANE-001", "LANE-002"]
        lane1, lane2 = graph["lanes"]
        # Each lane carries its own Planner-authored purpose + tasks + files.
        assert lane1["purpose"] == "Greeting module lane"
        assert lane1["tasks"] == ["TASK-001"]
        assert lane1["expected_files"] == ["src/greet.py"]
        assert lane2["purpose"] == "Exit handling lane"
        assert lane2["tasks"] == ["TASK-002"]
        assert lane2["expected_files"] == ["src/cli.py"]
        # Each lane got its own verify command set (zero hand-authored planning).
        assert [vc["name"] for vc in lane1["verification_commands"]] == ["pytest", "mypy"]
        assert [vc["name"] for vc in lane2["verification_commands"]] == ["pytest", "mypy"]
        # lane-status synced to both lanes.
        lane_status = yaml.safe_load(
            (root / "status" / "lane-status.yml").read_text()
        )
        assert list(lane_status["lanes"]) == ["LANE-001", "LANE-002"]
