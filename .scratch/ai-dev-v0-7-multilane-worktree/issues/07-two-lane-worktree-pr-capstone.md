# 07 — Capstone: two-lane real dogfood with worktrees and PRs

**What to build:** The v0.7 milestone capstone evidence. Drive one feature through a real two-lane flow: Planner-generated tasks assign work to at least two lanes; each lane gets its own git worktree and branch; each lane runs implement/review/spec-gap/verify/triage/lane-gate against its own worktree; each lane reaches lane gate pass; each lane is projected to a GitHub PR; the final report aggregates both lanes and their PR URLs while explicitly saying no automatic merge/coherence integration was performed. This must use at least one real Agent Profile/backend run, not only fake-claude/mocked-`gh` tests. Record evidence under `.scratch/ai-dev-v0-7-multilane-worktree/evidence/`.

**Blocked by:** 01, 02, 03, 04, 05, 06.

**Status:** done

- [x] one feature has at least two lanes with separate tasks and lane graph entries
- [x] each lane uses a distinct git worktree and branch; `worktree.json` artifacts are recorded
- [x] each lane completes implement→review→spec-gap→verify→triage→lane-gate with lane gate pass
- [x] each lane branch is pushed and projected to a GitHub PR after lane gate pass
- [x] final report aggregates both lanes, worktree metadata, run/profile evidence, gate verdicts, and PR URLs
- [x] evidence records commands, artifact paths, PR URLs, token-safety grep, and the explicit non-claim that Merge Coordinator / auto-merge was not run
- [x] real backend/profile evidence included; `uv run mypy` + `uv run pytest` green
