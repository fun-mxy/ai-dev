"""``ai-dev project-github`` — push canonical issues to GitHub + PR comment (ADR-0006).

The **basic GitHub projection** (v0.5 ticket 07): a one-way push of canonical
``issues/ISSUE-NNN`` -> GitHub Issues (via ``gh issue create`` / ``gh issue
edit``) and an optional post/update of ``final-report.md`` as a comment on a PR
the operator points at. It is the §27.1 #4 "basic GitHub projection" item,
deliberately *basic* — issues + one PR comment only (ADR-0006 D1). PR creation
stays human-owned (§28 "auto-PR" non-goal intact); the orchestrator never opens
a PR, only comments on one.

Idempotency (D2): a canonical ``projections/github/mapping.json`` records
``ISSUE-NNN -> GH issue number`` and ``feature -> PR``. Re-projection reads the
map: an existing GH issue is **edited in place**; a new one is **created**. No
duplicates, safe to re-run. ``--pr <N>`` on first projection is stored as
``feature -> PR``; without it projection is **issues-only** (D3).

Failure handling (D4): a **pre-flight** checks the token env var is set
(``GITHUB_TOKEN``), ``gh`` is on ``PATH``, the PR exists (if ``--pr`` given),
and the rate limit is healthy. Pre-flight failure -> exit 1, **no pushes**. With
pre-flight passed, each item (issue, then the PR comment) is pushed one at a
time; on the first mid-stream failure the run **stops, keeps every successful
push + its mapping entry, reports what is pending, and exits 1**. Re-running
resumes from the mapping — already-pushed issues are edited, not re-created.

Auth (D5 / invariant #11): ``gh`` reads ``GITHUB_TOKEN`` from the environment;
this module references the variable by **name only**, never reads its value, and
never persists it. The mapping carries GH numbers/ids — never a token.

.. note:: **Non-deterministic canonical write.** ``mapping.json`` is the *first*
   canonical state written as a side-effect of a network call — every prior
   canonical write in the runtime is deterministic. This is accepted (ADR-0006
   Consequences) and bounded by the pre-flight + per-item-fail-loud +
   re-run-resumes model: the mapping *is* the resumption point, so a half-done
   projection is recoverable rather than corrupting. Projection is strictly
   one-way (invariant #10): GitHub state never writes back to canonical
   artifacts — the mapping records *what was projected where*, it does not feed
   back into ``issues/`` / decisions / final-report. Like ``final-report`` and
   ``compare-profiles`` (ADR-0003) it is a side-channel projection, not a gate:
   it touches no ``feature.status`` / ``verdict`` and appends no audit event
   (the audit log stays a record of deterministic events; a network side-effect
   has no place in it).

The ``gh`` subprocess is abstracted behind a ``GhRunner`` callable so the unit
tests drive every code path (create / update / pre-flight fail / mid-stream
fail + resume / PR comment) with a mocked runner and no real network — real
GitHub evidence is ticket 08.
"""

from __future__ import annotations

import os
import re
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from ai_dev.final_report import FINAL_REPORT_JSON, FINAL_REPORT_MD
from ai_dev.issue_bundle import ISSUES_DIR
from ai_dev.json_artifact import read_json_object, write_json
from ai_dev.paths import feature_dir

GITHUB_PROJECTION_DIR = "github"
MAPPING_JSON = "mapping.json"

# The env var `gh` reads for auth (invariant #11: referenced by name only).
GITHUB_TOKEN_ENV = "GITHUB_TOKEN"

# A markdown header stamped on every pushed issue/comment body so a reader of
# GitHub can trace a projected item back to its canonical source id. Also serves
# as the comment's identity marker.
_PROJECTION_MARKER = "<!-- ai-dev projection: feature={feature} -->"


# ---------------------------------------------------------------------------
# gh runner abstraction (so tests inject a fake; no real network).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GhResult:
    """One ``gh`` invocation's captured outcome.

    ``returncode`` is ``gh``'s process exit; ``stdout`` / ``stderr`` are captured
    text. The projection reads GH numbers/ids out of ``stdout`` (the issue URL
    / comment URL ``gh`` prints) and never persists a token.
    """

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


# A function that runs ``gh <argv...>`` and returns the captured result. The
# default shells out to the real ``gh`` (reading ``GITHUB_TOKEN`` from the live
# env, never inlined); tests pass a scripted fake.
GhRunner = Callable[[list[str]], GhResult]


