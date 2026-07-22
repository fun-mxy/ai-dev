# ADR-0005: Multi-CLI agent-runner dispatch (strategy per `profile.cli`) + codex adapter contract

- **Status:** Accepted
- **Date:** 2026-07-22
- **Supersedes / amends:** amends spec §27.1 (future roadmap "add second profile") by promoting it to a committed v0.5 milestone and locking the mechanism. Relies on §10 (Agent Profile), §12/§13 (run/output contract), §14.2 (allowed-files), §10.2 / invariant #11 (token by env-var name only). No earlier ADR is amended; ADR-0001's `origin` audit tag gains two new leg origins only notionally (the codex legs reuse the existing `implement-leg` / `review-leg` / `spec-gap-leg` origins).

## Context

v0.0-v0.4 ran exactly one profile (`cc-glm52`) because `run_wrapper.py` is claude-only.
`_resolve_claude()` is the sole binary resolver and the spawn is hard-coded
`[_resolve_claude(...), "-p", prompt, *flags]` with `ANTHROPIC_*` env and a `--settings`
file. The profile schema already carried a `cli` field (and `profiles.py` even named the
`codex-default` shape), but `run_wrapper` recorded `profile.cli` in metadata and **never
dispatched on it** - it ran claude regardless of `cli`.

§27.1 asks for a second profile (`codex-default`). Adding it requires making `profile.cli`
actually dispatch, with per-CLI adapters, because claude and codex differ *structurally*,
not parametrically: claude takes a `--settings` file + `ANTHROPIC_MODEL` env + no engine
sandbox; codex takes `-c key=value` config overrides + a `-m/--model` flag + a
`-s/--sandbox` mode + `--remote-auth-token-env` + prompt-as-arg-or-stdin. A config table
cannot paper over those structural differences cleanly.

## Decisions

### D1 - Dispatch shape: strategy/adapter per CLI

An `AgentRunner` interface + `ClaudeRunner` / `CodexRunner` implementations + a registry
keyed by `profile.cli`. Each adapter owns: child-env build (strip + inject), argv build
(binary, flags, settings/config, sandbox, prompt), stdout/stderr/exit capture, and
`changed_files` computation. `run_wrapper` becomes a thin dispatcher that resolves the
adapter from `profile.cli` and delegates.

**Rejected - config-driven table** (one wrapper, all variation from a `cli -> {binary,
flags, auth_target, env_strip, prompt_mode}` map): claude vs codex differ structurally
(settings file vs `-c` config, sandbox flag, prompt arg vs stdin), so the table grows
gnarly fast and re-implements a strategy pattern badly.

**Rejected - inline `if profile.cli == "codex"` fork**: gets codex running fastest but
doesn't scale to the 3rd+ profiles §27 forecasts (v0.3+ multi-profile reviewer panel,
profile capability benchmark) and rots - exactly the fork those later profiles would force
a rewrite of.

### D2 - Registry key = `profile.cli`, not `profile.backend`

The adapter owns the **invocation contract** (binary, flags, settings/config, sandbox,
prompt mode), which is per-CLI-*tool*. `backend` / `auth_env` / `base_url` / `model` are
**env-injection** concerns handled uniformly from profile fields: the claude adapter maps
`base_url`/`model`/`auth_target` -> `ANTHROPIC_*`; the codex adapter maps `model` -> `-m`
and `auth_env` -> `--remote-auth-token-env`. So dispatch is on `cli`; profile fields feed
env/argv uniformly. (This is why `cc-glm52` is `cli: claude` + `backend: glm` - the claude
CLI talking to a glm backend - and the adapter is oblivious to `backend`.)

### D3 - Codex auth via `--remote-auth-token-env <profile.auth_env>`

The codex adapter passes `--remote-auth-token-env` with the profile's resolved token-source
var (`auth_env`, falling back to `auth_env_fallback` as today). This honors invariant #11
(token by env-var **name** only, never the value, never persisted) and reuses the existing
`auth_env` / `auth_env_fallback` / `auth_target` resolution in `profiles.py` unchanged.

**Rejected - `codex login` stored credentials**: a different auth model that persists
credentials in `~/.codex/`, straining invariant #11 and diverging from the env-var-name
contract every other profile obeys.

### D4 - Codex sandbox = `-s workspace-write`, spawned with `cwd` = run workspace

The codex adapter runs `codex exec -s workspace-write` with `cwd` = the run's `workspace/`
dir. The engine confines model writes to that workspace; the orchestrator's §14.2
allowed-files gate stays the fine-grained allowlist on top - **defense in depth** (engine
sandbox = coarse workspace isolation; §14.2 = declared-path allowlist).

**Rejected - `--dangerously-bypass-approvals-and-sandbox`** (no engine sandbox, mirroring
the claude path which has none today): loses codex's defense-in-depth and hands a headless
agent full write access for no gain.

**Deliberate asymmetry, accepted:** the claude path has *no* engine sandbox today and this
ADR does **not** retro-fit one. codex gets `workspace-write` because codex offers it cheaply
and the implementer leg writes code; §14.2 remains the common floor both engines pass
through. Retrofitting a claude sandbox is out of scope and would be a separate ADR.

### D5 - Thin adapter, uniform §13 contract via prompt

The adapter only **invokes + captures** (stdout/stderr/exit_code) and computes
`changed_files`; it **never translates** the agent's native output into `result.json`. The
role prompts (implementer / reviewer / spec-gap) instruct the agent to write
`result.json` / `result.md` at the declared workspace paths, exactly as claude does today;
§14 validates that contract engine-agnostically.

**Rejected - thick adapter translating codex's native diff/patch into `result.json`**:
couples §14's trust boundary to adapter correctness and creates a second code path the
gate must reason about. The uniform-contract path keeps one contract, enforced once.

### D6 - Spike-first de-risk

The one genuine unknown is whether `codex exec` respects "write `result.json` to
`workspace/`" vs. insisting on emitting its native diff/patch. Before building the full
adapter + test suite, run a **throwaway spike**: `codex exec` on the `examples/string-utils`
dogfood target with the existing implementer prompt, confirm it writes the §13 contract at
the declared paths. This matches the `prototype/` culture (DEVELOPMENT.md: "gitignored
throwaway de-risk artifact"). The adapter is built on the spike's findings; if the spike
shows codex won't honor the prompt-written contract, D5 is revisited (a thicker adapter or
a codex-specific prompt variant) and this ADR amended.

## Consequences

- A 3rd+ profile (§27.3 multi-profile reviewer panel, profile benchmark) becomes one
  `AgentRunner` impl + one `agent-profiles.yml` entry - no `run_wrapper` surgery.
- §14.2 validation stays engine-agnostic; the §13 output contract is enforced uniformly
  regardless of which CLI produced it.
- codex runs with an engine sandbox claude lacks - a deliberate, documented asymmetry; §14.2
  is the common floor.
- The word "profile" is now ambiguous in-repo (codex's `-p/--profile` config layer vs. the
  orchestrator's Agent Profile); `docs/glossary.md` pins the disambiguation (Agent Profile
  = the orchestrator concept; codex's flag is an engine-internal config layer, never called
  "profile" unqualified).
- The spike (D6) may force a revision of D5; until it runs, the thin-adapter contract is
  the committed direction, not a proven one. v0.5's real-codex end-to-end evidence (the
  milestone capstone, mirroring v0.1/v0.4) is what closes that uncertainty.
