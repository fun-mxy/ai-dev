"""Lane gate evaluator (v0.3 triage-aware blocking formula).

This module is the deterministic final gate for a lane. It reads only the
existing lane artifacts (implement result, shell verification report, and issue
bundle), applies the §18.4 conditions plus the ADR-0001 lane-gate blocking
formula, and writes the lane-level ``lane-decision.{json,md}`` double product.
No model is called here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ai_dev.audit import append_audit_event
from ai_dev.implement_leg import IMPLEMENT_RESULT_JSON
from ai_dev.issue_bundle import ISSUE_BUNDLE_JSON
from ai_dev.json_artifact import read_json_object, write_json
from ai_dev.paths import feature_dir, lane_dir
from ai_dev.shell_verifier import VERIFICATION_DIR, VERIFICATION_REPORT_JSON
from ai_dev.status import update_lane_status
from ai_dev.timeutil import elapsed_ms_between, utc_now_iso

LANE_DECISION_MD = "lane-decision.md"
LANE_DECISION_JSON = "lane-decision.json"

_LANE_GATE_EVENT = "lane_gate"
_BLOCKING_SEVERITIES = {"P0", "P1"}
_DISARMING_ACTIONS = {
    ("P0", "reject"),
    ("P1", "override"),
    ("P1", "reject"),
}
_FOLLOWUP_ACTIONS = {"request_fix", "request_change_proposal", "request_cp"}
_ILLEGAL_BLOCKING_ACTIONS = {"accept", "defer"}


@dataclass(frozen=True)
class LaneDecisionResult:
    """Summary of one deterministic lane gate evaluation."""

    feature_id: str
    lane_id: str
    decision: str
    conditions: list[dict[str, Any]]
    blocking_issues: list[dict[str, Any]]
    decision_md_path: Path
    decision_json_path: Path

    @property
    def passed(self) -> bool:
        """True when all lane-gate conditions passed."""
        return self.decision == "pass"

    @property
    def condition_count(self) -> int:
        """Number of §18.4 conditions evaluated."""
        return len(self.conditions)

    @property
    def passed_condition_count(self) -> int:
        """Number of §18.4 conditions that passed."""
        return sum(1 for c in self.conditions if c.get("passed"))

    @property
    def failed_conditions(self) -> list[str]:
        """Condition names whose result was failing."""
        return [str(c["name"]) for c in self.conditions if not c.get("passed")]



def _require_artifact(path: Path) -> dict[str, Any]:
    artifact = read_json_object(path)
    if artifact is None:
        raise ValueError(f"required lane gate artifact missing or invalid: {path}")
    return artifact


def _extract_issues(bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = bundle.get("issues")
    if not isinstance(raw, list):
        return []
    return [dict(issue) for issue in raw if isinstance(issue, dict)]


def _issue_source(issue: Mapping[str, Any]) -> str:
    source = issue.get("source")
    return source if isinstance(source, str) else ""


def _issue_severity(issue: Mapping[str, Any]) -> str:
    severity = issue.get("severity")
    return severity if isinstance(severity, str) else ""


def _triage(issue: Mapping[str, Any]) -> Mapping[str, Any] | None:
    triage = issue.get("triage")
    return triage if isinstance(triage, Mapping) else None


def _triage_action(issue: Mapping[str, Any]) -> str | None:
    triage = _triage(issue)
    if triage is None:
        return None
    action = triage.get("action")
    return action if isinstance(action, str) else None


def _triage_reason(issue: Mapping[str, Any]) -> str | None:
    triage = _triage(issue)
    if triage is None:
        return None
    reason = triage.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return None
    return reason


def _decision_ids(issue: Mapping[str, Any]) -> list[str]:
    triage = _triage(issue)
    if triage is None:
        return []
    raw = triage.get("decision_ids")
    if not isinstance(raw, list):
        return []
    return [str(decision_id) for decision_id in raw if isinstance(decision_id, str)]


def _blocking_reason(issue: Mapping[str, Any]) -> str | None:
    severity = _issue_severity(issue)
    if severity not in _BLOCKING_SEVERITIES:
        return None

    action = _triage_action(issue)
    if action is None:
        return f"{severity} is untriaged"
    if action in _FOLLOWUP_ACTIONS:
        return f"{severity} triage action {action} is unresolved"
    if action == "override" and severity == "P0":
        return "P0 override is not recognized by the lane gate"
    if action in _ILLEGAL_BLOCKING_ACTIONS:
        return f"{action} is illegal on {severity} and fails loud at the gate"
    if (severity, action) in _DISARMING_ACTIONS:
        if not _triage_reason(issue):
            return f"{severity} {action} is missing required triage reason"
        if _decision_ids(issue):
            return None
        return f"{severity} {action} is missing required Decision id"
    return f"{severity} triage action {action} does not disarm the gate"


def _blocking_issues(issues: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    return [
        issue
        for issue in issues
        if _issue_source(issue) == source and _blocking_reason(issue) is not None
    ]


def _summarize_issue(issue: Mapping[str, Any]) -> dict[str, Any]:
    summary = {
        "id": issue.get("id"),
        "source": issue.get("source"),
        "severity": issue.get("severity"),
        "title": issue.get("title"),
        "requires_change_proposal": issue.get("requires_change_proposal", False),
        "triage_action": _triage_action(issue),
    }
    decision_ids = _decision_ids(issue)
    if decision_ids:
        summary["decision_ids"] = decision_ids
    reason = _blocking_reason(issue)
    if reason is not None:
        summary["blocking_reason"] = reason
    action = _triage_action(issue)
    if action in _FOLLOWUP_ACTIONS:
        summary["resolution_path"] = action
    return summary


def _condition(name: str, passed: bool, reason: str) -> dict[str, Any]:
    return {"name": name, "passed": passed, "reason": reason}


def _proposed_done_condition(implement_result: Mapping[str, Any]) -> dict[str, Any]:
    status = implement_result.get("status")
    if status == "proposed_done":
        tasks = implement_result.get("tasks")
        proposed = [
            task.get("id")
            for task in tasks
            if isinstance(task, Mapping) and task.get("status") == "proposed_done"
        ] if isinstance(tasks, list) else []
        detail = f"; tasks={proposed}" if proposed else ""
        return _condition(
            "proposed_done",
            True,
            f"implement result status is proposed_done{detail}",
        )
    return _condition(
        "proposed_done",
        False,
        f"implement result status is {status!r}, expected 'proposed_done'",
    )


def _verification_condition(report: Mapping[str, Any]) -> dict[str, Any]:
    verdict = report.get("verdict")
    passed_count = report.get("passed_count", 0)
    command_count = report.get("command_count", 0)
    if verdict == "pass":
        return _condition(
            "verification_passed",
            True,
            f"verification verdict is pass ({passed_count}/{command_count} commands passed)",
        )
    return _condition(
        "verification_passed",
        False,
        f"verification verdict is {verdict} ({passed_count}/{command_count} commands passed)",
    )


def _no_blocking_condition(name: str, label: str, blockers: list[dict[str, Any]]) -> dict[str, Any]:
    if not blockers:
        return _condition(name, True, f"no triage-blocking {label} issues")
    reasons = "; ".join(
        f"{issue.get('id', '<unknown>')}: {_blocking_reason(issue)}"
        for issue in blockers
    )
    return _condition(
        name,
        False,
        f"triage-blocking {label} issue(s): {len(blockers)}; {reasons}",
    )


def _decision_json(
    feature_id: str,
    lane_id: str,
    conditions: list[dict[str, Any]],
    blocking_issues: list[dict[str, Any]],
) -> dict[str, Any]:
    decision = "pass" if all(c.get("passed") for c in conditions) else "fail"
    return {
        "feature": feature_id,
        "lane": lane_id,
        "decision": decision,
        "conditions": conditions,
        "blocking_issue_count": len(blocking_issues),
        "blocking_issues": blocking_issues,
    }


def _decision_md(decision: Mapping[str, Any]) -> str:
    raw_conditions = decision.get("conditions")
    conditions = raw_conditions if isinstance(raw_conditions, list) else []
    raw_blockers = decision.get("blocking_issues")
    blockers = raw_blockers if isinstance(raw_blockers, list) else []
    lines = [
        f"# Lane Decision - {decision.get('lane', '')}",
        "",
        f"- feature: {decision.get('feature', '')}",
        f"- lane: {decision.get('lane', '')}",
        f"- decision: **{decision.get('decision', '')}**",
        f"- blocking_issue_count: {decision.get('blocking_issue_count', 0)}",
        "",
        "## Conditions",
        "",
    ]
    for condition in conditions:
        if not isinstance(condition, Mapping):
            continue
        mark = "PASS" if condition.get("passed") else "FAIL"
        lines.append(
            f"- {mark} `{condition.get('name', '')}` - {condition.get('reason', '')}"
        )
    lines.extend(["", "## Blocking Issues", ""])
    if not blockers:
        lines.append("No P0/P1 review or spec-gap blocking issues.")
    else:
        for issue in blockers:
            if not isinstance(issue, Mapping):
                continue
            lines.append(
                f"- [{issue.get('severity', '')}] {issue.get('id', '')}: "
                f"{issue.get('title', '')} ({issue.get('source', '')})"
            )
    lines.append("")
    return "\n".join(lines)



@dataclass(frozen=True)
class LaneGateCompute:
    """The pure read+compute half of the lane gate (no writes).

    Extracted so ``--dry-run`` (ticket 04 / ADR-0004) can report the would-be
    decision without writing ``lane-decision.{json,md}``. The writer
    (``evaluate_lane_gate``) and the dry-run planner share this one computation
    so they can never diverge.
    """

    conditions: list[dict[str, Any]]
    blocking_issues: list[dict[str, Any]]
    decision: dict[str, Any]


def compute_lane_decision(
    repo_root: Path, feature_id: str, lane_id: str
) -> LaneGateCompute:
    """Run the §18.4 precondition + condition compute, no writes.

    Reads the lane's implement-result / verification-report / issue-bundle,
    enforces the §24.2 structural preconditions, evaluates the five lane-gate
    conditions, and assembles the decision dict. Pure of side effects: it writes
    nothing and appends no audit record. Missing/invalid prerequisites raise
    ``ValueError`` exactly as the writer does.
    """
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id} not found under {repo_root}")
    lane_root = lane_dir(repo_root, feature_id, lane_id)
    if not lane_root.is_dir():
        raise ValueError(f"lane {lane_id} not found under feature {feature_id}")

    implement_result = _require_artifact(lane_root / IMPLEMENT_RESULT_JSON)
    verification_report = _require_artifact(
        lane_root / VERIFICATION_DIR / VERIFICATION_REPORT_JSON
    )
    issue_bundle = _require_artifact(lane_root / ISSUE_BUNDLE_JSON)
    # §24.2 fail-loud: a structurally invalid bundle (valid JSON dict but no
    # ``issues`` list) must not silently yield zero issues and a wrong PASS.
    if not isinstance(issue_bundle.get("issues"), list):
        raise ValueError(
            f"issue bundle {lane_root / ISSUE_BUNDLE_JSON} is structurally "
            "invalid: missing 'issues' list (§24.2 fail-loud)"
        )

    issues = _extract_issues(issue_bundle)
    review_blockers = _blocking_issues(issues, "code_review")
    gap_blockers = _blocking_issues(issues, "spec_gap")
    blocking_issues = [_summarize_issue(issue) for issue in review_blockers + gap_blockers]

    conditions = [
        _proposed_done_condition(implement_result),
        _verification_condition(verification_report),
        _no_blocking_condition(
            "review_no_blocking_issues", "review", review_blockers
        ),
        _no_blocking_condition(
            "spec_gap_no_blocking_issues", "spec-gap", gap_blockers
        ),
        _condition(
            "issue_bundle_generated",
            True,
            f"issue bundle generated with {len(issues)} issue(s)",
        ),
    ]
    decision = _decision_json(feature_id, lane_id, conditions, blocking_issues)
    return LaneGateCompute(
        conditions=conditions, blocking_issues=blocking_issues, decision=decision
    )


def evaluate_lane_gate(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    *,
    origin: str | None = None,
) -> LaneDecisionResult:
    """Evaluate §18.4 for one lane and write ``lane-decision.{json,md}``.

    Missing or invalid prerequisite artifacts fail loud (§24.2) before any decision
    product is written. A normal gate failure (e.g. P1 issue or failed verifier)
    still writes the decision artifacts and returns a FAIL result.
    """
    feature_root = feature_dir(repo_root, feature_id)
    # v0.4 ticket 02: the gate evaluation's wall-clock duration lands on the
    # ``lane_gate`` event (``elapsed_ms``). Captured around the deterministic
    # evaluation so the log answers "how long did the lane gate take".
    gate_started = utc_now_iso()
    compute = compute_lane_decision(repo_root, feature_id, lane_id)
    decision = compute.decision
    conditions = compute.conditions
    blocking_issues = compute.blocking_issues

    lane_root = lane_dir(repo_root, feature_id, lane_id)
    decision_json_path = lane_root / LANE_DECISION_JSON
    decision_md_path = lane_root / LANE_DECISION_MD
    write_json(decision_json_path, decision)
    decision_md_path.write_text(_decision_md(decision))

    # v0.7 (ADR-0009 D4): record the lane's gate_verdict in lane-status.yml
    # so the feature-level `declared_lane_ids` / `aggregate_lane_gate_states`
    # helpers and any future CLI status commands can surface per-lane gate
    # state without reading every lane's lane-decision.json. The actual
    # dependency precheck reads lane-decision.json directly (the source of
    # truth for the gate verdict); lane-status is the runtime mirror.
    # The lane gate is the sole writer of this field (mirrors how
    # record_coherence_verdict is the sole writer of the feature-level
    # verdict).
    update_lane_status(
        feature_root,
        lane_id=lane_id,
        gate_verdict=str(decision["decision"]),
        origin=origin,
    )

    result = LaneDecisionResult(
        feature_id=feature_id,
        lane_id=lane_id,
        decision=str(decision["decision"]),
        conditions=conditions,
        blocking_issues=blocking_issues,
        decision_md_path=decision_md_path,
        decision_json_path=decision_json_path,
    )
    append_audit_event(
        feature_root,
        _LANE_GATE_EVENT,
        payload={
            "feature": feature_id,
            "lane": lane_id,
            "decision": decision["decision"],
            "failed_conditions": result.failed_conditions,
            "blocking_issue_count": len(blocking_issues),
            "elapsed_ms": elapsed_ms_between(gate_started, utc_now_iso()),
        },
        origin=origin,
    )
    return result
