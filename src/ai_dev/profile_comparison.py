"""``ai-dev compare-profiles`` projection (v0.5 ticket 06, ADR-0003-style).

A **non-canonical, re-computable** side-by-side projection of two parallel
feature-runs - the same intent executed once per Agent Profile (e.g.
``cc-glm52`` vs ``codex-default``), each a full pipeline reaching a verdict.
Like ``final-report`` (ADR-0003 D5/D7) it is a projection, not a spec: it reads
existing artifacts, writes nothing canonical, appends no audit record, and
carries no wall-clock stamp, so two runs project byte-identically.

The lane/RUN model is untouched. The two parallel feature-runs are *separate*
``FEATURE-NNN`` directories (the v0.5 capstone's "chosen orchestration": one
feature-run per profile, identical intent). ``compare-profiles FEATURE-NNN
--profiles p1,p2`` takes a single anchor feature id and locates the two runs to
compare by:

1. reading the anchor's original intent (``00-intent.md``);
2. grouping every feature-run that shares that identical intent text (the
   parallel runs);
3. selecting, for each requested profile, the feature-run whose Agent Profile
   configuration (``feature-status.yml``'s ``agent_profiles`` ``role -> profile``
   dict, recorded by the orchestrator as each leg runs) has that profile as its
   implementer - the representative backend for the run.

It then reads each selected feature's ``final-report.json`` (verdict,
``failure_class``, requirement/acceptance coverage, issue dispositions) and
audit timeline (per-leg ``elapsed_ms``) and projects the comparison into
``projections/profile-comparison.{json,md}`` under the anchor feature.

Metrics (ticket 06): per-leg ``elapsed_ms``, verifier (lane) pass/fail, final
verdict + ``failure_class``, issue count by severity (reviewer profile noted in
``agent_profiles``), and requirement coverage (Q2/Q3 - the quality axis, real
via ticket 05 / ADR-0007). ``meta.known_gaps`` records the caveats
(reviewer-variance, planner non-determinism from independent planning,
self-attested coverage, unnormalized wall-clock latency).

Fail-loud (§24.2) on missing *required* inputs (a selected feature with no
generated ``final-report.json`` / null verdict, or a requested profile with no
matching intent-sibling); defensive on *optional* ones (no runs, no issues).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from ai_dev.audit import AUDIT_LOG_JSON
from ai_dev.checking_legs import (
    REVIEW_REPORT_JSON,
    SPEC_GAP_REPORT_JSON,
)
from ai_dev.final_report import FINAL_REPORT_JSON
from ai_dev.implement_leg import IMPLEMENT_RESULT_JSON
from ai_dev.json_artifact import read_json_object, write_json
from ai_dev.lane_gate import LANE_DECISION_JSON
from ai_dev.paths import LANES_DIR, feature_dir, features_dir
from ai_dev.profiles import ROLE_IMPLEMENTER
from ai_dev.shell_verifier import VERIFICATION_REPORT_JSON
from ai_dev.status import agent_profiles
from ai_dev.timeutil import elapsed_ms_between

PROFILE_COMPARISON_JSON = "profile-comparison.json"
PROFILE_COMPARISON_MD = "profile-comparison.md"

_INTENT_FILE = "00-intent.md"
_INTENT_HEADING = "## Original intent"

# The four agent legs whose per-leg elapsed_ms the projection attributes.
_LEG_IMPLEMENT = "implement"
_LEG_REVIEW = "review"
_LEG_SPEC_GAP = "spec_gap"
_LEG_VERIFY = "verify"


# ---------------------------------------------------------------------------
# Result types (mirror final_report's compute/result split).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProfileComparisonCompute:
    """The pure, no-write half: the projected report document."""

    report: dict[str, Any]


@dataclass(frozen=True)
class ProfileComparisonResult:
    """The writer's result: where the two projection products landed."""

    feature_id: str
    projection_json_path: Path
    projection_md_path: Path


# ---------------------------------------------------------------------------
# Readers.
# ---------------------------------------------------------------------------


def _lane_dirs(feature_root: Path) -> list[Path]:
    """Return the sorted ``lanes/*/`` directories under a feature root (defensive)."""
    lanes = feature_root / LANES_DIR
    if not lanes.is_dir():
        return []
    return sorted(child for child in lanes.iterdir() if child.is_dir())


