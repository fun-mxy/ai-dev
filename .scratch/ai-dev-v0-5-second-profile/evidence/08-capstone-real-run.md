# 08 - v0.5 dogfood capstone (multi-profile + comparison + projection)

**Date:** 2026-07-22 (run timestamps UTC: FEATURE-001 codex RUN-001 14:38:25–14:39:35Z,
RUN-002 review 14:39:56Z, RUN-003 spec-gap 14:40:45Z, verify FAIL 14:41:54Z, RUN-004
re-implement 14:43:14Z, verify PASS 14:44:22Z, RUN-005 review 14:44:36Z, RUN-006 spec-gap
14:45:41Z; FEATURE-002 cc-glm52 RUN-001 implement 14:47:02Z, RUN-002 review 14:51:00Z,
RUN-003 spec-gap 14:54:17Z, verify PASS 14:58:47Z; UTC+8 local ~22:38–22:59).
**Target:** `examples/string-utils/` (the committed v0.4 dogfood target, re-used; `.ai-dev/`
is gitignored throwaway, wiped clean for this capstone).
**Intent (identical for both parallel runs):** `加一个 slugify(s) 函数，带边界测试（空串 / unicode / 首尾连字符）`.
**Profiles:** `codex-default` (codex CLI headless, OpenAI provider via stored-cred `crs` → gpt-5.5,
ADR-0005 `CodexRunner`) AND `cc-glm52` (claude headless → Ark/glm-5.2). Each feature-run ran its
whole pipeline under one profile (implement + review + spec-gap all `--profile <P>`), so each run's
representative backend is unambiguous.
**Verdicts:** BOTH runs reach **`verdict=pass` / `status=done`** — FEATURE-001 (codex-default) and
FEATURE-002 (cc-glm52). Then `compare-profiles` projected a side-by-side (quality axis =
requirement coverage, populated via ticket 05 / ADR-0007: REQ-001 implemented, AC-001..004
verified, for *both* runs), and `project-github` pushed the canonical issues + final-report to
real GitHub (issues + a PR comment). **This is the genuine proof that all four §27.1 items +
Q2/Q3 work end-to-end across both Agent Profiles.** Per the real-backend evidence discipline, this
file is the evidence of record — not the mocked-`gh` / fake-claude unit tests.

> **Stronger than ticket 04.** Ticket 04's all-codex run left Q2/Q3 *empty* (codex's result.json
> carried no `related_requirements` linkage, so `requirement_coverage` showed `REQ-001: NOT
> implemented` / `evidence_runs=[]`). This capstone re-runs under the **current** code (ticket 05's
> §14.4 traceability-declaration enforcement is live), and both profiles now declare their REQ/AC
> linkage → Q2/Q3 populate cleanly (1/1 implemented, 4/4 verified) with `known_gaps` retired.
> The deterministic tests still lock the seams in CI; this file is the real-backend proof.

## The two parallel feature-runs

The two runs are intent-siblings (identical `## Original intent` text), planned identically
(REQ-001 + AC-001..004 + DES-001 + TASK-001 + LANE-001), differing only in the Agent Profile that
executes every leg. `compare-profiles FEATURE-001 --profiles cc-glm52,codex-default` locates each by
its `feature-status.yml` `agent_profiles.implementer` (F1=codex-default, F2=cc-glm52).

### FEATURE-001 — `codex-default` → `verdict=pass`

The fix-loop path (same self-contradiction shape as ticket 04, now corrected at the source):

```
14:38:25Z · implement   · RUN-001 (codex, Implementer)        proposed_done
14:39:56Z · review      · RUN-002 (codex, Code Reviewer)      issues=0
14:40:45Z · spec-gap    · RUN-003 (codex, Spec Gap Analyst)   issues=0
           · verify          · FAIL (pytest 14/15: CamelCase expectation vs formula)
14:43:14Z · implement   · RUN-004 (codex, re-Implementer)     proposed_done  [fix loop]
14:44:22Z · verify          · PASS (2/2: pytest 15/15 + mypy)
14:44:36Z · review      · RUN-005 (codex, Code Reviewer)      issues=0
14:45:41Z · spec-gap    · RUN-006 (codex, Spec Gap Analyst)   issues=0
           · collect-issues  · 0 issues
           · lane-gate        · PASS (5/5)
           · coherence-gate   · verdict=pass, status=done
           · final-report     · verdict=pass
```

Per-leg elapsed_ms (audit `run` events): implement 80+67s (RUN-001+RUN-004), review 49+64s,
spec-gap 54+47s — all codex/gpt-5.5, exit_code=0. RUN-001..003 are the first attempt; RUN-004..006
are the fix loop. IDs continuous (RUN-001..006 / LANE-001), no re-issue.

### FEATURE-002 — `cc-glm52` → `verdict=pass`

Clean single pass (the planning self-contradiction was corrected before this run's first implement,
so verify passed first try):

