# 06 - v0.2 End-to-End Integration: real cc-glm52 / ark run evidence

**Date:** 2026-07-20 (run timestamps UTC: RUN-001 2026-07-20T07:06:16–07:06:48Z; UTC+8 local ~15:06).
**Profile:** `cc-glm52` (Volcengine Ark, `glm-5.2`).
**Verdict:** **PASS scenario -> `lane-decision` PASS**; **FAIL scenario -> `lane-decision` FAIL** (exit 1).
Both are real `claude -p` headless runs against Ark through the v0.2 code - the
deterministic fake-`claude` test (`tests/test_e2e_integration.py::TestV02EndToEndIntegration`,
commit `cdc8ca4`) locks the same seam repeatably in CI; this file is the genuine
backend evidence, isomorphic to the v0.1 `evidence/05-e2e-real-run.md`.

The v0.2 walking skeleton strings the five stages on one real feature run with no
manual intervention between stages: freeze tasks/lane-graph -> implement (01) ->
review + spec-gap (02) + verify (03) -> collect-issues (04) -> lane-gate (05).
The PASS run is fully green; the FAIL run injects a verification failure (the
ticket allows "P0/P1 或 verification 失败") - the P0/P1-issue FAIL path is covered
deterministically by the fake-`claude` test
`test_cli_pipeline_fail_decision_on_p1_review_issue`.

## How to reproduce

`.ai-dev/` is gitignored in the orchestrator's own dev repo, so each scenario runs
against a `mktemp -d` directory. The cc-glm52 profile (ark base_url, token by
source-name only - `auth_env: CC_GLM52_TOKEN` with `auth_env_fallback: ANTHROPIC_AUTH_TOKEN`)
lives in `.ai-dev/agent-profiles.yml`; with `CC_GLM52_TOKEN` unset the fallback
resolves the live token, exactly as v0.1's real run did.

```bash
DEV=/Users/maxy1/Projects/playground/pp_8_codex_cc_cowork
DEMO=$(mktemp -d); mkdir -p "$DEMO/.ai-dev"
# write .ai-dev/agent-profiles.yml (cc-glm52 profile; see Profile below)
# write .ai-dev/features/FEATURE-001/03-tasks.md with a `## Tasks` body of:
#   "TASK-001: Create workspace/hello.py defining answer() returning 42."
# write 04-lane-graph.yml with one lane whose verification_commands check
#   hello.answer()==42  (PASS)  or  ==43  (FAIL, injected)
uv run ai-dev freeze FEATURE-001 tasks      --repo-root "$DEMO"
uv run ai-dev freeze FEATURE-001 lane_graph --repo-root "$DEMO"
uv run ai-dev implement      FEATURE-001 LANE-001 --profile cc-glm52 --repo-root "$DEMO"
uv run ai-dev review         FEATURE-001 LANE-001 --profile cc-glm52 --repo-root "$DEMO"
uv run ai-dev spec-gap       FEATURE-001 LANE-001 --profile cc-glm52 --repo-root "$DEMO"
uv run ai-dev verify         FEATURE-001 LANE-001 --repo-root "$DEMO"   # exits 1 on a fail verdict - the gate reads the report, not the exit code
uv run ai-dev collect-issues FEATURE-001 LANE-001 --repo-root "$DEMO"
uv run ai-dev lane-gate      FEATURE-001 LANE-001 --repo-root "$DEMO"   # 0=PASS / 1=FAIL
```

`claude` v2.1.207 is on `PATH` (`/Users/maxy1/.npm-global/bin/claude`); the wrapper
resolves it via `shutil.which`, strips the parent Claude-Code identity vars
(§10.3), and injects `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL`.

## PASS scenario (fully green -> lane-decision PASS)

```
$ ai-dev implement FEATURE-001 LANE-001 --profile cc-glm52
IMPLEMENT PASS - RUN-001 lane=LANE-001 status=proposed_done tasks_marked=['TASK-001']
$ ai-dev review FEATURE-001 LANE-001 --profile cc-glm52
REVIEW PASS - RUN-002 lane=LANE-001 role=Code Reviewer issues=0
$ ai-dev spec-gap FEATURE-001 LANE-001 --profile cc-glm52
SPEC-GAP PASS - RUN-003 lane=LANE-001 role=Spec Gap Analyst issues=0
$ ai-dev verify FEATURE-001 LANE-001
VERIFY PASS - lane=LANE-001 implement_run=RUN-001 commands=1/1 passed
$ ai-dev collect-issues FEATURE-001 LANE-001
COLLECT-ISSUES PASS - lane=LANE-001 issues=0
$ ai-dev lane-gate FEATURE-001 LANE-001
LANE-GATE PASS - lane=LANE-001 conditions=5/5 decision=...lane-decision.json   (exit 0)
```

### `lane-decision.json` (PASS - all five §18.4 conditions green)

```json
{
  "feature": "FEATURE-001", "lane": "LANE-001", "decision": "pass",
  "conditions": [
    {"name": "proposed_done",            "passed": true, "reason": "implement result status is proposed_done; tasks=['TASK-001']"},
    {"name": "verification_passed",      "passed": true, "reason": "verification verdict is pass (1/1 commands passed)"},
    {"name": "review_no_blocking_issues","passed": true, "reason": "no P0/P1 blocking review issues"},
    {"name": "spec_gap_no_blocking_issues","passed": true,"reason": "no P0/P1 blocking spec-gap issues"},
    {"name": "issue_bundle_generated",   "passed": true, "reason": "issue bundle generated with 0 issue(s)"}
  ],
  "blocking_issue_count": 0, "blocking_issues": []
}
```

### Model-written workspace + implement-result rollup

The implementer wrote exactly what was asked:

```python
# workspace/hello.py
def answer():
    return 42
