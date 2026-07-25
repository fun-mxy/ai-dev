# CONTEXT — ai-dev domain glossary

Domain vocabulary for the multi-agent SDD orchestrator. Glossary only — no
implementation details. Spec of record: `docs/multi-agent-profile-orchestrator-spec.md`.

## Planning artifact lifecycle (v0.6 Planner)

A planning artifact (`01-requirements`, `02-design`, `03-tasks`, `04-lane-graph`)
moves through three states. The transition between each is owned by a *different*
actor — that separation is the whole point:

- **proposed** — the model's (Planner run's) output, living in the run's `output/`
  directory. Mutable: re-running the generator replaces it. The model writes
  semantic content only; it never assigns canonical stable IDs.
- **canonical (unfrozen)** — the artifact as written into `01-requirements.json`
  etc. by the deterministic **promote** step (the planning-leg analogue of the
  implement leg's result rollup). Promote runs automatically right after the
  Planner run, allocating stable IDs and stitching cross-references. Mutable:
  the next promote overwrites it.
- **frozen** — same file, frozen flag flipped. Written only by a **human gate**
  (`freeze_artifact`), which advances `current_gate` to the next stage. Immutable
  from then on; any further change requires a Change Proposal.

**promote** ≠ **freeze**: promote is an automatic deterministic write (proposed →
canonical-unfrozen); freeze is the human decision (canonical-unfrozen → frozen).
The model is confined to `output/`; only the deterministic writer touches
canonical files; only the human gate touches the frozen flag — the uniform
contract (§4.3 / §13 / §14) holds for planning roles exactly as for the implementer.

### Authoring target & stable IDs

- The Planner run authors **structured JSON only** (an id-free proposal with
  *local* references to upstream items). It never authors `.md` and never
  assigns stable IDs. Markdown is always a *rendered mirror* of the canonical
  JSON — single source of truth.
- **promote** owns three jobs: (1) allocate canonical stable IDs from the
  counter; (2) stitch cross-references by resolving local refs against the
  *frozen* upstream artifacts; (3) write the canonical `.json` **and** render
  the human `.md` mirror. So promote is both the id-assigning stitcher and the
  sole md renderer.

### Artifact model (content vs runtime-state)

All four planning artifacts follow the JSON-canonical / md-rendered-mirror rule:

- `01-requirements` / `02-design`: `.md` + `.json`.
- `03-tasks`: `.md` + `.json` (**.json added in v0.6** — tasks previously md-only).
  The `.json` holds task *content* (description, refs, lane, files); the md is
  its rendered mirror.
- `04-lane-graph.yml`: YAML (single lane in v0).

`status/task-status.yml` is a **separate concern** — runtime state
(`pending → proposed_done → …`), not task content. `generate-tasks` promote
*seeds* it (every task `pending`) but does not freeze it; the task/lane gate
freezes `03-tasks(.json+.md)` and `04-lane-graph.yml` together.

### Traceability enforcement (ADR-0007 backbone) — split across two stages

Coverage is enforced at **two different points**, because a proposal is
*expected* to be incomplete while being refined:

- **reference integrity (promote-time):** every local ref resolves to a real
  allocated upstream id. An unresolvable ref is a malformed proposal → promote
  fails loud (§24.2) → Human Triage. Always checked, even mid-refinement.
- **coverage completeness (freeze-gate):** every upstream item is referenced at
  least once (every REQ in some DES `requirement_mapping`; every REQ+DES in some
  task's refs). Checked at the **freeze** action; a gap refuses to freeze (→
  back to refinement, or Triage). Not checked on draft promotes — incompleteness
  is normal during refinement.

A coverage gap does not self-heal (no looping until it passes). The human gate
keeps the **semantic** judgment (does this DES actually realize REQ-003's
intent?); **structural** coverage is the machine's job.

### Scope: happy-path only (no Change Proposals)

v0.6 implements the **linear** flow: requirements → design → tasks, each frozen
once and never revisited. `change_proposals[]` is always empty. If the model's
proposal exposes a defect in an already-frozen upstream artifact, it routes to
Human Triage (same path as a coverage gap) — the human fixes externally and
re-runs the stage. The full Change-Proposal mechanism (§9.1 limit / §17) is
deferred to a later milestone; promote stays forward-only.

### Refinement is first-class (gate ≠ one-shot)

One-shot generation ("intent → final artifact in one run") is too strong an
assumption for non-trivial requirements. A planning gate is a **refinement
session**, not a single generate:

```
generate-X → promote (draft) → human reviews rendered md
   ↑                                                       
   └─ not done? `generate-X --feedback "…"` → promote again ─┘
done → freeze (runs coverage-completeness) → advance current_gate
```

- **Feedback is in scope** — `generate-X` carries the human's feedback note in
  its input package; that note is the refinement channel.
- **promote fires after every generate** (auto), each producing a reviewable
  canonical-unfrozen artifact + rendered md. Re-running overwrites (ids
  re-allocated clean for the unfrozen stage — see Q7).
- **Two refinement channels**, the second optional within v0.6:
  1. **Model-mediated feedback loop** (primary, single-writer-pure): all content
     authored by the model; human directs via feedback. Human never edits
     artifacts directly.
  2. **Direct human edit of the unfrozen `.json`** (optional add-on ticket):
     surgical fixes the human makes to `01-X.json`; a deterministic `render`
     re-renders the `.md` mirror; new items go through an `allocate-id` helper
     so ids stay in the counter. Allowed because §4.3 reserves only
     ids/status/gate-verdict for scripts — unfrozen *content* is editable.
