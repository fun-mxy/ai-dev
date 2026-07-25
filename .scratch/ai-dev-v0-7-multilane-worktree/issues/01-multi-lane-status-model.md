# 01 — Multi-lane lane graph + canonical status model

**What to build:** Turn the existing single-lane-shaped state into true multi-lane canonical state for v0.7 (ADR-0009 D1/D4). `04-lane-graph.yml` may contain multiple `LANE-*` entries; `status/lane-status.yml` tracks each lane independently; `status/task-status.yml` maps every task to its lane and refuses impossible lane refs. Existing single-lane behavior must remain the degenerate case. This ticket is only the canonical model + validation + rendering/reading seams; lane worktrees and execution happen in later tickets.

**Blocked by:** none — can start immediately.

**Status:** done

- [x] multiple lanes can be represented in `04-lane-graph.yml` with tasks, dependencies, expected/exclusive files, provides/consumes, verification scope, and merge policy fields preserved
- [x] `lane-status.yml` initializes and updates more than one lane without assuming `LANE-001`
- [x] `task-status.yml` records task→lane mapping and rejects tasks assigned to missing lanes
- [x] read/status commands render all lanes clearly while preserving current single-lane output compatibility where practical
- [x] feature/coherence helpers stop assuming there is exactly one lane
- [x] unit tests cover two-lane and single-lane compatibility cases; `uv run mypy` + `uv run pytest` green
