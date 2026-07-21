# ADR-0002: Issue lifecycle across the fix loop; bundle as a projection of persisted state

- **Status:** Accepted (OQ6-9 resolved).
- **Date:** 2026-07-20
- **Spec:** §6, §15.2, §17, §18.4, §19, §24.2; invariants #2, #14
- **Depends on:** ADR-0001 (triage data model)
- **Supersedes:** ADR-0001 #3 (request_cp -> CP-NNN) for v0.3 only - CP-NNN creation deferred to v0.4 (Decision #7)

## Context

ADR-0001's gate formula assumes `issue.triage` is visible to the gate. It is not.
v0.2 `collect_issue_bundle` (`issue_bundle.py:114` `_normalize_issues`) rebuilds
each issue from the reviewer/gap **report** (`dict(issue)`, only the id overlaid)
and writes both `issues/ISSUE-NNN.json` and `lanes/LANE-NNN/issue-bundle.json`
from that report-derived dict. `apply_triage` (ADR-0001) writes `triage` only to
`issues/ISSUE-NNN.json`. The gate reads `issue-bundle.json` (`lane_gate.py:236`).
**There is no bridge.** So even on the happy path (no fix loop), every P0/P1 is
"untriaged" to the gate and it FAILs. The fix loop only widens the gap (re-collect
also wipes the `triage` on `issues/`).

§19's flow diagram omits a **re-triage** step that ADR-0001's gate formula makes
mandatory: after re-collect, reappeared P0/P1 issues have `triage is None`, so the
gate FAILs on "untriaged" unless the human re-triages first. §19 is
under-specified; this ADR amends it.

## Decision

1. **`issues/ISSUE-NNN.json` is the single source of truth for persisted issue
   state** - `status`, `triage`, `triage_history`, run-tracking fields. The
   `issue-bundle.json` entry is a **projection**: report-derived fields
   (severity, evidence, description, recommendation) + the projected persisted
   state. The collector merges both in one pass; the bundle never carries state
   that isn't also in `issues/`. The gate reads the projected `triage` from the
   bundle. (Confirmed: issues/ SoT, bundle projection.)

2. **Issue `status` field, four values:** `raised | triaged | resolved | reappeared`.
   The gate does **not** read `status` - it reads `severity` + `triage` (a
   `raised`-untriaged and a `reappeared`-untriaged issue both surface as
   `triage is None` -> FAIL). `status` is lifecycle bookkeeping for the
   collector / driver / `apply_triage`.

   State machine (transitions; who writes `status` / `triage` / `triage_history` /
   run fields):

   | transition | trigger | status | triage | triage_history | run field |
   | --- | --- | --- | --- | --- | --- |
   | -> `raised` | collector sees new fingerprint | collector | - | - | `first_seen_in_run` |
   | `raised` -> `triaged` | `apply_triage` | apply_triage | write current | append prior if any | - |
   | `triaged(request_fix)` -> `reappeared` | after its own fix run, re-collect, fingerprint still present | collector/driver | wipe -> None | append old | `fix_targeted_in_run` (driver) |
   | `triaged(request_fix)` -> `resolved` | after fix run, re-collect, fingerprint gone | collector | keep -> history | append | `resolved_in_run` |
   | `triaged(non-rf)` -> `resolved` | re-collect, fingerprint gone (fixed incidentally) | collector | keep -> history | append | `resolved_in_run` |
   | `triaged(non-rf)` -> `triaged` | re-collect, fingerprint still present, not fix-targeted | - (no change) | keep | - | `last_seen_in_run` |
   | `reappeared` -> `triaged` | `apply_triage` (re-triage; `request_fix` refused) | apply_triage | write current | append | - |

3. **Re-triage is a mandatory step** (amends §19 flow). After re-collect, the
   human must re-triage any `reappeared` issue before the lane gate can pass.

4. **Triage invalidation is scoped, not blanket.** Only a `request_fix` issue
   that reappears after **its own** fix run has its effective `triage` wiped
   (-> `reappeared`). Other dispositions persist across a fix run.

5. **"One round" is enforced by a feature-level `fix_loop_budget: {used, max: 1}`,
   written by the fix-loop driver.** `apply_triage` reads it and refuses
   `request_fix` once `used >= max`. This is the **sole** loop-count enforcer and
   it covers the regression case (a newly-`raised` P1 produced by the fix run)
   that `status=reappeared` does not. `status=reappeared`'s job is the
   **re-triage trigger** (gate sees `triage is None` -> FAIL), not loop-count
   enforcement - refusing `request_fix` on a `reappeared` issue via `status` would
   be redundant, since `reappeared` implies the budget is already used. Two
   distinct roles, not "double enforcement". (OQ6 resolved.)

6. **`resolved` is recorded by the collector diffing prior bundle vs new bundle.**
   A fingerprint present in the prior bundle but absent in the new one -> that
   `issues/ISSUE-NNN.json` gets `status: resolved` + `resolved_in_run`; its
   `triage` is preserved into `triage_history`. Caveat: "resolved" really means
   "not re-reported"; it depends on the reviewer not under-reporting. The defense
   is the gate's `verification_passed` condition (`lane_gate.py:133-147`) - a
   false `resolved` with a still-failing verifier still FAILs the gate.