def default_gh_runner(argv: list[str]) -> GhResult:
    """Run ``gh`` for real, capturing stdout/stderr (the production runner).

    Inherits the live environment so ``gh`` reads ``GITHUB_TOKEN`` (referenced
    by name only — the value is never touched by this module). A non-zero exit
    is a captured push failure, not a crash: the caller (``project_github``)
    surfaces it through the per-item fail-loud path.
    """
    completed = subprocess.run(
        ["gh", *argv],
        capture_output=True,
        text=True,
        errors="replace",
        env=os.environ.copy(),
    )
    return GhResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _gh_available() -> bool:
    """Whether the ``gh`` binary is resolvable on ``PATH`` (no spawn)."""
    from shutil import which

    return which("gh") is not None


# ---------------------------------------------------------------------------
# Result types.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IssuePush:
    """One issue's push outcome."""

    issue_id: str
    action: str  # "created" | "updated"
    number: int


@dataclass(frozen=True)
class GithubProjectionResult:
    """Summary of one ``project-github`` run (possibly partial — see D4)."""

    feature_id: str
    mapping_path: Path
    issues: list[IssuePush] = field(default_factory=list)
    pr_number: int | None = None
    pr_comment_action: str | None = None  # "created" | "updated" | None
    # A non-empty ``pending`` list means the run stopped mid-stream (D4): these
    # items were never pushed. Empty on a clean run.
    pending: list[str] = field(default_factory=list)
    # Populated when the run stopped mid-stream or pre-flight failed — the
    # operator-facing reason (surfaced by the CLI as an ``error:`` line).
    failure_reason: str | None = None

    @property
    def complete(self) -> bool:
        """True when every planned item was pushed (no mid-stream stop)."""
        return not self.pending and self.failure_reason is None


# ---------------------------------------------------------------------------
# Mapping load / save.
# ---------------------------------------------------------------------------


def _mapping_path(repo_root: Path, feature_id: str) -> Path:
    """``projections/github/mapping.json`` under the feature root."""
    return (
        feature_dir(repo_root, feature_id)
        / "projections"
        / GITHUB_PROJECTION_DIR
        / MAPPING_JSON
    )


def _empty_mapping(feature_id: str) -> dict[str, Any]:
    return {"feature": feature_id, "pr_number": None, "pr_comment_id": None, "issues": {}}


def _load_mapping(path: Path, feature_id: str) -> dict[str, Any]:
    """Read the mapping. Missing -> fresh; **corrupt -> fail loud** (§24.2, D4).

    The mapping is the resumption point (D4): a present, well-formed map means
    "edit in place" for known issues. A *missing* file is a first projection, so
    a fresh map is returned. A *present-but-corrupt* map (unparseable JSON, not
    a JSON object, or ``issues`` not a dict) is fail-loud rather than silently
    reset: resetting would make the next run ``gh issue create`` duplicates for
    items already pushed — the exact "looks like success, records a half-state
    silently" failure D4's "Rejected - warn-and-continue" rules out, and §24.2
    forbids. An operator who genuinely wants to re-project from scratch deletes
    the file. (Parsed directly rather than via ``read_json_object`` so a non-dict
    JSON body is *corrupt*, not indistinguishable from missing.)
    """
    if not path.is_file():
        return _empty_mapping(feature_id)
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(
            f"mapping at {path} is corrupt (unparseable JSON: {exc}); inspect "
            f"or delete it before re-projecting (§24.2)"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"mapping at {path} is corrupt (not a JSON object); inspect or "
            f"delete it before re-projecting (§24.2)"
        )
    issues = data.get("issues")
    if not isinstance(issues, dict):
        raise ValueError(
            f"mapping at {path} is corrupt ('issues' is not an object); inspect "
            f"or delete it before re-projecting (§24.2)"
        )
    data.setdefault("feature", feature_id)
    data.setdefault("pr_number", None)
    data.setdefault("pr_comment_id", None)
    return data


def _save_mapping(path: Path, mapping: Mapping[str, Any]) -> None:
    """Persist the mapping (the non-deterministic canonical write)."""
    write_json(path, mapping)


