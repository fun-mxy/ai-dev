# 06 — Multi-lane final report aggregation

**What to build:** Update final reporting and coherence summaries for true multi-lane execution (ADR-0009 D1/D6/D7). The final report must aggregate every lane's gate verdict, worktree metadata, branch/diff/commits, run/profile metadata, verification summary, unresolved issues, dependency state, and lane PR projection metadata. It must state clearly that lane PRs are projections and that v0.7 does not perform automatic feature integration or Merge Coordination.

**Blocked by:** 01, 03, 04, 05.

**Status:** open

- [ ] final report lists all lanes and their independent lane gate verdicts
- [ ] report includes worktree branch/path/base-ref metadata and changed-files/commits per lane
- [ ] report aggregates reviewer/spec-gap/verifier outcomes and unresolved P0/P1/P2/P3 issues per lane
- [ ] report includes lane PR projection URLs/mapping where present and labels them as projection metadata, not canonical state
- [ ] feature verdict aggregation fails/blocks if any lane gate has blocking unresolved state, but does not claim branches are merged or semantically integrated
- [ ] tests cover all-pass, one-lane-fail, projection-present, projection-missing, and dependency-blocked cases; `uv run mypy` + `uv run pytest` green
