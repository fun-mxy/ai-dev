# 03 - v0.6 Planner End-to-End: real cc-glm52 / Ark run -> `generate-design` PASS + design gate (coverage precheck) -> `task_gate`

**Date:** 2026-07-23 (run timestamps UTC: generate RUN-003 07:06:23-07:08:50Z; refinement
RUN-004 07:09:47-07:12:46Z; UTC+8 local ~15:06-15:12).
**Target:** `examples/string-utils/` (the committed v0.4/v0.5/v0.6 dogfood target, re-used;
FEATURE-003 was left at `current_gate=design_gate` with requirements frozen by ticket 02 -
exactly the precondition the design leg needs).
**Profile:** `cc-glm52` (claude CLI headless; GLM provider via `ANTHROPIC_BASE_URL` ->
`glm-5.2`; ADR-0008). Resolved through `role_defaults[planner] = cc-glm52` (no `--profile`
given) - the same role policy ticket 01 extended to the Planner.
**Token:** `CC_GLM52_TOKEN` unset in this env -> resolved through the `auth_env_fallback` =
`ANTHROPIC_AUTH_TOKEN` path (token-by-env-var-name only, never persisted; env-snapshot redacts
to `<set>` - token-safety invariant #11 holds).
**Verdict:** the second live planning gate runs end-to-end on a **real** glm-5.2 / Ark backend -
`generate-design` (input package = intent + **frozen** `01-requirements.json`) -> auto-`promote`
(canonical-unfrozen `02-design.json` + rendered `.md`, DES ids allocated + `requirement_mapping`
stitched against the frozen REQ upstream - the **first exercise of the generic resolver's
`add_upstream`**) -> `--feedback` refinement (second pass overwrites the unfrozen artifact) ->
human `freeze` running the **freeze-gate coverage precheck** (every REQ in >=1
`requirement_mapping`, ADR-0008 D3 / section 18.2) advancing `current_gate`
`design_gate -> task_gate`. The coverage-**refusal** path is demonstrated on the real promoted
artifact (inject a gap -> `freeze` exits 1, gate holds). The deterministic fake-`claude` test
(`tests/test_planner_leg.py` + `tests/test_dry_run.py`) locks the prepare->run->validate->promote
->coverage->freeze seam repeatably in CI; this file is the genuine backend evidence. **Per the
[[e2e-tickets-need-real-ark-run]] bar, this ticket is NOT done on the fake-claude test alone** -
the run below is the evidence of record.

## Model de-risk (the ticket's explicit carrier) - PASS, no ADR-0008 amendment needed

The ticket carries the v0.5/01-spike analogue: *can glm-5.2 emit a schema-valid id-free design
proposal that references the frozen REQ upstream by canonical id, or does it need retries /
fail?* **Answer: it honors the design proposal schema on the first pass.** RUN-003 exited 0 and
`validate-run` returned `VALIDATE PASS - RUN-003 (schema + boundary + frozen OK)` - the section
14.1 schema check (against the role-aware `DESIGN_PROPOSAL_SCHEMA`, ticket 03 task 1), the 14.2
file-boundary check (Planner wrote only `output/result.{json,md}`), and the 14.3 frozen check all
passed with **zero retries**. The model authored genuinely id-free content (local `key` handles
for design elements + requirement-mapping entries; `requirement` refs spelled as the canonical
`REQ-006/007/008` it read from the frozen upstream) and `promote` allocated the canonical
`DES-001..003` ids and stitched every `requirement_mapping.requirement` to its frozen REQ id and
every `design_elements` local key to its allocated DES id (reference-integrity, ADR-0008 D3, held
with no `UnresolvedRefError`). **No Planner-specific prompt variant is required; ADR-0008 is
unchanged.**

## The coverage split, demonstrated live (ADR-0008 D3)

This is the ticket's structural contribution - the coverage split across the two gate actions:

* **reference-integrity at promote-time.** `build_canonical_design` registers the proposal's
  local design-element keys (`register_local`) and adds the frozen REQ upstream (`add_upstream` -
  the generic resolver's upstream path, exercised for the first time here), then `resolve`s each
  `requirement_mapping.requirement` (against the frozen REQ set) and each `design_elements` entry
  (against the allocated DES set). Every ref resolves or promote fails loud. RUN-003/RUN-004 both
  stitched cleanly: `REQ-006 -> [DES-001, DES-002]`, `REQ-007 -> [DES-001, DES-003]`,
  `REQ-008 -> [DES-001]` (RUN-003); `REQ-006 -> [DES-004, DES-005]`, `REQ-007 -> [DES-005]`,
  `REQ-008 -> [DES-005]` (RUN-004). Mapping entries carry their local `key` provenance and **no**
  section-5.2 id (requirement-mapping is not an id-allocating type) - exactly as designed.

* **coverage-completeness at freeze-time.** `freeze_gate_coverage("design", ...)` reads the frozen
  REQ set + the canonical `02-design.json` and collects every `requirement` ref across
  `requirement_mapping`; any frozen REQ not so referenced is uncovered -> the freeze refuses (no
  self-heal: back to `generate-design --feedback` or Human Triage). Demonstrated both ways below.

## How to reproduce

`.ai-dev/` is gitignored (throwaway runtime state), so the run lives at
`examples/string-utils/.ai-dev/`. The module is invoked from the repo root (the example dir is
its own uv project without `ai_dev`), `--repo-root` pointing at the target. FEATURE-003 is
already at `design_gate` with requirements frozen (ticket 02), so the design leg starts clean:

```bash
# from repo root
# 1. generate -> promote (Planner = cc-glm52 via role_defaults; auto-promote gated on validation)
uv run python -m ai_dev generate-design FEATURE-003 --repo-root examples/string-utils
# GENERATE-DESIGN PASS - RUN-003 feature=FEATURE-003 stage=design promoted=02-design.json DES=['DES-001','DES-002','DES-003']
# validate-run: VALIDATE PASS - RUN-003 (schema + boundary + frozen OK); exit_code=0; ~2m27s wall

# 2. refinement (--feedback carried in the input package; 2nd pass overwrites the unfrozen artifact)
uv run python -m ai_dev generate-design FEATURE-003 --repo-root examples/string-utils \
  --feedback "Split DES-002 (Greeting formatter) into its own pure module so the greeting
  composition is unit-testable independently of the CLI entrypoint; keep the requirement_mapping
  covering REQ-006/007/008."
# GENERATE-DESIGN PASS - RUN-004 ... DES=['DES-004','DES-005']

# 3. coverage-refusal demonstration (inject a gap on the real promoted artifact):
#    drop the REQ-008 mapping entry, then attempt freeze -> REFUSED, gate holds.
uv run python -c "import json;p='examples/string-utils/.ai-dev/features/FEATURE-003/02-design.json';d=json.load(open(p));d['requirement_mapping']=[m for m in d['requirement_mapping'] if m.get('requirement')!='REQ-008'];open(p,'w').write(json.dumps(d,indent=2))"
uv run python -m ai_dev freeze FEATURE-003 design --repo-root examples/string-utils
# error: freeze-gate coverage precheck FAILED for 'design': 1 REQ id(s) not referenced in any
# design mapping - REQ-008. Refine the proposal (generate-design --feedback) to cover them, or
# route to Human Triage (ADR-0008 D3 / section 18.2).   [exit 1; design stays unfrozen]

# 4. restore the full design + human freeze -> gate advance
#    (here: re-run generate-design, or restore the pre-edit 02-design.json)
uv run python -m ai_dev freeze FEATURE-003 design --repo-root examples/string-utils
# FEATURE-003: froze design   ->  current_gate: task_gate   [exit 0]
```

## Evidence captured

**RUN-003 (first pass, no feedback):** `validate` PASS (schema + boundary + frozen);
`exit_code=0`; `metadata.json` records `profile=cc-glm52`, `model=glm-5.2`,
`started_at=2026-07-23T07:06:23Z`, `ended_at=2026-07-23T07:08:50Z` (~2m27s wall). Promoted
`02-design.json` (`frozen=false`) with 3 design elements (`DES-001 greet CLI command`,
`DES-002 Greeting formatter`, `DES-003 Empty-name guard`) and a `requirement_mapping` covering
all three frozen REQs (`REQ-006 -> [DES-001, DES-002]`, `REQ-007 -> [DES-001, DES-003]`,
`REQ-008 -> [DES-001]`); `02-design.md` rendered. The model emitted the design elements,
architecture decision, data model, API/CLI contract, file layout, invariants, risks, and the
`requirement_mapping` (all the ticket-03 design facets) - the proposal is id-free, with
`requirement` refs spelled as the canonical `REQ-006/007/008` read from the frozen upstream and
local design-element `key`s.

**RUN-004 (refinement with `--feedback`):** the "## Human feedback (refinement - revise the
proposal accordingly)" section is present in `runs/RUN-004/input/task-package.md`, confirming the
feedback channel (ADR-0008 D4). Promote overwrote the unfrozen `02-design.json`; the model
followed the feedback - the greeting formatter was split into its own pure module (`DES-004
Greeting formatter (pure module)` + `DES-005 greet CLI command (entrypoint)`) while still
covering all three REQs. The DES id counter is monotonic across passes (RUN-004 allocated
`DES-004/005`, continuing from RUN-003's `DES-003` - ids are not reset per pass); RUN counter is
monotonic (RUN-003, RUN-004). This counter-doesn't-reset behavior is the documented **Q7** open
question (deferred to ticket 06, id-stability-across-refinement), not a defect introduced here.

**Coverage-refusal (injected gap):** with the `REQ-008` mapping entry removed from the canonical
`02-design.json`, `freeze FEATURE-003 design` exits **1** with `error: freeze-gate coverage
precheck FAILED for 'design': 1 REQ id(s) not referenced in any design mapping - REQ-008. Refine
the proposal (generate-design --feedback) to cover them, or route to Human Triage (ADR-0008 D3 /
section 18.2).` The status is unchanged (`frozen_artifacts.design=false`,
`current_gate=design_gate`) - the gate refused without self-healing, exactly as ADR-0008 D3
requires. (The gap was injected by editing the promoted artifact to exercise the gate on a real
canonical file; the deterministic refusal path is also locked by the `design_gap` fake-`claude`
variant in `tests/test_planner_leg.py::TestFreezeDesignGateAndCoverage` and the dry-run
`would be REFUSED: design coverage gap` case in `tests/test_dry_run.py::TestFreezeDryRun`.)

**Freeze:** with the full design restored, `freeze FEATURE-003 design` exits **0** ->
`status/feature-status.yml` now has `frozen_artifacts.design=true`,
`current_gate=task_gate`. The audit log records the two design `promote` events (allocated
`DES-001..003` then `DES-004/005`) and the `freeze {artifact: design}` event.

## Conclusion

All ticket-03 checkboxes are met: the `generate-design` command runs the Planner design leg
against the frozen requirements upstream (input package = intent + frozen `01-requirements.json`,
fail-loud if requirements is not frozen - ADR-0008 D2); the run emits an id-free ticket-03-schema
design proposal in `output/`; promote fires automatically after the run writing
canonical-unfrozen `02-design.{json,md}` with DES ids allocated + `requirement_mapping` stitched
against the frozen REQ upstream (the first `add_upstream` exercise, reference-integrity held);
`--feedback` carries refinement and multiple passes overwrite the unfrozen artifact; the
freeze-gate coverage precheck (every REQ in >=1 `requirement_mapping`, section 18.2) refuses on a
gap (exit 1, no self-heal) and the human `freeze` advances `design_gate -> task_gate`; real
cc-glm52/Ark evidence is captured (model emits a schema-valid design proposal on the first pass,
no ADR-0008 amendment needed); and `uv run mypy` + `uv run pytest` are green (33 source files
clean; 917 tests pass, including the new `tests/test_coverage.py`, design cases in
`tests/test_planner_leg.py`, and design dry-run/coverage cases in `tests/test_dry_run.py`).
