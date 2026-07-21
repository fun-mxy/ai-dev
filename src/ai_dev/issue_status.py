"""Issue ``status`` lifecycle state machine (ADR-0002 D2, ticket 03).

``issues/ISSUE-NNN.json`` carries a ``status`` field whose value is one of four
lifecycle states -- ``raised | triaged | resolved | reappeared`` -- driven by
the collector, ``apply_triage``, and the fix-run driver. The lane gate does
**not** read ``status`` (it reads ``severity`` + ``triage``); ``status`` is
lifecycle bookkeeping that lets the collector/driver detect resolution and the
mandatory re-triage trigger (a ``reappeared`` issue surfaces as ``triage is
None`` -> gate FAIL).

This module owns the *transition validator*: a closed directed graph of legal
``(current -> target)`` edges. Every writer fires its own transition through
``transition_issue_status``, so an illegal jump fails loud (§24.2) rather than
silently corrupting the lifecycle. The legal edges (ADR-0002 D2), including the
idempotent self-loops a re-collect needs and the collector's re-raise of a
previously-resolved fingerprint::

    (none)     --> raised          # collector: brand-new fingerprint
    raised     --> raised          # re-collect: still untriaged (re-reported)
    raised     --> triaged         # apply_triage: disposition written
    raised     --> resolved        # collector: fingerprint gone (D6)
    triaged    --> triaged         # re-collect: still present, not fix-targeted
    triaged    --> reappeared      # collector/driver: request_fix + fix run + still present
    triaged    --> resolved        # collector: fingerprint gone (D6)
    reappeared --> reappeared      # re-collect: still present, not re-triaged
    reappeared --> triaged         # apply_triage: re-triage
    reappeared --> resolved        # collector: fingerprint gone (D6 - any disappeared fingerprint)
    resolved   --> raised          # collector: a resolved fingerprint re-reported (re-raise)

``resolved`` is otherwise terminal in v0.3 -- its only outgoing edge is the
re-raise. ``apply_triage`` (ticket 05) and the fix-run driver (ticket 07) call
``transition_issue_status`` for the transitions they own; the collector (ticket
03) calls it for ``raised`` / ``resolved`` / ``reappeared``.
"""

from __future__ import annotations

STATUS_RAISED = "raised"
STATUS_TRIAGED = "triaged"
STATUS_RESOLVED = "resolved"
STATUS_REAPPEARED = "reappeared"

ISSUE_STATUSES: tuple[str, ...] = (
    STATUS_RAISED,
    STATUS_TRIAGED,
    STATUS_RESOLVED,
    STATUS_REAPPEARED,
)

# Legal ``(current -> target)`` edges. ``None`` is the pre-status state of a
# brand-new issue; the only legal first status is ``raised``. Self-loops are the
# idempotent re-collect case (re-reported, no lifecycle change) so a no-op
# re-collect does not trip the fail-loud guard.
_LEGAL_TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({STATUS_RAISED}),
    STATUS_RAISED: frozenset({STATUS_RAISED, STATUS_TRIAGED, STATUS_RESOLVED}),
    STATUS_TRIAGED: frozenset({STATUS_TRIAGED, STATUS_REAPPEARED, STATUS_RESOLVED}),
    STATUS_REAPPEARED: frozenset({STATUS_REAPPEARED, STATUS_TRIAGED, STATUS_RESOLVED}),
    STATUS_RESOLVED: frozenset({STATUS_RAISED}),
}


def transition_issue_status(current: str | None, target: str) -> str:
    """Validate and apply one lifecycle transition, returning ``target``.

    Raises ``ValueError`` if ``target`` is not a known status, if ``current`` is
    neither ``None`` nor a known status, or if ``(current, target)`` is not a
    legal edge. Writers call this so an illegal jump fails loud (§24.2) instead
    of silently corrupting the issue lifecycle.
    """
    if target not in ISSUE_STATUSES:
        raise ValueError(
            f"unknown issue status {target!r}; expected one of {ISSUE_STATUSES}"
        )
    if current is not None and current not in ISSUE_STATUSES:
        raise ValueError(
            f"unknown issue status {current!r}; expected one of {ISSUE_STATUSES}"
        )
    legal = _LEGAL_TRANSITIONS.get(current, frozenset())
    if target not in legal:
        raise ValueError(
            f"illegal issue status transition {current!r} -> {target!r}; "
            f"legal targets from {current!r}: {sorted(legal) or '<none>'}"
        )
    return target


def initial_issue_status() -> str:
    """The status of a brand-new issue: ``raised``.

    Convenience wrapper around ``transition_issue_status(None, STATUS_RAISED)``
    so call sites read as "a new issue starts raised" rather than spelling the
    ``None``-from edge. Validates the edge on every call.
    """
    return transition_issue_status(None, STATUS_RAISED)
