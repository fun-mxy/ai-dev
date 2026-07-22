# 01 - Spike: `codex exec` on the dogfood target (de-risk ADR-0005 D6)

**What to build:** A throwaway de-risk spike (`prototype/`-style, NOT a `src/` deliverable) answering
the one genuine unknown from ADR-0005 D6: does `codex exec` honor "write `result.json`/`result.md` at
the declared workspace paths" when given the existing implementer prompt, or does it insist on emitting
its native diff/patch? Run `codex exec` (the `codex-default` shape from spec §10.1: `cli: codex`,
`auth_env: OPENAI_API_KEY`; per ADR-0005 D3 auth via `--remote-auth-token-env`, D4 `-s workspace-write`,
cwd = a run workspace) against `examples/string-utils/` with the existing implementer input package
(the slugify feature, or a fresh tiny intent). Confirm: (a) codex writes `result.json`/`result.md` at the
declared paths with the §13 schema; (b) `changed_files` land in `workspace/`; (c) exit code +
stdout/stderr are capturable the way claude's are. Record findings in `prototype/` (or
`.scratch/ai-dev-v0-5-second-profile/evidence/01-spike-findings.md`). **If codex will NOT honor the
prompt-written contract**, ADR-0005 D5 revisits (thicker adapter translating codex's native diff, or a
codex-specific prompt variant) - surface that in findings and amend ADR-0005 before ticket 02 starts.

**Blocked by:** none - start immediately (parallel with 05, 07).

**Status:** done (2026-07-22) - spike PASS; D5 holds; ADR-0005 D3 + D4 amended. Evidence:
`prototype/codex-spike/FINDINGS.md` (detailed) +
`.scratch/ai-dev-v0-5-second-profile/evidence/01-spike-findings.md` (committed summary).

- [x] `codex exec` runs non-interactively on the dogfood target with `workspace-write`
      (auth via stored `~/.codex/auth.json` on the `crs` custom-provider path - the path
      both runs exercised. `--remote-auth-token-env` is rejected by `codex exec`, D3
      amended. The OpenAI-provider `OPENAI_API_KEY` env-injection path is **inferred, not
      exercised** here - deferred to ticket 04)
- [x] confirms codex writes `result.json`/`result.md` at declared paths (§13 contract) -
      schema-valid, both runs (RUN-001 non-git, RUN-002 in-git-repo); D5 holds, no revision needed
- [x] `changed_files` land in `workspace/`; exit/stdout/stderr capturable like claude's
- [x] findings recorded (`prototype/codex-spike/FINDINGS.md` + `evidence/01-spike-findings.md`)
- [x] ADR-0005 amended before 02 starts (D3 + D4 mechanism corrections; D5 unchanged)