@dataclass(frozen=True)
class GithubProjectionPlan:
    """The pure, no-push projection preview (shared by the dry-run planner).

    The create-vs-edit split is what makes a dry-run faithful about idempotency
    (D2): an already-projected issue shows under ``would_edit``, a new one under
    ``would_create``. ``pr_number`` is the *effective* PR (``--pr`` overrides the
    stored mapping; without it the stored value, if any, applies).
    """

    feature_id: str
    issues_total: int
    would_create: list[str]
    would_edit: list[str]
    pr_number: int | None


def compute_github_plan(
    repo_root: Path, feature_id: str, pr_number: int | None
) -> GithubProjectionPlan:
    """Compute what a projection would push, with no network and no write.

    The public pure seam the dry-run planner shares (mirroring the
    ``compute_final_report`` / ``compute_profile_comparison`` pattern): reads the
    feature's issues + the mapping to split create vs edit, and resolves the
    effective PR (``--pr`` overrides the stored mapping). Raises ``ValueError``
    on a missing feature or a corrupt mapping (fail loud, §24.2); a missing
    mapping is a first projection (all create).
    """
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id!r} not found under {repo_root}")
    mapping_path = _mapping_path(repo_root, feature_id)
    mapping = _load_mapping(mapping_path, feature_id)
    issues = _load_issues(feature_root)
    would_create: list[str] = []
    would_edit: list[str] = []
    for issue in issues:
        issue_id = issue["id"]
        if _issue_number(mapping, issue_id) is not None:
            would_edit.append(issue_id)
        else:
            would_create.append(issue_id)
    effective_pr = pr_number if pr_number is not None else mapping.get("pr_number")
    if not isinstance(effective_pr, int) or isinstance(effective_pr, bool):
        effective_pr = None
    return GithubProjectionPlan(
        feature_id=feature_id,
        issues_total=len(issues),
        would_create=would_create,
        would_edit=would_edit,
        pr_number=effective_pr,
    )


# ---------------------------------------------------------------------------
# Readers: issues + final-report body.
# ---------------------------------------------------------------------------


def _load_issues(feature_root: Path) -> list[dict[str, Any]]:
    """Return the feature's canonical ``issues/ISSUE-NNN.json`` records, sorted.

    Sorted by id so projection order is stable across re-runs (a re-run must
    push the same items in the same order for "edit the ones already pushed,
    then resume at the first unpushed one" to be deterministic).
    """
    issue_root = feature_root / ISSUES_DIR
    if not issue_root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(issue_root.glob("ISSUE-*.json")):
        issue = read_json_object(path)
        if isinstance(issue, dict) and issue.get("id"):
            out.append(issue)
    return out


def _issue_number(mapping: Mapping[str, Any], issue_id: str) -> int | None:
    """The GH issue number already mapped for ``issue_id``, or ``None`` (create)."""
    issues = mapping.get("issues")
    if not isinstance(issues, Mapping):
        return None
    value = issues.get(issue_id)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _issue_body(feature_id: str, issue: Mapping[str, Any]) -> str:
    """Render the GitHub issue body from the canonical issue record.

    Markdown (GitHub renders it): the projection marker (traceability), the
    severity + source, then the description / evidence / recommendation. The
    title carries the stable id so the GH issue is findable by content.
    """
    marker = _PROJECTION_MARKER.format(feature=feature_id)
    severity = issue.get("severity") or "unspecified"
    source = issue.get("source") or ""
    description = issue.get("description") or ""
    recommendation = issue.get("recommendation") or ""
    lines = [
        marker,
        f"**severity:** {severity}  ·  **source:** {source}",
        "",
        "### Description",
        "",
        description.strip() or "_(none)_",
        "",
    ]
    evidence = issue.get("evidence")
    if isinstance(evidence, list) and evidence:
        lines += ["### Evidence", ""]
        for entry in evidence:
            if isinstance(entry, Mapping):
                file = entry.get("file", "")
                line = entry.get("line")
                suffix = f":{line}" if line is not None else ""
                lines.append(f"- `{file}{suffix}`")
            else:
                lines.append(f"- {entry}")
        lines.append("")
    if recommendation.strip():
        lines += ["### Recommendation", "", recommendation.strip(), ""]
    return "\n".join(lines)


def _issue_title(issue: Mapping[str, Any]) -> str:
    """``[ISSUE-NNN] <title>`` — stable, findable, carries the canonical id."""
    issue_id = issue.get("id", "ISSUE-???")
    title = issue.get("title") or "untitled"
    return f"[{issue_id}] {title}"


