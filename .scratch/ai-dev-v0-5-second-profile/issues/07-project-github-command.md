# 07 - `project-github` command (ADR-0006)

**What to build:** A new `project-github FEATURE-NNN [--pr N]` command per ADR-0006. Pushes canonical
`ISSUE-NNN` -> GitHub issues via `gh issue create`/`gh issue edit` and posts/updates the `final-report`
as a PR comment (D1, amends §28 "full integration" non-goal for the *basic* case). Idempotent via
canonical `projections/github/mapping.json` (`ISSUE-NNN -> GH issue number`, `feature -> PR`): existing
-> edit-in-place, new -> create (D2). `--pr N` on first projection stored as `feature -> PR`; without
it, issues-only (D3). Pre-flight (`GITHUB_TOKEN` set by name, `gh` available, PR exists if `--pr`, rate
limit OK) -> exit 1, no pushes on failure (§24.2-style); pre-flight passes -> per-item fail-loud, keep
successes + mapping, exit 1 on first mid-stream failure, re-run resumes (D4). `GITHUB_TOKEN` by env-var
name only (invariant #11); one-way (invariant #10); human owns PR creation (§28 "auto-PR" intact). Note:
`mapping.json` is the **first non-deterministic canonical write** (canonical state written as a
network side-effect) - document this consequence.

**Blocked by:** none - build + unit-test with mocked `gh`/fixtures; real-GitHub evidence is part of 08.

**Status:** done

- [x] `project-github` command: `gh issue create`/`edit` + PR comment (D1)
- [x] canonical `projections/github/mapping.json` + upsert (D2); idempotent re-run
- [x] `--pr N` stored as `feature -> PR`; issues-only without it (D3)
- [x] pre-flight + per-item fail-loud; re-run resumes from mapping (D4)
- [x] `GITHUB_TOKEN` by env-var name (invariant #11); one-way (invariant #10)
- [x] unit tests with mocked `gh` (create / update / pre-flight fail / mid-stream fail + resume)
- [x] `mapping.json` recognized as non-deterministic canonical write (documented)
- [x] `uv run mypy` + `uv run pytest` green
