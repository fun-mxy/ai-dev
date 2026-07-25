"""Read-only query helpers for the v0.4 CLI observability commands (ticket 03).

The v0.4 ``list-features`` / ``show-status`` / ``log`` commands (§26.5 CLI UX)
turn the CLI from "write-only" into "observable". They read canonical state that
other commands already write — never mutating it — so an operator can answer
"which features exist and where is each one in the pipeline?" / "what did this
feature's lanes decide?" / "what is the audit timeline?" without hand-editing
YAML or grepping ``audit.log.md``.

These helpers are pure projections over on-disk state:

* ``list_features`` — one ``FeatureSummary`` per ``FEATURE-NNN`` under
  ``.ai-dev/features`` (derived ``feature.status`` + ``current_gate``).
* ``show_feature_status`` — the feature's gate/verdict/derived status plus one
  ``LaneDecisionSummary`` per lane (its ``lane-decision.json`` verdict, when the
  lane gate has run).
* ``read_audit_timeline`` — the ``audit.log.json`` record array in chronological
  order, the single source the ``log`` command renders (it consumes the machine
  product, including ticket 02's ``origin`` / ``elapsed_ms`` fields).

All three raise ``ValueError`` on a genuinely missing/corrupt feature run
(§24.2 fail loud) — the CLI renders that as a clean ``error:`` + exit 1.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_dev.audit import AUDIT_LOG_JSON
from ai_dev.json_artifact import read_json_object
from ai_dev.lane_gate import LANE_DECISION_JSON
from ai_dev.paths import feature_dir, features_dir, lane_dir
from ai_dev.status import declared_lane_ids, load_feature_status


@dataclass(frozen=True)
class FeatureSummary:
    """One row of ``list-features``: a feature's derived status + current gate."""

    feature_id: str
    status: str
    current_gate: str
    verdict: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "status": self.status,
            "current_gate": self.current_gate,
            "verdict": self.verdict,
        }


@dataclass(frozen=True)
class LaneDecisionSummary:
    """One lane's gate verdict inside ``show-status``.

    ``decision`` is ``None`` when the lane gate has not run yet (no
    ``lane-decision.json``); otherwise it is the gate's ``pass`` / ``fail``
    verdict. ``failed_conditions`` / ``blocking_issue_count`` are ``None`` in the
    no-decision case so the JSON shape distinguishes "not yet evaluated" from
    "evaluated clean".
    """

    lane_id: str
    decision: str | None
    failed_conditions: list[str] = field(default_factory=list)
    blocking_issue_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane_id": self.lane_id,
            "decision": self.decision,
            "failed_conditions": list(self.failed_conditions),
            "blocking_issue_count": self.blocking_issue_count,
        }


@dataclass(frozen=True)
class FeatureStatusView:
    """The ``show-status`` payload: gate/verdict/status + per-lane decisions."""

    feature_id: str
    status: str
    current_gate: str
    verdict: str | None
    lanes: list[LaneDecisionSummary]

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "status": self.status,
            "current_gate": self.current_gate,
            "verdict": self.verdict,
            "lanes": [lane.to_dict() for lane in self.lanes],
        }


@dataclass(frozen=True)
class AuditRecordView:
    """One typed audit record in the ``log`` timeline.

    A typed view over the free-form ``audit.log.json`` record so the renderer
    (and JSON emitter) consume named fields rather than reaching through a
    ``dict[str, Any]``. The ``payload`` stays a generic mapping because audit
    payloads are heterogeneous (every event carries its own detail keys, e.g.
    ticket 02's ``elapsed_ms``) — only the envelope (``timestamp`` / ``event`` /
    ``origin``) is fixed.
    """

    timestamp: str
    event: str
    origin: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "timestamp": self.timestamp,
            "event": self.event,
            "payload": dict(self.payload),
        }
        if self.origin is not None:
            record["origin"] = self.origin
        return record


def _existing_feature_ids(repo_root: Path) -> list[str]:
    """Sorted ``FEATURE-NNN`` directory names under ``.ai-dev/features``."""
    parent = features_dir(repo_root)
    if not parent.is_dir():
        return []
    names = [
        p.name
        for p in parent.iterdir()
        if p.is_dir() and p.name.startswith("FEATURE-")
    ]
    return sorted(names)


def _feature_row(repo_root: Path, feature_id: str) -> FeatureSummary:
    """Read one feature's derived status + gate from its ``feature-status.yml``."""
    feature_root = feature_dir(repo_root, feature_id)
    doc = load_feature_status(feature_root)
    feature = doc["feature"]
    return FeatureSummary(
        feature_id=feature_id,
        status=str(feature.get("status")),
        current_gate=str(feature.get("current_gate")),
        verdict=feature.get("verdict"),
    )


