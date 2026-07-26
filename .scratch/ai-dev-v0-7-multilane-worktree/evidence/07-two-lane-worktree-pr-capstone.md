# 07 - Capstone evidence: two-lane real dogfood with worktrees and PRs

**Feature:** FEATURE-001 (slugify + word_count standalone packages)
**Target repo (throwaway):** https://github.com/fun-mxy/ai-dev-v07-capstone
**Date:** 2026-07-26
**Verdict:** PASS — one feature driven through a real two-lane worktree flow end-to-end on the cc-glm52/Ark backend, both lane gates PASS, both lane branches projected to GitHub PRs, final report aggregates both lanes.

All commands run from the ai-dev project root with `--repo-root /tmp/ai-dev-v07-capstone` (the throwaway target repo). The orchestrator itself is the in-tree `ai-dev` package (`uv run --project <project-root> ai-dev ...`).

---

## Acceptance checklist

- [x] one feature has at least two lanes with separate tasks and lane graph entries
- [x] each lane uses a distinct git worktree and branch; `worktree.json` artifacts are recorded
- [x] each lane completes implement->review->spec-gap->verify->triage->lane-gate with lane gate pass
- [x] each lane branch is pushed and projected to a GitHub PR after lane gate pass
- [x] final report aggregates both lanes, worktree metadata, run/profile evidence, gate verdicts, and PR URLs
- [x] evidence records commands, artifact paths, PR URLs, token-safety grep, and the explicit non-claim that Merge Coordinator / auto-merge was not run
- [x] real backend/profile evidence included; `uv run mypy` + `uv run pytest` green

---

## 1. Two lanes with separate tasks and lane graph entries

Planner (multi-lane, `--lanes 2`) seeded two independent lanes from the frozen task package. Frozen lane graph: `.ai-dev/features/FEATURE-001/04-lane-graph.yml`.

| Lane   | Task     | Purpose                              | depends_on | exclusive_files                                                                   | verification_commands                          |
|--------|----------|--------------------------------------|------------|------------------------------------------------------------------------------------|------------------------------------------------|
| LANE-001 | TASK-001 | Build and test the slugify package   | `[]`       | `workspace/slugify/__init__.py`, `workspace/slugify/slugify.py`, `workspace/tests/test_slugify.py` | `PYTHONPATH=. python -m pytest -q -p no:cacheprovider -c /dev/null tests`; `python -m mypy slugify` |
| LANE-002 | TASK-002 | Build and test the word_count package | `[]`       | `workspace/word_count/__init__.py`, `workspace/word_count/word_count.py`, `workspace/tests/test_word_count.py` | `PYTHONPATH=. python -m pytest -q -p no:cacheprovider -c /dev/null tests`; `python -m mypy word_count` |

The two lanes have **disjoint `exclusive_files`** (no file conflict) and **empty `depends_on`** (independent), so they satisfy the multi-lane dependency precheck (ticket 04) and run as parallel lanes.

---

## 2. Distinct git worktree + branch per lane; `worktree.json` recorded

Each lane has its own worktree + branch, created by `ensure_lane_worktree` (resolve-or-create). `worktree.json` metadata recorded per lane:

| Lane   | branch                       | worktree path                                                        | `worktree.json` |
|--------|------------------------------|----------------------------------------------------------------------|-----------------|
| LANE-001 | `ai-dev/FEATURE-001/LANE-001` | `/tmp/ai-dev-v07-capstone/.ai-dev/worktrees/FEATURE-001/LANE-001` | `.ai-dev/features/FEATURE-001/lanes/LANE-001/worktree.json` |
| LANE-002 | `ai-dev/FEATURE-001/LANE-002` | `/tmp/ai-dev-v07-capstone/.ai-dev/worktrees/FEATURE-001/LANE-002` | `.ai-dev/features/FEATURE-001/lanes/LANE-002/worktree.json` |

Worktree isolation verified: each worktree's `workspace/` holds only that lane's package (slugify in LANE-001, word_count in LANE-002), and each lane branch carries exactly its own commit:

- LANE-001 branch `ai-dev/FEATURE-001/LANE-001`: commit `ba84b49` "ai-dev Implementer LANE-001 (run RUN-009): workspace deliverables"
- LANE-002 branch `ai-dev/FEATURE-001/LANE-002`: commit `cb4dd87` "ai-dev Implementer LANE-002 (run RUN-010): workspace deliverables"

