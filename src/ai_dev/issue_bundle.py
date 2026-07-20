"""Issue normalization + bundle writer (v0.2 ticket 04, spec §15/§26.3).

The Code Reviewer and Spec Gap Analyst both emit lane-local ``issues[]`` in
their reports. This module is the deterministic collector between those agent
reports and the later lane gate: it reads only reviewer + gap reports, assigns
feature-stable ``ISSUE-NNN`` ids via the shared allocator, de-duplicates exact
``source``/``title``/``evidence`` matches, preserves the producer's severity, and
writes the §4.4 double products:

* ``issues/ISSUE-NNN.{json,md}`` at feature level;
* ``lanes/LANE-NNN/issue-bundle.{json,md}`` at lane level.

Verifier pass/fail is intentionally outside this bundle; the lane gate consumes
the verifier report directly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ai_dev.audit import append_audit_event
from ai_dev.checking_legs import (
    REVIEW_DIR,
    REVIEW_REPORT_JSON,
    SPEC_GAP_DIR,
    SPEC_GAP_REPORT_JSON,
)
from ai_dev.feature_ids import allocate_id
from ai_dev.json_artifact import read_json_object, write_json
from ai_dev.paths import feature_dir, lane_dir

ISSUES_DIR = "issues"
ISSUE_BUNDLE_MD = "issue-bundle.md"
ISSUE_BUNDLE_JSON = "issue-bundle.json"

_COLLECT_EVENT = "collect_issues"


@dataclass(frozen=True)
class IssueBundleResult:
    """Summary of one deterministic issue collection."""

    feature_id: str
    lane_id: str
    issue_ids: list[str]
    issue_count: int
    bundle_md_path: Path
    bundle_json_path: Path



def _require_report(path: Path) -> dict[str, Any]:
    report = read_json_object(path)
    if report is None:
        raise ValueError(f"required checking report missing or invalid: {path}")
    return report


def _canonical_json(value: Any) -> str:
    """Stable JSON text for matching an issue fingerprint across restarts."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _fingerprint(issue: Mapping[str, Any]) -> str:
    """The ticket's de-duplication key: same source/title/evidence."""
    return _canonical_json(
        {
            "source": issue.get("source"),
            "title": issue.get("title"),
            "evidence": issue.get("evidence", []),
        }
    )


def _extract_report_issues(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = report.get("issues")
    if not isinstance(raw, list):
        return []
    return [dict(issue) for issue in raw if isinstance(issue, dict)]


def _existing_issue_ids_by_fingerprint(feature_root: Path, lane_root: Path) -> dict[str, str]:
    """Map already-normalized issue fingerprints to stable ids.

    Re-reading previous feature issue artifacts makes consecutive collector runs
    restart-safe: a repeated normalized issue reuses its original ``ISSUE-NNN``
    instead of calling the allocator again. The current lane bundle is also read
    as a fallback in case the per-issue file has not been created yet but a prior
    bundle exists.
    """
    found: dict[str, str] = {}
    issue_root = feature_root / ISSUES_DIR
    if issue_root.is_dir():
        for path in sorted(issue_root.glob("ISSUE-*.json")):
            issue = read_json_object(path)
            if issue is None:
                continue
            issue_id = issue.get("id")
            if isinstance(issue_id, str) and issue_id:
                found.setdefault(_fingerprint(issue), issue_id)

    bundle = read_json_object(lane_root / ISSUE_BUNDLE_JSON)
    if bundle is not None:
        for issue in _extract_report_issues(bundle):
            issue_id = issue.get("id")
            if isinstance(issue_id, str) and issue_id:
                found.setdefault(_fingerprint(issue), issue_id)
    return found


def _normalize_issues(
    feature_root: Path, lane_root: Path, raw_issues: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    existing_ids = _existing_issue_ids_by_fingerprint(feature_root, lane_root)
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []

    for issue in raw_issues:
        key = _fingerprint(issue)
        if key in seen:
            continue
        seen.add(key)
        issue_id = existing_ids.get(key)
        if issue_id is None:
            issue_id = allocate_id(feature_root, "ISSUE")
            existing_ids[key] = issue_id
        normalized_issue = dict(issue)
        # Agent-local ids are not stable. The collector is the authoritative place
        # where §5.2 feature-stable ISSUE ids are assigned.
        normalized_issue["id"] = issue_id
        normalized.append(normalized_issue)
    return normalized


def _issue_md(issue: Mapping[str, Any]) -> str:
    lines = [
        f"# {issue.get('id', 'ISSUE-???')} - {issue.get('title', '')}",
        "",
        f"- source: {issue.get('source', '')}",
        f"- severity: {issue.get('severity', '')}",
        f"- requires_change_proposal: {issue.get('requires_change_proposal', False)}",
        "",
        "## Description",
        "",
        str(issue.get("description", "")),
        "",
        "## Evidence",
        "",
    ]
    evidence = issue.get("evidence")
    if isinstance(evidence, list) and evidence:
        for entry in evidence:
            if isinstance(entry, Mapping):
                file = entry.get("file", "")
                line = entry.get("line")
                suffix = f":{line}" if line is not None else ""
                lines.append(f"- {file}{suffix}")
            else:
                lines.append(f"- {entry}")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Related",
            "",
            f"- tasks: {issue.get('related_tasks', [])}",
            f"- requirements: {issue.get('related_requirements', [])}",
            f"- acceptance_criteria: {issue.get('related_acceptance_criteria', [])}",
            "",
            "## Recommendation",
            "",
            str(issue.get("recommendation", "")),
            "",
        ]
    )
    return "\n".join(lines)