```

`implement-result.json` (lane rollup, §4.4): `status=proposed_done`, `TASK-001
proposed_done`, `run_metadata` field-complete (`profile=cc-glm52`, `model=glm-5.2`,
`exit_code=0`, `changed_files=[result.json, result.md, workspace/hello.py]`,
`started_at=2026-07-20T07:06:16Z`, `ended_at=…07:06:48Z`), `validation.passed=true`,
`accepted_done=false` (§9.2 - the implementer may only propose, never declare final
done). Review (`RUN-002`) and spec-gap (`RUN-003`) reports both carry `issues: []`.

### Lane artifact chain (12 files - six artifacts × md+json, §4.4)

```
implement-result.{json,md}  review/review-report.{json,md}  spec-gap/spec-gap-report.{json,md}
verification/verification-report.{json,md}  issue-bundle.{json,md}  lane-decision.{json,md}
```

### ID scoping (v0.0/v0.1/v0.2 continuity, no re-issue/mis-scope)

`id-counters.yml` reads `LANE: 1` / `RUN: 3` (no `ISSUE:` counter on the green
path). `runs/` holds exactly `RUN-001`, `RUN-002`, `RUN-003` - the three agent
runs (implement, review, spec-gap); verify/collect/gate are deterministic and
allocate no RUN. `LANE-001` is the seeded single lane.

### Audit trail (§2.1 - full v0.2 lifecycle, in order)

```
create                {feature: FEATURE-001}
allocate_id           LANE-001 (seq 1)
freeze                tasks
freeze                lane_graph
allocate_id           RUN-001 (seq 1)   prepare_run Implementer   run exit_code=0   validate passed
mark_task_proposed_done  TASK-001 (§4.3 - deterministic writeback, no model)
allocate_id           RUN-002 (seq 2)   prepare_run Code Reviewer run exit_code=0   validate passed
allocate_id           RUN-003 (seq 3)   prepare_run Spec Gap Analyst run exit_code=0 validate passed
verify                verdict=pass (1/1)
collect_issues        issue_count=0
lane_gate             decision=pass  failed_conditions=[]  blocking_issue_count=0
```

## FAIL scenario (injected verification failure -> lane-decision FAIL)

Same chain, but the lane's verify command asserts `hello.answer()==43` (the task
still produces `answer()==42`, so verification fails by injection - the ticket's
"verification 失败" option). The implement/review/spec-gap runs are unchanged and
clean; the gate fails solely on `verification_passed`.

```
$ ai-dev verify FEATURE-001 LANE-001
VERIFY FAIL - lane=LANE-001 implement_run=RUN-001 commands=0/1 passed:   (exit 1)
  - answer-returns-43: FAIL (exit_code=1)
