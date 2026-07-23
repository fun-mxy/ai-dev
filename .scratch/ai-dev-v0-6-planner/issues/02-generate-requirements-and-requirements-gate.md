# 02 — `generate-requirements` command + `requirements_gate` (tracer bullet, real ark) (ADR-0008)

**What to build:** The first live planning gate — a complete `generate → promote → review → freeze`
vertical slice for requirements. The **Planner** role (profile `cc-glm52` via the v0.5/03
`role_defaults` policy) runs through the existing `run_wrapper`: input package = the feature intent;
the run emits an id-free requirements proposal JSON (schema from ticket 01) in `output/`. `promote`
(ticket 01) then fires **automatically** after the run, writing the canonical-unfrozen
`01-requirements.json` + rendered `.md`. **Refinement is first-class** (ADR-0008 D4 / CONTEXT.md):
`generate-requirements --feedback "…"` carries the human's note in the input package, and the
generate→promote loop repeats until the human is satisfied. The human gate is then **freeze**
(existing `status.freeze_artifact`), which advances `current_gate` from `requirements_gate` to
`design_gate`. Requirements has no upstream, so the freeze-gate coverage precheck is trivial here
(the non-trivial coverage machinery lands in 03). **This ticket carries the model de-risk** (the
v0.5/01-spike analogue): can glm-5.2 emit a schema-valid id-free proposal, or does it need retries /
fail? If the model will not honor the proposal schema, **amend ADR-0008 before 03 starts** (e.g. a
Planner-specific prompt variant) and record findings in evidence. Real cc-glm52/Ark evidence required
— see the [[e2e-tickets-need-real-ark-run]] bar; the pytest fake-claude test is not sufficient.

**Blocked by:** 01 (the promote primitive + proposal schema this command consumes).

**Status:** resolved

- [x] `generate-requirements` command: Planner role (cc-glm52 via `role_defaults`) → input package = intent → run via `run_wrapper`
- [x] run emits id-free requirements proposal JSON (ticket-01 schema) in `output/`
- [x] `promote` fires automatically after the run → canonical-unfrozen `01-requirements.json` + rendered `.md`
- [x] refinement loop: `--feedback` carried in input package; multiple generate→promote passes overwrite the unfrozen artifact
- [x] human `freeze` advances `current_gate` requirements_gate → design_gate
- [x] real cc-glm52/Ark evidence (model emits schema-valid proposal); ADR-0008 amended here if the model won't honor the schema — evidence: `.scratch/ai-dev-v0-6-planner/evidence/02-generate-requirements-real-run.md`; **no amendment needed** (glm-5.2 honors the proposal schema first-pass)
- [x] `uv run mypy` + `uv run pytest` green

## Answer

**Done.** The first live planning gate — `generate → promote → (refine) → freeze` — is wired and
verified on a real cc-glm52 / Ark backend.

- **Code:** `src/ai_dev/planner_leg.py` (the planning-leg analogue of `implement_leg`: prepare →
  run → validate → promote, gated on validation), the `generate-requirements` CLI subcommand +
  `--feedback` refinement flag + `--dry-run` planner (`src/ai_dev/cli.py`, `src/ai_dev/dry_run.py`),
  `ROLE_PLANNER` + `role_defaults[planner]=cc-glm52` (`src/ai_dev/profiles.py`,
  `examples/string-utils/.ai-dev/agent-profiles.yml`).
- **Tests:** `tests/test_planner_leg.py` (20 cases — input-package assembly, auto-promote gated on
  validation, refinement overwrite, frozen-refusal, CLI e2e with fake claude, freeze gate advance).
- **Real Ark evidence:** `.scratch/ai-dev-v0-6-planner/evidence/02-generate-requirements-real-run.md`
  — glm-5.2 emits a schema-valid id-free proposal on the **first pass** (no retries),
  `validate-run` PASS (schema + boundary + frozen), promote allocates REQ/AC ids + stitches AC refs,
  `--feedback` refinement overwrites the unfrozen artifact, `freeze` advances
  `requirements_gate → design_gate`.
- **Model de-risk:** glm-5.2 honors the ticket-01 proposal schema first-pass → **ADR-0008 needs no
  amendment** (no Planner-specific prompt variant required).
- **Quality:** `uv run mypy` clean (32 files); `uv run pytest` 853 pass.

**Open carry-forward (not this ticket):** id-counter does not reset across refinement passes
(RUN-002 continued REQ-006… from RUN-001's REQ-005) — the documented **Q7** open question, deferred
to ticket 06 (id-stability-across-refinement).