def _read_intent(feature_root: Path) -> str:
    """Return the feature's original-intent text (sibling-grouping key).

    ``00-intent.md`` carries a ``Captured: <timestamp>`` line that differs
    between two otherwise-identical parallel runs, so the whole file is not a
    stable key. The text under ``## Original intent (原始需求)`` is the verbatim
    user intent - identical across parallel runs by construction - so that is
    the grouping key. Fails loud (§24.2) if the intent file is missing: a real
    feature run always has one (``create_feature_run`` writes it).
    """
    path = feature_root / _INTENT_FILE
    if not path.is_file():
        raise ValueError(
            f"intent file missing at {path} (broken feature run, §24.2)"
        )
    text = path.read_text()
    # Slice from the heading to the next heading (or EOF); strip whitespace.
    start = text.find(_INTENT_HEADING)
    if start == -1:
        # Pre-template intent file: fall back to the whole body minus the title.
        return text.strip()
    body = text[start + len(_INTENT_HEADING):]
    next_heading = body.find("\n## ")
    if next_heading != -1:
        body = body[:next_heading]
    return body.strip()


def _read_final_report(feature_root: Path) -> dict[str, Any]:
    """Read a feature's generated ``final-report.json`` (required input).

    Fails loud (§24.2) if the report is missing or has no non-null verdict: the
    projection compares *completed* runs (the capstone's "both reaching a
    verdict"), and ``generate_final_report`` itself refuses a null verdict, so a
    report without one is either the creation placeholder or a not-yet-complete
    run - not comparable material.
    """
    report = read_json_object(feature_root / FINAL_REPORT_JSON)
    if report is None:
        raise ValueError(
            f"no generated {FINAL_REPORT_JSON} at {feature_root} - run the "
            f"full pipeline (coherence-gate + final-report) before comparing "
            f"(§24.2)"
        )
    verdict = report.get("verdict")
    if verdict is None:
        raise ValueError(
            f"{FINAL_REPORT_JSON} at {feature_root} has a null verdict - the "
            f"feature has not reached a coherence verdict; run coherence-gate + "
            f"final-report before comparing (§24.2)"
        )
    return report


def _audit_events(feature_root: Path) -> list[dict[str, Any]]:
    """Return the feature's audit events, defensive on a missing/unreadable log.

    ``audit.log.json`` is a JSON *array* (unlike the dict artifacts elsewhere),
    so it cannot go through ``read_json_object`` (which is dict-only); it is read
    directly and any non-array shape is treated as "no events" (defensive).
    """
    path = feature_root / AUDIT_LOG_JSON
    if not path.is_file():
        return []
    try:
        log = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(log, list):
        return []
    return [e for e in log if isinstance(e, dict)]


def _run_elapsed_ms(audit_events: list[dict[str, Any]]) -> dict[str, int]:
    """Map ``run_id -> elapsed_ms`` from the audit ``run`` events.

    ``elapsed_ms`` lives in the ``run`` event payload (v0.4 ticket 02), not in
    ``metadata.json`` - this is why the projection reads the audit timeline.
    """
    out: dict[str, int] = {}
    for event in audit_events:
        if event.get("event") != "run":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        run_id = payload.get("run")
        elapsed = payload.get("elapsed_ms")
        if isinstance(run_id, str) and isinstance(elapsed, int) and not isinstance(elapsed, bool):
            out[run_id] = elapsed
    return out


def _run_role_map(feature_root: Path) -> dict[str, str]:
    """Map ``run_id -> leg`` by scanning each lane's leg artifacts.

    Each lane's implement-result / review-report / spec-gap-report carries the
    run id of the run that produced it, so a run is attributed to its leg. The
    verifier is not a profile run (no audit ``run`` event); it is handled
    separately from its own report.
    """
    role_map: dict[str, str] = {}
    for lane_root in _lane_dirs(feature_root):
        implement = read_json_object(lane_root / IMPLEMENT_RESULT_JSON)
        if isinstance(implement, dict):
            run_id = implement.get("run")
            if isinstance(run_id, str):
                role_map[run_id] = _LEG_IMPLEMENT
        review = read_json_object(lane_root / "review" / REVIEW_REPORT_JSON)
        if isinstance(review, dict):
            run_id = review.get("run")
            if isinstance(run_id, str):
                role_map[run_id] = _LEG_REVIEW
        gap = read_json_object(lane_root / "spec-gap" / SPEC_GAP_REPORT_JSON)
        if isinstance(gap, dict):
            run_id = gap.get("run")
            if isinstance(run_id, str):
                role_map[run_id] = _LEG_SPEC_GAP
    return role_map


