# 03 — Run lane legs inside their lane worktrees

**What to build:** Route lane execution through the lane worktree (ADR-0009 D2). Implementer, reviewer, spec-gap analyst, verifier, and fix-run legs for a lane must execute with cwd rooted at that lane's worktree while writing/collecting their run outputs back into the feature run's lane artifact area. Metadata, changed-file computation, diff/commits capture, allowed-files validation, and frozen-artifact validation must use the lane worktree as the repo surface while preserving canonical feature artifacts in the source feature directory.

**Blocked by:** 01, 02.

**Status:** open

- [ ] lane run commands resolve the target lane and require/prepare its worktree before executing file-mutating legs
- [ ] Implementer/fix runs execute in the lane worktree and collect `result.md/json`, `diff.patch`, `commits.log`, and `metadata.json` into the canonical lane directory
- [ ] reviewer/spec-gap/verifier legs use the lane worktree diff/files as their evidence surface
- [ ] allowed-files and frozen-artifact validation work against lane worktree changes, excluding out-of-band harness state as before
- [ ] metadata records lane id, worktree path, branch, base ref, profile, cli/backend/model, changed files, commands, and exit code
- [ ] tests cover two lanes changing different files without sharing one checkout; `uv run mypy` + `uv run pytest` green
