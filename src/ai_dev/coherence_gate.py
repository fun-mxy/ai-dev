"""Feature coherence gate evaluator (v0.3, ADR-0003 D1/D2/D4, ticket 08).

The terminal §18.5 gate. Deterministically checks the three ADR-0003 D1 input
conditions, atomically writes ``current_gate = feature_coherence_gate`` +
``verdict`` (pass/fail) + derived ``feature.status`` (done/blocked) on
``feature-status.yml`` (via :func:`ai_dev.status.record_coherence_verdict`), and
writes a ``coherence-decision.{json,md}`` double product recording the
conditions. No model is called here (invariant #2).

The three conditions (ADR-0003 D1; §18.5 is amended to drop the "final report
是否完整" forward-reference - the final report is downstream, §23.5 step 21, not
a coherence input - the gate verifies *inputs*):

1. ``status_consistent`` - ``feature.status`` equals
   ``derive_feature_status(current_gate, verdict)``. Automatic once D3 lands
   (every status writer derives), so a mismatch is corruption (§24.2 fail-loud),
   *not* a coherence failure: a corrupt status field must not flip a would-be
   pass verdict into fail, so the gate refuses to run rather than writing a
   misleading verdict.
2. ``lane_passed_and_p0_p1_handled`` - every P0/P1 issue in ``issues/`` is
   resolved or disarmed, AND the §18.4 lane gate has PASSed (the established
   mechanism - "经 06 lane-gate 已 PASS"). An unhandled P0/P1 (untriaged /
   ``request_fix`` / ``request_change_proposal`` / reappeared / raised) or a
   non-pass lane decision (e.g. verification failed) -> verdict=fail.
3. ``decisions_recorded`` - every disarmed blocking issue (``override`` x P1,
   ``reject`` x {P0, P1}; ADR-0001 #3) has a ``DEC-NNN`` whose file exists in
   ``decisions/`` (ADR-0001 invariant #15). A disarmed issue missing its DEC ->
   verdict=fail. The lane gate checks the DEC *id* is present in triage for
   bundle issues; this gate checks the DEC *file* exists for every issue
   (feature-level invariant).

``verdict`` is mutable (ADR-0003 D4): a re-coherence overwrites a prior verdict
(e.g. fail -> fix -> re-coherence -> pass), mirroring ``lane-decision.json``'s
re-evaluable verdict. The coherence evaluator is the sole writer of
``feature-status.yml.verdict`` (D4); the final-report generator (ticket 09)
reads it and never writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ai_dev.issue_status import STATUS_RESOLVED
from ai_dev.json_artifact import read_json_object, write_json
from ai_dev.lane_gate import LANE_DECISION_JSON
from ai_dev.paths import feature_dir
from ai_dev.status import declared_lane_ids, derive_feature_status, load_feature_status, record_coherence_verdict
from ai_dev.timeutil import elapsed_ms_between, utc_now_iso
from ai_dev.triage import DECISIONS_DIR

COHERENCE_DECISION_JSON = "coherence-decision.json"
COHERENCE_DECISION_MD = "coherence-decision.md"

_BLOCKING_SEVERITIES = {"P0", "P1"}
# ADR-0001 #3: the (disposition x severity) cells that disarm a blocking issue
# and so mint a DEC. Mirrors lane_gate._DISARMING_ACTIONS; duplicated here so
# the feature-level coherence check is self-contained (the same pattern
# issue_bundle / lane_gate already follow for the triage helpers).
_DISARMING_ACTIONS = {
    ("P0", "reject"),
    ("P1", "override"),
    ("P1", "reject"),
}
# current_gate values from which the coherence gate may run: the normal first
# coherence (lane_gate, after the human gates + lane gate) and a re-coherence
# (feature_coherence_gate, overwriting a prior verdict). Running from any
# earlier gate would skip human gates - refused as broken sequencing (§24.2).
_COHERENCE_ENTRY_GATES = {"lane_gate", "feature_coherence_gate"}


@dataclass(frozen=True)
class CoherenceResult:
    """Summary of one deterministic coherence gate evaluation."""

    feature_id: str
    verdict: str
    conditions: list[dict[str, Any]]
    decision_md_path: Path
    decision_json_path: Path

    @property
    def passed(self) -> bool:
        """True when the coherence verdict is pass."""
        return self.verdict == "pass"

    @property
    def failed_conditions(self) -> list[str]:
        """Condition names whose result was failing."""
        return [str(c["name"]) for c in self.conditions if not c.get("passed")]


def _severity(issue: Mapping[str, Any]) -> str:
    severity = issue.get("severity")
    return severity if isinstance(severity, str) else ""


def _issue_id(issue: Mapping[str, Any]) -> str:
    iid = issue.get("id")
    return iid if isinstance(iid, str) else "<unknown>"


def _issue_status(issue: Mapping[str, Any]) -> str | None:
    status = issue.get("status")
    return status if isinstance(status, str) else None


def _triage(issue: Mapping[str, Any]) -> Mapping[str, Any] | None:
    triage = issue.get("triage")
    return triage if isinstance(triage, Mapping) else None


def _triage_action(issue: Mapping[str, Any]) -> str | None:
    triage = _triage(issue)
    if triage is None:
        return None
    action = triage.get("action")
    return action if isinstance(action, str) else None


def _has_triage_reason(issue: Mapping[str, Any]) -> bool:
    triage = _triage(issue)
    if triage is None:
        return False
    reason = triage.get("reason")
    return isinstance(reason, str) and bool(reason.strip())


def _decision_ids(issue: Mapping[str, Any]) -> list[str]:
    triage = _triage(issue)
    if triage is None:
        return []
    raw = triage.get("decision_ids")
    if not isinstance(raw, list):
        return []
    return [str(decision_id) for decision_id in raw if isinstance(decision_id, str)]


def _is_disarmed(issue: Mapping[str, Any]) -> bool:
    """True iff the issue is currently disarmed by a recorded triage (ADR-0001 #3).

    A P0/P1 whose current triage is a disarming action (``override`` x P1,
    ``reject`` x {P0, P1}) with a reason. ``apply_triage`` enforces the reason
    for every disarming cell, so a disarming action without a reason is
    corruption that surfaces as "not disarmed" (hence unhandled) at condition 2.
    Resolved issues have their triage cleared to history by the collector, so
    they are not currently disarmed - they are handled via the resolved branch.
    """
    severity = _severity(issue)
    if severity not in _BLOCKING_SEVERITIES:
        return False
    action = _triage_action(issue)
    if action is None or (severity, action) not in _DISARMING_ACTIONS:
        return False
    return _has_triage_reason(issue)


def _p0_p1_handled(issue: Mapping[str, Any]) -> bool:
    """True iff a P0/P1 issue is resolved or disarmed (ADR-0003 D1 condition 2).

    P2/P3 are never blocking, so they are unconditionally handled. A P0/P1 is
    handled iff it is ``resolved`` (fixed/gone) or currently disarmed by a
    recorded triage; anything else (untriaged, ``request_fix`` pending,
    ``request_change_proposal`` clean deferral, reappeared, raised) is unhandled
    and fails the gate.
    """
    severity = _severity(issue)
    if severity not in _BLOCKING_SEVERITIES:
        return True
    if _issue_status(issue) == STATUS_RESOLVED:
        return True
    return _is_disarmed(issue)


def _condition(name: str, passed: bool, reason: str) -> dict[str, Any]:
    return {"name": name, "passed": passed, "reason": reason}


def _status_consistent_condition(feature: Mapping[str, Any]) -> dict[str, Any]:
    """D1 condition 1 - the on-disk ``feature.status`` matches the derived
    projection of ``(current_gate, verdict)``.

    Automatic once D3 lands (every status writer derives), so this is a
    corruption guard: a mismatch (or a state ``derive_feature_status`` rejects,
    like the unreachable ``(fcg, null)`` transient) fail-louds (§24.2) rather
    than producing a misleading verdict.
    """
    current_gate = feature.get("current_gate")
    verdict = feature.get("verdict")
    on_disk = feature.get("status")
    if not isinstance(current_gate, str):
        raise ValueError(
            f"feature-status.yml current_gate is not a string: {current_gate!r} (§24.2)"
        )
    if verdict is not None and not isinstance(verdict, str):
        raise ValueError(
            f"feature-status.yml verdict is not a string or null: {verdict!r} (§24.2)"
        )
    try:
        derived = derive_feature_status(current_gate, verdict)
    except ValueError as exc:
        raise ValueError(
            f"feature-status.yml is corrupt: derive_feature_status raised on "
            f"(current_gate={current_gate!r}, verdict={verdict!r}): {exc} (§24.2)"
        ) from exc
    if derived != on_disk:
        raise ValueError(
            f"feature-status.yml status is inconsistent: on-disk status={on_disk!r} "
            f"but derive(current_gate={current_gate!r}, verdict={verdict!r})="
            f"{derived!r}; every status writer derives, so a mismatch is "
            f"corruption (§24.2)"
        )
    return _condition(
        "status_consistent",
        True,
        f"feature.status={on_disk!r} matches derive({current_gate!r}, {verdict!r})",
    )


def _load_lane_decisions(feature_root: Path) -> list[dict[str, Any]]:
    """Read every declared lane's ``lane-decision.json``; fail-loud if any missing.

    In v0.7 the canonical lane set is ``04-lane-graph.yml`` and runtime
    ``lane-status.yml`` must agree with it. Coherence therefore requires every
    canonical lane to have reached a lane gate verdict, not only whichever
    lane-decision files happen to exist under ``lanes/``.
    """
    decisions: list[dict[str, Any]] = []
    declared = declared_lane_ids(feature_root)
    paths = [(lane_id, feature_root / "lanes" / lane_id / LANE_DECISION_JSON) for lane_id in declared]
    for lane_id, path in paths:
        decision = read_json_object(path)
        if decision is None:
            raise ValueError(
                f"lane-decision.json for lane {lane_id} at {path} is missing or invalid (§24.2)"
            )
        decisions.append(decision)
    if not decisions:
        raise ValueError(
            f"no lane-decision.json found under {feature_root}/lanes/; run "
            f"`ai-dev lane-gate` before `ai-dev coherence-gate` (§24.2)"
        )
    return decisions


def _load_all_issues(feature_root: Path) -> list[dict[str, Any]]:
    """Read every ``issues/ISSUE-NNN.json`` (the SoT, ADR-0002 D1).

    Empty when no issues were ever raised (a clean feature). A structurally
    invalid issue file fail-louds (§24.2) - the coherence gate reads the same
    SoT the collector writes, so an unreadable issue is corruption, not a
    silently-dropped entry.
    """
    issues: list[dict[str, Any]] = []
    issue_root = feature_root / "issues"
    if not issue_root.is_dir():
        return issues
    for path in sorted(issue_root.glob("ISSUE-*.json")):
        issue = read_json_object(path)
        if issue is None:
            raise ValueError(f"issue file {path} is missing or invalid (§24.2)")
        issues.append(issue)
    return issues


def _lane_passed_and_p0_p1_handled_condition(
    issues: list[dict[str, Any]], lane_decisions: list[dict[str, Any]]
) -> dict[str, Any]:
    """D1 condition 2 - the lane gate PASSed and every P0/P1 is resolved or
    disarmed.

    The ticket's "经 06 lane-gate 已 PASS" makes the lane-gate verdict part of
    this condition (not a separate one): a lane gate that FAILed for a non-issue
    reason (verification, implement not proposed_done) means the feature is not
    coherent, even if no P0/P1 is unhandled. The name carries both halves so a
    verification-only failure does not report as "P0/P1 unhandled".
    """
    non_pass_lanes = [
        d for d in lane_decisions if d.get("decision") != "pass"
    ]
    unhandled = [
        {
            "id": _issue_id(issue),
            "severity": _severity(issue),
            "status": _issue_status(issue),
            "triage_action": _triage_action(issue),
        }
        for issue in issues
        if not _p0_p1_handled(issue)
    ]
    if not non_pass_lanes and not unhandled:
        return _condition(
            "lane_passed_and_p0_p1_handled",
            True,
            f"all {len(issues)} issue(s) resolved or disarmed; "
            f"{len(lane_decisions)} lane gate(s) passed",
        )
    parts: list[str] = []
    if non_pass_lanes:
        parts.append(
            "lane gate not passed: "
            + ", ".join(str(d.get("decision")) for d in non_pass_lanes)
        )
    if unhandled:
        parts.append(f"unhandled P0/P1 issue(s): {unhandled}")
    return _condition("lane_passed_and_p0_p1_handled", False, "; ".join(parts))


def _decisions_recorded_condition(
    issues: list[dict[str, Any]], feature_root: Path
) -> dict[str, Any]:
    """D1 condition 3 - every disarmed P0/P1 has a DEC-NNN file (invariant #15)."""
    missing: list[dict[str, Any]] = []
    for issue in issues:
        if not _is_disarmed(issue):
            continue
        issue_id = _issue_id(issue)
        dec_ids = _decision_ids(issue)
        if not dec_ids:
            missing.append({"id": issue_id, "reason": "no DEC-NNN in triage"})
            continue
        for dec_id in dec_ids:
            dec_path = feature_root / DECISIONS_DIR / f"{dec_id}.json"
            if not dec_path.is_file():
                missing.append({"id": issue_id, "reason": f"{dec_id} file missing"})
    if not missing:
        disarmed = sum(1 for issue in issues if _is_disarmed(issue))
        return _condition(
            "decisions_recorded",
            True,
            f"all {disarmed} disarmed P0/P1 issue(s) have a DEC-NNN file",
        )
    return _condition(
        "decisions_recorded",
        False,
        f"disarmed issue(s) missing a Decision: {missing}",
    )


def _coherence_decision_json(
    feature_id: str,
    verdict: str,
    conditions: list[dict[str, Any]],
    lane_decisions: list[dict[str, Any]],
    issue_count: int,
) -> dict[str, Any]:
    return {
        "feature": feature_id,
        "verdict": verdict,
        "conditions": conditions,
        "lane_decision_count": len(lane_decisions),
        "issue_count": issue_count,
    }


def _coherence_decision_md(decision: Mapping[str, Any]) -> str:
    raw_conditions = decision.get("conditions")
    conditions = raw_conditions if isinstance(raw_conditions, list) else []
    lines = [
        f"# Coherence Decision - {decision.get('feature', '')}",
        "",
        f"- feature: {decision.get('feature', '')}",
        f"- verdict: **{decision.get('verdict', '')}**",
        f"- issue_count: {decision.get('issue_count', 0)}",
        f"- lane_decision_count: {decision.get('lane_decision_count', 0)}",
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
    lines.append("")
    return "\n".join(lines)


@dataclass(frozen=True)
class CoherenceCompute:
    """The pure read+compute half of the coherence gate (no writes).

    Extracted so ``--dry-run`` (ticket 04 / ADR-0004) can compute the would-be
    verdict + condition breakdown without writing canonical state. The writer
    (``evaluate_coherence_gate``) and the dry-run planner share this one
    computation so they can never diverge.
    """

    verdict: str
    conditions: list[dict[str, Any]]
    lane_decisions: list[dict[str, Any]]
    issue_count: int


def compute_coherence(feature_root: Path) -> CoherenceCompute:
    """Run the §18.5 precondition + condition compute (ADR-0003 D1), no writes.

    Loads ``feature-status.yml``, enforces the sequencing guard (coherence runs
    only from ``lane_gate`` or ``feature_coherence_gate``), and evaluates the
    three D1 input conditions -> a ``pass``/``fail`` verdict. Pure of side
    effects: it reads status / lane-decisions / issues / decisions and returns
    the verdict + conditions, writing nothing. Missing/corrupt prerequisites or
    bad sequencing raise ``ValueError`` (§24.2) exactly as the writer does.
    """
    feature = load_feature_status(feature_root)["feature"]

    # Sequencing guard: coherence runs from lane_gate (first coherence) or fcg
    # (re-coherence), never an earlier gate (which would skip human gates).
    current_gate = feature.get("current_gate")
    if current_gate not in _COHERENCE_ENTRY_GATES:
        raise ValueError(
            f"coherence gate cannot run from current_gate={current_gate!r}; "
            f"expected one of {sorted(_COHERENCE_ENTRY_GATES)} (run the human "
            f"gates + `ai-dev lane-gate` first; §24.2)"
        )

    # D1 condition 1 - corruption guard (fail-loud); always passes in a
    # non-corrupt run because every status writer derives (ADR-0003 D3).
    status_condition = _status_consistent_condition(feature)

    lane_decisions = _load_lane_decisions(feature_root)
    issues = _load_all_issues(feature_root)

    # D1 conditions 2 and 3 produce the verdict.
    handled_condition = _lane_passed_and_p0_p1_handled_condition(issues, lane_decisions)
    decisions_condition = _decisions_recorded_condition(issues, feature_root)
    conditions = [status_condition, handled_condition, decisions_condition]
    verdict = "pass" if all(c.get("passed") for c in conditions) else "fail"
    return CoherenceCompute(
        verdict=verdict,
        conditions=conditions,
        lane_decisions=lane_decisions,
        issue_count=len(issues),
    )


def evaluate_coherence_gate(
    repo_root: Path, feature_id: str, *, origin: str | None = None
) -> CoherenceResult:
    """Evaluate §18.5 for a feature and write the terminal verdict (ADR-0003 D7).

    Deterministically checks the three D1 input conditions, writes
    ``coherence-decision.{json,md}`` at the feature root, and atomically writes
    ``current_gate = feature_coherence_gate`` + ``verdict`` + derived
    ``feature.status`` on ``feature-status.yml``. Missing or invalid
    prerequisite artifacts (no lane-decision, corrupt feature-status, broken
    sequencing) fail loud (§24.2) *before* any verdict is written; a normal
    coherence failure (unhandled P0/P1, missing DEC) still writes the decision
    artifacts and the verdict, and returns a FAIL result.
    """
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id} not found under {repo_root}")

    # v0.4 ticket 02: the coherence evaluation's wall-clock duration lands on
    # the ``coherence_gate`` event (``elapsed_ms``), captured around the
    # deterministic condition checks.
    gate_started = utc_now_iso()

    compute = compute_coherence(feature_root)
    verdict = compute.verdict
    conditions = compute.conditions

    decision = _coherence_decision_json(
        feature_id,
        verdict,
        conditions,
        compute.lane_decisions,
        compute.issue_count,
    )
    decision_json_path = feature_root / COHERENCE_DECISION_JSON
    decision_md_path = feature_root / COHERENCE_DECISION_MD

    # ADR-0003 D2/D4: the terminal atomic write - current_gate=fcg + verdict +
    # derived feature.status, audited as one coherence_gate event carrying the
    # condition breakdown. The (fcg, null) transient is unreachable by
    # construction (verdict is always pass/fail here). This canonical mutation
    # lands BEFORE the coherence-decision record so a failure here cannot orphan
    # a decision product over a verdict that never committed; the decision
    # product is a re-computable record written afterward.
    record_coherence_verdict(
        feature_root,
        verdict,
        audit_payload={
            "feature": feature_id,
            "failed_conditions": [
                str(c["name"]) for c in conditions if not c.get("passed")
            ],
            "condition_count": len(conditions),
            "issue_count": compute.issue_count,
            "elapsed_ms": elapsed_ms_between(gate_started, utc_now_iso()),
        },
        origin=origin,
    )
    write_json(decision_json_path, decision)
    decision_md_path.write_text(_coherence_decision_md(decision))

    return CoherenceResult(
        feature_id=feature_id,
        verdict=verdict,
        conditions=conditions,
        decision_md_path=decision_md_path,
        decision_json_path=decision_json_path,
    )