7. **CP is a clean deferral, not a broken stub.** v0.3 has **no CP lifecycle** -
   zero caps, not half a cap. `request_cp` splits into two segments:
   - **Fix-loop CP exit (v0.3, testable):** `apply_triage(request_cp)` records
     `triage.action=request_change_proposal` + reason. Per ADR-0001's matrix,
     `request_cp` on P0/P1 still blocks, so the lane gate FAILs. This is one of
     §19:1167's terminal fix-failed decisions; the fix loop ends here. Fully
     v0.3-testable.
   - **CP fulfillment (v0.4):** CP-NNN creation -> approval -> re-freeze -> new
     implement cycle (with a fresh `fix_loop_budget`). This is a separate
     post-loop spec-change workflow, never part of the fix loop.

   Consequence: ADR-0001 #3's promotion row "`request_change_proposal` -> CP-NNN"
   is the **v0.4 target**. In v0.3, `apply_triage(request_cp)` records the
   disposition only and does **not** create CP-NNN. A feature with a `request_cp`'d
   P0/P1 sits at lane-gate FAIL pending v0.4 - a legitimate terminal state, not a
   broken one. (OQ8 resolved.) No total CP cap and no "1 open CP" serialization
   exist in v0.3 (no lifecycle to cap); both land in v0.4 with the CP writer.

8. **Fix-loop driver is a new v0.3 orchestration command `ai-dev fix-run
   <FEATURE> <LANE>`**, parallel to `ai-dev triage` (ADR-0001 #8). It runs the
   auto bookend `implement[fix] -> review -> spec-gap -> verify -> collect` and
   stops before re-triage (the human triages in the loop), then the existing
   `ai-dev lane-gate` closes. It is orchestration (calls model legs), not a pure
   writer. It writes, per fix-targeted issue, `fix_targeted_in_run`, and the
   feature-level `fix_loop_budget`. One fix run targets **all** `request_fix`
   issues in a single implement pass (one budget = one run).

9. **`fix_loop_budget.used` timing (OQ9).** The budget increments iff the fix
   run's implement leg produces a **§14-validated** implement-result (passes
   schema §14.1 + boundary §14.2 + frozen §14.3) - i.e., the run is not `failed`.
   This is the earliest point a fix materially happened (code changed within
   bounds). NOT on launch, NOT on re-collect/checking-done (waiting for
   re-collect would open a window where fix-valid + checking-crash allows a
   second fix on dirty code), NOT on issue-resolution. No budget consumed on:
   crash (no result); schema failure exhausted §24.3 auto-retry; boundary/frozen
   violation (run `failed`). §24.3's one-shot schema auto-retry is internal to
   one run and does not consume budget. Human relaunch after a §24.2 crash is a
   new fix run invocation, allowed iff (a) budget not consumed AND (b) worktree
   clean; a dirty worktree (boundary/frozen violation, or a file-modifying
   crash) requires **manual revert** (§24.4: no worktree self-heal) - relaunch
   alone is insufficient. The budget bounds *automatically-chainable valid
   fixing*; crashes/validation-failures are already caught by §24.2's human gate.
   Precision: "valid implement-result" = §14-validated (not schema-valid-only),
   so a boundary/frozen-violating fix is a failed run, not a budget-consuming fix.

## Resolved open questions

- **OQ6 -> Decision #5.** `fix_loop_budget` is the sole loop-count enforcer;
  `status=reappeared` is the re-triage trigger. Dropped the redundant
  status-based `request_fix` refusal.
- **OQ7.** §19:1167's fix-failed option list amended to
  `{override(P1), reject, request_cp, fail}` for P0/P1 (defer/accept remain
  P2/P3 bookkeeping; `reject` was missing from §19's list).
- **OQ8 -> Decision #7.** v0.3 stubs `request_cp` as a clean deferral (record +
  FAIL); CP-NNN lifecycle is v0.4.
- **OQ9 -> Decision #9.** Budget increments on §14-validated implement-result
  only; crashes / boundary / frozen failures don't consume it; dirty worktree
  needs manual revert (§24.4); §24.3 auto-retry is internal to a run.

## Consequences

- **Critical-path reordering:** the collector bundle-merge retrofit (Decision #1)
  is a prerequisite for *any* triage to affect the gate - not just fix loops. v0.3
  must land it before the triage/lane-gate path is exercisable end-to-end.
- v0.3 shape: `triage.py` + `ai-dev triage` (ADR-0001) + **collector retrofit**
  + **`ai-dev fix-run` driver** + `fix_loop_budget` writer + `lane_gate.py`
  retrofit + DEC writer + audit + tests. CP writer is v0.4.
- §19 flow amended: `... -> Re-review/Re-gap/Re-verify -> Re-collect ->
  **Re-triage** -> Lane Gate`.
- ADR-0001 #3's `request_cp -> CP-NNN` deferred to v0.4.

## Open questions

None - ADR-0002 is Accepted (OQ6-9 resolved). Issue lifecycle + fix loop are
fully specified; remaining v0.3 design questions (final report, feature-status
gate state machine) are scoped in ADR-0003 / Grill #3.
