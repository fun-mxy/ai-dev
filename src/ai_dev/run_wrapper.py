"""Claude Code headless wrapper - env isolation, invocation, capture (ticket 03).

The wrapper is the v0.1 run-adapter's execution seam: given a prepared
``RUN-NNN`` directory (ticket 02's ``prepare_run``) and a parsed ``AgentProfile``
(ticket 01's ``load_profile``), it builds a self-contained prompt, isolates the
child environment (§10.3), invokes ``claude -p`` headless with the §11.1 hard
flags, captures stdout/stderr, computes ``changed_files`` (§13.2/§14.2), and
writes ``metadata.json`` (§13.2). It is the deterministic Python runtime
standing between ``prepare_run`` and ticket 04's ``validate-run``.

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

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ai_dev.audit import append_audit_event
from ai_dev.paths import (
    METADATA_JSON,
    OUTPUT_DIR,
    WORKSPACE_DIR,
    feature_dir,
    run_dir,
)
from ai_dev.profiles import AgentProfile, token_source_var
from ai_dev.timeutil import utc_now_iso

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
    any extra entry would betray a strip miss. The header records the profile
    name, ``base_url``, ``model`` and the token *source* name (never the value),
    matching the prototype's ``env-snapshot.txt`` shape.
    """
    stamp = timestamp if timestamp is not None else utc_now_iso()
    header = (
        f"# Child claude env snapshot (names only; values redacted) - {stamp}\n"
        f"# profile={profile.name} base_url={profile.base_url} "
        f"model={profile.model} token_src={token_source}"
    )
    # case-insensitive match against claude|anthropic|^AI_, like the prototype's
    # ``env | grep -iE 'claude|anthropic|^AI_'``.
    pattern = re.compile(r"claude|anthropic|^AI_", re.IGNORECASE)
    rows = sorted(k for k in child_env if pattern.search(k))
    body = "\n".join(f"{k}={_REDACTED}" for k in rows)
    return f"{header}\n{body}\n" if body else f"{header}\n"


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
    list. Deletions (in ``before``, gone in ``after``) are not reported - the
    list captures what the agent wrote, not what it removed. Sorted for
    diff-stable output matching the prototype.
    """
    changed = [
        path
        for path, meta in after.items()
        if not wrapper_owned.search(path) and before.get(path) != meta
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
) -> None:
    """Write the §13.2 ``metadata.json`` with the wrapper-computed fact set.

    Every field from the spec's example is present and sourced from the profile
    (``profile`` / ``cli`` / ``backend`` / ``model``) and the run facts
    (``run_id`` / ``started_at`` / ``ended_at`` / ``exit_code`` /
    ``changed_files``). ``commits`` and ``checks`` are empty in v0.1 - commit
    capture and the §14 verification commands are later tickets' concerns - but
    the fields are present so the schema is stable from day one.

    No token field is ever written: the profile carries only variable *names*
    (§10.2) and the token value never reaches this function.
    """
    md: dict[str, object] = {
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


def run_headless(
    repo_root: Path,
    feature_id: str,
    run_id: str,
    profile: AgentProfile,
    *,
    max_turns: int = DEFAULT_MAX_TURNS,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    claude_path: str | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
) -> RunResult:
    """Run a prepared ``RUN-NNN`` headless against ``profile`` and capture it.

    Orchestrates the §10.3 -> §11.1 -> §13.2 flow: resolve the token source
    (fail loud if unset, §24.2), build the isolated child env, persist the
    redacted env snapshot + the §14.2 auto-memory-off settings, snapshot the
    RUN tree, invoke ``claude -p`` with the §11.1 flags (cwd = run dir, env =
    child env, stdout/stderr captured to files), snapshot again, compute
    ``changed_files`` (subtracting wrapper-owned artifacts), write
    ``metadata.json``, and append a ``run`` audit event. Returns the captured
    facts as a ``RunResult``.

    The token *value* is read from ``os.environ`` by source name and lives only
    in the in-memory child env passed to the subprocess - it is on no returned
    object and written to no file (the env snapshot redacts it). Timestamps
    default to ``utc_now_iso()`` captured around the subprocess; both are
    injectable for deterministic tests.
    """
    run_root = run_dir(repo_root, feature_id, run_id)
    if not run_root.is_dir():
        raise ValueError(
            f"run directory {run_id} not found under feature {feature_id} "
            f"(prepare it with `ai-dev prepare-run` first)"
        )
    output_dir = run_root / OUTPUT_DIR
    workspace_dir = run_root / WORKSPACE_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    # §10.2/§10.3: resolve the token source NAME, then read the value from the
    # live env. No source -> fail loud before any subprocess is spawned.
    token_source = token_source_var(profile)
    if token_source is None:
        raise ValueError(
            f"token source not set for profile {profile.name!r} "
            f"({profile.token_source_description()} is unset); set it before "
            f"running (§24.2)"
        )
    # ``.get`` + re-check closes the window between ``token_source_var`` and the
    # read: a var deleted in the race surfaces as a clean ValueError (§24.2),
    # not a KeyError traceback. An empty value is no credential (matches
    # ``token_source_var``'s non-empty test).
    token_value = os.environ.get(token_source, "")
    if not token_value:
        raise ValueError(
            f"token source {token_source!r} for profile {profile.name!r} "
            f"is not set; set it before running (§24.2)"
        )

    child_env = build_child_env(profile, dict(os.environ), token_value)

    started = started_at if started_at is not None else utc_now_iso()

    # Persist the §10.3 env snapshot (redacted) and the §14.2 settings file
    # BEFORE the before-snapshot so both are present (unchanged) in it.
    env_snapshot_path = output_dir / _ENV_SNAPSHOT
    env_snapshot_path.write_text(
        render_env_snapshot(child_env, profile, token_source, started)
    )
    settings_path = output_dir / _RUN_SETTINGS
    settings_path.write_text(json.dumps(auto_memory_settings(), indent=2) + "\n")

    before = snapshot_tree(run_root)

    prompt = build_prompt(run_id, run_root)
    flags = build_cli_flags(settings_path, max_turns, permission_mode)
    argv = [_resolve_claude(claude_path), "-p", prompt, *flags]

    stdout_path = output_dir / _STDOUT_LOG
    stderr_path = output_dir / _STDERR_LOG
    with stdout_path.open("w") as out_f, stderr_path.open("w") as err_f:
        completed = subprocess.run(
            argv,
            cwd=str(run_root),
            env=child_env,
            stdout=out_f,
            stderr=err_f,
        )
    exit_code = completed.returncode

    ended = ended_at if ended_at is not None else utc_now_iso()

    after = snapshot_tree(run_root)
    changed_files = compute_changed_files(before, after, WRAPPER_OWNED_RE)

    metadata_path = output_dir / METADATA_JSON
    write_metadata(
        metadata_path,
        run_id=run_id,
        profile=profile,
        started_at=started,
        ended_at=ended,
        exit_code=exit_code,
        changed_files=changed_files,
    )

    # §2.1: the run lifecycle flows through the audit log. Carries no token -
    # only the run id, profile name, exit code and changed files.
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
        },
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
