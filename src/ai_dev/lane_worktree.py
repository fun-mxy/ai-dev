"""Lane worktree lifecycle primitive — v0.7 multi-lane worktree engine.

ADR-0009 D2 / D3 makes lane worktrees the isolation primitive for v0.7.
Each lane has a dedicated git worktree + branch, recorded in
``worktree.json``, with explicit create / keep / remove lifecycle
operations. This module is the deterministic engine behind those
operations; it does not run lane legs (that lands in ticket 03) and it
does not implement the full declarative worktree-profile engine from
spec §20 (resource classes, secret symlink policy, bootstrap hooks, port
allocation, …) — those are deferred per ADR-0009 D3.

## Path / branch naming

* Worktree directory: ``<repo_root>/.ai-dev/worktrees/<feature_id>/<lane_id>``
  — under the existing ``.ai-dev/`` data plane, but *outside* the feature
  directory so the worktree cannot accidentally be cleaned up by feature
  garbage-collection and is easy to discover in one place.
* Branch name: ``ai-dev/<feature_id>/<lane_id>`` — namespaced and
  deterministic, so re-creating a lane always reuses the same branch (and
  a pre-existing branch from a previous run is detected as a precondition
  violation rather than silently overwritten).

## ``worktree.json`` shape

``<feature_root>/lanes/<lane_id>/worktree.json`` holds the per-lane
worktree record (v0.7 ticket 02):

.. code-block:: yaml

    lane_id: LANE-001
    feature_id: FEATURE-001
    branch: ai-dev/FEATURE-001/LANE-001
    base_ref: HEAD
    path: <absolute worktree path>
    created_at: 2026-07-25T01:00:00Z
    updated_at: 2026-07-25T01:00:00Z
    lifecycle: active   # active | kept | removed
    clean: true         # last-known clean/dirty snapshot

The record is canonical: every mutation re-derives ``updated_at`` and
``clean`` and re-audits (the JSON log is the audit trail). Models never
write this file (§4.3).

## Failure philosophy

ADR-0009 D2 / ticket 02: "Fail loud on unsafe states — do not silently
delete dirty worktrees, do not overwrite an existing unrelated worktree,
and do not pretend a lane is isolated if worktree creation failed." The
public functions therefore:

* refuse to create over an already-active lane (re-running create is
  resolved by ``keep`` / explicit ``remove`` first);
* refuse to remove a dirty worktree unless ``force=True``;
* refuse to operate on a non-git root (precondition check is the cheap
  "this isn't going to work" answer before shelling out);
* refuse to remove / keep a lane with no ``worktree.json`` (a missing
  record is not a free pass to no-op — that is the silent state the
  ticket explicitly forbids).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Mapping

import yaml

from ai_dev.audit import append_audit_event
from ai_dev.json_artifact import read_json_object, write_json
from ai_dev.templates import LANE_GRAPH_YML

# §6 / §8.2: worktree metadata lives at the per-lane artifact home, flat
# alongside ``implement-result.json`` / ``lane-decision.json``. The double
# product (worktree.md mirror) is not produced for v0.7 ticket 02 — the
# JSON is the canonical record and is meant to be machine-read; ticket
# 03/04 will pick the human-rendering policy when the lane-leg work
# actually consumes the record.
LANE_WORKTREE_FILE = "worktree.json"

# Three lifecycle values, in the order they transition through:
# ``active`` is the post-create default; ``kept`` is an explicit
# retention signal (the worktree stays on disk, but the lifecycle
# metadata marks it as deliberately preserved); ``removed`` is the
# post-removal terminal state. There is no automatic transition; the
# values are written only by this module's three mutating functions,
# and a re-run is free to start from ``active`` again only after an
# explicit ``removed`` (or after a ``kept`` is also explicitly removed).
WORKTREE_LIFECYCLE_ACTIVE = "active"
WORKTREE_LIFECYCLE_KEPT = "kept"
WORKTREE_LIFECYCLE_REMOVED = "removed"
WORKTREE_LIFECYCLE_VALUES: tuple[str, ...] = (
    WORKTREE_LIFECYCLE_ACTIVE,
    WORKTREE_LIFECYCLE_KEPT,
    WORKTREE_LIFECYCLE_REMOVED,
)

# Branch-name validation: ``git worktree add -b <branch>`` will accept
# most sane strings, but we restrict the *naming* side to a
# deterministic, git-safe shape so the worktree metadata and the
# on-disk branch can never disagree on what the branch "should" be. The
# lane and feature ids already pass §5.2 stable-id validation (the
# allocator rejects anything not ``<TYPE>-<NNN>``), so they are safe by
# construction — this regex is a belt-and-braces check at the worktree
# boundary.
_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

# Run-time constant for the worktree parent directory under
# ``.ai-dev/`` (kept as a module-level symbol so tests can spell the
# path the same way the data plane does).
_WORKTREES_PARENT = "worktrees"


# ---------------------------------------------------------------------------
# Path & branch derivation
# ---------------------------------------------------------------------------


def lane_worktree_path(repo_root: Path, feature_id: str, lane_id: str) -> Path:
    """Return the deterministic worktree path for ``(feature_id, lane_id)``.

    Pure path join — no I/O, no precondition checks beyond a non-empty
    id. Centralised so the worktree creator, remover, and tests share
    one source of truth for "where does a lane's worktree live?".
    """
    if not feature_id:
        raise ValueError("feature_id must be a non-empty string")
    if not lane_id:
        raise ValueError("lane_id must be a non-empty string")
    return repo_root / ".ai-dev" / _WORKTREES_PARENT / feature_id / lane_id


def _lane_branch_name(feature_id: str, lane_id: str) -> str:
    """Return the deterministic branch name for ``(feature_id, lane_id)``.

    ``ai-dev/<feature_id>/<lane_id>`` is namespaced and stable across
    re-creations of the same lane. Refuses to synthesise a branch name
    from anything other than §5.2-shaped ids.
    """
    branch = f"ai-dev/{feature_id}/{lane_id}"
    if not _BRANCH_NAME_RE.match(branch):
        raise ValueError(
            f"derived lane branch {branch!r} is not git-safe; "
            f"check feature_id/lane_id shapes (lane_worktree D2)"
        )
    return branch


# ---------------------------------------------------------------------------
# Precondition helpers
# ---------------------------------------------------------------------------


def _is_git_repo(repo_root: Path) -> bool:
    """True iff ``repo_root`` has a git working tree (``.git/`` file or dir)."""
    return (repo_root / ".git").exists()


def _require_lane_registered(repo_root: Path, feature_id: str, lane_id: str) -> None:
    """Refuse to operate on a lane that ``04-lane-graph.yml`` does not declare.

    The lane registry owns the lane set; worktree operations live
    downstream and must never invent a lane that the graph never knew
    about. Failure here is structural corruption (§24.2) and is a loud
    ``ValueError``.
    """
    if not feature_id:
        raise ValueError("feature_id must be a non-empty string")
    if not lane_id:
        raise ValueError("lane_id must be a non-empty string")
    graph_path = repo_root / ".ai-dev" / "features" / feature_id / LANE_GRAPH_YML
    if not graph_path.is_file():
        raise ValueError(
            f"{LANE_GRAPH_YML} missing at {graph_path} (§7.5) - cannot operate on lane worktrees"
        )
    try:
        graph = yaml.safe_load(graph_path.read_text())
    except yaml.YAMLError as exc:
        raise ValueError(
            f"{LANE_GRAPH_YML} at {graph_path} is not valid YAML: {exc} (§24.2)"
        ) from exc
    lanes = graph.get("lanes") if isinstance(graph, dict) else None
    if not isinstance(lanes, list):
        raise ValueError(
            f"{LANE_GRAPH_YML} at {graph_path} has no 'lanes' list (§7.5)"
        )
    known = {lane.get("id") for lane in lanes if isinstance(lane, dict)}
    if lane_id not in known:
        raise ValueError(
            f"lane {lane_id!r} is not registered in {LANE_GRAPH_YML}; "
            f"known lanes: {sorted(str(k) for k in known)} (§7.5)"
        )


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` in ``cwd`` and capture stdout/stderr as text.

    Returns the CompletedProcess; callers raise on ``returncode != 0``
    with the stderr text so the failure message carries git's own
    explanation (typically more actionable than a generic OS error).
    """
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
    )