def _read_pr_comment_body(feature_root: Path) -> str:
    """The PR comment body — the generated ``final-report.md`` (required for --pr).

    A feature run seeds a placeholder ``final-report.md`` at creation; the
    projection must comment a *generated* report (one with a coherence verdict),
    not the placeholder. So this reads ``final-report.json`` and requires a
    non-null verdict (the generation marker, mirroring ``final_report`` /
    ``profile_comparison``), then returns the rendered markdown. Fails loud
    (§24.2): ``--pr`` before ``coherence-gate`` + ``final-report`` is a
    precondition miss, not a silent no-op.
    """
    report = read_json_object(feature_root / FINAL_REPORT_JSON)
    verdict = report.get("verdict") if isinstance(report, dict) else None
    if verdict is None:
        raise ValueError(
            f"no generated {FINAL_REPORT_MD} at {feature_root} (verdict is null) "
            f"— run `ai-dev coherence-gate` + `ai-dev final-report "
            f"{feature_root.name}` before projecting a PR comment (§24.2)"
        )
    md_path = feature_root / FINAL_REPORT_MD
    if not md_path.is_file():
        raise ValueError(
            f"no {FINAL_REPORT_MD} at {feature_root} despite a non-null verdict "
            f"(corrupt feature run, §24.2)"
        )
    return md_path.read_text()


# ---------------------------------------------------------------------------
# Output parsing — pull GH numbers/ids out of what ``gh`` prints (no token).
# ---------------------------------------------------------------------------


def _parse_issue_number(stdout: str) -> int:
    """The GH issue number from ``gh issue create``'s printed URL.

    ``gh issue create`` prints ``https://github.com/<owner>/<repo>/issues/<n>``.
    The trailing integer is the number. Fails loud if it cannot be parsed — a
    create that printed no recognizable URL means the push silently mis-shaped,
    and recording a bad mapping entry would corrupt the resume point.
    """
    match = re.search(r"/issues/(\d+)", stdout)
    if not match:
        raise ValueError(
            f"could not parse GH issue number from `gh issue create` output: "
            f"{stdout.strip()!r}"
        )
    return int(match.group(1))


def _parse_comment_id(stdout: str) -> int:
    """The comment id from ``gh pr comment``'s printed URL.

    ``gh pr comment`` prints ``.../pull/<n>#issuecomment-<id>``. The id lets a
    later run *edit* the comment in place (true post/update idempotency, D2)
    rather than stack duplicates.
    """
    match = re.search(r"#issuecomment-(\d+)", stdout)
    if not match:
        raise ValueError(
            f"could not parse comment id from `gh pr comment` output: "
            f"{stdout.strip()!r}"
        )
    return int(match.group(1))


# ---------------------------------------------------------------------------
# Pre-flight (D4).
# ---------------------------------------------------------------------------


def _preflight(
    *,
    feature_id: str,
    pr_number: int | None,
    env: Mapping[str, str],
    gh_runner: GhRunner,
    gh_available: Callable[[], bool] = _gh_available,
) -> None:
    """Verify the §24.2-style preconditions before any push (D4: no pushes on fail).

    Raises ``ValueError`` (surfaced as a clean ``error:`` + exit 1 by the CLI)
    when: the token env var is unset, ``gh`` is not on ``PATH``, the rate-limit
    probe fails / is exhausted, or the ``--pr`` PR does not exist. All four are
    checked *before* the first push so a failed pre-flight leaves GitHub (and
    the mapping) untouched.
    """
    if env.get(GITHUB_TOKEN_ENV) in (None, ""):
        raise ValueError(
            f"{GITHUB_TOKEN_ENV} is not set in the environment — `gh` reads it "
            f"for auth; export it before projecting (invariant #11, §24.2)"
        )
    if not gh_available():
        raise ValueError(
            "`gh` CLI not found on PATH — install it (https://cli.github.com) "
            "before projecting (§24.2)"
        )
    # Rate-limit probe: a failed or exhausted limit aborts before any push.
    rate = gh_runner(["api", "rate_limit", "--jq", ".resources.core.remaining"])
    if not rate.ok:
        raise ValueError(
            f"GitHub rate-limit probe failed (exit {rate.returncode}): "
            f"{rate.stderr.strip() or 'no detail'} (§24.2)"
        )
    remaining = rate.stdout.strip()
    try:
        remaining_int = int(remaining)
    except ValueError as exc:
        # A non-numeric remaining is treated as a probe failure, not 0.
        raise ValueError(
            f"could not parse rate-limit remaining ({remaining!r}); "
            f"aborting before any push (§24.2)"
        ) from exc
    if remaining_int <= 0:
        raise ValueError(
            f"GitHub rate limit exhausted (core.remaining={remaining_int}); "
            f"retry later (§24.2)"
        )
    if pr_number is not None:
        view = gh_runner(
            ["pr", "view", str(pr_number), "--json", "number"]
        )
        if not view.ok:
            raise ValueError(
                f"PR #{pr_number} not found or not viewable: "
                f"{view.stderr.strip() or 'no detail'} (§24.2)"
            )


