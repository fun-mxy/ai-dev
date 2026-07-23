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
from ai_dev.feature_run import create_feature_run
from ai_dev.paths import run_dir
from ai_dev.planner_leg import (
    build_requirements_input_package,
    read_intent,
    run_generate_requirements,
)
from ai_dev.planner_schemas import PLANNER_ROLE, REQUIREMENTS_PROPOSAL_SCHEMA
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
from ai_dev.templates import REQUIREMENTS_JSON, REQUIREMENTS_MD

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


# A fake ``claude`` that writes a schema-valid id-free requirements proposal
# (ticket-01 schema) + result.md into its cwd. Stands in for the real CLI so the
# generate→promote slice is exercised end-to-end without network or token.
# ``__PY__`` is replaced with the test interpreter so the shebang resolves under
# ``uv run`` (string replace, not ``.format``, so the JSON braces are literal).
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
    """Write the fake ``claude`` script into ``bin_dir`` and return its path."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "claude"
    src = _FAKE_CLAUDE if variant == "valid" else _FAKE_CLAUDE_INVALID
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
