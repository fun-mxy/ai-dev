"""``--dry-run`` mode (v0.4 ticket 04 / ADR-0004).

The only *new capability* of v0.4: a ``--dry-run`` flag on the side-effect
commands that runs the command's full planning + §24.2 precondition + legality
check but skips the expensive/irreversible step (claude spawn for agent
commands; canonical-state write for deterministic commands).

These tests pin the three invariants the ticket names:

* **dry-run never mints a stable id** - the ``RUN`` / ``DEC`` counters in
  ``id-counters.yml`` are byte-identical before and after a dry-run, and the
  feature-run tree is unchanged (no ``runs/RUN-NNN`` created);
* **dry-run writes nothing** - no canonical state, no audit append (the
  ``origin=dry-run`` audit tag is deferred to ticket 02, ADR-0004);
* **dry-run plans + refuses cleanly** - a plan is printed and exits 0; a
  legality refusal is *reported* (``would be refused``) rather than raised; a
  §24.2 precondition failure still exits 1.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ai_dev.audit import AUDIT_LOG_JSON
from ai_dev.cli import main
from ai_dev.dry_run import (
    DryRunPlan,
    plan_coherence_gate,
    plan_final_report,
    plan_fix_run,
    plan_freeze,
    plan_generate_design,
    plan_implement,
    plan_lane_gate,
    plan_review,
    plan_run_headless,
    plan_spec_gap,
    plan_triage,
    render_plan,
)
from ai_dev.feature_run import create_feature_run
from ai_dev.feature_ids import ID_COUNTERS_FILE
from ai_dev.final_report import FINAL_REPORT_JSON
from ai_dev.lane_gate import LANE_DECISION_JSON
from ai_dev.profiles import load_profile
from ai_dev.promote import promote_requirements
from ai_dev.run_prepare import prepare_run
from ai_dev.status import freeze_artifact
from ai_dev.templates import DESIGN_JSON, REQUIREMENTS_JSON

from test_checking_legs import _stage_implement_run  # noqa: E402
from test_implement_leg import _feature_root, _seed_frozen_feature  # noqa: E402


# ---------------------------------------------------------------------------
# Tree / counter snapshot helpers.
# ---------------------------------------------------------------------------


def _inventory(root: Path) -> dict[str, bytes]:
    """Recursive ``{rel_path: file_bytes}`` snapshot of a feature run tree."""
    if not root.is_dir():
        return {}
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            files[str(path.relative_to(root))] = path.read_bytes()
    return files


def _counters(feature_root: Path) -> dict[str, int]:
    """Read the feature's ``id-counters.yml`` (empty when no id minted yet)."""
    path = feature_root / ID_COUNTERS_FILE
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return {k: int(v) for k, v in data.items()}


def _audit_count(feature_root: Path) -> int:
    path = feature_root / AUDIT_LOG_JSON
    if not path.is_file():
        return 0
    return len(json.loads(path.read_text()))


def _exhaust_fix_loop_budget(feature_root: Path) -> None:
    """Force ``fix_loop_budget`` to ``used == max`` so the ADR-0002 D5 guard fires."""
    status_path = feature_root / "status" / "feature-status.yml"
    doc = yaml.safe_load(status_path.read_text())
    doc["feature"]["fix_loop_budget"] = {"used": 1, "max": 1}
    status_path.write_text(yaml.safe_dump(doc, sort_keys=False))


def _run_cli(repo_root: Path, argv: list[str]) -> int:
    """Run ``main`` with ``--repo-root`` appended; return the exit code."""
    return main([*argv, "--repo-root", str(repo_root)])


