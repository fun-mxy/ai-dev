"""``apply_triage`` deterministic command (ADR-0001, ticket 05).

The triage write chokepoint: a pure, model-free function that takes a Human
Triage disposition for one ``ISSUE-NNN`` and writes it as the ``triage`` state
object on ``issues/ISSUE-NNN.json`` (ADR-0001 #2 - never a standalone artifact,
never the bundle, never a Decision). It enforces, in one place, the
disposition x severity legality matrix (ADR-0001 #4), the reason-presence rule
for disarming dispositions (ADR-0001 #6), the promotion rule that mints a
``DEC-NNN`` when a blocking issue is disarmed (ADR-0001 #3), the P0 ``override``
write-layer refusal (ADR-0001 #7 - two-layer defense, layer 1), and the
``request_change_proposal`` clean deferral (ADR-0002 #7 - record only, no
``CP-NNN`` lifecycle in v0.3). It drives the issue lifecycle through the
ticket-03 status helper so the ``triaged`` transition fails loud on an illegal
jump (ADR-0002 D2).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ai_dev.audit import AUDIT_LOG_JSON
from ai_dev.cli import main
from ai_dev.feature_run import create_feature_run
from ai_dev.issue_status import STATUS_RAISED, STATUS_TRIAGED
from ai_dev.json_artifact import write_json
from ai_dev.paths import feature_dir
from ai_dev.triage import (
    ACCEPT,
    DECISIONS_DIR,
    DEFER,
    DISPOSITIONS,
    OVERRIDE,
    REJECT,
    REQUEST_CHANGE_PROPOSAL,
    REQUEST_FIX,
    SEVERITIES,
    TriageRefusedError,
    TriageResult,
    apply_triage,
)

from test_implement_leg import _feature_root  # noqa: E402

_ISSUE_ID = "ISSUE-001"
_FIXED_TS = "2026-07-21T09:00:00Z"


def _stage_issue(
    repo_root: Path,
    *,
    severity: str = "P1",
    status: str = STATUS_RAISED,
    issue_id: str = _ISSUE_ID,
    triage: dict[str, Any] | None = None,
    title: str = "answer() has no docstring",
) -> tuple[Path, str, str, Path]:
    """Stand up one issue on disk for ``apply_triage`` to read.

    Creates a real feature run (so ``issues/`` + ``decisions/`` + the audit log
    exist) then writes ``issues/ISSUE-001.json`` directly with the requested
    severity / lifecycle status / prior triage. ``apply_triage`` reads only the
    issue file, so the full collect pipeline is not needed to exercise the
    writer - one integration test below stages a real collected issue instead.
    """
    feature_id = create_feature_run(repo_root, "triage test")
    feature_root = _feature_root(repo_root, feature_id)
    issue: dict[str, Any] = {
        "id": issue_id,
        "source": "code_review",
        "severity": severity,
        "title": title,
        "description": "The answer() function does not document its return value.",
        "evidence": [{"file": "workspace/hello.py", "line": 2}],
        "status": status,
    }
    if triage is not None:
        issue["triage"] = triage
    issue_path = feature_root / "issues" / f"{issue_id}.json"
    write_json(issue_path, issue)
    return feature_root, feature_id, issue_id, issue_path


def _audit_events(feature_root: Path) -> list[dict[str, Any]]:
    log = feature_root / AUDIT_LOG_JSON
    if not log.is_file():
        return []
    return json.loads(log.read_text())


def _last_audit(event: str, feature_root: Path) -> dict[str, Any]:
    for record in reversed(_audit_events(feature_root)):
        if record.get("event") == event:
            return record
    raise AssertionError(f"no {event!r} audit event in {_audit_events(feature_root)}")


class TestApplyTriageWritesDisposition:
    """ADR-0001 #2: the disposition lands as ``triage`` on the issue, nowhere
    else (not the bundle, not a standalone artifact). A successful apply also
    flips the lifecycle status to ``triaged`` (ADR-0002 D2) via the ticket-03
    helper, so the transition is validated rather than free-written."""

    def test_writes_triage_object_and_advances_status(self, repo_root: Path) -> None:
        feature_root, feature_id, issue_id, issue_path = _stage_issue(
            repo_root, severity="P1"
        )

        result = apply_triage(
            repo_root,
            feature_id,
            issue_id,
            OVERRIDE,
            reason="Known limitation acceptable for MVP v0.",
            by="human",
            timestamp=_FIXED_TS,
        )

        assert isinstance(result, TriageResult)
        assert result.action == OVERRIDE
        assert result.severity == "P1"
        assert result.issue_id == issue_id
        issue = json.loads(issue_path.read_text())
        assert issue["status"] == STATUS_TRIAGED
        assert issue["triage"] == {
            "action": OVERRIDE,
            "reason": "Known limitation acceptable for MVP v0.",
            "by": "human",
            "ts": _FIXED_TS,
            "decision_ids": ["DEC-001"],
        }

    def test_does_not_write_to_lane_bundle(self, repo_root: Path) -> None:
        # ADR-0001 #2 + ticket 05: triage lives on ``issues/``, not the bundle.
        # The bundle is a collector projection; apply_triage must not touch it.
        feature_root, feature_id, issue_id, _ = _stage_issue(
            repo_root, severity="P1"
        )

        apply_triage(
            repo_root,
            feature_id,
            issue_id,
            OVERRIDE,
            reason="waive for MVP",
            by="human",
            timestamp=_FIXED_TS,
        )

        # No lane bundle exists at all (we never collected); that is the point -
        # apply_triage neither needs nor writes one.
        bundles = list(feature_root.glob("lanes/*/issue-bundle.json"))
        assert bundles == []

    def test_missing_issue_fails_loud(self, repo_root: Path) -> None:
        feature_id = create_feature_run(repo_root, "triage test")
        # No ISSUE-001.json written.

        with pytest.raises(ValueError, match="ISSUE-999"):
            apply_triage(
                repo_root, feature_id, "ISSUE-999", REQUEST_FIX, None, "human"
            )

    def test_unknown_disposition_fails_loud(self, repo_root: Path) -> None:
        _, feature_id, issue_id, _ = _stage_issue(repo_root, severity="P1")

        with pytest.raises(ValueError, match="unknown disposition"):
            apply_triage(
                repo_root, feature_id, issue_id, "frobnicate", None, "human"
            )


class TestLegalityMatrix:
    """ADR-0001 #4: every (disposition x severity) cell is either a legal
    pass-through or a fail-loud refusal. Full cell coverage - no cell is left
    to chance. Refusals raise ``TriageRefusedError`` (ADR-0001 #7: illegal
    human input is refused, not a runtime crash)."""

    # (disposition, severity, requires_reason) for every LEGAL cell.
    _LEGAL: list[tuple[str, str, bool]] = [
        (REQUEST_FIX, "P0", False),
        (REQUEST_FIX, "P1", False),
        (REQUEST_FIX, "P2", False),
        (REQUEST_FIX, "P3", False),
        (REQUEST_CHANGE_PROPOSAL, "P0", False),
        (REQUEST_CHANGE_PROPOSAL, "P1", False),
        (REQUEST_CHANGE_PROPOSAL, "P2", False),
        (REQUEST_CHANGE_PROPOSAL, "P3", False),
        (OVERRIDE, "P1", True),
        (OVERRIDE, "P2", False),  # matrix: "n/a (already non-blocking)" - legal, no-op
        (OVERRIDE, "P3", False),
        (REJECT, "P0", True),
        (REJECT, "P1", True),
        (REJECT, "P2", False),
        (REJECT, "P3", False),
        (DEFER, "P2", False),
        (DEFER, "P3", False),
        (ACCEPT, "P2", False),
        (ACCEPT, "P3", False),
    ]

    # (disposition, severity) for every ILLEGAL cell (refused at write layer).
    _ILLEGAL: list[tuple[str, str]] = [
        (OVERRIDE, "P0"),  # ADR-0001 #5/#7: P0 override forbidden entirely
        (DEFER, "P0"),
        (DEFER, "P1"),
        (ACCEPT, "P0"),
        (ACCEPT, "P1"),
    ]

    @pytest.mark.parametrize(("action", "severity", "requires_reason"), _LEGAL)
    def test_legal_cell_writes_triage(
        self,
        repo_root: Path,
        action: str,
        severity: str,
        requires_reason: bool,
    ) -> None:
        _, feature_id, issue_id, issue_path = _stage_issue(
            repo_root, severity=severity
        )
        reason = "recorded rationale" if requires_reason else None

        result = apply_triage(
            repo_root, feature_id, issue_id, action, reason, "human",
            timestamp=_FIXED_TS,
        )

        assert result.action == action
        issue = json.loads(issue_path.read_text())
        assert issue["status"] == STATUS_TRIAGED
        assert issue["triage"]["action"] == action
        assert issue["triage"]["by"] == "human"
        assert issue["triage"]["ts"] == _FIXED_TS

    @pytest.mark.parametrize(("action", "severity"), _ILLEGAL)
    def test_illegal_cell_refused_with_audit(
        self, repo_root: Path, action: str, severity: str
    ) -> None:
        feature_root, feature_id, issue_id, issue_path = _stage_issue(
            repo_root, severity=severity
        )
        original = issue_path.read_text()

        with pytest.raises(TriageRefusedError):
            apply_triage(
                repo_root, feature_id, issue_id, action, "any reason", "human",
                timestamp=_FIXED_TS,
            )

        # Two-layer defense layer 1: the issue is untouched (stays untriaged).
        assert issue_path.read_text() == original
        issue = json.loads(issue_path.read_text())
        assert issue["status"] == STATUS_RAISED
        assert "triage" not in issue or issue.get("triage") is None
        # ADR-0001 #7: the refused attempt is audited (not a denied DEC, a log).
        refused = _last_audit("triage_refused", feature_root)
        assert refused["payload"]["issue"] == issue_id
        assert refused["payload"]["action"] == action
        assert refused["payload"]["severity"] == severity
        assert "refusal_reason" in refused["payload"]

    def test_legal_cells_plus_illegal_cells_cover_whole_matrix(self) -> None:
        # Guard against a typo silently dropping or duplicating a cell: the
        # union of the legal and illegal parametrize lists is exactly the
        # 6-disposition x 4-severity matrix (24 cells), with no overlap.
        legal = {(a, s) for a, s, _ in self._LEGAL}
        illegal = set(self._ILLEGAL)
        full = {(a, s) for a in DISPOSITIONS for s in SEVERITIES}
        assert legal | illegal == full
        assert legal.isdisjoint(illegal)


class TestReasonRequirement:
    """ADR-0001 #6: a ``reject`` that disarms a P0/P1, and any ``override``,
    carry a reason - otherwise they are a DEC-free escape hatch and are refused.
    ``reject`` on P2/P3 disarms nothing and needs no reason."""

    @pytest.mark.parametrize(
        ("action", "severity"),
        [(OVERRIDE, "P1"), (REJECT, "P0"), (REJECT, "P1")],
    )
    def test_disarming_without_reason_refused(
        self, repo_root: Path, action: str, severity: str
    ) -> None:
        feature_root, feature_id, issue_id, issue_path = _stage_issue(
            repo_root, severity=severity
        )
        original = issue_path.read_text()

        with pytest.raises(TriageRefusedError, match="reason"):
            apply_triage(
                repo_root, feature_id, issue_id, action, None, "human",
                timestamp=_FIXED_TS,
            )

        assert issue_path.read_text() == original
        refused = _last_audit("triage_refused", feature_root)
        assert "reason" in refused["payload"]["refusal_reason"]

    def test_blank_reason_treated_as_missing(self, repo_root: Path) -> None:
        feature_root, feature_id, issue_id, _ = _stage_issue(repo_root, severity="P1")

        with pytest.raises(TriageRefusedError):
            apply_triage(
                repo_root, feature_id, issue_id, REJECT, "   ", "human",
                timestamp=_FIXED_TS,
            )

    def test_reject_on_p2_needs_no_reason(self, repo_root: Path) -> None:
        _, feature_id, issue_id, issue_path = _stage_issue(repo_root, severity="P2")

        result = apply_triage(
            repo_root, feature_id, issue_id, REJECT, None, "human",
            timestamp=_FIXED_TS,
        )

        issue = json.loads(issue_path.read_text())
        assert issue["triage"]["action"] == REJECT
        # reason is optional on P2/P3 -> stored as null/absent, no DEC.
        assert result.decision_ids == []


class TestPromotionRule:
    """ADR-0001 #3: a Decision is produced iff the disposition disarms a
    blocking issue - ``override`` x P1, or ``reject`` x {P0, P1}. Every other
    disposition records triage but mints no DEC. Promotion is a 2D matrix
    lookup, mechanically decidable at write time."""

    @pytest.mark.parametrize(
        ("action", "severity", "kind"),
        [
            (OVERRIDE, "P1", "p1_override"),
            (REJECT, "P0", "p0_reject"),
            (REJECT, "P1", "p1_reject"),
        ],
    )
    def test_disarming_produces_decision(
        self,
        repo_root: Path,
        action: str,
        severity: str,
        kind: str,
    ) -> None:
        feature_root, feature_id, issue_id, issue_path = _stage_issue(
            repo_root, severity=severity
        )

        result = apply_triage(
            repo_root, feature_id, issue_id, action, "recorded rationale", "human",
            timestamp=_FIXED_TS,
        )

        assert result.decision_ids == ["DEC-001"]
        issue = json.loads(issue_path.read_text())
        assert issue["triage"]["decision_ids"] == ["DEC-001"]
        dec = json.loads((feature_root / DECISIONS_DIR / "DEC-001.json").read_text())
        assert dec == {
            "id": "DEC-001",
            "kind": kind,
            "title": f"{kind} - answer() has no docstring",
            "rationale": "recorded rationale",
            "triggered_by_issue": issue_id,
            "status": "accepted",
            "by": "human",
            "ts": _FIXED_TS,
        }
        # §4.4 double product: the DEC also has a markdown rendering.
        assert (feature_root / DECISIONS_DIR / "DEC-001.md").is_file()

    @pytest.mark.parametrize(
        ("action", "severity"),
        [
            (REQUEST_FIX, "P0"),
            (REQUEST_FIX, "P1"),
            (REQUEST_CHANGE_PROPOSAL, "P0"),
            (REQUEST_CHANGE_PROPOSAL, "P1"),
            (OVERRIDE, "P2"),
            (REJECT, "P2"),
            (REJECT, "P3"),
            (DEFER, "P2"),
            (ACCEPT, "P2"),
        ],
    )
    def test_non_disarming_produces_no_decision(
        self, repo_root: Path, action: str, severity: str
    ) -> None:
        feature_root, feature_id, issue_id, _ = _stage_issue(
            repo_root, severity=severity
        )
        # None of these cells disarm a blocker, so a reason is never required
        # (and never promotes); pass None to assert the no-reason path is legal.
        result = apply_triage(
            repo_root, feature_id, issue_id, action, None, "human",
            timestamp=_FIXED_TS,
        )

        assert result.decision_ids == []
        assert not list((feature_root / DECISIONS_DIR).glob("DEC-*.json"))


class TestP0OverrideRefused:
    """ADR-0001 #5/#7: P0 cannot be waived by ``override`` - it can only be
    disarmed by a recorded Decision (``reject``-as-false-positive). The
    write-layer refusal is the first of two defenses; the lane gate (ticket 06)
    is the second. A refused P0 override writes no triage and no DEC."""

    def test_p0_override_refused_and_audited(self, repo_root: Path) -> None:
        feature_root, feature_id, issue_id, issue_path = _stage_issue(
            repo_root, severity="P0"
        )
        original = issue_path.read_text()

        with pytest.raises(TriageRefusedError, match="P0"):
            apply_triage(
                repo_root, feature_id, issue_id, OVERRIDE, "trying to waive", "human",
                timestamp=_FIXED_TS,
            )

        assert issue_path.read_text() == original
        # No DEC was minted for a refused attempt.
        assert not list((feature_root / DECISIONS_DIR).glob("DEC-*.json"))
        refused = _last_audit("triage_refused", feature_root)
        assert refused["payload"]["action"] == OVERRIDE
        assert refused["payload"]["severity"] == "P0"

    def test_p0_reject_is_the_legitimate_escape(self, repo_root: Path) -> None:
        # P0 x reject is legal and produces a DEC (false-positive denial).
        feature_root, feature_id, issue_id, issue_path = _stage_issue(
            repo_root, severity="P0"
        )

        result = apply_triage(
            repo_root, feature_id, issue_id, REJECT, "false positive", "human",
            timestamp=_FIXED_TS,
        )

        assert result.decision_ids == ["DEC-001"]
        issue = json.loads(issue_path.read_text())
        assert issue["triage"]["action"] == REJECT
        assert issue["status"] == STATUS_TRIAGED


class TestRequestChangeProposalCleanDeferral:
    """ADR-0002 #7: v0.3 has no CP lifecycle. ``request_change_proposal``
    records the disposition (and still blocks P0/P1 at the gate) but mints no
    ``CP-NNN`` - a clean deferral, not a broken stub. CP-NNN creation is v0.4."""

    @pytest.mark.parametrize("severity", ["P0", "P1", "P2", "P3"])
    def test_records_disposition_no_cp_no_dec(
        self, repo_root: Path, severity: str
    ) -> None:
        feature_root, feature_id, issue_id, issue_path = _stage_issue(
            repo_root, severity=severity
        )

        result = apply_triage(
            repo_root, feature_id, issue_id, REQUEST_CHANGE_PROPOSAL,
            "needs a spec change", "human", timestamp=_FIXED_TS,
        )

        issue = json.loads(issue_path.read_text())
        assert issue["triage"]["action"] == REQUEST_CHANGE_PROPOSAL
        assert issue["status"] == STATUS_TRIAGED
        # Clean deferral: no DEC, no CP-NNN.
        assert result.decision_ids == []
        assert not list((feature_root / DECISIONS_DIR).glob("DEC-*.json"))
        assert not list((feature_root / "projections").glob("CP-*.json"))
        assert not list(feature_root.glob("**/CP-*.json"))


class TestStatusTransitionAndHistory:
    """ADR-0002 D2: ``apply_triage`` drives the lifecycle ``status`` through
    the ticket-03 helper (``-> triaged``) and appends any prior disposition
    into ``triage_history`` so a re-triage does not silently overwrite the
    previous verdict."""

    def test_raised_to_triaged(self, repo_root: Path) -> None:
        _, feature_id, issue_id, issue_path = _stage_issue(
            repo_root, severity="P1", status=STATUS_RAISED
        )

        apply_triage(
            repo_root, feature_id, issue_id, OVERRIDE, "waive", "human",
            timestamp=_FIXED_TS,
        )

        assert json.loads(issue_path.read_text())["status"] == STATUS_TRIAGED

    def test_reappear_to_triaged_retriage(self, repo_root: Path) -> None:
        # A reappeared issue (triage wiped to history by the collector) can be
        # re-triaged: reappeared -> triaged is a legal edge.
        _, feature_id, issue_id, issue_path = _stage_issue(
            repo_root, severity="P1", status="reappeared", triage=None,
        )

        apply_triage(
            repo_root, feature_id, issue_id, REJECT, "still a false positive", "human",
            timestamp=_FIXED_TS,
        )

        issue = json.loads(issue_path.read_text())
        assert issue["status"] == STATUS_TRIAGED
        assert issue["triage"]["action"] == REJECT

    def test_retriage_appends_prior_triage_to_history(self, repo_root: Path) -> None:
        # Re-triaging a still-triaged issue (human changes disposition) appends
        # the prior triage into triage_history before overwriting (ADR-0002 D2).
        # Two real apply_triage calls stand up the prior state faithfully - the
        # first mints DEC-001, the second (re-triage) mints DEC-002.
        feature_root, feature_id, issue_id, issue_path = _stage_issue(
            repo_root, severity="P1", status=STATUS_RAISED,
        )
        apply_triage(
            repo_root, feature_id, issue_id, OVERRIDE, "first attempt", "human",
            timestamp="2026-07-21T08:00:00Z",
        )
        prior = json.loads(issue_path.read_text())["triage"]
        assert prior["decision_ids"] == ["DEC-001"]

        apply_triage(
            repo_root, feature_id, issue_id, REJECT, "reconsidered: false positive",
            "human", timestamp=_FIXED_TS,
        )

        issue = json.loads(issue_path.read_text())
        assert issue["status"] == STATUS_TRIAGED
        assert issue["triage"]["action"] == REJECT
        assert issue["triage_history"] == [prior]
        # The new disarming disposition mints its own DEC.
        assert issue["triage"]["decision_ids"] == ["DEC-002"]

    def test_triaging_resolved_issue_fails_loud(self, repo_root: Path) -> None:
        # resolved is terminal except re-raise; triage is not a legal edge from
        # it, so the ticket-03 helper refuses the transition (§24.2).
        _, feature_id, issue_id, _ = _stage_issue(
            repo_root, severity="P1", status="resolved"
        )

        with pytest.raises(ValueError, match="illegal issue status transition"):
            apply_triage(
                repo_root, feature_id, issue_id, REQUEST_FIX, None, "human",
                timestamp=_FIXED_TS,
            )


class TestAuditEvents:
    """ADR-0001 #8: ``apply_triage`` is pure and deterministic but still
    traces - a ``triage`` audit event records every successful apply with the
    disposition, severity, and any minted decision ids."""

    def test_success_audits_triage_event(self, repo_root: Path) -> None:
        feature_root, feature_id, issue_id, _ = _stage_issue(
            repo_root, severity="P1"
        )

        apply_triage(
            repo_root, feature_id, issue_id, OVERRIDE, "waive", "human",
            timestamp=_FIXED_TS,
        )

        event = _last_audit("triage", feature_root)
        assert event["payload"] == {
            "feature": feature_id,
            "issue": issue_id,
            "action": OVERRIDE,
            "severity": "P1",
            "by": "human",
            "decision_ids": ["DEC-001"],
        }
        assert event["timestamp"] == _FIXED_TS

    def test_success_without_promotion_audits_empty_decision_ids(
        self, repo_root: Path
    ) -> None:
        feature_root, feature_id, issue_id, _ = _stage_issue(
            repo_root, severity="P2"
        )

        apply_triage(
            repo_root, feature_id, issue_id, REQUEST_FIX, None, "human",
            timestamp=_FIXED_TS,
        )

        event = _last_audit("triage", feature_root)
        assert event["payload"]["decision_ids"] == []

    def test_refusal_audits_before_raising(self, repo_root: Path) -> None:
        feature_root, feature_id, issue_id, _ = _stage_issue(
            repo_root, severity="P0"
        )

        with pytest.raises(TriageRefusedError):
            apply_triage(
                repo_root, feature_id, issue_id, OVERRIDE, "waive", "human",
                timestamp=_FIXED_TS,
            )

        # The refusal is recorded even though the apply raised.
        events = [e for e in _audit_events(feature_root) if e["event"] == "triage_refused"]
        assert len(events) == 1
        assert events[0]["timestamp"] == _FIXED_TS


class TestDeterministic:
    """ADR-0001 #8: ``apply_triage`` calls no model. It is a pure function of
    its inputs + the issue file, with an injectable timestamp for replay."""

    def test_same_inputs_same_outputs(self, repo_root: Path) -> None:
        # Two identical applies on two identical feature runs produce identical
        # issue state and identical decision artifacts (timestamp injected).
        def _one(root: Path) -> dict[str, Any]:
            _, feature_id, issue_id, issue_path = _stage_issue(root, severity="P1")
            apply_triage(
                root, feature_id, issue_id, REJECT, "false positive", "human",
                timestamp=_FIXED_TS,
            )
            return json.loads(issue_path.read_text())

        import tempfile

        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            issue_a = _one(Path(a))
            issue_b = _one(Path(b))

        assert issue_a == issue_b


class TestCollectThenTriageIntegration:
    """End-to-end seam: a real collected issue (via the collector pipeline) is
    triageable. Proves apply_triage reads the SoT the collector writes and the
    triage survives a later re-collect (ADR-0002 D1 merge preserves it)."""

    def test_triage_survives_recollect(
        self, repo_root: Path
    ) -> None:
        from ai_dev.checking_legs import (
            write_review_report,
            write_spec_gap_report,
        )
        from ai_dev.issue_bundle import ISSUE_BUNDLE_JSON, collect_issue_bundle
        from ai_dev.paths import lane_dir
        from ai_dev.validate import ValidationResult
        from test_checking_legs import _REVIEW_RUN_METADATA, _stage_implement_run
        from test_issue_bundle import _issue, _overwrite_review_report

        review_issues = [
            _issue(
                id="agent-review-1",
                source="code_review",
                severity="P1",
                title="collected issue",
                evidence=[{"file": "workspace/hello.py", "line": 2}],
            )
        ]
        feature_id, lane_id, _ = _stage_implement_run(repo_root)
        feature_root = _feature_root(repo_root, feature_id)
        write_review_report(
            feature_root, lane_id, run_id="RUN-002",
            result={"issues": review_issues}, metadata=_REVIEW_RUN_METADATA,
            validation=ValidationResult("RUN-002", []),
        )
        write_spec_gap_report(
            feature_root, lane_id, run_id="RUN-003", result={"issues": []},
            metadata={**_REVIEW_RUN_METADATA, "run_id": "RUN-003"},
            validation=ValidationResult("RUN-003", []),
        )
        collect_issue_bundle(repo_root, feature_id, lane_id)

        # Triage the collected issue (raised -> triaged).
        apply_triage(
            repo_root, feature_id, _ISSUE_ID, OVERRIDE, "waive for MVP", "human",
            timestamp=_FIXED_TS,
        )

        issue_path = feature_root / "issues" / f"{_ISSUE_ID}.json"
        assert json.loads(issue_path.read_text())["triage"]["action"] == OVERRIDE

        # Re-collect: the triage is preserved (merge, not overwrite).
        _overwrite_review_report(repo_root, feature_id, lane_id, review_issues)
        collect_issue_bundle(repo_root, feature_id, lane_id)

        merged = json.loads(issue_path.read_text())
        assert merged["triage"]["action"] == OVERRIDE
        assert merged["status"] == STATUS_TRIAGED
        # The bundle projects the triage straight through (SoT).
        bundle = json.loads(
            (lane_dir(repo_root, feature_id, lane_id) / ISSUE_BUNDLE_JSON).read_text()
        )
        assert bundle["issues"][0]["triage"]["action"] == OVERRIDE


class TestTriageCli:
    """CLI seam: ``ai-dev triage <FEATURE> --issue ISSUE-NNN --disposition <d>
    [--reason ...] [--by human]``."""

    def test_cli_applies_triage_and_exits_zero(
        self, repo_root: Path, capsys: Any
    ) -> None:
        _, feature_id, issue_id, issue_path = _stage_issue(
            repo_root, severity="P1"
        )

        rc = main([
            "triage", feature_id,
            "--issue", issue_id,
            "--disposition", OVERRIDE,
            "--reason", "waive for MVP",
            "--by", "human",
            "--repo-root", str(repo_root),
        ])

        assert rc == 0
        out = capsys.readouterr().out
        assert "TRIAGE" in out
        assert issue_id in out
        assert json.loads(issue_path.read_text())["triage"]["action"] == OVERRIDE

    def test_cli_refusal_exits_one_clean_error(
        self, repo_root: Path, capsys: Any
    ) -> None:
        _, feature_id, issue_id, issue_path = _stage_issue(
            repo_root, severity="P0"
        )
        original = issue_path.read_text()

        rc = main([
            "triage", feature_id,
            "--issue", issue_id,
            "--disposition", OVERRIDE,
            "--reason", "trying to waive",
            "--repo-root", str(repo_root),
        ])

        assert rc == 1
        err = capsys.readouterr().err
        assert "error:" in err
        assert "P0" in err
        # Issue untouched.
        assert issue_path.read_text() == original

    def test_cli_missing_reason_exits_one(self, repo_root: Path, capsys: Any) -> None:
        _, feature_id, issue_id, _ = _stage_issue(repo_root, severity="P1")

        rc = main([
            "triage", feature_id,
            "--issue", issue_id,
            "--disposition", REJECT,
            "--repo-root", str(repo_root),
        ])

        assert rc == 1
        assert "reason" in capsys.readouterr().err.lower()

    def test_cli_missing_issue_exits_one(self, repo_root: Path, capsys: Any) -> None:
        feature_id = create_feature_run(repo_root, "triage test")

        rc = main([
            "triage", feature_id,
            "--issue", "ISSUE-999",
            "--disposition", REQUEST_FIX,
            "--repo-root", str(repo_root),
        ])

        assert rc == 1
        assert "ISSUE-999" in capsys.readouterr().err

    def test_cli_unknown_disposition_rejected_by_argparse(
        self, repo_root: Path, capsys: Any
    ) -> None:
        # An unknown disposition is a CLI usage error (argparse ``choices``,
        # matching ``freeze``'s ``choices=FROZEN_ARTIFACTS``) - rejected before
        # the library runs. The library's own ValueError for an unknown
        # disposition is covered by ``test_unknown_disposition_fails_loud``.
        _, feature_id, issue_id, _ = _stage_issue(repo_root, severity="P1")

        with pytest.raises(SystemExit) as exc_info:
            main([
                "triage", feature_id,
                "--issue", issue_id,
                "--disposition", "bogus",
                "--repo-root", str(repo_root),
            ])

        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "disposition" in err.lower()
        assert "bogus" in err

    def test_cli_defaults_by_to_human(self, repo_root: Path, capsys: Any) -> None:
        _, feature_id, issue_id, issue_path = _stage_issue(
            repo_root, severity="P2"
        )

        rc = main([
            "triage", feature_id,
            "--issue", issue_id,
            "--disposition", DEFER,
            "--repo-root", str(repo_root),
        ])

        assert rc == 0
        assert json.loads(issue_path.read_text())["triage"]["by"] == "human"
