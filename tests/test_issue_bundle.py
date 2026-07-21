"""Issue normalization + lane bundle (v0.2 ticket 04).

The public seams are the collector library function and the CLI command. The
collector reads the lane's reviewer + spec-gap reports, normalizes their §15
issues onto feature-stable ``ISSUE-NNN`` ids, de-duplicates, and writes the
feature-level issue artifacts plus the lane-level issue bundle double product.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ai_dev.checking_legs import (
    REVIEW_DIR,
    REVIEW_REPORT_JSON,
    SPEC_GAP_DIR,
    SPEC_GAP_REPORT_JSON,
    write_review_report,
    write_spec_gap_report,
)
from ai_dev.cli import main
from ai_dev.issue_bundle import (
    ISSUE_BUNDLE_JSON,
    ISSUE_BUNDLE_MD,
    ISSUES_DIR,
    collect_issue_bundle,
)
from ai_dev.json_artifact import write_json
from ai_dev.paths import lane_dir
from ai_dev.validate import ValidationResult

from test_checking_legs import (  # noqa: E402
    _ISSUES_PAYLOAD,
    _REVIEW_RUN_METADATA,
    _stage_implement_run,
)
from test_implement_leg import _feature_root  # noqa: E402


def _issue(**overrides: Any) -> dict[str, Any]:
    base = json.loads(json.dumps(_ISSUES_PAYLOAD["issues"][0]))
    base.update(overrides)
    return base


def _write_review_and_gap_reports(
    repo_root: Path,
    *,
    review_issues: list[dict[str, Any]],
    gap_issues: list[dict[str, Any]],
) -> tuple[str, str]:
    feature_id, lane_id, _ = _stage_implement_run(repo_root)
    feature_root = _feature_root(repo_root, feature_id)
    write_review_report(
        feature_root,
        lane_id,
        run_id="RUN-002",
        result={"issues": review_issues},
        metadata=_REVIEW_RUN_METADATA,
        validation=ValidationResult("RUN-002", []),
    )
    write_spec_gap_report(
        feature_root,
        lane_id,
        run_id="RUN-003",
        result={"issues": gap_issues},
        metadata={**_REVIEW_RUN_METADATA, "run_id": "RUN-003"},
        validation=ValidationResult("RUN-003", []),
    )
    return feature_id, lane_id


def _overwrite_review_report(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    review_issues: list[dict[str, Any]],
) -> None:
    """Re-write the lane's review-report in place for a re-collect scenario.

    Reuses the same lane the initial ``_write_review_and_gap_reports`` staged so
    the re-collect sees an updated reviewer report against the same feature/lane
    (a fresh ``_write_review_and_gap_reports`` would stage a brand-new feature).
    """
    feature_root = _feature_root(repo_root, feature_id)
    write_review_report(
        feature_root,
        lane_id,
        run_id="RUN-002",
        result={"issues": review_issues},
        metadata=_REVIEW_RUN_METADATA,
        validation=ValidationResult("RUN-002", []),
    )


class TestCollectIssueBundle:
    """Library seam: collect reviewer + spec-gap issues into stable artifacts."""

    def test_deduplicates_by_source_title_evidence_and_preserves_severity(
        self, repo_root: Path
    ) -> None:
        same_evidence = [{"file": "workspace/hello.py", "line": 2}]
        review_issues = [
            _issue(
                id="agent-review-1",
                source="code_review",
                severity="P2",
                title="answer() has no docstring",
                evidence=same_evidence,
                description="First phrasing from the reviewer.",
            ),
            _issue(
                id="agent-review-duplicate",
                source="code_review",
                severity="P2",
                title="answer() has no docstring",
                evidence=same_evidence,
                description="Duplicate phrasing should be merged away.",
            ),
        ]
        gap_issues = [
            _issue(
                id="agent-gap-1",
                source="spec_gap",
                severity="P1",
                title="answer() has no docstring",
                evidence=same_evidence,
                description="Same title/evidence but different source is distinct.",
            )
        ]
        feature_id, lane_id = _write_review_and_gap_reports(
            repo_root, review_issues=review_issues, gap_issues=gap_issues
        )

        result = collect_issue_bundle(repo_root, feature_id, lane_id)

        assert result.issue_ids == ["ISSUE-001", "ISSUE-002"]
        issue_root = _feature_root(repo_root, feature_id) / ISSUES_DIR
        first = json.loads((issue_root / "ISSUE-001.json").read_text())
        second = json.loads((issue_root / "ISSUE-002.json").read_text())
        assert first["id"] == "ISSUE-001"
        assert first["source"] == "code_review"
        assert first["severity"] == "P2"  # preserved, not re-judged
        assert first["description"] == "First phrasing from the reviewer."
        assert second["id"] == "ISSUE-002"
        assert second["source"] == "spec_gap"
        assert second["severity"] == "P1"  # preserved, not re-judged

        bundle = json.loads(
            (lane_dir(repo_root, feature_id, lane_id) / ISSUE_BUNDLE_JSON).read_text()
        )
        assert bundle["feature"] == feature_id
        assert bundle["lane"] == lane_id
        assert bundle["issue_count"] == 2
        assert [issue["id"] for issue in bundle["issues"]] == ["ISSUE-001", "ISSUE-002"]

    def test_consecutive_collect_runs_reuse_existing_issue_ids(
        self, repo_root: Path
    ) -> None:
        review_issues = [
            _issue(
                id="agent-review-7",
                source="code_review",
                severity="P3",
                title="Use a clearer helper name",
                evidence=[{"file": "workspace/hello.py", "line": 1}],
            )
        ]
        feature_id, lane_id = _write_review_and_gap_reports(
            repo_root, review_issues=review_issues, gap_issues=[]
        )

        first = collect_issue_bundle(repo_root, feature_id, lane_id)
        second = collect_issue_bundle(repo_root, feature_id, lane_id)

        assert first.issue_ids == ["ISSUE-001"]
        assert second.issue_ids == ["ISSUE-001"]
        assert first.issue_count == second.issue_count == 1
        counters = (_feature_root(repo_root, feature_id) / "id-counters.yml").read_text()
        assert "ISSUE: 1" in counters

    def test_writes_markdown_and_json_double_products_and_ignores_verifier(
        self, repo_root: Path
    ) -> None:
        review_issues = [
            _issue(
                id="agent-review-1",
                source="code_review",
                severity="P2",
                title="Review-only issue",
                evidence=[{"file": "workspace/hello.py", "line": 2}],
            )
        ]
        feature_id, lane_id = _write_review_and_gap_reports(
            repo_root, review_issues=review_issues, gap_issues=[]
        )
        verify_dir = lane_dir(repo_root, feature_id, lane_id) / "verification"
        verify_dir.mkdir(parents=True)
        (verify_dir / "verification-report.json").write_text(
            json.dumps(
                {
                    "verdict": "fail",
                    "issues": [
                        _issue(
                            id="verifier-must-not-enter-bundle",
                            source="code_review",
                            severity="P0",
                            title="Verifier failure",
                            evidence=[{"file": "workspace/hello.py"}],
                        )
                    ],
                }
            )
        )

        collect_issue_bundle(repo_root, feature_id, lane_id)

        issue_root = _feature_root(repo_root, feature_id) / ISSUES_DIR
        assert (issue_root / "ISSUE-001.json").is_file()
        assert (issue_root / "ISSUE-001.md").is_file()
        lane_root = lane_dir(repo_root, feature_id, lane_id)
        assert (lane_root / ISSUE_BUNDLE_JSON).is_file()
        assert (lane_root / ISSUE_BUNDLE_MD).is_file()
        bundle = json.loads((lane_root / ISSUE_BUNDLE_JSON).read_text())
        assert [issue["title"] for issue in bundle["issues"]] == ["Review-only issue"]
        assert "Review-only issue" in (lane_root / ISSUE_BUNDLE_MD).read_text()
        assert "Verifier failure" not in (lane_root / ISSUE_BUNDLE_MD).read_text()

    def test_collect_raises_when_prerequisite_reports_missing(
        self, repo_root: Path
    ) -> None:
        # §24.2 fail-loud: a collector with no reviewer/gap reports to read must
        # not silently yield an empty bundle; it raises before writing anything.
        feature_id, lane_id, _ = _stage_implement_run(repo_root)

        with pytest.raises(ValueError):
            collect_issue_bundle(repo_root, feature_id, lane_id)


class TestBundleMergeIsProjection:
    """ADR-0002 D1: ``issues/ISSUE-NNN.json`` is the source of truth and the
    lane ``issue-bundle.json`` is a projection of it. Re-collect must MERGE -
    preserving persisted state other writers own (``triage`` / ``status`` /
    run-tracking) while refreshing report-derived fields - not overwrite.

    These tests do not introduce the ``triage`` / ``status`` fields themselves
    (those land in tickets 05 / 03); they write arbitrary persisted state onto
    ``ISSUE-NNN.json`` between collects to prove the merge preserves it.
    """

    def test_first_collect_has_no_persisted_state(self, repo_root: Path) -> None:
        review_issues = [_issue(id="agent-review-1")]
        feature_id, lane_id = _write_review_and_gap_reports(
            repo_root, review_issues=review_issues, gap_issues=[]
        )

        collect_issue_bundle(repo_root, feature_id, lane_id)

        issue = json.loads(
            (_feature_root(repo_root, feature_id) / ISSUES_DIR / "ISSUE-001.json").read_text()
        )
        assert issue["id"] == "ISSUE-001"
        # No writer has touched persisted state yet -> the issue carries only
        # report-derived fields + the stable id.
        assert "triage" not in issue
        assert "status" not in issue

    def test_re_collect_preserves_persisted_state_and_refreshes_report_fields(
        self, repo_root: Path
    ) -> None:
        # Same fingerprint across both reports (source/title/evidence are the
        # ``_issue`` defaults) so the id is reused and the merge path is exercised.
        review_v1 = [
            _issue(
                id="agent-review-1",
                description="v1 description",
                recommendation="v1 recommendation",
            )
        ]
        feature_id, lane_id = _write_review_and_gap_reports(
            repo_root, review_issues=review_v1, gap_issues=[]
        )
        collect_issue_bundle(repo_root, feature_id, lane_id)

        # Simulate the writers that land in later tickets: apply_triage (05)
        # writes `triage`, the status helper (03) writes `status`, the
        # collector/fix-run write run-tracking -- all onto the SoT issue file.
        issue_path = _feature_root(repo_root, feature_id) / ISSUES_DIR / "ISSUE-001.json"
        issue = json.loads(issue_path.read_text())
        issue["triage"] = {
            "action": "override_issue",
            "reason": "Known limitation acceptable for MVP v0.",
            "by": "human",
        }
        issue["status"] = "triaged"
        issue["first_seen_in_run"] = "RUN-001"
        write_json(issue_path, issue)

        # Re-review: same fingerprint, refreshed severity/description/recommendation.
        review_v2 = [
            _issue(
                id="agent-review-1",
                severity="P1",
                description="v2 description - sharper",
                recommendation="v2 recommendation",
            )
        ]
        _overwrite_review_report(repo_root, feature_id, lane_id, review_v2)

        result = collect_issue_bundle(repo_root, feature_id, lane_id)

        merged = json.loads(issue_path.read_text())
        # Fingerprint match -> id reused, not re-allocated.
        assert result.issue_ids == ["ISSUE-001"]
        assert merged["id"] == "ISSUE-001"
        # Persisted state survives the re-collect (the bridge-bug fix).
        assert merged["triage"] == {
            "action": "override_issue",
            "reason": "Known limitation acceptable for MVP v0.",
            "by": "human",
        }
        assert merged["status"] == "triaged"
        assert merged["first_seen_in_run"] == "RUN-001"
        # Report-derived fields are refreshed from the new report.
        assert merged["severity"] == "P1"
        assert merged["description"] == "v2 description - sharper"
        assert merged["recommendation"] == "v2 recommendation"
        # The id counter did not advance (no new allocation).
        counters = (_feature_root(repo_root, feature_id) / "id-counters.yml").read_text()
        assert "ISSUE: 1" in counters

    def test_bundle_is_projection_of_issues_dir(self, repo_root: Path) -> None:
        review_issues = [_issue(id="agent-review-1", severity="P1")]
        feature_id, lane_id = _write_review_and_gap_reports(
            repo_root, review_issues=review_issues, gap_issues=[]
        )
        collect_issue_bundle(repo_root, feature_id, lane_id)

        # Persisted state written to the SoT must project into the bundle.
        issue_path = _feature_root(repo_root, feature_id) / ISSUES_DIR / "ISSUE-001.json"
        issue = json.loads(issue_path.read_text())
        issue["triage"] = {"action": "reject", "reason": "false positive", "by": "human"}
        write_json(issue_path, issue)

        collect_issue_bundle(repo_root, feature_id, lane_id)

        bundle = json.loads(
            (lane_dir(repo_root, feature_id, lane_id) / ISSUE_BUNDLE_JSON).read_text()
        )
        persisted_issue = json.loads(issue_path.read_text())
        # The bundle entry IS the merged issues/ state - never a divergent copy.
        assert bundle["issues"][0] == persisted_issue
        assert bundle["issues"][0]["triage"] == {
            "action": "reject",
            "reason": "false positive",
            "by": "human",
        }


class TestCollectIssuesCli:
    """CLI seam: ``ai-dev collect-issues <FEATURE> <LANE>``."""

    def test_collect_issues_command_writes_bundle_and_prints_summary(
        self,
        repo_root: Path,
        capsys: Any,
    ) -> None:
        feature_id, lane_id = _write_review_and_gap_reports(
            repo_root,
            review_issues=[
                _issue(
                    id="agent-review-1",
                    source="code_review",
                    severity="P2",
                    title="CLI-visible issue",
                    evidence=[{"file": "workspace/hello.py", "line": 2}],
                )
            ],
            gap_issues=[],
        )

        rc = main(["collect-issues", feature_id, lane_id, "--repo-root", str(repo_root)])

        assert rc == 0
        out = capsys.readouterr().out
        assert "COLLECT-ISSUES PASS" in out
        assert f"lane={lane_id}" in out
        assert "issues=1" in out
        assert (lane_dir(repo_root, feature_id, lane_id) / ISSUE_BUNDLE_JSON).is_file()

    def test_collect_issues_missing_reports_exits_one(
        self,
        repo_root: Path,
        capsys: Any,
    ) -> None:
        feature_id, lane_id, _ = _stage_implement_run(repo_root)

        rc = main(["collect-issues", feature_id, lane_id, "--repo-root", str(repo_root)])

        assert rc == 1
        err = capsys.readouterr().err
        assert REVIEW_REPORT_JSON in err
        assert SPEC_GAP_REPORT_JSON in err
        assert REVIEW_DIR in err
        assert SPEC_GAP_DIR in err
