"""Shell Verifier leg - v0.2 ticket 03 (spec §9.5, §18.4, §24, §26.3).

The Verifier is the third checking role in the v0.2 loop, but unlike the Code
Reviewer (§9.3) and Spec Gap Analyst (§9.4) it is a **non-agent run kind**: a
deterministic shell adapter that does NOT go through ``claude -p`` and does NOT
call a model (§9.5 MVP prioritises the shell adapter). Given an implement run's
workspace changes, it:

1. reads the lane's *declared* verify command set (pytest/mypy/build) from the
   frozen lane-graph's ``verification_commands`` field - the "feature's verifier
   config" source named in the ticket (parsed off ``LaneEntry`` so the lane-graph
   parser stays the single source of truth for the §7.5 lane-entry shape);
2. runs each command in the implement run's ``workspace/`` directory (cwd), one
   by one, capturing each command's ``exit_code`` + ``stdout``/``stderr``
   summary - never stopping at the first failure, so the report carries the
   complete pass/fail picture;
3. rolls the per-command results up into the lane-level
   ``verification-report.{md,json}`` §4.4 double product with an overall
   ``verdict`` (``pass`` iff every command exited 0).

Two contract points the ticket pins explicitly (§9.5 vs §15):

* the verifier outputs a **report**, NOT ``issues[]``. The checking roles that
  emit ``issues[]`` are the reviewer + gap (§15); verification pass/fail is a
  separate, independent condition the §18.4 lane gate consumes alongside the
  issue bundle. So this report has ``commands[]`` + ``verdict`` and no
  ``issues`` field.
* it is deterministic shell only - no profile, no token, no ``run_headless``,
  no ``validate_run`` (the §14 three checks are for agent runs that produce a
  ``result.json``; a shell run has no agent output to schema-check). The
  verifier's own correctness is the exit codes it captures.

Fail-loud (§24.2): a lane with no ``verification_commands`` declared (the
"缺失" case), a missing/malformed command spec, a missing ``implement-result``,
or an unfrozen precondition raises ``ValueError`` before any command runs - these
are config/precondition breaches that need human triage, not silent skips. A
command that *runs* and exits non-zero (or times out, or its binary is missing)
is NOT an exception: it is a captured verification failure recorded in the
report with ``verdict: fail``; the CLI surfaces it with a non-zero exit code
(§24.1 lists ``verification command failed`` / ``timeout`` as failure types).

Path-space note (v0.2): per §6 the report nests under
``lanes/<lane_id>/verification/verification-report.{md,json}``, alongside the
``review/`` and ``spec-gap/`` subdirs (ticket 02). The verifier allocates NO
``RUN-NNN`` - it is a non-agent run kind, so it produces the lane-level report
directly and appends one ``verify`` audit record (no ``prepare_run`` / ``run``
/ ``validate`` records, which are the agent-run lifecycle).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ai_dev.audit import append_audit_event
from ai_dev.implement_leg import read_lane_entry
from ai_dev.json_artifact import read_json_object
from ai_dev.lane_worktree import (
    WORKTREE_LIFECYCLE_ACTIVE,
    load_lane_worktree,
)
from ai_dev.paths import (
    LANES_DIR,
    WORKSPACE_DIR,
    feature_dir,
    run_dir,
)
from ai_dev.status import frozen_artifacts_status
from ai_dev.templates import LANE_GRAPH_YML
from ai_dev.timeutil import elapsed_ms_between, utc_now_iso

# Lane-level §4.4 double-product filenames (public so later tickets / tests
# reference one source of truth for the on-disk layout, §6
# ``lanes/LANE-001/verification/``). §6 nests the verifier report in its own
# ``verification/`` subdir under the lane, like ``review/`` and ``spec-gap/``.
VERIFICATION_DIR = "verification"
VERIFICATION_REPORT_MD = "verification-report.md"
VERIFICATION_REPORT_JSON = "verification-report.json"

# The single role this leg embodies (§9.5). Pinned, not caller-supplied: the
# shell verifier is the Verifier role by definition.
_VERIFIER_ROLE = "Verifier"
# §9.5 adapter kind: the verifier "can be a shell script adapter / Coding Agent
# Profile / CI adapter"; v0.2 ships the shell adapter. Recorded on the report so
# a later agent-profile verifier is distinguishable from this one.
_VERIFY_KIND = "shell"

# The audit event name (§2.1 traceability). The verifier appends one ``verify``
# record - not the agent-run prepare/run/validate triplet, since it allocates no
# RUN-NNN and invokes no model.
_VERIFY_EVENT = "verify"

# §24.1 lists ``timeout`` as a failure type. Each command is bounded so a hung
# verify command cannot hang the verifier; a timeout is captured as a command
# failure (exit code ``_TIMEOUT_EXIT``), not raised.
DEFAULT_TIMEOUT = 300

# stdout/stderr are captured in full then bounded to this many chars (keeping the
# tail, where tracebacks land) so the report stays readable while preserving the
# error-bearing part of the output (§24.2 "保存 stdout/stderr"). Typical command
# output is far smaller, so the cap only bites on pathological verbosity.
_MAX_OUTPUT_CHARS = 8192

# Sentinel exit codes for the two non-``subprocess`` failure modes. Both are
# non-zero so ``CommandResult.passed`` (``exit_code == 0``) is False, flowing
# naturally to ``verdict: fail`` without special-casing.
_TIMEOUT_EXIT = -1  # negative so it can never collide with a real exit code
_NOT_FOUND_EXIT = 127  # the shell convention for "command not found"


# ---------------------------------------------------------------------------
# The declared verify command set (§9.5 source: lane-graph verification_commands).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifyCommand:
    """One declared verify command (§9.5): a short label + the shell command.

    ``name`` is the human/label (e.g. ``pytest`` / ``mypy`` / ``build``) the
    report groups by; ``command`` is the verbatim shell string executed in the
    implement run's workspace. Both are non-empty - a nameless or commandless
    entry is a malformed declaration (§24.2).
    """

    name: str
    command: str


def _parse_verify_command(
    raw: Mapping[str, Any], lane_id: str, index: int
) -> VerifyCommand:
    """Parse one ``verification_commands`` mapping into a ``VerifyCommand``.

    Validates the two semantic fields the lane-graph parser only shape-checked:
    ``name`` and ``command`` must both be present, non-empty strings. A missing
    or empty field is a config error (§24.2) - silently defaulting would hide a
    half-written declaration and then run nothing under that label.
    """
    name = raw.get("name")
    command = raw.get("command")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(
            f"lane {lane_id!r} verification_commands[{index}] in "
            f"{LANE_GRAPH_YML} has no non-empty 'name' (§9.5)"
        )
    if not isinstance(command, str) or not command.strip():
        raise ValueError(
            f"lane {lane_id!r} verification_commands[{index}] "
            f"(name={name!r}) in {LANE_GRAPH_YML} has no non-empty 'command' "
            f"(§9.5)"
        )
    return VerifyCommand(name=name.strip(), command=command.strip())


def read_verification_commands(
    feature_root: Path, lane_id: str
) -> list[VerifyCommand]:
    """Read the lane's declared verify command set (§9.5, ticket 03 source).

    Parses ``04-lane-graph.yml``'s lane entry (reusing ``read_lane_entry``, so
    the lane-graph parser stays the single source of truth) and converts each
    ``verification_commands`` mapping into a typed ``VerifyCommand``. Fail-loud
    (§24.2) when the lane declares NO commands - the "缺失" case the ticket
    names: a lane with nothing to verify is a precondition breach (the Planner
    must declare the verify command set at the task gate), not a silent
    vacuous-pass. A malformed command spec is rejected by ``_parse_verify_command``.
    """
    lane = read_lane_entry(feature_root, lane_id)
    commands = [
        _parse_verify_command(raw, lane_id, i)
        for i, raw in enumerate(lane.verification_commands)
    ]
    if not commands:
        raise ValueError(
            f"lane {lane_id!r} declares no verification_commands in "
            f"{LANE_GRAPH_YML} (§9.5); the Planner must declare the verify "
            f"command set (pytest/mypy/build) at the task gate before verifying"
        )
    return commands


# ---------------------------------------------------------------------------
# The implement run whose workspace is verified (ticket 01 -> 03).
# ---------------------------------------------------------------------------


def read_implement_run_id(feature_root: Path, lane_id: str) -> str:
    """Return the implement run id backing the lane (from implement-result.json).

    The verifier runs its commands in the implement run's ``workspace/``, so it
    needs the run id. Reads the lane's ``implement-result.json`` (written by
    ticket 01) for the ``run`` field. Fail-loud (§24.2) when the lane has no
    ``implement-result`` (no implement run to verify) or the rollup lacks a run
    id - a verifier with nothing to verify is a precondition breach, not a
    silent no-op (mirrors ``checking_legs.read_implement_run_facts``).
    """
    implement_result_path = (
        feature_root / LANES_DIR / lane_id / "implement-result.json"
    )
    implement_result = read_json_object(implement_result_path)
    if implement_result is None:
        raise ValueError(
            f"no implement-result.json under lanes/{lane_id}/ for feature "
            f"{feature_root.name}; run the implementer leg first (ticket 01) "
            f"before verifying (§26.3)"
        )
    implement_run_id = implement_result.get("run")
    if not isinstance(implement_run_id, str) or not implement_run_id:
        raise ValueError(
            f"implement-result.json at {implement_result_path} has no 'run' id "
            f"(§24.2)"
        )
    return implement_run_id


# ---------------------------------------------------------------------------
# §4.2 frozen precondition (shared with the other legs).
# ---------------------------------------------------------------------------


def _require_frozen(feature_root: Path) -> None:
    """Reject an unfrozen precondition before reading commands or running any.

    The verifier runs after the implementer leg, which already required frozen
    tasks + lane-graph; re-checking is cheap defense-in-depth and keeps the
    precondition shape identical to the implementer / checking legs. The verify
    command set lives in the (frozen) lane-graph, so an unfrozen graph means the
    declared commands are not yet stable.
    """
    frozen = frozen_artifacts_status(feature_root)
    if not (frozen.get("tasks") and frozen.get("lane_graph")):
        raise ValueError(
            "verifier requires frozen tasks + lane_graph (§4.2); the verify "
            "command set lives in the lane-graph - freeze them at the task gate "
            "first"
        )


# ---------------------------------------------------------------------------
# Command execution: deterministic shell, no model (§9.5).
# ---------------------------------------------------------------------------


def _summarize_output(text: str | None, max_chars: int = _MAX_OUTPUT_CHARS) -> str:
    """Bound ``text`` to ``max_chars`` keeping the tail (where tracebacks land).

    ``None`` (no output captured) becomes ``""``. Short output passes through
    verbatim; only pathological verbosity is trimmed, with a prefix marker
    recording how many head chars were dropped so the truncation is visible, not
    silent (§24.2 preserve-artifacts spirit: keep the error-bearing part).
    """
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    dropped = len(text) - max_chars
    return f"...[truncated {dropped} head chars]...{text[-max_chars:]}"


def run_verify_command(
    cmd: VerifyCommand, cwd: Path, *, timeout: float = DEFAULT_TIMEOUT
) -> CommandResult:
    """Execute one verify command in ``cwd`` and capture its outcome (§9.5).

    Runs ``cmd.command`` through the shell (``shell=True``) with ``cwd`` as the
    working directory - the implement run's ``workspace/``, so the command sees
    the implemented files. stdout/stderr are captured (text, replacement errors
    so binary-ish output cannot crash the capture) and summarised. Three
    outcomes, all returned as a ``CommandResult`` (never raised - a verification
    failure is a captured result, not an exception, §24.1/§24.2):

    * normal completion -> ``exit_code`` from the shell; ``passed`` iff 0;
    * timeout (``subprocess.TimeoutExpired``, §24.1) -> ``_TIMEOUT_EXIT`` with
      whatever partial output was captured;
    * missing ``cwd`` (``FileNotFoundError`` - the workspace is gone) ->
      ``_NOT_FOUND_EXIT`` with a clear stderr. (A missing *binary* does NOT
      raise with ``shell=True`` - the shell reports it as exit 127 + stderr
      "command not found", which flows through as a normal non-zero exit.)

    ``shell=True`` is acceptable because the commands come from the frozen,
    human-approved lane-graph (trusted config), not untrusted input.
    """
    try:
        completed = subprocess.run(
            cmd.command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        # §24.1 timeout: a hung verify command is a verification failure, not a
        # crash. ``exc.stdout``/``exc.stderr`` carry the partial output captured
        # before the kill (str under text=True, but defensively coerced).
        partial_out = exc.stdout if isinstance(exc.stdout, str) else ""
        partial_err = exc.stderr if isinstance(exc.stderr, str) else ""
        return CommandResult(
            name=cmd.name,
            command=cmd.command,
            exit_code=_TIMEOUT_EXIT,
            stdout=_summarize_output(partial_out),
            stderr=_summarize_output(partial_err)
            or f"command timed out after {timeout}s",
        )
    except FileNotFoundError as exc:
        # ``cwd`` does not exist (the implement run's workspace is gone). With
        # ``shell=True`` a missing *binary* does not land here - the shell
        # returns 127 - so this is specifically a broken precondition surfaced
        # as a command failure rather than crashing the verifier.
        return CommandResult(
            name=cmd.name,
            command=cmd.command,
            exit_code=_NOT_FOUND_EXIT,
            stdout="",
            stderr=f"could not run command (working directory missing): {exc}",
        )
    return CommandResult(
        name=cmd.name,
        command=cmd.command,
        exit_code=completed.returncode,
        stdout=_summarize_output(completed.stdout),
        stderr=_summarize_output(completed.stderr),
    )


# ---------------------------------------------------------------------------
# Lane-level verification-report rollup (§4.4 double product, no issues[]).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    """One verify command's captured outcome.

    ``passed`` is ``exit_code == 0`` (so timeouts / not-found / non-zero exits
    are all ``False``); ``stdout``/``stderr`` are bounded summaries
    (``_summarize_output``). Carried verbatim into the report's ``commands[]``.
    """

    name: str
    command: str
    exit_code: int
    stdout: str
    stderr: str

    @property
    def passed(self) -> bool:
        """``True`` iff the command exited 0 (the sole pass criterion, §9.5)."""
        return self.exit_code == 0


def _build_rollup(
    feature_id: str,
    lane_id: str,
    implement_run_id: str,
    results: Sequence[CommandResult],
    started_at: str,
    ended_at: str,
) -> dict[str, Any]:
    """Assemble the ``verification-report.json`` document from command results.

    Field-complete against the per-command facts plus the overall verdict: each
    command's ``name`` / ``command`` / ``exit_code`` / ``passed`` /
    ``stdout`` / ``stderr`` is carried verbatim, ``verdict`` is ``pass`` iff
    every command passed, and ``passed_count`` / ``command_count`` are precomputed
    for the §18.4 gate and the human mirror. Deliberately NO ``issues[]`` (§9.5
    vs §15 - the verifier emits a report, not issues) and NO ``validation``
    block (no agent ``result.json`` to §14-validate) and NO ``run_metadata``
    (no RUN-NNN / profile - this is a deterministic shell run, not an agent run).
    """
    passed_count = sum(1 for r in results if r.passed)
    return {
        "feature": feature_id,
        "lane": lane_id,
        "implement_run": implement_run_id,
        "role": _VERIFIER_ROLE,
        "kind": _VERIFY_KIND,
        "verdict": "pass" if results and passed_count == len(results) else "fail",
        "command_count": len(results),
        "passed_count": passed_count,
        "commands": [
            {
                "name": r.name,
                "command": r.command,
                "exit_code": r.exit_code,
                "passed": r.passed,
                "stdout": r.stdout,
                "stderr": r.stderr,
            }
            for r in results
        ],
        "started_at": started_at,
        "ended_at": ended_at,
    }


def _verification_report_md(rollup: Mapping[str, Any]) -> str:
    """Render the ``verification-report.md`` human-readable mirror (§4.4)."""
    commands = rollup.get("commands") or []
    verdict = rollup.get("verdict")
    # One line per command: name, pass/fail, exit code, and a compact stderr
    # excerpt (stdout is usually progress noise; stderr carries the failure).
    cmd_lines = (
        "\n".join(
            f"- {c.get('name')}: {'PASS' if c.get('passed') else 'FAIL'} "
            f"(exit_code={c.get('exit_code')})"
            + (f" :: {c.get('stderr', '').strip()[:200]}" if c.get("stderr") else "")
            for c in commands
        )
        or "_none_"
    )
    return (
        f"# Verification Report - {rollup.get('lane')}\n"
        f"\n"
        f"- feature: {rollup.get('feature')}\n"
        f"- lane: {rollup.get('lane')}\n"
        f"- implement_run: {rollup.get('implement_run')}\n"
        f"- role: {rollup.get('role')}\n"
        f"- kind: {rollup.get('kind')} (§9.5 shell adapter; deterministic, no "
        f"model)\n"
        f"- verdict: **{verdict}** "
        f"({rollup.get('passed_count')}/{rollup.get('command_count')} commands "
        f"passed)\n"
        f"- started_at: {rollup.get('started_at')}\n"
        f"- ended_at: {rollup.get('ended_at')}\n"
        f"\n"
        f"## Commands ({len(commands)})\n"
        f"\n"
        f"{cmd_lines}\n"
        f"\n"
        f"> The verifier outputs a report (pass/fail), not `issues[]` (§9.5 vs "
        f"§15). This `verdict` is an independent condition the §18.4 lane gate "
        f"consumes alongside the review / spec-gap issue bundles.\n"
    )


def write_verification_report(
    feature_root: Path,
    lane_id: str,
    *,
    implement_run_id: str,
    results: Sequence[CommandResult],
    started_at: str,
    ended_at: str,
) -> tuple[Path, Path]:
    """Write the lane-level ``verification-report.{md,json}`` rollup (§4.4, §6).

    Rolls the per-command ``CommandResult`` list up into the §4.4 double product
    under ``lanes/<lane_id>/verification/``. The JSON is the canonical
    machine-readable rollup; the markdown is the human mirror. Returns
    ``(md_path, json_path)``. Pure writer: runs no commands and reads nothing
    from disk beyond ``feature_root`` (the caller - ``run_verifier`` - already
    ran the commands and gathered the results), so it is unit-testable from
    literals (mirrors ``write_implement_result`` / ``write_review_report``).
    """
    feature_id = feature_root.name
    rollup = _build_rollup(
        feature_id, lane_id, implement_run_id, results, started_at, ended_at
    )
    report_dir = feature_root / LANES_DIR / lane_id / VERIFICATION_DIR
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / VERIFICATION_REPORT_JSON
    md_path = report_dir / VERIFICATION_REPORT_MD
    json_path.write_text(json.dumps(rollup, indent=2, ensure_ascii=False) + "\n")
    md_path.write_text(_verification_report_md(rollup))
    return md_path, json_path


# ---------------------------------------------------------------------------
# Orchestration: read commands -> find workspace -> run each -> rollup -> audit.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifierResult:
    """The verifier leg's return: the captured command outcomes + the report.

    Carries the lane / feature / implement-run identity, the overall ``verdict``
    (``pass`` iff every command passed), the full ``command_results`` list, and
    the paths to the lane-level report products. No ``run_id`` - the verifier
    allocates no ``RUN-NNN`` (it is a non-agent run kind, §9.5). No
    ``validation`` - there is no agent ``result.json`` to §14-validate.
    """

    lane_id: str
    feature_id: str
    implement_run_id: str
    verdict: str
    command_results: list[CommandResult]
    report_md: Path
    report_json: Path


def _lane_worktree_root(
    repo_root: Path, feature_id: str, lane_id: str
) -> Path | None:
    """Return the lane's active worktree cwd, or ``None`` when no active one.

    v0.7 (ADR-0009 D2): the implementer leg writes the files under
    verification into the lane's git worktree (under the worktree's
    ``workspace/`` subdir, per the ``workspace/`` prefix convention the
    Planner's tasks prompt instructs the implementer to use); the verifier
    runs its commands with that ``workspace/`` as cwd so it sees the
    implemented package + ``tests/`` and the Planner's workspace-relative
    verify commands (``PYTHONPATH=. python -m pytest tests``,
    ``python -m mypy <pkg>``) resolve. When the worktree has been created
    but not yet populated with a ``workspace/`` subdir, the bare worktree
    root is returned (the else-branch). When the lane has no active
    worktree (v0.1-v0.6 run, a removed worktree, or a test that bypassed
    the lane-aware path), this returns ``None`` and the caller falls back
    to the implement run's ``workspace/``.
    """
    feature_root = feature_dir(repo_root, feature_id)
    metadata = load_lane_worktree(feature_root, lane_id)
    if metadata is None or metadata.get("lifecycle") != WORKTREE_LIFECYCLE_ACTIVE:
        return None
    path_str = metadata.get("path")
    if not isinstance(path_str, str) or not path_str:
        return None
    path = Path(path_str)
    if not path.is_dir():
        return None
    # Prefer <worktree>/workspace/ (the cwd the implementer wrote the
    # package + tests/ into, and the cwd the Planner's workspace-relative
    # verify commands assume) when the implementer has populated it; fall
    # back to the bare worktree root for a not-yet-populated worktree.
    workspace = path / WORKSPACE_DIR
    if workspace.is_dir():
        return workspace
    return path


def run_verifier(
    repo_root: Path,
    feature_id: str,
    lane_id: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    started_at: str | None = None,
    ended_at: str | None = None,
    origin: str | None = None,
) -> VerifierResult:
    """Run the shell Verifier leg end to end (v0.2 ticket 03, §9.5).

    Composes the deterministic seams: ``read_verification_commands`` (the
    declared command set from the frozen lane-graph), ``read_implement_run_id``
    (the implement run whose workspace is verified), ``run_verify_command`` per
    command (deterministic shell, no model), then ``write_verification_report``
    (the lane-level §4.4 double product) and one ``verify`` audit record.

    v0.7 (ADR-0009 D2): the verify commands run with ``cwd=<worktree>/workspace/``
    (the same cwd the implementer leg wrote the package + ``tests/`` into, and the
    cwd the Planner's workspace-relative verify commands assume) when an active
    lane worktree exists; otherwise the v0.1-v0.6 cwd
    (``implement_run/workspace/``) is used. This way a file written into the
    worktree between implement and verify is visible to the verify command (the
    §18.4 lane gate's "verification of what was actually implemented"
    precondition). Returns a
    ``VerifierResult`` whether every command passed or some failed - a
    verification failure is a captured result (reported, non-zero CLI exit),
    not a raised exception. It *does* raise ``ValueError`` (§24.2 fail loud)
    when the precondition is broken: an unfrozen feature, a lane with no
    declared verify commands, a missing ``implement-result``, or a missing
    cwd (worktree or workspace) - all config/precondition breaches needing
    human triage, raised before any command runs.

    ``timeout`` bounds each command (§24.1 timeout); ``started_at`` /
    ``ended_at`` are injectable for deterministic tests (defaulting to
    ``utc_now_iso()`` captured around the command loop).
    """
    feature_root = feature_dir(repo_root, feature_id)
    if not feature_root.is_dir():
        raise ValueError(
            f"feature run {feature_id} not found under {repo_root}"
        )
    _require_frozen(feature_root)
    commands = read_verification_commands(feature_root, lane_id)
    implement_run_id = read_implement_run_id(feature_root, lane_id)

    # v0.7 cwd resolution: prefer the lane's worktree workspace (the canonical
    # cwd of the implement leg's writes - ``<worktree>/workspace/`` when the
    # implementer populated it, else the bare worktree root) when an active
    # worktree exists; fall back to the implement run's ``workspace/`` (the
    # v0.1-v0.6 cwd) for the non-lane path. The fallback keeps the v0.1-v0.6
    # tests and ticket-03 e2e green without forcing every caller through the
    # lane worktree lifecycle; the lane-worktree path is the v0.7 default.
    worktree_root = _lane_worktree_root(repo_root, feature_id, lane_id)
    if worktree_root is not None:
        cwd = worktree_root
    else:
        implement_run_root = run_dir(repo_root, feature_id, implement_run_id)
        workspace = implement_run_root / WORKSPACE_DIR
        if not workspace.is_dir():
            raise ValueError(
                f"implement run {implement_run_id} has no workspace/ at "
                f"{workspace}; cannot run verify commands against it (§24.2)"
            )
        cwd = workspace

    started = started_at if started_at is not None else utc_now_iso()
    results = [
        run_verify_command(cmd, cwd, timeout=timeout) for cmd in commands
    ]
    ended = ended_at if ended_at is not None else utc_now_iso()

    md_path, json_path = write_verification_report(
        feature_root,
        lane_id,
        implement_run_id=implement_run_id,
        results=results,
        started_at=started,
        ended_at=ended,
    )
    verdict = "pass" if results and all(r.passed for r in results) else "fail"

    append_audit_event(
        feature_root,
        event=_VERIFY_EVENT,
        payload={
            "lane": lane_id,
            "feature": feature_id,
            "implement_run": implement_run_id,
            "verdict": verdict,
            "command_count": len(results),
            "passed_count": sum(1 for r in results if r.passed),
            "commands": [
                {
                    "name": r.name,
                    "command": r.command,
                    "exit_code": r.exit_code,
                    "passed": r.passed,
                }
                for r in results
            ],
            "elapsed_ms": elapsed_ms_between(started, ended),
        },
        origin=origin,
    )

    return VerifierResult(
        lane_id=lane_id,
        feature_id=feature_id,
        implement_run_id=implement_run_id,
        verdict=verdict,
        command_results=results,
        report_md=md_path,
        report_json=json_path,
    )
