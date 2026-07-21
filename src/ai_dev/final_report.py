"""``ai-dev final-report`` generator (v0.3, ADR-0003 D5/D6/D7, ticket 09).

The §23.5 step-21 projection writer. Reads the coherence ``verdict`` (written by
:mod:`ai_dev.coherence_gate`, the sole verdict writer per ADR-0003 D4) and the
feature-run artifacts, and produces a re-computable ``final-report.json`` (the
canonical projection) plus a deterministic ``final-report.md`` skeleton rendered
*from* that JSON. No model is called here (invariant #2); no canonical state is
mutated and no audit event is appended (D7 supplement b - a pure render, unlike
the audited ``coherence-gate`` verdict write).

``final-report.json`` is keyed by §2.1's five audit questions
(``code_to_requirement`` / ``requirement_coverage`` /
``acceptance_verification`` / ``issue_dispositions`` / ``agent_timeline``) plus
``meta`` and the failure-shape (``verdict`` + ``failure_class`` +
``blocking_reasons``). Four generation disciplines (ADR-0003 D5/D6):

1. **Failure-shape is the auditable carrier of D6's classification.** ``verdict``
   + ``failure_class: recoverable|terminal|null`` + ``blocking_reasons[]``
   (each ``issue_id`` / ``kind`` / ``resolution_path``). ``null`` / ``[]`` when
   ``verdict == pass``.
2. **Stable enumeration, keys always present, values may be empty.** Multi-value
   inputs are enumerated by stable key sort; every top-level key is always
   present. A validator can thus distinguish *absent because empty* (legitimate)
   from *absent because corrupt* (§24.2).
3. **The code->requirement traceability index (Q1) must exist.** When no run
   contributes ``changed_files`` the index is left **explicitly empty with a
   ``meta.known_gaps`` marker** - never silently omitted.
4. **Per-section inner field enumeration is bounded here** (the ADR pinned only
   the skeleton); each section carries a small, deterministic field set.

The report is **not frozen** (§4.2's frozen set excludes it) and is
**re-computable**: the same artifacts yield byte-identical JSON, so the
generator writes no wall-clock timestamp (it stamps ``meta`` from the audit log,
which is itself an artifact).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ai_dev.audit import AUDIT_LOG_JSON
from ai_dev.checking_legs import REVIEW_REPORT_JSON, SPEC_GAP_REPORT_JSON
from ai_dev.coherence_gate import COHERENCE_DECISION_JSON
from ai_dev.implement_leg import IMPLEMENT_RESULT_JSON
from ai_dev.issue_bundle import ISSUE_BUNDLE_JSON, ISSUES_DIR
from ai_dev.json_artifact import read_json_object, write_json
from ai_dev.lane_gate import LANE_DECISION_JSON
from ai_dev.paths import METADATA_JSON, feature_dir
from ai_dev.status import load_feature_status
from ai_dev.templates import REQUIREMENTS_JSON
from ai_dev.triage import DECISIONS_DIR

FINAL_REPORT_JSON = "final-report.json"
FINAL_REPORT_MD = "final-report.md"

# The five §2.1 audit questions, in spec order. Public so a validator can pin
# "five keys present" mechanically (ADR-0003 D5 / OQ11).
FIVE_QUESTION_KEYS: tuple[str, ...] = (
    "code_to_requirement",       # §2.1 Q1: which code corresponds to which requirement
    "requirement_coverage",      # §2.1 Q2: is a requirement implemented
    "acceptance_verification",   # §2.1 Q3: what verifies an acceptance criterion
    "issue_dispositions",        # §2.1 Q4: why was an issue accepted/rejected/overridden
    "agent_timeline",            # §2.1 Q5: which profile did what when
)

_BLOCKING_SEVERITIES = {"P0", "P1"}
# ADR-0001 #3: the (disposition x severity) cells that disarm a blocking issue.
_DISARMING_ACTIONS = {
    ("P0", "reject"),
    ("P1", "override"),
    ("P1", "reject"),
}
_RECOVERABLE = "recoverable"
_TERMINAL = "terminal"


@dataclass(frozen=True)
class FinalReportResult:
    """Summary of one deterministic final-report generation."""

    feature_id: str
    verdict: str
    failure_class: str | None
    report_json_path: Path
    report_md_path: Path


# ---------------------------------------------------------------------------
# Small defensive readers (optionality per ADR-0003 D6).
# ---------------------------------------------------------------------------


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _load_required_json(path: Path, what: str) -> dict[str, Any]:
    """Read a required JSON object; fail-loud (§24.2) if missing/invalid.

    ADR-0003 D6: ``feature-status.yml``, ``lane-decision.json`` and the issue
    bundle are *required* artifacts - their absence is a generator error, not a
    silent empty report. ``coherence-decision.json`` is required too: a non-null
    verdict implies coherence ran, and coherence always writes its decision
    record, so a missing record over a non-null verdict is corruption.
    """
    obj = read_json_object(path)
    if obj is None:
        raise ValueError(
            f"required {what} missing or invalid at {path} (§24.2)"
        )
    return obj


def _load_optional_json(path: Path) -> dict[str, Any] | None:
    """Read an optional JSON object; ``None`` when missing/invalid (D6).

    ``decisions/``, fix-loop runs, and an empty ``issues/`` bundle are optional:
    the generator must not crash on their absence.
    """
    return read_json_object(path)


def _load_all_issues(feature_root: Path) -> list[dict[str, Any]]:
    """Read every ``issues/ISSUE-NNN.json`` sorted by id; empty when none.

    A clean feature raises no issues, so an empty ``issues/`` is legitimate
    (D6). A structurally invalid issue file is corruption - but unlike the
    coherence gate (which fail-louds on it), the report is a projection, so a
    single unreadable issue is skipped rather than aborting the whole report;
    the good issues still answer Q4. (Required-issue-set corruption is caught
    upstream by the lane gate / coherence gate, the authoritative readers.)
    """
    issues: list[dict[str, Any]] = []
    issue_root = feature_root / ISSUES_DIR
    if not issue_root.is_dir():
        return issues
    for path in sorted(issue_root.glob("ISSUE-*.json")):
        issue = read_json_object(path)
        if issue is not None:
            issues.append(issue)
    return issues


def _load_lane_decisions(feature_root: Path) -> list[dict[str, Any]]:
    """Read every lane-decision.json under lanes/ sorted by path (D6: required)."""
    decisions: list[dict[str, Any]] = []
    for path in sorted((feature_root / "lanes").glob(f"*/{LANE_DECISION_JSON}")):
        decisions.append(_load_required_json(path, "lane-decision.json"))
    if not decisions:
        raise ValueError(
            f"no lane-decision.json found under {feature_root}/lanes/; run "
            f"`ai-dev lane-gate` before `ai-dev final-report` (§24.2)"
        )
    return decisions


def _load_lane_bundles(feature_root: Path) -> list[dict[str, Any]]:
    """Read every lane issue-bundle.json (D6: the issue bundle is required)."""
    from ai_dev.issue_bundle import ISSUE_BUNDLE_JSON

    # The bundle content is unused beyond this existence check; the Q4
    # issue-disposition rows are read from ``issues/ISSUE-NNN.json``, the single
    # source of truth. The bundle is loaded only to enforce its required
    # presence (ADR-0003 D6: the issue bundle is a required artifact).
    bundles: list[dict[str, Any]] = []
    for path in sorted((feature_root / "lanes").glob(f"*/{ISSUE_BUNDLE_JSON}")):
        bundles.append(_load_required_json(path, "issue-bundle.json"))
    if not bundles:
        raise ValueError(
            f"no issue-bundle.json found under {feature_root}/lanes/; run "
            f"`ai-dev collect-issues` before `ai-dev final-report` (§24.2)"
        )
    return bundles


# ---------------------------------------------------------------------------
# Q5 / Q1 / Q2 / Q3 inputs: runs, implement-results, requirements.
# ---------------------------------------------------------------------------


def _run_metadata(feature_root: Path) -> dict[str, dict[str, Any]]:
    """Map ``RUN-NNN`` -> its ``output/metadata.json`` (optional; D6 defensive).

    A run directory without a metadata.json (e.g. one that never captured) is
    skipped - it contributes no timeline entry and no changed-files.
    """
    runs: dict[str, dict[str, Any]] = {}
    runs_root = feature_root / "runs"
    if not runs_root.is_dir():
        return runs
    for path in sorted(runs_root.glob("RUN-*/output/" + METADATA_JSON)):
        md = read_json_object(path)
        run_id = path.parent.parent.name
        if md is not None and isinstance(run_id, str):
            runs[run_id] = md
    return runs


def _run_role_map(feature_root: Path) -> dict[str, str]:
    """Map ``RUN-NNN`` -> role, from the lane reports that carry one.

    implement-result.json (Implementer), review-report.json (Code Reviewer) and
    spec-gap-report.json (Spec Gap Analyst) each roll up the run id and the
    pinned role. Verification runs are deterministic shell (no profile) and
    carry no role - they surface in the timeline via metadata only when present.
    """
    role_map: dict[str, str] = {}
    patterns = [
        f"*/{IMPLEMENT_RESULT_JSON}",
        f"*/review/{REVIEW_REPORT_JSON}",
        f"*/spec-gap/{SPEC_GAP_REPORT_JSON}",
    ]
    for pattern in patterns:
        for path in sorted((feature_root / "lanes").glob(pattern)):
            report = read_json_object(path)
            if report is None:
                continue
            run_id = _str(report.get("run")) or _str(report.get("run_id"))
            role = _str(report.get("role"))
            if run_id and role:
                role_map.setdefault(run_id, role)
    return role_map


def _implement_results(feature_root: Path) -> list[dict[str, Any]]:
    """Read every lane implement-result.json (carries related_requirements/ACs)."""
    results: list[dict[str, Any]] = []
    for path in sorted((feature_root / "lanes").glob(f"*/{IMPLEMENT_RESULT_JSON}")):
        obj = read_json_object(path)
        if obj is not None:
            results.append(obj)
    return results


def _requirements_doc(feature_root: Path) -> dict[str, Any] | None:
    """Read ``01-requirements.json`` (optional in v0.3; seeded empty at create)."""
    return _load_optional_json(feature_root / REQUIREMENTS_JSON)


def _requirement_ids(req_doc: Mapping[str, Any] | None) -> list[str]:
    raw = req_doc.get("requirements") if req_doc else None
    if not isinstance(raw, list):
        return []
    ids: list[str] = []
    for entry in raw:
        if isinstance(entry, Mapping):
            rid = _str(entry.get("id"))
            if rid:
                ids.append(rid)
        elif isinstance(entry, str) and entry:
            ids.append(entry)
    return sorted(set(ids))


def _acceptance_criterion_ids(req_doc: Mapping[str, Any] | None) -> list[str]:
    raw = req_doc.get("acceptance_criteria") if req_doc else None
    if not isinstance(raw, list):
        return []
    ids: list[str] = []
    for entry in raw:
        if isinstance(entry, Mapping):
            aid = _str(entry.get("id"))
            if aid:
                ids.append(aid)
        elif isinstance(entry, str) and entry:
            ids.append(entry)
    return sorted(set(ids))


# ---------------------------------------------------------------------------
# Section builders.
# ---------------------------------------------------------------------------


def _agent_timeline(
    runs: Mapping[str, Mapping[str, Any]],
    role_map: Mapping[str, str],
) -> list[dict[str, Any]]:
    """§2.1 Q5: which profile did what when. One entry per captured run, sorted
    by ``started_at`` (then run id) so the timeline is stable."""
    entries: list[dict[str, Any]] = []
    for run_id, md in runs.items():
        entries.append(
            {
                "run_id": run_id,
                "profile": _str(md.get("profile")),
                "role": role_map.get(run_id),
                "started_at": _str(md.get("started_at")),
                "ended_at": _str(md.get("ended_at")),
                "exit_code": md.get("exit_code") if isinstance(md.get("exit_code"), int) else None,
                "changed_files": _str_list(md.get("changed_files")),
            }
        )
    entries.sort(key=lambda e: (e["started_at"] or "", e["run_id"]))
    return entries


def _code_to_requirement(
    runs: Mapping[str, Mapping[str, Any]],
    implement_results: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """§2.1 Q1: which code corresponds to which requirement.

    Maps each run's ``changed_files`` (from metadata) to the requirements that
    run's implement-result declared (``related_requirements``). Returns the
    index plus a ``known_gap`` flag: true when no run contributed any
    changed-files (D5 constraint 3 - explicit empty + known gap, never a silent
    omission). Sorted by (file, source_run) for stable enumeration.
    """
    run_to_reqs: dict[str, list[str]] = {}
    for rollup in implement_results:
        run_id = _str(rollup.get("run"))
        if run_id:
            run_to_reqs[run_id] = sorted(set(_str_list(rollup.get("related_requirements"))))

    index: list[dict[str, Any]] = []
    for run_id, md in runs.items():
        for file in sorted(set(_str_list(md.get("changed_files")))):
            index.append(
                {
                    "file": file,
                    "source_run": run_id,
                    "requirements": run_to_reqs.get(run_id, []),
                }
            )
    index.sort(key=lambda e: (e["file"], e["source_run"]))
    known_gap = not any(_str_list(md.get("changed_files")) for md in runs.values())
    return index, known_gap


def _requirement_coverage(
    req_ids: list[str],
    implement_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """§2.1 Q2: is a requirement implemented?

    A requirement is ``implemented`` when some implement-result declares it in
    ``related_requirements``; ``evidence_runs`` lists those runs. Requirements
    the Planner never allocated (an empty ``01-requirements.json`` at v0.3) yield
    an empty list - legitimate (constraint 2), not corruption.
    """
    coverage: list[dict[str, Any]] = []
    for rid in req_ids:
        evidence = sorted(
            str(run_id)
            for run_id in (
                _str(r.get("run"))
                for r in implement_results
                if rid in _str_list(r.get("related_requirements"))
            )
            if run_id
        )
        coverage.append(
            {
                "requirement": rid,
                "implemented": bool(evidence),
                "evidence_runs": evidence,
            }
        )
    return coverage


def _acceptance_verification(
    ac_ids: list[str],
    implement_results: list[dict[str, Any]],
    lane_decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """§2.1 Q3: what verifies an acceptance criterion?

    An acceptance criterion is ``verified`` when some implement-result declares
    it in ``related_acceptance_criteria``; ``evidence_runs`` lists those runs and
    ``lane_verification`` carries the lane gate verdict (the independent §18.4
    condition). v0.3 has no AC->test traceability index yet, so evidence is
    run-declared - a known limitation recorded in ``meta.known_gaps`` when the
    section is non-empty.
    """
    # Aggregate the lane-gate verdicts meaningfully: ``pass`` iff every lane
    # passed, otherwise the first non-pass decision. (Sorting alphabetically and
    # taking [0] would let "fail" win over "pass" by accident; v0.3 is
    # single-lane so this only matters in the multi-lane future, but the
    # aggregation is written correctly now.)
    decisions = [
        v for v in (_str(d.get("decision")) for d in lane_decisions) if v
    ]
    non_pass = [d for d in decisions if d != "pass"]
    lane_verification = non_pass[0] if non_pass else ("pass" if decisions else None)
    rows: list[dict[str, Any]] = []
    for aid in ac_ids:
        evidence = sorted(
            str(run_id)
            for run_id in (
                _str(r.get("run"))
                for r in implement_results
                if aid in _str_list(r.get("related_acceptance_criteria"))
            )
            if run_id
        )
        rows.append(
            {
                "acceptance_criterion": aid,
                "verified": bool(evidence),
                "evidence_runs": evidence,
                "lane_verification": lane_verification,
            }
        )
    return rows


def _issue_dispositions(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """§2.1 Q4: why was an issue accepted/rejected/overridden.

    One row per persisted issue, sorted by id. ``disposition``/``reason`` are
    ``null`` when no triage has been written (an untriaged issue is still an
    auditable answer: "not yet triaged"). ``decision_ids`` is ``[]`` when the
    triage did not promote a DEC.
    """
    rows: list[dict[str, Any]] = []
    for issue in sorted(issues, key=lambda i: _str(i.get("id")) or ""):
        triage = issue.get("triage")
        triage = triage if isinstance(triage, Mapping) else {}
        rows.append(
            {
                "issue_id": _str(issue.get("id")),
                "severity": _str(issue.get("severity")),
                "status": _str(issue.get("status")),
                "source": _str(issue.get("source")),
                "disposition": _str(triage.get("action")),
                "reason": _str(triage.get("reason")),
                "decision_ids": _str_list(triage.get("decision_ids")),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Failure-shape (D6): failure_class + blocking_reasons[].
# ---------------------------------------------------------------------------


def _severity(issue: Mapping[str, Any]) -> str:
    return _str(issue.get("severity")) or ""


def _issue_status(issue: Mapping[str, Any]) -> str:
    return _str(issue.get("status")) or ""


def _triage(issue: Mapping[str, Any]) -> Mapping[str, Any]:
    triage = issue.get("triage")
    return triage if isinstance(triage, Mapping) else {}


def _triage_action(issue: Mapping[str, Any]) -> str | None:
    return _str(_triage(issue).get("action"))


def _is_resolved(issue: Mapping[str, Any]) -> bool:
    return _issue_status(issue) == "resolved"


def _is_disarmed(issue: Mapping[str, Any]) -> bool:
    severity = _severity(issue)
    action = _triage_action(issue)
    if severity not in _BLOCKING_SEVERITIES:
        return False
    if action is None or (severity, action) not in _DISARMING_ACTIONS:
        return False
    reason = _str(_triage(issue).get("reason"))
    return reason is not None and bool(reason.strip())


def _is_handled(issue: Mapping[str, Any]) -> bool:
    """Mirror of coherence_gate._p0_p1_handled: P0/P1 resolved or disarmed."""
    if _severity(issue) not in _BLOCKING_SEVERITIES:
        return True
    if _is_resolved(issue):
        return True
    return _is_disarmed(issue)


def _issue_decision_ids(issue: Mapping[str, Any]) -> list[str]:
    return _str_list(_triage(issue).get("decision_ids"))


def _blocking_reason(
    issue_id: str | None,
    kind: str,
    resolution_path: str,
    *,
    klass: str,
    detail: str = "",
) -> dict[str, Any]:
    return {
        "issue_id": issue_id,
        "kind": kind,
        "resolution_path": resolution_path,
        "class": klass,
        "detail": detail,
    }


def _issue_level_blocking_reasons(
    issues: list[dict[str, Any]], feature_root: Path
) -> list[dict[str, Any]]:
    """Re-derive structured per-issue blocking reasons from the same inputs the
    coherence gate uses (issues/, decisions/, lane-decisions). Independent of
    the coherence-decision prose, so the report is a clean re-computation."""
    reasons: list[dict[str, Any]] = []
    for issue in issues:
        issue_id = _str(issue.get("id"))
        severity = _severity(issue)
        # Only P0/P1 can block; P2/P3 are never blocking.
        if severity in _BLOCKING_SEVERITIES and not _is_handled(issue):
            action = _triage_action(issue)
            status = _issue_status(issue)
            if action == "request_change_proposal":
                # D6 terminal: v0.3 has no CP lifecycle; recorded as a clean
                # deferral that cannot reach pass without the v0.4 CP lifecycle.
                reasons.append(
                    _blocking_reason(
                        issue_id,
                        "pending_change_proposal",
                        "change_proposal",
                        klass=_TERMINAL,
                        detail=f"{severity} awaits a Change Proposal (CP lifecycle is v0.4)",
                    )
                )
            elif action == "request_fix":
                # Recoverable: the bounded fix loop (or re-triage) can unblock it
                # without touching frozen specs.
                reasons.append(
                    _blocking_reason(
                        issue_id,
                        "pending_fix",
                        "fix_run",
                        klass=_RECOVERABLE,
                        detail=f"{severity} request_fix pending a bounded fix run",
                    )
                )
            elif status == "reappeared":
                reasons.append(
                    _blocking_reason(
                        issue_id,
                        "fix_failed_reappeared",
                        "human_triage",
                        klass=_RECOVERABLE,
                        detail=f"{severity} reappeared after a fix run; re-triage required",
                    )
                )
            else:
                # Untriaged (or otherwise unhandled) -> pending human triage.
                reasons.append(
                    _blocking_reason(
                        issue_id,
                        "pending_triage",
                        "human_triage",
                        klass=_RECOVERABLE,
                        detail=f"{severity} unhandled (status={status or 'none'}, action={action or 'none'})",
                    )
                )
        # Disarmed but missing the DEC file (mirrors coherence condition 3).
        if _is_disarmed(issue):
            dec_ids = _issue_decision_ids(issue)
            missing = [
                dec_id
                for dec_id in dec_ids
                if not (feature_root / DECISIONS_DIR / f"{dec_id}.json").is_file()
            ]
            if not dec_ids:
                reasons.append(
                    _blocking_reason(
                        issue_id,
                        "missing_decision",
                        "record_decision",
                        klass=_RECOVERABLE,
                        detail=f"{severity} disarmed but triage carries no DEC-NNN",
                    )
                )
            elif missing:
                reasons.append(
                    _blocking_reason(
                        issue_id,
                        "missing_decision",
                        "record_decision",
                        klass=_RECOVERABLE,
                        detail=f"{severity} references missing Decision file(s): {sorted(missing)}",
                    )
                )
    return reasons


def _lane_level_blocking_reasons(
    lane_decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """A lane gate that did not pass blocks the feature (recoverable: a fix or
    re-triage within the lane can unblock it without a Change Proposal)."""
    reasons: list[dict[str, Any]] = []
    for decision in lane_decisions:
        if _str(decision.get("decision")) == "pass":
            continue
        lane = _str(decision.get("lane")) or _str(decision.get("feature")) or "lane"
        failed = [
            str(c.get("name"))
            for c in decision.get("conditions", [])
            if isinstance(c, Mapping) and not c.get("passed")
        ]
        reasons.append(
            _blocking_reason(
                None,
                "lane_gate_not_passed",
                "fix_or_triage",
                klass=_RECOVERABLE,
                detail=f"lane {lane} gate decision={decision.get('decision')!r}; failed_conditions={failed}",
            )
        )
    return reasons


def _failure_shape(
    verdict: str,
    issues: list[dict[str, Any]],
    lane_decisions: list[dict[str, Any]],
    coherence_decision: Mapping[str, Any],
    feature_root: Path,
) -> tuple[str | None, list[dict[str, Any]]]:
    """Compute ``failure_class`` + ``blocking_reasons`` (ADR-0003 D6).

    On ``pass``: ``failure_class=None``, ``blocking_reasons=[]``. On ``fail``:
    blocking reasons are seeded from the coherence decision's failed conditions
    (authoritative - the verdict *is* their disjunction) then enriched with
    structured per-issue / per-lane reasons. ``failure_class`` is ``terminal``
    iff any reason is terminal, else ``recoverable``. Seeding from the failed
    conditions guarantees ``blocking_reasons`` is never empty on a fail even when
    the structured enrichment finds nothing (defensive completeness).

    D6 note on the terminal cases: D6's prose names "P0 rejected (disarmed,
    won't-fix)" as terminal, but in v0.3's disarming model a P0 ``reject`` is a
    *disarming* action (it mints a DEC and disarms the blocker), so coherence
    treats the issue as handled and the feature PASSES - there is no FAIL report
    to carry that terminal reason. The terminal class is therefore reachable in
    v0.3 only via ``request_change_proposal`` (the clean deferral: v0.3 has no
    CP lifecycle, so the feature cannot reach pass without the v0.4 lifecycle).
    This mirrors the coherence verdict writer's disarming semantics exactly; the
    gap between D6's enumeration and the disarming model is an ADR-level nuance,
    not a generator bug.
    """
    if verdict == "pass":
        return None, []

    reasons: list[dict[str, Any]] = []
    for condition in coherence_decision.get("conditions", []):
        if not isinstance(condition, Mapping) or condition.get("passed"):
            continue
        reasons.append(
            _blocking_reason(
                None,
                f"coherence_condition:{condition.get('name')}",
                "resolve_coherence_condition",
                klass=_RECOVERABLE,
                detail=str(condition.get("reason", "")),
            )
        )
    reasons.extend(_issue_level_blocking_reasons(issues, feature_root))
    reasons.extend(_lane_level_blocking_reasons(lane_decisions))

    failure_class = _TERMINAL if any(r["class"] == _TERMINAL for r in reasons) else _RECOVERABLE
    return failure_class, reasons


# ---------------------------------------------------------------------------
# meta + top-level assembly.
# ---------------------------------------------------------------------------


def _audit_tail(feature_root: Path) -> tuple[int, str | None]:
    """Return ``(event_count, latest_timestamp)`` from audit.log.json.

    Both are deterministic functions of the artifacts (the audit log is itself an
    artifact), so the report stays re-computable without a wall-clock stamp. A
    missing/invalid log yields ``(0, None)`` - the report still generates.
    """
    log = read_json_object(feature_root / AUDIT_LOG_JSON)
    if log is None or not isinstance(log, list):
        return 0, None
    count = len(log)
    latest: str | None = None
    for record in log:
        if isinstance(record, Mapping):
            ts = _str(record.get("timestamp"))
            if ts and (latest is None or ts > latest):
                latest = ts
    return count, latest


def _meta(
    feature_id: str,
    feature: Mapping[str, Any],
    coherence_decision: Mapping[str, Any],
    audit_count: int,
    latest_event_ts: str | None,
    known_gaps: list[str],
) -> dict[str, Any]:
    conditions = coherence_decision.get("conditions", [])
    return {
        "feature": feature_id,
        "current_gate": _str(feature.get("current_gate")),
        "feature_status": _str(feature.get("status")),
        "coherence_conditions": [
            {
                "name": _str(c.get("name")) if isinstance(c, Mapping) else None,
                "passed": bool(c.get("passed")) if isinstance(c, Mapping) else False,
            }
            for c in conditions
            if isinstance(c, Mapping)
        ],
        "audit_event_count": audit_count,
        "latest_event_timestamp": latest_event_ts,
        "known_gaps": sorted(set(known_gaps)),
    }


def _final_report_json(
    feature_id: str,
    feature: Mapping[str, Any],
    verdict: str,
    coherence_decision: Mapping[str, Any],
    code_to_requirement: list[dict[str, Any]],
    code_to_requirement_gap: bool,
    requirement_coverage: list[dict[str, Any]],
    acceptance_verification: list[dict[str, Any]],
    issue_dispositions: list[dict[str, Any]],
    agent_timeline: list[dict[str, Any]],
    failure_class: str | None,
    blocking_reasons: list[dict[str, Any]],
    audit_count: int,
    latest_event_ts: str | None,
) -> dict[str, Any]:
    known_gaps: list[str] = []
    if code_to_requirement_gap:
        known_gaps.append(
            "code_to_requirement: no run contributed changed_files; the Q1 "
            "traceability index is empty (v0.3 collects changed_files per run; "
            "an empty index means none were captured for this feature run)"
        )
    if acceptance_verification:
        known_gaps.append(
            "acceptance_verification: v0.3 has no AC->test traceability index; "
            "verification evidence is run-declared (related_acceptance_criteria), "
            "not a mapped test"
        )

    report: dict[str, Any] = {
        "meta": _meta(
            feature_id,
            feature,
            coherence_decision,
            audit_count,
            latest_event_ts,
            known_gaps,
        ),
        "verdict": verdict,
        FIVE_QUESTION_KEYS[0]: code_to_requirement,
        FIVE_QUESTION_KEYS[1]: requirement_coverage,
        FIVE_QUESTION_KEYS[2]: acceptance_verification,
        FIVE_QUESTION_KEYS[3]: issue_dispositions,
        FIVE_QUESTION_KEYS[4]: agent_timeline,
        "failure_class": failure_class,
        "blocking_reasons": blocking_reasons,
    }
    return report


# ---------------------------------------------------------------------------
# MD skeleton (deterministic render from the JSON; D5: no narrative in v0.3).
# ---------------------------------------------------------------------------


def _render_list(items: list[Mapping[str, Any]], formatter: Any) -> list[str]:
    lines: list[str] = []
    if not items:
        lines.append("_none_")
    else:
        for item in items:
            lines.append(f"- {formatter(item)}")
    return lines


def _final_report_md(report: Mapping[str, Any]) -> str:
    meta = report.get("meta", {}) if isinstance(report.get("meta"), Mapping) else {}
    lines: list[str] = [
        f"# Final Report - {report.get('verdict', '').upper()}",
        "",
        f"- feature: {meta.get('feature', '')}",
        f"- verdict: **{report.get('verdict', '')}**",
        f"- feature_status: {meta.get('feature_status', '')}",
        f"- current_gate: {meta.get('current_gate', '')}",
        f"- failure_class: {report.get('failure_class')}",
        f"- latest_event_timestamp: {meta.get('latest_event_timestamp')}",
        "",
        "> Deterministic skeleton rendered from `final-report.json` (ADR-0003",
        "> D5). v0.3 ships no narrative section: every line below is canonical",
        "> audit fact projected from the feature-run artifacts. Future",
        "> model-generated narrative lands in a separate, clearly-marked",
        "> non-canonical section (spec/model isolation).",
        "",
        "## Code -> Requirement (Q1)",
        "",
    ]
    lines.extend(
        _render_list(
            report.get("code_to_requirement", []),
            lambda e: (
                f"`{e.get('file')}` (run {e.get('source_run')}) -> "
                f"{e.get('requirements') or 'no requirement declared'}"
            ),
        )
    )
    lines += ["", "## Requirement Coverage (Q2)", ""]
    lines.extend(
        _render_list(
            report.get("requirement_coverage", []),
            lambda e: (
                f"`{e.get('requirement')}`: "
                f"{'implemented' if e.get('implemented') else 'NOT implemented'} "
                f"(evidence_runs={e.get('evidence_runs')})"
            ),
        )
    )
    lines += ["", "## Acceptance Verification (Q3)", ""]
    lines.extend(
        _render_list(
            report.get("acceptance_verification", []),
            lambda e: (
                f"`{e.get('acceptance_criterion')}`: "
                f"{'verified' if e.get('verified') else 'NOT verified'} "
                f"(evidence_runs={e.get('evidence_runs')}, "
                f"lane_verification={e.get('lane_verification')})"
            ),
        )
    )
    lines += ["", "## Issue Dispositions (Q4)", ""]
    lines.extend(
        _render_list(
            report.get("issue_dispositions", []),
            lambda e: (
                f"`{e.get('issue_id')}` [{e.get('severity')}] "
                f"status={e.get('status')} disposition={e.get('disposition')} "
                f"decisions={e.get('decision_ids')} :: {e.get('reason') or 'no reason recorded'}"
            ),
        )
    )
    lines += ["", "## Agent Timeline (Q5)", ""]
    lines.extend(
        _render_list(
            report.get("agent_timeline", []),
            lambda e: (
                f"`{e.get('run_id')}` profile={e.get('profile')} role={e.get('role')} "
                f"started={e.get('started_at')} ended={e.get('ended_at')} "
                f"exit={e.get('exit_code')} changed_files={len(e.get('changed_files') or [])}"
            ),
        )
    )
    lines += ["", "## Failure Shape", ""]
    lines.append(f"- failure_class: {report.get('failure_class')}")
    lines.append(f"- blocking_reasons: {len(report.get('blocking_reasons', []))}")
    for reason in report.get("blocking_reasons", []):
        if not isinstance(reason, Mapping):
            continue
        lines.append(
            f"  - [{reason.get('class')}] {reason.get('kind')} "
            f"(issue={reason.get('issue_id')}, path={reason.get('resolution_path')}): "
            f"{reason.get('detail')}"
        )
    known = meta.get("known_gaps", []) if isinstance(meta.get("known_gaps"), list) else []
    lines += ["", "## Known Gaps", ""]
    if known:
        for gap in known:
            lines.append(f"- {gap}")
    else:
        lines.append("_none_")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FinalReportCompute:
    """The pure read+compute half of the final report (no writes).

    Extracted so ``--dry-run`` (ticket 04 / ADR-0004) can report the would-be
    verdict + failure_class without writing ``final-report.{json,md}``. The
    writer (``generate_final_report``) and the dry-run planner share this one
    computation so they can never diverge.
    """

    verdict: str
    failure_class: str | None
    report: dict[str, Any]


def compute_final_report(feature_root: Path) -> FinalReportCompute:
    """Run the §23.5 projection read+compute, no writes.

    Reads the coherence verdict + feature-run artifacts, enforces the §24.2
    preconditions (verdict present and pass/fail, coherence-decision + lane
    bundles exist), and assembles the full ``final-report.json`` document. Pure
    of side effects: writes nothing. Re-running over the same artifacts yields a
    byte-identical document (no wall-clock stamp).
    """
    feature_id = feature_root.name
    feature = load_feature_status(feature_root)["feature"]
    verdict = feature.get("verdict")
    if verdict is None:
        raise ValueError(
            f"feature-status.yml verdict is null for {feature_id}; run "
            f"`ai-dev coherence-gate` before `ai-dev final-report` "
            f"(the report consumes the coherence verdict, §24.2 / ADR-0003 D7-c)"
        )
    if verdict not in ("pass", "fail"):
        raise ValueError(
            f"feature-status.yml verdict is {verdict!r}; expected pass/fail (§24.2)"
        )

    coherence_decision = _load_required_json(
        feature_root / COHERENCE_DECISION_JSON, "coherence-decision.json"
    )
    lane_decisions = _load_lane_decisions(feature_root)
    _load_lane_bundles(feature_root)  # required-existence check (D6); content unused
    issues = _load_all_issues(feature_root)

    runs = _run_metadata(feature_root)
    role_map = _run_role_map(feature_root)
    implement_results = _implement_results(feature_root)
    req_doc = _requirements_doc(feature_root)
    req_ids = _requirement_ids(req_doc)
    ac_ids = _acceptance_criterion_ids(req_doc)

    agent_timeline = _agent_timeline(runs, role_map)
    code_to_requirement, code_to_requirement_gap = _code_to_requirement(
        runs, implement_results
    )
    requirement_coverage = _requirement_coverage(req_ids, implement_results)
    acceptance_verification = _acceptance_verification(
        ac_ids, implement_results, lane_decisions
    )
    issue_dispositions = _issue_dispositions(issues)
    failure_class, blocking_reasons = _failure_shape(
        verdict, issues, lane_decisions, coherence_decision, feature_root
    )
    audit_count, latest_event_ts = _audit_tail(feature_root)

    report = _final_report_json(
        feature_id,
        feature,
        verdict,
        coherence_decision,
        code_to_requirement,
        code_to_requirement_gap,
        requirement_coverage,
        acceptance_verification,
        issue_dispositions,
        agent_timeline,
        failure_class,
        blocking_reasons,
        audit_count,
        latest_event_ts,
    )
    return FinalReportCompute(
        verdict=verdict, failure_class=failure_class, report=report
    )


def generate_final_report(repo_root: Path, feature_id: str) -> FinalReportResult:
    """Generate ``final-report.{json,md}`` for ``feature_id`` (ADR-0003 D5/D6/D7).

    A pure projection: reads the coherence ``verdict`` and the feature-run
    artifacts, writes the two report files, touches no canonical state and
    appends no audit event. ``verdict == null`` (coherence has not run) fail-loud
    refuses (§24.2 / D7 supplement c) - the report is downstream of coherence and
    cannot consume a verdict that does not exist. Re-running over the same
    artifacts yields byte-identical output (no wall-clock stamp).
    """
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id} not found under {repo_root}")

    compute = compute_final_report(feature_root)
    report = compute.report

    report_json_path = feature_root / FINAL_REPORT_JSON
    report_md_path = feature_root / FINAL_REPORT_MD
    write_json(report_json_path, report)
    report_md_path.write_text(_final_report_md(report))

    return FinalReportResult(
        feature_id=feature_id,
        verdict=compute.verdict,
        failure_class=compute.failure_class,
        report_json_path=report_json_path,
        report_md_path=report_md_path,
    )
