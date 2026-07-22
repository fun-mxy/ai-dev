# 02 - `AgentRunner` dispatch + `CodexRunner` adapter (ADR-0005)

**What to build:** Refactor `src/ai_dev/run_wrapper.py` from claude-only to dispatch on `profile.cli`
per ADR-0005. Introduce an `AgentRunner` interface + `ClaudeRunner` (current behavior, extracted) +
`CodexRunner` + a registry keyed by `profile.cli` (D1). Each adapter owns: child-env build
(strip + inject), argv build, stdout/stderr/exit capture, `changed_files` computation. `run_wrapper`
becomes a thin dispatcher that resolves the adapter from `profile.cli` and delegates. `CodexRunner`:
`codex exec <prompt> --remote-auth-token-env <auth_env> -s workspace-write [-m <model>]` (D2/D3/D4),
cwd = run workspace; thin adapter - reuse the role prompts, **no output translation** (D5). Dispatch on
`cli` not `backend` (D2 - `cc-minimaxm3`/`cc-deepseekv4pro` are `cli: claude` and must use
`ClaudeRunner`). Keep the claude path behavior-identical (extract, don't rewrite) so the existing
claude tests stay green. Tests at the public seam (`run_wrapper` run) for both adapters.

**Blocked by:** 01 (spike confirms codex honors the §13 contract - avoids D5 rework).

> **Spike findings (ticket 01, 2026-07-22) - READ BEFORE IMPLEMENTING:** the spike verified
> D5 (codex honors the §13 prompt-written contract - no output translation) AND amended two
> mechanisms in ADR-0005. The `CodexRunner` argv below must follow the **amended** ADR, not
> the original checklist text:
> - **D3 amended:** do **NOT** pass `--remote-auth-token-env` - `codex exec` rejects it
>   (`Error: ... only supported for interactive TUI commands, not codex exec`). For the
>   OpenAI provider, inject `OPENAI_API_KEY` into the child env (same pattern claude uses
>   for `ANTHROPIC_AUTH_TOKEN`); for custom providers, codex uses stored `~/.codex/auth.json`.
> - **D4 amended:** `cwd = run_dir` (the `RUN-NNN` directory), **not** `workspace/`. The
>   §13 contract needs `output/result.json` at RUN-level; rooting the sandbox at
>   `workspace/` would block it. `-s workspace-write` confines writes to
>   `[workdir, /tmp, /tmp]` and both `output/` + `workspace/` are reachable.
> - **Hygiene:** pass `--ephemeral` (suppress `~/.codex/` session persistence; codex
>   analogue of claude's `--settings autoMemoryEnabled=false`, §14.2) and `--skip-git-repo-check`
>   + `--color never` for clean capture. Prompt via stdin (`codex exec -`).
> - Verified argv shape: `codex exec - -s workspace-write --skip-git-repo-check --color never
>   --ephemeral [-m <model>]` (cwd = run_dir, prompt on stdin).
> See `prototype/codex-spike/FINDINGS.md` + `.scratch/ai-dev-v0-5-second-profile/evidence/01-spike-findings.md`.

**Status:** pending

- [ ] `AgentRunner` interface + registry keyed by `profile.cli` (D1)
- [ ] `ClaudeRunner` extracted from current `run_wrapper` (behavior-identical; existing tests green)
- [ ] `CodexRunner`: `codex exec`, `--remote-auth-token-env <auth_env>`, `-s workspace-write`, `-m` if model set, cwd=workspace (D2/D3/D4)
- [ ] thin adapter: reuses role prompts, no output translation (D5)
- [ ] dispatch on `cli` not `backend` (D2)
- [ ] unit tests for both adapters at the `run_wrapper` seam
- [ ] `uv run mypy` + `uv run pytest` green