```
14:47:02Z · implement   · RUN-001 (claude/Ark, Implementer)   proposed_done
14:51:00Z · review      · RUN-002 (claude/Ark, Code Reviewer) issues=1 (P2)
14:54:17Z · spec-gap    · RUN-003 (claude/Ark, Spec Gap Analyst) issues=1 (P2)
14:58:47Z · verify          · PASS (2/2: pytest + mypy)
           · collect-issues  · ISSUE-001 (P2 code_review) + ISSUE-002 (P2 spec_gap)
           · lane-gate        · PASS (5/5; P2 non-blocking)
           · coherence-gate   · verdict=pass, status=done
           · final-report     · verdict=pass
```

Per-leg elapsed_ms: implement 225s, review 197s, spec_gap 270s (Ark/glm-5.2 is materially slower
per leg than codex/gpt-5.5 — see comparison). The two raised issues are **real** reviewer/spec-gap
findings (a regex-duplication drift hazard; a traceability-ID nitpick), not injected. Both P2 →
non-blocking → coherence pass with no triage needed; both still get projected to GitHub below.

### The planning self-contradiction (and the fix-loop it triggered)

The TASK-001 spec was internally contradictory in its test expectation:
`slugify('CamelCase') == 'camelcase'` (no hyphen) vs the impl formula
`boundary = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", s)` + DES-001 ("slugify reuses snake_case
regulation" — snake_case inserts a separator at CamelCase boundaries) → `camel-case`. **FEATURE-001's
RUN-001 codex copied both contradictory parts faithfully** (test asserting `camelcase`, impl
producing `camel-case`) → the deterministic verify gate caught the mismatch (pytest 14/15,
`assert 'camel-case' == 'camelcase'`). Rather than repeat ticket 04's spec-gap→override→DEC
theatre around an *injected* contradiction, the authoritative spec (formula + DES-001) was honoured:
the test expectation was corrected to `'camel-case'` in **both** tasks, RUN-004 made test+impl
consistent (15/15 pass), and FEATURE-002 ran clean first try. The §9.5 Verifier owned verification,
not the model — caught on real codex output, exactly as designed.

## `compare-profiles` (ticket 06) — the side-by-side projection

```
$ ai-dev compare-profiles FEATURE-001 --profiles cc-glm52,codex-default
COMPARE-PROFILES - feature=FEATURE-001 profiles=cc-glm52,codex-default
  projection=…/FEATURE-001/projections/profile-comparison.json
```

| metric | cc-glm52 (FEATURE-002) | codex-default (FEATURE-001) |
|---|---|---|
| verdict | **pass** | **pass** |
| failure_class | None | None |
| verifier / lane_decision | pass / pass | pass / pass |
| elapsed_ms total | 692000 (impl 225, review 197, spec_gap 270) | 362000 (impl 67, review 64, spec_gap 47; +183 "other" = failed RUN-001..003) |
| issues | 2 (P2×2) | 0 |
| requirement_coverage (Q2) | **1/1 implemented** (REQ-001, ev RUN-001) | **1/1 implemented** (REQ-001, ev RUN-004) |
| acceptance_verification (Q3) | **4/4 verified** (AC-001..004) | **4/4 verified** (AC-001..004) |

The **quality axis (requirement coverage, Q2/Q3) is populated for both runs** — the headline
deliverable of ticket 05. `meta.known_gaps` records the honest caveats: reviewer-variance (the two
runs use different reviewer profiles, so issue sets legitimately differ — cc-glm52 raised 2, codex
raised 0), planner-non-determinism, self-attested coverage, and unnormalized wall-clock latency.
Non-canonical (ADR-0003-style), byte-recomputable, read-only over canonical state.

The "other"=183000ms bucket on codex-default is correct behaviour: RUN-001/002/003 (the verify-failed
first attempt) are not attributable to a *current* lane leg artifact (the re-implement overwrote
`implement-result.json` with RUN-004), so `_run_role_map` buckets them as `other` rather than
silently dropping them — the projection never hides elapsed time.

## `project-github` (ticket 07 / ADR-0006) — real GitHub push

Projected **FEATURE-002** (the run with 2 real issues) onto a human-created PR:

```
$ export GITHUB_TOKEN="$(gh auth token)"      # invariant #11: token by env-var name only
$ ai-dev project-github FEATURE-002 --pr 1
PROJECT-GITHUB - feature=FEATURE-002 issues_created=['ISSUE-001','ISSUE-002'] issues_updated=[] pr=1 comment=created
```

| canonical id | GitHub | URL |
|---|---|---|
| ISSUE-001 (P2, code_review) | issue #2 | https://github.com/fun-mxy/ai-dev/issues/2 |
| ISSUE-002 (P2, spec_gap) | issue #3 | https://github.com/fun-mxy/ai-dev/issues/3 |
| final-report | PR #1 comment (id 5047816091) | https://github.com/fun-mxy/ai-dev/pull/1 |

`projections/github/mapping.json` (the first non-deterministic canonical write, ADR-0006):

```json
{"feature":"FEATURE-002","pr_number":1,"pr_comment_id":5047816091,"issues":{"ISSUE-001":2,"ISSUE-002":3}}
```

PR #1 (`v0.5-capstone-dogfood-pr`, throwaway) was created by hand first — the orchestrator never
creates the PR (§28 / invariant #10, human owns PR creation). Pre-flight (GITHUB_TOKEN set by name,
`gh` on PATH, rate-limit OK, PR #1 exists) passed before any push; D4 (no pushes on pre-flight fail)
honoured.

### Idempotent re-run → surfaces + fixes a real ticket-07 bug

The first re-run (edit-in-place, D2) **fail-louded on real `gh`**:

```
error: gh pr comment edit #1/5047816091 failed (exit 1): unknown flag: --edit
```

The PR-comment **edit** path built `gh pr comment <PR> --edit <id> --body <body>` — but `gh pr
comment` has **no edit-by-id flag** (`--edit`/`--edit-last` take no id and edit only the authed
user's *last* comment, ambiguous once a human comments). The unit test (ticket 07) couldn't catch
this: `FakeGh` only checked `"--edit" in argv` and returned ok, so the mocked-`gh` test passed
against an argv real `gh` rejects. **This is exactly why the real-GitHub capstone exists** — the
dogfood run caught what the mocked test couldn't.

**Fix (in-ticket, like v0.4's seam fixes):** the edit path now PATCHes the stored comment id via the
REST API, `gh api repos/{owner}/{repo}/issues/comments/<id> --method PATCH -f body=<body>` (PR
conversation comments are issue comments in the REST API; `{owner}/{repo}` resolved by `gh` from the
repo's git remote, since the projection runs from the repo root). The create path (`gh pr comment`)
is unchanged. `FakeGh` + the re-run test were updated to the corrected argv; mypy clean, 22
github_projection tests green. Re-run after the fix:

```
$ ai-dev project-github FEATURE-002 --pr 1
PROJECT-GITHUB - feature=FEATURE-002 issues_created=[] issues_updated=['ISSUE-001','ISSUE-002'] pr=1 comment=updated
# GH issues still [#2, #3] (edited in place, no dupes); PR #1 still 1 comment (PATCHed in place).
```

D2 idempotency now holds end-to-end on real GitHub: re-projection edits issues + PATCHes the comment
in place — no duplicates created.

## Token safety (§10.2 / invariant #11)

An independent `grep -rlF "<each-secret-value>"` over both feature trees returns **0 files** for
every secret env var (only the *count* is reported, never the value):

```
CRS_OAI_KEY            (len 67): matches in feature trees = 0   OK
ANTHROPIC_AUTH_TOKEN   (len 49): matches in feature trees = 0   OK
CC_GLM52_TOKEN         : unset (cc-glm52 used the ANTHROPIC_AUTH_TOKEN fallback)
OPENAI_API_KEY         : unset (codex used stored-cred crs; token_src=None)
```

No token value in any `result.json` / `result.md` / `metadata.json` / `stdout.log` / `stderr.log` /
`env-snapshot.txt` / lane / feature / issue / decision / projection artifact. `env-snapshot.txt`
redacts to a names-only comment:

```
# profile=codex-default base_url=None model=None token_src=None        # F1 (stored-cred crs)
# profile=cc-glm52 base_url=https://ark.cn-beijing.volces.com/api/coding model=glm-5.2 token_src=ANTHROPIC_AUTH_TOKEN   # F2
```

## Auth-path honesty (ADR-0005 D3)

codex path (b) — stored-credential custom provider (`crs`) — exercised end-to-end here on both the
implement and the two checking legs, reaching `verdict=pass`. codex path (a) — `OPENAI_API_KEY`
env-injected — remains inferred/unit-covered pending a real `api.openai.com` key (`OPENAI_API_KEY`
is unset in this env). cc-glm52 exercises the Ark/glm backend via `ANTHROPIC_AUTH_TOKEN`.

## Ticket-08 checklist

- [x] same intent through `cc-glm52` AND `codex-default` → two feature-runs, both verdict=pass
- [x] `compare-profiles` across the two (requirement-coverage quality axis populated, both runs)
- [x] `project-github` to a real GH issue (#2, #3) + PR comment (PR #1), `--pr` to a human-created PR
- [x] `evidence/08-capstone-real-run.md`: both verdicts, comparison artifact, GH mapping + URLs, token grep
- [x] real backend (codex/OpenAI + Ark/glm) + real GitHub push — not fake-claude / mocked-`gh`
- [x] milestone tickets 01–07 all done (this capstone dogfoods 02 CodexRunner, 03 role_defaults,
      05 §14.4 Q2/Q3, 06 compare-profiles, 07 project-github; and fixed a real 07 seam bug)
- [x] seam bug found + fixed in-ticket (PR-comment edit argv; mocked-`gh` test couldn't catch it)
- [x] README.md v0.5 status section added
