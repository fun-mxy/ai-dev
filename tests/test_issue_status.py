"""Issue ``status`` state machine (ADR-0002 D2, ticket 03).

The state machine is a closed directed graph of legal ``(current -> target)``
edges. Every writer fires its transition through ``transition_issue_status``;
an illegal jump fails loud (§24.2) rather than silently corrupting the
lifecycle. These tests pin every legal edge and a representative slice of the
illegal ones, plus the unknown-status and initial-state seams.
"""

from __future__ import annotations

import pytest

from ai_dev.issue_status import (
    ISSUE_STATUSES,
    STATUS_RAISED,
    STATUS_REAPPEARED,
    STATUS_RESOLVED,
    STATUS_TRIAGED,
    initial_issue_status,
    transition_issue_status,
)

# Every legal edge in the state machine, as ``(current, target)``. ``None`` is
# the pre-status state of a brand-new issue. Mirrors the table in the
# ``issue_status`` module docstring (ADR-0002 D2 + idempotent self-loops + the
# collector's resolved-re-raise).
_LEGAL_EDGES: list[tuple[str | None, str]] = [
    (None, STATUS_RAISED),
    (STATUS_RAISED, STATUS_RAISED),
    (STATUS_RAISED, STATUS_TRIAGED),
    (STATUS_RAISED, STATUS_RESOLVED),
    (STATUS_TRIAGED, STATUS_TRIAGED),
    (STATUS_TRIAGED, STATUS_REAPPEARED),
    (STATUS_TRIAGED, STATUS_RESOLVED),
    (STATUS_REAPPEARED, STATUS_REAPPEARED),
    (STATUS_REAPPEARED, STATUS_TRIAGED),
    (STATUS_REAPPEARED, STATUS_RESOLVED),
    (STATUS_RESOLVED, STATUS_RAISED),
]


def _all_edges() -> list[tuple[str | None, str]]:
    """Every ``(current, target)`` cell of the status x status table, including
    the ``None``-from row, so the illegal-edge coverage is exhaustive rather
    than a hand-picked slice."""
    rows: list[str | None] = [None, *ISSUE_STATUSES]
    return [(current, target) for current in rows for target in ISSUE_STATUSES]


class TestTransitionIssueStatus:
    """``transition_issue_status`` validates + applies one lifecycle edge."""

    @pytest.mark.parametrize(("current", "target"), _LEGAL_EDGES)
    def test_legal_transition_returns_target(self, current: str | None, target: str) -> None:
        assert transition_issue_status(current, target) == target

    def test_all_illegal_edges_raise(self) -> None:
        legal = set(_LEGAL_EDGES)
        illegal = [edge for edge in _all_edges() if edge not in legal]
        # Sanity: the illegal set is non-empty and the legal set is a strict
        # subset of the full table (guards against a typo flipping the sense).
        assert illegal
        assert legal < set(_all_edges())
        for current, target in illegal:
            with pytest.raises(ValueError, match="illegal issue status transition"):
                transition_issue_status(current, target)

    @pytest.mark.parametrize("bad_target", ["", "closed", "RAISED", "re-appeared", "triage"])
    def test_unknown_target_raises(self, bad_target: str) -> None:
        with pytest.raises(ValueError, match="unknown issue status"):
            transition_issue_status(STATUS_RAISED, bad_target)

    @pytest.mark.parametrize("bad_current", ["closed", "RAISED", "pending"])
    def test_unknown_current_raises(self, bad_current: str) -> None:
        with pytest.raises(ValueError, match="unknown issue status"):
            transition_issue_status(bad_current, STATUS_TRIAGED)

    def test_illegal_transition_message_names_legal_targets(self) -> None:
        # The fail-loud message must say what *was* legal so the operator can
        # see the typo/misuse without opening the ADR.
        with pytest.raises(ValueError) as exc_info:
            transition_issue_status(STATUS_RAISED, STATUS_REAPPEARED)
        message = str(exc_info.value)
        assert "raised" in message
        assert "triaged" in message
        assert "resolved" in message
        assert "reappeared" not in message.split("legal targets")[1]

    def test_cannot_skip_triaged_to_reappear(self) -> None:
        # reappeared is only reachable from triaged (a fix run targets a
        # request_fix issue); a raised issue cannot jump straight there.
        with pytest.raises(ValueError):
            transition_issue_status(STATUS_RAISED, STATUS_REAPPEARED)

    def test_resolved_is_terminal_except_reraise(self) -> None:
        # resolved has exactly one outgoing edge: re-raise to raised.
        assert transition_issue_status(STATUS_RESOLVED, STATUS_RAISED) == STATUS_RAISED
        for target in (STATUS_TRIAGED, STATUS_REAPPEARED, STATUS_RESOLVED):
            with pytest.raises(ValueError):
                transition_issue_status(STATUS_RESOLVED, target)

    def test_new_issue_can_only_start_raised(self) -> None:
        # None (brand-new) -> raised is the only entry edge.
        assert transition_issue_status(None, STATUS_RAISED) == STATUS_RAISED
        for target in (STATUS_TRIAGED, STATUS_RESOLVED, STATUS_REAPPEARED):
            with pytest.raises(ValueError):
                transition_issue_status(None, target)


class TestInitialIssueStatus:
    def test_returns_raised(self) -> None:
        assert initial_issue_status() == STATUS_RAISED

    def test_validates_the_none_from_edge(self) -> None:
        # If the entry edge were ever removed from the table, this would raise
        # instead of silently returning a stale constant.
        assert initial_issue_status() in ISSUE_STATUSES