`commits.log` per lane: `.ai-dev/features/FEATURE-001/lanes/LANE-001/commits.log`, `.../LANE-002/commits.log`.

---

## 3. Each lane completes implement->review->spec-gap->verify->triage->lane-gate (PASS)

### LANE-001 (slugify)

| Step          | Command (abridged)                                                        | Run / artifact | Result |
|---------------|---------------------------------------------------------------------------|----------------|--------|
| implement     | `ai-dev implement FEATURE-001 LANE-001 --profile cc-glm52 --max-turns 30` | RUN-009        | PASS, `proposed_done`, tasks_marked=['TASK-001'] |
| review        | `ai-dev review FEATURE-001 LANE-001 --profile cc-glm52 --max-turns 30`    | RUN-007        | PASS, 0 issues |
| spec-gap      | `ai-dev spec-gap FEATURE-001 LANE-001 --profile cc-glm52 --max-turns 30`  | RUN-008        | PASS, 2 issues |
| verify        | `ai-dev verify FEATURE-001 LANE-001`                                      | deterministic  | PASS, 2/2 commands |
| collect-issues| `ai-dev collect-issues FEATURE-001 LANE-001`                              | deterministic  | PASS, 2 issues bundled |
| triage        | `ai-dev triage FEATURE-001 --issue ISSUE-001 --disposition override --reason ...` (P1) ; `... --issue ISSUE-002 --disposition accept --reason ...` (P2) | DEC-001 / DEC-002 | PASS |
| collect-issues (refresh) | `ai-dev collect-issues FEATURE-001 LANE-001` (re-run after triage to refresh the bundle with live triage state) | deterministic | PASS, 2 issues |
| lane-gate     | `ai-dev lane-gate FEATURE-001 LANE-001`                                   | lane-decision.json | **PASS, 5/5 conditions** |

Triage rationale (both spec-gap findings are cross-lane / design false positives):
- ISSUE-001 (P1, "word_count package not implemented") → `override`: cross-lane false positive — word_count is TASK-002 / LANE-002 scope, not LANE-001. LANE-001 implements slugify only (TASK-001). `(P1, override)` is a gate-disarming action with required `--reason` and Decision id DEC-001.
- ISSUE-002 (P2, "tests in shared workspace/tests/ dir") → `accept`: per frozen design — `04-lane-graph.yml` declares `PYTHONPATH=. python -m pytest ... tests`, expecting a shared `workspace/tests/` dir; AC-007 is satisfied by the dedicated `test_slugify.py` module.

### LANE-002 (word_count)

| Step          | Command (abridged)                                                        | Run / artifact | Result |
|---------------|---------------------------------------------------------------------------|----------------|--------|
| implement     | `ai-dev implement FEATURE-001 LANE-002 --profile cc-glm52 --max-turns 30` | RUN-010        | PASS, `proposed_done`, tasks_marked=['TASK-002'] |
| review        | `ai-dev review FEATURE-001 LANE-002 --profile cc-glm52 --max-turns 30`    | RUN-011        | PASS, 1 issue |
| spec-gap      | `ai-dev spec-gap FEATURE-001 LANE-002 --profile cc-glm52 --max-turns 30`  | RUN-012        | PASS, 1 issue |
| verify        | `ai-dev verify FEATURE-001 LANE-002`                                      | deterministic  | PASS, 2/2 commands |
| collect-issues| `ai-dev collect-issues FEATURE-001 LANE-002`                              | deterministic  | PASS, 2 issues bundled |
| triage        | (not required — both issues P2/P3, non-blocking)                          | —              | n/a |
| lane-gate     | `ai-dev lane-gate FEATURE-001 LANE-002`                                   | lane-decision.json | **PASS, 5/5 conditions** |

LANE-002's collected issues: ISSUE-003 (code_review, P3, opaque AttributeError on None/non-string input) and ISSUE-004 (spec_gap, P2, shared tests/ dir — same design choice as LANE-001's ISSUE-002). Both are P2/P3, outside `_BLOCKING_SEVERITIES = {P0, P1}`, so the lane gate passes without triage. The triage step is part of the lane flow and was exercised on LANE-001; LANE-002 had no blocking issues to triage.

Lane gate conditions (both lanes, 5/5): `proposed_done`, `verification_passed`, `review_no_blocking_issues`, `spec_gap_no_blocking_issues`, `issue_bundle_generated`.

Lane-decision artifacts:
- `.ai-dev/features/FEATURE-001/lanes/LANE-001/lane-decision.json`
- `.ai-dev/features/FEATURE-001/lanes/LANE-002/lane-decision.json`

