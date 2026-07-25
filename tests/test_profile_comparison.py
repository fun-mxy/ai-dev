"""``ai-dev compare-profiles`` projection (v0.5 ticket 06, ADR-0003-style).

The non-canonical side-by-side projection of two parallel feature-runs (same
intent, one profile each). These tests pin: the two-profile requirement, the
intent-sibling discovery + implementer-profile matching, the metric set
(per-leg ``elapsed_ms``, verifier, verdict + ``failure_class``, issue count by
severity, requirement coverage), ``meta.known_gaps``, recomputability, the
fail-loud contracts (missing sibling / missing final-report / null verdict),
and the CLI ``--json`` + ``--dry-run`` surface.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ai_dev.audit import append_audit_event
from ai_dev.cli import main
from ai_dev.coherence_gate import evaluate_coherence_gate
from ai_dev.final_report import generate_final_report
from ai_dev.profile_comparison import (
    PROFILE_COMPARISON_JSON,
    PROFILE_COMPARISON_MD,
    ProfileComparisonResult,
    compute_profile_comparison,
    generate_profile_comparison,
)
from ai_dev.status import record_agent_profile

from test_coherence_gate import _stage_coherence_inputs  # noqa: E402
from test_final_report import _seed_requirements  # noqa: E402
from test_implement_leg import _feature_root  # noqa: E402

_PROFILE_A = "cc-glm52"
_PROFILE_B = "codex-default"


def _stage_completed_feature(
    repo_root: Path,
    *,
    implementer: str,
    elapsed: tuple[int, int, int] = (5000, 3000, 2000),
    review_issues: list[dict[str, Any]] | None = None,
) -> str:
    """Stage a full happy-path feature-run (intent -> verdict -> final-report).

    Mirrors test_final_report's passing setup, then records the implementer
    profile on ``feature-status.yml`` (the compare-profiles record) and appends
    audit ``run`` events with per-leg ``elapsed_ms`` so the projection has real
    timing to attribute. Returns the feature id.
    """
    feature_id, _lane_id = _stage_coherence_inputs(repo_root, review_issues=review_issues)
    _seed_requirements(repo_root, feature_id)
    evaluate_coherence_gate(repo_root, feature_id)
    generate_final_report(repo_root, feature_id)

    feature_root = _feature_root(repo_root, feature_id)
    # Record the agent-profile config the orchestrator would write as legs run.
    record_agent_profile(feature_root, "implementer", implementer)
    record_agent_profile(feature_root, "reviewer", implementer)
    record_agent_profile(feature_root, "spec_gap_analyst", implementer)
    # Audit run events carry per-leg elapsed_ms (the timing source). RUN-001 is
    # the implement run, RUN-002 review, RUN-003 spec-gap (per the staging helper).
    for run_id, ms in zip(("RUN-001", "RUN-002", "RUN-003"), elapsed):
        append_audit_event(
            feature_root,
            event="run",
            payload={
                "run": run_id,
                "feature": feature_id,
                "profile": implementer,
                "exit_code": 0,
                "changed_files": [],
                "elapsed_ms": ms,
            },
        )
    return feature_id


def _stage_pair(repo_root: Path) -> tuple[str, str]:
    """Stage two identical-intent feature-runs, one per profile. Returns (id_a, id_b)."""
    id_a = _stage_completed_feature(repo_root, implementer=_PROFILE_A)
    id_b = _stage_completed_feature(repo_root, implementer=_PROFILE_B)
    return id_a, id_b


def _projection_json(repo_root: Path, anchor: str) -> dict[str, Any]:
    path = _feature_root(repo_root, anchor) / "projections" / PROFILE_COMPARISON_JSON
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Library seam: compute + generate.
# ---------------------------------------------------------------------------


class TestComputeProfileComparison:
    def test_projects_two_profiles_side_by_side(self, repo_root: Path) -> None:
        id_a, id_b = _stage_pair(repo_root)

        compute = compute_profile_comparison(
            repo_root, id_a, [_PROFILE_A, _PROFILE_B]
        )

        assert set(compute.report["profiles"].keys()) == {_PROFILE_A, _PROFILE_B}
        meta = compute.report["meta"]
        assert meta["anchor_feature"] == id_a
        assert meta["profiles_compared"] == [_PROFILE_A, _PROFILE_B]
        assert meta["canonical"] is False
        # The two compared feature-runs are the anchor (profile A) and its
        # intent-sibling (profile B); the anchor is one of them.
        assert set(meta["feature_ids"]) == {id_a, id_b}

    def test_metrics_carry_the_required_set(self, repo_root: Path) -> None:
        id_a, _ = _stage_pair(repo_root)

        report = compute_profile_comparison(
            repo_root, id_a, [_PROFILE_A, _PROFILE_B]
        ).report
        for name in (_PROFILE_A, _PROFILE_B):
            metrics = report["profiles"][name]
            assert metrics["verdict"] == "pass"
            assert "failure_class" in metrics
            assert set(metrics["elapsed_ms"]["by_leg"]) >= {
                "implement", "review", "spec_gap", "verify",
            }
            # RUN-001/002/003 audit elapsed (5s/3s/2s) attribute to the three legs.
            assert metrics["elapsed_ms"]["by_leg"]["implement"] == 5000
            assert metrics["elapsed_ms"]["by_leg"]["review"] == 3000
            assert metrics["elapsed_ms"]["by_leg"]["spec_gap"] == 2000
            assert "total" in metrics["elapsed_ms"]
            assert "by_severity" in metrics["issues"]
            assert "requirement_coverage" in metrics["coverage"]
            assert "acceptance_verification" in metrics["coverage"]
            # The full agent-profile config dict is surfaced (reviewer profile noted).
            assert metrics["agent_profiles"]["implementer"] == name

    def test_known_gaps_record_the_caveats(self, repo_root: Path) -> None:
        id_a, _ = _stage_pair(repo_root)
        meta = compute_profile_comparison(
            repo_root, id_a, [_PROFILE_A, _PROFILE_B]
        ).report["meta"]
        gaps = " ".join(meta["known_gaps"])
        assert "reviewer-variance" in gaps
        assert "planner-non-determinism" in gaps
        assert "self-attestation" in gaps

    def test_issue_severity_counts_from_final_report(self, repo_root: Path) -> None:
        issue = {
            "id": "ISSUE-001", "source": "code_review", "severity": "P2",
            "title": "t", "description": "d", "related_tasks": [],
            "related_requirements": [], "related_acceptance_criteria": [],
            "evidence": [], "recommendation": "r", "requires_change_proposal": False,
        }
        id_a = _stage_completed_feature(repo_root, implementer=_PROFILE_A, review_issues=[issue])
        _stage_completed_feature(repo_root, implementer=_PROFILE_B)

        report = compute_profile_comparison(
            repo_root, id_a, [_PROFILE_A, _PROFILE_B]
        ).report
        assert report["profiles"][_PROFILE_A]["issues"]["by_severity"].get("P2") == 1
        assert report["profiles"][_PROFILE_A]["issues"]["total"] == 1


class TestGenerateProfileComparison:
    def test_writes_both_projection_products(self, repo_root: Path) -> None:
        id_a, _ = _stage_pair(repo_root)

        result = generate_profile_comparison(repo_root, id_a, [_PROFILE_A, _PROFILE_B])

        assert isinstance(result, ProfileComparisonResult)
        projections = _feature_root(repo_root, id_a) / "projections"
        assert result.projection_json_path == projections / PROFILE_COMPARISON_JSON
        assert result.projection_md_path == projections / PROFILE_COMPARISON_MD
        assert result.projection_json_path.is_file()
        assert result.projection_md_path.is_file()
        # MD is a deterministic skeleton rendered from the JSON.
        md = result.projection_md_path.read_text()
        assert "# Profile Comparison" in md
        assert _PROFILE_A in md and _PROFILE_B in md

    def test_recomputable_byte_identical(self, repo_root: Path) -> None:
        id_a, _ = _stage_pair(repo_root)
        generate_profile_comparison(repo_root, id_a, [_PROFILE_A, _PROFILE_B])
        first = (
            _feature_root(repo_root, id_a) / "projections" / PROFILE_COMPARISON_JSON
        ).read_text()
        generate_profile_comparison(repo_root, id_a, [_PROFILE_A, _PROFILE_B])
        second = (
            _feature_root(repo_root, id_a) / "projections" / PROFILE_COMPARISON_JSON
        ).read_text()
        assert first == second


# ---------------------------------------------------------------------------
# Fail-loud contracts (§24.2).
# ---------------------------------------------------------------------------


class TestFailLoud:
    def test_anchor_feature_missing(self, repo_root: Path) -> None:
        _stage_pair(repo_root)
        with pytest.raises(ValueError):
            compute_profile_comparison(repo_root, "FEATURE-999", [_PROFILE_A, _PROFILE_B])

    def test_requested_profile_has_no_sibling(self, repo_root: Path) -> None:
        id_a, _ = _stage_pair(repo_root)
        with pytest.raises(ValueError):
            compute_profile_comparison(repo_root, id_a, [_PROFILE_A, "nonexistent-profile"])

    def test_requires_exactly_two_profiles(self, repo_root: Path) -> None:
        id_a, _ = _stage_pair(repo_root)
        with pytest.raises(ValueError):
            compute_profile_comparison(repo_root, id_a, [_PROFILE_A])
        with pytest.raises(ValueError):
            compute_profile_comparison(repo_root, id_a, [_PROFILE_A, _PROFILE_B, "third"])

    def test_two_profiles_must_differ(self, repo_root: Path) -> None:
        id_a, _ = _stage_pair(repo_root)
        with pytest.raises(ValueError):
            compute_profile_comparison(repo_root, id_a, [_PROFILE_A, _PROFILE_A])

    def test_null_verdict_final_report_fails_loud(self, repo_root: Path) -> None:
        # A feature whose final-report never reached a verdict (placeholder) is
        # not comparable material.
        id_a, _ = _stage_pair(repo_root)
        placeholder = {"feature": id_a}
        (_feature_root(repo_root, id_a) / "final-report.json").write_text(
            json.dumps(placeholder)
        )
        with pytest.raises(ValueError):
            compute_profile_comparison(repo_root, id_a, [_PROFILE_A, _PROFILE_B])


# ---------------------------------------------------------------------------
# Intent-sibling discovery.
# ---------------------------------------------------------------------------


class TestSiblingDiscovery:
    def test_unrelated_intent_feature_is_excluded(self, repo_root: Path) -> None:
        from ai_dev.feature_run import create_feature_run

        id_a, _ = _stage_pair(repo_root)
        # A third feature with a *different* intent must not be picked up.
        create_feature_run(repo_root, "a completely different intent")
        report = compute_profile_comparison(
            repo_root, id_a, [_PROFILE_A, _PROFILE_B]
        ).report
        assert report["meta"]["feature_ids"] == sorted(
            report["meta"]["feature_ids"]
        )  # sanity
        assert len(report["meta"]["feature_ids"]) == 2

    def test_implementer_falls_back_to_run_metadata(self, repo_root: Path) -> None:
        # A pre-v0.5 feature-run has no recorded agent_profiles; the projection
        # falls back to the implement run's metadata.profile to identify it.
        from ai_dev.json_artifact import write_json
        from ai_dev.paths import run_dir

        id_a, id_b = _stage_pair(repo_root)
        # Strip the recorded config from feature B so the fallback path is taken.
        from ai_dev.status import AGENT_PROFILES_KEY
        import yaml as _yaml
        status_path = _feature_root(repo_root, id_b) / "status" / "feature-status.yml"
        doc = _yaml.safe_load(status_path.read_text())
        del doc["feature"][AGENT_PROFILES_KEY]
        status_path.write_text(_yaml.safe_dump(doc, sort_keys=False))
        # Seed the implement run's metadata profile so the fallback resolves.
        impl_run = run_dir(repo_root, id_b, "RUN-001") / "output" / "metadata.json"
        write_json(impl_run, {**json.loads(impl_run.read_text()), "profile": _PROFILE_B})

        report = compute_profile_comparison(
            repo_root, id_a, [_PROFILE_A, _PROFILE_B]
        ).report
        assert set(report["profiles"]) == {_PROFILE_A, _PROFILE_B}
        # The fallback feature surfaces no recorded agent_profiles dict.
        assert report["profiles"][_PROFILE_B]["agent_profiles"] == {}


# ---------------------------------------------------------------------------
# CLI seam.
# ---------------------------------------------------------------------------


class TestCompareProfilesCli:
    def test_writes_projection_and_human_summary(self, repo_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
        id_a, _ = _stage_pair(repo_root)

        rc = main(["compare-profiles", id_a, "--profiles", f"{_PROFILE_A},{_PROFILE_B}", "--repo-root", str(repo_root)])

        assert rc == 0
        out = capsys.readouterr().out
        assert "COMPARE-PROFILES" in out or "compare-profiles" in out.lower()
        assert _feature_root(repo_root, id_a).joinpath(
            "projections", PROFILE_COMPARISON_JSON
        ).is_file()

    def test_json_emits_full_report(self, repo_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
        id_a, _ = _stage_pair(repo_root)

        rc = main([
            "compare-profiles", id_a,
            "--profiles", f"{_PROFILE_A},{_PROFILE_B}",
            "--json",
            "--repo-root", str(repo_root),
        ])

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert set(payload["profiles"]) == {_PROFILE_A, _PROFILE_B}
        assert payload["meta"]["anchor_feature"] == id_a

    def test_dry_run_does_not_write_and_plans(self, repo_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
        id_a, _ = _stage_pair(repo_root)

        rc = main([
            "compare-profiles", id_a,
            "--profiles", f"{_PROFILE_A},{_PROFILE_B}",
            "--dry-run",
            "--repo-root", str(repo_root),
        ])

        assert rc == 0
        out = capsys.readouterr().out
        assert "profile-comparison.json" in out
        assert not _feature_root(repo_root, id_a).joinpath(
            "projections", PROFILE_COMPARISON_JSON
        ).is_file()

    def test_missing_sibling_exits_one(self, repo_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
        id_a, _ = _stage_pair(repo_root)
        rc = main([
            "compare-profiles", id_a,
            "--profiles", f"{_PROFILE_A},nonexistent-profile",
            "--repo-root", str(repo_root),
        ])
        assert rc == 1
        assert "error" in capsys.readouterr().err.lower()
