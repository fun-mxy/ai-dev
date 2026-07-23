"""Agent-profile loader + token-by-name resolution (ticket 01, spec §10).

Loads ``.ai-dev/agent-profiles.yml`` and parses a single named profile (e.g.
``cc-glm52``) into an ``AgentProfile`` - the config snapshot the run adapter
(ticket 03) consumes to inject/strip env (§10.3) and invoke the CLI (§11).

The token is resolved **by env-var name only** (§10.2, invariant #11):
``AgentProfile`` carries the source / fallback / target *names* declared in
the profile, never a token *value*. ``token_source_var`` reports which source
name currently holds a non-empty value in the environment, so a caller can bind
the value to the injection target (``${auth_target}``) at run time without the
value ever entering this module's return values, the config file, or any log.

Two scopes, deliberately:

* ``load_profile`` parses YAML into config - it fails loud (§24.2) on a missing
  file, a missing profile name, or a missing/wrong-type required field, but it
  does **not** require the token to be present (a profile is valid config even
  before its secret is set).
* ``token_source_var`` resolves the token *source name* against the live
  environment - it returns ``None`` (not an exception) when neither source is
  set, so ``show-profile`` can report "not set" diagnostically. The run adapter
  (ticket 03) treats that ``None`` as the fail-loud trigger before spawning.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ai_dev.paths import agent_profiles_path

# Top-level registry keys in agent-profiles.yml (§10.1).
PROFILES_KEY = "agent_profiles"
# v0.5 ticket 03: the role -> profile-name policy table, a sibling of
# ``agent_profiles:``. Optional (absent => no role defaults; callers must pass
# ``--profile`` explicitly). See ``load_role_defaults`` / ``resolve_profile_name``.
ROLE_DEFAULTS_KEY = "role_defaults"

# The canonical ``role_defaults`` keys (ticket 03) - one per agent leg that
# carries a ``--profile`` default. These are stable role identifiers, distinct
# from (a) the human-readable role strings the legs pin ("Implementer" /
# "Code Reviewer" / "Spec Gap Analyst") and (b) the §15 issue ``source`` enum
# (``code_review`` / ``spec_gap``): a key here names only "which default profile
# does this leg run", so the three concerns cannot collide. ``fix-run`` resolves
# all three (one per leg); ``implement`` / ``review`` / ``spec-gap`` resolve one.
ROLE_IMPLEMENTER = "implementer"
ROLE_REVIEWER = "reviewer"
ROLE_SPEC_GAP_ANALYST = "spec_gap_analyst"
# v0.6 ticket 02: the Planner (spec §9.1) — the model role that authors the
# id-free requirements/design/tasks proposals. The ``generate-X`` legs resolve
# their profile through this key (``role_defaults[planner]``), exactly as the
# implementer/reviewer/spec-gap legs resolve theirs.
ROLE_PLANNER = "planner"


class ProfileError(ValueError):
    """Profile loading failed - missing file, missing profile, or bad config.

    Subclasses ``ValueError`` so callers may catch either. All profile
    misconfigurations surface here (§24.2 fail loud) rather than returning a
    partial/defaults-filled profile.
    """


@dataclass(frozen=True)
class AgentProfile:
    """A parsed agent profile - config fields only, no token value (§10.2/#11).

    Every §10.1 field is carried (``cli`` / ``backend`` / ``base_url`` /
    ``auth_env`` / ``auth_env_fallback`` / ``auth_target`` / ``model`` /
    ``invocation`` / ``extra_env``) plus the §10.3 ``env_strip_pattern``. Only
    ``cli`` and ``auth_env`` are required; the rest default to ``None`` (or ``{}``
    for ``extra_env``) when the profile omits them, matching the §10.1
    ``codex-default`` shape (null ``base_url`` / ``model``, empty ``extra_env``,
    no fallback / target / strip pattern).

    The token *value* is intentionally absent: ``auth_env`` /
    ``auth_env_fallback`` / ``auth_target`` are variable **names**. Resolving the
    value is the wrapper's job (ticket 03), via ``token_source_var`` + the live
    environment, so the secret never lives on this object.
    """

    name: str
    cli: str
    auth_env: str
    backend: str | None = None
    base_url: str | None = None
    auth_env_fallback: str | None = None
    auth_target: str | None = None
    model: str | None = None
    invocation: str | None = None
    extra_env: dict[str, str] = field(default_factory=dict)
    env_strip_pattern: str | None = None

    def token_source_description(self) -> str:
        """Human description of the declared token source vars.

        For error/display messages - never includes the value, only the names
        (§10.2, invariant #11). Branches on whether a fallback was declared so a
        no-fallback profile (e.g. codex-default) is not described as having one.
        """
        if self.auth_env_fallback:
            return f"{self.auth_env} (or fallback {self.auth_env_fallback})"
        return f"{self.auth_env} (no fallback declared)"


def _require_str(raw: dict[str, Any], name: str, profile: str) -> str:
    """Return ``raw[name]`` after checking it is a non-empty string.

    Required fields (``cli``, ``auth_env``) must be present and non-empty;
    anything else is a config error the loader rejects loud (§24.2).
    """
    if name not in raw:
        raise ProfileError(f"profile {profile!r} missing required field {name!r}")
    value = raw[name]
    if not isinstance(value, str) or not value:
        raise ProfileError(
            f"profile {profile!r} field {name!r} must be a non-empty string"
        )
    return value


def _optional_str(
    raw: dict[str, Any], name: str, profile: str
) -> str | None:
    """Return ``raw[name]`` if it is a string, ``None`` if absent or null.

    Optional §10.1 fields may be omitted or set to ``null`` (e.g. codex-default's
    ``base_url: null``); a present-but-non-string value is a config error.
    """
    if name not in raw or raw[name] is None:
        return None
    value = raw[name]
    if not isinstance(value, str):
        raise ProfileError(
            f"profile {profile!r} field {name!r} must be a string or null"
        )
    return value


def _parse_extra_env(
    raw: dict[str, Any], profile: str
) -> dict[str, str]:
    """Return the ``extra_env`` mapping, defaulting to empty when absent.

    Env vars are strings, so a non-dict value or a non-string key/value is a
    config error (§24.2 fail loud) rather than silently coerced.
    """
    if "extra_env" not in raw or raw["extra_env"] is None:
        return {}
    value = raw["extra_env"]
    if not isinstance(value, dict):
        raise ProfileError(
            f"profile {profile!r} field 'extra_env' must be a mapping"
        )
    parsed: dict[str, str] = {}
    for key, val in value.items():
        if not isinstance(key, str) or not isinstance(val, str):
            raise ProfileError(
                f"profile {profile!r} extra_env entries must be string -> string"
            )
        parsed[key] = val
    return parsed


def _parse_env_strip_pattern(
    raw: dict[str, Any], profile: str
) -> str | None:
    """Return the §10.3 ``env_strip_pattern`` (validated), or ``None``.

    A declared pattern must compile - a bad regex would only surface at wrapper
    time (ticket 03); validating at load lets ``show-profile`` flag it early.
    """
    pattern = _optional_str(raw, "env_strip_pattern", profile)
    if pattern is None:
        return None
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ProfileError(
            f"profile {profile!r} env_strip_pattern is not a valid regex: {exc}"
        ) from exc
    return pattern


def _load_registry_doc(repo_root: Path) -> dict[str, Any]:
    """Read + parse ``agent-profiles.yml`` into a top-level mapping (§24.2).

    Shared by ``load_profile`` and ``load_role_defaults`` so the two cannot
    drift on the missing-file / malformed-YAML / non-mapping failure surface: a
    corrupt registry surfaces once as a ``ProfileError`` (not a raw yaml
    traceback) regardless of which top-level key the caller then reads.
    """
    path = agent_profiles_path(repo_root)
    if not path.is_file():
        raise ProfileError(f"agent-profiles.yml not found at {path} (§10.1)")
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        # §24.2 fail loud: a corrupt registry surfaces as a ProfileError (not a
        # raw yaml traceback) so the CLI's catch-all maps it to a clean exit.
        raise ProfileError(
            f"agent-profiles.yml at {path} is not valid YAML: {exc}"
        ) from exc
    if not isinstance(doc, dict):
        raise ProfileError(
            f"agent-profiles.yml at {path} must be a mapping with key "
            f"{PROFILES_KEY!r}"
        )
    return doc


def load_profile(repo_root: Path, name: str) -> AgentProfile:
    """Load and parse the named profile from ``agent-profiles.yml``.

    Reads ``<repo_root>/.ai-dev/agent-profiles.yml`` (§10.1), pulls out
    ``agent_profiles[<name>]``, validates the required fields and field types,
    validates ``env_strip_pattern`` compiles, and returns an ``AgentProfile``
    snapshot. Token *values* are never touched - only the declared variable
    *names* are carried.

    Raises ``ProfileError`` (a ``ValueError``) if the file is missing, the
    registry key is absent, the named profile is unknown, a required field is
    missing/empty, an optional field has the wrong type, or ``env_strip_pattern``
    is not a valid regex (§24.2 fail loud - no silent defaults for misconfig).
    """
    doc = _load_registry_doc(repo_root)
    profiles = doc.get(PROFILES_KEY)
    if not isinstance(profiles, dict) or not profiles:
        raise ProfileError(
            f"agent-profiles.yml at {agent_profiles_path(repo_root)} has no "
            f"{PROFILES_KEY!r} mapping"
        )
    if name not in profiles:
        raise ProfileError(
            f"profile {name!r} not found in {agent_profiles_path(repo_root)}; "
            f"available: {sorted(profiles)}"
        )
    raw = profiles[name]
    if not isinstance(raw, dict):
        raise ProfileError(
            f"profile {name!r} must be a mapping, got {type(raw).__name__}"
        )

    return AgentProfile(
        name=name,
        cli=_require_str(raw, "cli", name),
        auth_env=_require_str(raw, "auth_env", name),
        backend=_optional_str(raw, "backend", name),
        base_url=_optional_str(raw, "base_url", name),
        auth_env_fallback=_optional_str(raw, "auth_env_fallback", name),
        auth_target=_optional_str(raw, "auth_target", name),
        model=_optional_str(raw, "model", name),
        invocation=_optional_str(raw, "invocation", name),
        extra_env=_parse_extra_env(raw, name),
        env_strip_pattern=_parse_env_strip_pattern(raw, name),
    )


def load_role_defaults(repo_root: Path) -> dict[str, str]:
    """Load the ``role_defaults`` role -> profile-name mapping (ticket 03).

    Returns the declared mapping, or ``{}`` when ``role_defaults`` is absent
    (backward compat with v0.4 registries that predate the field - a caller
    that passes ``--profile`` explicitly never needs it). Values are profile
    *names* and are **not** validated against the ``agent_profiles`` registry:
    ticket 03's "no allowed-set, no enforcement" boundary means a dangling
    reference is returned as-is and surfaces later as a ``load_profile``
    ``ProfileError``, not a policy refusal here.

    Raises ``ProfileError`` (§24.2 fail loud) on a present-but-non-mapping
    ``role_defaults`` or a non-string key/value - a misconfigured policy table
    is rejected rather than silently coerced.
    """
    doc = _load_registry_doc(repo_root)
    if ROLE_DEFAULTS_KEY not in doc or doc[ROLE_DEFAULTS_KEY] is None:
        return {}
    raw = doc[ROLE_DEFAULTS_KEY]
    if not isinstance(raw, dict):
        raise ProfileError(
            f"role_defaults in {agent_profiles_path(repo_root)} must be a "
            f"mapping of role -> profile-name"
        )
    parsed: dict[str, str] = {}
    for role, profile_name in raw.items():
        if not isinstance(role, str) or not isinstance(profile_name, str):
            raise ProfileError(
                f"role_defaults in {agent_profiles_path(repo_root)} entries "
                f"must be role-string -> profile-name-string"
            )
        parsed[role] = profile_name
    return parsed


def resolve_profile_name(
    repo_root: Path, role: str, override: str | None = None
) -> str:
    """Resolve the profile name for a role (ticket 03 policy).

    ``override`` (the ``--profile`` value) always wins, with **no allowed-set
    and no refusal**: even an unknown name is returned verbatim, deferring the
    not-found failure to ``load_profile`` - the "no constraint" boundary a
    future contributor must not add refusal logic to without a fitness
    justification. When ``override`` is ``None``, the role's ``role_defaults``
    entry is the default; a role with no entry and no override fails loud
    (§24.2) with an actionable message naming both fixes.

    The override path skips ``role_defaults`` entirely, so ``--profile`` is
    self-sufficient even on a registry without the field.
    """
    if override is not None:
        return override
    defaults = load_role_defaults(repo_root)
    if role not in defaults:
        raise ProfileError(
            f"no --profile given and role {role!r} has no role_defaults entry "
            f"in {agent_profiles_path(repo_root)}; either pass --profile <name> "
            f"or add '{role}: <profile>' under role_defaults (ticket 03)"
        )
    return defaults[role]


def token_source_var(profile: AgentProfile) -> str | None:
    """Return the env-var NAME holding a non-empty token, or ``None``.

    Resolution order (§10.1): the profile's own ``auth_env`` first, then
    ``auth_env_fallback`` if the primary is unset/empty. An empty string counts
    as unset (matches the prototype's ``[ -n ]`` test - an empty credential is
    no credential). Returns ``None`` when neither source is set, so callers can
    report "not set" (``show-profile``) or fail loud (the run adapter) as
    appropriate.

    Only the variable **name** is returned - the token value is read solely to
    test for non-emptiness and is never stored or returned (§10.2, invariant
    #11).
    """
    if os.environ.get(profile.auth_env):
        return profile.auth_env
    if profile.auth_env_fallback and os.environ.get(profile.auth_env_fallback):
        return profile.auth_env_fallback
    return None


def _fmt(value: str | None) -> str:
    """Render an optional field for display: the value, or ``<none>``."""
    return value if value is not None else "<none>"


def render_profile(profile: AgentProfile, token_source: str | None) -> str:
    """Render a profile as plain text for ``show-profile``.

    ``token_source`` is the NAME returned by ``token_source_var`` (or ``None``);
    the token *value* is deliberately not a parameter, so this function
    structurally cannot print it (§10.2, invariant #11) - every output path
    shows only the source name and a redacted placeholder.
    """
    # (label, value) pairs drive the scalar fields so the rendering shape stays
    # in one place; ``_fmt`` renders None as ``<none>`` uniformly.
    fields: tuple[tuple[str, str | None], ...] = (
        ("profile", profile.name),
        ("cli", profile.cli),
        ("backend", profile.backend),
        ("base_url", profile.base_url),
        ("model", profile.model),
        ("invocation", profile.invocation),
        ("auth_env", profile.auth_env),
        ("auth_env_fallback", profile.auth_env_fallback),
        ("auth_target", profile.auth_target),
        ("env_strip_pattern", profile.env_strip_pattern),
    )
    lines = [f"{label}: {_fmt(value)}" for label, value in fields]
    if profile.extra_env:
        lines.append("extra_env:")
        for key in sorted(profile.extra_env):
            lines.append(f"  {key} = {profile.extra_env[key]}")
    else:
        lines.append("extra_env: <none>")
    lines.append(f"token_source: {_fmt(token_source)}")
    lines.append(f"token_set: {'true' if token_source else 'false'}")
    lines.append("token_value: <redacted - name only, per §10.2/invariant #11>")
    return "\n".join(lines)