---

## 4. Each lane branch pushed and projected to a GitHub PR after gate pass

`project-lane-pr` (ticket 05) ran after each lane gate PASS. `GITHUB_TOKEN` provided at runtime via `GITHUB_TOKEN="$(gh auth token)"` (env-var, never persisted, never printed). One-way projection: GitHub state never writes back into canonical lane/task/feature status.

| Lane   | PR  | URL                                                  | head branch                       | base | action   | mapping |
|--------|-----|------------------------------------------------------|-----------------------------------|------|----------|---------|
| LANE-001 | #1  | https://github.com/fun-mxy/ai-dev-v07-capstone/pull/1 | `ai-dev/FEATURE-001/LANE-001`     | main | created  | `.ai-dev/features/FEATURE-001/projections/github/lane-prs.json` |
| LANE-002 | #2  | https://github.com/fun-mxy/ai-dev-v07-capstone/pull/2 | `ai-dev/FEATURE-001/LANE-002`     | main | created  | (same mapping file) |

Projection mapping (`lane-prs.json`) records both lanes with `pr_number`, `pr_url`, `head_branch`, `base_branch`, `remote`, `projected_at`.

---

## 5. Final report aggregates both lanes

`ai-dev final-report FEATURE-001` → `.ai-dev/features/FEATURE-001/final-report.json` (verdict=pass, failure_class=None). Preceded by `ai-dev coherence-gate FEATURE-001` → `.ai-dev/features/FEATURE-001/coherence-decision.json` (verdict=pass).

Per-lane aggregation in `final-report.json` (`lanes[]`):

| Lane   | impl run | profile / cli / backend / model           | review        | spec-gap      | verify     | gate | PR | changed_files | worktree branch |
|--------|----------|-------------------------------------------|---------------|---------------|------------|------|----|---------------|-----------------|
| LANE-001 | RUN-009  | cc-glm52 / claude / glm / glm-5.2         | RUN-007 (0)   | RUN-008 (2)   | pass 2/2   | pass | #1 | 3             | ai-dev/FEATURE-001/LANE-001 |
| LANE-002 | RUN-010  | cc-glm52 / claude / glm / glm-5.2         | RUN-011 (1)   | RUN-012 (1)   | pass 2/2   | pass | #2 | 3             | ai-dev/FEATURE-001/LANE-002 |

Each lane entry in the final report carries: `gate.verdict`, `run` (run_id + profile/cli/backend/model + timestamps + exit_code), `changed_files`, `review`, `spec_gap`, `verification`, `issues` (by severity), `worktree` (branch / base_ref / path / lifecycle / clean), `dependency_state`, and `pr_projection` (projected / pr_number / pr_url / head_branch / base_branch / remote / projected_at).

Feature status: `.ai-dev/features/FEATURE-001/status/feature-status.yml` → `status: done` (frozen artifacts all true).

---

## 6. Token-safety grep (invariant #11 — COUNT only, never values)

Token-by-env-var-name-only is upheld. `agent-profiles.yml` references the token by env-var name (`auth_env: "CC_GLM52_TOKEN"`, `auth_target: "ANTHROPIC_AUTH_TOKEN"`); run `env-snapshot.txt` records `ANTHROPIC_AUTH_TOKEN=<set>` (redacted) and `token_src=ANTHROPIC_AUTH_TOKEN` (name only).

Grep counts across `.ai-dev/` in the target repo (config + all run artifacts):

| Pattern (inline token values)                         | matching files |
|-------------------------------------------------------|----------------|
| `gh[pousr]_[A-Za-z0-9]{20,}` / `github_pat_[A-Za-z0-9]{20,}` (excl. `runs/`) | **0** |
| same pattern (incl. `runs/`)                          | **0** |
| suspicious long secret-like strings (≥32 chars, no spaces) in `env-snapshot.txt` | **0** |

`GITHUB_TOKEN` appears only as a runtime env-var (set inline per `project-lane-pr` invocation); it is **not** persisted in any `.ai-dev/` artifact. Evidence reports the COUNT of matches (0 inline values), never a token value.

---

## 7. Explicit non-claim: Merge Coordinator / auto-merge NOT performed