def _verify_elapsed_ms(feature_root: Path) -> int:
    """Sum the verifier ``elapsed_ms`` across lanes (from each verification report).

    The verifier report carries ``elapsed_ms`` when written by current code; older
    reports carry only ``started_at`` / ``ended_at`` and are derived. The verifier
    is not a profile run, so its duration never appears in the audit ``run``
    events - it is read here.
    """
    total = 0
    for lane_root in _lane_dirs(feature_root):
        report = read_json_object(lane_root / "verification" / VERIFICATION_REPORT_JSON)
        if not isinstance(report, dict):
            continue
        elapsed = report.get("elapsed_ms")
        if isinstance(elapsed, int) and not isinstance(elapsed, bool):
            total += elapsed
            continue
        started = report.get("started_at")
        ended = report.get("ended_at")
        if isinstance(started, str) and isinstance(ended, str):
            total += elapsed_ms_between(started, ended)
    return total


def _aggregate_lane_verdict(
    feature_root: Path, report_path: Callable[[Path], Path], key: str
) -> str | None:
    """Aggregate a per-lane verdict string (``pass`` unless any lane is non-pass).

    Both the shell verifier (``verification-report.json`` / ``verdict``) and the
    lane gate (``lane-decision.json`` / ``decision``) project the same shape - one
    verdict per lane - so this one helper serves both. ``pass`` iff every lane's
    value is ``pass``; otherwise the first non-pass value; ``None`` when no lane
    has the artifact (v0.5 is single-lane, so this is one lane's verdict).
    """
    verdicts: list[str] = []
    for lane_root in _lane_dirs(feature_root):
        report = read_json_object(report_path(lane_root))
        if isinstance(report, dict):
            verdict = report.get(key)
            if isinstance(verdict, str):
                verdicts.append(verdict)
    if not verdicts:
        return None
    non_pass = [v for v in verdicts if v != "pass"]
    return non_pass[0] if non_pass else "pass"


def _verify_verdict(feature_root: Path) -> str | None:
    """The shell-verifier verdict (``pass`` / ``fail``), from ``verification-report.json``."""
    return _aggregate_lane_verdict(
        feature_root,
        lambda lane_root: lane_root / "verification" / VERIFICATION_REPORT_JSON,
        "verdict",
    )


def _issue_severity_counts(report: Mapping[str, Any]) -> dict[str, int]:
    """Count issues by severity from the final-report ``issue_dispositions`` rows."""
    counts: dict[str, int] = {}
    for row in report.get("issue_dispositions", []) or []:
        if not isinstance(row, dict):
            continue
        severity = row.get("severity") or "unspecified"
        counts[severity] = counts.get(severity, 0) + 1
    return dict(sorted(counts.items()))


def _coverage_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    """Summarize Q2 requirement coverage + Q3 acceptance verification."""
    req_rows = report.get("requirement_coverage", []) or []
    ac_rows = report.get("acceptance_verification", []) or []
    req_implemented = sum(
        1 for r in req_rows if isinstance(r, dict) and r.get("implemented")
    )
    ac_verified = sum(
        1 for r in ac_rows
        if isinstance(r, dict) and (r.get("verified") or r.get("lane_verification") == "pass")
    )
    return {
        "requirement_coverage": {
            "requirements_total": len(req_rows),
            "implemented": req_implemented,
            "rows": list(req_rows),
        },
        "acceptance_verification": {
            "acceptance_total": len(ac_rows),
            "verified": ac_verified,
            "rows": list(ac_rows),
        },
    }


