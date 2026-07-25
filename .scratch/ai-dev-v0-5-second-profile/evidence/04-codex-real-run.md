# 04 - v0.5 codex End-to-End: real codex / OpenAI-backend run -> `verdict=pass`

**Date:** 2026-07-22 (run timestamps UTC: implement RUN-001 11:14:05–11:15:08Z; review RUN-002
11:15:19–11:16:08Z; spec-gap RUN-003 11:16:26–11:17:17Z; re-implement RUN-004 11:20:02–11:21:23Z;
review RUN-005 11:21:33–11:22:31Z; spec-gap RUN-006 11:26:57–11:27:57Z; UTC+8 local ~19:14–19:28).
**Target:** `examples/string-utils/` (the committed v0.4 dogfood target, re-used).
**Profile:** `codex-default` (codex CLI 0.144.5 headless; OpenAI provider via the `crs` custom
provider → `gpt-5.5`; ADR-0005 `CodexRunner`). All three agent legs ran on `codex-default`.
**Verdict:** the multi-CLI `ai-dev` runs the full happy-path feature -
intent -> freeze -> implement -> review -> spec-gap -> verify -> collect-issues -> [triage] ->
lane-gate -> coherence-gate -> final-report - on a **real** codex / OpenAI backend and reaches
**`verdict=pass`** / `status=done`. The deterministic fake-`codex` + fake-`claude` test
(`tests/test_e2e_integration.py::TestV05CodexMultiCliE2E`) locks the multi-CLI dispatch +
`role_defaults` seam repeatably in CI; this file is the genuine backend evidence, isomorphic to the
v0.1/v0.2/v0.3/v0.4 `evidence/*-e2e-real-run.md`. **Per the real-backend evidence discipline, this
ticket is NOT done on the fake-codex test alone** - the run below is the evidence of record.

This is the v0.5 **#1 capstone** (ticket 04): the second Agent Profile (`codex-default`) +
`CodexRunner` adapter (ticket 02) + `role_defaults` policy (ticket 03) string together on real
code, and one codex run stands as the integration evidence.

> **Auth-path honesty (ADR-0005 D3).** The codex adapter supports two auth paths: (a) OpenAI
> provider with `OPENAI_API_KEY` **env-injected** onto the child env, and (b) a **stored-credential
> custom provider** (`~/.codex/auth.json`, e.g. `crs`) where codex resolves the key itself and the
> adapter injects *no* token (token-not-required). This environment has **no `api.openai.com` key**,
> so path (a) remains **inferred, not exercised** - the spike (ticket 01) and this run both used the
> stored-cred `crs` path (b). D3 path (b) is now exercised end-to-end on a real codex/gpt-5.5 backend
> reaching `verdict=pass`; path (a) is unit-covered (`TestCodexEnvSnapshot`) and awaits a real
> `api.openai.com` key to close. This gap is recorded in ADR-0005 D3's status.

## The feature (re-used dogfood intent)

> 加一个 `slugify(s)` 函数，带边界测试（空串 / unicode / 首尾连字符）