def _require_git_success(
    result: subprocess.CompletedProcess[str], *, context: str
) -> None:
    """Raise a loud ``ValueError`` if a ``git`` subprocess exited non-zero.

    ``context`` is a short, caller-supplied label (e.g.
    ``"create worktree"``) that the failure message prefixes so the
    user knows which lifecycle operation failed. We use ``ValueError``
    (not ``subprocess.CalledProcessError``) so the worktree API has a
    single exception type callers catch.
    """
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise ValueError(
            f"git {context} failed (exit={result.returncode}): "
            f"{stderr or 'no stderr'}"
        )


def _worktree_metadata_path(
    feature_root: Path, lane_id: str
) -> Path:
    """Path to ``<feature_root>/lanes/<lane_id>/worktree.json``.

    The worktree metadata is a per-lane artifact, so it lives under the
    feature's ``lanes/<lane_id>/`` directory alongside the other lane
    artifacts (implement-result, lane-decision, …). ``feature_root`` is
    the §6 ``.ai-dev/features/<feature_id>/`` directory, so the path is
    a pure join — no repo-root reconstruction needed.
    """
    return feature_root / "lanes" / lane_id / LANE_WORKTREE_FILE


def _set_lane_worktree_pointer(
    feature_root: Path,
    lane_id: str,
    worktree_path: str | None,
    *,
    timestamp: str,
    origin: str | None,
) -> None:
    """Set or clear the lane's §8.2 ``worktree`` pointer (set-or-clear, not append).

    ``status.update_lane_status`` treats ``None`` as "leave the field
    alone" (it filters ``None`` out of its update dict), so it can *set*
    the worktree pointer but cannot *clear* it. Clearing is part of the
    worktree lifecycle — the lane's runtime row must follow the
    on-disk state — so this helper does the small direct yaml edit
    ``update_lane_status`` cannot. The audit record uses the same
    ``lane_status`` event the rest of the lane runtime uses, so a
    reader can treat the worktree field as a regular lane runtime
    field rather than a side channel.
    """
    path = feature_root / "status" / "lane-status.yml"
    if not path.is_file():
        raise ValueError(
            f"lane-status.yml missing at {path} (broken feature run, §24.2)"
        )
    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ValueError(
            f"lane-status.yml at {path} is not valid YAML: {exc} (§24.2)"
        ) from exc
    if not isinstance(doc, dict):
        raise ValueError(
            f"lane-status.yml at {path} is not a mapping (§24.2)"
        )
    lanes = doc.get("lanes")
    if not isinstance(lanes, dict):
        raise ValueError(
            f"lane-status.yml at {path} has no 'lanes' mapping (§24.2)"
        )
    row = lanes.get(lane_id)
    if not isinstance(row, dict):
        raise ValueError(
            f"lane {lane_id!r} is not registered in lane-status.yml; "
            f"known lanes: {sorted(str(k) for k in lanes)} (§24.2)"
        )
    row["worktree"] = worktree_path
    with path.open("w") as f:
        yaml.safe_dump(
            doc,
            f,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )
    append_audit_event(
        feature_root,
        event="lane_status",
        payload={"lane": lane_id, "updates": {"worktree": worktree_path}},
        timestamp=timestamp,
        origin=origin,
    )


