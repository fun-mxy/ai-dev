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
from ai_dev.issue_status import (
    STATUS_RAISED,
    STATUS_REAPPEARED,
    STATUS_RESOLVED,
    STATUS_TRIAGED,
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


def _stage_triaged_issue(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    *,
    action: str,
    reason: str,
    **extra: Any,
) -> Path:
    """Collect once (-> status=raised), then simulate apply_triage (ticket 05)
    having written a disposition: set ``status=triaged`` + ``triage`` on the
    lane's ISSUE-001.json. ``extra`` adds persisted fields later writers own
    (e.g. ``fix_targeted_in_run`` from ticket 07's driver). Returns the path."""
    collect_issue_bundle(repo_root, feature_id, lane_id)
    issue_path = _feature_root(repo_root, feature_id) / ISSUES_DIR / "ISSUE-001.json"
    issue = json.loads(issue_path.read_text())
    issue["status"] = STATUS_TRIAGED
    issue["triage"] = {"action": action, "reason": reason, "by": "human"}
    issue.update(extra)
    write_json(issue_path, issue)
    return issue_path


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
        # No writer has touched triage yet -> the issue carries no triage.
        assert "triage" not in issue
        # Ticket 03: the collector starts every new fingerprint at status=raised.
        assert issue["status"] == "raised"

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


class TestIssueStatusLifecycle:
    """ADR-0002 D2/D6: the collector drives the issue ``status`` state machine.

    A brand-new fingerprint starts ``raised``; a fingerprint gone from the new
    report is ``resolved``; a ``request_fix`` issue still present after a fix
    run targeted it is ``reappeared`` (and its triage is invalidated). These
    tests exercise the collector-owned transitions through the public
    ``collect_issue_bundle`` seam - the helper itself is unit-tested in
    ``test_issue_status.py``.
    """

    def _issue_path(self, repo_root: Path, feature_id: str) -> Path:
        return _feature_root(repo_root, feature_id) / ISSUES_DIR / "ISSUE-001.json"

    def test_new_fingerprint_starts_raised(self, repo_root: Path) -> None:
        feature_id, _ = _write_review_and_gap_reports(
            repo_root, review_issues=[_issue(id="agent-review-1")], gap_issues=[]
        )

        collect_issue_bundle(repo_root, feature_id, "LANE-001")

        issue = json.loads(self._issue_path(repo_root, feature_id).read_text())
        assert issue["status"] == STATUS_RAISED

    def test_re_collect_keeps_raised_issue_raised(self, repo_root: Path) -> None:
        review_issues = [_issue(id="agent-review-1")]
        feature_id, lane_id = _write_review_and_gap_reports(
            repo_root, review_issues=review_issues, gap_issues=[]
        )
        collect_issue_bundle(repo_root, feature_id, lane_id)
        # Still untriaged, still reported -> stays raised (idempotent self-loop).
        collect_issue_bundle(repo_root, feature_id, lane_id)

        issue = json.loads(self._issue_path(repo_root, feature_id).read_text())
        assert issue["status"] == STATUS_RAISED

    def test_disappeared_fingerprint_is_resolved(self, repo_root: Path) -> None:
        # D6: a fingerprint in the prior lane bundle but absent from the new
        # report -> that issues/ record becomes resolved.
        feature_id, lane_id = _write_review_and_gap_reports(
            repo_root, review_issues=[_issue(id="agent-review-1")], gap_issues=[]
        )
        collect_issue_bundle(repo_root, feature_id, lane_id)
        assert json.loads(self._issue_path(repo_root, feature_id).read_text())["status"] == (
            STATUS_RAISED
        )

        # Re-review drops the issue entirely (reviewer no longer reports it).
        _overwrite_review_report(repo_root, feature_id, lane_id, review_issues=[])
        result = collect_issue_bundle(repo_root, feature_id, lane_id)

        assert result.issue_ids == []  # nothing in the new report
        resolved = json.loads(self._issue_path(repo_root, feature_id).read_text())
        assert resolved["status"] == STATUS_RESOLVED
        # The resolved record stays on disk (lifecycle history) even though it
        # no longer projects into the current lane bundle.
        bundle = json.loads(
            (lane_dir(repo_root, feature_id, lane_id) / ISSUE_BUNDLE_JSON).read_text()
        )
        assert [i["id"] for i in bundle["issues"]] == []

    def test_resolved_preserves_triage_to_history(self, repo_root: Path) -> None:
        # D6: when a triaged issue disappears, "its triage is preserved into
        # triage_history" (and the active triage cleared, so a later re-raise
        # starts cleanly untriaged).
        feature_id, lane_id = _write_review_and_gap_reports(
            repo_root, review_issues=[_issue(id="agent-review-1")], gap_issues=[]
        )
        issue_path = _stage_triaged_issue(
            repo_root,
            feature_id,
            lane_id,
            action="reject",
            reason="false positive",
        )

        # Re-review drops the issue -> resolved diff fires.
        _overwrite_review_report(repo_root, feature_id, lane_id, review_issues=[])
        collect_issue_bundle(repo_root, feature_id, lane_id)

        resolved = json.loads(issue_path.read_text())
        assert resolved["status"] == STATUS_RESOLVED
        assert resolved["triage"] is None
        assert resolved["triage_history"] == [
            {"action": "reject", "reason": "false positive", "by": "human"}
        ]

    def test_resolved_fingerprint_reraises_when_reported_again(
        self, repo_root: Path
    ) -> None:
        feature_id, lane_id = _write_review_and_gap_reports(
            repo_root, review_issues=[_issue(id="agent-review-1")], gap_issues=[]
        )
        collect_issue_bundle(repo_root, feature_id, lane_id)  # raised
        _overwrite_review_report(repo_root, feature_id, lane_id, review_issues=[])
        collect_issue_bundle(repo_root, feature_id, lane_id)  # -> resolved
        assert (
            json.loads(self._issue_path(repo_root, feature_id).read_text())["status"]
            == STATUS_RESOLVED
        )

        # The fingerprint comes back in a later report -> re-raise (resolved is
        # terminal except for this edge). Same fingerprint reuses ISSUE-001.
        _overwrite_review_report(
            repo_root, feature_id, lane_id, review_issues=[_issue(id="agent-review-1")]
        )
        collect_issue_bundle(repo_root, feature_id, lane_id)

        issue = json.loads(self._issue_path(repo_root, feature_id).read_text())
        assert issue["id"] == "ISSUE-001"
        assert issue["status"] == STATUS_RAISED

    def test_request_fix_reappear_after_fix_run_invalidates_triage(
        self, repo_root: Path
    ) -> None:
        # triaged(request_fix) + fix-targeted + still present -> reappeared, and
        # the prior triage is wiped to triage_history so the gate sees None.
        feature_id, lane_id = _write_review_and_gap_reports(
            repo_root, review_issues=[_issue(id="agent-review-1")], gap_issues=[]
        )
        # Simulate apply_triage (ticket 05) + the fix-run driver (ticket 07):
        # the issue is triaged request_fix and a fix run targeted it.
        issue_path = _stage_triaged_issue(
            repo_root,
            feature_id,
            lane_id,
            action="request_fix",
            reason="Fix before freeze.",
            fix_targeted_in_run="RUN-009",
        )

        # Re-collect: same fingerprint still present after the fix run.
        collect_issue_bundle(repo_root, feature_id, lane_id)

        reappeared = json.loads(issue_path.read_text())
        assert reappeared["status"] == STATUS_REAPPEARED
        # Triage invalidated (wipe -> None) so the gate forces a re-triage.
        assert reappeared["triage"] is None
        assert reappeared["triage_history"] == [
            {
                "action": "request_fix",
                "reason": "Fix before freeze.",
                "by": "human",
            }
        ]
        # The bundle projects the reappeared state straight through (SoT).
        bundle = json.loads(
            (lane_dir(repo_root, feature_id, lane_id) / ISSUE_BUNDLE_JSON).read_text()
        )
        assert bundle["issues"][0]["status"] == STATUS_REAPPEARED
        assert bundle["issues"][0]["triage"] is None

    def test_non_request_fix_triaged_stays_triaged_when_still_present(
        self, repo_root: Path
    ) -> None:
        # triaged(non-rf) + still present + not fix-targeted -> triaged (no
        # lifecycle change); triage is preserved, not invalidated.
        feature_id, lane_id = _write_review_and_gap_reports(
            repo_root, review_issues=[_issue(id="agent-review-1")], gap_issues=[]
        )
        issue_path = _stage_triaged_issue(
            repo_root,
            feature_id,
            lane_id,
            action="override_issue",
            reason="Known limitation acceptable for MVP v0.",
        )

        collect_issue_bundle(repo_root, feature_id, lane_id)

        merged = json.loads(issue_path.read_text())
        assert merged["status"] == STATUS_TRIAGED
        assert merged["triage"] == {
            "action": "override_issue",
            "reason": "Known limitation acceptable for MVP v0.",
            "by": "human",
        }

    def test_request_fix_without_fix_targeting_stays_triaged(
        self, repo_root: Path
    ) -> None:
        # request_fix alone (no fix run yet) does not reappear: the reappear
        # trigger requires fix_targeted_in_run, so a pre-fix-loop re-collect
        # leaves the triaged request_fix issue triaged.
        feature_id, lane_id = _write_review_and_gap_reports(
            repo_root, review_issues=[_issue(id="agent-review-1")], gap_issues=[]
        )
        issue_path = _stage_triaged_issue(
            repo_root,
            feature_id,
            lane_id,
            action="request_fix",
            reason="Fix later.",
        )

        collect_issue_bundle(repo_root, feature_id, lane_id)

        merged = json.loads(issue_path.read_text())
        assert merged["status"] == STATUS_TRIAGED
        assert merged["triage"] == {
            "action": "request_fix",
            "reason": "Fix later.",
            "by": "human",
        }

    def test_reappeared_issue_that_disappears_is_resolved(self, repo_root: Path) -> None:
        # D6: any fingerprint absent from the new report is resolved, even one
        # currently reappeared (a second fix worked, or the reviewer stopped
        # reporting it). The state machine permits reappeared -> resolved.
        feature_id, lane_id = _write_review_and_gap_reports(
            repo_root, review_issues=[_issue(id="agent-review-1")], gap_issues=[]
        )
        collect_issue_bundle(repo_root, feature_id, lane_id)  # raised
        issue_path = self._issue_path(repo_root, feature_id)
        issue = json.loads(issue_path.read_text())
        issue["status"] = STATUS_REAPPEARED
        issue["triage"] = None
        issue["triage_history"] = [{"action": "request_fix", "reason": "Fix failed.", "by": "human"}]
        write_json(issue_path, issue)

        # Re-review drops the issue -> resolved diff fires.
        _overwrite_review_report(repo_root, feature_id, lane_id, review_issues=[])
        collect_issue_bundle(repo_root, feature_id, lane_id)

        resolved = json.loads(issue_path.read_text())
        assert resolved["status"] == STATUS_RESOLVED


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