# ---------------------------------------------------------------------------
# Per-item push (D4: fail-loud, keep successes, resume from mapping).
# ---------------------------------------------------------------------------


def _push_issue(
    *,
    feature_id: str,
    issue: Mapping[str, Any],
    mapping: dict[str, Any],
    gh_runner: GhRunner,
) -> IssuePush:
    """Upsert one canonical issue to GitHub (create or edit-in-place).

    Existing mapping entry -> ``gh issue edit`` (body + title refreshed); no
    entry -> ``gh issue create`` (parse the new number, record it). The mapping
    is mutated in place so the caller can persist after each item (D4 resume).
    """
    issue_id = issue["id"]
    title = _issue_title(issue)
    body = _issue_body(feature_id, issue)
    existing = _issue_number(mapping, issue_id)
    if existing is not None:
        _run_or_raise(
            gh_runner,
            ["issue", "edit", str(existing), "--title", title, "--body", body],
            f"gh issue edit {issue_id} -> #{existing}",
        )
        return IssuePush(issue_id=issue_id, action="updated", number=existing)
    result = _run_or_raise(
        gh_runner,
        ["issue", "create", "--title", title, "--body", body],
        f"gh issue create {issue_id}",
    )
    number = _parse_issue_number(result.stdout)
    mapping["issues"][issue_id] = number
    return IssuePush(issue_id=issue_id, action="created", number=number)


def _push_pr_comment(
    *,
    feature_id: str,
    pr_number: int,
    body: str,
    mapping: dict[str, Any],
    gh_runner: GhRunner,
) -> str:
    """Post or update the final-report PR comment (D1/D2).

    First projection (no stored comment id) -> ``gh pr comment`` (create; parse
    + store the id). Re-projection -> ``gh pr comment --edit <id>`` (update in
    place). Either way the comment body is the re-computed final-report, so the
    projection tracks ``final-report`` (ADR-0003: re-computable).
    """
    marker = _PROJECTION_MARKER.format(feature=feature_id)
    full_body = f"{marker}\n\n{body}"
    comment_id = mapping.get("pr_comment_id")
    if isinstance(comment_id, int) and not isinstance(comment_id, bool):
        _run_or_raise(
            gh_runner,
            ["pr", "comment", str(pr_number), "--edit", str(comment_id),
             "--body", full_body],
            f"gh pr comment edit #{pr_number}/{comment_id}",
        )
        return "updated"
    result = _run_or_raise(
        gh_runner,
        ["pr", "comment", str(pr_number), "--body", full_body],
        f"gh pr comment create #{pr_number}",
    )
    mapping["pr_comment_id"] = _parse_comment_id(result.stdout)
    return "created"


class _PushFailed(RuntimeError):
    """A single ``gh`` push failed mid-stream (D4). Carries the item context."""


def _run_or_raise(gh_runner: GhRunner, argv: list[str], what: str) -> GhResult:
    """Run one ``gh`` call; raise ``_PushFailed`` on a non-zero exit (D4).

    Shared by the issue and PR-comment pushers so the "failed (exit N):
    <stderr>" shape is spelled once. ``what`` names the item for the message.
    """
    result = gh_runner(argv)
    if not result.ok:
        detail = result.stderr.strip() or "no detail"
        raise _PushFailed(f"{what} failed (exit {result.returncode}): {detail}")
    return result


# ---------------------------------------------------------------------------
# Public compute / project seams.
# ---------------------------------------------------------------------------


