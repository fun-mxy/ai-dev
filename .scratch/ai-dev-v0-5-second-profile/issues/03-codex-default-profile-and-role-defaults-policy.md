# 03 - `codex-default` profile + `role_defaults` policy

**What to build:** Land the `codex-default` profile entry (spec §10.1 shape: `cli: codex`,
`backend: openai`, `base_url: null`, `auth_env: OPENAI_API_KEY`, `model: null`, `invocation: headless`,
`extra_env: {}`) in `examples/string-utils/.ai-dev/agent-profiles.yml` and the canonical profile
template. Add `role_defaults:` to `agent-profiles.yml` (e.g. `{implementer: codex-default,
reviewer: cc-glm52, spec_gap_analyst: cc-glm52}`) per the #2 policy decision. **Remove the hardcoded
`cc-glm52` default** from `cli.py` (the `implement`/`review`/`spec-gap`/`fix-run` `--profile` defaults)
and resolve the default from `role_defaults` by role instead. `--profile` always overrides; **no
allowed-set, no enforcement** (the "no constraint" boundary - record the rationale in this ticket so a
future contributor doesn't add refusal logic without a fitness justification). `fix-run` uses each
leg's role default.

**Blocked by:** 02 (`CodexRunner` must exist to run `codex-default`; default resolution needs the dispatch).

**Status:** done (2026-07-22) - `codex-default` + `role_defaults` landed in the example registry and
spec §10.1; `profiles.py` gains `load_role_defaults` + `resolve_profile_name`; `cli.py` resolves the
`--profile` default by role for `implement`/`review`/`spec-gap`/`fix-run` (override always wins);
`run_fix_run` + `plan_fix_run` take three profiles (per-leg). `uv run mypy` clean (27 files);
`uv run pytest` 724 passed (+18 new). Real `codex exec` end-to-end is ticket 04.

**Decisions / rationale (read before extending):**

- **"No constraint" boundary (the #2 policy).** `resolve_profile_name` returns the `--profile`
  override **verbatim**, even when it names a profile that does not exist - it does not gate against
  an allowed-set and does not refuse. A dangling reference surfaces later as a `load_profile`
  `ProfileError` (the same fail-loud path every other profile misconfig takes), not a policy refusal.
  **Do not add allowed-set / refusal logic here without a fitness justification** (a measured reason
  the orchestrator should second-guess the operator's explicit `--profile` choice). The boundary is
  structural: `resolve_profile_name` only *names*; `load_profile` only *loads*; neither *judges*.
- **Role keys.** `role_defaults` keys are `implementer` / `reviewer` / `spec_gap_analyst` (constants
  in `profiles.py`). Deliberately distinct from (a) the human-readable role strings the legs pin
  (`"Implementer"` / `"Code Reviewer"` / `"Spec Gap Analyst"`) and (b) the §15 issue `source` enum
  (`code_review` / `spec_gap`) - a key here names only "which default profile does this leg run", so
  the three concerns cannot collide.
- **`role_defaults` is optional.** Absent => `{}` (backward compat with v0.4 registries). A caller
  passing `--profile` explicitly never needs it (the override path skips `role_defaults` entirely).
  Omitting both `--profile` and the role's entry fails loud (§24.2) with an actionable message naming
  both fixes.
- **`fix-run` per-leg.** `run_fix_run` / `plan_fix_run` take three profiles (`implement` / `reviewer`
  / `spec_gap`), one per leg - the implementer runs on `codex-default`, the two checking roles on
  `cc-glm52`. `--profile`, when given, is applied to all three legs (a single override covers the
  whole chain); when omitted, each leg resolves its own role default.
- **`run-headless` is intentionally NOT changed.** The ticket names exactly `implement`/`review`/
  `spec-gap`/`fix-run`; `run-headless` is a low-level primitive with no role concept (the role is
  assigned at `prepare-run` time and stored on the run), so it keeps its `--profile` default of
  `cc-glm52` (§23.4 v0 recommended profile). Retrofitting role-based resolution onto `run-headless`
  would mean reading the run's role back from metadata - out of scope for this ticket.

- [x] `codex-default` profile entry in `agent-profiles.yml` (§10.1 shape) + template
- [x] `role_defaults:` mapping in `agent-profiles.yml`
- [x] hardcoded `cc-glm52` default removed from `cli.py`; default resolved from `role_defaults` by role
- [x] `--profile` always overrides; no allowed-set, no refusal logic (boundary noted)
- [x] `fix-run` uses per-leg role defaults
- [x] `show-profile` resolves `codex-default`; tests
- [x] `uv run mypy` + `uv run pytest` green