Same v0.4 target: preset `snake_case` + green `pytest`/`mypy` baseline; the feature **adds
`slugify`** (same two-step fold as `snake_case` but `-`-separated and lower-cased) with boundary
tests. REQ-001 / AC-001..AC-004 pin: empty -> `""`, `Café Noël` -> `café-noël` (non-ASCII passed
through), `  Hello   World--` -> `hello-world`, non-str -> `TypeError`. Design invariant DES-001:
"slugify 复用 snake_case 两步规约，仅分隔符 `-` 且结果小写" (slugify reuses snake_case's two-step
regulation, separator `-`, lower-cased). The 04-lane-graph declares
`workspace/string_utils/{__init__,casing}.py` + `workspace/tests/test_casing.py` and the v0.4
verify commands (`PYTHONPATH=. python -m pytest -q -p no:cacheprovider -c /dev/null tests` +
`python -m mypy string_utils`, cwd = the implement run's `workspace/`).

## How to reproduce

`.ai-dev/` is gitignored (throwaway runtime state, v0.4 ticket 05), so the run lives at
`examples/string-utils/.ai-dev/`. `examples/string-utils/.ai-dev/agent-profiles.yml` carries
`codex-default` + `cc-glm52` + the ticket-03 `role_defaults` table (implementer=codex-default,
reviewer/spec_gap_analyst=cc-glm52). The agent legs below pass `--profile codex-default` explicitly
to force the all-codex capstone (see **Ark 429 pivot** in Seam notes for why the `role_defaults`
multi-CLI path was deferred).

```bash
cd examples/string-utils                          # repo-root = the target (relative)
ai-dev create-feature-run "加一个 slugify(s) 函数，带边界测试（空串 / unicode / 首尾连字符）"
# Planner fills 01-requirements / 02-design / 03-tasks / 04-lane-graph (slugify feature)
ai-dev freeze FEATURE-001 requirements            # -> design_gate
ai-dev freeze FEATURE-001 design                  # -> task_gate
ai-dev freeze FEATURE-001 tasks                   # -> lane_gate
ai-dev freeze FEATURE-001 lane_graph              # (shares task-gate window; no advance)
ai-dev implement   FEATURE-001 LANE-001 --profile codex-default   # RUN-001 (Implementer, codex)
ai-dev review      FEATURE-001 LANE-001 --profile codex-default   # RUN-002 (Code Reviewer, codex)
ai-dev spec-gap    FEATURE-001 LANE-001 --profile codex-default   # RUN-003 (Spec Gap Analyst, codex)
ai-dev verify      FEATURE-001 LANE-001                           # FAIL on RUN-001 (see Seam notes)
# verify caught a self-inconsistent test codex wrote -> re-implement (the legitimate fix loop):
ai-dev implement   FEATURE-001 LANE-001 --profile codex-default   # RUN-004 (re-implement, codex)
ai-dev review      FEATURE-001 LANE-001 --profile codex-default   # RUN-005 (Code Reviewer, codex)
ai-dev spec-gap    FEATURE-001 LANE-001 --profile codex-default   # RUN-006 (Spec Gap Analyst, codex)
ai-dev verify      FEATURE-001 LANE-001                           # 2/2 PASS (pytest + mypy)
ai-dev collect-issues FEATURE-001 LANE-001                        # ISSUE-001 (P1 spec_gap)
ai-dev triage FEATURE-001 --issue ISSUE-001 --disposition override --by human --reason "..."   # DEC-001
ai-dev collect-issues FEATURE-001 LANE-001                        # re-collect refreshes the bundle
ai-dev lane-gate       FEATURE-001 LANE-001                       # 0=PASS (5/5 conditions)
ai-dev coherence-gate  FEATURE-001                                # 0=PASS; writes terminal verdict
ai-dev final-report    FEATURE-001                                # 0; renders verdict=pass
```

The whole chain is observed gate-by-gate with the read-only commands (v0.4 ticket 03):
`list-features` / `show-status` / `log`.

## Dry-run pre-flight (v0.4 ticket 04 / ADR-0004)

Before spending tokens, `--dry-run` validated the codex wiring with **no state change** and **no
codex spawn**. This surfaced and fixed a real product gap (see Seam notes): the codex adapter
(ticket 02) was **never wired into the dry-run planner** - `dry_run.py` hardcoded the claude argv
and raised "token source not set" unconditionally, so `--dry-run` refused every codex profile. The
fix dispatches the dry-run through `get_runner` (mirroring ADR-0005 D1) and honours
`token_required` (codex is token-not-required). After the fix:

```
$ ai-dev implement FEATURE-001 LANE-001 --profile codex-default --dry-run
IMPLEMENT DRY-RUN - would prepare Implementer run for lane LANE-001 + spawn codex (no id minted, no spawn)
- profile: codex-default
- role_cli: codex
- invocation: ["codex", "exec", "-", "-s", "workspace-write", "--skip-git-repo-check", "--color", "never", "--ephemeral"]
- prompt_on_stdin: true
- env_target_names: {"token_source": null, ...}     # codex uses stored-cred crs; no env token
- would_spawn: true
- would_mint_ids: ["RUN-NNN (next monotonic)"]
# id-counters.yml stayed LANE:1 (no RUN counter) - dry-run minted nothing.
```

The argv matches the ticket-01 spike-verified form exactly (cwd = run_dir, prompt on stdin, `-s
workspace-write --skip-git-repo-check --color never --ephemeral`). `token_source: null` confirms
the stored-cred path (b): codex needs no env-injected key.

## Real run -> `verdict=pass`

All six agent runs are `codex-default` / `cli=codex` / `backend=openai` / `exit_code=0`. Each leg
~50–80 s of real codex/gpt-5.5 time. The verify gate is deterministic (pytest + mypy).

```
$ ai-dev show-status FEATURE-001   # final
FEATURE-001
  status: done
  current_gate: feature_coherence_gate
  verdict: pass
  lanes:
    LANE-001: decision=pass

$ ai-dev list-features
FEATURE-001  status=done  gate=feature_coherence_gate  verdict=pass
```

Gate-by-gate (`ai-dev log FEATURE-001`, abridged):

```
11:12:17Z · create          · FEATURE-001
11:12:17Z · allocate_id     · LANE-001 (type=LANE, seq=1)
11:12:17Z · freeze          · requirements -> design_gate
11:12:17Z · freeze          · design -> task_gate
11:12:17Z · freeze          · tasks -> lane_gate
11:12:17Z · freeze          · lane_graph
11:14:05Z · implement       · RUN-001 (codex, Implementer)        proposed_done
11:15:19Z · review          · RUN-002 (codex, Code Reviewer)      issues=0
11:16:26Z · spec-gap        · RUN-003 (codex, Spec Gap Analyst)   issues=0
           · verify          · FAIL (pytest 1/2: RUN-001 self-inconsistent test)
11:20:02Z · implement       · RUN-004 (codex, re-Implementer)     proposed_done  [fix loop]
11:21:33Z · review          · RUN-005 (codex, Code Reviewer)      issues=0
11:26:57Z · spec-gap        · RUN-006 (codex, Spec Gap Analyst)   issues=1 (SPEC-GAP-001, P1)
           · verify          · PASS (2/2: pytest + mypy, 15/15 tests)
           · collect-issues  · ISSUE-001 (P1, spec_gap)
           · triage          · ISSUE-001 disposition=override -> DEC-001
           · collect-issues  · re-collect (bundle refreshed, triage preserved)
           · lane-gate        · PASS (5/5)
           · coherence-gate   · verdict=pass, status=done
           · final-report     · verdict=pass
```

### `coherence-decision.json` (all three ADR-0003 D1 conditions green)

```
coherence verdict=pass, status=done
conditions:
  status_consistent:            passed=true
  lane_passed_and_p0_p1_handled: passed=true   # P1 disarmed by override (DEC-001)
  decisions_recorded:           passed=true     # DEC-001 recorded
```

### `final-report.json` (verdict=pass)

```
verdict: pass
feature_status: done
failure_class: None
blocking_reasons: []
agent_timeline: [RUN-001..RUN-006]  # all profile=codex-default, exit_code=0
                                    # RUN-004=Implementer, RUN-005=Code Reviewer, RUN-006=Spec Gap Analyst
issue_dispositions:
  - ISSUE-001 [P1] status=triaged source=spec_gap disposition=override decisions=['DEC-001']
```

> **Q2/Q3 projection note (honest).** `requirement_coverage` shows `REQ-001: NOT implemented`
> (evidence_runs=[]) and `acceptance_verification` shows `AC-001..004: NOT verified`
> (evidence_runs=[], lane_verification=pass). This is a *projection* gap, not a pipeline failure:
> the real codex runs' `result.json` did not declare `related_requirements`/`related_acceptance_criteria`
> linkage (codex wrote its own result, unlike the v0.2 fake-claude payload which hard-coded it), so
> the Q2/Q3 projectors found no evidence runs. The lane verify gate (pytest + mypy) **passed 2/2** and
> the coherence verdict is `pass` on `lane_passed + p0_p1_handled + decisions_recorded` - the
> ticket-04 exit criterion (`verdict=pass` / `status=done`) is met. The linkage gap is a known
> final-report projection limitation, out of scope for this capstone.

### The Spec-Gap issue (real codex finding, triaged override)

The codex Spec Gap Analyst (RUN-006) flagged a **real** divergence - and the verify gate had already
caught its precursor. The task spec (`03-tasks.md`) is **self-contradictory**:

- impl formula (line 20-21): `boundary = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", s)` inserts `-` at
  the CamelCase boundary -> `slugify("CamelCase") == "camel-case"`;
- design invariant DES-001: "slugify reuses snake_case regulation" (snake_case inserts separators at
  CamelCase boundaries) -> agrees with the formula -> `camel-case`;
- test assertion (line 25): `slugify('CamelCase') == 'camelcase'` (no hyphen) -> **contradicts both**.

**RUN-001** codex copied both contradictory parts faithfully (test expecting `camelcase`, impl
producing `camel-case`) -> the verify gate caught the test/impl mismatch (`assert 'camel-case' ==
'camelcase'`, pytest 14/15). **RUN-004** codex re-implemented with test+impl both `camel-case`
(following the formula + design) -> verify **15/15 pass**, but now diverges from the task's test
assertion. **RUN-006** codex spec-gap correctly flagged this:

```
SPEC-GAP-001 (P1): "slugify CamelCase expectation differs from task package"
  TASK-001 explicitly asks for a TestSlugify case where slugify('CamelCase') == 'camelcase',
  but the implementation diff's test expects 'camel-case' and the implementation inserts a hyphen...
```

Triage (ADR-0001 #4 matrix: `override` x P1 = legal, non-blocking + reason + DEC): the
implementation is **correct** per the formula + design; the flagged divergence is a
spec-documentation defect (the task's test assertion contradicts its own impl formula + DES-001),
out of scope for the implementation lane. `disposition=override`, `DEC-001` recorded, P1 disarmed,
lane-gate 5/5, coherence pass. This is the v0.4-style triage-to-`verdict=pass` path - exercised on a
**real codex finding**, not an injected one.

## ID scoping (continuous, no re-issue / mis-scope)

```
runs/: RUN-001 RUN-002 RUN-003 RUN-004 RUN-005 RUN-006   (monotonic, no gaps)
lanes/: LANE-001
issues/: ISSUE-001
decisions/: DEC-001
id-counters.yml: DEC: 1  ISSUE: 1  LANE: 1  RUN: 6
```

RUN-001/002/003 are the first attempt (RUN-001's verify-failing test); RUN-004/005/006 are the fix
loop. Each `RUN-NNN` was minted once and refers to one completed run - no re-issue. (One interrupted
spec-gap attempt - codex finished writing `result.json` but a shell timeout killed the orchestrator
before it wrote `metadata.json` - was discarded as a never-completed run record and re-minted cleanly
as RUN-006; the broken record left no artifact, so this is recovery from an interrupted mint, not
re-issue of a completed run.)

## Token safety (§10.2 / invariant #11)

An independent `grep -rlF "<each-secret-value>" .ai-dev/features/FEATURE-001` returns **0 files** for
every secret env var (token values are secret - only the *count* is reported, never the value):

```
CRS_OAI_KEY            (len 67): matches in feature tree = 0   OK
ANTHROPIC_AUTH_TOKEN   (len 36): matches in feature tree = 0   OK
OPENAI_API_KEY         : unset (codex used stored-cred crs; token_source=null)
```

The token appears in no `result.json` / `result.md` / `metadata.json` / `stdout.log` /
`stderr.log` / `env-snapshot.txt` / lane / feature / issue / decision artifact. `env-snapshot.txt`
redacts to a names-only comment (codex injects no env token on the stored-cred path):

```
# profile=codex-default base_url=None model=None token_src=None
```

## Seam notes (integration friction found/fixed in-ticket)

- **Dry-run never supported codex (ticket-02 gap, fixed).** `dry_run.py` hardcoded the claude argv
  (`build_cli_flags`) and `_require_token_source` raised unconditionally, so `--dry-run` refused
  every codex profile with "token source not set". Fix: `_build_invocation` returns an `Invocation`
  and dispatches via `get_runner(profile).build_invocation(...)` (mirrors ADR-0005 D1);
  `_require_token_source` returns `str | None` and raises only when `get_runner(profile).token_required`
  (codex is token-not-required). This is exactly the integration friction the real run is meant to
  surface (like v0.4's seam fixes); mypy clean, 36 dry-run tests pass.
- **The verify gate catches real codex defects (RUN-001).** Codex wrote a test (`CamelCase` ->
  `camelcase`) its own implementation failed (`camel-case`). The deterministic verify gate caught it
  (pytest 14/15) - the §9.5 Verifier owns verification, not the model.
- **The re-implement loop works (RUN-004).** Re-running `implement` overwrites
  `implement-result.json` with the new run and mints a fresh monotonic `RUN-NNN`; RUN-004 made
  test+impl consistent (15/15 pass). No counter reset, no re-issue.
- **The spec-gap gate catches real divergences (RUN-006).** The codex Spec Gap Analyst correctly
  identified the impl-vs-task-spec divergence (a genuine task-spec self-contradiction), rated P1.
  D5 (thin adapter, uniform §13 contract via prompt) holds for all three agent roles - codex honours
  the §13 `result.json` contract for Implementer, Code Reviewer, and Spec Gap Analyst alike.
- **Ark 429 rate-limit pivot to all-codex.** The first attempt used the ticket-03 `role_defaults`
  policy (implementer=codex-default, reviewer/spec-gap=cc-glm52/Ark). The Ark review leg (RUN-002
  of that attempt) hit a 429: `"Usage limit reached for 5 hour. Your limit will reset at
  2026-07-22 19:56:47"` - an **infra** rate limit, not a code bug. Rather than wait ~46 min or
  re-issue a stable id, the capstone pivoted to **all-codex** (`--profile codex-default` on every
  leg), which directly matches the ticket's "with `--profile codex-default`" phrasing and is the
  stronger codex capstone. The multi-CLI dispatch (D1/D2) is still proven three ways: the ticket-02
  unit suite (22 tests), the dry-run (claude review wiring), and the failed Ark RUN-002 (which
  really spawned `claude` and reached Ark). The deterministic `TestV05CodexMultiCliE2E` test locks
  the multi-CLI `role_defaults` dispatch (codex implementer + claude reviewers) in CI.
- **Auth-path (b) exercised, (a) inferred.** See the honesty note at the top: stored-cred `crs`
  (path b) is exercised end-to-end here; `OPENAI_API_KEY` env-injection (path a) remains
  inferred/unit-covered pending a real `api.openai.com` key. ADR-0005 D3 status updated accordingly.

## Ticket-04 checklist

- [x] full happy path on real `codex-default` -> `verdict=pass` / `status=done`
- [x] `--dry-run` pre-flighted the chain (no spawn / no state change) before the real spend
- [x] `list-features` / `show-status` / `log` observed gate-by-gate advance
- [x] token never persisted (`grep -rlF` -> 0 matches; env-snapshot redacted)
- [x] IDs continuous (RUN-001..006 / LANE-001 / ISSUE-001 / DEC-001); no re-issue / mis-scope
- [x] `evidence/04-codex-real-run.md` captured
- [x] deterministic fake-codex e2e test locks the seam in CI
      (`tests/test_e2e_integration.py::TestV05CodexMultiCliE2E`)
