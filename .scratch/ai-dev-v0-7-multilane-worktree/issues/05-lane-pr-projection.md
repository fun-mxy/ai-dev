# 05 — Lane PR projection after lane gate pass

**What to build:** Extend GitHub projection to create/update one PR per gate-passed lane (ADR-0009 D5/D6, amending ADR-0006's v0.5 boundary). After a lane gate passes, the orchestrator may push the lane branch and create or update a GitHub PR whose title/body summarize the lane, tasks, REQ/AC/DES refs, verification evidence, review/spec-gap issues, worktree branch, and canonical artifact paths. The PR is a one-way projection and integration handoff: GitHub state never writes back to canonical status or verdict. Projection must be idempotent via a lane PR mapping and fail loud on network/preflight errors while preserving already-successful side effects.

**Blocked by:** 02, 03, 04.

**Status:** open

- [ ] lane PR projection refuses to run before lane gate pass; Implementer `proposed_done` alone is insufficient
- [ ] projection pushes the lane branch to the configured remote and creates or updates a lane PR using `gh`
- [ ] PR body includes lane id, feature id, task ids, related REQ/AC/DES ids, lane gate verdict, verification summary, issue summary, branch/worktree metadata, and canonical artifact pointers
- [ ] projection mapping records lane→PR number/URL/head branch/base branch and supports idempotent reruns/updates
- [ ] projection failure does not mutate lane/feature verdicts; it exits non-zero with pending/completed side effects reported
- [ ] tests cover preflight, gate-pass requirement, create, update, mapping reuse, partial failure, and no token persistence; `uv run mypy` + `uv run pytest` green