def list_features(repo_root: Path) -> list[FeatureSummary]:
    """List every ``FEATURE-NNN`` with its derived status + current gate.

    Returns a sorted list (one row per feature). Empty when no features exist
    yet — a fresh repo is a valid, observable state, not an error. A feature
    whose ``feature-status.yml`` is missing or corrupt (§24.2) propagates the
    ``ValueError`` from ``load_feature_status`` so the CLI fails loud rather than
    silently dropping a broken feature from the listing.
    """
    return [_feature_row(repo_root, fid) for fid in _existing_feature_ids(repo_root)]


def _lane_ids(repo_root: Path, feature_id: str) -> list[str]:
    """Lane ids from the canonical lane graph/status registry."""
    return declared_lane_ids(feature_dir(repo_root, feature_id))


def _lane_summaries(repo_root: Path, feature_id: str) -> list[LaneDecisionSummary]:
    """One ``LaneDecisionSummary`` per lane (from ``lane-status.yml``), sorted."""
    summaries: list[LaneDecisionSummary] = []
    for lane_id in _lane_ids(repo_root, feature_id):
        decision_path = lane_dir(repo_root, feature_id, lane_id) / LANE_DECISION_JSON
        artifact = read_json_object(decision_path)
        if artifact is None:
            summaries.append(LaneDecisionSummary(lane_id=lane_id, decision=None))
            continue
        conditions = artifact.get("conditions")
        failed = [
            str(c.get("name"))
            for c in conditions
            if isinstance(c, dict) and not c.get("passed")
        ] if isinstance(conditions, list) else []
        summaries.append(
            LaneDecisionSummary(
                lane_id=lane_id,
                decision=str(artifact.get("decision")),
                failed_conditions=failed,
                blocking_issue_count=artifact.get("blocking_issue_count"),
            )
        )
    return summaries


def show_feature_status(repo_root: Path, feature_id: str) -> FeatureStatusView:
    """Read a feature's gate/verdict/derived status + each lane's decision.

    Raises ``ValueError`` (via ``load_feature_status``) when the feature run is
    missing or its ``feature-status.yml`` is corrupt (§24.2 fail loud). Lanes
    without a ``lane-decision.json`` are still listed with ``decision=None`` —
    the lane exists (it was allocated at feature-run creation) even before its
    gate has run.
    """
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id} not found under {repo_root}")
    doc = load_feature_status(feature_root)
    feature = doc["feature"]
    return FeatureStatusView(
        feature_id=feature_id,
        status=str(feature.get("status")),
        current_gate=str(feature.get("current_gate")),
        verdict=feature.get("verdict"),
        lanes=_lane_summaries(repo_root, feature_id),
    )


def read_audit_timeline(
    repo_root: Path, feature_id: str
) -> list[AuditRecordView]:
    """Return the feature's audit record array in chronological order.

    Reads ``audit.log.json`` (the machine product) — not ``audit.log.md`` — so
    the rendered timeline carries ticket 02's structured ``origin`` and
    ``elapsed_ms`` fields verbatim. The records are re-sorted by ``timestamp``
    defensively (the log is append-only so insertion order is already
    chronological, but a hand-edited or merged log must not mislead the
    timeline). Raises ``ValueError`` when the feature run is missing (§24.2); an
    existing feature always has an ``audit.log.json`` (``create_feature_run``
    writes the ``create`` event), so a missing file on an existing feature is
    corruption and is surfaced rather than rendered as an empty timeline.
    """
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id} not found under {repo_root}")
    json_path = feature_root / AUDIT_LOG_JSON
    if not json_path.is_file():
        raise ValueError(
            f"audit.log.json missing at {json_path} (broken feature run, §24.2)"
        )
    try:
        records = json.loads(json_path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"audit.log.json at {json_path} is not valid JSON: {exc} (§24.2)"
        ) from exc
    if not isinstance(records, list):
        raise ValueError(
            f"audit.log.json at {json_path} is not a JSON array (§24.2)"
        )
    views = [_record_view(r) for r in records]
    # Chronological order: the audit log is append-only so insertion order is
    # already time-ordered, but sort defensively so a hand-merged log cannot
    # present events out of order (stable on the timestamp string).
    return sorted(views, key=lambda v: v.timestamp)


def _record_view(record: Any) -> AuditRecordView:
    """Coerce one raw ``audit.log.json`` element into an ``AuditRecordView``.

    Defends against a malformed element (non-mapping, or missing the required
    ``timestamp`` / ``event`` envelope fields) by falling back to readable
    placeholders rather than raising — a single corrupt record must not blank
    the whole timeline, and the envelope drift is itself visible in the output.
    """
    if not isinstance(record, dict):
        return AuditRecordView(timestamp="?", event=f"<non-mapping: {record!r}>")
    payload = record.get("payload")
    return AuditRecordView(
        timestamp=str(record.get("timestamp", "?")),
        event=str(record.get("event", "?")),
        origin=record.get("origin") if isinstance(record.get("origin"), str) else None,
        payload=dict(payload) if isinstance(payload, dict) else {},
    )
