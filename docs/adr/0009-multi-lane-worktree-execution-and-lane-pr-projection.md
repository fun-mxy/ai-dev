# ADR-0009: v0.7 multi-lane worktree execution with lane-level PR projection

- **Status:** Accepted
- **Date:** 2026-07-25
- **Supersedes / amends:** advances spec §20 (worktree design), §21 (Merge Coordinator), §22 (GitHub projection), §27.2 (multi-lane/worktree roadmap). Amends ADR-0006's v0.5 boundary that PR creation is human-owned: v0.7 permits **lane-level PR creation** as a projection after a lane gate passes. Relies on §4.1 / §22 / invariant #10: local artifacts are canonical and GitHub cannot write back.

## Context

After v0.5, the orchestrator already has a second real profile (`codex-default`), role→profile policy, profile comparison, and basic GitHub projection. After v0.6, Planner propose→promote→freeze makes the planning artifacts model-generated and traceable. The next roadmap frontier is §27.2: multi-lane support, worktree profiles, lane dependency DAG execution, Merge Coordinator, and lane-level merge gates.

A paper-only multi-lane milestone would be misleading. If several lanes execute in one checkout, agents can trample each other's working-tree state even before any Git merge conflict exists. Worktree isolation is therefore part of the smallest honest multi-lane execution milestone.

The open boundary is GitHub PR handling. ADR-0006 deliberately left PR creation human-owned in v0.5 because that milestone only needed issues + a final-report comment. In v0.7, each completed lane naturally has a branch, diff/commits, lane gate verdict, verification evidence, and lane summary. Creating/updating a PR from that lane branch is now a direct projection of lane execution, not a full Merge Coordinator.

## Decisions

### D1 — v0.7 scope is lane execution, not feature integration

v0.7 implements **multi-lane worktree execution**: multiple lanes can be represented in canonical state, executed in isolated worktrees, checked independently, gated independently, and summarized at feature level.

v0.7 does **not** implement automatic feature integration. Combining lane branches, resolving Git merge conflicts, and resolving semantic conflicts remain human-owned until the Merge Coordinator milestone.

A feature may have every lane gate pass while still requiring human integration before the product branch is coherent.

### D2 — Each executing lane gets its own git worktree and branch

A lane worktree is the isolation primitive for v0.7. Each lane has a dedicated worktree and branch, recorded in lane worktree metadata. Lane legs run in that worktree cwd; their outputs are collected back into the feature run's canonical lane artifact area.

This solves only working-tree interference. It does not solve branch merge conflicts, API conflicts, design conflicts, or semantic product conflicts.

Rejected alternative — multi-lane state without worktrees: would prove only that files can contain multiple lanes, not that lanes can safely execute concurrently or independently.

### D3 — Minimal worktree engine first; full worktree profiles later

v0.7 implements a minimal lane worktree lifecycle:

- create a worktree for a lane from a base ref;
- assign a lane branch;
- record path, branch, base ref, and lifecycle status;
- detect clean/dirty state;
- refuse unsafe cleanup or rerun states loudly;
- run lane legs in the lane worktree.

v0.7 does not implement the full declarative worktree profile engine from §20. Resource classes, secret symlink policy, bootstrap hooks, port allocation, dependency-cache strategy, and shared/forbidden resource declarations are deferred.

### D4 — Lane dependency DAG is enforced as preconditions, not as a full scheduler

v0.7 honors `depends_on` as a start precondition: a lane cannot begin execution until all dependency lanes have passed their lane gates.

A full parallel scheduler is optional and not the milestone's proof. Sequential or manual lane execution is acceptable as long as lanes are isolated, canonical state supports multiple lanes, dependency prechecks work, and lane artifacts/gates are independent.

### D5 — Lane PR projection is allowed after lane gate pass

After a lane gate passes, the orchestrator may push the lane branch and create or update a GitHub PR for that lane. This amends ADR-0006's v0.5 boundary: PR creation was out for basic feature-level projection, but lane-level PR creation is now in scope because it is the natural integration handoff for worktree-backed lanes.

The trigger is **lane gate pass**, not Implementer `proposed_done`. A model's completion claim alone must not publish a PR.

### D6 — GitHub remains a projection; PR state never writes back to canonical state

A lane PR is an outward-facing projection, not source of truth. GitHub state must not update canonical lane status, task status, issue status, gate verdicts, or feature verdict.

The canonical write associated with projection is limited to projection metadata/mapping: which lane branch was projected to which PR, plus enough observed push/create/update metadata to make reruns idempotent and auditable.

Projection failure is not a lane gate failure. It is a network-side effect failure: fail loud, preserve mapping for completed side effects, and allow rerun/resume, following ADR-0006's projection discipline.

### D7 — Merge Coordinator remains deferred

Creating lane PRs is not Merge Coordination. v0.7 does not:

- automatically merge lane PRs;
- classify or resolve merge conflicts;
- resolve semantic conflicts;
- decide API/design conflicts;
- infer feature coherence from GitHub merge state.

Those remain for the future Merge Coordinator milestone.

## Consequences

- The phrase "multi-lane complete" in v0.7 means independently executable and auditable lanes, not automatically integrated branches.
- Worktree metadata becomes a first-class lane artifact.
- Final reports must aggregate lane gate results and PR projection metadata while clearly marking PRs as projections.
- Existing GitHub projection code can be extended toward lane PRs, but without violating invariant #10.
- A later Merge Coordinator can consume lane branches/PR metadata as inputs, but v0.7 must not hide integration risk behind a passing feature verdict.