def _effective_implementer_profile(feature_root: Path, profiles: Mapping[str, str]) -> str | None:
    """The feature's implementer profile - the run's representative backend.

    Primary source is ``feature-status.yml``'s ``agent_profiles.implementer``
    (the orchestrator's record, v0.5 ticket 06). Fallback for pre-v0.5 runs that
    predate the record: the implement run's ``metadata.json`` ``profile``. This
    keeps the projection useful against historical runs while the recorded dict
    remains the source of truth going forward.
    """
    implementer = profiles.get(ROLE_IMPLEMENTER)
    if implementer:
        return implementer
    # Defensive fallback: derive from the implement run's metadata profile.
    for lane_root in _lane_dirs(feature_root):
        implement = read_json_object(lane_root / IMPLEMENT_RESULT_JSON)
        if not isinstance(implement, dict):
            continue
        run_id = implement.get("run")
        if not isinstance(run_id, str):
            continue
        metadata = read_json_object(
            feature_root / "runs" / run_id / "output" / "metadata.json"
        )
        if isinstance(metadata, dict):
            profile = metadata.get("profile")
            if isinstance(profile, str):
                return profile
    return None


# ---------------------------------------------------------------------------
# Discovery: locate the two parallel feature-runs.
# ---------------------------------------------------------------------------


def _intent_siblings(repo_root: Path, intent: str) -> list[Path]:
    """Every feature-run whose original-intent text equals ``intent``."""
    siblings: list[Path] = []
    features = features_dir(repo_root)
    if not features.is_dir():
        return siblings
    for child in sorted(features.iterdir()):
        if not child.is_dir():
            continue
        try:
            if _read_intent(child) == intent:
                siblings.append(child)
        except ValueError:
            # A corrupt/missing intent file cannot be a sibling.
            continue
    return siblings


def _resolve_profiles(
    repo_root: Path,
    anchor: Path,
    profile_names: list[str],
) -> dict[str, Path]:
    """Resolve each requested profile name to its intent-sibling feature root.

    A feature "is" profile ``p`` when its effective implementer profile is ``p``.
    Fails loud (§24.2) if a requested profile matches no sibling, or matches
    several (ambiguous), or if the two profiles resolve to the same feature.
    """
    intent = _read_intent(anchor)
    siblings = _intent_siblings(repo_root, intent)
    # Pre-compute each sibling's effective implementer profile once.
    implementer_by_root: dict[Path, str | None] = {}
    for root in siblings:
        try:
            profiles = agent_profiles(root)
        except ValueError:
            profiles = {}
        implementer_by_root[root] = _effective_implementer_profile(root, profiles)

    resolved: dict[str, Path] = {}
    for name in profile_names:
        matches = [root for root, imp in implementer_by_root.items() if imp == name]
        if not matches:
            raise ValueError(
                f"no feature-run with implementer profile {name!r} among the "
                f"{len(siblings)} intent-sibling(s) of {anchor.name}; "
                f"run the full pipeline under profile {name!r} first (§24.2)"
            )
        if len(matches) > 1:
            ids = sorted(r.name for r in matches)
            raise ValueError(
                f"profile {name!r} is ambiguous: {ids} all use it as implementer; "
                f"a compared profile must map to exactly one feature-run (§24.2)"
            )
        resolved[name] = matches[0]

    # The two compared features must be distinct runs.
    distinct = {r.name for r in resolved.values()}
    if len(distinct) < len(resolved):
        raise ValueError(
            f"the requested profiles {profile_names} resolve to the same "
            f"feature-run; compare two different profiles (§24.2)"
        )
    return resolved


# ---------------------------------------------------------------------------
# Per-profile metrics.
# ---------------------------------------------------------------------------


