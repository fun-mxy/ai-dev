# 03 — `generate-design` + `design_gate` with REQ-coverage freeze precheck (ADR-0008)

**What to build:** The second planning gate, and the place **coverage enforcement** first bites
(ADR-0008 D3). Planner role (cc-glm52) runs with input package = intent + the **frozen**
`01-requirements.json`; it emits an id-free design proposal (design elements (local DES slots),
architecture decision, data model, API/CLI contract, file layout, invariants, risks, and a
`requirement_mapping` carrying **local refs to requirements**). `promote` allocates DES ids,
**stitches `requirement_mapping` against the frozen `01-requirements.json`** (exercising the generic
resolver built in 01 against a real frozen upstream, for the first time), and renders `02-design.md`.
Refinement loop as in 02. This ticket introduces the **freeze-gate coverage precheck** as a reusable
helper: at the design **freeze**, every REQ must appear in at least one design `requirement_mapping`
(§18.2 "design 是否覆盖 REQ/AC") — a gap **refuses to freeze** (→ back to refinement, or Human
Triage), and does **not** self-heal. (Note: coverage-completeness is checked at freeze, not at draft
promote — a proposal is expected-incomplete mid-refinement; only reference-integrity is checked at
promote.) Real cc-glm52/Ark evidence required.

**Blocked by:** 02 (needs frozen requirements to stitch against, and the generate→promote→freeze flow proven).

**Status:** done

- [x] `generate-design` command: Planner role, input package = intent + frozen `01-requirements.json`
- [x] run emits id-free design proposal (DES slots, architecture, data model, API/CLI, file layout, invariants, risks, `requirement_mapping` with local REQ refs)
- [x] `promote` allocates DES ids + stitches `requirement_mapping` against frozen requirements (generic resolver from 01, live for the first time) + renders `02-design.md`
- [x] refinement loop (`--feedback`) + human `freeze` advances design_gate → task_gate
- [x] **freeze-gate coverage precheck** (reusable helper): every REQ in ≥1 `requirement_mapping`, else refuse to freeze (no self-heal)
- [x] real cc-glm52/Ark evidence; `uv run mypy` + `uv run pytest` green