# ---------------------------------------------------------------------------
# worktree.json read / write
# ---------------------------------------------------------------------------


def load_lane_worktree(
    feature_root: Path, lane_id: str
) -> dict[str, Any] | None:
    """Read ``worktree.json`` for ``lane_id`` under ``feature_root``.

    Returns ``None`` if the file is missing (the lane has never had a
    worktree). A present-but-malformed file is genuine corruption
    (§24.2) and raises ``ValueError`` — silent recovery would let a
    corrupted record survive as a phantom worktree, which is precisely
    the "pretend a lane is isolated" failure the ticket forbids.
    """
    if not lane_id:
        raise ValueError("lane_id must be a non-empty string")
    path = _worktree_metadata_path(feature_root, lane_id)
    data = read_json_object(path)
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ValueError(
            f"worktree.json at {path} is not a JSON object (§24.2)"
        )
    return data


def _write_worktree_metadata(
    feature_root: Path,
    lane_id: str,
    payload: Mapping[str, Any],
) -> Path:
    """Write ``worktree.json`` deterministically.

    The double product (worktree.md mirror) is intentionally skipped —
    worktree.json is a machine record consumed by the run wrapper /
    later lane legs; ticket 03/04 owns the human-rendering policy. We
    document the omission here so a future maintainer doesn't
    re-introduce a stale md mirror without thought.
    """
    path = _worktree_metadata_path(feature_root, lane_id)
    write_json(path, dict(payload))
    return path


