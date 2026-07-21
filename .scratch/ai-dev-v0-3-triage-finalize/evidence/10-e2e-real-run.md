# 10 - v0.3 End-to-End Integration: real cc-glm52 / ark run evidence

**Date:** 2026-07-21 (run timestamps UTC: PASS RUN-001 2026-07-21T09:42:05Z; FAIL RUN-003 2026-07-21T09:45:04Z; UTC+8 local ~17:42–17:47).
**Profile:** `cc-glm52` (Volcengine Ark, `glm-5.2`), `claude` v2.1.207 headless.
**Verdict:** **PASS scenario -> coherence `verdict=pass` -> `final-report` pass**; **FAIL scenario -> spec-gap catches the 43-vs-42 requirement mismatch -> Human Triage `request_change_proposal` -> coherence `verdict=fail` -> `final-report` `failure_class=terminal`**.
Both are real `claude -p` headless runs against Ark through the v0.3 code - the
deterministic fake-`claude` test (`tests/test_e2e_integration.py::TestV03EndToEndIntegration`,
four scenarios) locks the same seams repeatably in CI; this file is the genuine
backend evidence, isomorphic to the v0.1 `evidence/05-e2e-real-run.md` and v0.2
`evidence/06-e2e-real-run.md`.

The v0.3 walking skeleton strings tickets 01-09 on one real feature run with no
manual intervention between stages: freeze (advance `current_gate`
requirements_gate -> design_gate -> task_gate -> lane_gate) -> implement (01) ->
review + spec-gap (02) + verify (03) -> collect-issues (04) -> Human Triage
`apply_triage` (05) -> [if `request_fix`] fix-run (07) -> re-collect -> lane-gate
(06) -> coherence-gate (08) -> final-report (09). The PASS run is fully green; the
FAIL run injects a requirement/code mismatch (REQ-001/AC-001 require `answer()==43`,
the task + code produce `42`) that the real Spec Gap Analyst catches as a P0 with
`requires_change_proposal: true`, which Human Triage defers with
`request_change_proposal` - the clean deferral v0.3 has no CP lifecycle to resolve,
so the feature terminates. The fix-loop `request_fix -> resolved -> pass` path is
covered deterministically by the fake-`claude` test `test_fix_loop_resolves_to_pass`
(the real Ark fix-loop re-uses the same model legs the PASS run proves; the
deterministic test pins the resolve/re-collect/lane-gate/coherence seam).

## How to reproduce

`.ai-dev/` is gitignored in the orchestrator's own dev repo, so each scenario runs
against a `mktemp -d` directory. The cc-glm52 profile (ark `base_url`, token by
source-name only - `auth_env: CC_GLM52_TOKEN` with `auth_env_fallback: ANTHROPIC_AUTH_TOKEN`)
lives in `.ai-dev/agent-profiles.yml`; with `CC_GLM52_TOKEN` unset the fallback
resolves the live token, exactly as v0.1/v0.2's real runs did.

```bash
DEV=/Users/maxy1/Projects/playground/pp_8_codex_cc_cowork
DEMO=$(mktemp -d); mkdir -p "$DEMO/.ai-dev"
# write .ai-dev/agent-profiles.yml (cc-glm52 profile; see v0.2 evidence)
# create FEATURE-001; fill 01-requirements.{md,json} (REQ-001/AC-001, answer()==42 for PASS / ==43 for FAIL),
#   02-design.{md,json} (DES-001), 03-tasks.md (TASK-001: answer()==42), 04-lane-graph.yml (LANE-001, verify answer()==42)
uv run ai-dev freeze FEATURE-001 requirements  --repo-root "$DEMO"   # -> design_gate
uv run ai-dev freeze FEATURE-001 design        --repo-root "$DEMO"   # -> task_gate
uv run ai-dev freeze FEATURE-001 tasks         --repo-root "$DEMO"   # -> lane_gate
uv run ai-dev freeze FEATURE-001 lane_graph    --repo-root "$DEMO"   # (shares task-gate window; no advance)
uv run ai-dev implement      FEATURE-001 LANE-001 --profile cc-glm52 --repo-root "$DEMO"
uv run ai-dev review         FEATURE-001 LANE-001 --profile cc-glm52 --repo-root "$DEMO"
uv run ai-dev spec-gap       FEATURE-001 LANE-001 --profile cc-glm52 --repo-root "$DEMO"   # >=420s; reads req/design/tasks
uv run ai-dev verify         FEATURE-001 LANE-001 --repo-root "$DEMO"
uv run ai-dev collect-issues FEATURE-001 LANE-001 --repo-root "$DEMO"
# FAIL only:
uv run ai-dev triage FEATURE-001 --issue ISSUE-001 --disposition request_change_proposal \
    --reason "..." --repo-root "$DEMO"
uv run ai-dev collect-issues FEATURE-001 LANE-001 --repo-root "$DEMO"   # re-collect refreshes the bundle
uv run ai-dev lane-gate      FEATURE-001 LANE-001 --repo-root "$DEMO"   # 0=PASS / 1=FAIL
uv run ai-dev coherence-gate FEATURE-001        --repo-root "$DEMO"     # 0=PASS / 1=FAIL; writes terminal verdict
uv run ai-dev final-report   FEATURE-001        --repo-root "$DEMO"     # 0 for a successful render of either verdict
```

