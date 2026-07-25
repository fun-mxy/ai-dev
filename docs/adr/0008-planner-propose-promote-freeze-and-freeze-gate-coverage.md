# ADR-0008: Planner propose→promote→freeze; promote is the deterministic stitcher/renderer, coverage checked at the freeze gate

- **Status:** Accepted
- **Date:** 2026-07-23
- **Supersedes / amends:** realizes spec §9.1 (Planner) and main-flow steps 3-9 of §23.5, which through v0.5 were done by hand (v0.4 dogfood ran the implement→review→gap→verify loop on hand-authored requirements/design/tasks). Relies on §4.3 (canonical writes are deterministic-only), §13 (output contract), §14 (validation), §17 (Change Proposals, deferred), §18 (gate model). Closes the v0.4 planning gap and becomes the source of the REQ/AC/DES ids that ADR-0007's coverage self-attestation flows through.

## Context

Through v0.5 no CLI command generates planning artifacts; `templates.py` only seeds empty placeholders ("the Planner fills these"). v0.6 adds the Planner as a model role. The spec's uniform contract (§4.3 / §13) says canonical ids/status/freeze are written by deterministic scripts only, and §9.1 forbids the Planner from modifying frozen artifacts directly. So the design question is: how does a *model* role produce canonical planning artifacts (requirements/design/tasks/lane-graph) without ever writing them, and where do stable IDs, cross-references, markdown mirrors, and coverage checks live?

## Decisions

### D1 — Three-state lifecycle; promote automatic, freeze the only human action

A planning artifact moves through three states, each transition owned by a different actor:

- **proposed** — the Planner run's output in `output/` (mutable; re-running replaces).
- **canonical (unfrozen)** — `01-requirements.json` etc. as written by deterministic **promote** (mutable; next promote overwrites).
- **frozen** — same file, flag flipped by a **human gate** (`freeze_artifact`, advances `current_gate`).

**promote is automatic after the run** (the planning-leg analogue of the implement leg's `implement-result` rollup); **freeze is the only human action.** The model is confined to `output/`; only the deterministic writer touches canonical files; only the human gate touches the frozen flag. The uniform contract holds for planning roles exactly as for the implementer.

### D2 — Model authors id-free structured JSON; promote is the sole canonical writer *and* md renderer

The Planner run authors **structured JSON only** (id-free content with *local* references to upstream items) in `output/`. It never authors `.md` and never assigns stable IDs. **promote owns three jobs**: (1) allocate canonical IDs from the existing counter; (2) stitch cross-references by resolving local refs against the *frozen* upstream artifacts; (3) write the canonical `.json` **and render the `.md` mirror**. Markdown is always a rendered mirror of canonical JSON — single source of truth. This makes promote both the id-assigning stitcher and the sole md renderer; `03-tasks.json` is **added** so tasks conform to the same rule (task *content* in `.json`; runtime state stays in `status/task-status.yml`).

### D3 — Coverage is split: reference-integrity at promote, coverage-completeness at the freeze gate

A planning proposal is *expected* to be incomplete while being refined, so the two coverage invariants are checked at different points:

- **reference integrity (promote-time):** every local ref resolves to a real allocated upstream id. Unresolvable → promote fails loud (§24.2) → Human Triage. Always checked.
- **coverage completeness (freeze-gate):** every upstream item referenced at least once (every REQ in some DES `requirement_mapping`; every REQ+DES in some task). Checked at the **freeze** action; a gap refuses to freeze (→ back to refinement or Triage). Not checked on draft promotes.

Neither self-heals (no looping until it passes). The human gate keeps the **semantic** judgment (does this DES realize REQ-003's intent?); **structural** coverage is the machine's job. This split is the deterministic backbone of ADR-0007's requirement coverage.

### D4 — Refinement is first-class; feedback in scope; CPs deferred (rejected alternatives recorded)

A gate is a **refinement session**, not one-shot generation: `generate-X → promote (draft) → human reviews → repeat with --feedback → … → freeze`. `generate-X` carries the human's feedback note in its input package (in scope — the refinement channel). **Model-mediated feedback is the primary refinement path** (single-writer-pure); direct human edit of the unfrozen `.json` (+ deterministic `render` + `allocate-id` helper) is an *optional* v0.6 ticket, allowed because §4.3 reserves only ids/status/gate-verdict for scripts.

**Rejected alternatives:**

- **Model writes canonical artifacts directly** — violates §4.3/§13 and gives an untrusted writer control of ids/status; rejected.
- **Coverage-completeness checked at promote-time** — would fail every intentionally-incomplete draft during refinement; rejected (moved to freeze, D3).
- **One-shot generation (cold re-run, feedback deferred)** — too strong an assumption for non-trivial requirements; refinement would be impossible. Rejected; feedback is in scope.
- **Change-Proposal support in v0.6** — `change_proposals[]` stays empty; amending a frozen upstream routes to Human Triage. The full §17 CP mechanism is deferred to a later milestone so promote stays forward-only and v0.6 stays bounded.

## Consequences

- v0.6's new machinery is essentially one primitive — **promote** (id-allocate + stitch + render + reference-integrity) — plus a **freeze-gate coverage precheck**. Run-adapter, input-package, schema validation, stable-id allocator, freeze, and `current_gate` advance are all reused.
- Output-schema validation (§14.1) becomes **role-aware** (Planner proposal schemas differ from the implementer's `result.json`); the 3-check validation otherwise applies unchanged.
- Planning artifacts gain a canonical machine form across the board (incl. new `03-tasks.json`), which is what downstream spec-gap analysis and final-report task tracing need.
- The model never touches ids or canonical files; the only content mutation paths are promote (deterministic) and, optionally, direct human edit of unfrozen JSON. A future reader who proposes letting the Planner write `01-requirements.json` directly, or checking coverage at promote-time, should read D2/D3/D4 first.
