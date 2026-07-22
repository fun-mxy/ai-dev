"""profiles - agent-profiles.yml loader + token-by-name resolution (ticket 01).

Loads ``.ai-dev/agent-profiles.yml`` and parses a single named profile into an
``AgentProfile`` - the config snapshot the run adapter (ticket 03) consumes.
The token is resolved **by env-var name only** (§10.2, invariant #11): the
profile carries the source/target *names*; ``token_source_var`` reports which
source name currently holds a non-empty value, but the value itself is never
stored on the profile and never returned by any function here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from ai_dev.profiles import (
    AgentProfile,
    ProfileError,
    ROLE_IMPLEMENTER,
    ROLE_REVIEWER,
    ROLE_SPEC_GAP_ANALYST,
    load_profile,
    load_role_defaults,
    render_profile,
    resolve_profile_name,
    token_source_var,
)

# codex-default from §10.1 - exercises the null/optional fields: base_url null,
# model null, no auth_env_fallback, no auth_target, empty extra_env. Variant
# registry local to this module (only the profiles-module tests need it).
CODEX_DEFAULT_YAML = """\
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

# A v0.4-era cc-glm52 registry that predates role_defaults (ticket 03). Used to
# pin backward compat: the field is optional, and a caller passing --profile
# explicitly never needs it. The shared conftest fixture now ships role_defaults
# (so CLI tests that omit --profile resolve through it), so this no-role-defaults
# shape lives here for the loader/resolver backward-compat cases.
CC_GLM52_NO_ROLE_DEFAULTS_YAML = """\
agent_profiles:
  cc-glm52:
    cli: claude
    backend: glm
    base_url: "https://ark.cn-beijing.volces.com/api/coding"
    auth_env: "CC_GLM52_TOKEN"
    auth_env_fallback: "ANTHROPIC_AUTH_TOKEN"
    auth_target: "ANTHROPIC_AUTH_TOKEN"
    model: "glm-5.2"
    invocation: headless
    extra_env:
      ANTHROPIC_MODEL: "glm-5.2"
"""