`claude` v2.1.207 is on `PATH` (`/Users/maxy1/.npm-global/bin/claude`); the wrapper
resolves it via `shutil.which`, strips the parent Claude-Code identity vars
(§10.3), and injects `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL`.
The spec-gap leg reads `01-requirements.md` / `02-design.md` / `03-tasks.md` and
takes the most turns (allow >=420s; a transient Ark `529 overloaded` is retried by
the CLI's bounded retry - if a run is orphaned by a timeout, re-issue the leg).

## PASS scenario (fully green -> coherence pass -> final-report pass)

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
$ ai-dev coherence-gate FEATURE-001
COHERENCE-GATE PASS - feature=FEATURE-001 verdict=pass status=done decision=...coherence-decision.json   (exit 0)
$ ai-dev final-report FEATURE-001
FINAL-REPORT - feature=FEATURE-001 verdict=pass failure_class=None report=...final-report.json   (exit 0)
```

### Model-written workspace

```python
# workspace/hello.py  (RUN-001, written by glm-5.2)
def answer():
    return 42
```

`implement-result.json` (lane rollup, §4.4): `status=proposed_done`, `TASK-001
proposed_done`, `run=RUN-001`, `validation.passed=true`, `accepted_done=false`
(§9.2). Review (`RUN-002`) and spec-gap (`RUN-003`) reports both carry `issues: []`
- the requirement (42) matches the code (42), so the Spec Gap Analyst raises nothing.

### `coherence-decision.json` (PASS - all three ADR-0003 D1 conditions green)

```json
{
  "feature": "FEATURE-001", "verdict": "pass",
  "conditions": [
    {"name": "status_consistent",                  "passed": true, "reason": "feature.status='implementing' matches derive('lane_gate', None)"},
    {"name": "lane_passed_and_p0_p1_handled",      "passed": true, "reason": "all 0 issue(s) resolved or disarmed; 1 lane gate(s) passed"},
    {"name": "decisions_recorded",                 "passed": true, "reason": "all 0 disarmed P0/P1 issue(s) have a DEC-NNN file"}
  ],
  "lane_decision_count": 1, "issue_count": 0
}
```

`final-report.json`: `verdict=pass`, `failure_class=null`, `blocking_reasons=[]`,
all five §2.1 audit-question keys present (`code_to_requirement` /
`requirement_coverage` / `acceptance_verification` / `issue_dispositions` /
`agent_timeline`), `agent_timeline` = 3 entries (RUN-001/002/003).

### current_gate progression (ADR-0003 D2/D4)

```
create                  current_gate=requirements_gate  status=planning
freeze requirements     -> design_gate                  status=planning
freeze design           -> task_gate                     status=planning
freeze tasks            -> lane_gate                     status=implementing
freeze lane_graph        (no advance; shares task-gate window)
coherence-gate          -> feature_coherence_gate        verdict=pass  status=done   (atomic D4 write)
```

`feature-status.yml` (final): `current_gate=feature_coherence_gate`,
`verdict=pass`, `status=done`, `fix_loop_budget={used: 0, max: 1}`.

### Lane + feature artifact chain (v0.2 lane chain + v0.3 feature-level products)

```
lanes/LANE-001/: implement-result.{json,md}  review/review-report.{json,md}
                 spec-gap/spec-gap-report.{json,md}  verification/verification-report.{json,md}
                 issue-bundle.{json,md}  lane-decision.{json,md}
features/FEATURE-001/: coherence-decision.{json,md}  final-report.{json,md}   (v0.3 additions)
```

### ID scoping (v0.0/v0.1/v0.2/v0.3 continuity, no re-issue/mis-scope)

`id-counters.yml` reads `LANE: 1` / `RUN: 3` (no `ISSUE:` / `DEC:` counter on the
green path). `runs/` holds exactly `RUN-001`, `RUN-002`, `RUN-003` - the three
agent runs (implement, review, spec-gap); verify/collect/gate/coherence/final-report
are deterministic and allocate no RUN. `LANE-001` is the seeded single lane.

## FAIL scenario (spec-gap requirement mismatch -> request_change_proposal -> terminal)

Same chain, but `01-requirements` requires `answer()==43` while the task + code
produce `42`. The implementer (reads the task) writes `return 42`; the verify
command checks `==42` (passes); the Spec Gap Analyst reads the requirement (43)
against the code (42) and raises a P0 with `requires_change_proposal: true`. Human
Triage defers it with `request_change_proposal` - the clean deferral v0.3 has no
CP lifecycle to resolve, so the feature cannot reach pass.

```
$ ai-dev spec-gap FEATURE-001 LANE-001 --profile cc-glm52
SPEC-GAP PASS - RUN-003 lane=LANE-001 role=Spec Gap Analyst issues=1   (exit 0; "PASS" = leg ran, 1 issue raised)
$ ai-dev collect-issues FEATURE-001 LANE-001
COLLECT-ISSUES PASS - lane=LANE-001 issues=1   (ISSUE-001 raised)
$ ai-dev triage FEATURE-001 --issue ISSUE-001 --disposition request_change_proposal --reason "..."
TRIAGE PASS - issue=ISSUE-001 disposition=request_change_proposal severity=P0 decisions=-   (exit 0; no DEC minted)
$ ai-dev collect-issues FEATURE-001 LANE-001
COLLECT-ISSUES PASS - lane=LANE-001 issues=1   (re-collect refreshes the bundle with the triage)
$ ai-dev lane-gate FEATURE-001 LANE-001
LANE-GATE FAIL - lane=LANE-001 failed_conditions=spec_gap_no_blocking_issues decision=...lane-decision.json   (exit 1)
$ ai-dev coherence-gate FEATURE-001
COHERENCE-GATE FAIL - feature=FEATURE-001 verdict=fail status=blocked failed_conditions=lane_passed_and_p0_p1_handled decision=...coherence-decision.json   (exit 1)
$ ai-dev final-report FEATURE-001
FINAL-REPORT - feature=FEATURE-001 verdict=fail failure_class=terminal report=...final-report.json   (exit 0; report renders for both verdicts)
```

### `ISSUE-001.json` (the spec-gap P0, triaged request_change_proposal)

```json
{
  "id": "ISSUE-001", "severity": "P0", "source": "spec_gap", "status": "triaged",
  "title": "answer() returns 42 but REQ-001/AC-001 require 43",
  "requires_change_proposal": true,
  "triage": {
    "action": "request_change_proposal",
    "reason": "Requirement mismatch (43 vs 42) needs a Change Proposal (v0.4 lifecycle).",
    "by": "human", "ts": "2026-07-21T09:47:11Z"
  },
  "triage_history": null
}
```

No `decision_ids` and no `decisions/DEC-*.json`: `request_change_proposal` is a
clean deferral (ADR-0001 #7 / ADR-0002 #7) - v0.3 has no CP lifecycle, so no
`CP-NNN` is minted and no `DEC-NNN` is minted (disarming alone mints a DEC;
`request_change_proposal` is not disarming).

### `lane-decision.json` (FAIL - the spec-gap P0 is an unresolved followup)

```json
{
  "decision": "fail",
  "conditions": [
    {"name": "proposed_done",             "passed": true},
    {"name": "verification_passed",       "passed": true},
    {"name": "review_no_blocking_issues", "passed": true},
    {"name": "spec_gap_no_blocking_issues","passed": false},
    {"name": "issue_bundle_generated",    "passed": true}
  ],
  "blocking_issues": [
    {
      "id": "ISSUE-001", "source": "spec_gap", "severity": "P0",
      "title": "answer() returns 42 but REQ-001/AC-001 require 43",
      "requires_change_proposal": true, "triage_action": "request_change_proposal",
      "blocking_reason": "P0 triage action request_change_proposal is unresolved",
      "resolution_path": "request_change_proposal"
    }
  ]
}
```

`resolution_path: request_change_proposal` proves the re-collect refreshed the
lane bundle with the triage: without it the bundle would still show ISSUE-001
`raised` (untriaged) and the gate would report "P0 is untriaged" rather than the
faithful "request_change_proposal is unresolved" + `resolution_path`.

### `coherence-decision.json` (FAIL - the P0 is unhandled)

```json
{
  "feature": "FEATURE-001", "verdict": "fail",
  "conditions": [
    {"name": "status_consistent",                 "passed": true,  "reason": "feature.status='implementing' matches derive('lane_gate', None)"},
    {"name": "lane_passed_and_p0_p1_handled",     "passed": false, "reason": "lane gate not passed: fail; unhandled P0/P1 issue(s): [{'id': 'ISSUE-001', 'severity': 'P0', 'status': 'triaged', 'triage_action': 'request_change_proposal'}]"},
    {"name": "decisions_recorded",                "passed": true,  "reason": "all 0 disarmed P0/P1 issue(s) have a DEC-NNN file"}
  ],
  "lane_decision_count": 1, "issue_count": 1
}
```

`request_change_proposal` is not a disarming action, so the P0 is neither resolved
nor disarmed -> condition 2 fails. Condition 3 passes trivially (0 disarmed issues
-> 0 DECs required). `status_consistent` passes: the gate runs from `lane_gate`
with `verdict=null` -> `derive('lane_gate', null)='implementing'` == on-disk, then
the atomic D4 write sets `current_gate=feature_coherence_gate` + `verdict=fail` +
`status=blocked`.

### `final-report.json` (FAIL - failure_class=terminal)

```json
{
  "verdict": "fail", "failure_class": "terminal",
  "blocking_reasons": [
    {"class": "recoverable", "kind": "coherence_condition:lane_passed_and_p0_p1_handled", "resolution_path": "resolve_coherence_condition"},
    {"class": "terminal",    "kind": "pending_change_proposal", "issue_id": "ISSUE-001", "resolution_path": "change_proposal"},
    {"class": "recoverable", "kind": "lane_gate_not_passed", "resolution_path": "fix_or_triage"}
  ]
}
```

`failure_class=terminal` because the `pending_change_proposal` reason is terminal
(v0.3 has no CP lifecycle, so the feature cannot reach pass without the v0.4
lifecycle). `issue_dispositions` records `ISSUE-001 [P0] disposition=request_change_proposal
decision_ids=[]`; `requirement_coverage` shows `REQ-001` NOT implemented and
`acceptance_verification` shows `AC-001` NOT verified (`lane_verification=fail`) -
the implementer did not declare `related_requirements`/`related_acceptance_criteria`
on this run, so the Q2/Q3 traceability is honestly empty (a known v0.3 limitation
recorded in `meta.known_gaps`).

`feature-status.yml` (final): `current_gate=feature_coherence_gate`,
`verdict=fail`, `status=blocked`, `fix_loop_budget={used: 0, max: 1}`.

`id-counters.yml`: `ISSUE: 1` / `LANE: 1` / `RUN: 3` (no `DEC:` - the clean
deferral mints nothing). `agent_timeline` = 3 entries (RUN-001 Implementer,
RUN-002 Code Reviewer, RUN-003 Spec Gap Analyst).

## Token safety (§10.2 / invariant #11)

An independent `grep -rl` of each feature run tree for the live token value
returns **0 files** (both scenarios). The token appears in no `result.json` /
`result.md` / `metadata.json` / `stdout.log` / `stderr.log` / `env-snapshot.txt` /
`.run-settings.json` / lane or feature artifact. `env-snapshot.txt` redacts every
value to `<set>` (names only). The token is resolved by env-var **name**
(`auth_env: CC_GLM52_TOKEN` / `auth_env_fallback: ANTHROPIC_AUTH_TOKEN`), never
inlined.

## Seam notes (ticket 10: fix integration friction in-ticket)

- **Bundle-staleness after triage.** `apply_triage` writes the `triage` state on
  `issues/ISSUE-NNN.json` only (ADR-0001 #2); the lane gate reads the lane
  `issue-bundle.json` projection (ADR-0002 D1). After triage the bundle is stale,
  so the spec §19 flow's **re-collect** step is mandatory before the lane gate:
  `collect-issues` re-projects `issues/` into the bundle, letting the gate see the
  triage disposition and record the faithful `resolution_path` (e.g.
  `request_change_proposal`) instead of a stale "untriaged". This run follows that
  ordering (`triage -> collect-issues -> lane-gate`); the fake-`claude` test
  `test_cli_fail_terminal_request_change_proposal` pins it repeatably.
- **`spec-gap` "PASS" with `issues=1`.** The checking-leg CLI summary prints
  `SPEC-GAP PASS` when the leg ran and produced a schema-valid report - the
  `issues` count is the analyst's findings, not the leg verdict. The lane gate
  consumes the report's `issues[]`, so a "PASS" leg with `issues=1` still fails
  the gate on `spec_gap_no_blocking_issues`. Documented here so the pipeline driver
  does not abort on a "PASS" leg that raised a blocker.
- **`final-report` exits 0 for both verdicts.** `_run_final_report` returns `0` on
  a successful render of either `pass` or `fail` (the report exists for both,
  ADR-0003 D6); `1` is reserved for a missing/corrupt required artifact or a null
  verdict (coherence has not run). The verdict/failure_class are in the printed
  summary line, not the exit code.
- **Severity is the model's, the failure_class is the disposition's.** The Spec
  Gap Analyst classified the 43-vs-42 mismatch as **P0** (the ticket's prose named
  "P1 override / request_cp"; the model assigned P0). For `request_change_proposal`
  the severity is immaterial: it is a non-disarming clean deferral on any blocking
  severity, so coherence treats the issue as unhandled and the report classifies it
  `terminal` regardless. `failure_class` is a function of the disposition, not the
  severity.
- **Q2/Q3 traceability is run-declared.** The implementer did not declare
  `related_requirements`/`related_acceptance_criteria` on this run, so
  `requirement_coverage`/`acceptance_verification` are honestly empty/NOT-verified
  even on the PASS run. v0.3 has no AC->test traceability index; the report records
  this in `meta.known_gaps`. The verdict is driven by the issue/coherence state,
  not by Q2/Q3.

## Ticket-10 checklist

- [x] From one intent: freeze (advance `current_gate`) -> implement -> review+spec-gap+verify -> collect-issues -> Human Triage -> [fix-run] -> re-collect/re-triage -> lane-gate -> coherence-gate -> final-report, in sequence, no manual intervention
- [x] PASS scenario (fully green -> `verdict=pass` -> `final-report` pass) and FAIL scenario (spec-gap requirement mismatch -> `request_change_proposal` -> `verdict=fail` -> `final-report` `failure_class=terminal`) each captured once as real Ark evidence
- [x] `ISSUE`/`RUN`/`LANE`/`DEC` IDs correctly scoped across v0.0-v0.3 (RUN-001/002/003, LANE-001, ISSUE-001 on FAIL only, no DEC on either path - `request_change_proposal` is a clean deferral; `id-counters.yml` LANE:1 / RUN:3, + ISSUE:1 on FAIL)
- [x] `current_gate` advances correctly throughout: requirements_gate -> design_gate -> task_gate -> lane_gate (freeze) -> feature_coherence_gate (coherence); `feature.status` derived correctly (planning -> implementing -> done|blocked)
- [x] Token never persisted (independent grep of both feature run trees finds 0 matches for the token value)
- [x] Integration seam friction fixed in-ticket (bundle-staleness re-collect after triage; `spec-gap` PASS-with-issues + `final-report` exit-code contracts documented)
- [x] `evidence/` records real Ark run evidence (real cc-glm52/glm-5.2, isomorphic to v0.1/v0.2 e2e; the fake-`claude` `TestV03EndToEndIntegration` locks the seams in CI)
