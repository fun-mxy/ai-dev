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

**Status:** pending

- [ ] `codex-default` profile entry in `agent-profiles.yml` (§10.1 shape) + template
- [ ] `role_defaults:` mapping in `agent-profiles.yml`
- [ ] hardcoded `cc-glm52` default removed from `cli.py`; default resolved from `role_defaults` by role
- [ ] `--profile` always overrides; no allowed-set, no refusal logic (boundary noted)
- [ ] `fix-run` uses per-leg role defaults
- [ ] `show-profile` resolves `codex-default`; tests
- [ ] `uv run mypy` + `uv run pytest` green