class TestLoadProfile:
    """Parsing agent-profiles.yml into an ``AgentProfile`` (§10.1 + §10.3)."""

    def test_parses_all_section_10_1_fields(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")

        assert isinstance(profile, AgentProfile)
        assert profile.name == "cc-glm52"
        # Every §10.1 field is carried verbatim...
        assert profile.cli == "claude"
        assert profile.backend == "glm"
        assert profile.base_url == "https://ark.cn-beijing.volces.com/api/coding"
        assert profile.auth_env == "CC_GLM52_TOKEN"
        assert profile.auth_env_fallback == "ANTHROPIC_AUTH_TOKEN"
        assert profile.auth_target == "ANTHROPIC_AUTH_TOKEN"
        assert profile.model == "glm-5.2"
        assert profile.invocation == "headless"
        assert profile.extra_env == {
            "ANTHROPIC_BASE_URL": "https://ark.cn-beijing.volces.com/api/coding",
            "ANTHROPIC_MODEL": "glm-5.2",
        }
        # ...plus the §10.3 strip pattern.
        assert profile.env_strip_pattern == (
            "^(CLAUDE_CODE_|CLAUDECODE$|AI_AGENT$|CLAUDE_EFFORT$)"
        )

    def test_optional_fields_default_to_none(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        # codex-default omits auth_env_fallback / auth_target / env_strip_pattern
        # and has null base_url / model / empty extra_env.
        write_profiles(repo_root, CODEX_DEFAULT_YAML)
        profile = load_profile(repo_root, "codex-default")

        assert profile.base_url is None
        assert profile.model is None
        assert profile.auth_env_fallback is None
        assert profile.auth_target is None
        assert profile.env_strip_pattern is None
        assert profile.extra_env == {}

    def test_extra_env_defaults_to_empty_when_absent(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        write_profiles(
            repo_root,
            "agent_profiles:\n"
            "  minimal:\n"
            "    cli: claude\n"
            "    auth_env: MINIMAL_TOKEN\n",
        )
        profile = load_profile(repo_root, "minimal")

        assert profile.extra_env == {}
        assert profile.invocation is None
        assert profile.backend is None

    def test_missing_file_fails_loud(self, repo_root: Path) -> None:
        # §24.2: no registry -> ProfileError, not an empty profile.
        with pytest.raises(ProfileError, match="agent-profiles.yml"):
            load_profile(repo_root, "cc-glm52")

    def test_malformed_yaml_fails_loud(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        # §24.2 fail loud: a corrupt registry surfaces as a ProfileError (not a
        # raw yaml traceback) so the CLI maps it to a clean non-zero exit.
        write_profiles(repo_root, "agent_profiles: {")
        with pytest.raises(ProfileError, match="not valid YAML"):
            load_profile(repo_root, "cc-glm52")

    def test_missing_profile_fails_loud(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        write_profiles(repo_root)
        with pytest.raises(ProfileError, match="cc-minimaxm3"):
            load_profile(repo_root, "cc-minimaxm3")

    def test_missing_required_field_fails_loud(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        # auth_env is the §10.2 secret-source name - a profile without it cannot
        # resolve a token, so loading must reject it (§24.2 fail loud).
        write_profiles(
            repo_root,
            "agent_profiles:\n"
            "  no-auth:\n"
            "    cli: claude\n"
            "    model: glm-5.2\n",
        )
        with pytest.raises(ProfileError, match="auth_env"):
            load_profile(repo_root, "no-auth")

    def test_missing_cli_fails_loud(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        write_profiles(
            repo_root,
            "agent_profiles:\n"
            "  no-cli:\n"
            "    auth_env: SOME_TOKEN\n",
        )
        with pytest.raises(ProfileError, match="cli"):
            load_profile(repo_root, "no-cli")

    def test_invalid_env_strip_pattern_fails_loud(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        # A bad regex would only blow up at wrapper time (ticket 03); fail it at
        # load so show-profile surfaces the misconfiguration early.
        write_profiles(
            repo_root,
            "agent_profiles:\n"
            "  bad-regex:\n"
            "    cli: claude\n"
            "    auth_env: SOME_TOKEN\n"
            '    env_strip_pattern: "^(unterminated["\n',
        )
        with pytest.raises(ProfileError, match="env_strip_pattern"):
            load_profile(repo_root, "bad-regex")


class TestTokenSourceVar:
    """§10.2 token-by-name resolution: auth_env -> fallback -> none.

    Every assertion here is on the variable *name*; the token *value* is never
    read, stored, or compared (invariant #11).
    """

    def test_prefers_auth_env_when_set(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "anything-non-empty")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "also-set-but-not-preferred")

        assert token_source_var(profile) == "CC_GLM52_TOKEN"

    def test_falls_back_when_auth_env_unset(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        # auth_env absent -> the fallback source is used.
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "fallback-token")

        assert token_source_var(profile) == "ANTHROPIC_AUTH_TOKEN"

    def test_returns_none_when_neither_set(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
    ) -> None:
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")

        assert token_source_var(profile) is None

    def test_treats_empty_string_as_unset(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # An empty var carries no credential; the prototype's `[ -n ]` test and
        # this resolver agree: empty falls through to the next source.
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "")

        assert token_source_var(profile) is None

    def test_empty_auth_env_falls_through_to_fallback(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "fallback-holds-it")

        assert token_source_var(profile) == "ANTHROPIC_AUTH_TOKEN"

    def test_returns_name_only_never_the_value(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The whole point of §10.2: the resolver hands back the NAME, so a
        # caller can never accidentally log or store the secret via this API.
        sentinel = "tok-DO-NOT-LEAK-9f3a7c"
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", sentinel)

        result = token_source_var(profile)

        assert result == "CC_GLM52_TOKEN"
        assert sentinel not in result

    def test_profile_without_fallback_returns_none_when_auth_env_unset(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # codex-default has no auth_env_fallback; with OPENAI_API_KEY unset there
        # is no fallback to try, so resolution yields None.
        write_profiles(repo_root, CODEX_DEFAULT_YAML)
        profile = load_profile(repo_root, "codex-default")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)  # hermetic

        assert token_source_var(profile) is None


class TestTokenSourceDescription:
    """``token_source_description`` - honest error/display hint (Spec finding c).

    A no-fallback profile must not be described as having a fallback; the
    description carries only names, never the value.
    """

    def test_with_fallback_names_both_sources(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
    ) -> None:
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")

        desc = profile.token_source_description()

        assert "CC_GLM52_TOKEN" in desc
        assert "ANTHROPIC_AUTH_TOKEN" in desc
        assert "fallback" in desc

    def test_without_fallback_does_not_claim_one(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_profiles(repo_root, CODEX_DEFAULT_YAML)
        profile = load_profile(repo_root, "codex-default")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        desc = profile.token_source_description()

        assert "OPENAI_API_KEY" in desc
        # codex-default declares no fallback - the hint must not invent a second
        # source var. "no fallback declared" is fine; "or fallback <var>" is not.
        assert "or fallback" not in desc
        assert "no fallback declared" in desc


class TestRenderProfile:
    """``render_profile`` is the single output path for ``show-profile``.

    Its signature takes the token source *name* (or None), never the value, so
    it cannot leak the secret by construction - these tests pin that guarantee.
    """

    def test_renders_all_fields_and_token_status(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", "some-value")

        text = render_profile(profile, token_source_var(profile))

        assert "profile: cc-glm52" in text
        assert "cli: claude" in text
        assert "auth_env: CC_GLM52_TOKEN" in text
        assert "auth_target: ANTHROPIC_AUTH_TOKEN" in text
        assert "env_strip_pattern:" in text
        assert "extra_env:" in text
        assert "token_source: CC_GLM52_TOKEN" in text
        assert "token_set: true" in text

    def test_renders_token_unset_status(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
    ) -> None:
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")

        text = render_profile(profile, token_source_var(profile))

        assert "token_source: <none>" in text
        assert "token_set: false" in text

    def test_token_value_never_appears_in_output(
        self,
        repo_root: Path,
        write_profiles: Callable[..., Path],
        clean_token_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # §10.2 / invariant #11: the rendered text must not contain the token
        # value under any branch (set, fallback, or unset).
        sentinel = "tok-DO-NOT-LEAK-render-4b8e21"
        write_profiles(repo_root)
        profile = load_profile(repo_root, "cc-glm52")
        monkeypatch.setenv("CC_GLM52_TOKEN", sentinel)

        text_set = render_profile(profile, token_source_var(profile))
        monkeypatch.delenv("CC_GLM52_TOKEN")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", sentinel)
        text_fallback = render_profile(profile, token_source_var(profile))
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN")
        text_unset = render_profile(profile, token_source_var(profile))

        assert sentinel not in text_set
        assert sentinel not in text_fallback
        assert sentinel not in text_unset
        # The redaction marker is always present so the absence of a value is
        # explicit, not a silent omission.
        for text in (text_set, text_fallback, text_unset):
            assert "<redacted" in text


# A registry carrying the v0.5 role_defaults policy (ticket 03): implementer on
# codex-default, the two checking roles on cc-glm52. Used by the role-defaults
# loader + resolver tests below.
ROLE_DEFAULTS_YAML = """\
agent_profiles:
  cc-glm52:
    cli: claude
    backend: glm
    base_url: "https://ark.cn-beijing.volces.com/api/coding"
    auth_env: "CC_GLM52_TOKEN"
    auth_env_fallback: "ANTHROPIC_AUTH_TOKEN"
    auth_target: "ANTHROPIC_AUTH_TOKEN"
    model: "glm-5.2"
    invocation: headless
    extra_env:
      ANTHROPIC_MODEL: "glm-5.2"
  codex-default:
    cli: codex
    backend: openai
    base_url: null
    auth_env: "OPENAI_API_KEY"
    model: null
    invocation: headless
    extra_env: {}
role_defaults:
  implementer: codex-default
  reviewer: cc-glm52
  spec_gap_analyst: cc-glm52
"""


class TestLoadRoleDefaults:
    """``load_role_defaults`` - the ticket-03 role -> profile-name policy table.

    A plain top-level ``role_defaults:`` mapping alongside ``agent_profiles:``;
    values are profile *names* (strings), never validated against the registry
    (no allowed-set, no enforcement - a dangling reference surfaces later as a
    ``load_profile`` ProfileError, not a policy refusal).
    """

    def test_returns_the_role_to_profile_mapping(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        write_profiles(repo_root, ROLE_DEFAULTS_YAML)

        defaults = load_role_defaults(repo_root)

        assert defaults == {
            "implementer": "codex-default",
            "reviewer": "cc-glm52",
            "spec_gap_analyst": "cc-glm52",
        }

    def test_absent_role_defaults_is_empty_not_error(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        # Backward compat: a v0.4 registry predating the field loads as {} so a
        # caller that passes --profile explicitly still works without the field.
        write_profiles(repo_root, CC_GLM52_NO_ROLE_DEFAULTS_YAML)

        assert load_role_defaults(repo_root) == {}

    def test_missing_file_fails_loud(self, repo_root: Path) -> None:
        # §24.2: no registry -> ProfileError (same surface as load_profile).
        with pytest.raises(ProfileError, match="agent-profiles.yml"):
            load_role_defaults(repo_root)

    def test_non_mapping_role_defaults_fails_loud(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        write_profiles(
            repo_root,
            "agent_profiles:\n  cc-glm52:\n    cli: claude\n    auth_env: T\n"
            "role_defaults: [not, a, mapping]\n",
        )
        with pytest.raises(ProfileError, match="role_defaults"):
            load_role_defaults(repo_root)

    def test_non_string_value_fails_loud(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        # A profile name must be a string; a non-string value is a config error
        # (§24.2 fail loud), not silently coerced.
        write_profiles(
            repo_root,
            "agent_profiles:\n  cc-glm52:\n    cli: claude\n    auth_env: T\n"
            "role_defaults:\n  implementer: 42\n",
        )
        with pytest.raises(ProfileError, match="role_defaults"):
            load_role_defaults(repo_root)


class TestResolveProfileName:
    """``resolve_profile_name`` - ticket-03 default-resolution policy.

    ``--profile`` (the override) always wins, with NO allowed-set and NO refusal
    - even an unknown name is returned as-is, deferring the not-found failure to
    ``load_profile`` (the "no constraint" boundary). When no override is given,
    the role's ``role_defaults`` entry is the default; a role with no entry and
    no override fails loud (§24.2) telling the operator exactly how to fix it.
    """

    def test_override_wins_even_when_role_default_differs(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        # implementer defaults to codex-default, but --profile cc-glm52 overrides.
        write_profiles(repo_root, ROLE_DEFAULTS_YAML)

        assert resolve_profile_name(
            repo_root, ROLE_IMPLEMENTER, override="cc-glm52"
        ) == "cc-glm52"

    def test_override_returned_verbatim_no_enforcement(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        # "no allowed-set, no refusal": an override that names a profile that does
        # not exist is returned as-is - resolve does not gate, load_profile does.
        write_profiles(repo_root, ROLE_DEFAULTS_YAML)

        assert resolve_profile_name(
            repo_root, ROLE_IMPLEMENTER, override="no-such-profile"
        ) == "no-such-profile"

    def test_role_default_used_when_no_override(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        write_profiles(repo_root, ROLE_DEFAULTS_YAML)

        assert resolve_profile_name(repo_root, ROLE_IMPLEMENTER) == "codex-default"
        assert resolve_profile_name(repo_root, ROLE_REVIEWER) == "cc-glm52"
        assert resolve_profile_name(repo_root, ROLE_SPEC_GAP_ANALYST) == "cc-glm52"

    def test_role_default_resolves_to_a_loadable_profile(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        # The resolved name must actually load - pins that role_defaults points
        # at a real registry entry (codex-default's null fields exercise this).
        write_profiles(repo_root, ROLE_DEFAULTS_YAML)

        name = resolve_profile_name(repo_root, ROLE_IMPLEMENTER)

        assert load_profile(repo_root, name).name == "codex-default"

    def test_no_override_no_role_entry_fails_loud(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        # A v0.4 registry (no role_defaults) + no --profile: the operator must be
        # told to either pass --profile or add the role to role_defaults.
        write_profiles(repo_root, CC_GLM52_NO_ROLE_DEFAULTS_YAML)

        with pytest.raises(ProfileError, match="implementer"):
            resolve_profile_name(repo_root, ROLE_IMPLEMENTER)

    def test_override_works_without_role_defaults_field(
        self, repo_root: Path, write_profiles: Callable[..., Path]
    ) -> None:
        # --profile is self-sufficient: it must not require role_defaults to exist
        # (the override path skips role_defaults entirely).
        write_profiles(repo_root, CC_GLM52_NO_ROLE_DEFAULTS_YAML)

        assert resolve_profile_name(
            repo_root, ROLE_IMPLEMENTER, override="cc-glm52"
        ) == "cc-glm52"
