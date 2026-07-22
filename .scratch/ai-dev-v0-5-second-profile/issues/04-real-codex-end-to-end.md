# 04 - Real codex end-to-end -> `verdict=pass` (#1 capstone)

**What to build:** The #1 capstone evidence (mirrors the v0.1/v0.4 evidence bar). Run the full happy
path - intent -> freeze -> implement -> review -> spec-gap -> verify -> collect-issues -> [triage] ->
lane-gate -> coherence-gate -> final-report - on `examples/string-utils/` (or a fresh target) with
`--profile codex-default` (real codex / OpenAI backend), reaching `verdict=pass` / `status=done`.
Capture evidence at `.scratch/ai-dev-v0-5-second-profile/evidence/04-codex-real-run.md` (isomorphic to
the v0.1/v0.2/v0.3/v0.4 `evidence/*-e2e-real-run.md`): command sequence, each gate verdict,
`final-report` summary, token-not-on-disk grep, ID continuity. Pre-flight the chain with `--dry-run`
(v0.4 ticket 04 / ADR-0004) before the real spend. A deterministic fake-codex e2e test locks the seam in
CI; this file is the genuine backend evidence. **Per the real-backend evidence discipline, this ticket
is NOT done on the fake-codex test alone.**

**Blocked by:** 02, 03.

**Status:** pending

- [ ] full happy path on real `codex-default` -> `verdict=pass` / `status=done`
- [ ] `--dry-run` pre-flighted the chain (no spawn / no state change) before the real spend
- [ ] `list-features` / `show-status` / `log` observed gate-by-gate advance
- [ ] token never persisted (`grep -rlF` -> 0 matches; env-snapshot redacted)
- [ ] IDs continuous (RUN/LANE/ISSUE); no re-issue / mis-scope
- [ ] `evidence/04-codex-real-run.md` captured
- [ ] deterministic fake-codex e2e test locks the seam in CI
