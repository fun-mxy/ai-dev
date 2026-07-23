# 02 - v0.6 Planner End-to-End: real cc-glm52 / Ark run → `generate-requirements` PASS + gate advance

**Date:** 2026-07-23 (run timestamps UTC: generate RUN-001 05:52:47–05:56:38Z; refinement
RUN-002 ~05:57–06:01Z; UTC+8 local ~13:52–14:01).
**Target:** `examples/string-utils/` (the committed v0.4/v0.5 dogfood target, re-used).
**Profile:** `cc-glm52` (claude CLI headless; GLM provider via `ANTHROPIC_BASE_URL` →
`glm-5.2`; ADR-0008). Resolved through `role_defaults[planner] = cc-glm52` (no `--profile`
given) — the v0.5/03 role policy, now extended to the Planner role (ticket 01).
**Token:** `CC_GLM52_TOKEN` unset in this env → resolved through the `auth_env_fallback` =
`ANTHROPIC_AUTH_TOKEN` path (token-by-env-var-name only, never persisted; env-snapshot redacts
to `<set>` — token-safety invariant #11 holds).
**Verdict:** the first live planning gate runs end-to-end on a **real** glm-5.2 / Ark backend —
`generate-requirements` → auto-`promote` (canonical-unfrozen `01-requirements.json` + rendered
`.md`) → `--feedback` refinement (second pass overwrites the unfrozen artifact) → human `freeze`
advancing `current_gate` `requirements_gate → design_gate`. The deterministic fake-`claude` test
(`tests/test_planner_leg.py`) locks the prepare→run→validate→promote→freeze seam repeatably in
CI; this file is the genuine backend evidence. **Per the [[e2e-tickets-need-real-ark-run]] bar,
this ticket is NOT done on the fake-claude test alone** — the run below is the evidence of record.

## Model de-risk (the ticket's explicit carrier) — PASS, no ADR-0008 amendment needed

The ticket carries the v0.5/01-spike analogue: *can glm-5.2 emit a schema-valid id-free
requirements proposal, or does it need retries / fail?* **Answer: it honors the proposal schema
on the first pass.** RUN-001 exited 0 and `validate-run` returned `VALIDATE PASS - RUN-001
(schema + boundary + frozen OK)` — the §14.1 schema check (against the role-aware
`REQUIREMENTS_PROPOSAL_SCHEMA`, ticket 01), the §14.2 file-boundary check (Planner wrote only
`output/result.{json,md}`, both in the allowed-files seed), and the §14.3 frozen check all
passed with **zero retries**. The model authored genuinely id-free content (local `key` handles
`r1..r5` / `ac1..ac7`, local `requirement` refs) and `promote` allocated the canonical
`REQ-001..005` / `AC-001..007` ids and stitched every AC's `requirement` to its allocated REQ id
(reference-integrity, ADR-0008 D3, held with no `UnresolvedRefError`). **No Planner-specific
prompt variant is required; ADR-0008 is unchanged.**

## The feature (fresh dogfood intent)

> Add a `greet(name)` CLI that prints `Hello, <name>!` and exits 0; reject empty names with exit 1.

A fresh `FEATURE-003` (the example repo's existing FEATURE-001/002 are frozen v0.4/v0.5 runs;
reusing a frozen-requirements feature would trip promote's §4.2 frozen-refusal, so a clean
feature was created). Requirements is the root artifact (no upstream), so the freeze-gate
coverage precheck is trivial here; the non-trivial coverage machinery lands in ticket 03.

## How to reproduce

`.ai-dev/` is gitignored (throwaway runtime state, v0.4 ticket 05), so the run lives at
`examples/string-utils/.ai-dev/`. `examples/string-utils/.ai-dev/agent-profiles.yml` carries
`cc-glm52` + the ticket-01 `role_defaults` table (`planner: cc-glm52`). The module is invoked
from the repo root (the example dir is its own uv project without `ai_dev`), `--repo-root`
pointing at the target:

```bash
# from repo root
uv run python -m ai_dev create-feature-run "Add a greet(name) CLI ..." --repo-root examples/string-utils
# → FEATURE-003, current_gate=requirements_gate

# 1. generate → promote (Planner = cc-glm52 via role_defaults; auto-promote gated on validation)
uv run python -m ai_dev generate-requirements FEATURE-003 --repo-root examples/string-utils
# GENERATE-REQUIREMENTS PASS - RUN-001 ... REQ=['REQ-001'..'REQ-005'] AC=['AC-001'..'AC-007']
# validate-run: VALIDATE PASS - RUN-001 (schema + boundary + frozen OK); exit_code=0; ~3m51s wall

# 2. refinement (--feedback carried in the input package; 2nd pass overwrites the unfrozen artifact)
uv run python -m ai_dev generate-requirements FEATURE-003 --repo-root examples/string-utils \
  --feedback "Add a non-functional requirement: invocable as python -m string_utils.greet ..."
# GENERATE-REQUIREMENTS PASS - RUN-002 ... REQ=['REQ-006','REQ-007','REQ-008'] AC=['AC-008'..'AC-011']

# 3. human freeze → gate advance
uv run python -m ai_dev freeze FEATURE-003 requirements --repo-root examples/string-utils
# FEATURE-003: froze requirements  →  current_gate: design_gate
```

## Evidence captured

**RUN-001 (first pass, no feedback):** `validate` PASS (schema + boundary + frozen); `exit_code=0`;
`metadata.json` records `profile=cc-glm52`, `model=glm-5.2`, `started_at=2026-07-23T05:52:47Z`,
`ended_at=2026-07-23T05:56:38Z` (~3m51s wall). Promoted `01-requirements.json` (`frozen=false`)
with 5 REQs / 7 ACs, all AC `requirement` refs resolved to allocated REQ ids; `01-requirements.md`
rendered. The model also emitted `scope` / `constraints` / `open_questions` (optional facets) and
surfaced 3 genuine clarifying questions (greeting exact format, whitespace-as-empty, stream
usage) — the proposal is *deliberately incomplete-while-refined* (ADR-0008 D4), exactly as the
task text instructs.

**RUN-002 (refinement with `--feedback`):** the "## Human feedback (refinement — revise the
proposal accordingly)" section is present in `runs/RUN-002/input/task-package.md`, confirming the
feedback channel. Promote overwrote the unfrozen `01-requirements.json`; the REQ/AC id counters
are monotonic across passes (RUN-002 allocated `REQ-006..008` / `AC-008..011`, continuing from
RUN-001's `REQ-005` / `AC-007` — ids are not reset per pass). This counter-doesn't-reset behavior
is the documented **Q7 open question** (deferred to ticket 06, id-stability-across-refinement),
not a defect introduced here.

**Freeze:** `freeze FEATURE-003 requirements` → `status/feature-status.yml` now has
`frozen_artifacts.requirements=true`, `current_gate=design_gate`.

## Conclusion

All seven ticket-02 checkboxes are met: the `generate-requirements` command resolves the Planner
role via `role_defaults`, the run emits an id-free ticket-01-schema proposal in `output/`, promote
fires automatically after the run writing canonical-unfrozen `01-requirements.{json,md}`,
`--feedback` carries refinement and multiple passes overwrite the unfrozen artifact, human `freeze`
advances `requirements_gate → design_gate`, real cc-glm52/Ark evidence is captured (model emits a
schema-valid proposal, no ADR-0008 amendment needed), and `uv run mypy` + `uv run pytest` are green
(32 source files clean; 853 tests pass, including the 20 new `tests/test_planner_leg.py` cases).