def project_github(
    repo_root: Path,
    feature_id: str,
    pr_number: int | None = None,
    *,
    gh_runner: GhRunner | None = None,
    env: Mapping[str, str] | None = None,
    gh_available: Callable[[], bool] | None = None,
) -> GithubProjectionResult:
    """Push the feature's issues (+ optional PR comment) to GitHub (ADR-0006).

    ``pr_number`` (from ``--pr``) overrides / seeds the stored PR mapping (D3).
    Without it on first projection, projection is issues-only. The mapping is
    the resume point: already-pushed issues are edited, the PR comment is
    updated; the first unpushed item is where a prior mid-stream stop resumes.

    Pre-flight failure or a mid-stream push failure both return a result whose
    ``failure_reason`` is set (the CLI turns that into ``error:`` + exit 1);
    successful pushes + their mapping entries are kept either way (D4).
    """
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id!r} not found under {repo_root}")
    # Resolve lazily from module globals so a test (or operator) can patch
    # ``default_gh_runner`` / ``_gh_available`` at the module level and have the
    # CLI path pick the fake up without threading it through every call.
    if gh_runner is None:
        gh_runner = default_gh_runner
    if gh_available is None:
        gh_available = _gh_available
    live_env = env if env is not None else os.environ
    mapping_path = _mapping_path(repo_root, feature_id)
    mapping = _load_mapping(mapping_path, feature_id)

    # --pr overrides / seeds the stored PR (D3). Persisted immediately so a
    # later mid-stream stop still remembers the PR target.
    if pr_number is not None:
        mapping["pr_number"] = pr_number
        _save_mapping(mapping_path, mapping)
    resolved_pr = mapping.get("pr_number")
    if not isinstance(resolved_pr, int) or isinstance(resolved_pr, bool):
        resolved_pr = None

    # Pre-flight (D4): no pushes on failure.
    try:
        _preflight(
            feature_id=feature_id,
            pr_number=resolved_pr,
            env=live_env,
            gh_runner=gh_runner,
            gh_available=gh_available,
        )
    except ValueError as exc:
        return GithubProjectionResult(
            feature_id=feature_id,
            mapping_path=mapping_path,
            pr_number=resolved_pr,
            failure_reason=str(exc),
        )

    issues = _load_issues(feature_root)
    pushed: list[IssuePush] = []
    # The full work list, in push order: issues first, then the PR comment last.
    # (The PR comment is the final-report — the feature's summary — so it lands
    # after the per-issue detail it summarises.)
    pending = [i["id"] for i in issues]
    if resolved_pr is not None:
        pending.append(f"pr-comment:{resolved_pr}")

    for issue in issues:
        try:
            outcome = _push_issue(
                feature_id=feature_id,
                issue=issue,
                mapping=mapping,
                gh_runner=gh_runner,
            )
        except _PushFailed as exc:
            # D4: stop, keep successes + mapping, report what is pending.
            _save_mapping(mapping_path, mapping)
            remaining = [o for o in pending if o not in {p.issue_id for p in pushed}]
            return GithubProjectionResult(
                feature_id=feature_id,
                mapping_path=mapping_path,
                issues=pushed,
                pr_number=resolved_pr,
                pending=remaining,
                failure_reason=str(exc),
            )
        pushed.append(outcome)
        _save_mapping(mapping_path, mapping)

    pr_comment_action: str | None = None
    if resolved_pr is not None:
        try:
            body = _read_pr_comment_body(feature_root)
            pr_comment_action = _push_pr_comment(
                feature_id=feature_id,
                pr_number=resolved_pr,
                body=body,
                mapping=mapping,
                gh_runner=gh_runner,
            )
        except (_PushFailed, ValueError) as exc:
            # A missing generated final-report (§24.2) or a ``gh pr comment``
            # failure both stop here: issues already pushed are kept (D4), and
            # the pending comment is reported so a re-run resumes after the
            # report is generated / the transient push error clears.
            _save_mapping(mapping_path, mapping)
            return GithubProjectionResult(
                feature_id=feature_id,
                mapping_path=mapping_path,
                issues=pushed,
                pr_number=resolved_pr,
                pending=[f"pr-comment:{resolved_pr}"],
                failure_reason=str(exc),
            )
        _save_mapping(mapping_path, mapping)

    return GithubProjectionResult(
        feature_id=feature_id,
        mapping_path=mapping_path,
        issues=pushed,
        pr_number=resolved_pr,
        pr_comment_action=pr_comment_action,
    )
