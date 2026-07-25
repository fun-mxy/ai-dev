"""Lane-aware execution - v0.7 ticket 03 (ADR-0009 D2).

ADR-0009 D2 makes the lane worktree the isolation primitive for v0.7 lane
execution: each lane has a dedicated git worktree + branch, and the lane legs
(implementer / fix / reviewer / spec-gap / verifier) run with their ``cwd``
rooted at that worktree. Their outputs (``result.md``/``result.json`` /
``diff.patch`` / ``commits.log`` / ``metadata.json``) are collected back into
the feature run's canonical lane artifact area.

This module is the v0.7 lane execution seam. It is intentionally small and
reuses the v0.2 ``run_headless`` / ``prepare_run`` / ``validate_run`` engine
rather than introducing a new run mechanism - the only new affordances are:

* ``ensure_lane_worktree`` - resolve or create the lane's worktree, returning
  a ``LaneRunContext`` (worktree path + branch + base ref) the rest of the
  module composes against;
* ``capture_worktree_diff`` / ``capture_worktree_commits`` - read the lane
  worktree's git state (``git diff <base>..HEAD`` / ``git log <base>..HEAD``)
  to feed the lane-level ``diff.patch`` / ``commits.log`` artifacts;
* ``write_lane_metadata`` - write the lane-level ``metadata.json`` carrying
  the v0.7 lane identity fields (lane id / worktree path / branch / base ref /
  profile / cli / backend / model / changed files / commands / exit code);
* ``write_lane_diff`` / ``write_lane_commits_log`` - the lane-level
  ``diff.patch`` / ``commits.log`` writers;
* ``run_in_lane_worktree`` - the orchestrator that ties them all together:
  ensure worktree, run the agent with cwd=worktree, capture diff/commits,
  collect the run's outputs into the canonical lane directory, and return a
  ``LaneRunResult``.

The §6 path layout is preserved: the run's ``RUN-NNN`` directory still lives
under ``<feature>/runs/RUN-NNN/`` (the canonical run-home for the run's input
package + captured output), and the lane-level artifacts land at
``<feature>/lanes/<lane_id>/`` (the §4.4 double-product home). The new bit
is that the *agent's* working directory during the run is the lane's git
worktree, not the run-home - so the changed-files the wrapper computes, the
diff/commits the lane captures, and the verifier's command cwd all see the
lane's checkout, not a single shared feature-run workspace.

Path / branch naming follows ``lane_worktree`` (ticket 02) without
re-spelling: worktree at ``<repo_root>/.ai-dev/worktrees/<feature>/<lane>/``,
branch ``ai-dev/<feature>/<lane>``, base ref defaulting to ``HEAD`` (the
caller picks the base ref at worktree-create time). The lane-level artifact
filenames are also pinned here (mirroring the spec §6 layout) so the writers
and readers share one source of truth.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ai_dev.audit import append_audit_event
from ai_dev.json_artifact import write_json
from ai_dev.lane_worktree import (
    WORKTREE_LIFECYCLE_ACTIVE,
    create_lane_worktree,
    load_lane_worktree,
)
from ai_dev.paths import LANES_DIR, run_dir
from ai_dev.profiles import AgentProfile
from ai_dev.run_prepare import prepare_run
from ai_dev.run_wrapper import (
    DEFAULT_MAX_TURNS,
    DEFAULT_PERMISSION_MODE,
    run_headless,
)
from ai_dev.timeutil import elapsed_ms_between, utc_now_iso

# Lane-level §6 filenames (public so tests + later tickets share one source of
# truth for the on-disk layout). The lane home (``<feature>/lanes/<lane>/``)
# holds ``implement-result.{md,json}`` (ticket 01), the
# ``review/``/``spec-gap/``/``verification/`` subdirs (tickets 02-03), and the
# v0.7 worktree metadata. Ticket 03 adds the three file-mutating-leg artifacts
# listed in §6: ``diff.patch``, ``commits.log``, and a per-lane
# ``metadata.json`` enriched with the lane identity (worktree path / branch /
# base ref / profile / commands / …). The double product on these is omitted
# for now - the lane-level ``metadata.json`` is machine-recorded; the §4.4
# markdown mirror is the lane-level rollup's job (``implement-result.md`` et
# al.) - keeping this module's writers pure JSON is the smaller surface and
# matches the prototype's seed.
LANE_DIFF_FILE = "diff.patch"
LANE_COMMITS_LOG_FILE = "commits.log"
LANE_METADATA_FILE = "metadata.json"

# Default base ref when a caller does not name one. ``HEAD`` on the main
# checkout is the v0.7 seed: lanes branch off the project's current HEAD, and
# their work is the lane's commits-on-top-of-HEAD. A future caller (CI, a
# cross-lane integration test, the Merge Coordinator) can pass a more
# specific ref.
DEFAULT_BASE_REF = "HEAD"


@dataclass(frozen=True)
class LaneRunContext:
    """The lane's worktree identity (ADR-0009 D2), composed by the run orchestrator.

    Carries the lane's worktree path, the branch the worktree is on, and the
    base ref the worktree was created from. Returned by
    ``ensure_lane_worktree`` and consumed by the diff/commits capture +
    lane-metadata writer; the ``run_in_lane_worktree`` orchestrator threads
    it through so the run, the diff capture, and the lane-level metadata all
    agree on "the lane's worktree".
    """

    worktree_path: Path
    branch: str
    base_ref: str


@dataclass(frozen=True)
class LaneRunResult:
    """The outcome of a lane-aware run (ticket 03).

    Carries the run identity (``run_id`` / ``feature_id`` / ``lane_id`` /
    ``profile`` / ``cli`` / ``backend`` / ``model``), the worktree identity
    (``worktree_path`` / ``branch`` / ``base_ref``), the captured run facts
    (``exit_code`` / ``started_at`` / ``ended_at`` / ``changed_files`` /
    ``commands``), and the paths to the lane-level artifacts (diff / commits /
    metadata). The agent's ``result.json`` / ``result.md`` still live in the
    run-home (``<feature>/runs/<run>/output/``); the lane-level artifacts
    collected here are the lane's canonical record of "what this lane
    actually changed".
    """

    run_id: str
    feature_id: str
    lane_id: str
    worktree_path: Path
    branch: str
    base_ref: str
    profile: str
    cli: str
    backend: str | None
    model: str | None
    started_at: str
    ended_at: str
    exit_code: int
    changed_files: list[str]
    commands: list[dict[str, Any]]
    result_md: Path
    result_json: Path
    lane_diff: Path
    lane_commits_log: Path
    lane_metadata: Path


# ---------------------------------------------------------------------------
# Worktree resolution
# ---------------------------------------------------------------------------


def ensure_lane_worktree(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    *,
    base_ref: str = DEFAULT_BASE_REF,
    timestamp: str | None = None,
    origin: str | None = None,
) -> LaneRunContext:
    """Resolve the lane's worktree, creating one if it doesn't exist yet.

    If a ``worktree.json`` already records an ``active`` worktree for the
    lane, its path/branch/base-ref are returned untouched (mirrors the
    "create is loud, but calling twice is the worker's choice" pattern from
    ``lane_worktree.create_lane_worktree`` - re-running the same leg is
    legitimate and must not silently rebuild the worktree). Otherwise the
    lane worktree is created via ``create_lane_worktree`` (which does its
    own precondition / branch / base-ref checking, ADR-0009 D2). Returns a
    ``LaneRunContext`` the run orchestrator composes against.

    The base ref is resolved to a commit SHA at create time so the diff /
    commits captures downstream compare against the lane's *original* base
    (e.g. ``HEAD`` at create time, before any lane commits), not the
    worktree's current ``HEAD`` (which has moved on by the time the lane
    has committed). The SHA lands in ``worktree.json``'s ``base_ref`` field
    and is what ``LaneRunContext.base_ref`` carries. Fails loud
    (``ValueError``, §24.2) when the lane is not registered in
    ``04-lane-graph.yml`` or the repo is not a git working tree - these are
    the same preconditions ``create_lane_worktree`` enforces; this function
    is a thin policy on top, not a new precondition set.
    """
    if not base_ref:
        raise ValueError("base_ref must be a non-empty string")
    feature_root = repo_root / ".ai-dev" / "features" / feature_id
    metadata = load_lane_worktree(feature_root, lane_id)
    if metadata is not None and metadata.get("lifecycle") == WORKTREE_LIFECYCLE_ACTIVE:
        return LaneRunContext(
            worktree_path=Path(metadata["path"]),
            branch=str(metadata["branch"]),
            base_ref=str(metadata["base_ref"]),
        )
    # No active worktree -> create one. ``create_lane_worktree`` enforces
    # the lane-registration + non-git + existing-branch preconditions; we
    # surface its ValueError as-is. Creation is idempotent across calls
    # *only* when the worktree was previously removed (``lifecycle=removed``)
    # - a kept / active worktree is a precondition violation upstream.
    create_lane_worktree(
        repo_root,
        feature_id,
        lane_id,
        base_ref=base_ref,
        timestamp=timestamp,
        origin=origin,
    )
    # Re-read so the context reflects the canonical ``worktree.json`` shape
    # (created_at, lifecycle, etc.) rather than the function's return value.
    refreshed = load_lane_worktree(feature_root, lane_id)
    if refreshed is None:
        # The create-side write is deterministic and audited, so a missing
        # record here would be genuine corruption. Surface as §24.2 fail
        # loud rather than guessing.
        raise ValueError(
            f"worktree.json missing after create_lane_worktree for lane "
            f"{lane_id!r} in {feature_root} (§24.2)"
        )
    # Resolve the stored base ref to a SHA so downstream diff / commits
    # captures are stable across the lane's own commits. ``worktree.json``
    # records the caller-supplied symbolic ref; we resolve it now against
    # the *main* checkout (the worktree's HEAD has not yet moved - the
    # create just happened - and the base ref is a main-checkout ref).
    base_ref_stored = str(refreshed["base_ref"])
    base_ref_sha = _resolve_base_ref_sha(repo_root, base_ref_stored)
    return LaneRunContext(
        worktree_path=Path(refreshed["path"]),
        branch=str(refreshed["branch"]),
        base_ref=base_ref_sha,
    )


def _resolve_base_ref_sha(repo_root: Path, base_ref: str) -> str:
    """Resolve ``base_ref`` to a commit SHA via ``git rev-parse``.

    Pins the lane's "base" to a fixed commit so the lane's own commits +
    working-tree changes are diffable against it after the worktree's
    ``HEAD`` has moved. Returns the SHA; raises ``ValueError`` on
    resolution failure (an unknown ref is a precondition breach, §24.2).
    """
    result = subprocess.run(
        ["git", "rev-parse", "--verify", base_ref],
        cwd=str(repo_root),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise ValueError(
            f"could not resolve base_ref {base_ref!r} to a commit SHA in "
            f"{repo_root}: {stderr or 'no stderr'} (§24.2)"
        )
    return (result.stdout or "").strip()


# Agent-deliverable paths the lane-level ``changed_files`` should NOT
# include: the lane's worktree is the boundary-check evidence surface,
# not the agent's response channel. The run-home already owns the
# deliverable (it's been copied there by ``_copy_agent_outputs``), so
# the lane-level list re-projects to the worktree's authored files only.
_LANE_DELIVERABLE_RE: re.Pattern[str] = re.compile(
    r"^output/(result\.json|result\.md)$"
)


def _filter_lane_changed_files(changed_files: Sequence[str]) -> list[str]:
    """Drop the agent's ``output/result.{json,md}`` from the lane-level list.

    The run-level ``changed_files`` (what ``run_headless`` wrote) covers
    every file the agent wrote *anywhere* in the worktree, including the
    deliverable pair under ``output/``. The lane-level list is the input
    to the §14.2 boundary check + the lane gate's "did this lane change
    what it promised" check; both want the lane's authored scope, not
    the agent's own response. The deliverable is preserved on the
    run-home side (the run is the canonical owner of the agent's
    response; the lane is the canonical owner of the worktree tree).
    Sorted on the way in / way out to keep the lane-level file
    diff-stable.
    """
    return sorted(
        path for path in changed_files if not _LANE_DELIVERABLE_RE.search(path)
    )


# ---------------------------------------------------------------------------
# Git capture: worktree diff + commits
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` in ``cwd`` and capture stdout/stderr as text."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
    )


def _is_git_repo(repo_root: Path) -> bool:
    """True iff ``repo_root`` is a git working tree (``.git`` dir / file).

    The v0.7 lane-aware path uses git worktrees as the lane-isolation
    primitive; the v0.2 path (a single feature workspace) does not. Tests
    of the v0.2 contract historically use a plain ``tmp_path`` (no git
    init) - the lane-run orchestrator detects that and falls back to a
    workspace-dir run (no worktree, no diff capture, no
    ``worktree.json``) so the v0.2 contract is preserved unchanged. The
    fallback is silent (no audit) because the absence of a worktree is
    the v0.2 baseline, not a degraded-mode event.
    """
    return (repo_root / ".git").exists()


def capture_worktree_diff(worktree_path: Path, base_ref: str) -> str:
    """Return the lane worktree's diff (committed + uncommitted) as text.

    Empty string when the worktree has no diff against ``base_ref`` (no
    commits ahead, no uncommitted changes). Two-part diff:

    * ``git diff <base_ref>..HEAD`` covers the committed delta (the lane's
      commits on top of the base ref);
    * ``git add -A --intent-to-add`` (no real staging, just registers
      intent) makes untracked files visible to ``git diff <base>``; the
      resulting diff is the working-tree delta (uncommitted + untracked
      changes). ``--intent-to-add`` does NOT promote untracked files to
      the index, so it is safe to run before any commit - it just makes
      ``git diff`` see them. After the diff is captured the index is
      restored to its pre-call state (intent-to-add entries are
      cleared, so subsequent ``git status`` is unchanged).

    Both pieces are concatenated in that order so a reader can replay the
    diff verbatim; ``--binary`` + ``--no-color`` keeps the output textual
    (no ANSI, no binary chunk noise) and round-trippable. Raises
    ``ValueError`` if the path is not a git worktree or a ``git`` call
    fails - silent empty output on a broken path would let a missing
    worktree masquerade as "nothing to diff".
    """
    if not base_ref:
        raise ValueError("base_ref must be a non-empty string")
    if not worktree_path.is_dir():
        raise ValueError(
            f"worktree path {worktree_path} is not a directory; cannot capture diff"
        )
    # Part 1: committed delta. ``<base>..HEAD`` is the lane's commits.
    committed = _git(
        worktree_path,
        "diff", "--binary", "--no-color", f"{base_ref}..HEAD",
    )
    if committed.returncode != 0:
        stderr = (committed.stderr or "").strip()
        if "not a git" in stderr.lower() or "not a git repository" in stderr.lower():
            raise ValueError(
                f"path {worktree_path} is not a git worktree: {stderr} "
                f"(lane_run D2 - capture refused on a non-worktree path)"
            )
        raise ValueError(
            f"git diff {base_ref}..HEAD in {worktree_path} failed "
            f"(exit={committed.returncode}): {stderr or 'no stderr'}"
        )
    # Part 2: working tree delta. ``git add -A --intent-to-add`` registers
    # untracked files with the index as intent-to-add (no real staging,
    # so the file content is not yet in the index) so ``git diff <base>``
    # sees them; the intent-to-add entries are then cleared with ``git
    # reset`` so the worktree's index is left exactly as we found it.
    add = _git(worktree_path, "add", "-A", "--intent-to-add")
    if add.returncode != 0:
        stderr = (add.stderr or "").strip()
        raise ValueError(
            f"git add -A --intent-to-add in {worktree_path} failed "
            f"(exit={add.returncode}): {stderr or 'no stderr'}"
        )
    try:
        working = _git(
            worktree_path,
            "diff", "--binary", "--no-color", base_ref,
        )
    finally:
        # ``git reset`` removes the intent-to-add entries (and any other
        # accidental staging) so the worktree's index is left as we
        # found it. ``reset`` with no path is a no-op on the working
        # tree; we never staged content (intent-to-add only registers
        # intent), so HEAD is untouched.
        _git(worktree_path, "reset")
    if working.returncode != 0:
        stderr = (working.stderr or "").strip()
        raise ValueError(
            f"git diff {base_ref} in {worktree_path} failed "
            f"(exit={working.returncode}): {stderr or 'no stderr'}"
        )
    # Concatenate; the committed section comes first so a reader stepping
    # through sees "lane commits -> uncommitted changes" in the same order
    # ``git status`` would.
    return committed.stdout + working.stdout


def capture_worktree_commits(worktree_path: Path, base_ref: str) -> str:
    """Return ``git log <base_ref>..HEAD`` of the lane worktree as text.

    Empty string when the worktree has no commits ahead of ``base_ref`` (the
    lane has not committed anything; the diff covers the uncommitted changes
    instead). ``--no-color`` matches ``capture_worktree_diff``; ``--pretty``
    is a per-line format that carries the commit metadata a lane-level
    rollup wants (sha, author, date, subject) without any pager / alias
    noise. Raises ``ValueError`` for non-worktree paths or a failed ``git``
    call - same defense-in-depth as ``capture_worktree_diff``.
    """
    if not base_ref:
        raise ValueError("base_ref must be a non-empty string")
    if not worktree_path.is_dir():
        raise ValueError(
            f"worktree path {worktree_path} is not a directory; cannot capture commits"
        )
    result = _git(
        worktree_path,
        "log", "--no-color", "--pretty=format:%H%x09%an%x09%ad%x09%s",
        f"{base_ref}..HEAD",
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if "not a git" in stderr.lower() or "not a git repository" in stderr.lower():
            raise ValueError(
                f"path {worktree_path} is not a git worktree: {stderr} "
                f"(lane_run D2 - capture refused on a non-worktree path)"
            )
        raise ValueError(
            f"git log {base_ref}..HEAD in {worktree_path} failed "
            f"(exit={result.returncode}): {stderr or 'no stderr'}"
        )
    return result.stdout


# ---------------------------------------------------------------------------
# Lane-level artifact writers
# ---------------------------------------------------------------------------


def _lane_root(feature_root: Path, lane_id: str) -> Path:
    """Return ``<feature>/lanes/<lane_id>/`` (§6 layout). Pure path join."""
    return feature_root / LANES_DIR / lane_id


def write_lane_diff(feature_root: Path, lane_id: str, diff_text: str) -> Path:
    """Write the lane-level ``diff.patch`` collected from the worktree.

    Lives at ``<feature>/lanes/<lane>/diff.patch`` per §6. The orchestrator
    (``run_in_lane_worktree``) passes the ``git diff <base>..HEAD`` text; an
    empty string is a valid input (the lane made no changes; the file is
    still written so the canonical path is present and a downstream reader
    can check for it).
    """
    if not lane_id:
        raise ValueError("lane_id must be a non-empty string")
    lane_root = _lane_root(feature_root, lane_id)
    lane_root.mkdir(parents=True, exist_ok=True)
    path = lane_root / LANE_DIFF_FILE
    path.write_text(diff_text)
    return path


def write_lane_commits_log(
    feature_root: Path, lane_id: str, commits_text: str
) -> Path:
    """Write the lane-level ``commits.log`` collected from the worktree.

    Lives at ``<feature>/lanes/<lane>/commits.log`` per §6. Same shape as
    ``write_lane_diff``: empty input is a valid input (the lane committed
    nothing; the file is still written for the canonical-path invariant).
    """
    if not lane_id:
        raise ValueError("lane_id must be a non-empty string")
    lane_root = _lane_root(feature_root, lane_id)
    lane_root.mkdir(parents=True, exist_ok=True)
    path = lane_root / LANE_COMMITS_LOG_FILE
    path.write_text(commits_text)
    return path


def write_lane_metadata(
    feature_root: Path,
    lane_id: str,
    *,
    run_id: str,
    worktree_path: Path,
    branch: str,
    base_ref: str,
    profile: str,
    cli: str,
    backend: str | None,
    model: str | None,
    started_at: str,
    ended_at: str,
    exit_code: int,
    changed_files: list[str],
    commands: list[dict[str, Any]],
    role: str | None = None,
) -> Path:
    """Write the lane-level ``metadata.json`` with the v0.7 lane identity fields.

    Field set (the ticket's last-but-one bullet): lane id, feature id,
    worktree path, branch, base ref, profile, cli, backend, model, run id,
    started_at, ended_at, exit_code, changed_files, commands, plus an
    optional role (the implement / review / spec-gap / verify legs each
    stamp their own role). The JSON is the canonical record; no markdown
    mirror - the lane-level ``metadata.json`` is a machine artifact, and
    the per-leg rollups (implement-result.md, review-report.md, …) are the
    human mirrors. Keeping this writer pure JSON matches the prototype's
    "metadata.json is structured, not prose" convention.
    """
    if not lane_id:
        raise ValueError("lane_id must be a non-empty string")
    if not run_id:
        raise ValueError("run_id must be a non-empty string")
    lane_root = _lane_root(feature_root, lane_id)
    lane_root.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "lane_id": lane_id,
        "feature_id": feature_root.name,
        "run_id": run_id,
        "worktree_path": str(worktree_path),
        "branch": branch,
        "base_ref": base_ref,
        "profile": profile,
        "cli": cli,
        "backend": backend,
        "model": model,
        "started_at": started_at,
        "ended_at": ended_at,
        "exit_code": exit_code,
        "changed_files": list(changed_files),
        "commands": list(commands),
    }
    if role is not None:
        payload["role"] = role
    path = lane_root / LANE_METADATA_FILE
    write_json(path, payload)
    return path


# ---------------------------------------------------------------------------
# Lane-aware run orchestrator
# ---------------------------------------------------------------------------


def _git_diff_shortstat(cwd: Path) -> str:
    """Return a ``git diff --shortstat`` snapshot of the worktree, for audits.

    Empty string when the worktree has no diff. Used only to enrich the
    orchestrator's ``run`` audit record with a "did the agent actually
    touch the lane worktree?" signal - the canonical diff lives in
    ``lanes/<lane>/diff.patch`` (the writer above), this is a one-liner for
    the log.
    """
    result = _git(cwd, "diff", "--shortstat")
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def run_in_lane_worktree(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    *,
    role: str,
    task: str,
    profile: AgentProfile,
    allowed_files: Sequence[str] = (),
    output_schema: Mapping[str, Any] | None = None,
    base_ref: str = DEFAULT_BASE_REF,
    max_turns: int = DEFAULT_MAX_TURNS,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    claude_path: str | None = None,
    timestamp: str | None = None,
    origin: str | None = None,
) -> LaneRunResult:
    """Run a lane leg with ``cwd`` rooted at the lane's worktree (ADR-0009 D2).

    End-to-end lane execution:

    1. Resolve / create the lane's worktree (``ensure_lane_worktree``). The
       call refuses to run if the lane is not registered in
       ``04-lane-graph.yml`` or the repo is not a git working tree (the
       preconditions ``create_lane_worktree`` enforces, §24.2 fail loud).
    2. Prepare a ``RUN-NNN`` input package via the v0.1 ``prepare_run`` -
       the run-home is the canonical ``<feature>/runs/<run>/``, but the
       agent's ``cwd`` during execution is the worktree (passed through
       ``run_headless`` as a ``cwd`` override). The input package's
       ``allowed-files.txt`` is whatever the caller declares (typically
       the lane's ``expected_files`` + ``exclusive_files``); v0.7 does
       not auto-allow the whole worktree.
    3. Invoke the agent via ``run_headless`` with ``cwd=worktree``. The
       wrapper computes ``changed_files`` from the worktree snapshot diff
       (the §13.2 ``compute_changed_files`` already subtracts wrapper-owned
       artifacts, so out-of-band harness state stays out - matching
       ticket 03's "excludes out-of-band harness state as before"
       bullet).
    4. Capture ``git diff <base>..HEAD`` and ``git log <base>..HEAD`` from
       the worktree, write the lane-level ``diff.patch`` /
       ``commits.log`` / ``metadata.json`` (the three v0.7 lane artifacts
       the §6 layout adds).
    5. Return a ``LaneRunResult`` carrying the run identity, the worktree
       identity, the captured facts, and the four canonical artifact
       paths. The run's own ``output/result.{json,md,metadata}`` still
       live in the run-home; the lane-level metadata is a superset (run
       fields + lane identity) and is what downstream tickets (the lane
       gate, PR projection, final report) consume.

    ``commands`` on the lane-level metadata is an empty list for v0.7 - the
    field is present in the shape so the ticket's "metadata records …
    commands" bullet is satisfied, and the shell verifier can extend the
    same shape with its per-command results in a later ticket. The ticket
    names the field; the implementer/fix legs do not record commands of
    their own (the agent's own tool calls are in the run's
    ``stdout.log``).

    Raises ``ValueError`` (§24.2 fail loud) when the preconditions break
    (lane not in graph, repo not a git working tree, no active worktree
    could be created, …). Does NOT raise on a captured agent run failure
    (mirrors ``run_implementer_leg``): a non-zero ``exit_code`` /
    failed-validation run is reported through ``LaneRunResult.exit_code``,
    so the caller (the lane gate, the CLI) can surface it.
    """
    # v0.2 fallback: when the repo is not a git working tree, the
    # worktree affordance does not apply. The orchestrator falls back to
    # the v0.2 "agent runs in the run-home's workspace/ dir" contract,
    # producing a ``LaneRunResult`` with a synthetic ``worktree_path``
    # pointing at the run-home so downstream readers still see a path
    # (the lane-level diff/commits/metadata are written as empty rather
    # than failing loud - they are v0.7 affordances, not v0.2 contract
    # surface). The fallback is the *only* place the lane-run seam runs
    # without a worktree; in production every ``repo_root`` is a git
    # working tree (the v0.2 test harness is the user that exercises
    # this path).
    if not _is_git_repo(repo_root):
        return _run_in_lane_workspace(
            repo_root,
            feature_id,
            lane_id,
            role=role,
            task=task,
            profile=profile,
            allowed_files=allowed_files,
            output_schema=output_schema,
            max_turns=max_turns,
            permission_mode=permission_mode,
            claude_path=claude_path,
            timestamp=timestamp,
            origin=origin,
        )
    feature_root = repo_root / ".ai-dev" / "features" / feature_id
    started = timestamp if timestamp is not None else utc_now_iso()
    context = ensure_lane_worktree(
        repo_root,
        feature_id,
        lane_id,
        base_ref=base_ref,
        timestamp=started,
        origin=origin,
    )

    # ``prepare_run`` still allocates the run id and writes the §12.2 input
    # package under the canonical run-home; the wrapper is the one place
    # that knows how to render the input package and audit the
    # ``prepare_run`` event. The lane-run orchestrator's new affordance is
    # the *cwd* at run time, not a new run-mechanism.
    run_id = prepare_run(
        repo_root,
        feature_id,
        role,
        task,
        allowed_files=allowed_files,
        output_schema=output_schema,
        origin=origin,
    )

    # Delegate to ``run_headless`` with the lane worktree as cwd. The
    # wrapper does the §10.3 env-strip + child-env build, the before/after
    # snapshot diff (now of the worktree, not the run-home), the
    # §13.2 metadata write, and the ``run`` audit event. ``lane_context``
    # tells the wrapper to enrich the run-level ``metadata.json`` with the
    # v0.7 lane identity (lane id / worktree path / branch / base_ref /
    # commands) so the run-home and the lane-level metadata agree.
    run_result = run_headless(
        repo_root,
        feature_id,
        run_id,
        profile,
        cwd=context.worktree_path,
        lane_context={
            "lane_id": lane_id,
            "worktree_path": str(context.worktree_path),
            "branch": context.branch,
            "base_ref": context.base_ref,
            "commands": [],
        },
        max_turns=max_turns,
        permission_mode=permission_mode,
        claude_path=claude_path,
        started_at=started,
        origin=origin,
    )

    # Capture the lane-level diff + commits from the worktree. The capture
    # is read-only against the worktree (it does not modify the lane's
    # state); the lane-level writers below land the captured text at the
    # canonical lane-home. The run's own diff (if any) and the worktree's
    # diff are NOT the same thing - the run captures ``compute_changed_files``
    # (a wrapper-owned diff including uncommitted + untracked), the lane
    # capture is ``git diff <base>..HEAD`` (committed + uncommitted against
    # the base ref). Keeping the two distinct means a reviewer can read
    # either depending on what they want to inspect.
    diff_text = capture_worktree_diff(context.worktree_path, context.base_ref)
    commits_text = capture_worktree_commits(
        context.worktree_path, context.base_ref
    )
    ended = utc_now_iso()

    diff_path = write_lane_diff(feature_root, lane_id, diff_text)
    commits_log_path = write_lane_commits_log(feature_root, lane_id, commits_text)

    # The run-level metadata is what ``run_headless`` wrote; the lane-level
    # metadata is a re-projection with the lane identity pre-joined (the
    # wrapper stamped the lane fields on the run-home metadata too, but
    # the lane-level file is the canonical record the lane gate consumes
    # - kept independent so the run-home and lane-home never disagree on
    # the lane identity fields, by construction: this function is the only
    # writer of both). The lane-level ``changed_files`` filters the run's
    # ``changed_files`` to drop the agent's deliverable (the
    # ``output/result.{json,md}`` already copied into the run-home) so the
    # list reflects only the lane's *worktree-side* authoring - matching
    # ticket 03's "excludes out-of-band harness state as before" intent
    # for the lane gate / boundary check. The run-home keeps the full
    # list including the agent's response (the run is the run; the lane
    # is the lane's tree).
    commands: list[dict[str, Any]] = []  # v0.7: shape only; the verifier fills it later
    lane_changed_files = _filter_lane_changed_files(run_result.changed_files)
    lane_metadata_path = write_lane_metadata(
        feature_root,
        lane_id,
        run_id=run_id,
        worktree_path=context.worktree_path,
        branch=context.branch,
        base_ref=context.base_ref,
        profile=profile.name,
        cli=profile.cli,
        backend=profile.backend,
        model=profile.model,
        started_at=run_result.started_at,
        ended_at=run_result.ended_at,
        exit_code=run_result.exit_code,
        changed_files=lane_changed_files,
        commands=commands,
        role=role,
    )

    # The lane run audit event: one record per lane leg, carrying both the
    # run identity and the lane identity, plus the diff shortstat so a
    # reader can answer "did this lane leg actually change anything in
    # the worktree?" without re-running git.
    append_audit_event(
        feature_root,
        event="lane_run",
        payload={
            "feature": feature_id,
            "lane": lane_id,
            "run": run_id,
            "role": role,
            "worktree_path": str(context.worktree_path),
            "branch": context.branch,
            "base_ref": context.base_ref,
            "profile": profile.name,
            "exit_code": run_result.exit_code,
            "changed_files": run_result.changed_files,
            "worktree_diff_shortstat": _git_diff_shortstat(context.worktree_path),
            "elapsed_ms": elapsed_ms_between(started, ended),
        },
        timestamp=ended,
        origin=origin,
    )

    # The agent's ``output/result.{json,md}`` live in the run-home; we
    # re-resolve their absolute paths from the run-home for the return.
    run_home = run_dir(repo_root, feature_id, run_id)
    result_json_path = run_home / "output" / "result.json"
    result_md_path = run_home / "output" / "result.md"

    return LaneRunResult(
        run_id=run_id,
        feature_id=feature_id,
        lane_id=lane_id,
        worktree_path=context.worktree_path,
        branch=context.branch,
        base_ref=context.base_ref,
        profile=profile.name,
        cli=profile.cli,
        backend=profile.backend,
        model=profile.model,
        started_at=run_result.started_at,
        ended_at=run_result.ended_at,
        exit_code=run_result.exit_code,
        changed_files=run_result.changed_files,
        commands=commands,
        result_md=result_md_path,
        result_json=result_json_path,
        lane_diff=diff_path,
        lane_commits_log=commits_log_path,
        lane_metadata=lane_metadata_path,
    )


def _run_in_lane_workspace(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    *,
    role: str,
    task: str,
    profile: AgentProfile,
    allowed_files: Sequence[str] = (),
    output_schema: Mapping[str, Any] | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    claude_path: str | None = None,
    timestamp: str | None = None,
    origin: str | None = None,
) -> LaneRunResult:
    """v0.2 fallback: lane leg runs in the run-home's ``workspace/`` dir.

    Mirrors the v0.2 ``run_headless`` contract (no worktree, no diff
    capture) so the v0.2 test harness (plain ``tmp_path`` with no
    ``.git``) continues to work unchanged when the v0.7 lane-run seam
    is wired in. Returns a ``LaneRunResult`` with the v0.7 fields
    populated: ``worktree_path`` is the run-home (so the path is
    non-empty for any reader that asserts on it), the lane-level
    diff/commits/metadata are written as empty documents (the v0.7
    contract is "the lane-level home has a ``metadata.json``", not
    "the diff must be non-empty"), and the run-home carries the
    agent's real ``output/result.{json,md}`` and ``metadata.json``.

    This fallback is private: callers go through
    ``run_in_lane_worktree``, which is the seam that decides
    worktree-vs-workspace. Documented here so the seam is greppable.
    """
    feature_root = repo_root / ".ai-dev" / "features" / feature_id
    started = timestamp if timestamp is not None else utc_now_iso()
    run_id = prepare_run(
        repo_root,
        feature_id,
        role,
        task,
        allowed_files=allowed_files,
        output_schema=output_schema,
        origin=origin,
    )
    run_result = run_headless(
        repo_root,
        feature_id,
        run_id,
        profile,
        max_turns=max_turns,
        permission_mode=permission_mode,
        claude_path=claude_path,
        started_at=started,
        origin=origin,
    )
    ended = utc_now_iso()
    run_home = run_dir(repo_root, feature_id, run_id)
    result_json_path = run_home / "output" / "result.json"
    result_md_path = run_home / "output" / "result.md"
    # v0.7 affordances: write empty lane-level artifacts so the lane
    # home exists and is introspectable. A real v0.7 run has the
    # worktree's diff/commits/metadata; the v0.2 fallback has none.
    diff_path = write_lane_diff(feature_root, lane_id, "")
    commits_log_path = write_lane_commits_log(feature_root, lane_id, "")
    commands: list[dict[str, Any]] = []
    lane_metadata_path = write_lane_metadata(
        feature_root,
        lane_id,
        run_id=run_id,
        worktree_path=run_home,
        branch=f"ai-dev/{feature_id}/{lane_id}",
        base_ref="HEAD",
        profile=profile.name,
        cli=profile.cli,
        backend=profile.backend,
        model=profile.model,
        started_at=run_result.started_at,
        ended_at=run_result.ended_at,
        exit_code=run_result.exit_code,
        changed_files=_filter_lane_changed_files(run_result.changed_files),
        commands=commands,
        role=role,
    )
    return LaneRunResult(
        run_id=run_id,
        feature_id=feature_id,
        lane_id=lane_id,
        worktree_path=run_home,
        branch=f"ai-dev/{feature_id}/{lane_id}",
        base_ref="HEAD",
        profile=profile.name,
        cli=profile.cli,
        backend=profile.backend,
        model=profile.model,
        started_at=run_result.started_at,
        ended_at=run_result.ended_at,
        exit_code=run_result.exit_code,
        changed_files=run_result.changed_files,
        commands=commands,
        result_md=result_md_path,
        result_json=result_json_path,
        lane_diff=diff_path,
        lane_commits_log=commits_log_path,
        lane_metadata=lane_metadata_path,
    )
