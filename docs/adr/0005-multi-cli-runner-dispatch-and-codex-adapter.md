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

### D3 - Codex auth via `--remote-auth-token-env <profile.auth_env>` ⚠ AMENDED by spike (ticket 01)

> **Spike amendment (2026-07-22, ticket 01):** this decision is **wrong as written**.
> `codex exec` rejects `--remote-auth-token-env` outright:
> `Error: --remote-auth-token-env is only supported for interactive TUI commands, not
> codex exec`. The flag is a **top-level** option naming the env var whose value is sent as
> a bearer token to a **remote app-server websocket** (`--remote` mode) - it is not
> OpenAI/crs model-API auth. The original decision text is retained below for audit; the
> **corrected** mechanism follows it.

The codex adapter passes `--remote-auth-token-env` with the profile's resolved token-source
var (`auth_env`, falling back to `auth_env_fallback` as today). This honors invariant #11
(token by env-var **name** only, never the value, never persisted) and reuses the existing
`auth_env` / `auth_env_fallback` / `auth_target` resolution in `profiles.py` unchanged.

**Rejected - `codex login` stored credentials**: a different auth model that persists
credentials in `~/.codex/`, straining invariant #11 and diverging from the env-var-name
contract every other profile obeys.

> **Corrected mechanism (partially spike-verified):**
> - **OpenAI provider** (`codex-default`'s declared `backend: openai`): codex reads
>   `OPENAI_API_KEY` from the child env **directly**. The adapter satisfies
>   `auth_env: OPENAI_API_KEY` by injecting the token value into that env var - the same
>   env-injection pattern the claude adapter uses for `ANTHROPIC_AUTH_TOKEN`. No flag.
>   **This path is inferred from codex's documented behavior, NOT exercised** - neither the
>   spike nor ticket 04's real e2e had a real `api.openai.com` key (`OPENAI_API_KEY` was unset
>   in both). Ticket 04's real capstone ran on the **stored-cred custom-provider path** (below)
>   and reached `verdict=pass`; the OpenAI env-injection path is unit-covered
>   (`TestCodexEnvSnapshot`) and remains **inferred, pending a real `api.openai.com` key**.
> - **Custom provider** (this dev env's `crs`, `requires_openai_auth = true`): codex uses
>   the stored `~/.codex/auth.json` credential from `codex login --with-api-key`. This is
>   the auth model the original "Rejected" paragraph dismissed - but it is codex's native
>   non-interactive path for custom providers, and **this is the path both spike runs and
>   ticket 04's real e2e used** (ticket 04 reached `verdict=pass` with `OPENAI_API_KEY` unset).
>   Ticket 03 decides the profile's provider; ticket 04 closed the real stored-cred run.
>
> Either way, **`--remote-auth-token-env` is not in the `codex exec` argv.** Invariant #11
> holds: the OpenAI path injects by name (like claude); the stored-cred path carries no
> token in profile config at all. The "Rejected - `codex login`" paragraph is
> softened: stored creds are acceptable for custom providers (a different auth model, not a
> violation of #11 since no token sits in profile config), but the env-var-name path remains
> preferred for the OpenAI provider.

### D4 - Codex sandbox = `-s workspace-write`, spawned with `cwd` = run dir ⚠ AMENDED by spike (ticket 01)

> **Spike amendment (2026-07-22, ticket 01):** the original "cwd = the run's `workspace/`
> dir" is **incompatible with the §13 contract** and is corrected to `cwd = run_dir`. The
> role prompts and `allowed-files.txt` require `output/result.json` / `output/result.md` at
> RUN-level (siblings of `workspace/`, outside it); a `workspace-write` sandbox rooted at
> `workspace/` would block those writes. The spike ran with `cwd = run_dir` (matching the
> claude wrapper) and confirmed both `output/` and `workspace/` are reachable and confined.
> Original text retained below for audit.

The codex adapter runs `codex exec -s workspace-write` with `cwd` = the run's `workspace/`
dir. The engine confines model writes to that workspace; the orchestrator's §14.2
allowed-files gate stays the fine-grained allowlist on top - **defense in depth** (engine
sandbox = coarse workspace isolation; §14.2 = declared-path allowlist).

> **Corrected mechanism (spike-verified):** `codex exec -s workspace-write` with `cwd =
> run_dir` (the `RUN-NNN` directory, **not** `workspace/`). The engine confines writes to
> `[workdir, /tmp, $TMPDIR]` (per the codex stderr banner: `sandbox: workspace-write
> [workdir, /tmp, $TMPDIR]`); the run dir holds both `output/` and `workspace/`, so the §13
> outputs and the task files are both writable and both within the §14.2 RUN-dir mtime-diff.
> The spike confirmed writes are confined to the run dir in **both** a non-git work root and
> a `git init` work root - codex's `workdir` is the cwd (run dir), **not** the git repo
> root, so the CodexRunner is safe in a git-tracked target project. Writes to `/tmp` /
> `$TMPDIR` are out-of-band harness state (same treatment as claude's
> `~/.claude/projects/` transcript per the §14.2 note) - not in the diff, not a boundary
> violation.

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

### D6 - Spike-first de-risk ✅ RESOLVED by spike (ticket 01, 2026-07-22)

The one genuine unknown is whether `codex exec` respects "write `result.json` to
`workspace/`" vs. insisting on emitting its native diff/patch. Before building the full
adapter + test suite, run a **throwaway spike**: `codex exec` on the `examples/string-utils`
dogfood target with the existing implementer prompt, confirm it writes the §13 contract at
the declared paths. This matches the `prototype/` culture (DEVELOPMENT.md: "gitignored
throwaway de-risk artifact"). The adapter is built on the spike's findings; if the spike
shows codex won't honor the prompt-written contract, D5 is revisited (a thicker adapter or
a codex-specific prompt variant) and this ADR amended.

> **Spike outcome (ticket 01, 2026-07-22):** **D5 holds - codex honors the §13
> prompt-written contract.** Two runs (`prototype/codex-spike/runs/RUN-001` non-git,
> `RUN-002` in-git-repo) reused the slugify implementer input package verbatim; both
> exited 0 with schema-valid `result.json` (§14.1), all `changed_files` within
> `allowed-files.txt` (§14.2), `result.md` written, and exit/stdout/stderr captured like
> claude's. codex writes files via its internal `apply patch` tool **and** emits a native
> `diff --git` block to stderr; the thin adapter ignores the diff and reads the files (D5).
> `--output-schema` (codex native structured output) was deliberately not used (§13.3).
>
> The spike did force **two mechanism corrections** (neither is D5, which stands):
> - **D3 amended** - `codex exec` rejects `--remote-auth-token-env` (TUI-only); auth is
>   env-injection of `OPENAI_API_KEY` (OpenAI provider) or stored `~/.codex/auth.json`
>   (custom providers).
> - **D4 amended** - `cwd = run_dir`, not `workspace/` (the latter would block
>   RUN-level `output/result.json`).
>
> Findings: `prototype/codex-spike/FINDINGS.md` (detailed) +
> `.scratch/ai-dev-v0-5-second-profile/evidence/01-spike-findings.md` (committed summary).
> The thin-adapter contract is now spike-verified, not just committed; v0.5's real-codex
> end-to-end (ticket 04, mirroring v0.1/v0.4) remains the milestone capstone that closes
> the auth-provider uncertainty (D3's stored-cred vs env-var path).
>
> **Hygiene note for ticket 02:** codex persists a session to `~/.codex/` (`session id:`
> in the stderr banner). Pass `--ephemeral` to suppress on-disk session persistence - the
> codex analogue of the claude path's `--settings autoMemoryEnabled=false` (§14.2).
> Adapter hygiene, not a contract gate.

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
  - **Update (ticket 01, 2026-07-22):** the spike **has run** and confirmed D5 (codex
    honors the prompt-written §13 contract) while amending D3/D4 (auth is not
    `--remote-auth-token-env`; cwd is `run_dir` not `workspace/`). The thin-adapter
    contract is now spike-verified. The remaining uncertainty is the auth *provider* path
    (D3's stored-cred vs env-var) - the spike exercised only the stored-cred (`crs`)
    path; the OpenAI env-injection path is inferred, pending ticket 04's real
    end-to-end.
  - **Update (ticket 04, 2026-07-22):** the real codex capstone **has run** - the full
    happy path on `codex-default` reached `verdict=pass` / `status=done`
    (`evidence/04-codex-real-run.md`). It exercised the **stored-cred `crs` path** (path b)
    end-to-end across all three agent roles (Implementer, Code Reviewer, Spec Gap Analyst -
    D5 holds for all three). The **OpenAI env-injection path** (path a) is **still
    inferred**: this env has no real `api.openai.com` key, so ticket 04 could not exercise
    it (unit-covered by `TestCodexEnvSnapshot`). Path (a) awaits a real OpenAI key to close;
    until then D3 path (a) remains inferred, path (b) is proven.