$ ai-dev collect-issues FEATURE-001 LANE-001
COLLECT-ISSUES PASS - lane=LANE-001 issues=0
$ ai-dev lane-gate FEATURE-001 LANE-001
LANE-GATE FAIL - lane=LANE-001 failed_conditions=verification_passed decision=...lane-decision.json   (exit 1)
```

`lane-decision.json`: `decision=fail`; `verification_passed` is `false`
("verification verdict is fail (0/1 commands passed)"); the other four conditions
pass; `blocking_issue_count=0`. `verification-report.json`: `verdict=fail`,
`passed_count=0/1`, command `answer-returns-43` exit 1. `id-counters.yml`:
`LANE: 1` / `RUN: 3` (no `ISSUE:` - the failure is verification, not a blocking
issue).

## Token safety (§10.2 / invariant #11)

`grep -rl` of each feature run tree for the live token value returns **0 files**
(both scenarios). The token appears in no `result.json` / `result.md` /
`metadata.json` / `stdout.log` / `stderr.log` / `env-snapshot.txt` /
`.run-settings.json` / lane artifact. `env-snapshot.txt` redacts every value to
`<set>`:

```
# Child claude env snapshot (names only; values redacted)
# profile=cc-glm52 base_url=…ark… model=glm-5.2 token_src=ANTHROPIC_AUTH_TOKEN
ANTHROPIC_AUTH_TOKEN=<set>
ANTHROPIC_BASE_URL=<set>
ANTHROPIC_MODEL=<set>
```

## Seam notes (ticket 06: fix integration friction in-ticket)

- **Task id in the task text.** The Implementer reads its task verbatim from the
  `## Tasks` body of `03-tasks.md`; the `output-schema.json` carries no `TASK-NNN`
  example and the package header shows `RUN-001`, so a model given a body without
  the task id will echo `RUN-001` and fail the §9.2 "only the lane's own tasks"
  check. The intended convention (which this run follows): the tasks doc lists
  `TASK-001: …`, and the model echoes that id in `result.json tasks[].id`. No code
  change - a staging/authoring convention.
- **`verify` exits 1 on a fail verdict.** `_run_verify` returns `1` when any
  command fails (a captured §24.1 failure is reported, not raised). The lane gate
  consumes the `verification-report`, not the exit code, so the chain runs
  `collect-issues` and `lane-gate` past a failing `verify` - documented here so
  the pipeline driver does not abort on it.

## Ticket-06 checklist

- [x] From one intent: freeze tasks/lane-graph -> implement -> review+spec-gap+verify -> collect-issues -> lane-gate, five stages in sequence, no manual intervention
- [x] Complete lane artifact chain: implement-result / review-report / spec-gap-report / verification-report / issue-bundle / lane-decision (each md+json)
- [x] PASS scenario (fully green -> `lane-decision` PASS) and FAIL scenario (injected verification failure -> FAIL) each captured once as evidence
- [x] `ISSUE-NNN` / `RUN-NNN` / `LANE-001` IDs correctly scoped across v0.0/v0.1/v0.2 (RUN-001/002/003, LANE-001, no ISSUE on the green/blue paths; `id-counters.yml` LANE:1 / RUN:3)
- [x] Token never persisted (grep of lane artifacts + run dirs finds 0 matches for the token value)
- [x] Integration seam friction fixed in-ticket (task-id convention noted; `verify` exit-code contract documented)
