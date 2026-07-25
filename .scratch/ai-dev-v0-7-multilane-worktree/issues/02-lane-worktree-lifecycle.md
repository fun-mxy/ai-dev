# 02 — Lane worktree lifecycle primitive

**What to build:** Add the minimal lane worktree engine required for honest multi-lane execution (ADR-0009 D2/D3). Given a feature run and lane id, create a git worktree on a lane-specific branch from a declared base ref, record `worktree.json`, detect clean/dirty state, and provide explicit keep/remove lifecycle operations. Fail loud on unsafe states: do not silently delete dirty worktrees, do not overwrite an existing unrelated worktree, and do not pretend a lane is isolated if worktree creation failed. Full declarative worktree profiles (resource classes, symlink policy, bootstrap hooks, port allocation) are out of scope.

**Blocked by:** 01.

**Status:** open

- [ ] command/helper creates one git worktree per lane with a deterministic branch naming scheme
- [ ] lane `worktree.json` records lane id, branch, base ref, path, created/updated timestamps, and lifecycle status
- [ ] clean/dirty detection is available and used before destructive lifecycle operations
- [ ] remove/keep behavior is explicit; dirty or unknown worktrees are preserved unless a human explicitly chooses otherwise
- [ ] repeated create/run attempts are idempotent or fail loud with an actionable message
- [ ] tests cover create, existing worktree, dirty refusal, clean removal/keep, and non-git/precondition errors; `uv run mypy` + `uv run pytest` green
