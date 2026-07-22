"""run_wrapper - Claude Code headless wrapper (ticket 03, spec §10.3/§11/§13/§14).

The wrapper turns a prepared ``RUN-NNN`` directory + a parsed ``AgentProfile``
into a self-contained ``claude -p`` headless invocation: it isolates the child
env (§10.3), builds the prompt, invokes the CLI with the §11.1 hard flags,
captures stdout/stderr, computes ``changed_files`` (§13.2/§14.2), and writes
``metadata.json``. Token values are read from the environment by source NAME
only (§10.2, invariant #11) and never persisted - the env snapshot redacts
every value to ``=<set>`` and ``metadata.json`` carries no token field.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Callable

import pytest

from ai_dev.audit import AUDIT_LOG_JSON
from ai_dev.feature_run import create_feature_run
from ai_dev.paths import run_dir
from ai_dev.profiles import AgentProfile, load_profile
from ai_dev.run_prepare import prepare_run
from ai_dev.run_wrapper import (
    CODEX_WRAPPER_OWNED_RE,
    STRIP_VARS,
    WRAPPER_OWNED_RE,
    ClaudeRunner,
    CodexRunner,
    auto_memory_settings,
    build_child_env,
    build_cli_flags,
    build_prompt,
    compute_changed_files,
    get_runner,
    inject_profile_env,
    render_codex_env_snapshot,
    render_env_snapshot,
    run_headless,
    snapshot_tree,
    strip_parent_identity,
    write_metadata,
)

# A parent env carrying the contamination the wrapper must strip: nested
# Claude-Code identity vars, the model-alias overrides, and the AI_AGENT env.
# Plus a few benign vars that must survive (PATH) and a stale ANTHROPIC_AUTH_TOKEN
# the injection must overwrite with the resolved token.
_CONTAMINATED_PARENT = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/tmp/home",
    "CLAUDE_CODE_SESSION_ID": "parent-session-111",
    "CLAUDE_CODE_CHILD_SESSION": "1",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
    "CLAUDE_CODE_EXECPATH": "/opt/claude",
    "CLAUDECODE": "1",
    "AI_AGENT": "1",
    "CLAUDE_EFFORT": "high",
    "ANTHROPIC_DEFAULT_FABLE_MODEL": "gpt-5.5",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-4-5",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-8",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-5",
    "ANTHROPIC_REASONING_MODEL": "gpt-5.5",
    "IMG_BASE_URL": "https://img.example",
    "ANTHROPIC_AUTH_TOKEN": "STALE-PARENT-TOKEN",
}


class TestStripParentIdentity:
    """§10.3: the explicit strip list + the profile ``env_strip_pattern``.

    Every contamination var the spec names must be removed before injection so
    the child ``claude`` cannot inherit the parent session or fall back to a
    non-GLM model alias (prototype FINDINGS: clean snapshot had exactly three
    ANTHROPIC vars).
    """

    def test_removes_section_10_3_identity_vars(self, repo_root: Path, write_profiles: Callable[..., Path]) -> None:
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")

        stripped = strip_parent_identity(_CONTAMINATED_PARENT, profile)

        for var in (
            "CLAUDE_CODE_SESSION_ID",
            "CLAUDE_CODE_CHILD_SESSION",
            "CLAUDE_CODE_ENTRYPOINT",
            "CLAUDE_CODE_EXECPATH",
            "CLAUDECODE",
            "AI_AGENT",
            "CLAUDE_EFFORT",
        ):
            assert var not in stripped, f"identity var {var} survived strip"

    def test_removes_model_alias_overrides(self, repo_root: Path, write_profiles: Callable[..., Path]) -> None:
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")

        stripped = strip_parent_identity(_CONTAMINATED_PARENT, profile)

        for var in (
            "ANTHROPIC_DEFAULT_FABLE_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_REASONING_MODEL",
            "IMG_BASE_URL",
        ):
            assert var not in stripped, f"alias var {var} survived strip"

    def test_preserves_unrelated_vars(self, repo_root: Path, write_profiles: Callable[..., Path]) -> None:
        # PATH / HOME are not contamination - they must survive so the child
        # process can still locate binaries and a HOME.
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")

        stripped = strip_parent_identity(_CONTAMINATED_PARENT, profile)

        assert stripped["PATH"] == "/usr/bin:/bin"
        assert stripped["HOME"] == "/tmp/home"

    def test_does_not_mutate_input_env(self, repo_root: Path, write_profiles: Callable[..., Path]) -> None:
        # A pure function: the caller's dict is untouched (the orchestrator
        # passes ``dict(os.environ)`` but the helper must not rely on that).
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")

        strip_parent_identity(_CONTAMINATED_PARENT, profile)

        assert "CLAUDECODE" in _CONTAMINATED_PARENT
        assert "ANTHROPIC_DEFAULT_HAIKU_MODEL" in _CONTAMINATED_PARENT

    def test_strip_pattern_removes_matching_vars_beyond_explicit_list(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        # The profile's env_strip_pattern is applied on top of the explicit
        # list; a CLAUDE_CODE_-prefixed var NOT in the explicit list still
        # matches the pattern and must be removed.
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        parent = {"CLAUDE_CODE_SOMETHING_ELSE": "x", "PATH": "/bin"}

        stripped = strip_parent_identity(parent, profile)

        assert "CLAUDE_CODE_SOMETHING_ELSE" not in stripped
        assert stripped["PATH"] == "/bin"

    def test_strip_vars_constant_covers_section_10_3_list(self) -> None:
        # The explicit list is the spec's §10.3 strip set - pin it so a refactor
        # cannot silently drop a contamination var.
        expected = {
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
        }
        assert expected.issubset(set(STRIP_VARS))


class TestInjectProfileEnv:
    """§10.3 injection: base_url / auth_token / model + extra_env.

    Explicit profile fields win over ``extra_env`` (§10.3 says base_url ←
    profile.base_url, model ← profile.model). The token maps onto
    ``auth_target`` (default ``ANTHROPIC_AUTH_TOKEN``) - the var the Claude CLI
    actually reads for 3P Anthropic-compatible backends.
    """

    def test_sets_three_target_vars(self, repo_root: Path, write_profiles: Callable[..., Path]) -> None:
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")

        env = inject_profile_env({"PATH": "/bin"}, profile, "tok-resolved")

        assert env["ANTHROPIC_BASE_URL"] == profile.base_url
        assert env["ANTHROPIC_MODEL"] == profile.model
        assert env["ANTHROPIC_AUTH_TOKEN"] == "tok-resolved"

    def test_overwrites_stale_parent_token(self, repo_root: Path, write_profiles: Callable[..., Path]) -> None:
        # The parent may carry a stale ANTHROPIC_AUTH_TOKEN; injection must
        # replace it with the resolved token (prototype unset-then-export).
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")

        env = inject_profile_env({"ANTHROPIC_AUTH_TOKEN": "STALE"}, profile, "fresh-token")

        assert env["ANTHROPIC_AUTH_TOKEN"] == "fresh-token"

    def test_explicit_base_url_wins_over_extra_env(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        # If profile.base_url and extra_env.ANTHROPIC_BASE_URL disagree, the
        # explicit §10.3 field (base_url) wins.
        write_profiles(
            repo_root,
            "agent_profiles:\n"
            "  conflict:\n"
            "    cli: claude\n"
            "    auth_env: TOK\n"
            "    base_url: 'https://explicit.example'\n"
            "    model: glm-5.2\n"
            "    extra_env:\n"
            "      ANTHROPIC_BASE_URL: 'https://stale.example'\n",
        )
        profile = load_profile(repo_root, "conflict")

        env = inject_profile_env({}, profile, "tok")

        assert env["ANTHROPIC_BASE_URL"] == "https://explicit.example"

    def test_auth_target_defaults_to_anthropic_auth_token(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        # codex-default declares no auth_target; the claude wrapper maps the
        # token onto ANTHROPIC_AUTH_TOKEN by default (§10.3 injection target).
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")

        env = inject_profile_env({}, profile, "tok-default-target")

        assert env["ANTHROPIC_AUTH_TOKEN"] == "tok-default-target"

    def test_skips_base_url_when_profile_omits_it(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        # codex-default has base_url: null -> ANTHROPIC_BASE_URL is not injected.
        write_profiles(
            repo_root,
            "agent_profiles:\n"
            "  minimal:\n"
            "    cli: claude\n"
            "    auth_env: TOK\n"
            "    model: glm-5.2\n",
        )
        profile = load_profile(repo_root, "minimal")

        env = inject_profile_env({}, profile, "tok")

        assert "ANTHROPIC_BASE_URL" not in env
        assert env["ANTHROPIC_MODEL"] == "glm-5.2"


class TestBuildChildEnv:
    """Strip + inject composed: the child env the subprocess receives."""

    def test_child_env_has_only_three_anthropic_vars(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")

        env = build_child_env(profile, _CONTAMINATED_PARENT, "resolved-token")

        anthropic = {k: v for k, v in env.items() if k.startswith("ANTHROPIC_")}
        assert set(anthropic) == {
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_MODEL",
        }
        # No Claude-Code identity or alias leakage.
        assert not any(k.startswith("CLAUDE") for k in env)
        assert not any(k.startswith("AI_AGENT") for k in env)

    def test_child_env_token_is_resolved_value(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")

        env = build_child_env(profile, _CONTAMINATED_PARENT, "resolved-token")

        assert env["ANTHROPIC_AUTH_TOKEN"] == "resolved-token"


class TestRenderEnvSnapshot:
    """§10.3 evidence: child env snapshot - NAMES ONLY, values redacted.

    The snapshot is the proof env isolation worked: only the three target vars
    appear, every value is ``=<set>``, and the token value is never written.
    """

    def test_snapshot_lists_only_target_vars_redacted(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        child_env = build_child_env(profile, _CONTAMINATED_PARENT, "secret-tok-abc")

        text = render_env_snapshot(child_env, profile, "CC_GLM52_TOKEN", "2026-07-19T12:00:00Z")

        # Exactly the three target vars, values redacted.
        assert "ANTHROPIC_AUTH_TOKEN=<set>" in text
        assert "ANTHROPIC_BASE_URL=<set>" in text
        assert "ANTHROPIC_MODEL=<set>" in text
        # No contamination leaked into the snapshot.
        for var in ("CLAUDECODE", "AI_AGENT", "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
            assert var not in text

    def test_token_value_never_in_snapshot(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        # §10.2 / invariant #11: the token value must not appear in the snapshot
        # under any branch. A distinctive sentinel makes a leak visible.
        sentinel = "tok-SNAPSHOT-LEAK-CHECK-9f3a7c"
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        child_env = build_child_env(profile, _CONTAMINATED_PARENT, sentinel)

        text = render_env_snapshot(child_env, profile, "CC_GLM52_TOKEN", "2026-07-19T12:00:00Z")

        assert sentinel not in text

    def test_snapshot_header_carries_profile_and_source_names_only(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        child_env = build_child_env(profile, _CONTAMINATED_PARENT, "tok")

        text = render_env_snapshot(child_env, profile, "CC_GLM52_TOKEN", "2026-07-19T12:00:00Z")

        # The header carries profile name, base_url, model and the token SOURCE
        # name (never the value).
        assert "profile=cc-glm52" in text
        assert "base_url=https://ark.cn-beijing.volces.com/api/coding" in text
        assert "model=glm-5.2" in text
        assert "token_src=CC_GLM52_TOKEN" in text


class TestSnapshotTree:
    """``snapshot_tree`` - the RUN-dir file inventory used for changed-file diff.

    Each file is keyed by RUN-relative path with ``(size, mtime_ns)`` metadata,
    so an agent writing/changing a file shows up as a metadata delta. The
    prototype's ``snapshot_tree`` heredoc is the seed.
    """

    def test_captures_files_with_size_and_mtime(self, tmp_path: Path) -> None:
        (tmp_path / "workspace").mkdir()
        (tmp_path / "workspace" / "hello.py").write_text("def answer():\n    return 42\n")
        (tmp_path / "output").mkdir()
        (tmp_path / "output" / "result.json").write_text("{}")

        tree = snapshot_tree(tmp_path)

        assert tree["workspace/hello.py"][0] == len("def answer():\n    return 42\n")
        assert tree["output/result.json"][0] == 2
        # mtime_ns is an int (the diff compares tuples).
        assert isinstance(tree["workspace/hello.py"][1], int)

    def test_uses_run_relative_paths(self, tmp_path: Path) -> None:
        (tmp_path / "input").mkdir()
        (tmp_path / "input" / "role.md").write_text("role")

        tree = snapshot_tree(tmp_path)

        assert "input/role.md" in tree
        # No absolute paths leak into the keys.
        assert all(not k.startswith("/") for k in tree)

    def test_empty_dir_yields_empty_tree(self, tmp_path: Path) -> None:
        assert snapshot_tree(tmp_path) == {}


class TestComputeChangedFiles:
    """§13.2/§14.2: changed_files = (after - before) minus wrapper-owned.

    Wrapper-owned artifacts (stdout/stderr/metadata/snapshot/settings) are
    subtracted so ``changed_files`` only reports files the *agent* wrote.
    """

    def test_lists_new_files(self) -> None:
        before: dict[str, tuple[int, int]] = {}
        after = {
            "output/result.json": (50, 1),
            "output/result.md": (12, 2),
            "workspace/hello.py": (30, 3),
        }

        changed = compute_changed_files(before, after, WRAPPER_OWNED_RE)

        assert changed == ["output/result.json", "output/result.md", "workspace/hello.py"]

    def test_lists_modified_files(self) -> None:
        # Same path, different (size, mtime) -> changed.
        before = {"workspace/hello.py": (30, 1)}
        after = {"workspace/hello.py": (45, 2)}

        changed = compute_changed_files(before, after, WRAPPER_OWNED_RE)

        assert changed == ["workspace/hello.py"]

    def test_unchanged_files_excluded(self) -> None:
        before = {"input/role.md": (10, 1), "workspace/hello.py": (30, 2)}
        after = {"input/role.md": (10, 1), "workspace/hello.py": (30, 2)}

        changed = compute_changed_files(before, after, WRAPPER_OWNED_RE)

        assert changed == []

    def test_subtracts_wrapper_owned_artifacts(self) -> None:
        # stdout.log / stderr.log / metadata.json / env-snapshot / run-settings
        # are all wrapper-written - none may appear even though they are new in
        # ``after``. (The before/after tree snapshots themselves are held
        # in memory, not persisted, so they are not in the regex.)
        before: dict[str, tuple[int, int]] = {}
        after = {
            "output/stdout.log": (100, 1),
            "output/stderr.log": (0, 2),
            "output/metadata.json": (200, 3),
            "output/env-snapshot.txt": (80, 4),
            "output/.run-settings.json": (40, 7),
            "output/result.json": (50, 8),
        }

        changed = compute_changed_files(before, after, WRAPPER_OWNED_RE)

        assert changed == ["output/result.json"]

    def test_result_is_sorted(self) -> None:
        before: dict[str, tuple[int, int]] = {}
        after = {
            "workspace/zeta.py": (1, 1),
            "output/result.json": (1, 2),
            "workspace/alpha.py": (1, 3),
        }

        changed = compute_changed_files(before, after, WRAPPER_OWNED_RE)

        assert changed == ["output/result.json", "workspace/alpha.py", "workspace/zeta.py"]

    def test_deleted_files_not_listed(self) -> None:
        # A file present in before but gone in after is a deletion, not a change
        # the agent made - the prototype's ``after.items()`` iteration excludes
        # these (changed_files reports what the agent wrote, not what it removed).
        before = {"workspace/scratch.py": (10, 1)}
        after: dict[str, tuple[int, int]] = {}

        changed = compute_changed_files(before, after, WRAPPER_OWNED_RE)

        assert changed == []


class TestWriteMetadata:
    """§13.2: ``metadata.json`` carries the full wrapper-computed fact set.

    Every field the spec's example shows is present, sourced from the profile
    and the run facts. No token field ever appears (§10.2/invariant #11).
    """

    def test_writes_all_section_13_2_fields(self, tmp_path: Path, repo_root: Path, write_profiles: Callable[..., Path]) -> None:
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        meta_path = tmp_path / "metadata.json"

        write_metadata(
            meta_path,
            run_id="RUN-001",
            profile=profile,
            started_at="2026-07-19T10:00:00Z",
            ended_at="2026-07-19T10:05:00Z",
            exit_code=0,
            changed_files=["output/result.json", "output/result.md", "workspace/hello.py"],
        )

        md = json.loads(meta_path.read_text())
        assert md == {
            "run_id": "RUN-001",
            "profile": "cc-glm52",
            "cli": "claude",
            "backend": "glm",
            "model": "glm-5.2",
            "started_at": "2026-07-19T10:00:00Z",
            "ended_at": "2026-07-19T10:05:00Z",
            "exit_code": 0,
            "changed_files": ["output/result.json", "output/result.md", "workspace/hello.py"],
            "commits": [],
            "checks": [],
        }

    def test_no_token_field_anywhere(self, tmp_path: Path, repo_root: Path, write_profiles: Callable[..., Path]) -> None:
        # §10.2 / invariant #11: metadata carries no secret. A sentinel in the
        # env must not bleed into the file via the profile or changed_files.
        sentinel = "tok-META-LEAK-CHECK-4b8e21"
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        meta_path = tmp_path / "metadata.json"

        write_metadata(
            meta_path,
            run_id="RUN-001",
            profile=profile,
            started_at="2026-07-19T10:00:00Z",
            ended_at="2026-07-19T10:05:00Z",
            exit_code=0,
            changed_files=[],
        )

        assert sentinel not in meta_path.read_text()
        md = json.loads(meta_path.read_text())
        assert not any(k.lower().endswith("token") for k in md)

    def test_exit_code_recorded_verbatim(self, tmp_path: Path, repo_root: Path, write_profiles: Callable[..., Path]) -> None:
        # A non-zero claude exit (§24.1 run failure) is recorded as-is - the
        # wrapper captures, it does not reinterpret; consistency with
        # result.json is ticket 04's job.
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")

        write_metadata(
            tmp_path / "metadata.json",
            run_id="RUN-002",
            profile=profile,
            started_at="2026-07-19T10:00:00Z",
            ended_at="2026-07-19T10:01:00Z",
            exit_code=2,
            changed_files=[],
        )

        md = json.loads((tmp_path / "metadata.json").read_text())
        assert md["exit_code"] == 2


class TestBuildPrompt:
    """The self-contained ``claude -p`` prompt references the input package.

    The prompt is RUN-relative and self-contained: the agent reads the prepared
    input package (role/system/task-package/output-schema/allowed-files) and
    executes, with the hard constraints summarised so the boundary is unambiguous
    even before §14 validation runs.
    """

    def test_prompt_names_run_and_working_directory(self) -> None:
        text = build_prompt("RUN-007", Path("/tmp/repo/runs/RUN-007"))

        assert "RUN-007" in text
        assert "/tmp/repo/runs/RUN-007" in text

    def test_prompt_references_input_package_files(self) -> None:
        text = build_prompt("RUN-001", Path("/tmp/run"))

        for rel in (
            "input/role.md",
            "input/system.md",
            "input/task-package.md",
            "input/output-schema.json",
            "input/allowed-files.txt",
            "input/context/",
        ):
            assert rel in text

    def test_prompt_states_mandatory_result_outputs(self) -> None:
        text = build_prompt("RUN-001", Path("/tmp/run"))

        assert "output/result.json" in text
        assert "output/result.md" in text

    def test_prompt_carries_hard_constraints(self) -> None:
        text = build_prompt("RUN-001", Path("/tmp/run"))

        # The §14.2 boundary and §4.2/§4.3 prohibitions, summarised.
        assert "allowed-files.txt" in text
        assert "frozen" in text.lower()
        assert "git" in text.lower()


class TestAutoMemorySettings:
    """§14.2 hygiene: a settings file that disables Claude Code auto-memory.

    ``--settings`` carries ``{"autoMemoryEnabled": false}`` so the child
    ``claude`` does not write its memory directory under
    ``~/.claude/projects/<cwd>/`` (out-of-band harness state, §14.2). ``--bare``
    would also disable it but forces ``ANTHROPIC_API_KEY`` auth, incompatible
    with the 3P ``ANTHROPIC_AUTH_TOKEN`` flow - so the settings route is used.
    """

    def test_disables_auto_memory(self) -> None:
        settings = auto_memory_settings()

        assert settings["autoMemoryEnabled"] is False

    def test_settings_is_json_serialisable(self) -> None:
        # The dict is written via json.dumps and passed to ``--settings`` as a
        # file path; it must round-trip through JSON cleanly.
        settings = auto_memory_settings()

        assert json.loads(json.dumps(settings)) == {"autoMemoryEnabled": False}


class TestBuildCliFlags:
    """§11.1: the headless invocation carries every hard flag, incl ``--verbose``.

    The prototype's flag set is the seed (ticket 03 inlines the decision):
    ``--output-format stream-json --verbose --include-partial-messages
    --permission-mode bypassPermissions --max-turns <n>``, plus ``--settings``
    for §14.2 auto-memory hygiene. ``--verbose`` is hard-required for
    ``stream-json`` + ``-p`` on claude v2.1.207+ (spec §11.1 footnote).
    """

    def test_includes_every_hard_flag(self) -> None:
        flags = build_cli_flags(Path("/tmp/run/output/.run-settings.json"), max_turns=12)

        assert "--output-format" in flags
        assert flags[flags.index("--output-format") + 1] == "stream-json"
        assert "--verbose" in flags
        assert "--include-partial-messages" in flags
        assert "--permission-mode" in flags
        assert flags[flags.index("--permission-mode") + 1] == "bypassPermissions"
        assert "--max-turns" in flags
        assert flags[flags.index("--max-turns") + 1] == "12"

    def test_includes_settings_flag(self) -> None:
        settings_path = Path("/tmp/run/output/.run-settings.json")

        flags = build_cli_flags(settings_path, max_turns=12)

        assert "--settings" in flags
        assert flags[flags.index("--settings") + 1] == str(settings_path)

    def test_max_turns_parameterised(self) -> None:
        flags = build_cli_flags(Path("/tmp/s.json"), max_turns=6)

        assert flags[flags.index("--max-turns") + 1] == "6"

    def test_permission_mode_parameterised(self) -> None:
        flags = build_cli_flags(Path("/tmp/s.json"), max_turns=12, permission_mode="plan")

        assert flags[flags.index("--permission-mode") + 1] == "plan"

    def test_does_not_pin_streaming_model_id(self) -> None:
        # Ticket 03 decision: do NOT pass ``--model`` - the streaming model id
        # varies across backends (z.ai reports gpt-5.5, ark reports glm-5.2);
        # the profile-declared model is injected via ``ANTHROPIC_MODEL`` env
        # instead, so the flag set must not contain ``--model``.
        flags = build_cli_flags(Path("/tmp/s.json"), max_turns=12)

        assert "--model" not in flags


# A fake ``claude`` binary: ignores its argv, writes the §13.1 agent outputs
# (result.json / result.md / a workspace file) into its cwd, prints one
# stream-json ``result`` line to stdout, and exits 0. Stands in for the real
# CLI so the orchestrator can be exercised end-to-end without network or token.
# ``__PY__`` is replaced with the test interpreter so the shebang resolves under
# ``uv run`` (string replace, not ``.format``, so the JSON braces are literal).
_FAKE_CLAUDE = """\
#!__PY__
import json, os, sys
os.makedirs("workspace", exist_ok=True)
os.makedirs("output", exist_ok=True)
with open("workspace/hello.py", "w") as f:
    f.write("# throwaway prototype module\\n")
    f.write("def answer():\\n    return 42\\n")
with open("output/result.md", "w") as f:
    f.write("Wrote workspace/hello.py for the run.\\n")
with open("output/result.json", "w") as f:
    json.dump(
        {
            "status": "proposed_done",
            "summary": "Wrote workspace/hello.py for the run.",
            "tasks": [
                {"id": "TASK-001", "status": "proposed_done",
                 "evidence": ["workspace/hello.py"]}
            ],
        },
        f,
    )
sys.stdout.write('{"type":"result","subtype":"success","is_error":false}\\n')
sys.exit(0)
"""


def _write_fake_claude(tmp_path: Path) -> Path:
    """Write the fake ``claude`` script and return its executable path."""
    script = tmp_path / "fake-claude"
    script.write_text(_FAKE_CLAUDE.replace("__PY__", sys.executable))
    os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _make_prepared_run(repo_root: Path, role: str = "Implementer") -> str:
    """Create a feature run + prepare RUN-001; return the run id."""
    feature_id = create_feature_run(repo_root, "de-risk the headless wrapper")
    return prepare_run(repo_root, feature_id, role, "Create workspace/hello.py.")


def _audit_records(repo_root: Path, feature_id: str) -> list[dict[str, object]]:
    log = (
        repo_root / ".ai-dev" / "features" / feature_id / AUDIT_LOG_JSON
    )
    return json.loads(log.read_text())


class TestRunHeadless:
    """``run_headless`` - the full wrapper seam, driven by a fake ``claude``.

    Exercises env isolation, capture, changed_files, metadata and the audit
    event against a prepared run, without touching the real CLI. Token safety
    (§10.2/invariant #11) is pinned: the resolved token value never lands on
    disk inside the run directory.
    """

    def test_captures_exit_code_and_agent_outputs(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "resolved-token-value")
        feature_id = "FEATURE-001"
        _make_prepared_run(repo_root)
        fake = _write_fake_claude(tmp_path)

        result = run_headless(
            repo_root,
            feature_id,
            "RUN-001",
            profile,
            claude_path=str(fake),
            started_at="2026-07-19T10:00:00Z",
            ended_at="2026-07-19T10:00:05Z",
        )

        assert result.exit_code == 0
        assert result.changed_files == [
            "output/result.json",
            "output/result.md",
            "workspace/hello.py",
        ]

    def test_writes_stdout_stderr_logs(self, repo_root: Path, write_profiles: Callable[..., Path], clean_token_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok")
        _make_prepared_run(repo_root)
        fake = _write_fake_claude(tmp_path)

        result = run_headless(
            repo_root, "FEATURE-001", "RUN-001", profile, claude_path=str(fake),
            started_at="2026-07-19T10:00:00Z", ended_at="2026-07-19T10:00:05Z",
        )

        assert result.stdout_path.is_file()
        assert result.stderr_path.is_file()
        # The fake claude prints one stream-json line to stdout.
        assert "result" in result.stdout_path.read_text()
        # stderr is empty (the fake writes nothing there).
        assert result.stderr_path.read_text() == ""

    def test_writes_metadata_with_full_field_set(
        self, repo_root: Path, write_profiles: Callable[..., Path], clean_token_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok")
        _make_prepared_run(repo_root)
        fake = _write_fake_claude(tmp_path)

        run_headless(
            repo_root, "FEATURE-001", "RUN-001", profile, claude_path=str(fake),
            started_at="2026-07-19T10:00:00Z", ended_at="2026-07-19T10:00:05Z",
        )

        md = json.loads(_result_metadata(repo_root, "FEATURE-001", "RUN-001"))
        assert md["run_id"] == "RUN-001"
        assert md["profile"] == "cc-glm52"
        assert md["cli"] == "claude"
        assert md["backend"] == "glm"
        assert md["model"] == "glm-5.2"
        assert md["started_at"] == "2026-07-19T10:00:00Z"
        assert md["ended_at"] == "2026-07-19T10:00:05Z"
        assert md["exit_code"] == 0
        assert md["changed_files"] == [
            "output/result.json",
            "output/result.md",
            "workspace/hello.py",
        ]
        assert md["commits"] == []
        assert md["checks"] == []

    def test_writes_env_snapshot_with_only_three_target_vars(
        self, repo_root: Path, write_profiles: Callable[..., Path], clean_token_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # §10.3 evidence: the persisted env snapshot has exactly the three
        # target vars, redacted - proving the strip + inject worked end-to-end.
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok")
        _make_prepared_run(repo_root)
        fake = _write_fake_claude(tmp_path)

        run_headless(
            repo_root, "FEATURE-001", "RUN-001", profile, claude_path=str(fake),
            started_at="2026-07-19T10:00:00Z", ended_at="2026-07-19T10:00:05Z",
        )

        snap = (
            run_dir(repo_root, "FEATURE-001", "RUN-001") / "output" / "env-snapshot.txt"
        ).read_text()
        assert "ANTHROPIC_AUTH_TOKEN=<set>" in snap
        assert "ANTHROPIC_BASE_URL=<set>" in snap
        assert "ANTHROPIC_MODEL=<set>" in snap
        for var in ("CLAUDECODE", "AI_AGENT", "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
            assert var not in snap

    def test_writes_auto_memory_off_settings(
        self, repo_root: Path, write_profiles: Callable[..., Path], clean_token_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # §14.2 hygiene: a settings file disabling auto-memory is written and
        # passed via ``--settings``.
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok")
        _make_prepared_run(repo_root)
        fake = _write_fake_claude(tmp_path)

        run_headless(
            repo_root, "FEATURE-001", "RUN-001", profile, claude_path=str(fake),
            started_at="2026-07-19T10:00:00Z", ended_at="2026-07-19T10:00:05Z",
        )

        settings_path = (
            run_dir(repo_root, "FEATURE-001", "RUN-001") / "output" / ".run-settings.json"
        )
        assert settings_path.is_file()
        assert json.loads(settings_path.read_text()) == auto_memory_settings()

    def test_token_value_never_lands_on_disk(
        self, repo_root: Path, write_profiles: Callable[..., Path], clean_token_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # §10.2 / invariant #11: the resolved token value must not appear in any
        # file the wrapper writes inside the run directory. A distinctive
        # sentinel makes a leak visible across env-snapshot / metadata /
        # stdout / stderr / settings.
        sentinel = "tok-RUN-LEAK-CHECK-e1c8a7"
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", sentinel)
        _make_prepared_run(repo_root)
        fake = _write_fake_claude(tmp_path)

        run_headless(
            repo_root, "FEATURE-001", "RUN-001", profile, claude_path=str(fake),
            started_at="2026-07-19T10:00:00Z", ended_at="2026-07-19T10:00:05Z",
        )

        run_root = run_dir(repo_root, "FEATURE-001", "RUN-001")
        for path in run_root.rglob("*"):
            if path.is_file():
                assert sentinel not in path.read_text(errors="ignore"), (
                    f"token leaked into {path}"
                )

    def test_audits_run_event(
        self, repo_root: Path, write_profiles: Callable[..., Path], clean_token_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # §2.1: a run lifecycle event flows through the audit log.
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok")
        _make_prepared_run(repo_root)
        fake = _write_fake_claude(tmp_path)

        run_headless(
            repo_root, "FEATURE-001", "RUN-001", profile, claude_path=str(fake),
            started_at="2026-07-19T10:00:00Z", ended_at="2026-07-19T10:00:05Z",
            origin="implement-leg",
        )

        records = _audit_records(repo_root, "FEATURE-001")
        run_events = [r for r in records if r.get("event") == "run"]
        assert len(run_events) == 1
        payload = run_events[0]["payload"]
        assert isinstance(payload, dict)
        assert payload.get("run") == "RUN-001"
        assert payload.get("exit_code") == 0
        # v0.4 ticket 02: elapsed_ms is the real ended-started delta (5s) and
        # origin threads through as the top-level driver tag.
        assert payload.get("elapsed_ms") == 5_000
        assert run_events[0].get("origin") == "implement-leg"

    def test_missing_run_dir_raises(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # §24.2 fail loud: no prepared run directory -> ValueError, not a
        # silent empty run.
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        create_feature_run(repo_root, "intent")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok")

        with pytest.raises(ValueError, match="RUN-999"):
            run_headless(repo_root, "FEATURE-001", "RUN-999", profile)

    def test_missing_token_raises(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
    ) -> None:
        # §24.2 fail loud: profile token source unset -> ValueError before any
        # subprocess is spawned (no token to inject).
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        _make_prepared_run(repo_root)

        with pytest.raises(ValueError, match="token"):
            run_headless(repo_root, "FEATURE-001", "RUN-001", profile)

    def test_passes_cli_flags_to_subprocess(
        self, repo_root: Path, write_profiles: Callable[..., Path], clean_token_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The fake claude records its argv into stderr so the test can assert
        # the §11.1 flag set (incl ``--verbose`` and ``--settings``) was passed.
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "tok")
        _make_prepared_run(repo_root)
        recording = tmp_path / "fake-claude-record"
        recording.write_text(
            f"#!{sys.executable}\n"
            "import sys, os\n"
            "open(os.path.join('output','argv.txt'),'w').write('\\n'.join(sys.argv))\n"
            "sys.exit(0)\n"
        )
        os.chmod(recording, os.stat(recording).st_mode | stat.S_IXUSR)

        run_headless(
            repo_root, "FEATURE-001", "RUN-001", profile, claude_path=str(recording),
            max_turns=9, started_at="2026-07-19T10:00:00Z",
            ended_at="2026-07-19T10:00:05Z",
        )

        argv = (
            run_dir(repo_root, "FEATURE-001", "RUN-001") / "output" / "argv.txt"
        ).read_text()
        assert "--verbose" in argv
        assert "--output-format" in argv
        assert "stream-json" in argv
        assert "--include-partial-messages" in argv
        assert "--permission-mode" in argv
        assert "bypassPermissions" in argv
        assert "--max-turns" in argv
        assert "9" in argv
        assert "--settings" in argv
        assert "-p" in argv


def _result_metadata(repo_root: Path, feature_id: str, run_id: str) -> str:
    """Read the metadata.json the wrapper wrote for a run."""
    return (
        run_dir(repo_root, feature_id, run_id) / "output" / "metadata.json"
    ).read_text()


# ---------------------------------------------------------------------------
# ADR-0005 (v0.5 ticket 02): multi-CLI dispatch. CodexRunner + registry tests
# at the run_headless seam - mirror the claude TestRunHeadless shape so the two
# adapters are exercised symmetrically. The codex argv follows the
# spike-amended ADR (D3/D4), NOT the original checklist text.
# ---------------------------------------------------------------------------

# A fake ``codex`` binary: reads the prompt from stdin (``codex exec -``),
# writes the §13.1 agent outputs into its cwd, and exits 0. Stands in for the
# real CLI so the codex dispatch is exercised without network or token.
# ``__PY__`` is replaced with the test interpreter (string replace, not
# ``.format``, so the JSON braces stay literal) - same trick as _FAKE_CLAUDE.
_FAKE_CODEX = """\
#!__PY__
import json, os, sys
_prompt = sys.stdin.read()
os.makedirs("workspace", exist_ok=True)
os.makedirs("output", exist_ok=True)
with open("workspace/hello.py", "w") as f:
    f.write("# codex-written module\\n")
    f.write("def answer():\\n    return 42\\n")
with open("output/result.md", "w") as f:
    f.write("Codex wrote workspace/hello.py for the run.\\n")
with open("output/result.json", "w") as f:
    json.dump(
        {
            "status": "proposed_done",
            "summary": "Codex wrote workspace/hello.py for the run.",
            "tasks": [
                {"id": "TASK-001", "status": "proposed_done",
                 "evidence": ["workspace/hello.py"]}
            ],
        },
        f,
    )
sys.exit(0)
"""

# A recording fake ``codex``: writes its argv + the stdin prompt + its cwd to
# ``output/`` so the dispatch/argv/stdin/cwd contract is assertable. Exits 0
# without writing the §13 outputs (the recording tests only inspect argv/stdin).
_FAKE_CODEX_RECORD = """\
#!__PY__
import os, sys
_prompt = sys.stdin.read()
os.makedirs("output", exist_ok=True)
with open("output/argv.txt", "w") as f:
    f.write("\\n".join(sys.argv))
with open("output/stdin.txt", "w") as f:
    f.write(_prompt)
with open("output/cwd.txt", "w") as f:
    f.write(os.getcwd())
sys.exit(0)
"""


def _write_fake_codex(tmp_path: Path, body: str) -> Path:
    """Write a fake ``codex`` script and return its executable path."""
    script = tmp_path / "fake-codex"
    script.write_text(body.replace("__PY__", sys.executable))
    os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


# The canonical codex-default profile shape (spec §10.1): cli: codex,
# backend: openai, base_url: null, auth_env: OPENAI_API_KEY, model: null,
# invocation: headless, extra_env: {}. A model-bearing variant exercises -m.
CODEX_DEFAULT_PROFILE_YAML = """\
agent_profiles:
  codex-default:
    cli: codex
    backend: openai
    base_url: null
    auth_env: "OPENAI_API_KEY"
    model: null
    invocation: headless
    extra_env: {}
"""

CODEX_MODEL_PROFILE_YAML = """\
agent_profiles:
  codex-gpt55:
    cli: codex
    backend: openai
    base_url: null
    auth_env: "OPENAI_API_KEY"
    model: "gpt-5.5"
    invocation: headless
    extra_env: {}
"""


class TestRunnerRegistry:
    """ADR-0005 D1/D2: the registry is keyed by ``profile.cli``, not backend."""

    def test_returns_codex_runner_for_codex_cli(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        write_profiles(repo_root, CODEX_DEFAULT_PROFILE_YAML)
        profile = load_profile(repo_root, "codex-default")

        runner = get_runner(profile)

        assert isinstance(runner, CodexRunner)
        assert runner.cli == "codex"
        assert runner.token_required is False

    def test_returns_claude_runner_for_claude_cli(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")

        runner = get_runner(profile)

        assert isinstance(runner, ClaudeRunner)
        assert runner.cli == "claude"
        assert runner.token_required is True

    def test_dispatch_on_cli_not_backend(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        # A ``cli: claude`` profile with a non-glm backend still resolves the
        # ClaudeRunner - dispatch is on ``cli``, not ``backend`` (D2). This is
        # the cc-minimaxm3 / cc-deepseekv4pro shape (claude CLI, other backend).
        write_profiles(
            repo_root,
            "agent_profiles:\n"
            "  cc-minimaxm3:\n"
            "    cli: claude\n"
            "    backend: minimax\n"
            "    auth_env: CC_MINIMAXM3_TOKEN\n"
            "    model: MiniMax-M3\n",
        )
        profile = load_profile(repo_root, "cc-minimaxm3")

        runner = get_runner(profile)

        assert isinstance(runner, ClaudeRunner)

    def test_unknown_cli_raises(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        # §24.2 fail loud: an unregistered cli surfaces as a ValueError, not a
        # silent claude fallback.
        write_profiles(
            repo_root,
            "agent_profiles:\n"
            "  gemini-cli:\n"
            "    cli: gemini\n"
            "    auth_env: GEMINI_TOKEN\n",
        )
        profile = load_profile(repo_root, "gemini-cli")

        with pytest.raises(ValueError, match="gemini"):
            get_runner(profile)

    def test_codex_wrapper_owned_re_excludes_settings_file(self) -> None:
        # codex writes no .run-settings.json; its subtract set is the claude one
        # minus the settings file (stdout/stderr/metadata/env-snapshot only).
        for path in (
            "output/stdout.log",
            "output/stderr.log",
            "output/metadata.json",
            "output/env-snapshot.txt",
        ):
            assert CODEX_WRAPPER_OWNED_RE.search(path), f"{path} should be wrapper-owned"
        assert not CODEX_WRAPPER_OWNED_RE.search("output/.run-settings.json")
        assert not CODEX_WRAPPER_OWNED_RE.search("output/result.json")
        assert not CODEX_WRAPPER_OWNED_RE.search("workspace/hello.py")


class TestCodexRunHeadless:
    """CodexRunner at the run_headless seam - argv, stdin, env, capture (D2-D5).

    The codex argv follows the spike-amended ADR-0005: ``codex exec -`` (prompt
    on stdin), ``-s workspace-write`` (cwd=run_dir), ``--skip-git-repo-check``,
    ``--color never``, ``--ephemeral``; NO ``--remote-auth-token-env`` (D3
    amended - rejected by ``codex exec``). Token via OPENAI_API_KEY env-injection
    (OpenAI provider) or stored creds (custom provider, no fail-loud).
    """

    def test_captures_exit_code_and_agent_outputs(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        write_profiles(repo_root, CODEX_DEFAULT_PROFILE_YAML)
        profile = load_profile(repo_root, "codex-default")
        monkeypatch.setenv("OPENAI_API_KEY", "codex-token-value")
        _make_prepared_run(repo_root)
        fake = _write_fake_codex(tmp_path, _FAKE_CODEX)

        result = run_headless(
            repo_root, "FEATURE-001", "RUN-001", profile, cli_path=str(fake),
            started_at="2026-07-22T10:00:00Z", ended_at="2026-07-22T10:00:05Z",
        )

        assert result.exit_code == 0
        assert result.changed_files == [
            "output/result.json",
            "output/result.md",
            "workspace/hello.py",
        ]

    def test_prompt_passed_on_stdin_not_argv(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # codex exec reads the prompt from stdin (``codex exec -``); the prompt
        # text must NOT ride in argv (unlike claude's ``-p <prompt>``).
        write_profiles(repo_root, CODEX_DEFAULT_PROFILE_YAML)
        profile = load_profile(repo_root, "codex-default")
        monkeypatch.setenv("OPENAI_API_KEY", "tok")
        _make_prepared_run(repo_root)
        fake = _write_fake_codex(tmp_path, _FAKE_CODEX_RECORD)

        run_headless(
            repo_root, "FEATURE-001", "RUN-001", profile, cli_path=str(fake),
            started_at="2026-07-22T10:00:00Z", ended_at="2026-07-22T10:00:05Z",
        )

        out = run_dir(repo_root, "FEATURE-001", "RUN-001") / "output"
        stdin_text = (out / "stdin.txt").read_text()
        argv_lines = (out / "argv.txt").read_text().split("\n")
        # The prompt is on stdin...
        assert "You are executing Agent Run RUN-001" in stdin_text
        assert "input/task-package.md" in stdin_text
        # ...and NOT in argv (no ``-p <prompt>``); the ``-`` sentinel is.
        assert "-p" not in argv_lines
        assert "You are executing Agent Run" not in "\n".join(argv_lines)
        assert "-" in argv_lines

    def test_codex_argv_shape(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # ADR-0005 D2/D4 + spike amendments: codex exec - (stdin prompt),
        # -s workspace-write, --skip-git-repo-check, --color never, --ephemeral.
        # NO --remote-auth-token-env (D3 amended - rejected by codex exec).
        write_profiles(repo_root, CODEX_DEFAULT_PROFILE_YAML)
        profile = load_profile(repo_root, "codex-default")
        monkeypatch.setenv("OPENAI_API_KEY", "tok")
        _make_prepared_run(repo_root)
        fake = _write_fake_codex(tmp_path, _FAKE_CODEX_RECORD)

        run_headless(
            repo_root, "FEATURE-001", "RUN-001", profile, cli_path=str(fake),
            started_at="2026-07-22T10:00:00Z", ended_at="2026-07-22T10:00:05Z",
        )

        argv_lines = (
            run_dir(repo_root, "FEATURE-001", "RUN-001") / "output" / "argv.txt"
        ).read_text().split("\n")
        assert "exec" in argv_lines
        assert "-" in argv_lines  # the stdin-prompt sentinel
        assert "-s" in argv_lines
        assert "workspace-write" in argv_lines
        assert "--skip-git-repo-check" in argv_lines
        assert "--color" in argv_lines
        assert "never" in argv_lines
        assert "--ephemeral" in argv_lines
        # D3 amended: --remote-auth-token-env is NOT in the codex argv.
        assert "--remote-auth-token-env" not in argv_lines

    def test_model_flag_when_profile_has_model(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        write_profiles(repo_root, CODEX_MODEL_PROFILE_YAML)
        profile = load_profile(repo_root, "codex-gpt55")
        monkeypatch.setenv("OPENAI_API_KEY", "tok")
        _make_prepared_run(repo_root)
        fake = _write_fake_codex(tmp_path, _FAKE_CODEX_RECORD)

        run_headless(
            repo_root, "FEATURE-001", "RUN-001", profile, cli_path=str(fake),
            started_at="2026-07-22T10:00:00Z", ended_at="2026-07-22T10:00:05Z",
        )

        argv_lines = (
            run_dir(repo_root, "FEATURE-001", "RUN-001") / "output" / "argv.txt"
        ).read_text().split("\n")
        assert "-m" in argv_lines
        assert "gpt-5.5" in argv_lines

    def test_no_model_flag_when_profile_model_null(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # codex-default has model: null -> no -m flag; codex resolves the model
        # from ~/.codex/config.toml.
        write_profiles(repo_root, CODEX_DEFAULT_PROFILE_YAML)
        profile = load_profile(repo_root, "codex-default")
        monkeypatch.setenv("OPENAI_API_KEY", "tok")
        _make_prepared_run(repo_root)
        fake = _write_fake_codex(tmp_path, _FAKE_CODEX_RECORD)

        run_headless(
            repo_root, "FEATURE-001", "RUN-001", profile, cli_path=str(fake),
            started_at="2026-07-22T10:00:00Z", ended_at="2026-07-22T10:00:05Z",
        )

        argv_lines = (
            run_dir(repo_root, "FEATURE-001", "RUN-001") / "output" / "argv.txt"
        ).read_text().split("\n")
        assert "-m" not in argv_lines

    def test_openai_api_key_injected_into_child_env(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # D3 amended (OpenAI-provider path): the token is injected onto
        # profile.auth_env (OPENAI_API_KEY) - the same env-injection pattern
        # claude uses for ANTHROPIC_AUTH_TOKEN. The env snapshot proves it.
        write_profiles(repo_root, CODEX_DEFAULT_PROFILE_YAML)
        profile = load_profile(repo_root, "codex-default")
        monkeypatch.setenv("OPENAI_API_KEY", "codex-openai-token")
        _make_prepared_run(repo_root)
        fake = _write_fake_codex(tmp_path, _FAKE_CODEX)

        run_headless(
            repo_root, "FEATURE-001", "RUN-001", profile, cli_path=str(fake),
            started_at="2026-07-22T10:00:00Z", ended_at="2026-07-22T10:00:05Z",
        )

        snap = (
            run_dir(repo_root, "FEATURE-001", "RUN-001") / "output" / "env-snapshot.txt"
        ).read_text()
        assert "OPENAI_API_KEY=<set>" in snap
        # codex snapshot header labels the engine and the token source name.
        assert "codex" in snap
        assert "token_src=OPENAI_API_KEY" in snap

    def test_no_run_settings_file_for_codex(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # codex has no --settings analogue; --ephemeral (in argv) is the
        # session-persistence hygiene flag. No .run-settings.json is written.
        write_profiles(repo_root, CODEX_DEFAULT_PROFILE_YAML)
        profile = load_profile(repo_root, "codex-default")
        monkeypatch.setenv("OPENAI_API_KEY", "tok")
        _make_prepared_run(repo_root)
        fake = _write_fake_codex(tmp_path, _FAKE_CODEX)

        run_headless(
            repo_root, "FEATURE-001", "RUN-001", profile, cli_path=str(fake),
            started_at="2026-07-22T10:00:00Z", ended_at="2026-07-22T10:00:05Z",
        )

        settings = (
            run_dir(repo_root, "FEATURE-001", "RUN-001") / "output" / ".run-settings.json"
        )
        assert not settings.exists()

    def test_metadata_records_codex_cli_and_backend(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        write_profiles(repo_root, CODEX_DEFAULT_PROFILE_YAML)
        profile = load_profile(repo_root, "codex-default")
        monkeypatch.setenv("OPENAI_API_KEY", "tok")
        _make_prepared_run(repo_root)
        fake = _write_fake_codex(tmp_path, _FAKE_CODEX)

        run_headless(
            repo_root, "FEATURE-001", "RUN-001", profile, cli_path=str(fake),
            started_at="2026-07-22T10:00:00Z", ended_at="2026-07-22T10:00:05Z",
        )

        md = json.loads(_result_metadata(repo_root, "FEATURE-001", "RUN-001"))
        assert md["cli"] == "codex"
        assert md["backend"] == "openai"
        assert md["profile"] == "codex-default"
        assert md["model"] is None
        assert md["exit_code"] == 0
        assert md["changed_files"] == [
            "output/result.json",
            "output/result.md",
            "workspace/hello.py",
        ]

    def test_cwd_is_run_dir(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # D4 amended: cwd = run_dir (the RUN-NNN directory), so the §13 outputs
        # and workspace files land inside the run dir and the workspace-write
        # sandbox confines writes to it.
        write_profiles(repo_root, CODEX_DEFAULT_PROFILE_YAML)
        profile = load_profile(repo_root, "codex-default")
        monkeypatch.setenv("OPENAI_API_KEY", "tok")
        _make_prepared_run(repo_root)
        fake = _write_fake_codex(tmp_path, _FAKE_CODEX_RECORD)

        run_headless(
            repo_root, "FEATURE-001", "RUN-001", profile, cli_path=str(fake),
            started_at="2026-07-22T10:00:00Z", ended_at="2026-07-22T10:00:05Z",
        )

        cwd_text = (
            run_dir(repo_root, "FEATURE-001", "RUN-001") / "output" / "cwd.txt"
        ).read_text()
        assert cwd_text == str(run_dir(repo_root, "FEATURE-001", "RUN-001").resolve())

    def test_token_value_never_lands_on_disk(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # §10.2 / invariant #11: the resolved OPENAI_API_KEY value must not
        # appear in any file the wrapper writes inside the run directory.
        sentinel = "tok-CODEX-LEAK-CHECK-7d2a9e"
        write_profiles(repo_root, CODEX_DEFAULT_PROFILE_YAML)
        profile = load_profile(repo_root, "codex-default")
        monkeypatch.setenv("OPENAI_API_KEY", sentinel)
        _make_prepared_run(repo_root)
        fake = _write_fake_codex(tmp_path, _FAKE_CODEX)

        run_headless(
            repo_root, "FEATURE-001", "RUN-001", profile, cli_path=str(fake),
            started_at="2026-07-22T10:00:00Z", ended_at="2026-07-22T10:00:05Z",
        )

        run_root = run_dir(repo_root, "FEATURE-001", "RUN-001")
        for path in run_root.rglob("*"):
            if path.is_file():
                assert sentinel not in path.read_text(errors="ignore"), (
                    f"token leaked into {path}"
                )

    def test_does_not_fail_loud_on_missing_token(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # D3 amended (custom-provider path): codex may use stored
        # ~/.codex/auth.json when no env token is set. Unlike claude (which
        # fails loud), a codex run with OPENAI_API_KEY unset proceeds - the
        # adapter injects nothing and lets codex resolve auth itself. The env
        # snapshot honestly records token_src=None.
        write_profiles(repo_root, CODEX_DEFAULT_PROFILE_YAML)
        profile = load_profile(repo_root, "codex-default")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        _make_prepared_run(repo_root)
        fake = _write_fake_codex(tmp_path, _FAKE_CODEX)

        result = run_headless(
            repo_root, "FEATURE-001", "RUN-001", profile, cli_path=str(fake),
            started_at="2026-07-22T10:00:00Z", ended_at="2026-07-22T10:00:05Z",
        )

        assert result.exit_code == 0
        snap = (
            run_dir(repo_root, "FEATURE-001", "RUN-001") / "output" / "env-snapshot.txt"
        ).read_text()
        assert "token_src=None" in snap

    def test_codex_binary_not_found_raises(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # §24.2 fail loud: codex not on PATH and no override -> ValueError
        # before any subprocess is spawned. (The dev env has codex installed, so
        # shutil.which is stubbed to None to exercise the not-found branch
        # without spawning the real CLI.)
        write_profiles(repo_root, CODEX_DEFAULT_PROFILE_YAML)
        profile = load_profile(repo_root, "codex-default")
        monkeypatch.setenv("OPENAI_API_KEY", "tok")
        _make_prepared_run(repo_root)
        monkeypatch.setattr(shutil, "which", lambda _name: None)

        with pytest.raises(ValueError, match="codex"):
            run_headless(
                repo_root, "FEATURE-001", "RUN-001", profile,
                started_at="2026-07-22T10:00:00Z", ended_at="2026-07-22T10:00:05Z",
            )

    def test_missing_run_dir_raises(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The run-dir precondition is shared (engine-agnostic): a missing run
        # dir fails loud regardless of the adapter.
        write_profiles(repo_root, CODEX_DEFAULT_PROFILE_YAML)
        profile = load_profile(repo_root, "codex-default")
        monkeypatch.setenv("OPENAI_API_KEY", "tok")
        create_feature_run(repo_root, "intent")

        with pytest.raises(ValueError, match="RUN-999"):
            run_headless(repo_root, "FEATURE-001", "RUN-999", profile)

    def test_audits_run_event(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # §2.1: a codex run lifecycle event flows through the audit log too -
        # the audit step is shared, engine-agnostic.
        write_profiles(repo_root, CODEX_DEFAULT_PROFILE_YAML)
        profile = load_profile(repo_root, "codex-default")
        monkeypatch.setenv("OPENAI_API_KEY", "tok")
        _make_prepared_run(repo_root)
        fake = _write_fake_codex(tmp_path, _FAKE_CODEX)

        run_headless(
            repo_root, "FEATURE-001", "RUN-001", profile, cli_path=str(fake),
            started_at="2026-07-22T10:00:00Z", ended_at="2026-07-22T10:00:05Z",
            origin="implement-leg",
        )

        records = _audit_records(repo_root, "FEATURE-001")
        run_events = [r for r in records if r.get("event") == "run"]
        assert len(run_events) == 1
        payload = run_events[0]["payload"]
        assert isinstance(payload, dict)
        assert payload.get("run") == "RUN-001"
        assert payload.get("profile") == "codex-default"
        assert payload.get("exit_code") == 0
        assert payload.get("elapsed_ms") == 5_000
        assert run_events[0].get("origin") == "implement-leg"


class TestCodexEnvSnapshot:
    """The codex env snapshot greps openai/codex/AI_ (not claude/anthropic)."""

    def test_lists_openai_api_key_redacted(self) -> None:
        # The codex snapshot must surface OPENAI_API_KEY (the var codex reads on
        # the OpenAI-provider path), redacted - proving the env-injection.
        child_env = {"OPENAI_API_KEY": "secret-openai", "PATH": "/bin"}
        profile = AgentProfile(
            name="codex-default", cli="codex", auth_env="OPENAI_API_KEY",
            backend="openai", model=None,
        )

        text = render_codex_env_snapshot(child_env, profile, "OPENAI_API_KEY", "2026-07-22T10:00:00Z")

        assert "OPENAI_API_KEY=<set>" in text
        assert "secret-openai" not in text

    def test_does_not_list_anthropic_vars(self) -> None:
        # A claude-orchestrator parent may carry ANTHROPIC_* vars; the codex
        # snapshot must NOT surface them (codex does not read ANTHROPIC_*).
        child_env = {
            "OPENAI_API_KEY": "tok",
            "ANTHROPIC_AUTH_TOKEN": "claude-tok",
            "ANTHROPIC_BASE_URL": "https://ark",
        }
        profile = AgentProfile(
            name="codex-default", cli="codex", auth_env="OPENAI_API_KEY",
            backend="openai", model=None,
        )

        text = render_codex_env_snapshot(child_env, profile, "OPENAI_API_KEY", "2026-07-22T10:00:00Z")

        assert "OPENAI_API_KEY=<set>" in text
        assert "ANTHROPIC_AUTH_TOKEN" not in text
        assert "ANTHROPIC_BASE_URL" not in text

    def test_token_value_never_in_snapshot(self) -> None:
        # §10.2 / invariant #11: the OPENAI_API_KEY value must not appear.
        sentinel = "tok-CODEX-SNAP-LEAK-2c4f81"
        child_env = {"OPENAI_API_KEY": sentinel}
        profile = AgentProfile(
            name="codex-default", cli="codex", auth_env="OPENAI_API_KEY",
            backend="openai", model=None,
        )

        text = render_codex_env_snapshot(child_env, profile, "OPENAI_API_KEY", "2026-07-22T10:00:00Z")

        assert sentinel not in text