def _bundle_json(feature_id: str, lane_id: str, issues: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "feature": feature_id,
        "lane": lane_id,
        "issue_count": len(issues),
        "issues": issues,
    }


def _bundle_md(bundle: Mapping[str, Any]) -> str:
    issues = bundle.get("issues") if isinstance(bundle.get("issues"), list) else []
    lines = [
        f"# Issue Bundle - {bundle.get('lane', '')}",
        "",
        f"- feature: {bundle.get('feature', '')}",
        f"- lane: {bundle.get('lane', '')}",
        f"- issue_count: {bundle.get('issue_count', 0)}",
        "",
        "## Issues",
        "",
    ]
    if not issues:
        lines.append("No reviewer/spec-gap issues collected.")
    else:
        for issue in issues:
            if not isinstance(issue, Mapping):
                continue
            lines.append(
                f"- [{issue.get('severity', '')}] {issue.get('id', '')}: "
                f"{issue.get('title', '')} ({issue.get('source', '')})"
            )
    lines.append("")
    return "\n".join(lines)



def collect_issue_bundle(repo_root: Path, feature_id: str, lane_id: str) -> IssueBundleResult:
    """Collect reviewer + spec-gap report issues into stable issue artifacts.

    Reads only ``review/review-report.json`` and
    ``spec-gap/spec-gap-report.json``. The shell verifier's report is deliberately
    ignored: its pass/fail verdict belongs to the later lane gate, not to the §15
    issue contract.
    """
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id} not found under {repo_root}")
    lane_root = lane_dir(repo_root, feature_id, lane_id)
    if not lane_root.is_dir():
        raise ValueError(f"lane {lane_id} not found under feature {feature_id}")

    review_report_path = lane_root / REVIEW_DIR / REVIEW_REPORT_JSON
    gap_report_path = lane_root / SPEC_GAP_DIR / SPEC_GAP_REPORT_JSON
    missing = [str(p) for p in (review_report_path, gap_report_path) if not p.is_file()]
    if missing:
        raise ValueError(
            "required checking reports missing before issue collection: "
            + ", ".join(missing)
        )

    review_report = _require_report(review_report_path)
    gap_report = _require_report(gap_report_path)
    raw_issues = _extract_report_issues(review_report) + _extract_report_issues(gap_report)
    issues = _normalize_issues(feature_root, lane_root, raw_issues)

    issue_root = feature_root / ISSUES_DIR
    for issue in issues:
        issue_id = str(issue["id"])
        write_json(issue_root / f"{issue_id}.json", issue)
        (issue_root / f"{issue_id}.md").write_text(_issue_md(issue))

    bundle = _bundle_json(feature_id, lane_id, issues)
    bundle_json_path = lane_root / ISSUE_BUNDLE_JSON
    bundle_md_path = lane_root / ISSUE_BUNDLE_MD
    write_json(bundle_json_path, bundle)
    bundle_md_path.write_text(_bundle_md(bundle))

    issue_ids = [str(issue["id"]) for issue in issues]
    append_audit_event(
        feature_root,
        _COLLECT_EVENT,
        payload={
            "feature": feature_id,
            "lane": lane_id,
            "issue_count": len(issue_ids),
            "issue_ids": issue_ids,
        },
    )
    return IssueBundleResult(
        feature_id=feature_id,
        lane_id=lane_id,
        issue_ids=issue_ids,
        issue_count=len(issue_ids),
        bundle_md_path=bundle_md_path,
        bundle_json_path=bundle_json_path,
    )
