# ADR-0006: GitHub projection via `gh` API push + PR comment (amends §28)

- **Status:** Accepted
- **Date:** 2026-07-22
- **Supersedes / amends:** amends spec §28 (non-goal "完整 GitHub integration" enters v0.5 scope as a *basic* push projection) and §22 ("MVP v0 暂不实现 GitHub projection" -> v0.5 implements basic projection). Relies on §22 (projection = mirror, local artifacts canonical), §29 invariant #10 (projection cannot write back to canonical), §10.2 / invariant #11 (token by env-var name only). Does **not** amend §28's "自动 PR 创建" non-goal - PR creation stays human-owned.

## Context

§27.1 lists "basic GitHub projection" as a future item. §22 establishes the principle: GitHub
Issue/PR/Project Board are *projections* (mirrors), local artifacts are canonical, and v0 does not
implement projection. §28 lists "完整 GitHub integration" (full GitHub integration) and "自动 PR 创建"
(auto PR creation) as MVP-v0 non-goals. v0.5 commits to "basic GitHub projection" (the all-four §27.1
scope). The open decision was the *mechanism*: render-only (human pushes a rendered markdown) vs API
push (orchestrator pushes via `gh`). We chose API push + PR comment - stepping across the §28 "full
integration" line for a deliberately *basic* push (issues + PR comment only), while keeping PR
creation human-owned so §28's "auto-PR" non-goal stays intact.

## Decisions

### D1 - Mechanism: `gh` CLI push + PR comment (amends §28)

`project-github` pushes canonical `ISSUE-NNN` -> GitHub issues (via `gh issue create` / `gh issue edit`)
and posts/updates the `final-report` as a comment on a PR. This is "basic": issues + one PR comment
only. PRs, Project Boards, checks/statuses, and full integration remain beyond v0.5 (toward §28's
"full integration" non-goal, now partially amended for the basic case).

**Rejected - render-only** (render GitHub-ready markdown under `projections/github/`, human pushes):
would be another report, not a projection, and leaves §27.1 #4 underwhelming. The render path is
subsumed - the push renders the same bodies internally before pushing.

PR creation stays human-owned: the orchestrator never creates a PR (§28 "auto-PR" untouched); it only
comments on a PR a human created and pointed at.

### D2 - Idempotency: canonical `projections/github/mapping.json` + upsert

A canonical mapping file records `ISSUE-NNN -> GH issue number` and `feature -> PR number`.
Re-projection reads the map: an existing GH issue is **edited in place** (body updated); a new one is
**created**. No duplicates. Re-running `project-github` is safe and expected, because `final-report`
is re-computable (ADR-0003) and projection follows it.

**Rejected - create-only** (new GH issue every run): duplicates on re-run, breaks the "projection is a
re-computable mirror" principle. **Rejected - GH-search identity** (find by label/title): fragile
(races, renames, rate limits). **Rejected - push-once** (terminal + `projected` flag): loses
update-on-change.

### D3 - PR target: explicit `--pr <N>`, stored in mapping

First projection takes `--pr <N>` (human-supplied); stored as `feature -> PR` in `mapping.json`;
re-projections comment on the stored PR (`--pr` overrides). Without `--pr` on first run, projection
is issues-only (no PR comment).

**Rejected - derive from current git branch** (`gh pr view` for the branch): magic that fails silently
when no PR exists and couples projection to the operator's checkout. **Rejected - `--pr` every time,
not stored**: drops the comment on re-run if forgotten.

### D4 - Failure handling: pre-flight + per-item fail-loud

A **pre-flight** checks `GITHUB_TOKEN` is set (by name), `gh` is available, the PR exists (if `--pr`),
and the rate limit is OK. Pre-flight failure -> exit 1, **no pushes** (a §24.2-style precondition).
Pre-flight passes -> push per-item; on the first mid-stream failure, **stop, keep successful pushes +
their mapping entries, report what is pending, exit 1**. Re-running resumes from the mapping.

**Rejected - all-or-nothing transactional**: cannot cleanly un-create a GH issue (rate limits, no
delete-by-default). **Rejected - warn-and-continue (best-effort)**: violates the codebase's fail-loud
discipline; a failed projection would look like success and the mapping could record a half-state
silently.

### D5 - Auth: `GITHUB_TOKEN` by env-var name (invariant #11)

`gh` reads `GITHUB_TOKEN` from the environment; `project-github` never handles the token value and
never persists it. Same env-var-name contract as every other profile (invariant #11).

## Consequences

- §28's "full GitHub integration" non-goal is **partially amended** for v0.5: a basic push (issues +
  PR comment) is in scope; PRs/boards/checks/full integration remain out. §28's "auto PR creation"
  stays a non-goal - humans create PRs; the orchestrator only comments.
- The mapping file is the **first non-deterministic canonical write**: canonical state
  (`ISSUE -> GH-number`) written as a side-effect of a network call. All prior canonical writes are
  deterministic. Accepted and documented; the pre-flight + per-item-fail-loud + re-run-resumes model
  bounds the non-determinism, and the mapping is the resumption point.
- Projection is strictly one-way (invariant #10): GitHub state never writes back to canonical
  artifacts. The mapping records *what was projected where*; it does not feed back into
  issues/decisions/final-report.
- `project-github` is non-deterministic and network-bound - it is **not a gate** and does not affect
  `feature.status` / `verdict`. It is a side-channel projection, akin to `final-report` (ADR-0003,
  non-canonical) but pushier.
