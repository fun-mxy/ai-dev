"""Agent-run wrapper - env isolation, invocation, capture (ticket 03 + ADR-0005).

The wrapper is the run-adapter's execution seam: given a prepared ``RUN-NNN``
directory (``prepare_run``) and a parsed ``AgentProfile`` (``load_profile``), it
builds a self-contained prompt, isolates the child environment (§10.3), invokes
the profile's CLI headless, captures stdout/stderr, computes ``changed_files``
(§13.2/§14.2), and writes ``metadata.json`` (§13.2). It is the deterministic
Python runtime standing between ``prepare_run`` and ``validate-run``.

ADR-0005 (v0.5 ticket 02) made the wrapper multi-CLI: ``run_headless`` is now a
thin dispatcher that resolves an ``AgentRunner`` adapter from ``profile.cli``
(D1/D2) and delegates the adapter-specific steps - child-env build (strip +
inject), argv build, capture, and the ``changed_files`` wrapper-owned subtract
set. ``ClaudeRunner`` is the v0.0-v0.4 behavior extracted (behavior-identical);
``CodexRunner`` is the codex adapter (``codex exec -``, ``-s workspace-write``).
The shared orchestration (run-dir precondition, token-source resolution,
snapshot diff, metadata, audit) stays in ``run_headless``. The claude-specific
helpers (``STRIP_VARS`` / ``inject_profile_env`` / ``build_cli_flags`` /
``render_env_snapshot`` / ``WRAPPER_OWNED_RE``) remain at module scope - the
``ClaudeRunner`` delegates to them, so the existing claude tests exercise the
exact same code path.


The prototype ``prototype/adapter/run.sh`` is the seed: this module ports its
``snapshot_tree`` / env-snapshot / changed-file-diff / metadata logic into the
typed Python data plane, parameterised by an ``AgentProfile`` instead of the
prototype's hardcoded cc-glm52 constants.

Token handling (§10.2, invariant #11): the token *value* is read from the live
environment by source *name* (``token_source_var``) inside ``run_headless`` and
passed only to ``inject_profile_env`` - it lives on no returned object and is
never persisted. The env snapshot redacts every value to ``=<set>``;
``metadata.json`` carries no token field. The §10.3 strip removes the parent
Claude-Code identity and model-alias vars so the child cannot inherit the
parent session or fall back to a non-profile alias.

Scope boundary (ticket 03 vs 04): the wrapper *captures* the run; it does not
*validate* ``result.json`` against the schema or the file boundary. Schema,
boundary, and frozen-artifact validation are ticket 04's ``validate-run`` -
kept out of this module so the two tickets do not entangle.
"""

from __future__ import annotations

import abc
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ai_dev.audit import append_audit_event
from ai_dev.paths import (
    METADATA_JSON,
    OUTPUT_DIR,
    RESULT_JSON,
    RESULT_MD,
    WORKSPACE_DIR,
    feature_dir,
    run_dir,
)
from ai_dev.profiles import AgentProfile, token_source_var
from ai_dev.timeutil import elapsed_ms_between, utc_now_iso

# §10.3 explicit strip set - the parent Claude-Code identity vars and the
# model-alias overrides a nested ``claude`` would otherwise inherit. The
# profile's ``env_strip_pattern`` (§10.3) is applied on top of this list, so a
# profile can extend but not narrow the hygiene baseline. Mirrors the
# prototype ``run.sh`` unset block verbatim (spec lists these by name).
STRIP_VARS: tuple[str, ...] = (
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDECODE",
    "AI_AGENT",
    "CLAUDE_EFFORT",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
    "ANTHROPIC_REASONING_MODEL",
    "IMG_BASE_URL",
)

# The Claude CLI reads ``ANTHROPIC_AUTH_TOKEN`` for 3P Anthropic-compatible
# backends (§10.1/§10.3). A profile may override the injection target via
# ``auth_target``; absent one, this is the default.
_DEFAULT_AUTH_TARGET = "ANTHROPIC_AUTH_TOKEN"

# The three §10.3 injection targets, in spec order.
_BASE_URL_VAR = "ANTHROPIC_BASE_URL"
_MODEL_VAR = "ANTHROPIC_MODEL"

# env-snapshot redaction: every captured var's value is replaced with this, so
# the snapshot proves a var is *set* without leaking its value (§10.2).
_REDACTED = "<set>"


def _strip_pattern(profile: AgentProfile) -> re.Pattern[str] | None:
    """Compile the profile's §10.3 ``env_strip_pattern`` once, or ``None``.

    The pattern was validated at load time (``profiles._parse_env_strip_pattern``
    compiles it), so compilation here cannot fail; recompiling keeps this module
    self-contained rather than reaching into the loader's private state.
    """
    if profile.env_strip_pattern is None:
        return None
    return re.compile(profile.env_strip_pattern)


def strip_parent_identity(
    parent_env: Mapping[str, str], profile: AgentProfile
) -> dict[str, str]:
    """Return ``parent_env`` with the §10.3 contamination vars removed.

    Strips the explicit ``STRIP_VARS`` set, then any var whose name matches the
    profile's ``env_strip_pattern`` (§10.3 - a profile may declare a regex to
    unset all matching vars, supplementing the explicit list). Non-matching
    vars (``PATH``, ``HOME``, ...) survive so the child process can still locate
    binaries. Does not mutate ``parent_env`` - returns a fresh dict.
    """
    stripped: dict[str, str] = {
        k: v for k, v in parent_env.items() if k not in STRIP_VARS
    }
    pattern = _strip_pattern(profile)
    if pattern is not None:
        stripped = {k: v for k, v in stripped.items() if not pattern.search(k)}
    return stripped


def inject_profile_env(
    env: Mapping[str, str], profile: AgentProfile, token_value: str
) -> dict[str, str]:
    """Return a copy of ``env`` with the §10.3 profile env injected.

    Applies ``profile.extra_env`` first, then the explicit §10.3 fields
    override: ``ANTHROPIC_BASE_URL`` ← ``profile.base_url`` (if set),
    ``ANTHROPIC_MODEL`` ← ``profile.model`` (if set), and the token ←
    ``token_value`` mapped onto ``profile.auth_target`` (default
    ``ANTHROPIC_AUTH_TOKEN``). Explicit fields winning over ``extra_env`` keeps
    §10.3 authoritative when the two disagree. Pure: the input ``env`` is not
    mutated.

    The token *value* enters here and nowhere else - the caller (``run_headless``)
    reads it from the environment by source name and passes it straight through;
    this function does not log or persist it.
    """
    result: dict[str, str] = dict(env)
    for key, value in profile.extra_env.items():
        result[key] = value
    if profile.base_url is not None:
        result[_BASE_URL_VAR] = profile.base_url
    if profile.model is not None:
        result[_MODEL_VAR] = profile.model
    result[profile.auth_target or _DEFAULT_AUTH_TARGET] = token_value
    return result