def _profile_metrics(feature_root: Path) -> dict[str, Any]:
    """Project one feature-run's comparison metrics from its artifacts."""
    report = _read_final_report(feature_root)
    profiles = agent_profiles(feature_root)
    audit = _audit_events(feature_root)
    run_elapsed = _run_elapsed_ms(audit)
    role_map = _run_role_map(feature_root)

    by_leg: dict[str, int] = {
        _LEG_IMPLEMENT: 0,
        _LEG_REVIEW: 0,
        _LEG_SPEC_GAP: 0,
        _LEG_VERIFY: 0,
    }
    for run_id, elapsed in run_elapsed.items():
        leg = role_map.get(run_id)
        if leg in by_leg:
            by_leg[leg] += elapsed
        else:
            # A run not attributable to a lane leg (e.g. a stray/legacy run) is
            # bucketed separately rather than silently dropped.
            by_leg["other"] = by_leg.get("other", 0) + elapsed
    by_leg[_LEG_VERIFY] += _verify_elapsed_ms(feature_root)
    total = sum(by_leg.values())

    issue_counts = _issue_severity_counts(report)
    return {
        "feature_id": feature_root.name,
        "agent_profiles": dict(profiles),
        "verdict": report.get("verdict"),
        "failure_class": report.get("failure_class"),
        # Two distinct artifacts, kept separate (ADR-0003 D2): the shell
        # verifier (verification-report.json) and the lane gate
        # (lane-decision.json) - nesting one under the other would muddy the
        # separation, so they are sibling metrics.
        "verifier": _verify_verdict(feature_root),
        "lane_decision": _lane_decision_verdict(feature_root),
        "elapsed_ms": {"total": total, "by_leg": by_leg},
        "issues": {"total": sum(issue_counts.values()), "by_severity": issue_counts},
        "coverage": _coverage_summary(report),
    }


def _lane_decision_verdict(feature_root: Path) -> str | None:
    """The lane-gate decision (``pass`` / ``fail`` / ``request_change``)."""
    return _aggregate_lane_verdict(
        feature_root,
        lambda lane_root: lane_root / LANE_DECISION_JSON,
        "decision",
    )


# ---------------------------------------------------------------------------
# Report assembly.
# ---------------------------------------------------------------------------


_KNOWN_GAPS = [
    "reviewer-variance: issue counts depend on the reviewer profile's judgement; "
    "the compared features may use different reviewer profiles (see each "
    "agent_profiles mapping), so identical intent can yield different issue sets",
    "planner-non-determinism: requirements/AC ids are planned independently per "
    "feature-run, so coverage rows may not be 1:1 comparable across profiles",
    "requirement-coverage: Q2/Q3 is implementer self-attestation cross-checked by "
    "the Spec Gap Analyst (ADR-0007), not objectively verified",
    "elapsed-ms: wall-clock durations reflect each profile's backend latency and "
    "are not normalized for load, model tier, or network",
]


def _meta(
    anchor_feature_id: str,
    intent: str,
    profile_names: list[str],
    feature_ids: list[str],
) -> dict[str, Any]:
    return {
        "anchor_feature": anchor_feature_id,
        "intent": intent,
        "profiles_compared": list(profile_names),
        "feature_ids": feature_ids,
        "projection": "profile-comparison",
        "canonical": False,
        "known_gaps": list(_KNOWN_GAPS),
    }


def _build_report(
    repo_root: Path,
    feature_id: str,
    profile_names: list[str],
) -> dict[str, Any]:
    anchor = feature_dir(repo_root, feature_id)
    if not anchor.is_dir():
        raise ValueError(
            f"feature {feature_id!r} not found at {anchor} (§24.2)"
        )
    resolved = _resolve_profiles(repo_root, anchor, profile_names)
    metrics_by_profile: dict[str, dict[str, Any]] = {}
    for name in profile_names:
        metrics_by_profile[name] = _profile_metrics(resolved[name])

    intent = _read_intent(anchor)
    feature_ids = [metrics_by_profile[name]["feature_id"] for name in profile_names]
    return {
        "meta": _meta(feature_id, intent, profile_names, feature_ids),
        "profiles": metrics_by_profile,
    }


# ---------------------------------------------------------------------------
# MD skeleton (deterministic render from the JSON; ADR-0003 D5-style).
# ---------------------------------------------------------------------------


