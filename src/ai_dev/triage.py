"""``apply_triage`` - the deterministic triage write chokepoint (ADR-0001 #8).

v0.3's Human-Triage write path. A pure, model-free function that takes one
Human-Triage disposition for an ``ISSUE-NNN`` and writes it as the ``triage``
state object on ``issues/ISSUE-NNN.json`` - **not** a standalone artifact,
**not** the lane bundle (a collector projection), **not** a Decision
(ADR-0001 #2). It is the single place that enforces:

* the disposition x severity **legality matrix** (ADR-0001 #4) - ``defer`` /
  ``accept`` are non-gate bookkeeping legal only on P2/P3; ``override`` on P0
  is forbidden entirely;
* the **reason-presence** rule for disarming dispositions (ADR-0001 #6) -
  ``override`` and ``reject`` on a blocking severity carry a reason, else they
  are a Decision-free escape hatch and are refused;
* the **promotion rule** (ADR-0001 #3) - a ``DEC-NNN`` is minted iff the
  disposition disarms a blocking issue (``override`` x P1, ``reject`` x
  {P0, P1}); a 2D matrix lookup, mechanically decidable at write time;
* the P0 ``override`` **write-layer refusal** (ADR-0001 #7) - the first of two
  defenses (the lane gate, ticket 06, is the second);
* the ``request_change_proposal`` **clean deferral** (ADR-0002 #7) - v0.3 has
  no CP lifecycle, so the disposition is recorded but no ``CP-NNN`` is minted.

It drives the issue lifecycle ``status`` to ``triaged`` through the ticket-03
helper so an illegal jump (e.g. triaging a ``resolved`` issue) fails loud
(ADR-0002 D2), and appends any prior disposition into ``triage_history`` so a
re-triage does not silently overwrite the previous verdict. Every apply -
success or refusal - is audited. Models may *propose* triage; only this
human-triggered deterministic command writes canonical ``triage``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ai_dev.audit import append_audit_event
from ai_dev.feature_ids import allocate_id
from ai_dev.issue_bundle import ISSUES_DIR
from ai_dev.issue_status import STATUS_TRIAGED, transition_issue_status
from ai_dev.json_artifact import read_json_object, write_json
from ai_dev.paths import feature_dir
from ai_dev.timeutil import utc_now_iso

DECISIONS_DIR = "decisions"

# §16 dispositions, renamed per ADR-0001 #1 / glossary "Disposition". The spec's
# ``accept_issue`` / ``override_issue`` / ... long forms collapse to these short
# forms, which is what the collector already reads (``request_fix``) and what
# the glossary pins as the canonical value domain.
ACCEPT = "accept"
REJECT = "reject"
DEFER = "defer"
OVERRIDE = "override"
REQUEST_FIX = "request_fix"
REQUEST_CHANGE_PROPOSAL = "request_change_proposal"

DISPOSITIONS: tuple[str, ...] = (
    ACCEPT,
    DEFER,
    OVERRIDE,
    REJECT,
    REQUEST_FIX,
    REQUEST_CHANGE_PROPOSAL,
)

SEVERITY_P0 = "P0"
SEVERITY_P1 = "P1"
SEVERITY_P2 = "P2"
SEVERITY_P3 = "P3"
SEVERITIES: tuple[str, ...] = (SEVERITY_P0, SEVERITY_P1, SEVERITY_P2, SEVERITY_P3)
_BLOCKING_SEVERITIES = frozenset({SEVERITY_P0, SEVERITY_P1})

_TRIAGE_EVENT = "triage"
_TRIAGE_REFUSED_EVENT = "triage_refused"


class TriageRefusedError(Exception):
    """Raised when a triage disposition is refused at the write layer.

    ADR-0001 #7: an illegal (disposition x severity) cell, or a disarming
    disposition missing its required reason, is refused - the triage is not
    written, the issue stays untriaged, and a ``triage_refused`` audit event
    records the attempt. This is illegal-input refusal, not a §24.2 runtime
    failure (subprocess crash / schema violation), so the CLI surfaces it as a
    clean ``error:`` line + non-zero exit rather than a traceback.
    """


@dataclass(frozen=True)
class TriageResult:
    """Summary of one successful deterministic triage apply."""

    feature_id: str
    issue_id: str
    action: str
    severity: str
    decision_ids: list[str]
    issue_path: Path
    timestamp: str


def _reason_missing(reason: str | None) -> bool:
    """True when ``reason`` carries no non-whitespace text."""
    return reason is None or not reason.strip()


@dataclass(frozen=True)
class _MatrixCell:
    """The ADR-0001 #3/#4/#5/#6 verdict for one (disposition x severity) cell.

    * ``legal`` is ``False`` for the refused cells - ``override`` x P0 (ADR-0001
      #5, forbidden entirely) and ``defer`` / ``accept`` x {P0, P1} (#4, illegal
      non-gate bookkeeping).
    * ``disarms`` is ``True`` for the cells that disarm a blocking issue -
      ``override`` x P1 and ``reject`` x {P0, P1}. These require a reason
      (ADR-0001 #6) and mint a ``DEC-NNN`` (ADR-0001 #3); the two are the same
      predicate - a disposition disarms iff it requires a reason iff it promotes.
    * ``decision_kind`` is set only when ``disarms``; ``refusal_reason`` only
      when not ``legal``.
    """

    legal: bool
    disarms: bool
    decision_kind: str | None
    refusal_reason: str | None


def _matrix_cell(action: str, severity: str) -> _MatrixCell:
    """The single source of truth for the (disposition x severity) matrix.

    One place switches on ``(action, severity)``; every question ``apply_triage``
    asks - legal? disarms (reason + promotion)? what DEC kind? what refusal
    reason? - is answered by this one lookup, so adding a disposition edits one
    table rather than four parallel if-cascades.
    """
    # Refused cells (ADR-0001 #4/#5).
    if action == OVERRIDE and severity == SEVERITY_P0:
        return _MatrixCell(
            legal=False,
            disarms=False,
            decision_kind=None,
            refusal_reason=(
                "P0 cannot be waived by override (ADR-0001 #5); only reject can "
                "disarm a P0, with a recorded Decision"
            ),
        )
    if action in {DEFER, ACCEPT} and severity in _BLOCKING_SEVERITIES:
        return _MatrixCell(
            legal=False,
            disarms=False,
            decision_kind=None,
            refusal_reason=(
                f"{action} is illegal on {severity} (ADR-0001 #4); defer/accept "
                "are non-gate bookkeeping for P2/P3 only"
            ),
        )
    # Disarming cells (ADR-0001 #3/#6) - legal, require a reason, mint a DEC.
    if action == OVERRIDE and severity == SEVERITY_P1:
        return _MatrixCell(
            legal=True, disarms=True, decision_kind="p1_override", refusal_reason=None
        )
    if action == REJECT and severity == SEVERITY_P0:
        return _MatrixCell(
            legal=True, disarms=True, decision_kind="p0_reject", refusal_reason=None
        )
    if action == REJECT and severity == SEVERITY_P1:
        return _MatrixCell(
            legal=True, disarms=True, decision_kind="p1_reject", refusal_reason=None
        )
    # Everything else: legal, non-disarming - ``request_fix`` /
    # ``request_change_proposal`` on any severity, and ``override`` / ``reject``
    # / ``defer`` / ``accept`` on P2/P3 (override on P2/P3 is the matrix's
    # "n/a" no-op: legal, no gate effect, no DEC).
    return _MatrixCell(
        legal=True, disarms=False, decision_kind=None, refusal_reason=None
    )


def _audit_refusal(
    feature_root: Path,
    feature_id: str,
    issue_id: str,
    action: str,
    severity: str,
    by: str,
    refusal_reason: str,
    *,
    timestamp: str,
) -> None:
    """Record a ``triage_refused`` audit event for a refused apply attempt.

    Carries ``by`` so the log can answer "who attempted this triage" without
    reading the issue file (the issue is untouched on refusal).
    """
    append_audit_event(
        feature_root,
        _TRIAGE_REFUSED_EVENT,
        payload={
            "feature": feature_id,
            "issue": issue_id,
            "action": action,
            "severity": severity,
            "by": by,
            "refusal_reason": refusal_reason,
        },
        timestamp=timestamp,
    )


def _preserve_prior_triage(issue: dict[str, Any]) -> None:
    """Append the issue's current ``triage`` into ``triage_history`` before a
    re-triage overwrites it (ADR-0002 D2: "write current, append prior if any").

    No-op when there is no current triage - a fresh ``raised`` issue, or a
    ``reappeared`` issue whose triage the collector already wiped to history.
    """
    triage = issue.get("triage")
    if triage is None:
        return
    history = issue.get("triage_history")
    if not isinstance(history, list):
        history = []
    history.append(triage)
    issue["triage_history"] = history


def _decision_md(decision: Mapping[str, Any]) -> str:
    lines = [
        f"# Decision {decision.get('id', '')} - {decision.get('title', '')}",
        "",
        f"- id: {decision.get('id', '')}",
        f"- kind: {decision.get('kind', '')}",
        f"- triggered_by_issue: {decision.get('triggered_by_issue', '')}",
        f"- status: {decision.get('status', '')}",
        f"- by: {decision.get('by', '')}",
        f"- ts: {decision.get('ts', '')}",
        "",
        "## Rationale",
        "",
        str(decision.get("rationale", "")),
        "",
    ]
    return "\n".join(lines)


def _write_decision(
    feature_root: Path,
    issue: Mapping[str, Any],
    issue_id: str,
    kind: str,
    rationale: str,
    by: str,
    *,
    timestamp: str,
) -> str:
    """Allocate ``DEC-NNN`` and write ``decisions/DEC-NNN.{json,md}``.

    The Decision is the rationale-bearing cross-cutting artifact that records
    *why* a blocking issue was disarmed (ADR-0001 #3), linked back to the issue
    via ``triggered_by_issue``. The id is minted by the deterministic stable-id
    allocator (no model); the §4.4 double product gets a markdown rendering.
    """
    dec_id = allocate_id(feature_root, "DEC", timestamp=timestamp)
    payload: dict[str, Any] = {
        "id": dec_id,
        "kind": kind,
        "title": f"{kind} - {issue.get('title', '')}",
        "rationale": rationale,
        "triggered_by_issue": issue_id,
        "status": "accepted",
        "by": by,
        "ts": timestamp,
    }
    decisions_root = feature_root / DECISIONS_DIR
    write_json(decisions_root / f"{dec_id}.json", payload)
    (decisions_root / f"{dec_id}.md").write_text(_decision_md(payload))
    return dec_id


def apply_triage(
    repo_root: Path,
    feature_id: str,
    issue_id: str,
    action: str,
    reason: str | None,
    by: str,
    *,
    timestamp: str | None = None,
) -> TriageResult:
    """Apply one Human-Triage disposition to ``issue_id`` and return the result.

    Writes the ``triage`` state object onto ``issues/ISSUE-NNN.json`` (ADR-0001
    #2), enforces the legality matrix + reason-presence + promotion in one
    place, drives the lifecycle ``status`` to ``triaged`` through the ticket-03
    helper, and audits the apply. Refused dispositions raise
    ``TriageRefusedError`` *after* auditing a ``triage_refused`` event and
    *before* mutating the issue (two-layer defense layer 1: the issue stays
    untriaged).

    ``timestamp`` is injectable for deterministic replay (audit records, the
    triage ``ts``, and the DEC ``ts`` all share it).
    """
    if action not in DISPOSITIONS:
        raise ValueError(
            f"unknown disposition {action!r}; expected one of {DISPOSITIONS}"
        )

    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id} not found under {repo_root}")
    issue_path = feature_root / ISSUES_DIR / f"{issue_id}.json"
    issue = read_json_object(issue_path)
    if issue is None:
        raise ValueError(
            f"issue {issue_id} not found under {feature_id}/{ISSUES_DIR} (§24.2)"
        )

    severity = issue.get("severity")
    if severity not in SEVERITIES:
        raise ValueError(
            f"issue {issue_id} has unknown severity {severity!r}; "
            f"expected one of {SEVERITIES}"
        )

    ts = timestamp if timestamp is not None else utc_now_iso()
    cell = _matrix_cell(action, severity)

    # Legality matrix (ADR-0001 #4) - refuse illegal cells before writing.
    if not cell.legal:
        _audit_refusal(
            feature_root, feature_id, issue_id, action, severity, by,
            cell.refusal_reason or "", timestamp=ts,
        )
        raise TriageRefusedError(
            f"triage refused for {issue_id}: {cell.refusal_reason}"
        )

    # Reason-presence for disarming dispositions (ADR-0001 #6): a disarming
    # disposition without a reason is a Decision-free escape hatch.
    if cell.disarms and _reason_missing(reason):
        msg = (
            f"{action} on {severity} requires a reason (ADR-0001 #6); without "
            "one it is a Decision-free escape hatch"
        )
        _audit_refusal(
            feature_root, feature_id, issue_id, action, severity, by, msg,
            timestamp=ts,
        )
        raise TriageRefusedError(f"triage refused for {issue_id}: {msg}")

    # Promotion (ADR-0001 #3): mint a DEC iff the disposition disarms a blocker.
    decision_ids: list[str] = []
    if cell.disarms:
        decision_ids.append(
            _write_decision(
                feature_root,
                issue,
                issue_id,
                cell.decision_kind or "",
                reason if reason is not None else "",
                by,
                timestamp=ts,
            )
        )

    # The triage state object (ADR-0001 reference data model). ``reason`` is
    # always present (null when none recorded); ``decision_ids`` is present
    # only when promotion minted one.
    triage: dict[str, Any] = {
        "action": action,
        "reason": reason if reason is not None and reason.strip() else None,
        "by": by,
        "ts": ts,
    }
    if decision_ids:
        triage["decision_ids"] = decision_ids

    # Preserve any prior triage to history before overwriting (ADR-0002 D2),
    # then drive the lifecycle through the ticket-03 helper so an illegal jump
    # (e.g. triaging a resolved issue) fails loud.
    _preserve_prior_triage(issue)
    issue["triage"] = triage
    issue["status"] = transition_issue_status(issue.get("status"), STATUS_TRIAGED)
    write_json(issue_path, issue)

    append_audit_event(
        feature_root,
        _TRIAGE_EVENT,
        payload={
            "feature": feature_id,
            "issue": issue_id,
            "action": action,
            "severity": severity,
            "by": by,
            "decision_ids": decision_ids,
        },
        timestamp=ts,
    )

    return TriageResult(
        feature_id=feature_id,
        issue_id=issue_id,
        action=action,
        severity=severity,
        decision_ids=decision_ids,
        issue_path=issue_path,
        timestamp=ts,
    )