def build_child_env(
    profile: AgentProfile,
    parent_env: Mapping[str, str],
    token_value: str,
) -> dict[str, str]:
    """Build the child ``claude`` env: strip the parent, then inject the profile.

    Composes ``strip_parent_identity`` and ``inject_profile_env`` - the two
    §10.3 halves - so the subprocess env carries the three target vars
    (``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_AUTH_TOKEN`` / ``ANTHROPIC_MODEL``)
    and none of the parent Claude-Code identity / alias contamination.
    """
    env = strip_parent_identity(parent_env, profile)
    return inject_profile_env(env, profile, token_value)


# The var-name grep patterns for each adapter's env snapshot. The snapshot
# proves the token was injected: it lists every CLI-relevant var present in the
# child env, each value redacted to ``=<set>``. claude reads ``ANTHROPIC_*``;
# codex reads ``OPENAI_API_KEY`` (OpenAI provider) plus any ``CODEX_*`` / ``AI_``
# var. case-insensitive, like the prototype's ``env | grep -iE '...'``.
_CLAUDE_SNAPSHOT_RE: re.Pattern[str] = re.compile(r"claude|anthropic|^AI_", re.IGNORECASE)
_CODEX_SNAPSHOT_RE: re.Pattern[str] = re.compile(r"openai|codex|^AI_", re.IGNORECASE)


def _render_snapshot(
    child_env: Mapping[str, str],
    profile: AgentProfile,
    token_source: str | None,
    timestamp: str | None,
    label: str,
    pattern: re.Pattern[str],
) -> str:
    """Render a redacted child-env snapshot for one adapter (names only).

    Shared core behind ``render_env_snapshot`` (claude) and
    ``render_codex_env_snapshot``: ``label`` names the engine in the header line,
    ``pattern`` selects which var names survive the redaction grep. Every
    captured value is replaced with ``<set>`` so the snapshot proves a var is
    present without leaking its value (§10.2). The header records the profile
    name, ``base_url``, ``model`` and the token *source* name (never the value),
    matching the prototype's ``env-snapshot.txt`` shape.
    """
    stamp = timestamp if timestamp is not None else utc_now_iso()
    header = (
        f"# Child {label} env snapshot (names only; values redacted) - {stamp}\n"
        f"# profile={profile.name} base_url={profile.base_url} "
        f"model={profile.model} token_src={token_source}"
    )
    rows = sorted(k for k in child_env if pattern.search(k))
    body = "\n".join(f"{k}={_REDACTED}" for k in rows)
    return f"{header}\n{body}\n" if body else f"{header}\n"


def render_env_snapshot(
    child_env: Mapping[str, str],
    profile: AgentProfile,
    token_source: str | None,
    timestamp: str | None = None,
) -> str:
    """Render the §10.3 child-env snapshot - NAMES ONLY, values redacted.

    Captures every ``claude``/``anthropic``/``AI_`` var present in the child env
    (the set the Claude CLI actually reads), each value replaced with
    ``<set>``. After ``build_child_env`` this is exactly the three target vars;
    any extra entry would betray a strip miss.
    """
    return _render_snapshot(
        child_env, profile, token_source, timestamp, "claude", _CLAUDE_SNAPSHOT_RE
    )


def render_codex_env_snapshot(
    child_env: Mapping[str, str],
    profile: AgentProfile,
    token_source: str | None,
    timestamp: str | None = None,
) -> str:
    """Render the codex child-env snapshot - NAMES ONLY, values redacted.

    The codex analogue of ``render_env_snapshot``: greps ``openai``/``codex``/
    ``AI_`` (the vars codex reads - ``OPENAI_API_KEY`` on the OpenAI-provider
    path, any ``CODEX_*`` config) so the snapshot proves the token was injected
    by name without leaking its value (§10.2, invariant #11). When the token
    source is unset (custom-provider / stored-cred path, D3 amended) the header
    still records ``token_src=None`` and the body is empty - honest about the
    no-injection path.
    """
    return _render_snapshot(
        child_env, profile, token_source, timestamp, "codex", _CODEX_SNAPSHOT_RE
    )


# ---------------------------------------------------------------------------
# §13.2 changed_files: before/after RUN-dir snapshot diff.
# ---------------------------------------------------------------------------

# Wrapper-owned output artifacts - subtracted from the diff so ``changed_files``
# reports only files the *agent* wrote. The before/after file-tree snapshots are
# held in memory (not persisted), so the regex covers only the wrapper-written
# output files: stdout/stderr capture, metadata, the env snapshot, and the
# §14.2 auto-memory-off settings file. Mirrors the prototype's
# ``WRAPPER_OWNED_RE`` minus its persisted snapshot files.
WRAPPER_OWNED_RE: re.Pattern[str] = re.compile(
    r"^output/(stdout\.log|stderr\.log|metadata\.json|env-snapshot\.txt|"
    r"\.run-settings\.json)$"
)

# The codex adapter's wrapper-owned subtract set: codex writes no
# ``.run-settings.json`` (no ``--settings`` analogue; ``--ephemeral`` is the
# session-persistence hygiene flag, carried in argv). So it is the claude set
# minus the settings file - stdout/stderr/metadata/env-snapshot only.
CODEX_WRAPPER_OWNED_RE: re.Pattern[str] = re.compile(
    r"^output/(stdout\.log|stderr\.log|metadata\.json|env-snapshot\.txt)$"
)

# Compiler-emitted Python bytecode cache - subtracted from the diff regardless of
# adapter, alongside the adapter's wrapper-owned set. A ``.pyc`` under
# ``__pycache__/`` is a non-deterministic build artifact (its name stamps the
# Python/pytest version, e.g. ``test_x.cpython-312-pytest-9.1.1.pyc``), emitted
# when the agent imports or runs the module during implementation - it is never
# source the agent *authors*. Excluding it keeps ``changed_files`` (and thus the
# §14.2 boundary check + the final-report Q1 traceability index) to authored
# files only. Shared across adapters because any Python-touching run may emit it.
_BUILD_ARTIFACT_RE: re.Pattern[str] = re.compile(r"(^|/)__pycache__/.*\.pyc$")


