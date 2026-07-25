# 01 — `promote` primitive + Planner output-schema (requirements stage), unit-tested (ADR-0008)

**What to build:** The deterministic `promote` primitive — the spine of the whole v0.6 milestone
(ADR-0008 D1/D2). Given a Planner run's id-free structured-JSON proposal, `promote`: (a) allocates
canonical stable IDs from the existing `feature_ids` counter (REQ/AC for this stage); (b) stitches
cross-references by resolving the proposal's *local* refs against *frozen* upstream artifacts
(none for requirements — requirements is the root — but the resolver is built **generically** so
design (ticket 03) and tasks (ticket 04) extend it with no rework); (c) writes the canonical `.json`
(`01-requirements.json`) and **renders the `.md` mirror** (`01-requirements.md`) — promote is the
*sole* md renderer, per ADR-0008 D2 / CONTEXT.md; (d) runs **reference-integrity** (every local ref
resolves to a real allocated id) — fail loud (§24.2) on a malformed proposal. Also define the Planner
**proposal output-schemas** as data and wire the **role-aware schema lookup** for §14.1 validation
(Planner proposal schemas differ from the implementer's `result.json`; the 3-check otherwise applies
unchanged). Unit-tested at the promote seam with synthetic proposals. **No real model run yet** — that
is ticket 02. Read ADR-0008 D1/D2 and CONTEXT.md ("Authoring target & stable IDs") before implementing.

**Blocked by:** none — can start immediately.

**Status:** done

- [x] `promote` reads an id-free proposal JSON and allocates canonical REQ/AC ids from the counter
- [x] generic local-ref resolver built + unit-tested synthetically (so 03/04 reuse it without rework)
- [x] promote writes canonical `01-requirements.json` and **renders** `01-requirements.md` (sole renderer)
- [x] reference-integrity check fails loud on unresolvable local refs
- [x] Planner proposal output-schema(s) defined as data + role-aware §14.1 schema lookup wired
- [x] unit tests at the promote seam (synthetic proposals); `uv run mypy` + `uv run pytest` green