# ---------------------------------------------------------------------------
# Public lifecycle operations
# ---------------------------------------------------------------------------


def create_lane_worktree(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    *,
    base_ref: str,
    timestamp: str | None = None,
    origin: str | None = None,
) -> Path:
    """Create the lane's dedicated worktree + branch and record it.

    Refuses to run unless:

    * ``repo_root`` is a git working tree (no .git → ``ValueError``);
    * ``lane_id`` is registered in ``04-lane-graph.yml`` (§7.5);
    * the lane has no ``active`` ``worktree.json`` already (a re-run is
      resolved by ``keep`` / explicit ``remove`` first; see
      ``TestWorktreeLifecycleComposition``);
    * the base ref resolves (unknown refs raise via ``git``).

    On success, returns the absolute worktree path, writes
    ``worktree.json`` (with ``lifecycle=active``, ``clean=true``), and
    records a ``lane_worktree_create`` audit event at the feature root.
    The lane-status row's ``worktree`` pointer is also updated in the
    same call so the canonical lane runtime reflects the new worktree.
    """
    if not _is_git_repo(repo_root):
        raise ValueError(
            f"repo_root {repo_root} is not a git working tree; "
            f"cannot create lane worktree"
        )
    if not base_ref:
        raise ValueError("base_ref must be a non-empty string")
    _require_lane_registered(repo_root, feature_id, lane_id)

    feature_root = repo_root / ".ai-dev" / "features" / feature_id
    metadata = load_lane_worktree(feature_root, lane_id)
    if metadata is not None and metadata.get("lifecycle") == WORKTREE_LIFECYCLE_ACTIVE:
        raise ValueError(
            f"lane {lane_id!r} already has an active worktree at "
            f"{metadata.get('path')!r}; keep or remove it before creating again "
            f"(lane_worktree D2 — fail loud on unsafe states)"
        )

    branch = _lane_branch_name(feature_id, lane_id)
    path = lane_worktree_path(repo_root, feature_id, lane_id)

    # ``git worktree add -b <branch> <path> <base_ref>`` fails loud if the
    # branch already exists or if the base ref is unresolvable;
    # surfacing that stderr (via ``_require_git_success``) is the
    # actionable error the ticket asks for, rather than a silent
    # overwrite.
    if path.exists():
        raise ValueError(
            f"worktree path {path} already exists on disk; refusing to overwrite "
            f"(lane_worktree D2 — do not overwrite an existing unrelated worktree)"
        )
    result = _git(
        "worktree", "add", "-b", branch, str(path), base_ref,
        cwd=repo_root,
    )
    _require_git_success(
        result, context=f"worktree add -b {branch} {path} {base_ref}"
    )

    stamp = timestamp if timestamp is not None else _utc_now_iso()
    payload: dict[str, Any] = {
        "lane_id": lane_id,
        "feature_id": feature_id,
        "branch": branch,
        "base_ref": base_ref,
        "path": str(path),
        "created_at": stamp,
        "updated_at": stamp,
        "lifecycle": WORKTREE_LIFECYCLE_ACTIVE,
        "clean": True,
    }
    _write_worktree_metadata(feature_root, lane_id, payload)

    # Hook the canonical lane runtime so the worktree pointer is set in
    # the same call that creates the worktree. We use a direct
    # set-or-clear helper (rather than ``status.update_lane_status``) so
    # the create and remove paths share one writer — ``update_lane_status``
    # can *set* a field but cannot *clear* one (it treats ``None`` as
    # "leave the field alone"), and we need both directions.
    _set_lane_worktree_pointer(
        feature_root, lane_id, str(path), timestamp=stamp, origin=origin,
    )

    append_audit_event(
        feature_root,
        event="lane_worktree_create",
        payload={
            "lane": lane_id,
            "branch": branch,
            "path": str(path),
            "base_ref": base_ref,
        },
        timestamp=stamp,
        origin=origin,
    )
    return path


