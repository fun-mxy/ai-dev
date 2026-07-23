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

**Status:** ready-for-agent

- [ ] `generate-requirements` command: Planner role (cc-glm52 via `role_defaults`) → input package = intent → run via `run_wrapper`
- [ ] run emits id-free requirements proposal JSON (ticket-01 schema) in `output/`
- [ ] `promote` fires automatically after the run → canonical-unfrozen `01-requirements.json` + rendered `.md`
- [ ] refinement loop: `--feedback` carried in input package; multiple generate→promote passes overwrite the unfrozen artifact
- [ ] human `freeze` advances `current_gate` requirements_gate → design_gate
- [ ] real cc-glm52/Ark evidence (model emits schema-valid proposal); ADR-0008 amended here if the model won't honor the schema
- [ ] `uv run mypy` + `uv run pytest` green
