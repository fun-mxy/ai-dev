# Ticket 03 — `generate-design` + `design_gate` with REQ-coverage freeze precheck

## Goal
Second planning gate. Planner (cc-glm52) runs with input = intent + **frozen** `01-requirements.json`,
emits an id-free design proposal; `promote` allocates DES ids + **stitches `requirement_mapping`
against the frozen requirements** (first live use of the generic resolver's `add_upstream` path);
human `freeze` advances `design_gate -> task_gate`. This ticket also introduces the
**freeze-gate coverage precheck** as a reusable helper: at design freeze, every REQ must appear in
≥1 `requirement_mapping` (§18.2) — a gap **refuses to freeze**, no self-heal.

Mirrors ticket 02's structure exactly; the only genuinely new machinery is (a) frozen-upstream
stitching in promote and (b) the coverage helper + freeze gating.

## Architecture decisions

1. **Frozen-upstream precondition.** Design may only stitch against a *frozen* requirements
   artifact (ADR-0008 D2). Enforce fail-loud: `build_design_input_package` and `promote_design`
   both refuse if `requirements` is not frozen (authoritative source =
   `status.frozen_artifacts_status`, **not** the doc's `frozen` field — which promote always
   writes `False`; freeze never touches the doc).

2. **Design refs to REQs are canonical ids.** The model reads frozen `01-requirements.json`
   (embedded in the task text) and references REQs by their canonical `REQ-NNN` id in
   `requirement_mapping`. promote calls `resolver.add_upstream("REQ", frozen_req_ids)` then
   `resolve("REQ", ref)` — a ref to a non-existent REQ fails loud (reference-integrity, D3). This
   is the "generic resolver against a real frozen upstream, for the first time". DES intra-proposal
   refs use local `key` handles (register_local), exactly as AC→REQ did in requirements.

3. **Coverage precheck lives in a new `src/ai_dev/coverage.py`** (reusable — ticket 04 adds a tasks
   variant). `freeze_gate_coverage(artifact, feature_root) -> CoverageResult | None` dispatches:
   `design` → `design_coverage`; `requirements`/`tasks`/`lane_graph` → `None` (tasks wired in 04).

4. **Coverage gates the freeze at the CLI layer** (`_run_freeze`), NOT inside `freeze_artifact`.
   `freeze_artifact` is a pure low-level writer (no artifact-reading dependency); the CLI is the
   sole production freeze path and the natural home for gate-stage policy. On a coverage gap,
   `_run_freeze` prints the uncovered REQs and returns 1 **without** calling `freeze_artifact`.
   `plan_freeze` dry-run is extended to report whether the coverage precheck would pass.

## Files

### New / changed source
- **`planner_schemas.py`**: add `DESIGN_PROPOSAL_SCHEMA` (+ `_DESIGN_ELEMENT_ITEM_SCHEMA`,
  `_REQUIREMENT_MAPPING_ITEM_SCHEMA`) and register `design` in `_PLANNER_PROPOSAL_SCHEMAS`.
  Schema = id-free: `design_elements[]` (required `key`+`name`), `requirement_mapping[]`
  (required `key`+`requirement`+`design_elements`), optional `architecture_decision` / `data_model`
  / `api_cli_contract` / `file_layout` / `invariants` / `risks` / `dependencies`. No `minItems`
  (proposal expected-incomplete while refined — D3).
- **`promote.py`**: add `DESIGN_ARTIFACT`, `read_frozen_requirements_doc(feature_root)` (fail-loud
  if missing/unfrozen), `build_canonical_design` (allocate DES ids, add_upstream REQ from frozen
  doc, resolve mapping `requirement` + `design_elements`), `render_design_md`, `promote_design`
  (frozen guard on `design` + reuse `RefResolver` + write `02-design.{json,md}` + audit).
  Reuse `_entries`/`_required_str`/`RefResolver`/`_render_inline` helpers already there.
- **`planner_leg.py`**: add `_STAGE_DESIGN`, `read_frozen_requirements_doc` re-use,
  `_design_task_text(feature_id, intent, req_summary, feedback)`,
  `build_design_input_package`, `run_generate_design` (mirrors `run_generate_requirements`;
  stage="design"). Reuse `PlannerLegResult`.
- **`coverage.py` (new)**: `CoverageResult` dataclass (`artifact`, `upstream_type`, `covered`,
  `uncovered`, `ok`), `design_coverage(feature_root)`, `freeze_gate_coverage(artifact, root)`.
- **`cli.py`**: add `generate-design` subparser + `_run_generate_design` + dispatch (mirror
  requirements, record planner profile); extend `_run_freeze` to run `freeze_gate_coverage` and
  refuse on gaps.
- **`dry_run.py`**: add `plan_generate_design` (mirror `plan_generate_requirements`); extend
  `plan_freeze` to surface the coverage precheck result for `design`.

### Tests (TDD at the promote + coverage seams, then leg/CLI)
- **`tests/test_promote.py`**: `TestBuildCanonicalDesign`, `TestRenderDesignMd`,
  `TestPromoteDesign` (allocates DES ids, stitches mapping REQ refs against frozen upstream,
  refuses if requirements not frozen, refuses over frozen design, re-promote reallocates,
  malformed-proposal fail-loud, unresolvable REQ/DES ref writes nothing).
- **`tests/test_coverage.py` (new)**: `design_coverage` passes when all REQs mapped; detects
  uncovered REQs; ignores non-REQ refs; `freeze_gate_coverage` returns None for non-design.
- **`tests/test_planner_leg.py`**: design-leg seams — input package carries intent + frozen REQs +
  feedback; role pinned; design proposal schema; auto-promote gated on validation; refinement
  overwrite; frozen-requirements precondition; frozen-design refusal; CLI e2e (fake claude);
  freeze `design_gate -> task_gate`; **freeze refused on a coverage gap** (the ticket's headline
  invariant).

### Real-run evidence
- `.scratch/ai-dev-v0-6-planner/evidence/03-generate-design-real-run.md`: continue the
  `examples/string-utils` dogfood at FEATURE-003 (currently `design_gate`, requirements frozen:
  REQ-006/007/008, AC-008..011). Run `generate-design` (→ `02-design.{json,md}` with stitched
  `requirement_mapping`), `--feedback` refinement, then `freeze design` (coverage precheck passes →
  `task_gate`). Also demonstrate the **refusal** path (a design proposal missing a REQ mapping →
  freeze exits 1 with the uncovered REQ). Real cc-glm52/Ark, per [[e2e-tickets-need-real-ark-run]].

## Verification
- `uv run mypy` clean (currently 32 files → 34 with coverage.py + design additions).
- `uv run pytest` green (currently 853 → +~30 new cases).
- Run single test files frequently during TDD; full suite once at end.
- Then `/code-review`, then commit to `ai-dev-v0-6/ticket-01-promote-primitive` branch
  (current branch — note: branch name is from ticket 01; ticket 03 work continues on it per the
  milestone's sequential-commit convention seen in git log).
