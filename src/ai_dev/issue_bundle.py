"""Issue normalization + bundle writer (v0.2 ticket 04, spec §15/§26.3).

The Code Reviewer and Spec Gap Analyst both emit lane-local ``issues[]`` in
their reports. This module is the deterministic collector between those agent
reports and the later lane gate: it reads only reviewer + gap reports, assigns
feature-stable ``ISSUE-NNN`` ids via the shared allocator, de-duplicates exact
``source``/``title``/``evidence`` matches, preserves the producer's severity, and
writes the §4.4 double products:

* ``issues/ISSUE-NNN.{json,md}`` at feature level;
* ``lanes/LANE-NNN/issue-bundle.{json,md}`` at lane level.

ADR-0002 D1 makes ``issues/ISSUE-NNN.json`` the single source of truth for
persisted issue state and the lane ``issue-bundle.json`` a *projection* of it.
A re-collect therefore MERGES rather than overwrites: report-derived fields
(§15) are refreshed from the new reviewer/gap report, while persisted state
that other writers own -- ``triage`` (ADR-0001), ``triage_history`` /
run-tracking (ADR-0002) -- is preserved across the re-collect. The bundle
never carries state that is not also in ``issues/``.

ADR-0002 D2/D6 makes this collector the writer of the issue lifecycle
``status`` (``raised | triaged | resolved | reappeared``): a brand-new
fingerprint starts ``raised``; a fingerprint present in the prior lane bundle
but absent from the new report is ``resolved``; a ``request_fix`` issue still
present after a fix run targeted it is ``reappeared`` (and its triage is
invalidated). Transitions go through ``ai_dev.issue_status`` so an illegal
jump fails loud. The lane gate does not read ``status`` (it reads ``severity``
+ ``triage``); ``status`` is collector/driver bookkeeping.

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
from ai_dev.issue_status import (
    STATUS_RAISED,
    STATUS_REAPPEARED,
    STATUS_RESOLVED,
    STATUS_TRIAGED,
    initial_issue_status,
    transition_issue_status,
)
from ai_dev.json_artifact import read_json_object, write_json
from ai_dev.paths import feature_dir, lane_dir

ISSUES_DIR = "issues"
ISSUE_BUNDLE_MD = "issue-bundle.md"
ISSUE_BUNDLE_JSON = "issue-bundle.json"

_COLLECT_EVENT = "collect_issues"

# The disposition that arms the fix loop (ADR-0001 #4). The collector reads it
# to detect the ``triaged -> reappeared`` trigger; apply_triage (ticket 05)
# writes it.
_REQUEST_FIX = "request_fix"

# Fields the reviewer/spec-gap report is authoritative for (the §15 Issue
# Contract). Everything else on ``issues/ISSUE-NNN.json`` is persisted state
# owned by other writers - ``triage`` (ADR-0001), ``status`` / ``triage_history``
# / run-tracking (ADR-0002) - and must survive a re-collect. Kept as a closed
# set so the merge stays forward-compatible with persisted fields later tickets
# add: any field not listed here is treated as persisted state and preserved.
# ``id`` is handled separately (the collector reasserts the stable ISSUE-NNN).
REPORT_DERIVED_FIELDS: tuple[str, ...] = (
    "source",
    "severity",
    "title",
    "description",
    "evidence",
    "recommendation",
    "requires_change_proposal",
    "related_tasks",
    "related_requirements",
    "related_acceptance_criteria",
)


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


def _existing_issues_by_fingerprint(
    feature_root: Path,
) -> dict[str, dict[str, Any]]:
    """Map persisted issue fingerprints to their ``issues/ISSUE-NNN.json`` state.

    ``issues/`` is the single source of truth for persisted issue state
    (ADR-0002 D1), so a re-collect reads prior state from there - not from the
    lane bundle, which is now a *projection* of ``issues/`` and would make
    fingerprint matching circular. Returns the full issue dict (carrying the
    stable ``id`` plus any persisted state) so the merge can preserve the
    latter while refreshing report-derived fields. A re-reported issue with a
    matching fingerprint reuses its original ``ISSUE-NNN`` instead of calling
    the allocator again, keeping collection restart-safe.
    """
    found: dict[str, dict[str, Any]] = {}
    issue_root = feature_root / ISSUES_DIR
    if issue_root.is_dir():
        for path in sorted(issue_root.glob("ISSUE-*.json")):
            issue = read_json_object(path)
            if issue is None:
                continue
            issue_id = issue.get("id")
            if isinstance(issue_id, str) and issue_id:
                found.setdefault(_fingerprint(issue), issue)
    return found


def _merge_issue(
    existing: Mapping[str, Any] | None,
    report_issue: Mapping[str, Any],
    issue_id: str,
) -> dict[str, Any]:
    """Merge a fresh report issue onto persisted issue state (ADR-0002 D1).

    ``issues/ISSUE-NNN.json`` is the source of truth: persisted state that
    other writers own - ``triage`` (ADR-0001), ``status`` / ``triage_history``
    / run-tracking (ADR-0002) - survives a re-collect. Only the
    report-derived fields (§15) are refreshed from the new reviewer/gap report;
    the stable ``ISSUE-NNN`` id is (re)asserted by the collector. The result is
    what gets written back to ``issues/`` and projected into the lane bundle,
    so the bundle never carries state that is not also in ``issues/``.
    """
    merged: dict[str, Any] = {}
    if existing is not None:
        # Persisted state: every field the report is NOT authoritative for.
        # ``id`` is skipped here and reasserted below.
        for key, value in existing.items():
            if key in REPORT_DERIVED_FIELDS or key == "id":
                continue
            merged[key] = value
    # Report-derived fields are refreshed from the current report every collect.
    for field in REPORT_DERIVED_FIELDS:
        if field in report_issue:
            merged[field] = report_issue[field]
    merged["id"] = issue_id
    # Drive the lifecycle ``status`` through the state machine (ADR-0002 D2) so
    # the collector-owned transitions (raised / reappeared / resolved) fail loud
    # on an illegal jump instead of silently corrupting the lifecycle.
    _apply_lifecycle_status(merged, existing)
    return merged


def _triage_action(issue: Mapping[str, Any]) -> str | None:
    """The issue's current triage disposition (``triage.action``), or ``None``.

    ``None`` covers both "no triage written yet" (ticket 05 not landed) and
    "triage wiped on reappear" (``triage`` set to null) - both surface as
    "untriaged" to the gate.
    """
    triage = issue.get("triage")
    if isinstance(triage, Mapping):
        action = triage.get("action")
        return action if isinstance(action, str) else None
    return None


def _is_fix_targeted(issue: Mapping[str, Any]) -> bool:
    """True iff a fix run targeted this issue (``fix_targeted_in_run`` set).

    Written by the fix-run driver (ticket 07); absent until then, so the
    ``triaged -> reappeared`` trigger never fires before the fix loop exists.
    """
    return bool(issue.get("fix_targeted_in_run"))


def _recollect_target_status(prior: Mapping[str, Any]) -> str:
    """The collector's re-collect policy for a fingerprint-matched issue: which
    lifecycle transition to fire based on the prior ``issues/`` state.

    ADR-0002 D2 (fingerprint still present in the new report):

    * ``raised`` -> ``raised``  (still untriaged; re-reported)
    * ``triaged`` + ``request_fix`` + fix-targeted -> ``reappeared``  (fix failed)
    * ``triaged`` (otherwise) -> ``triaged``  (no lifecycle change)
    * ``reappeared`` -> ``reappeared``  (not re-triaged yet)
    * ``resolved`` -> ``raised``  (a previously-resolved fingerprint re-reported)

    A prior issue with no ``status`` (written before ticket 03) is treated as
    ``raised`` so a re-collect migrates it onto the state machine.
    """
    prior_status = prior.get("status")
    if prior_status == STATUS_TRIAGED:
        if _triage_action(prior) == _REQUEST_FIX and _is_fix_targeted(prior):
            return STATUS_REAPPEARED
        return STATUS_TRIAGED
    if prior_status == STATUS_REAPPEARED:
        return STATUS_REAPPEARED
    if prior_status == STATUS_RESOLVED:
        return STATUS_RAISED
    # ``raised``, absent, or unknown -> raised.
    return STATUS_RAISED


def _preserve_triage_to_history(issue: dict[str, Any]) -> None:
    """Move the issue's current ``triage`` into ``triage_history`` and wipe the
    current ``triage`` to ``None`` (ADR-0002 D2/D6).

    Used by two collector transitions:

    * ``reappeared`` (D2 row "wipe -> None | append old"): the prior
      ``request_fix`` triage is invalidated so the lane gate sees
      ``triage is None`` -> FAIL -> the mandatory re-triage step.
    * ``resolved`` (D6 "its triage is preserved into triage_history"): the
      issue is no longer re-reported, so its last disposition is preserved into
      the history log and the active triage cleared. Clearing (rather than
      keeping) means a later re-raise (``resolved -> raised``) starts cleanly
      untriaged instead of carrying a stale disposition.

    No-op when no triage is present (e.g. ticket 05 has not written one yet).
    """
    triage = issue.get("triage")
    if triage is None:
        return
    history = issue.get("triage_history")
    if not isinstance(history, list):
        history = []
    history.append(triage)
    issue["triage_history"] = history
    issue["triage"] = None


def _apply_lifecycle_status(
    merged: dict[str, Any], existing: Mapping[str, Any] | None
) -> None:
    """Fire the collector-owned status transition for one merged issue.

    A brand-new fingerprint (``existing is None``) starts at ``raised``. A
    re-reported fingerprint is driven through ``_recollect_target_status``; the
    ``reappeared`` target also invalidates the prior triage. The transition is
    validated by ``transition_issue_status``, so an illegal jump fails loud.
    """
    if existing is None:
        merged["status"] = initial_issue_status()
        return
    prior_status = existing.get("status")
    target = _recollect_target_status(existing)
    merged["status"] = transition_issue_status(prior_status, target)
    if target == STATUS_REAPPEARED:
        _preserve_triage_to_history(merged)


def _normalize_issues(
    feature_root: Path, raw_issues: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    existing = _existing_issues_by_fingerprint(feature_root)
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []

    for issue in raw_issues:
        key = _fingerprint(issue)
        if key in seen:
            continue
        seen.add(key)
        prior = existing.get(key)
        if prior is not None:
            # Fingerprint match -> reuse the stable id (and its persisted state).
            issue_id = str(prior["id"])
        else:
            issue_id = allocate_id(feature_root, "ISSUE")
        # The collector is the authoritative place where §5.2 feature-stable
        # ISSUE ids are assigned; agent-local ids on the report are not stable.
        normalized.append(_merge_issue(prior, issue, issue_id))
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
    """Project the merged ``issues/`` state into the lane bundle (ADR-0002 D1).

    The bundle is not an independent source of truth: its ``issues`` are the
    same merged dicts just written to ``issues/ISSUE-NNN.json``, so any
    persisted state (``triage`` / ``status`` / run-tracking) projects straight
    through for the lane gate to read.
    """
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


def _record_resolved_issues(
    feature_root: Path, lane_root: Path, new_fingerprints: set[str]
) -> list[str]:
    """ADR-0002 D6: a fingerprint present in the prior lane bundle but absent
    from the new report is resolved (not re-reported). Transition those
    ``issues/ISSUE-NNN.json`` records to ``status: resolved`` and preserve
    their last disposition into ``triage_history``.

    The diff is scoped to the prior **lane** bundle (not the feature-level
    ``issues/`` dir, which may carry issues other lanes reported) so a
    fingerprint absent from one lane's report cannot resolve an issue another
    lane still reports. Idempotent: an already-``resolved`` record is skipped.
    Reads the prior bundle before the caller overwrites it with the new one.

    D6 also names a ``resolved_in_run`` run-tracking field; the collector has
    no run id today (``collect_issue_bundle`` takes none), so that field - like
    ``first_seen_in_run`` / ``fix_targeted_in_run`` - is written by the
    run-id-aware driver (ticket 07), not here.
    """
    prior_bundle = read_json_object(lane_root / ISSUE_BUNDLE_JSON)
    if prior_bundle is None:
        return []
    raw = prior_bundle.get("issues")
    if not isinstance(raw, list):
        return []
    issue_root = feature_root / ISSUES_DIR
    resolved_ids: list[str] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        if _fingerprint(entry) in new_fingerprints:
            continue
        issue_id = entry.get("id")
        if not isinstance(issue_id, str) or not issue_id:
            continue
        path = issue_root / f"{issue_id}.json"
        issue = read_json_object(path)
        if issue is None:
            continue
        prior_status = issue.get("status")
        if prior_status == STATUS_RESOLVED:
            continue
        issue["status"] = transition_issue_status(prior_status, STATUS_RESOLVED)
        # D6: the issue's last disposition is preserved into triage_history
        # (and the active triage cleared) as it leaves the active set.
        _preserve_triage_to_history(issue)
        write_json(path, issue)
        resolved_ids.append(issue_id)
    return resolved_ids



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
    issues = _normalize_issues(feature_root, raw_issues)

    issue_root = feature_root / ISSUES_DIR
    for issue in issues:
        issue_id = str(issue["id"])
        write_json(issue_root / f"{issue_id}.json", issue)
        (issue_root / f"{issue_id}.md").write_text(_issue_md(issue))

    # ADR-0002 D6: diff the prior lane bundle against this report to record
    # resolutions. Must run before the new bundle overwrites the prior one.
    new_fingerprints = {_fingerprint(issue) for issue in issues}
    resolved_ids = _record_resolved_issues(feature_root, lane_root, new_fingerprints)

    # The bundle is a projection of ``issues/`` (ADR-0002 D1): it carries the
    # merged issue state just written to ``issues/ISSUE-NNN.json`` - report
    # fields plus any persisted state - so the lane gate sees the same truth.
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
            "resolved_issue_ids": resolved_ids,
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