def _profile_md(name: str, metrics: Mapping[str, Any]) -> list[str]:
    agent = metrics.get("agent_profiles", {}) or {}
    agent_str = ", ".join(f"{k}={v}" for k, v in sorted(agent.items())) or "_none_"
    by_leg = (metrics.get("elapsed_ms") or {}).get("by_leg") or {}
    by_leg_str = ", ".join(f"{k}={v}" for k, v in sorted(by_leg.items())) or "_none_"
    issues = metrics.get("issues") or {}
    sev = issues.get("by_severity") or {}
    sev_str = ", ".join(f"{k}={v}" for k, v in sorted(sev.items())) or "_none_"
    coverage = metrics.get("coverage") or {}
    req = coverage.get("requirement_coverage") or {}
    ac = coverage.get("acceptance_verification") or {}
    return [
        f"### {name}",
        "",
        f"- feature: {metrics.get('feature_id')}",
        f"- verdict: **{metrics.get('verdict')}**  (failure_class={metrics.get('failure_class')})",
        f"- verifier: {metrics.get('verifier')}  (lane_decision={metrics.get('lane_decision')})",
        f"- elapsed_ms: total={metrics.get('elapsed_ms', {}).get('total', 0)} ({by_leg_str})",
        f"- issues: total={issues.get('total', 0)} ({sev_str})",
        f"- requirement_coverage: {req.get('implemented', 0)}/{req.get('requirements_total', 0)} implemented",
        f"- acceptance_verification: {ac.get('verified', 0)}/{ac.get('acceptance_total', 0)} verified",
        f"- agent_profiles: {agent_str}",
    ]


def _profile_comparison_md(report: Mapping[str, Any]) -> str:
    meta = report.get("meta", {}) if isinstance(report.get("meta"), Mapping) else {}
    profiles = report.get("profiles", {}) if isinstance(report.get("profiles"), Mapping) else {}
    lines: list[str] = [
        "# Profile Comparison",
        "",
        f"- anchor_feature: {meta.get('anchor_feature', '')}",
        f"- profiles_compared: {', '.join(meta.get('profiles_compared', []))}",
        f"- feature_ids: {', '.join(meta.get('feature_ids', []))}",
        f"- canonical: **{meta.get('canonical')}**",
        "",
        "## Profiles",
        "",
    ]
    for name in meta.get("profiles_compared", []) or list(profiles):
        lines.extend(_profile_md(name, profiles.get(name, {})))
        lines.append("")
    lines += ["## Known Gaps", ""]
    for gap in meta.get("known_gaps", []) or []:
        lines.append(f"- {gap}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Public compute / generate seams.
# ---------------------------------------------------------------------------


def _validate_profile_names(profile_names: list[str]) -> None:
    """Require exactly two non-empty, distinct profile names."""
    if len(profile_names) != 2:
        raise ValueError(
            f"compare-profiles takes exactly two profiles (got {profile_names!r}); "
            f"usage: compare-profiles FEATURE-NNN --profiles p1,p2 (§24.2)"
        )
    cleaned = [p.strip() for p in profile_names]
    if any(not p for p in cleaned):
        raise ValueError(
            f"profile names must be non-empty (got {profile_names!r}) (§24.2)"
        )
    if cleaned[0] == cleaned[1]:
        raise ValueError(
            f"the two compared profiles must differ (got {cleaned!r}) (§24.2)"
        )


def compute_profile_comparison(
    repo_root: Path,
    feature_id: str,
    profile_names: list[str],
) -> ProfileComparisonCompute:
    """Project the comparison document without writing (pure read + compute).

    Validates the profile list up front (exactly two distinct profiles) then
    resolves the two parallel feature-runs and projects their metrics. Shared by
    the writer and the dry-run planner.
    """
    _validate_profile_names(profile_names)
    return ProfileComparisonCompute(
        report=_build_report(repo_root, feature_id, profile_names)
    )


def generate_profile_comparison(
    repo_root: Path,
    feature_id: str,
    profile_names: list[str],
) -> ProfileComparisonResult:
    """Project and write ``projections/profile-comparison.{json,md}``.

    Writes both products into the anchor feature's ``projections/`` directory
    (seeded empty at feature-run creation). Non-canonical: no audit append, no
    canonical-state mutation, no wall-clock stamp - byte-recomputable.
    """
    compute = compute_profile_comparison(repo_root, feature_id, profile_names)
    anchor = feature_dir(repo_root, feature_id)
    projections = anchor / "projections"
    projections.mkdir(parents=True, exist_ok=True)
    json_path = projections / PROFILE_COMPARISON_JSON
    md_path = projections / PROFILE_COMPARISON_MD
    write_json(json_path, compute.report)
    md_path.write_text(_profile_comparison_md(compute.report))
    return ProfileComparisonResult(
        feature_id=feature_id,
        projection_json_path=json_path,
        projection_md_path=md_path,
    )
