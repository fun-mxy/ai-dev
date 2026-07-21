"""Fix-loop orchestration (ADR-0002 D5/D8/D9, v0.3 ticket 07).

``ai-dev fix-run <FEATURE> <LANE>`` is the bounded automatic bookend after
Human Triage records one or more ``request_fix`` dispositions. It is deliberately
an orchestration layer, not a pure writer: it runs one Implementer pass, then the
checking/verifier/collector bookend, and stops before the mandatory human
re-triage step. The deterministic state it owns is small and explicit:

* feature-level ``fix_loop_budget.used`` increments exactly once, and only after
  the implement leg produced a §14-validated implement result;
* every issue whose current triage is ``request_fix`` is stamped with
  ``fix_targeted_in_run`` for that validated implement run, so the collector can
  turn still-present fingerprints into ``status=reappeared`` and wipe active
  triage.

Crashes, precondition failures, and failed §14 validation return/raise before the
budget increment and before issue targeting, so they do not consume the one-round
budget (ADR-0002 D9).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from ai_dev.audit import append_audit_event
from ai_dev.checking_legs import CheckingLegResult, run_reviewer_leg, run_spec_gap_leg
from ai_dev.implement_leg import ImplementerLegResult, run_implementer_leg
from ai_dev.issue_bundle import ISSUES_DIR, IssueBundleResult, collect_issue_bundle
from ai_dev.json_artifact import read_json_object, write_json
from ai_dev.paths import feature_dir
from ai_dev.profiles import AgentProfile
from ai_dev.shell_verifier import VerifierResult, run_verifier
from ai_dev.status import fix_loop_budget_exhausted, increment_fix_loop_budget
from ai_dev.validate import ValidationResult

_REQUEST_FIX = "request_fix"
_FIX_TARGETED_EVENT = "fix_targeted_issue"
_FIX_RUN_EVENT = "fix_run"

ImplementLeg = Callable[..., ImplementerLegResult]
CheckingLeg = Callable[..., CheckingLegResult]
VerifierLeg = Callable[..., VerifierResult]
CollectorLeg = Callable[..., IssueBundleResult]


@dataclass(frozen=True)
class FixRunResult:
    """Summary of one fix-loop driver invocation."""

    feature_id: str
    lane_id: str
    implement_run_id: str
    target_issue_ids: list[str]
    budget_used: int
    budget_max: int
    implement: ImplementerLegResult
    review: CheckingLegResult
    spec_gap: CheckingLegResult
    verification: VerifierResult
    collection: IssueBundleResult


@dataclass(frozen=True)
class _IssueTarget:
    issue_id: str
    path: Path
    issue: Mapping[str, Any]


def _current_request_fix_targets(feature_root: Path) -> list[_IssueTarget]:
    """Return issues whose active triage is ``request_fix``.

    ``issues/`` is the persisted source of truth (ADR-0002 D1). Reappeared issues
    have ``triage: null`` and therefore are not selected; after the one budgeted
    fix run, they must be re-triaged by a human instead of automatically fixed
    again.
    """
    issue_root = feature_root / ISSUES_DIR
    if not issue_root.is_dir():
        raise ValueError(f"issues/ missing at {issue_root} (§6 broken feature run)")
    targets: list[_IssueTarget] = []
    for path in sorted(issue_root.glob("ISSUE-*.json")):
        issue = read_json_object(path)
        if issue is None:
            continue
        triage = issue.get("triage")
        if not isinstance(triage, dict):
            continue
        if triage.get("action") != _REQUEST_FIX:
            continue
        issue_id = issue.get("id")
        if not isinstance(issue_id, str) or not issue_id:
            continue
        targets.append(_IssueTarget(issue_id=issue_id, path=path, issue=issue))
    return targets


def _fix_task_context(targets: list[_IssueTarget]) -> str:
    """Render human-triaged request-fix issues into the implement task package."""
    lines = [
        "## Fix Run Context (ADR-0002 D8)",
        "",
        "This is a bounded fix implement run for the active `request_fix` issues below.",
        "Address these human-triaged issues and their recorded reasons in this run; ",
        "do not attempt a second automatic fix loop.",
        "",
    ]
    for target in targets:
        issue = target.issue
        triage = issue.get("triage") if isinstance(issue.get("triage"), Mapping) else {}
        reason = triage.get("reason") if isinstance(triage, Mapping) else None
        lines.extend(
            [
                f"### {target.issue_id}: {issue.get('title', '')}",
                f"- severity: {issue.get('severity', '')}",
                f"- source: {issue.get('source', '')}",
                f"- triage_reason: {reason or ''}",
                f"- recommendation: {issue.get('recommendation', '')}",
                f"- evidence: {issue.get('evidence', [])}",
                "",
                str(issue.get("description", "")),
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def _mark_fix_targets(
    feature_root: Path,
    feature_id: str,
    targets: list[_IssueTarget],
    run_id: str,
) -> None:
    """Stamp targeted issues with the validated implement run id and audit it."""
    for target in targets:
        issue = read_json_object(target.path)
        if issue is None:
            raise ValueError(f"issue {target.issue_id} disappeared at {target.path}")
        issue["fix_targeted_in_run"] = run_id
        write_json(target.path, issue)
        append_audit_event(
            feature_root,
            _FIX_TARGETED_EVENT,
            payload={
                "feature": feature_id,
                "issue": target.issue_id,
                "run": run_id,
            },
        )


def _require_validation_passed(label: str, run_id: str, validation: ValidationResult) -> None:
    if validation.passed:
        return
    failed = validation.failed_check or "validation"
    raise ValueError(
        f"fix-run stopped after {label} run {run_id}: §14 validation failed "
        f"({failed}, {len(validation.issues)} problem(s)); budget is consumed only "
        "for a validated implement result and no automatic relaunch is attempted"
    )


def run_fix_run(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    profile: AgentProfile,
    *,
    max_turns: int,
    permission_mode: str,
    verify_timeout: float,
    implement_leg: ImplementLeg = run_implementer_leg,
    reviewer_leg: CheckingLeg = run_reviewer_leg,
    spec_gap_leg: CheckingLeg = run_spec_gap_leg,
    verifier_leg: VerifierLeg = run_verifier,
    collector_leg: CollectorLeg = collect_issue_bundle,
) -> FixRunResult:
    """Run one bounded fix loop and stop before human re-triage.

    The preflight refuses when there are no active ``request_fix`` issues or when
    ``fix_loop_budget`` is already exhausted. Budget/target writes happen only
    after the implement validation passes; checking/verification/collection are
    then run once as the ADR-0002 D8 bookend.
    """
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id} not found under {repo_root}")
    if fix_loop_budget_exhausted(feature_root):
        raise ValueError(
            "fix_loop_budget exhausted; cannot run another request_fix loop "
            "(ADR-0002 D5)"
        )
    targets = _current_request_fix_targets(feature_root)
    if not targets:
        raise ValueError(
            f"no active request_fix issues found under {feature_id}/{ISSUES_DIR}; "
            "fix-run has nothing to target"
        )

    implement = implement_leg(
        repo_root,
        feature_id,
        lane_id,
        profile,
        max_turns=max_turns,
        permission_mode=permission_mode,
        task_context_append=_fix_task_context(targets),
    )
    _require_validation_passed("implement", implement.run_id, implement.validation)
    budget = increment_fix_loop_budget(feature_root, implement.run_id)
    _mark_fix_targets(feature_root, feature_id, targets, implement.run_id)

    review = reviewer_leg(
        repo_root,
        feature_id,
        lane_id,
        profile,
        max_turns=max_turns,
        permission_mode=permission_mode,
    )
    _require_validation_passed("review", review.run_id, review.validation)

    spec_gap = spec_gap_leg(
        repo_root,
        feature_id,
        lane_id,
        profile,
        max_turns=max_turns,
        permission_mode=permission_mode,
    )
    _require_validation_passed("spec-gap", spec_gap.run_id, spec_gap.validation)

    verification = verifier_leg(repo_root, feature_id, lane_id, timeout=verify_timeout)
    collection = collector_leg(repo_root, feature_id, lane_id)

    target_ids = [target.issue_id for target in targets]
    append_audit_event(
        feature_root,
        _FIX_RUN_EVENT,
        payload={
            "feature": feature_id,
            "lane": lane_id,
            "implement_run": implement.run_id,
            "target_issue_ids": target_ids,
            "verification_verdict": verification.verdict,
            "collected_issue_ids": collection.issue_ids,
        },
    )
    return FixRunResult(
        feature_id=feature_id,
        lane_id=lane_id,
        implement_run_id=implement.run_id,
        target_issue_ids=target_ids,
        budget_used=budget["used"],
        budget_max=budget["max"],
        implement=implement,
        review=review,
        spec_gap=spec_gap,
        verification=verification,
        collection=collection,
    )