def snapshot_tree(run_dir: Path) -> dict[str, tuple[int, int]]:
    """Inventory ``run_dir`` as ``{RUN-relative path: (size, mtime_ns)}``.

    Walks the tree deterministically (sorted dirs/files) so two snapshots of an
    unchanged tree are byte-identical. Each entry carries size and mtime in
    nanoseconds - the pair the diff compares - so a file the agent creates or
    edits surfaces as a metadata delta. The prototype's ``snapshot_tree``
    heredoc is the seed.
    """
    rows: dict[str, tuple[int, int]] = {}
    for root, dirs, files in os.walk(run_dir):
        dirs.sort()
        files.sort()
        for name in files:
            path = os.path.join(root, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            rel = os.path.relpath(path, run_dir)
            rows[rel] = (st.st_size, st.st_mtime_ns)
    return rows


def compute_changed_files(
    before: Mapping[str, tuple[int, int]],
    after: Mapping[str, tuple[int, int]],
    wrapper_owned: re.Pattern[str],
) -> list[str]:
    """Return the sorted list of agent-written changed files (§13.2/§14.2).

    A path is "changed" when it is present in ``after`` with a
    ``(size, mtime_ns)`` tuple that differs from ``before`` (new or modified).
    Wrapper-owned artifacts (matched by ``wrapper_owned``) are subtracted even
    when new, so stdout/stderr/metadata/snapshots/settings never pollute the
    list. Compiler-emitted Python bytecode (``__pycache__/*.pyc``, matched by
    ``_BUILD_ARTIFACT_RE``) is subtracted too, regardless of adapter: it is a
    non-deterministic build artifact the toolchain emits when a module is
    imported, never source the agent authors - so it stays out of both the
    boundary check and the final-report traceability index. Deletions (in
    ``before``, gone in ``after``) are not reported - the list captures what the
    agent wrote, not what it removed. Sorted for diff-stable output matching the
    prototype.
    """
    changed = [
        path
        for path, meta in after.items()
        if not wrapper_owned.search(path)
        and not _BUILD_ARTIFACT_RE.search(path)
        and before.get(path) != meta
    ]
    return sorted(changed)


# ---------------------------------------------------------------------------
# §13.2 metadata.json: wrapper-computed facts about the run.
# ---------------------------------------------------------------------------


def write_metadata(
    path: Path,
    *,
    run_id: str,
    profile: AgentProfile,
    started_at: str,
    ended_at: str,
    exit_code: int,
    changed_files: list[str],
    lane_id: str | None = None,
    worktree_path: str | None = None,
    branch: str | None = None,
    base_ref: str | None = None,
    commands: list[dict[str, Any]] | None = None,
) -> None:
    """Write the §13.2 ``metadata.json`` with the wrapper-computed fact set.

    Every field from the spec's example is present and sourced from the profile
    (``profile`` / ``cli`` / ``backend`` / ``model``) and the run facts
    (``run_id`` / ``started_at`` / ``ended_at`` / ``exit_code`` /
    ``changed_files``). ``commits`` and ``checks`` are empty in v0.1 - commit
    capture and the §14 verification commands are later tickets' concerns - but
    the fields are present so the schema is stable from day one.

    v0.7 (ADR-0009 D2): when the run was performed in a lane worktree, the
    caller passes the v0.7 lane identity (``lane_id`` / ``worktree_path`` /
    ``branch`` / ``base_ref`` / ``commands``) so the run-home ``metadata.json``
    records the lane context too. A standalone (non-lane) run omits these
    kwargs; the JSON keys are absent rather than null - the §13.2 shape is
    additive, not retroactively required. The lane-level ``metadata.json``
    written by ``lane_run.write_lane_metadata`` is the canonical lane record;
    this is the same fields stamped on the run-home record so the two files
    agree on the lane identity by construction (``run_in_lane_worktree`` is
    the only writer of both).

    No token field is ever written: the profile carries only variable *names*
    (§10.2) and the token value never reaches this function.
    """
    md: dict[str, Any] = {
        "run_id": run_id,
        "profile": profile.name,
        "cli": profile.cli,
        "backend": profile.backend,
        "model": profile.model,
        "started_at": started_at,
        "ended_at": ended_at,
        "exit_code": exit_code,
        "changed_files": changed_files,
        "commits": [],
        "checks": [],
    }
    # v0.7 lane identity: only present when this run was performed in a lane
    # worktree (``run_in_lane_worktree`` always passes the kwargs; a v0.5-era
    # caller that invokes ``run_headless`` without ``lane_context`` omits
    # them and gets the v0.1 shape back).
    if lane_id is not None:
        md["lane_id"] = lane_id
    if worktree_path is not None:
        md["worktree_path"] = worktree_path
    if branch is not None:
        md["branch"] = branch
    if base_ref is not None:
        md["base_ref"] = base_ref
    if commands is not None:
        md["commands"] = commands
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(md, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# §11.1 invocation: prompt + CLI flags + §14.2 auto-memory settings.
# ---------------------------------------------------------------------------

# The default §11.1 headless flag set. ``--permission-mode bypassPermissions``
# is safe because the wrapper enforces the file boundary post-hoc (§14.2); the
# run must not hang on a permission prompt in headless mode.
DEFAULT_PERMISSION_MODE = "bypassPermissions"


def auto_memory_settings() -> dict[str, object]:
    """Return the §14.2 auto-memory-off settings dict for ``--settings``.

    A fresh dict each call, written verbatim to the run's ``.run-settings.json``
    and its path passed to ``claude --settings``.
    """
    return {"autoMemoryEnabled": False}


def build_cli_flags(
    settings_path: Path,
    max_turns: int,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
) -> list[str]:
    """Return the §11.1 headless flag list for ``claude -p``.

    The caller prepends the binary and ``-p <prompt>``; this returns every flag
    after that. ``--verbose`` is hard-required for ``stream-json`` + ``-p`` on
    claude v2.1.207+ (§11.1 footnote - the prototype's one retry was adding
    it). ``--model`` is deliberately absent: the streaming model id varies
    across backends (z.ai reports ``gpt-5.5``, ark reports ``glm-5.2``), so the
    profile-declared model is injected via ``ANTHROPIC_MODEL`` env instead of a
    flag (ticket 03 inline decision).
    """
    return [
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--permission-mode", permission_mode,
        "--max-turns", str(max_turns),
        "--settings", str(settings_path),
    ]


def build_prompt(run_id: str, run_dir: Path) -> str:
    """Build the self-contained ``claude -p`` prompt for a prepared run.

    The prompt is RUN-relative: it points the agent at the prepared §12.2 input
    package (role / system / task-package / output-schema / allowed-files) by
    path, states the mandatory §13.1 outputs (``result.json`` + ``result.md``),
    and summarises the hard constraints so the §14.2 boundary is unambiguous
    before validation runs. Generic by design - the task itself lives in
    ``input/task-package.md``, which ``prepare_run`` wrote.
    """
    return (
        f"You are executing Agent Run {run_id}.\n"
        f"\n"
        f"Your working directory is: {run_dir}\n"
        f"All paths below are RELATIVE to that working directory.\n"
        f"\n"
        f"STEP 1 - Read the input package:\n"
        f"- input/role.md\n"
        f"- input/system.md\n"
        f"- input/task-package.md\n"
        f"- input/output-schema.json\n"
        f"- input/allowed-files.txt\n"
        f"- input/context/ (run-context.md and any other context files)\n"
        f"\n"
        f"STEP 2 - Execute the task described in input/task-package.md.\n"
        f"\n"
        f"STEP 3 - Write output/result.md: a short human-readable summary of "
        f"what you did.\n"
        f"\n"
        f"STEP 4 - Write output/result.json conforming to "
        f"input/output-schema.json. This is the mandatory final step.\n"
        f"\n"
        f"HARD CONSTRAINTS (from input/system.md):\n"
        f"- You may ONLY create or modify files listed in "
        f"input/allowed-files.txt. Anything else is a file-boundary violation "
        f"(spec §14.2).\n"
        f"- output/result.json MUST be written and MUST conform to "
        f"input/output-schema.json (§13.1).\n"
        f"- Do NOT modify any frozen artifact (§4.2).\n"
        f"- Do NOT write canonical status, close issues, or override gates "
        f"(§4.3).\n"
        f"- Do NOT run git commands.\n"
        f"- Keep it minimal. Stop as soon as output/result.json is written.\n"
    )


# ---------------------------------------------------------------------------
# §11.1/§13.2 orchestration: run_headless.
# ---------------------------------------------------------------------------

# §12.1 run-directory subdirs. ``prepare_run`` (ticket 02) creates these; the
# wrapper re-ensures them so it is robust to a run directory prepared by hand.
# The dir names live in ``paths`` (shared with ``run_prepare``); the §13.2
# metadata filename lives there too (shared with ``validate``).
_STDOUT_LOG = "stdout.log"
_STDERR_LOG = "stderr.log"
_ENV_SNAPSHOT = "env-snapshot.txt"
_RUN_SETTINGS = ".run-settings.json"

# Default ``--max-turns`` - the prototype's bounded value (the hello.py task
# needed ~6 tool calls; 12 leaves headroom). Caller-overridable.
DEFAULT_MAX_TURNS = 12

# The §11.1 binary name resolved when the caller does not pass ``claude_path``.
_CLAUDE_BIN = "claude"


@dataclass(frozen=True)
class RunResult:
    """The wrapper's return: the captured facts about a completed run.

    Carries the claude ``exit_code`` and the computed ``changed_files`` plus the
    paths to the captured ``stdout``/``stderr``/``metadata`` artifacts, so a
    caller (the CLI, ticket 05's integration, ticket 04's validator) can locate
    every output without re-spelling the run layout. No token field - the value
    never leaves the subprocess env (§10.2).
    """

    run_id: str
    feature_id: str
    profile: str
    exit_code: int
    changed_files: list[str]
    started_at: str
    ended_at: str
    stdout_path: Path
    stderr_path: Path
    metadata_path: Path


def _resolve_claude(claude_path: str | None) -> str:
    """Return the ``claude`` binary path, resolving from ``PATH`` if absent.

    The caller may pass an explicit path (tests pass a fake binary); otherwise
    the real CLI is located via ``shutil.which``. Fails loud (§24.2) when the
    binary is not on ``PATH`` - a headless run cannot proceed without it.
    """
    if claude_path is not None:
        return claude_path
    resolved = shutil.which(_CLAUDE_BIN)
    if resolved is None:
        raise ValueError(
            f"claude CLI not found on PATH (set --claude-path or install claude)"
        )
    return resolved


def _copy_agent_outputs(agent_cwd: Path, output_dir: Path) -> None:
    """Copy the agent's ``output/result.{json,md}`` from ``agent_cwd`` to ``output_dir``.

    v0.7 (ADR-0009 D2): when the agent ran in a non-run-home cwd (the lane
    worktree), the §13.1 outputs land in the worktree's ``output/`` (the
    agent writes to a relative path). This helper copies them back to the
    run-home's ``output/`` so the §13.1 contract lives in the canonical
    location the §14 validator reads from. The copy is best-effort: a
    missing file is silently skipped (the agent did not write one yet, or
    the §14.1 validator will surface it; this helper is not a validator).
    Files outside ``output/`` (e.g. ``workspace/...``) are not touched -
    only the two §13.1 mandatory outputs are collected.
    """
    for filename in (RESULT_JSON, RESULT_MD):
        src = agent_cwd / OUTPUT_DIR / filename
        if not src.is_file():
            continue
        dst = output_dir / filename
        shutil.copyfile(src, dst)


# Build-tool cache dirs the agent may emit into ``workspace/`` while
# self-verifying (pytest/mypy/ruff). These are non-deterministic build
# artifacts, never authored source - excluding them from the run-home ->
# worktree sync keeps ``changed_files`` (and the §14.2 boundary check) to
# authored files only. ``__pycache__`` is also subtracted downstream by
# ``_BUILD_ARTIFACT_RE``, but the sync excludes the whole cache dir tree so
# the worktree (and the lane branch) never carries them either.
_WORKSPACE_CACHE_DIRS: frozenset[str] = frozenset(
    {"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
)


def _sync_run_workspace_to_worktree(
    run_workspace: Path, worktree_workspace: Path
) -> None:
    """Copy the run-home ``workspace/`` deliverables into the lane worktree.

    v0.7 capstone (ADR-0009 D2): the real claude CLI agent resolves the
    working directory stated in ``build_prompt`` (the run-home) and writes
    its ``workspace/`` deliverables there with absolute paths, NOT to the
    lane worktree (its actual process cwd) - the fake-claude test writes
    relative-to-cwd so it hits the worktree directly, but the real agent
    does not. This sync copies any files the agent wrote to the run-home
    ``workspace/`` into the worktree ``workspace/`` BEFORE the after-snapshot
    so that:

    * ``changed_files`` (the §13.2 snapshot diff taken against the worktree
      cwd) reports the deliverables - the §14.2 file-boundary check then
      sees the real authored files (not a vacuous empty set);
    * the lane branch can commit them (in ``run_in_lane_worktree``) for PR
      projection;
    * the verifier's worktree-cwd commands find the package + ``tests/``.

    Build-tool cache dirs (``__pycache__`` / ``.mypy_cache`` /
    ``.pytest_cache`` / ``.ruff_cache``) are skipped. A no-op when the agent
    wrote no ``workspace/`` files (e.g. reviewer / spec-gap / a failed run);
    the commit decision is made independently by ``commit_lane_deliverables``
    via ``git diff --cached --quiet``, so this function does not return a
    file list. Idempotent: re-running overwrites in place, so a re-run that
    wrote identical files stages nothing new.
    """
    if not run_workspace.is_dir():
        return
    for src in run_workspace.rglob("*"):
        if not src.is_file():
            continue
        # Skip cache dirs anywhere under workspace/.
        if any(part in _WORKSPACE_CACHE_DIRS for part in src.relative_to(run_workspace).parts):
            continue
        rel = src.relative_to(run_workspace)
        dst = worktree_workspace / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


# ---------------------------------------------------------------------------
# ADR-0005: AgentRunner strategy per ``profile.cli`` (D1). Each adapter owns
# child-env build (strip + inject), argv build, stdout/stderr/exit capture, and
# the wrapper-owned subtract set for ``changed_files``. ``run_headless`` is the
# thin dispatcher: it resolves the adapter from ``profile.cli`` (D2) and
# delegates the adapter-specific steps, keeping the shared orchestration
# (run-dir precondition, token-source resolution, snapshot diff, metadata,
# audit) in one place.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Invocation:
    """An adapter-built CLI invocation: argv + optional stdin prompt.

    ``stdin`` is ``None`` when the prompt rides in argv (claude ``-p <prompt>``);
    a string when the prompt is piped on stdin (codex ``exec -``). The
    dispatcher passes ``input=stdin`` to ``subprocess.run`` only when stdin is
    set, so the claude path (no stdin) inherits the parent stdin exactly as
    before - only the codex path pipes the prompt.
    """

    argv: list[str]
    stdin: str | None = None


class AgentRunner(abc.ABC):
    """Per-CLI invocation adapter (ADR-0005 D1).

    Subclasses are registered under their ``cli`` key and resolved by
    ``get_runner`` from ``profile.cli``. The adapter owns the invocation
    contract (binary, flags, prompt mode, sandbox) and the env-injection shape;
    ``backend`` / ``auth_env`` / ``model`` are profile fields the adapter maps
    onto env/argv uniformly (D2 - dispatch is on ``cli``, not ``backend``).
    """

    # The registry key (``profile.cli``); subclasses set this to ``"claude"`` /
    # ``"codex"``. The registry is keyed by it (D2).
    cli: str
    # Whether a resolved env token is mandatory before spawning. claude has no
    # non-env auth path, so it fails loud (§24.2) when the token source is unset;
    # codex may fall back to stored ``~/.codex/auth.json`` (D3 amended), so its
    # token is optional.
    token_required: bool
    # Regex subtracting this adapter's wrapper-owned output artifacts from
    # ``changed_files`` (stdout/stderr/metadata/env-snapshot, plus the claude
    # settings file).
    wrapper_owned_re: re.Pattern[str]

    @abc.abstractmethod
    def compose_child_env(
        self,
        profile: AgentProfile,
        parent_env: Mapping[str, str],
        token_source: str | None,
        token_value: str,
    ) -> dict[str, str]:
        """Build the child env: strip parent identity, inject profile + token."""

    @abc.abstractmethod
    def prepare_prerun(
        self,
        *,
        output_dir: Path,
        child_env: Mapping[str, str],
        profile: AgentProfile,
        token_source: str | None,
        started: str,
    ) -> None:
        """Write adapter-specific pre-run artifacts before the before-snapshot.

        Both adapters write the redacted env snapshot; claude additionally
        writes the §14.2 auto-memory-off ``.run-settings.json``. These land
        before ``snapshot_tree`` so they are present (and unchanged) in the
        before-snapshot and subtracted by ``wrapper_owned_re``.
        """

    @abc.abstractmethod
    def resolve_binary(self, override: str | None) -> str:
        """Resolve the CLI binary (override or ``PATH``); fail loud if missing."""

    @abc.abstractmethod
    def build_invocation(
        self,
        *,
        profile: AgentProfile,
        output_dir: Path,
        binary: str,
        max_turns: int,
        permission_mode: str,
        prompt: str,
    ) -> Invocation:
        """Build the argv (+ optional stdin prompt) for the CLI."""


class ClaudeRunner(AgentRunner):
    """The claude CLI adapter - the v0.0-v0.4 behavior, extracted (D1).

    Behavior-identical to the pre-dispatch ``run_wrapper``: the same
    ``ANTHROPIC_*`` env injection, the same §11.1 flag set (incl ``--verbose``
    and ``--settings``), prompt as a ``-p`` arg. Delegates to the module-level
    helpers (``build_child_env`` / ``render_env_snapshot`` /
    ``auto_memory_settings`` / ``build_cli_flags`` / ``_resolve_claude``) so the
    existing claude tests exercise the exact same code path.
    """

    cli = "claude"
    token_required = True
    wrapper_owned_re = WRAPPER_OWNED_RE

    def compose_child_env(
        self,
        profile: AgentProfile,
        parent_env: Mapping[str, str],
        token_source: str | None,
        token_value: str,
    ) -> dict[str, str]:
        # ``token_source`` is guaranteed non-None here (``token_required`` made
        # the dispatcher fail loud before reaching this); the shared
        # ``build_child_env`` only needs the value, so ``token_source`` is unused
        # on this path - kept in the signature for interface uniformity.
        return build_child_env(profile, parent_env, token_value)

    def prepare_prerun(
        self,
        *,
        output_dir: Path,
        child_env: Mapping[str, str],
        profile: AgentProfile,
        token_source: str | None,
        started: str,
    ) -> None:
        env_snapshot_path = output_dir / _ENV_SNAPSHOT
        env_snapshot_path.write_text(
            render_env_snapshot(child_env, profile, token_source, started)
        )
        settings_path = output_dir / _RUN_SETTINGS
        settings_path.write_text(json.dumps(auto_memory_settings(), indent=2) + "\n")

    def resolve_binary(self, override: str | None) -> str:
        return _resolve_claude(override)

    def build_invocation(
        self,
        *,
        profile: AgentProfile,
        output_dir: Path,
        binary: str,
        max_turns: int,
        permission_mode: str,
        prompt: str,
    ) -> Invocation:
        # claude flags are profile-agnostic (model via ANTHROPIC_MODEL env, not a
        # flag - ticket 03 inline decision); the settings path is the only
        # output-dir-derived arg. ``profile`` is unused on this path.
        settings_path = output_dir / _RUN_SETTINGS
        flags = build_cli_flags(settings_path, max_turns, permission_mode)
        return Invocation(argv=[binary, "-p", prompt, *flags], stdin=None)


# The codex CLI binary name resolved when the caller passes no override.
_CODEX_BIN = "codex"

# ADR-0005 D4 (spike-amended): the engine sandbox confines writes to cwd (the
# run dir); ``workspace-write`` = ``[workdir, /tmp, $TMPDIR]``. §14.2 stays the
# fine-grained allowlist on top - defense in depth.
_CODEX_SANDBOX = "workspace-write"


def _resolve_codex(codex_path: str | None) -> str:
    """Return the ``codex`` binary path, resolving from ``PATH`` if absent.

    Mirrors ``_resolve_claude``: an explicit override (tests pass a fake binary)
    wins; otherwise the real CLI is located via ``shutil.which``. Fails loud
    (§24.2) when the binary is not on ``PATH`` - a headless codex run cannot
    proceed without it.
    """
    if codex_path is not None:
        return codex_path
    resolved = shutil.which(_CODEX_BIN)
    if resolved is None:
        raise ValueError(
            f"codex CLI not found on PATH (pass a binary path override or "
            f"install codex)"
        )
    return resolved


class CodexRunner(AgentRunner):
    """The codex CLI adapter (ADR-0005 D2/D3/D4/D5).

    Thin adapter (D5): it only invokes + captures; it never translates codex's
    native diff/patch into ``result.json``. The role prompts (reused verbatim
    via ``build_prompt``) instruct codex to write the §13 contract at the
    declared paths, exactly as claude does - the spike (ticket 01) verified
    codex honors that prompt-written contract.

    Auth (D3 amended): ``--remote-auth-token-env`` is rejected by ``codex exec``
    (TUI-only), so it is NOT in the argv. For the OpenAI provider the token is
    injected onto ``profile.auth_env`` (``OPENAI_API_KEY``) - the same
    env-injection pattern claude uses for ``ANTHROPIC_AUTH_TOKEN``; for custom
    providers codex uses stored ``~/.codex/auth.json`` (no env token, no
    fail-loud - ``token_required`` is False). Either way invariant #11 holds: no
    token value sits in profile config.

    Sandbox (D4 amended): ``-s workspace-write`` with ``cwd`` = the run dir (set
    by the dispatcher), so RUN-level ``output/result.json`` is writable and
    writes are confined to the run dir. ``--ephemeral`` suppresses ``~/.codex/``
    session persistence (the codex analogue of claude's
    ``--settings autoMemoryEnabled=false``, §14.2).
    """

    cli = "codex"
    token_required = False
    wrapper_owned_re = CODEX_WRAPPER_OWNED_RE

    def compose_child_env(
        self,
        profile: AgentProfile,
        parent_env: Mapping[str, str],
        token_source: str | None,
        token_value: str,
    ) -> dict[str, str]:
        # Strip the shared §10.3 hygiene baseline (claude identity vars + the
        # profile's env_strip_pattern); harmless for codex in a pure-codex env
        # and correct hygiene when the orchestrator nests inside a claude
        # session. ``extra_env`` is applied before the token so a resolved token
        # always wins.
        env = strip_parent_identity(parent_env, profile)
        for key, value in profile.extra_env.items():
            env[key] = value
        # OpenAI-provider path: inject the token onto ``profile.auth_env``
        # (e.g. OPENAI_API_KEY), mirroring claude's ANTHROPIC_AUTH_TOKEN
        # injection. Custom-provider path: ``token_source`` is None -> no
        # injection; codex resolves auth from ~/.codex/auth.json.
        if token_source is not None and token_value:
            env[profile.auth_env] = token_value
        return env

    def prepare_prerun(
        self,
        *,
        output_dir: Path,
        child_env: Mapping[str, str],
        profile: AgentProfile,
        token_source: str | None,
        started: str,
    ) -> None:
        env_snapshot_path = output_dir / _ENV_SNAPSHOT
        env_snapshot_path.write_text(
            render_codex_env_snapshot(child_env, profile, token_source, started)
        )
        # No settings file: codex has no --settings analogue; --ephemeral (in
        # argv) is the session-persistence hygiene flag.

    def resolve_binary(self, override: str | None) -> str:
        return _resolve_codex(override)

    def build_invocation(
        self,
        *,
        profile: AgentProfile,
        output_dir: Path,
        binary: str,
        max_turns: int,
        permission_mode: str,
        prompt: str,
    ) -> Invocation:
        # ``max_turns`` / ``permission_mode`` are claude flags; codex has no argv
        # equivalent in the MVP (the spike ran without them). ``output_dir`` is
        # unused too - codex writes no settings file. All three are kept in the
        # signature for interface uniformity.
        # ADR-0005 D2/D4 + spike amendments: ``codex exec -`` (prompt on stdin),
        # ``-s workspace-write`` (engine sandbox; cwd=run_dir confines writes),
        # ``--skip-git-repo-check`` + ``--color never`` (clean capture),
        # ``--ephemeral`` (no ~/.codex/ session persistence). ``-m`` only when
        # the profile declares a model. cwd is set by the dispatcher to run_dir.
        argv: list[str] = [
            binary,
            "exec",
            "-",
            "-s",
            _CODEX_SANDBOX,
            "--skip-git-repo-check",
            "--color",
            "never",
            "--ephemeral",
        ]
        if profile.model is not None:
            argv += ["-m", profile.model]
        return Invocation(argv=argv, stdin=prompt)


# The registry keyed by ``profile.cli`` (D1/D2). A 3rd+ profile (§27.3) becomes
# one ``AgentRunner`` impl + one entry here - no ``run_headless`` surgery.
_RUNNERS: dict[str, AgentRunner] = {
    ClaudeRunner.cli: ClaudeRunner(),
    CodexRunner.cli: CodexRunner(),
}


def get_runner(profile: AgentProfile) -> AgentRunner:
    """Resolve the ``AgentRunner`` for ``profile.cli`` (ADR-0005 D1/D2).

    Dispatch is on ``cli`` (the invocation contract), not ``backend``: a
    ``cli: claude`` profile with any backend (glm / minimax / deepseek) resolves
    ``ClaudeRunner``; a ``cli: codex`` profile resolves ``CodexRunner``. Fails
    loud (§24.2) on an unknown ``cli`` so a misnamed profile surfaces at run
    time, not as a silent claude fallback.
    """
    runner = _RUNNERS.get(profile.cli)
    if runner is None:
        raise ValueError(
            f"no AgentRunner registered for cli={profile.cli!r} "
            f"(profile {profile.name!r}); registered: {sorted(_RUNNERS)}"
        )
    return runner


def run_headless(
    repo_root: Path,
    feature_id: str,
    run_id: str,
    profile: AgentProfile,
    *,
    max_turns: int = DEFAULT_MAX_TURNS,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    cli_path: str | None = None,
    claude_path: str | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
    origin: str | None = None,
    cwd: Path | None = None,
    lane_context: Mapping[str, Any] | None = None,
) -> RunResult:
    """Run a prepared ``RUN-NNN`` headless against ``profile`` and capture it.

    Thin dispatcher (ADR-0005 D1): resolves the ``AgentRunner`` for
    ``profile.cli`` and delegates the adapter-specific steps (child-env build,
    pre-run artifacts, argv, capture), keeping the shared orchestration - run-dir
    precondition, token-source resolution, before/after snapshot diff,
    ``metadata.json``, and the ``run`` audit event - in one place.

    Orchestrates the §10.3 -> §11 -> §13.2 flow: resolve the token source (fail
    loud if unset for a token-required adapter, §24.2), build the isolated child
    env via the adapter, persist the redacted env snapshot (+, for claude, the
    §14.2 auto-memory-off settings), snapshot the RUN tree, invoke the CLI (cwd =
    run dir, env = child env, stdout/stderr captured to files; prompt in argv for
    claude, on stdin for codex), snapshot again, compute ``changed_files``
    (subtracting the adapter's wrapper-owned artifacts), write ``metadata.json``,
    and append a ``run`` audit event. Returns the captured facts as a
    ``RunResult``.

    ``cli_path`` is the generic CLI binary override (preferred for new callers);
    ``claude_path`` is kept as a backward-compatible alias (existing callers and
    tests pass it). Whichever is set wins; the adapter resolves it onto its own
    binary (``claude`` / ``codex``).

    v0.7 (ADR-0009 D2) affordances:

    * ``cwd`` overrides the agent's working directory - normally the run-home
      (the default and the v0.1-v0.6 behavior), now optionally the lane's
      git worktree (``run_in_lane_worktree`` passes the worktree path). The
      pre-run artifacts (``env-snapshot.txt`` / ``.run-settings.json``) and
      ``stdout.log``/``stderr.log`` still live under the run-home's ``output/``
      (so a single grep across the run-home finds every wrapper artifact);
      ``changed_files`` is computed from the *cwd* (so an implement run on a
      lane worktree correctly reports the files the agent wrote in the
      worktree, not files copied into the run-home). When ``cwd`` differs from
      the run-home, the agent's ``output/result.{json,md}`` are copied from
      the cwd to the run-home after the run, so the §13.1 contract and the
      §14 validator read from the canonical run-home.
    * ``lane_context`` is the v0.7 lane identity dict (``lane_id`` /
      ``worktree_path`` / ``branch`` / ``base_ref`` / ``commands``) - the
      wrapper stamps the fields on the run-home ``metadata.json`` so the
      run-home and the lane-level ``metadata.json`` agree. ``commands`` is the
      per-leg command list (empty for the implementer / fix legs, populated by
      the shell verifier's per-command results - the ticket names the field
      for the metadata shape).

    The token *value* is read from ``os.environ`` by source name and lives only
    in the in-memory child env passed to the subprocess - it is on no returned
    object and written to no file (the env snapshot redacts it). Timestamps
    default to ``utc_now_iso()`` captured around the subprocess; both are
    injectable for deterministic tests.
    """
    runner = get_runner(profile)

    # Resolve to absolute up front: the CLI is spawned with ``cwd`` = this run
    # dir and (claude) re-resolves its ``--settings`` argument relative to that
    # cwd, so a relative ``repo_root`` (ticket-05's ``cd examples/string-utils``
    # + relative ``--repo-root``) makes the lookup fail with "Settings file not
    # found". Absolute here covers ``cwd`` + ``--settings`` + the prompt's
    # working-directory string at once; RUN-relative ``changed_files`` are
    # unaffected (``relpath`` over an absolute base yields the same keys).
    run_root = run_dir(repo_root, feature_id, run_id).resolve()
    if not run_root.is_dir():
        raise ValueError(
            f"run directory {run_id} not found under feature {feature_id} "
            f"(prepare it with `ai-dev prepare-run` first)"
        )
    output_dir = run_root / OUTPUT_DIR
    workspace_dir = run_root / WORKSPACE_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    # v0.7 (ADR-0009 D2): when the caller passes a ``cwd`` (e.g. the lane
    # worktree), the agent runs in that directory. The pre-run artifacts
    # (env snapshot, settings file) and ``stdout.log``/``stderr.log`` are
    # still written to the run-home's ``output/`` (so the wrapper's artifacts
    # stay in one canonical place); the agent's ``output/result.{json,md}``
    # are written to the *cwd*'s ``output/`` and copied back to the run-home
    # after the run (the §13.1 contract and §14 validator read from the
    # run-home). The ``changed_files`` snapshot diff is taken against the
    # *cwd* so it reports what the agent actually wrote in the worktree,
    # not the run-home's mirror.
    agent_cwd = cwd.resolve() if cwd is not None else run_root
    if agent_cwd != run_root and not agent_cwd.is_dir():
        raise ValueError(
            f"lane_run cwd {agent_cwd} is not a directory; cannot run "
            f"agent there (lane_run D2)"
        )

    # §10.2/§10.3: resolve the token source NAME, then read the value from the
    # live env. A token-required adapter (claude) fails loud before any subprocess
    # is spawned; codex (token_required=False) may proceed without one (stored
    # ~/.codex/auth.json, D3 amended).
    token_source = token_source_var(profile)
    token_value = os.environ.get(token_source, "") if token_source is not None else ""
    if runner.token_required:
        if token_source is None:
            raise ValueError(
                f"token source not set for profile {profile.name!r} "
                f"({profile.token_source_description()} is unset); set it before "
                f"running (§24.2)"
            )
        # ``.get`` + re-check closes the window between ``token_source_var`` and
        # the read: a var deleted in the race surfaces as a clean ValueError
        # (§24.2), not a KeyError traceback. An empty value is no credential
        # (matches ``token_source_var``'s non-empty test).
        if not token_value:
            raise ValueError(
                f"token source {token_source!r} for profile {profile.name!r} "
                f"is not set; set it before running (§24.2)"
            )

    child_env = runner.compose_child_env(
        profile, dict(os.environ), token_source, token_value
    )

    started = started_at if started_at is not None else utc_now_iso()

    # Adapter-specific pre-run artifacts (env snapshot +, for claude, the §14.2
    # settings file) are written BEFORE the before-snapshot so they are present
    # (unchanged) in it and subtracted from ``changed_files``.
    runner.prepare_prerun(
        output_dir=output_dir,
        child_env=child_env,
        profile=profile,
        token_source=token_source,
        started=started,
    )

    before = snapshot_tree(agent_cwd)

    prompt = build_prompt(run_id, run_root)
    binary = runner.resolve_binary(cli_path if cli_path is not None else claude_path)
    invocation = runner.build_invocation(
        profile=profile,
        output_dir=output_dir,
        binary=binary,
        max_turns=max_turns,
        permission_mode=permission_mode,
        prompt=prompt,
    )

    stdout_path = output_dir / _STDOUT_LOG
    stderr_path = output_dir / _STDERR_LOG
    # The two branches return ``CompletedProcess[str]`` (codex, ``text=True``)
    # vs ``CompletedProcess[bytes]`` (claude, no ``text``); only ``.returncode``
    # (non-generic) is read, so the union is precise without a cast.
    completed: subprocess.CompletedProcess[bytes] | subprocess.CompletedProcess[str]
    with stdout_path.open("w") as out_f, stderr_path.open("w") as err_f:
        if invocation.stdin is not None:
            # codex: prompt piped on stdin (``codex exec -``).
            completed = subprocess.run(
                invocation.argv,
                cwd=str(agent_cwd),
                env=child_env,
                stdout=out_f,
                stderr=err_f,
                input=invocation.stdin,
                text=True,
            )
        else:
            # claude: prompt rides in argv (``-p <prompt>``); stdin is inherited
            # exactly as before the dispatch refactor.
            completed = subprocess.run(
                invocation.argv,
                cwd=str(agent_cwd),
                env=child_env,
                stdout=out_f,
                stderr=err_f,
            )
    exit_code = completed.returncode

    ended = ended_at if ended_at is not None else utc_now_iso()

    # v0.7 (ADR-0009 D2): when the agent ran in a non-run-home cwd (the lane
    # worktree), the agent's ``output/result.{json,md}`` are written there
    # (the agent writes to a relative ``output/`` path). Copy them back to
    # the run-home so the §13.1 contract lives in the canonical location
    # the §14 validator reads from. The run-home's own ``output/`` was
    # pre-populated with the wrapper's env-snapshot + settings + the
    # ``stdout.log``/``stderr.log``; the copy is additive - it adds the
    # agent's two §13.1 outputs without disturbing the wrapper's artifacts.
    if agent_cwd != run_root:
        _copy_agent_outputs(agent_cwd, output_dir)
        # v0.7 capstone: the real claude CLI agent writes its workspace/
        # deliverables to the run-home (build_prompt states the run-home as
        # the working directory), not to the worktree cwd. Sync them into
        # the worktree BEFORE the after-snapshot so changed_files reports
        # them (the §14.2 boundary check then sees the real files) and the
        # lane branch can commit them downstream. No-op when the agent wrote
        # no workspace files (reviewer / spec-gap / a failed run) or when
        # the agent already wrote to the worktree (fake-claude tests).
        _sync_run_workspace_to_worktree(workspace_dir, agent_cwd / WORKSPACE_DIR)

    after = snapshot_tree(agent_cwd)
    changed_files = compute_changed_files(before, after, runner.wrapper_owned_re)

    metadata_path = output_dir / METADATA_JSON
    # v0.7 lane identity: stamp the run-home ``metadata.json`` with the lane
    # fields so the run-home and the lane-level ``metadata.json`` agree. The
    # keys are present only when ``lane_context`` was passed; a v0.1-v0.6
    # caller (no lane context) gets the v0.1 shape back.
    lane_kwargs: dict[str, Any] = {}
    if lane_context is not None:
        if "lane_id" in lane_context:
            lane_kwargs["lane_id"] = str(lane_context["lane_id"])
        if "worktree_path" in lane_context:
            lane_kwargs["worktree_path"] = str(lane_context["worktree_path"])
        if "branch" in lane_context:
            lane_kwargs["branch"] = str(lane_context["branch"])
        if "base_ref" in lane_context:
            lane_kwargs["base_ref"] = str(lane_context["base_ref"])
        if "commands" in lane_context:
            raw_cmds = lane_context["commands"]
            lane_kwargs["commands"] = list(raw_cmds) if raw_cmds else []
    write_metadata(
        metadata_path,
        run_id=run_id,
        profile=profile,
        started_at=started,
        ended_at=ended,
        exit_code=exit_code,
        changed_files=changed_files,
        **lane_kwargs,
    )

    # §2.1: the run lifecycle flows through the audit log. Carries no token -
    # only the run id, profile name, exit code, changed files and the wall-clock
    # duration (v0.4 ticket 02: ``elapsed_ms`` answers "how long did this leg
    # run" without reading metadata.json).
    feature_root = feature_dir(repo_root, feature_id)
    append_audit_event(
        feature_root,
        event="run",
        payload={
            "run": run_id,
            "feature": feature_id,
            "profile": profile.name,
            "exit_code": exit_code,
            "changed_files": changed_files,
            "elapsed_ms": elapsed_ms_between(started, ended),
        },
        origin=origin,
    )

    return RunResult(
        run_id=run_id,
        feature_id=feature_id,
        profile=profile.name,
        exit_code=exit_code,
        changed_files=changed_files,
        started_at=started,
        ended_at=ended,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        metadata_path=metadata_path,
    )