def is_worktree_clean(worktree_path: Path) -> bool:
    """Return ``True`` iff ``worktree_path`` is a git worktree with no
    uncommitted changes (tracked or untracked).

    Uses ``git status --porcelain``; an empty stdout is clean, anything
    else is dirty. Raises ``ValueError`` if the path is not a git
    worktree (e.g. the worktree was never created here, or the path is
    wrong) — silent ``False`` would let destructive operations proceed
    on a path the caller didn't actually create.
    """
    if not worktree_path.is_dir():
        raise ValueError(
            f"worktree path {worktree_path} is not a directory; "
            f"cannot check clean state"
        )
    result = _git("status", "--porcelain", cwd=worktree_path)
    if result.returncode != 0:
        raise ValueError(
            f"path {worktree_path} is not a git worktree: "
            f"{(result.stderr or '').strip() or 'no stderr'} (lane_worktree D2)"
        )
    return not result.stdout.strip()


def keep_lane_worktree(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    *,
    timestamp: str | None = None,
    origin: str | None = None,
) -> None:
    """Mark the lane's worktree as deliberately retained (``lifecycle=kept``).

    ``keep`` is the explicit retention signal: a human (or driver)
    chose not to delete the worktree even if it is dirty. It does
    **not** remove anything from disk; it only updates the metadata
    record and audits the choice. Refuses when there is no
    ``worktree.json`` — silently no-op'ing would hide "I never created
    this lane" the same way silently deleting would hide "I trashed
    someone's worktree".
    """
    feature_root = repo_root / ".ai-dev" / "features" / feature_id
    metadata = load_lane_worktree(feature_root, lane_id)
    if metadata is None:
        raise ValueError(
            f"no lane worktree record for {lane_id!r} in {feature_root}; "
            f"create one first (lane_worktree D2)"
        )
    stamp = timestamp if timestamp is not None else _utc_now_iso()
    clean = is_worktree_clean(Path(metadata["path"]))
    metadata["lifecycle"] = WORKTREE_LIFECYCLE_KEPT
    metadata["updated_at"] = stamp
    metadata["clean"] = clean
    _write_worktree_metadata(feature_root, lane_id, metadata)

    append_audit_event(
        feature_root,
        event="lane_worktree_keep",
        payload={"lane": lane_id, "clean": clean},
        timestamp=stamp,
        origin=origin,
    )


