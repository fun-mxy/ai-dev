# 04 — `generate-tasks` + `task`/`lane_gate` (adds `03-tasks.json`; REQ+DES coverage precheck) (ADR-0008)

**What to build:** The third and largest planning gate — `promote` writing **four** files in one step
(ADR-0008 D2 / CONTEXT.md "Artifact model"). Planner role (cc-glm52) runs with input package = intent
+ frozen requirements + frozen design; it emits an id-free tasks proposal (TASK slots with local refs
to REQ+DES, single-lane assignment, expected/exclusive files, lane purpose). `promote` allocates TASK
ids, stitches refs against frozen requirements **and** design, and writes: (1) **`03-tasks.json`** —
**new in v0.6**, the canonical task *content* (tasks previously md-only); (2) rendered `03-tasks.md`;
(3) **seeds** `status/task-status.yml` (every task `pending`) — runtime state, not frozen here; (4)
populates `04-lane-graph.yml` (the single lane's `tasks`, `purpose`, expected/exclusive files). The
task/lane gate coverage precheck (reusing 03's helper): every REQ+DES is referenced by ≥1 task, else
refuse to freeze. **Freeze freezes `03-tasks(.json+.md)` and `04-lane-graph.yml` together** (§18.3)
and advances to `lane_gate`. Refinement loop as in 02/03. Real cc-glm52/Ark evidence required.

**Blocked by:** 03 (needs frozen requirements + frozen design to stitch against, and the freeze-gate coverage helper).

**Status:** done

- [x] `generate-tasks` command: Planner role, input package = intent + frozen requirements + frozen design
- [x] run emits id-free tasks proposal (TASK slots, local REQ+DES refs, lane assignment, expected/exclusive files, purpose)
- [x] `promote` allocates TASK ids + stitches against frozen requirements **and** design, writing all four: `03-tasks.json` (**new**), rendered `03-tasks.md`, seeded `task-status.yml` (all `pending`), populated `04-lane-graph.yml`
- [x] task/lane gate coverage precheck: every REQ+DES referenced by ≥1 task, else refuse to freeze
- [x] freeze freezes `03-tasks(.json+.md)` + `04-lane-graph.yml` together; advances to `lane_gate`
- [x] refinement loop (`--feedback`); real cc-glm52/Ark evidence; `uv run mypy` + `uv run pytest` green
