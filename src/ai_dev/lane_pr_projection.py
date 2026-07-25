"""``ai-dev project-lane-pr`` - lane PR projection (v0.7 ticket 05, ADR-0009 D5/D6).

After a **lane gate passes**, the orchestrator may push the lane branch and
create or update one GitHub PR per gate-passed lane. This amends ADR-0006's
v0.5 boundary (PR creation was human-owned for basic feature-level
projection): lane-level PR creation is now in scope because it is the natural
integration handoff for worktree-backed lanes (ADR-0009 D5).

The PR is a **one-way projection**, not source of truth (ADR-0009 D6 /
invariant #10): GitHub state never writes back into canonical lane status,
task status, issue status, gate verdicts, or feature verdict. The only
canonical write associated with projection is the **lane PR mapping**
(``projections/github/lane-prs.json``) - which lane branch was projected to
which PR, plus enough observed push/create/update metadata to make reruns
idempotent and auditable. Like ``project-github`` (ADR-0006) it is a
side-channel projection, not a gate: it touches no
``feature.status`` / ``verdict`` / ``gate_verdict`` and appends no audit
event.

## Trigger (D5)

The trigger is **lane gate pass**, not Implementer ``proposed_done``. A
model's completion claim alone must not publish a PR. The projection reads
``lane-decision.json`` and refuses loud (§24.2) unless ``decision == "pass"``.

## Idempotency

The lane PR mapping is keyed by lane id: ``lanes[LANE-NNN] -> {pr_number,
pr_url, head_branch, base_branch, remote, projected_at}``. Re-projection
re-pushes the branch (``git push`` is idempotent when up to date) and
**edits the existing PR in place** (``gh pr edit``) rather than creating a
duplicate. No PR existence search by title/label (ADR-0006 D2 rejected that
as fragile). A present-but-corrupt mapping fails loud rather than silently
resetting (which would create duplicates - the silent path D6 forbids).

## Failure handling (D6)

A **pre-flight** checks ``GITHUB_TOKEN`` is set (by name), ``gh`` is on
``PATH``, the rate limit is healthy, and - when ``--base`` is omitted -
detects the repo's default branch. Pre-flight failure -> exit 1, **no
pushes**. With pre-flight passed: push the lane branch, then create or
update the PR. On a mid-stream failure, **stop, keep every successful side
effect + its mapping entry, report what is pending, exit 1**. Re-running
resumes (the push is repeated safely; the PR create retries; an existing
mapping entry is edited). Projection failure is **not** a lane gate failure:
verdicts are never mutated.

## Auth (invariant #11)

``gh`` reads ``GITHUB_TOKEN`` from the environment; this module references
the variable by **name only**, never reads its value, and never persists it.
The mapping carries GH PR numbers/URLs - never a token.

The ``gh`` subprocess is abstracted behind a ``GhRunner`` callable (reused
from ``github_projection``) and the ``git push`` behind a ``GitPushFn`` so
the unit tests drive every code path with mocks and no real network / remote
- real GitHub evidence is the v0.7 capstone (ticket 07).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from ai_dev.github_projection import (
    GITHUB_TOKEN_ENV,
    GhResult,
    GhRunner,
    _gh_available,
    default_gh_runner,
)
from ai_dev.implement_leg import IMPLEMENT_RESULT_JSON, read_lane_entry
from ai_dev.issue_bundle import ISSUE_BUNDLE_JSON
from ai_dev.json_artifact import read_json_object, write_json
from ai_dev.lane_gate import LANE_DECISION_JSON
from ai_dev.lane_worktree import LANE_WORKTREE_FILE
from ai_dev.paths import feature_dir, lane_dir
from ai_dev.shell_verifier import VERIFICATION_DIR, VERIFICATION_REPORT_JSON
from ai_dev.templates import DESIGN_JSON
from ai_dev.timeutil import utc_now_iso

GITHUB_PROJECTION_DIR = "github"
LANE_PR_MAPPING_JSON = "lane-prs.json"

# A markdown header stamped on every pushed PR body so a reader of GitHub can
# trace a projected lane PR back to its canonical feature + lane. Also serves
# as the projection's identity marker (parallel to ``github_projection``'s
# issue/comment marker).
_PROJECTION_MARKER = (
    "<!-- ai-dev lane-pr projection: feature={feature} lane={lane} -->"
)

# The default git remote the lane branch is pushed to. Overridable via
# ``--remote`` so an operator can target a fork upstream or a mirror.
DEFAULT_REMOTE = "origin"


# A function that pushes the lane branch to the remote. Returns a ``GhResult``
# (the push is a side effect, not a ``gh`` call, but reusing the result shape
# avoids a parallel type). ``ok`` is True iff ``git push`` exited 0. The
# default shells out to real ``git`` from the repo root; tests pass a fake.
GitPushFn = Callable[[str, str, Path], GhResult]


def default_git_push(remote: str, branch: str, repo_root: Path) -> GhResult:
    """Push ``branch`` to ``remote`` via ``git push -u`` (the production pusher).

    Runs from ``repo_root`` (the main checkout - any worktree shares the same
    repo, but the main checkout is the unambiguous place to push from). A
    non-zero exit is a captured push failure, not a crash: the caller
    (``project_lane_pr``) routes it through the per-item fail-loud path (D6).
    ``git push`` is idempotent when the branch is already up to date, so a
    rerun after a mid-stream stop re-pushes safely.
    """
    completed = subprocess.run(
        ["git", "push", "-u", remote, branch],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        errors="replace",
    )
    return GhResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


class _ProjectionFailed(RuntimeError):
    """A single ``gh`` side-effect failed mid-stream (D6). Carries item context.

    Used for PR create / update failures (the push goes through the
    ``GitPushFn`` result and is checked directly - it does not raise). The
    orchestrator catches this to stop, keep successful side effects, and
    report what is pending.
    """


# ---------------------------------------------------------------------------
# Result / plan types.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LanePrProjectionResult:
    """Summary of one ``project-lane-pr`` run (possibly partial - see D6)."""

    feature_id: str
    lane_id: str
    mapping_path: Path
    head_branch: str
    base_branch: str | None
    remote: str
    pushed: bool
    pr_number: int | None
    pr_url: str | None
    pr_action: str | None  # "created" | "updated" | None
    # A non-empty ``pending`` list means the run stopped mid-stream (D6): the
    # push and/or the PR create did not complete. Empty on a clean run.
    pending: list[str] = field(default_factory=list)
    # Populated when the run stopped mid-stream or pre-flight failed - the
    # operator-facing reason (surfaced by the CLI as an ``error:`` line).
    failure_reason: str | None = None

    @property
    def complete(self) -> bool:
        """True when the branch was pushed AND the PR was created/updated."""
        return not self.pending and self.failure_reason is None


@dataclass(frozen=True)
class LanePrProjectionPlan:
    """The pure, no-push projection preview (shared by the dry-run planner).

    The create-vs-edit split is what makes a dry-run faithful about idempotency
    (D2/D6): an already-projected lane shows ``would_create_pr=False`` with the
    existing PR number; a new lane shows ``would_create_pr=True``.
    ``base_branch`` is ``None`` on the pure seam - default-branch detection is
    a network call the dry-run does not perform.
    """

    feature_id: str
    lane_id: str
    head_branch: str
    base_ref: str
    base_branch: str | None
    lane_gate_passed: bool
    would_create_pr: bool
    existing_pr_number: int | None


# ---------------------------------------------------------------------------
# Mapping load / save.
# ---------------------------------------------------------------------------


def _lane_pr_mapping_path(repo_root: Path, feature_id: str) -> Path:
    """``projections/github/lane-prs.json`` under the feature root.

    A dedicated mapping (parallel to ``mapping.json``, which records the
    feature-level issue/PR-comment projection) so the two projection concerns
    - feature-level issues+comment vs lane-level PR creation - stay
    independently resumable and never corrupt each other's resume point.
    """
    return (
        feature_dir(repo_root, feature_id)
        / "projections"
        / GITHUB_PROJECTION_DIR
        / LANE_PR_MAPPING_JSON
    )


def _empty_mapping(feature_id: str) -> dict[str, Any]:
    return {"feature": feature_id, "lanes": {}}


def _load_lane_pr_mapping(path: Path, feature_id: str) -> dict[str, Any]:
    """Read the lane-PR mapping. Missing -> fresh; **corrupt -> fail loud**.

    Mirrors ``github_projection._load_mapping``: the mapping is the resume
    point (D6), so a *missing* file is a first projection (fresh map) but a
    *present-but-corrupt* file (unparseable JSON, not an object, or ``lanes``
    not a dict) is fail-loud rather than silently reset - resetting would make
    the next run ``gh pr create`` a duplicate for a lane already projected,
    the exact "looks like success, records a half-state silently" failure D6
    rules out. An operator who genuinely wants to re-project from scratch
    deletes the file.
    """
    if not path.is_file():
        return _empty_mapping(feature_id)
    # Parsed directly rather than via ``read_json_object`` so a non-dict JSON
    # body is *corrupt*, not indistinguishable from missing (mirrors
    # ``github_projection._load_mapping``): the mapping is the resume point, so
    # a present-but-corrupt file must fail loud, not silently reset to fresh
    # (which would make the next run ``gh pr create`` a duplicate).
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(
            f"lane-PR mapping at {path} is corrupt (unparseable JSON: {exc}); "
            f"inspect or delete it before re-projecting (§24.2, ADR-0009 D6)"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"lane-PR mapping at {path} is corrupt (not a JSON object); "
            f"inspect or delete it before re-projecting (§24.2, ADR-0009 D6)"
        )
    lanes = data.get("lanes")
    if not isinstance(lanes, dict):
        raise ValueError(
            f"lane-PR mapping at {path} is corrupt ('lanes' is not an object); "
            f"inspect or delete it before re-projecting (§24.2, ADR-0009 D6)"
        )
    data.setdefault("feature", feature_id)
    data.setdefault("lanes", {})
    return data


def _save_lane_pr_mapping(path: Path, mapping: Mapping[str, Any]) -> None:
    """Persist the lane-PR mapping (the non-deterministic canonical write)."""
    write_json(path, mapping)


def _lane_pr_entry(
    mapping: Mapping[str, Any], lane_id: str
) -> dict[str, Any] | None:
    """The mapping entry for ``lane_id``, or ``None`` (-> create)."""
    lanes = mapping.get("lanes")
    if not isinstance(lanes, Mapping):
        return None
    entry = lanes.get(lane_id)
    if not isinstance(entry, dict):
        return None
    # Only a real PR number makes the entry an "edit" target; a present entry
    # without a number is a half-state from a prior failed create - treat it
    # as "create" so the resume path retries rather than editing nothing.
    number = entry.get("pr_number")
    if not isinstance(number, int) or isinstance(number, bool):
        return None
    return entry


# ---------------------------------------------------------------------------
# Lane-gate-pass requirement (D5) + worktree read.
# ---------------------------------------------------------------------------


def _require_lane_gate_passed_and_worktree(
    repo_root: Path, feature_id: str, lane_id: str
) -> tuple[str, str]:
    """Read ``lane-decision.json`` (require pass) + ``worktree.json`` (branch).

    Returns ``(head_branch, base_ref)``. Raises ``ValueError`` (§24.2) when:
    the lane-decision is missing or not ``"pass"`` (ADR-0009 D5 - the trigger
    is lane gate pass, **not** Implementer ``proposed_done``; a model's
    completion claim alone must not publish a PR), or the worktree record is
    missing/malformed (no branch to push). Shared by the pure compute seam and
    the real projection so the gate-pass check can never diverge between them.
    """
    lane_root = lane_dir(repo_root, feature_id, lane_id)
    decision_doc = read_json_object(lane_root / LANE_DECISION_JSON)
    if decision_doc is None:
        raise ValueError(
            f"lane {lane_id!r} has no lane-decision.json; run `ai-dev "
            f"lane-gate {feature_id} {lane_id}` first (ADR-0009 D5 - the "
            f"Implementer's proposed_done alone is insufficient to project a PR)"
        )
    if not isinstance(decision_doc, dict):
        raise ValueError(
            f"lane-decision.json at {lane_root / LANE_DECISION_JSON} is not a "
            f"JSON object (§24.2)"
        )
    verdict = decision_doc.get("decision")
    if verdict != "pass":
        raise ValueError(
            f"lane {lane_id!r} gate verdict is {verdict!r}, not 'pass'; only a "
            f"PASSED lane gate may project a PR (ADR-0009 D5 - the Implementer's "
            f"proposed_done alone is insufficient)"
        )
    worktree = read_json_object(lane_root / LANE_WORKTREE_FILE)
    if worktree is None or not isinstance(worktree, dict):
        raise ValueError(
            f"lane {lane_id!r} has no worktree.json at {lane_root / LANE_WORKTREE_FILE}; "
            f"create a lane worktree before projecting a PR (§24.2)"
        )
    branch = worktree.get("branch")
    base_ref = worktree.get("base_ref")
    if not isinstance(branch, str) or not branch:
        raise ValueError(
            f"worktree.json for lane {lane_id!r} has no 'branch' (§24.2)"
        )
    if not isinstance(base_ref, str) or not base_ref:
        raise ValueError(
            f"worktree.json for lane {lane_id!r} has no 'base_ref' (§24.2)"
        )
    return branch, base_ref


# ---------------------------------------------------------------------------
# Public compute seam (no network, no write).
# ---------------------------------------------------------------------------


def compute_lane_pr_plan(
    repo_root: Path, feature_id: str, lane_id: str
) -> LanePrProjectionPlan:
    """Compute what a lane PR projection would push, with no network and no write.

    The public pure seam the dry-run planner shares (mirroring
    ``compute_github_plan``): enforces the lane-gate-pass precondition (D5),
    reads the lane's worktree branch, and reads the mapping to split create vs
    edit. Raises ``ValueError`` on a missing feature, a missing/non-passing
    lane-decision, a missing worktree, or a corrupt mapping (fail loud, §24.2).
    """
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id!r} not found under {repo_root}")
    head_branch, base_ref = _require_lane_gate_passed_and_worktree(
        repo_root, feature_id, lane_id
    )
    mapping = _load_lane_pr_mapping(_lane_pr_mapping_path(repo_root, feature_id), feature_id)
    existing = _lane_pr_entry(mapping, lane_id)
    return LanePrProjectionPlan(
        feature_id=feature_id,
        lane_id=lane_id,
        head_branch=head_branch,
        base_ref=base_ref,
        base_branch=None,  # default-branch detection is a network call
        lane_gate_passed=True,  # the require above raised if not
        would_create_pr=existing is None,
        existing_pr_number=existing["pr_number"] if existing else None,
    )


# ---------------------------------------------------------------------------
# PR body builder: lane/feature/task ids, REQ/AC/DES, gate verdict,
# verification summary, issue summary, worktree metadata, artifact pointers.
# ---------------------------------------------------------------------------


def _related_des_ids(
    feature_root: Path, req_ids: list[str]
) -> list[str]:
    """Best-effort DES-NNN ids whose design elements realize the lane's REQs.

    Reads ``02-design.json``'s ``requirement_mapping`` (each entry carries a
    ``requirement`` REQ-NNN and the resolved ``design_elements`` DES-NNN ids,
    per ADR-0008 D2) and collects the DES ids of mappings whose requirement is
    one of the lane's declared REQs. Empty when the design artifact is absent
    or has no mapping - the PR body then honestly reports no DES traceability
    rather than fabricating one. Sorted + deduped for stable output.
    """
    if not req_ids:
        return []
    design_doc = read_json_object(feature_root / DESIGN_JSON)
    if not isinstance(design_doc, dict):
        return []
    req_set = set(req_ids)
    found: list[str] = []
    seen: set[str] = set()
    for entry in design_doc.get("requirement_mapping", []) or []:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("requirement") in req_set:
            for des in entry.get("design_elements", []) or []:
                if isinstance(des, str) and des not in seen:
                    seen.add(des)
                    found.append(des)
    return sorted(found)


def _task_ids(
    lane_tasks: list[str], implement_result: Mapping[str, Any] | None
) -> list[str]:
    """Lane task ids: the lane-graph-declared tasks union the implement-result's.

    The lane graph's ``tasks`` is the canonical assignment (may name tasks the
    implementer has not yet proposed done); the implement-result's ``tasks[]``
    carries the ids the Implementer actually worked. Unioning both gives the
    PR reader the full lane scope, deduped + sorted for stable output.
    """
    seen: set[str] = set()
    combined: list[str] = []
    for tid in list(lane_tasks):
        if isinstance(tid, str) and tid not in seen:
            seen.add(tid)
            combined.append(tid)
    if isinstance(implement_result, Mapping):
        for task in implement_result.get("tasks", []) or []:
            if isinstance(task, Mapping):
                impl_tid: Any = task.get("id")
                if isinstance(impl_tid, str) and impl_tid not in seen:
                    seen.add(impl_tid)
                    combined.append(impl_tid)
    return sorted(combined)


def _issue_summary(bundle: Mapping[str, Any] | None) -> dict[str, Any]:
    """Per-source + per-severity issue counts from the lane issue bundle."""
    summary: dict[str, Any] = {"total": 0, "by_severity": {}, "by_source": {}}
    if not isinstance(bundle, Mapping):
        return summary
    issues = bundle.get("issues")
    if not isinstance(issues, list):
        return summary
    summary["total"] = len(issues)
    for issue in issues:
        if not isinstance(issue, Mapping):
            continue
        sev = str(issue.get("severity") or "unspecified")
        src = str(issue.get("source") or "unspecified")
        summary["by_severity"][sev] = summary["by_severity"].get(sev, 0) + 1
        summary["by_source"][src] = summary["by_source"].get(src, 0) + 1
    return summary


def _lane_pr_title(feature_id: str, lane_id: str, purpose: str | None) -> str:
    """``[FEATURE-NNN/LANE-NNN] <purpose>`` - stable, findable PR title."""
    head = f"[{feature_id}/{lane_id}]"
    return f"{head} {purpose}" if purpose else head


def _lane_pr_body(
    repo_root: Path, feature_id: str, lane_id: str, *, head_branch: str,
    base_branch: str | None, remote: str,
) -> str:
    """Render the lane PR body from the lane's canonical artifacts (D5).

    Pure of side effects: reads implement-result / lane-decision /
    verification-report / issue-bundle / worktree.json / 02-design.json /
    04-lane-graph and renders markdown. Every section degrades honestly when
    an artifact is absent (the projection still runs - a missing optional
    artifact is reported as ``_(none)_`` rather than failing the projection).
    """
    feature_root = feature_dir(repo_root, feature_id)
    lane_root = lane_dir(repo_root, feature_id, lane_id)
    marker = _PROJECTION_MARKER.format(feature=feature_id, lane=lane_id)

    lane_entry = _safe_lane_entry(repo_root, feature_id, lane_id)
    purpose = lane_entry.purpose if lane_entry is not None else None
    lane_task_ids = _task_ids(
        lane_entry.tasks if lane_entry is not None else [], None
    )

    implement_result = read_json_object(lane_root / IMPLEMENT_RESULT_JSON)
    if isinstance(implement_result, Mapping):
        req_ids = [str(r) for r in implement_result.get("related_requirements", []) or []
                   if isinstance(r, str)]
        ac_ids = [str(a) for a in implement_result.get("related_acceptance_criteria", []) or []
                  if isinstance(a, str)]
        impl_summary = implement_result.get("summary")
        impl_status = implement_result.get("status")
        meta = implement_result.get("run_metadata") or {}
        task_ids = _task_ids(lane_task_ids, implement_result)
    else:
        req_ids, ac_ids, impl_summary, impl_status, meta, task_ids = [], [], None, None, {}, lane_task_ids

    des_ids = _related_des_ids(feature_root, req_ids)

    decision = read_json_object(lane_root / LANE_DECISION_JSON)
    verdict = decision.get("decision") if isinstance(decision, Mapping) else None
    failed_conditions: list[str] = []
    if isinstance(decision, Mapping):
        for c in decision.get("conditions", []) or []:
            if isinstance(c, Mapping) and not c.get("passed"):
                failed_conditions.append(str(c.get("name", "")))

    verification = read_json_object(
        lane_root / VERIFICATION_DIR / VERIFICATION_REPORT_JSON
    )
    if isinstance(verification, Mapping):
        v_verdict = verification.get("verdict")
        v_passed = verification.get("passed_count", 0)
        v_total = verification.get("command_count", 0)
    else:
        v_verdict, v_passed, v_total = None, 0, 0

    bundle = read_json_object(lane_root / ISSUE_BUNDLE_JSON)
    issue_summary = _issue_summary(bundle if isinstance(bundle, Mapping) else None)

    worktree = read_json_object(lane_root / LANE_WORKTREE_FILE)
    base_ref = worktree.get("base_ref") if isinstance(worktree, Mapping) else None
    wt_path = worktree.get("path") if isinstance(worktree, Mapping) else None

    profile = meta.get("profile") if isinstance(meta, Mapping) else None
    cli = meta.get("cli") if isinstance(meta, Mapping) else None
    backend = meta.get("backend") if isinstance(meta, Mapping) else None
    model = meta.get("model") if isinstance(meta, Mapping) else None

    rel = lambda p: f"lanes/{lane_id}/{p}"
    lines: list[str] = [
        marker,
        f"# Lane PR - {feature_id} / {lane_id}",
        "",
        f"- feature: {feature_id}",
        f"- lane: {lane_id}",
        f"- purpose: {purpose or '_(unspecified)_'}",
        f"- lane gate verdict: **{verdict or 'unknown'}**"
        + (f" (failed: {', '.join(failed_conditions)})" if failed_conditions else ""),
        "",
        "## Tasks",
        "",
        ", ".join(task_ids) if task_ids else "_(none declared)_",
        "",
        "## Related requirements / acceptance / design",
        "",
        f"- requirements: {req_ids or '_(none declared)_'}",
        f"- acceptance_criteria: {ac_ids or '_(none declared)_'}",
        f"- design_elements: {des_ids or '_(none mapped)_'}",
        "",
        "## Verification summary",
        "",
        f"- verdict: **{v_verdict or 'unknown'}** ({v_passed}/{v_total} commands passed)",
        "",
        "## Issue summary",
        "",
        f"- total: {issue_summary['total']}",
        f"- by_severity: {issue_summary['by_severity'] or '_(none)_'}",
        f"- by_source: {issue_summary['by_source'] or '_(none)_'}",
        "",
        "## Branch / worktree metadata",
        "",
        f"- head branch: `{head_branch}`",
        f"- base branch: `{base_branch or '_(default - detected at projection time)_'}`",
        f"- base ref: `{base_ref or '_(unknown)_'}`",
        f"- worktree path: `{wt_path or '_(unknown)_'}`",
        f"- remote: `{remote}`",
        "",
        "## Run metadata",
        "",
        f"- profile: {profile or '_(unknown)_'}",
        f"- cli: {cli or '_(unknown)_'} / backend: {backend or '_(unknown)_'} / model: {model or '_(unknown)_'}",
        f"- implement status: {impl_status or '_(unknown)_'}",
        f"- implement summary: {impl_summary or '_(none)_'}",
        "",
        "## Canonical artifact pointers",
        "",
        "This PR is a *projection* of the lane's canonical artifacts under the feature run:",
        "",
        f"- `{rel(LANE_DECISION_JSON)}` - lane gate decision",
        f"- `{rel(IMPLEMENT_RESULT_JSON)}` - implement result rollup",
        f"- `{rel(VERIFICATION_DIR)}/{VERIFICATION_REPORT_JSON}` - verification report",
        f"- `{rel(ISSUE_BUNDLE_JSON)}` - issue bundle",
        f"- `{rel(LANE_WORKTREE_FILE)}` - worktree metadata",
        f"- `{rel('diff.patch')}` - lane diff against base ref",
        f"- `{rel('commits.log')}` - lane commits against base ref",
        f"- `{rel('metadata.json')}` - lane run metadata",
        "",
        "## Projection disclaimer",
        "",
        "This PR is a one-way GitHub projection (ADR-0009 D6): GitHub state never "
        "writes back to canonical lane/task/feature status or verdicts. v0.7 does "
        "NOT perform automatic merge, semantic conflict resolution, or Merge "
        "Coordination - a passing lane gate projects a PR but does not integrate "
        "the branch. Human integration (or a future Merge Coordinator) is required "
        "before the product branch is coherent.",
        "",
    ]
    return "\n".join(lines)


def _safe_lane_entry(
    repo_root: Path, feature_id: str, lane_id: str
) -> Any:
    """Read the lane graph entry, returning ``None`` if unreadable.

    ``read_lane_entry`` raises on a missing/malformed lane graph; the PR body is
    a projection and must not fail the whole projection over a missing purpose
    string, so degrade to ``None`` (the body reports ``_(unspecified)_``).
    """
    feature_root = feature_dir(repo_root, feature_id)
    try:
        return read_lane_entry(feature_root, lane_id)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Output parsing - pull PR number/URL out of what ``gh`` prints (no token).
# ---------------------------------------------------------------------------


def _parse_pr_number(stdout: str) -> int:
    """The PR number from ``gh pr create``'s printed URL.

    ``gh pr create`` prints ``https://github.com/<owner>/<repo>/pull/<n>``. The
    trailing integer is the number. Fails loud if unparseable - a create that
    printed no recognizable URL means the push may have landed but the PR is
    unrecorded, and a bad mapping entry would corrupt the resume point.
    """
    match = re.search(r"/pull/(\d+)", stdout)
    if not match:
        raise _ProjectionFailed(
            f"could not parse PR number from `gh pr create` output: "
            f"{stdout.strip()!r}"
        )
    return int(match.group(1))


# ---------------------------------------------------------------------------
# Pre-flight (D6).
# ---------------------------------------------------------------------------


def _preflight_lane_pr(
    *,
    env: Mapping[str, str],
    gh_runner: GhRunner,
    gh_available: Callable[[], bool],
    base_branch: str | None,
) -> str:
    """Verify the §24.2-style preconditions before any push (D6: no pushes on fail).

    Raises ``ValueError`` (surfaced as a clean ``error:`` + exit 1 by the CLI)
    when: the token env var is unset, ``gh`` is not on ``PATH``, the rate-limit
    probe fails / is exhausted, or default-branch detection fails (when
    ``--base`` was omitted). All checked *before* the first push so a failed
    pre-flight leaves GitHub (and the mapping) untouched. Returns the resolved
    base branch (the given one, or the detected default).
    """
    if env.get(GITHUB_TOKEN_ENV) in (None, ""):
        raise ValueError(
            f"{GITHUB_TOKEN_ENV} is not set in the environment - `gh` reads it "
            f"for auth; export it before projecting (invariant #11, §24.2)"
        )
    if not gh_available():
        raise ValueError(
            "`gh` CLI not found on PATH - install it (https://cli.github.com) "
            "before projecting (§24.2)"
        )
    rate = gh_runner(["api", "rate_limit", "--jq", ".resources.core.remaining"])
    if not rate.ok:
        raise ValueError(
            f"GitHub rate-limit probe failed (exit {rate.returncode}): "
            f"{rate.stderr.strip() or 'no detail'} (§24.2)"
        )
    try:
        remaining = int(rate.stdout.strip())
    except ValueError as exc:
        raise ValueError(
            f"could not parse rate-limit remaining ({rate.stdout.strip()!r}); "
            f"aborting before any push (§24.2)"
        ) from exc
    if remaining <= 0:
        raise ValueError(
            f"GitHub rate limit exhausted (core.remaining={remaining}); "
            f"retry later (§24.2)"
        )
    if base_branch is None:
        view = gh_runner(
            ["repo", "view", "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"]
        )
        if not view.ok:
            raise ValueError(
                f"could not detect the repo default branch via `gh repo view`: "
                f"{view.stderr.strip() or 'no detail'}; pass --base explicitly (§24.2)"
            )
        detected = view.stdout.strip()
        if not detected:
            raise ValueError(
                "the repo default branch probe returned an empty name; "
                "pass --base explicitly (§24.2)"
            )
        base_branch = detected
    return base_branch


# ---------------------------------------------------------------------------
# Per-item side effects (D6: fail-loud, keep successes, resume from mapping).
# ---------------------------------------------------------------------------


def _run_gh_or_raise(gh_runner: GhRunner, argv: list[str], what: str) -> GhResult:
    """Run one ``gh`` call; raise ``_ProjectionFailed`` on a non-zero exit (D6).

    Parallel to ``github_projection._run_or_raise``: the "failed (exit N):
    <stderr>" shape spelled once, with ``what`` naming the item for the message.
    """
    result = gh_runner(argv)
    if not result.ok:
        detail = result.stderr.strip() or "no detail"
        raise _ProjectionFailed(f"{what} failed (exit {result.returncode}): {detail}")
    return result


def _create_lane_pr(
    *,
    gh_runner: GhRunner,
    head: str,
    base: str,
    title: str,
    body: str,
) -> tuple[int, str]:
    """Create the lane PR via ``gh pr create`` and parse its number + URL.

    ``--head`` is the lane branch (just pushed to the remote); ``--base`` is
    the resolved base branch. Returns ``(pr_number, pr_url)``. A non-zero exit
    or an unparseable URL both raise ``_ProjectionFailed`` so the orchestrator
    reports the PR as pending (the push is kept) and a rerun retries.
    """
    result = _run_gh_or_raise(
        gh_runner,
        ["pr", "create", "--base", base, "--head", head, "--title", title, "--body", body],
        f"gh pr create --head {head} --base {base}",
    )
    number = _parse_pr_number(result.stdout)
    url = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    return number, url


def _update_lane_pr(
    *,
    gh_runner: GhRunner,
    pr_number: int,
    title: str,
    body: str,
) -> None:
    """Update an existing lane PR's title + body in place (``gh pr edit``).

    The re-computed body tracks the lane's current canonical artifacts (a
    re-projection after a re-run reflects the latest gate verdict / verification
    / issues, ADR-0003 re-computable projection tracking). Raises
    ``_ProjectionFailed`` on a non-zero exit so the orchestrator reports the
    update as pending.
    """
    _run_gh_or_raise(
        gh_runner,
        ["pr", "edit", str(pr_number), "--title", title, "--body", body],
        f"gh pr edit #{pr_number}",
    )


# ---------------------------------------------------------------------------
# Public project seam.
# ---------------------------------------------------------------------------


def project_lane_pr(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    *,
    base_branch: str | None = None,
    remote: str = DEFAULT_REMOTE,
    gh_runner: GhRunner | None = None,
    git_push: GitPushFn | None = None,
    env: Mapping[str, str] | None = None,
    gh_available: Callable[[], bool] | None = None,
) -> LanePrProjectionResult:
    """Push the lane branch and create/update its GitHub PR (ADR-0009 D5/D6).

    Enforces the lane-gate-pass trigger (D5 - raises ``ValueError`` if the lane
    gate has not passed; ``proposed_done`` alone is insufficient). With the gate
    passed: pre-flight (token / ``gh`` / rate-limit / base-branch detect), push
    the lane branch to ``remote``, then create (first projection) or edit
    (re-projection) the PR. The mapping (``lane-prs.json``) is the resume point:
    a re-run re-pushes (idempotent) and edits the stored PR.

    Pre-flight failure or a mid-stream push/PR failure both return a result
    whose ``failure_reason`` is set (the CLI turns that into ``error:`` + exit
    1); successful side effects + their mapping entries are kept either way
    (D6). The projection touches no ``feature.status`` / ``verdict`` /
    ``gate_verdict`` and appends no audit event - it is a one-way projection
    (invariant #10).
    """
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id!r} not found under {repo_root}")
    # Resolve lazily from module globals so a test (or operator) can patch
    # ``default_gh_runner`` / ``default_git_push`` / ``_gh_available`` at the
    # module level and have the CLI path pick the fakes up.
    if gh_runner is None:
        gh_runner = default_gh_runner
    if git_push is None:
        git_push = default_git_push
    if gh_available is None:
        gh_available = _gh_available
    live_env = env if env is not None else os.environ

    # D5 gate-pass + worktree precondition (raises ValueError -> CLI exit 1).
    # Shared with ``compute_lane_pr_plan`` via ``_require_lane_gate_passed_and_worktree``
    # so the gate-pass check can never diverge between the pure seam and the
    # real projection. The mapping is loaded once here (not via the compute
    # seam) so the create/edit decision and the update-time entry read share
    # one read.
    head_branch, _base_ref = _require_lane_gate_passed_and_worktree(
        repo_root, feature_id, lane_id
    )
    mapping_path = _lane_pr_mapping_path(repo_root, feature_id)
    mapping = _load_lane_pr_mapping(mapping_path, feature_id)
    existing = _lane_pr_entry(mapping, lane_id)

    # Pre-flight (D6): no pushes on failure.
    try:
        resolved_base = _preflight_lane_pr(
            env=live_env,
            gh_runner=gh_runner,
            gh_available=gh_available,
            base_branch=base_branch,
        )
    except ValueError as exc:
        return LanePrProjectionResult(
            feature_id=feature_id,
            lane_id=lane_id,
            mapping_path=mapping_path,
            head_branch=head_branch,
            base_branch=None,
            remote=remote,
            pushed=False,
            pr_number=None,
            pr_url=None,
            pr_action=None,
            pending=["preflight"],
            failure_reason=str(exc),
        )

    # Push the lane branch (D5). Failure -> keep nothing (no PR yet), report
    # push pending. The mapping is not touched (no PR was created).
    push_result = git_push(remote, head_branch, repo_root)
    if not push_result.ok:
        detail = push_result.stderr.strip() or "no detail"
        return LanePrProjectionResult(
            feature_id=feature_id,
            lane_id=lane_id,
            mapping_path=mapping_path,
            head_branch=head_branch,
            base_branch=resolved_base,
            remote=remote,
            pushed=False,
            pr_number=None,
            pr_url=None,
            pr_action=None,
            pending=["push"],
            failure_reason=f"git push -u {remote} {head_branch} failed "
            f"(exit {push_result.returncode}): {detail}",
        )

    # Create or update the PR. The body is re-computed from canonical artifacts
    # every run (re-computable projection, ADR-0003 tracking).
    entry = _safe_lane_entry(repo_root, feature_id, lane_id)
    title = _lane_pr_title(feature_id, lane_id, entry.purpose if entry else None)
    body = _lane_pr_body(
        repo_root,
        feature_id,
        lane_id,
        head_branch=head_branch,
        base_branch=resolved_base,
        remote=remote,
    )
    pr_number: int | None
    pr_url: str | None
    try:
        if existing is None:
            pr_number, pr_url = _create_lane_pr(
                gh_runner=gh_runner, head=head_branch, base=resolved_base,
                title=title, body=body,
            )
            pr_action = "created"
        else:
            _update_lane_pr(
                gh_runner=gh_runner,
                pr_number=existing["pr_number"],
                title=title,
                body=body,
            )
            pr_number = existing["pr_number"]
            pr_url = existing.get("pr_url")
            pr_action = "updated"
    except _ProjectionFailed as exc:
        # Push succeeded (kept) but the PR create/update failed. No mapping
        # entry is recorded on a create failure (there is no PR number); an
        # update failure leaves the prior mapping entry intact. Either way the
        # PR is reported pending so a re-run resumes (push idempotent + retry).
        return LanePrProjectionResult(
            feature_id=feature_id,
            lane_id=lane_id,
            mapping_path=mapping_path,
            head_branch=head_branch,
            base_branch=resolved_base,
            remote=remote,
            pushed=True,
            pr_number=None,
            pr_url=None,
            pr_action=None,
            pending=["pr"],
            failure_reason=str(exc),
        )

    # Record the mapping immediately after a successful PR create/update - the
    # one canonical write associated with projection (D6). Written before the
    # return so a subsequent failure (none here, but defensively) cannot leave
    # a PR on GitHub with no canonical record.
    mapping.setdefault("lanes", {})[lane_id] = {
        "pr_number": pr_number,
        "pr_url": pr_url,
        "head_branch": head_branch,
        "base_branch": resolved_base,
        "remote": remote,
        "projected_at": utc_now_iso(),
    }
    _save_lane_pr_mapping(mapping_path, mapping)

    return LanePrProjectionResult(
        feature_id=feature_id,
        lane_id=lane_id,
        mapping_path=mapping_path,
        head_branch=head_branch,
        base_branch=resolved_base,
        remote=remote,
        pushed=True,
        pr_number=pr_number,
        pr_url=pr_url,
        pr_action=pr_action,
    )