def remove_lane_worktree(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    *,
    force: bool = False,
    timestamp: str | None = None,
    origin: str | None = None,
) -> None:
    """Remove the lane's worktree directory and mark it ``lifecycle=removed``.

    Refuses if the worktree is dirty unless ``force=True`` — a silent
    delete would lose the dirty work the ticket explicitly warns about.
    On success: ``git worktree remove`` is invoked, ``worktree.json``
    is updated to ``lifecycle=removed`` (so re-create-after-removal
    works cleanly), the lane-status ``worktree`` pointer is cleared,
    and a ``lane_worktree_remove`` audit event is recorded.
    """
    feature_root = repo_root / ".ai-dev" / "features" / feature_id
    metadata = load_lane_worktree(feature_root, lane_id)
    if metadata is None:
        raise ValueError(
            f"no lane worktree record for {lane_id!r} in {feature_root}; "
            f"create one first (lane_worktree D2)"
        )
    path = Path(metadata["path"])
    clean = is_worktree_clean(path)
    stamp = timestamp if timestamp is not None else _utc_now_iso()
    # Always refresh the ``clean`` snapshot in the metadata record so
    # the canonical file reflects the last known state — even when the
    # remove is refused (the next operator deserves to know the worktree
    # was dirty when they tried to remove it). The ``updated_at`` stamp
    # is bumped for the same reason.
    metadata["clean"] = clean
    metadata["updated_at"] = stamp

    if not clean and not force:
        _write_worktree_metadata(feature_root, lane_id, metadata)
        raise ValueError(
            f"lane {lane_id!r} worktree at {path} is dirty; refusing to remove "
            f"without force=True (lane_worktree D2 — do not silently delete "
            f"dirty worktrees)"
        )

    # ``git worktree remove`` itself refuses a dirty worktree (so even
    # with force=True we still need to defend against the metadata-side
    # truth diverging from git's own check). Passing ``--force`` to git
    # parallels our own force flag — the operator's intent is the same.
    result = _git(
        "worktree", "remove", *(["--force"] if force else []), str(path),
        cwd=repo_root,
    )
    if result.returncode != 0:
        raise ValueError(
            f"git worktree remove failed for {path}: "
            f"{(result.stderr or '').strip() or 'no stderr'}"
        )

    metadata["lifecycle"] = WORKTREE_LIFECYCLE_REMOVED
    _write_worktree_metadata(feature_root, lane_id, metadata)

    # ``git worktree remove`` only unlinks the worktree — the branch
    # itself stays in the main repo. Without an explicit ``git branch
    # -D`` here, the next ``create_lane_worktree`` for this lane fails
    # with "a branch named … already exists" (the precondition the
    # create-side check enforces to avoid overwriting an unrelated
    # branch). ``-D`` (not ``-d``) is required because the branch tip
    # may carry lane-local commits the operator chose not to integrate
    # — that is exactly the "unmerged state" a force-remove is
    # acknowledging, so soft-delete (``-d``) would itself refuse. The
    # metadata's ``lifecycle=removed`` is the audit trail; we do not
    # need git to keep the branch around for that.
    branch = metadata.get("branch")
    if isinstance(branch, str) and branch:
        branch_result = _git(
            "branch", "-D", branch, cwd=repo_root,
        )
        if branch_result.returncode != 0:
            raise ValueError(
                f"git branch -D {branch} failed after worktree remove: "
                f"{(branch_result.stderr or '').strip() or 'no stderr'} "
                f"(lane_worktree D2 — recreate-after-remove must be possible)"
            )

    # Clear the lane-status pointer so downstream readers do not see a
    # dangling path; the canonical record still says "removed at
    # <stamp>". ``_set_lane_worktree_pointer`` accepts ``None`` as a
    # valid value to *clear* the field, which ``update_lane_status``
    # cannot express.
    _set_lane_worktree_pointer(
        feature_root, lane_id, None, timestamp=stamp, origin=origin,
    )

    append_audit_event(
        feature_root,
        event="lane_worktree_remove",
        payload={"lane": lane_id, "force": force, "path": str(path)},
        timestamp=stamp,
        origin=origin,
    )


# ---------------------------------------------------------------------------
# Local time import — kept at module bottom to keep the public top-of-file
# import block tight (the rest of the package uses timeutil for ISO stamps,
# but lane_worktree needs it only inside the mutating functions, so a
# function-local import lets readers see the dependency without it being
# part of the module's import surface).
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    from ai_dev.timeutil import utc_now_iso

    return utc_now_iso()