**No Merge Coordinator and no automatic merge was performed.** `project-lane-pr` is a one-way projection (push lane branch + open PR); it does not merge. The frozen lane graph sets `merge_policy.auto_merge: false` for both lanes, with `semantic_conflict_policy: human_triage`. The two PRs (#1, #2) remain **open** on `fun-mxy/ai-dev-v07-capstone`; neither was merged, auto-merged, nor coherence-integrated. Merge Coordinator / auto-merge is deferred out of v0.7 (per the milestone plan). GitHub state never writes back into canonical lane/task/feature status — projection is strictly lane-state → GitHub.

---

## 8. Real backend / profile evidence

Both lanes ran the implement/review/spec-gap legs on the **cc-glm52** Agent Profile (real backend: `claude` CLI → Ark → `glm-5.2`). This is not fake-claude/mocked-`gh`:

- `final-report.json` per-lane `run`: `profile=cc-glm52`, `cli=claude`, `backend=glm`, `model=glm-5.2`.
- `env-snapshot.txt` (RUN-010): `# profile=cc-glm52 base_url=https://ark.cn-beijing.volces.com/api/coding model=glm-5.2 token_src=ANTHROPIC_AUTH_TOKEN`.
- `project-lane-pr` used the real `gh` CLI against `github.com` (PRs #1/#2 created on the live throwaway repo), with `GITHUB_TOKEN` from `gh auth token`.
- `verify` ran the real `pytest` + `mypy` commands against each lane's worktree `workspace/` (2/2 passed each).

---

## 9. `uv run mypy` + `uv run pytest` green (ai-dev source)

Run in the ai-dev project root (`/Users/maxy1/Projects/playground/pp_8_codex_cc_cowork`):

- `uv run mypy src/ai_dev` → `Success: no issues found in 37 source files`
- `uv run pytest -q` → `1193 passed in 50.67s`

---

## 10. Real-backend bugs surfaced by the capstone and fixed

Dogfooding on the real backend surfaced two v0.7 bugs that the fake-claude/mocked-`gh` test suite did not catch. Both are fixed in-tree with tests:

1. **Real agent wrote `workspace/` deliverables to run-home, not the worktree.** `build_prompt` states the run-home (`runs/RUN-NNN/`) as the agent's working directory, so the real `claude` agent authored `workspace/...` under run-home. The worktree snapshot diff then saw an empty `workspace/` and `changed_files` was empty, leaving the lane branch empty. **Fix (`src/ai_dev/run_wrapper.py` + `src/ai_dev/lane_run.py`):** after the agent run, sync run-home `workspace/` files into the worktree `workspace/` (`_sync_run_workspace_to_worktree`, excluding `__pycache__`/`.mypy_cache`/`.pytest_cache`/`.ruff_cache`), then `commit_lane_deliverables` commits `workspace/` on the lane branch. Gated on `agent_cwd != run_root` so the v0.2 fallback (no worktree) is unaffected. LANE-002 (run after the fix) populated `changed_files`/`committed_files` = 3 expected files and the lane branch commit on the first try.

2. **`ensure_lane_worktree` reuse path did not resolve the symbolic `base_ref` to a SHA.** `worktree.json` stores `base_ref: "HEAD"`; the reuse path returned it unresolved, so `git diff HEAD..HEAD` produced an empty `diff.patch`. **Fix (`src/ai_dev/lane_run.py`):** the reuse path now resolves `base_ref` to a SHA via `_resolve_base_ref_sha` (idempotent on SHAs). LANE-002's `diff.patch` is 5522 bytes (3 files / 68 insertions), and `base_ref` in the lane-run audit payload is the pinned SHA `a528bb0...`.

Supporting test changes: `tests/test_shell_verifier.py` (`TestVerifyLaneWorktreeCwd` — verify cwd = `<worktree>/workspace/`), `tests/test_lane_run.py` (`test_verify_command_sees_files_in_lane_worktree` — workspace-relative verify command).

---

## 11. Notes / known gaps (honest)

- `status/lane-status.yml` records `gate_verdict: pass` for both lanes but leaves `current_phase: not_started` and the per-leg `*_run` pointers as `null` — the lane-status backfill is not fully driven by every leg. This is cosmetic; the authoritative per-lane run ids, gate verdicts, and PR projections live in `final-report.json` / `lane-decision.json` / `lane-prs.json`, all of which are correct and complete.
- LANE-001's `diff.patch` was captured before the base_ref-SHA fix landed and is stale/empty; LANE-001 was not re-run post-fix (the lane branch already carries commit `ba84b49`, so PR #1 is correct). LANE-002's `diff.patch` is correct (5522 bytes). The lane gate does not read `diff.patch`; PR projection uses the lane branch.