def _stage_frozen_requirements(repo_root: Path, feature_id: str) -> Path:
    """Create a feature run + promote + freeze requirements (REQ-001/002).

    The design leg (ticket 03) may only run against a *frozen* requirements
    upstream (ADR-0008 D2). Promotes + freezes directly (not via the requirements
    leg) to keep the design dry-run seam the unit under test. Returns the feature
    root.
    """
    feature_root = _feature_root(repo_root, feature_id)
    promote_requirements(
        feature_root,
        feature_id,
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
    freeze_artifact(feature_root, "requirements", origin="test")
    return feature_root


def _write_design_doc(feature_root: Path, *, covered_reqs: list[str]) -> None:
    """Write a canonical ``02-design.json`` whose ``requirement_mapping`` covers
    exactly the given REQ ids (the freeze-gate coverage precheck reads this)."""
    mapping = [
        {"key": f"m{i}", "requirement": req, "design_elements": ["DES-001"]}
        for i, req in enumerate(covered_reqs)
    ]
    (feature_root / DESIGN_JSON).write_text(
        json.dumps(
            {
                "feature_id": "FEATURE-001",
                "frozen": False,
                "design_elements": [{"id": "DES-001", "name": "core"}],
                "requirement_mapping": mapping,
            }
        )
    )


# ---------------------------------------------------------------------------
# Agent dry-run: plan + no spawn + no id + no write.
# ---------------------------------------------------------------------------


class TestAgentDryRun:
    """Agent commands plan the invocation without spawning or minting."""

    def test_plan_implement_no_spawn_no_mint_no_write(
        self,
        repo_root: Path,
        write_profiles,
        clean_token_env,
        monkeypatch,
    ) -> None:
        write_profiles(repo_root)
        monkeypatch.setenv("CC_GLM52_TOKEN", "test-token")
        feature_id, lane_id = _seed_frozen_feature(
            repo_root, expected_files=["workspace/hello.py"]
        )
        feature_root = _feature_root(repo_root, feature_id)
        profile = load_profile(repo_root, "cc-glm52")

        before_tree = _inventory(feature_root)
        before_counters = _counters(feature_root)
        before_audit = _audit_count(feature_root)

        plan = plan_implement(repo_root, feature_id, lane_id, profile)

        # Plan content: the would-be invocation + boundary are reported.
        assert plan.command == "implement"
        assert plan.details["would_spawn"] is True
        assert plan.details["invocation"][0] == "claude"
        assert "-p" in plan.details["invocation"]
        assert "output/result.json" in plan.details["allowed_files"]
        assert "workspace/hello.py" in plan.details["allowed_files"]
        assert plan.details["would_mint_ids"]  # placeholder, not a real id

        # No spawn, no mint, no write: the feature tree + counters + audit are
        # byte-identical. No RUN-NNN directory was created under runs/.
        assert _inventory(feature_root) == before_tree
        assert _counters(feature_root) == before_counters
        assert _audit_count(feature_root) == before_audit
        run_dirs = [p for p in (feature_root / "runs").glob("RUN-*")] if (feature_root / "runs").is_dir() else []
        assert run_dirs == []

    def test_implement_cli_dry_run_exits_0_prints_plan(
        self,
        repo_root: Path,
        write_profiles,
        clean_token_env,
        monkeypatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        write_profiles(repo_root)
        monkeypatch.setenv("CC_GLM52_TOKEN", "test-token")
        feature_id, lane_id = _seed_frozen_feature(repo_root)
        feature_root = _feature_root(repo_root, feature_id)
        before = _inventory(feature_root)

        rc = _run_cli(
            repo_root, ["implement", feature_id, lane_id, "--dry-run"]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "IMPLEMENT DRY-RUN" in out
        assert "would_spawn: true" in out
        # The feature tree is untouched end to end through the CLI path.
        assert _inventory(feature_root) == before

    def test_implement_unfrozen_precondition_exits_1(
        self, repo_root, write_profiles, clean_token_env, monkeypatch, capsys
    ) -> None:
        write_profiles(repo_root)
        monkeypatch.setenv("CC_GLM52_TOKEN", "test-token")
        feature_id, lane_id = _seed_frozen_feature(repo_root, freeze=False)

        rc = _run_cli(repo_root, ["implement", feature_id, lane_id, "--dry-run"])
        assert rc == 1
        assert "frozen tasks + lane_graph" in capsys.readouterr().err

    def test_review_requires_implement_result(
        self, repo_root, write_profiles, clean_token_env, monkeypatch, capsys
    ) -> None:
        write_profiles(repo_root)
        monkeypatch.setenv("CC_GLM52_TOKEN", "test-token")
        feature_id, lane_id = _seed_frozen_feature(repo_root)
        profile = load_profile(repo_root, "cc-glm52")

        with pytest.raises(ValueError, match="implement-result"):
            plan_review(repo_root, feature_id, lane_id, profile)

    def test_checking_leg_plans_without_spawn_or_mint(
        self,
        repo_root: Path,
        write_profiles,
        clean_token_env,
        monkeypatch,
    ) -> None:
        # review + spec-gap share _plan_checking; stage a real implement run so
        # the planner reaches the would-be package + invocation (the only happy
        # path through _plan_checking, previously untested).
        write_profiles(repo_root)
        monkeypatch.setenv("CC_GLM52_TOKEN", "test-token")
        feature_id, lane_id, impl_run_id = _stage_implement_run(repo_root)
        feature_root = _feature_root(repo_root, feature_id)
        profile = load_profile(repo_root, "cc-glm52")
        before_tree = _inventory(feature_root)
        before_counters = _counters(feature_root)

        review = plan_review(repo_root, feature_id, lane_id, profile)
        assert review.command == "review"
        assert review.details["would_spawn"] is True
        assert review.details["implement_run_id"] == impl_run_id
        assert review.details["role"] == "Code Reviewer"
        assert review.details["invocation"][0] == "claude"
        # No id minted, no spawn, feature tree untouched.
        assert review.details["would_mint_ids"] == ["RUN-NNN (next monotonic)"]
        assert _counters(feature_root) == before_counters
        assert _inventory(feature_root) == before_tree

        # spec-gap exercises the other role through the same shared planner.
        spec_gap = plan_spec_gap(repo_root, feature_id, lane_id, profile)
        assert spec_gap.command == "spec-gap"
        assert spec_gap.details["role"] == "Spec Gap Analyst"
        assert spec_gap.details["would_spawn"] is True
        assert _inventory(feature_root) == before_tree

    def test_token_source_not_set_exits_1(
        self,
        repo_root: Path,
        write_profiles,
        clean_token_env,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # clean_token_env deletes both token vars -> §24.2 precondition fails at
        # the planner's _require_token_source guard (not at profile load).
        write_profiles(repo_root)
        feature_id, lane_id = _seed_frozen_feature(repo_root)

        rc = _run_cli(repo_root, ["implement", feature_id, lane_id, "--dry-run"])
        assert rc == 1
        assert "token source not set" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "planner", [plan_implement, plan_review, plan_spec_gap]
    )
    def test_agent_planner_missing_feature_raises(
        self,
        repo_root: Path,
        write_profiles,
        clean_token_env,
        monkeypatch,
        planner,
    ) -> None:
        # The agent planners share the feature-exists precondition; one
        # parametrized check pins it for implement + both checking legs.
        write_profiles(repo_root)
        monkeypatch.setenv("CC_GLM52_TOKEN", "test-token")
        profile = load_profile(repo_root, "cc-glm52")
        with pytest.raises(ValueError, match="FEATURE-404"):
            planner(repo_root, "FEATURE-404", "LANE-001", profile)

    def test_run_headless_missing_run_exits_1(
        self, repo_root, write_profiles, clean_token_env, monkeypatch, capsys
    ) -> None:
        write_profiles(repo_root)
        monkeypatch.setenv("CC_GLM52_TOKEN", "test-token")
        feature_id, _ = _seed_frozen_feature(repo_root)

        rc = _run_cli(
            repo_root, ["run-headless", feature_id, "RUN-999", "--dry-run"]
        )
        assert rc == 1
        assert "RUN-999" in capsys.readouterr().err

    def test_run_headless_against_prepared_run(
        self, repo_root, write_profiles, clean_token_env, monkeypatch
    ) -> None:
        write_profiles(repo_root)
        monkeypatch.setenv("CC_GLM52_TOKEN", "test-token")
        feature_id, _ = _seed_frozen_feature(repo_root)
        # A real prepare-run mints RUN-001; dry-run run-headless plans against it
        # without spawning and without bumping the counter again.
        run_id = prepare_run(
            repo_root, feature_id, "Implementer", "do something",
            allowed_files=["workspace/hello.py"],
        )
        assert run_id == "RUN-001"
        feature_root = _feature_root(repo_root, feature_id)
        before_counters = _counters(feature_root)
        before_tree = _inventory(feature_root)
        profile = load_profile(repo_root, "cc-glm52")

        plan = plan_run_headless(repo_root, feature_id, run_id, profile)
        assert plan.details["run_id"] == run_id
        assert plan.details["would_spawn"] is True
        # Counter AND feature tree untouched (the run-headless dry-run must not
        # materialise the wrapper's .run-settings.json into runs/RUN-NNN).
        assert _counters(feature_root) == before_counters
        assert _inventory(feature_root) == before_tree


# ---------------------------------------------------------------------------
# Deterministic dry-run: would-write reported, nothing written.
# ---------------------------------------------------------------------------


class TestFreezeDryRun:
    def test_would_freeze_no_write(self, repo_root: Path) -> None:
        from ai_dev.feature_run import create_feature_run

        feature_id = create_feature_run(repo_root, "freeze dry-run test")
        feature_root = _feature_root(repo_root, feature_id)
        before = _inventory(feature_root)

        # ``requirements`` is the root (no upstream coverage precheck), so a fresh
        # feature plans a clean freeze + advance with no refusal. (``tasks`` now
        # carries a REQ+DES coverage precheck that raises on a fresh feature -
        # its dry-run is covered in TestTasksFreezeDryRun.)
        plan = plan_freeze(repo_root, feature_id, "requirements")
        assert plan.details["currently_frozen"] is False
        assert plan.details["would_be_refused"] is False
        assert plan.details["would_advance_current_gate_to"] == "design_gate"
        assert _inventory(feature_root) == before

    def test_already_frozen_reported_not_raised(self, repo_root: Path) -> None:
        from ai_dev.feature_run import create_feature_run

        feature_id = create_feature_run(repo_root, "freeze dry-run test")
        feature_root = _feature_root(repo_root, feature_id)
        freeze_artifact(feature_root, "requirements")
        before = _inventory(feature_root)

        plan = plan_freeze(repo_root, feature_id, "requirements")
        assert plan.details["would_be_refused"] is True
        assert "already frozen" in plan.details["refusal_reason"]
        assert _inventory(feature_root) == before

    def test_unknown_artifact_raises(self, repo_root: Path) -> None:
        # The CLI's ``artifact`` arg is choices-bounded (argparse rejects unknown
        # values before main), so the library guard is exercised directly here.
        from ai_dev.feature_run import create_feature_run

        feature_id = create_feature_run(repo_root, "freeze dry-run test")
        with pytest.raises(ValueError, match="unknown frozen artifact"):
            plan_freeze(repo_root, feature_id, "nope")

    def test_design_coverage_gap_refused_exit_0(self, repo_root: Path) -> None:
        # ADR-0008 D3: a REQ missing from every design requirement_mapping is a
        # legality refusal the real freeze would reject - dry-run reports it as
        # ``would be refused`` and exits 0 (the dry-run convention).
        feature_id = create_feature_run(repo_root, "design freeze coverage gap")
        feature_root = _stage_frozen_requirements(repo_root, feature_id)
        _write_design_doc(feature_root, covered_reqs=["REQ-001"])  # REQ-002 gap
        before = _inventory(feature_root)

        plan = plan_freeze(repo_root, feature_id, "design")
        assert plan.details["would_be_refused"] is True
        assert "coverage" in plan.details["refusal_reason"]
        assert "REQ-002" in plan.details["refusal_reason"]
        assert "REFUSED" in plan.summary and "coverage gap" in plan.summary
        assert _inventory(feature_root) == before

    def test_design_coverage_pass_would_freeze(self, repo_root: Path) -> None:
        feature_id = create_feature_run(repo_root, "design freeze coverage pass")
        feature_root = _stage_frozen_requirements(repo_root, feature_id)
        _write_design_doc(feature_root, covered_reqs=["REQ-001", "REQ-002"])
        before = _inventory(feature_root)

        plan = plan_freeze(repo_root, feature_id, "design")
        assert plan.details["would_be_refused"] is False
        assert plan.details["would_advance_current_gate_to"] == "task_gate"
        assert _inventory(feature_root) == before

    def test_design_freeze_before_requirements_frozen_raises(
        self, repo_root: Path
    ) -> None:
        # A corrupt precondition (requirements not frozen) is a §24.2 failure,
        # not a refusal: it raises ValueError (exit 1), mirroring the real CLI.
        feature_id = create_feature_run(repo_root, "design freeze no upstream")
        with pytest.raises(ValueError, match="is not frozen"):
            plan_freeze(repo_root, feature_id, "design")

    def test_design_freeze_no_design_promoted_raises(self, repo_root: Path) -> None:
        # Requirements frozen but 02-design.json deleted/unreadable - a corrupt
        # precondition -> ValueError (exit 1), not a refusal. (The seeded empty
        # design template normally stands in here; deleting it exercises the
        # missing-artifact guard the real freeze surfaces as a clean error.)
        feature_id = create_feature_run(repo_root, "design freeze nothing to freeze")
        feature_root = _stage_frozen_requirements(repo_root, feature_id)
        (feature_root / DESIGN_JSON).unlink()
        with pytest.raises(ValueError, match="nothing to freeze"):
            plan_freeze(repo_root, feature_id, "design")


class TestGenerateDesignDryRun:
    """``generate-design --dry-run``: plan the leg, mint/spell/write nothing."""

    def test_plans_design_leg_no_mint_no_write(
        self, repo_root: Path, write_profiles, clean_token_env, monkeypatch
    ) -> None:
        write_profiles(repo_root)
        monkeypatch.setenv("CC_GLM52_TOKEN", "test-token")
        feature_id = create_feature_run(repo_root, "design dry-run plan")
        feature_root = _stage_frozen_requirements(repo_root, feature_id)
        profile = load_profile(repo_root, "cc-glm52")
        before_counters = _counters(feature_root)
        before_tree = _inventory(feature_root)

        plan = plan_generate_design(repo_root, feature_id, profile)
        assert plan.command == "generate-design"
        assert plan.details["role"] == "Planner"
        assert plan.details["stage"] == "design"
        assert any("02-design.json" in w for w in plan.details["would_write"])
        assert any("02-design.md" in w for w in plan.details["would_write"])
        assert "frozen REQ" in plan.details["upstream"]
        assert "design proposal" in plan.details["output_schema"]
        # No id minted, no canonical write, no audit append.
        assert _counters(feature_root) == before_counters
        assert _inventory(feature_root) == before_tree

    def test_not_frozen_requirements_precondition_raises(
        self, repo_root: Path, write_profiles, clean_token_env, monkeypatch
    ) -> None:
        # design may only stitch against a frozen upstream (ADR-0008 D2); an
        # unfrozen requirements is a §24.2 precondition failure -> ValueError.
        write_profiles(repo_root)
        monkeypatch.setenv("CC_GLM52_TOKEN", "test-token")
        feature_id = create_feature_run(repo_root, "design dry-run no upstream")
        profile = load_profile(repo_root, "cc-glm52")
        with pytest.raises(ValueError, match="is not frozen"):
            plan_generate_design(repo_root, feature_id, profile)

    def test_missing_feature_raises(
        self, repo_root: Path, write_profiles, clean_token_env, monkeypatch
    ) -> None:
        write_profiles(repo_root)
        monkeypatch.setenv("CC_GLM52_TOKEN", "test-token")
        profile = load_profile(repo_root, "cc-glm52")
        with pytest.raises(ValueError, match="FEATURE-404"):
            plan_generate_design(repo_root, "FEATURE-404", profile)

    def test_cli_dry_run_exits_0_prints_plan(
        self, repo_root: Path, write_profiles, clean_token_env, monkeypatch, capsys
    ) -> None:
        write_profiles(repo_root)
        monkeypatch.setenv("CC_GLM52_TOKEN", "test-token")
        feature_id = create_feature_run(repo_root, "design dry-run cli")
        _stage_frozen_requirements(repo_root, feature_id)
        feature_root = _feature_root(repo_root, feature_id)
        before_counters = _counters(feature_root)
        before_tree = _inventory(feature_root)

        rc = _run_cli(repo_root, ["generate-design", feature_id, "--dry-run"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "GENERATE-DESIGN DRY-RUN" in out
        assert "02-design.json" in out
        # Dry-run mints/spells/writes nothing.
        assert _counters(feature_root) == before_counters
        assert _inventory(feature_root) == before_tree


class TestTriageDryRun:
    """Triage dry-run validates a disposition without recording it."""

    def test_would_apply_no_write_no_dec(self, repo_root: Path) -> None:
        from test_triage import _stage_issue

        feature_root, feature_id, issue_id, _ = _stage_issue(repo_root, severity="P2")
        before = _inventory(feature_root)
        before_counters = _counters(feature_root)

        plan = plan_triage(repo_root, feature_id, issue_id, "accept", None, "human")
        assert plan.details["would_be_refused"] is False
        assert plan.details["would_mint_ids"] == []
        assert _inventory(feature_root) == before
        assert _counters(feature_root) == before_counters

    def test_illegal_cell_refused_exit_0(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from test_triage import _stage_issue

        _stage_issue(repo_root, severity="P0")
        rc = _run_cli(
            repo_root,
            [
                "triage",
                "FEATURE-001",
                "--issue",
                "ISSUE-001",
                "--disposition",
                "override",
                "--reason",
                "x",
                "--dry-run",
            ],
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "would be REFUSED" in out
        assert "P0 cannot be waived by override" in out

    def test_disarming_dec_not_minted(self, repo_root: Path) -> None:
        from test_triage import _stage_issue

        feature_root, feature_id, issue_id, _ = _stage_issue(repo_root, severity="P1")
        before_counters = _counters(feature_root)
        before = _inventory(feature_root)

        plan = plan_triage(
            repo_root, feature_id, issue_id, "override", "rationale", "human"
        )
        # override x P1 would mint a DEC - but dry-run does not.
        assert plan.details["would_mint_ids"] == ["DEC-NNN (p1_override)"]
        assert _counters(feature_root) == before_counters
        assert _inventory(feature_root) == before

    def test_missing_issue_precondition_exits_1(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from ai_dev.feature_run import create_feature_run

        feature_id = create_feature_run(repo_root, "triage dry-run test")
        rc = _run_cli(
            repo_root,
            [
                "triage",
                feature_id,
                "--issue",
                "ISSUE-999",
                "--disposition",
                "accept",
                "--dry-run",
            ],
        )
        assert rc == 1
        assert "ISSUE-999" in capsys.readouterr().err

    def test_disarming_without_reason_refused_exit_0(self, repo_root: Path) -> None:
        # override x P1 disarms (would mint a DEC) so a reason is mandatory
        # (ADR-0001 #6). dry-run reports the refusal in the plan (exit 0) rather
        # than raising - it is a preview of "this would be refused".
        from test_triage import _stage_issue

        feature_root, feature_id, issue_id, _ = _stage_issue(repo_root, severity="P1")
        before = _inventory(feature_root)

        plan = plan_triage(repo_root, feature_id, issue_id, "override", None, "human")
        assert plan.details["would_be_refused"] is True
        assert "requires a reason" in plan.details["refusal_reason"]
        # Nothing written (no DEC, no triage state).
        assert _inventory(feature_root) == before

    def test_request_fix_budget_exhausted_refused_exit_0(self, repo_root: Path) -> None:
        # ADR-0002 D5: request_fix is refused once fix_loop_budget is exhausted.
        # dry-run surfaces this as a would_be_refused plan (exit 0), validating
        # the disposition without consuming or recording anything.
        from test_triage import _stage_issue

        feature_root, feature_id, issue_id, _ = _stage_issue(repo_root, severity="P1")
        _exhaust_fix_loop_budget(feature_root)
        before = _inventory(feature_root)

        plan = plan_triage(
            repo_root, feature_id, issue_id, "request_fix", "Try again.", "human"
        )
        assert plan.details["would_be_refused"] is True
        assert "fix_loop_budget" in plan.details["refusal_reason"]
        assert _inventory(feature_root) == before

    def test_unknown_disposition_raises(self, repo_root: Path) -> None:
        from test_triage import _stage_issue

        _stage_issue(repo_root, severity="P1")
        with pytest.raises(ValueError, match="unknown disposition"):
            plan_triage(
                repo_root, "FEATURE-001", "ISSUE-001", "nope", None, "human"
            )

    def test_unknown_issue_severity_raises(self, repo_root: Path) -> None:
        from ai_dev.feature_run import create_feature_run

        feature_id = create_feature_run(repo_root, "triage severity test")
        feature_root = _feature_root(repo_root, feature_id)
        issue_path = feature_root / "issues" / "ISSUE-001.json"
        issue_path.parent.mkdir(parents=True, exist_ok=True)
        issue_path.write_text(json.dumps({"id": "ISSUE-001", "severity": "P9"}))

        with pytest.raises(ValueError, match="unknown severity"):
            plan_triage(
                repo_root, feature_id, "ISSUE-001", "accept", None, "human"
            )


class TestDryRunPreconditionFeatureNotFound:
    """Every planner fails loud (§24.2) when its feature does not exist.

    The CLI surfaces the ValueError as a clean ``error:`` + exit 1; the
    precondition guard is shared across the deterministic + agent planners, so
    one parametrized check pins the contract for all of them.
    """

    @pytest.mark.parametrize(
        ("planner", "extra_args"),
        [
            (plan_freeze, ("tasks",)),
            (plan_coherence_gate, ()),
            (plan_final_report, ()),
        ],
    )
    def test_deterministic_planner_missing_feature_raises(
        self, repo_root: Path, planner, extra_args: tuple
    ) -> None:
        with pytest.raises(ValueError, match="FEATURE-404"):
            planner(repo_root, "FEATURE-404", *extra_args)

    def test_triage_missing_feature_raises(self, repo_root: Path) -> None:
        with pytest.raises(ValueError, match="FEATURE-404"):
            plan_triage(
                repo_root, "FEATURE-404", "ISSUE-001", "accept", None, "human"
            )

    def test_lane_gate_missing_feature_and_lane_raise(
        self, repo_root: Path
    ) -> None:
        with pytest.raises(ValueError, match="FEATURE-404"):
            plan_lane_gate(repo_root, "FEATURE-404", "LANE-001")


class TestGateDryRun:
    """coherence-gate / lane-gate / final-report compute the verdict, write nothing."""

    def test_lane_gate_no_write(self, repo_root: Path) -> None:
        from test_lane_gate import _stage_lane_gate_inputs

        feature_id, lane_id = _stage_lane_gate_inputs(repo_root)
        feature_root = _feature_root(repo_root, feature_id)
        decision_path = feature_root / "lanes" / lane_id / LANE_DECISION_JSON
        assert not decision_path.is_file()
        before = _inventory(feature_root)

        plan = plan_lane_gate(repo_root, feature_id, lane_id)
        assert plan.details["decision"] in ("pass", "fail")
        assert not decision_path.is_file()
        assert _inventory(feature_root) == before

    def test_coherence_gate_no_write(self, repo_root: Path) -> None:
        from test_coherence_gate import _stage_coherence_inputs

        feature_id, _ = _stage_coherence_inputs(repo_root)
        feature_root = _feature_root(repo_root, feature_id)
        before = _inventory(feature_root)

        plan = plan_coherence_gate(repo_root, feature_id)
        assert plan.details["verdict"] in ("pass", "fail")
        assert _inventory(feature_root) == before

    def test_final_report_no_write(self, repo_root: Path) -> None:
        from ai_dev.coherence_gate import evaluate_coherence_gate
        from test_coherence_gate import _stage_coherence_inputs

        feature_id, _ = _stage_coherence_inputs(repo_root)
        # Coherence must have run (real) so a verdict exists for the report to
        # consume - the dry-run itself still writes nothing. (The staging
        # fixture happens to leave a placeholder final-report from earlier
        # pipeline steps; what matters is the dry-run does not change it.)
        evaluate_coherence_gate(repo_root, feature_id)
        feature_root = _feature_root(repo_root, feature_id)
        report_path = feature_root / FINAL_REPORT_JSON
        before = _inventory(feature_root)

        plan = plan_final_report(repo_root, feature_id)
        assert plan.details["verdict"] in ("pass", "fail")
        assert _inventory(feature_root) == before
        # The report content is unchanged byte-for-byte.
        if report_path.is_file():
            assert report_path.read_bytes() == before[str(report_path.relative_to(feature_root))]


class TestFixRunDryRun:
    """fix-run dry-run preflights targets/budget without running the chain."""

    def test_no_targets_exits_1(
        self,
        repo_root: Path,
        write_profiles,
        clean_token_env,
        monkeypatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # A profile + token must be present so the dry-run reaches the real
        # precondition (no request_fix targets) rather than failing at profile load.
        write_profiles(repo_root)
        monkeypatch.setenv("CC_GLM52_TOKEN", "test-token")
        feature_id, lane_id = _seed_frozen_feature(repo_root, tasks=["TASK-001"])
        rc = _run_cli(
            repo_root, ["fix-run", feature_id, lane_id, "--dry-run"]
        )
        assert rc == 1
        assert "request_fix" in capsys.readouterr().err

    def test_with_targets_no_budget_consumed(
        self,
        repo_root: Path,
        write_profiles,
        clean_token_env,
        monkeypatch,
    ) -> None:
        from test_fix_run import _stage_request_fix_issue

        write_profiles(repo_root)
        monkeypatch.setenv("CC_GLM52_TOKEN", "test-token")
        feature_id, lane_id = _seed_frozen_feature(repo_root, tasks=["TASK-001"])
        _stage_request_fix_issue(repo_root, feature_id)
        feature_root = _feature_root(repo_root, feature_id)
        before = _inventory(feature_root)
        before_counters = _counters(feature_root)
        profile = load_profile(repo_root, "cc-glm52")

        plan = plan_fix_run(repo_root, feature_id, lane_id, profile, profile, profile)
        assert plan.details["target_issue_ids"]
        assert plan.details["would_consume_budget"] is False
        assert _inventory(feature_root) == before
        assert _counters(feature_root) == before_counters

    def test_budget_exhausted_exits_1(
        self,
        repo_root: Path,
        write_profiles,
        clean_token_env,
        monkeypatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # ADR-0002 D5: once fix_loop_budget is exhausted the driver refuses
        # before planning anything (a real precondition, not a would_be_refused).
        write_profiles(repo_root)
        monkeypatch.setenv("CC_GLM52_TOKEN", "test-token")
        feature_id, lane_id = _seed_frozen_feature(repo_root, tasks=["TASK-001"])
        _exhaust_fix_loop_budget(_feature_root(repo_root, feature_id))

        rc = _run_cli(repo_root, ["fix-run", feature_id, lane_id, "--dry-run"])
        assert rc == 1
        assert "fix_loop_budget exhausted" in capsys.readouterr().err

    def test_missing_feature_raises(
        self,
        repo_root: Path,
        write_profiles,
        clean_token_env,
        monkeypatch,
    ) -> None:
        write_profiles(repo_root)
        monkeypatch.setenv("CC_GLM52_TOKEN", "test-token")
        profile = load_profile(repo_root, "cc-glm52")
        with pytest.raises(ValueError, match="FEATURE-404"):
            plan_fix_run(repo_root, "FEATURE-404", "LANE-001", profile, profile, profile)


# ---------------------------------------------------------------------------
# Rendering / CLI plumbing.
# ---------------------------------------------------------------------------


def test_render_plan_roundtrip() -> None:
    plan = DryRunPlan(
        command="freeze",
        feature_id="FEATURE-001",
        summary="FREEZE DRY-RUN - would freeze tasks",
        details={"artifact": "tasks", "would_mint_ids": []},
    )
    text = render_plan(plan)
    assert "FREEZE DRY-RUN" in text
    assert "- artifact: tasks" in text
    assert "- would_mint_ids: []" in text


def test_dry_run_flag_only_on_side_effect_commands() -> None:
    """Read-only commands do NOT accept --dry-run (noise), per ADR-0004."""
    from ai_dev.cli import _build_parser

    parser = _build_parser()
    choices = parser._subparsers._group_actions[0].choices  # type: ignore[attr-defined]
    show_actions = {a.dest for a in choices["show-profile"]._actions}
    assert "dry_run" not in show_actions
    impl_actions = {a.dest for a in choices["implement"]._actions}
    assert "dry_run" in impl_actions
