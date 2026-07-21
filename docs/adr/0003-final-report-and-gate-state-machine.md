# ADR-0003: Final report + feature-status gate state machine

- **Status:** Accepted (OQ10-12 resolved)
- **Date:** 2026-07-20
- **Supersedes / amends:** amends spec §8.3 (line 513, 519-520), §18.5 (line 1142); relies on ADR-0001 (Verdict ubiquitous language) and ADR-0002 (issue lifecycle / fix-loop budget)
- **v0.3 scope:** final report (the last unfinished item in §26.4's v0.3 list) + the gate-state-machine that v0.1/v0.2 left unwired

## Context

v0.1/v0.2 shipped the status writer (`src/ai_dev/status.py`) with three mutating primitives - `freeze_artifact` (line 187), `set_current_gate` (line 222), `mark_task_proposed_done` (line 331) - but **only the first half of each is wired**:

- `freeze_artifact` flips the frozen flag but does **not** advance `current_gate`; after freezing all three human-gate artifacts, `current_gate` is still `requirements_gate`.
- `set_current_gate` exists as a low-level primitive whose docstring explicitly defers sequencing to "later tickets" (`status.py:229-231`) - v0.3 is that ticket. Nothing calls it.
- `final_verdict` (§8.3 line 520, `status.py:90`) is initialized to `null` and **no code ever writes it**.
- `feature.status` (§8.3 line 513, `status.py:87`) is initialized to `"planning"` and has **no defined transition** - it stays `"planning"` forever.

Separately, the coherence gate (§18.5) and the final report (§23.5 step 21) are undesigned in the spec: §18.5 lists four coherence conditions but §23.5 generates the final report *after* the coherence gate, and the final-report file is named in three places (lines 357, 1265, 1361) with **no schema defined anywhere**.

Two spec defects surfaced during grilling:

1. **§18.5 vs §23.5 ordering contradiction.** §18.5 (line 1142) lists "final report 是否完整" as a coherence-gate *condition*, but §23.5 (lines 1360-1361) puts the final report at step 21, *after* the coherence gate at step 20. The coherence gate cannot verify an artifact that does not exist yet. Same forward-reference pattern as the §19 re-triage gap (ADR-0002).
2. **`final_verdict` is a write-only-on-init field.** §8.3 declares it; no writer exists. And the name "final" implies immutability, which conflicts with the re-coherence-after-fix-loop flow (a feature can fail coherence, run a fix, then re-pass).

## Decisions

### D1 - §18.5 amend: drop "final report 是否完整" (Q3.1)

The coherence gate (step 20) verifies **inputs**, not the report artifact. The final report (step 21) is generated **downstream** from coherence-gate-PASS, consuming the verdict. §18.5 line 1142 "final report 是否完整" is a forward-reference error - **delete it**. The remaining three conditions are the input checks:

- final status 是否一致 (current_gate / verdict / feature.status mutually consistent - automatic once D3 lands);
- 所有 P0/P1 是否处理 (every P0/P1 issue is resolved or disarmed per ADR-0001's promotion rule);
- decisions 是否记录 (every disarming of a blocking issue has a `DEC-NNN` per ADR-0001's invariant #15).

The draft→finalize alternative (final report drafted before coherence, verified by coherence, then finalized) is **rejected on principle**: it is non-monotonic and over-engineered for single-lane v0, and it duplicates the coherence verdict into a separate "draft verdict". Amend §18.5 only; §23.5 step ordering (20 then 21) is correct as-is.

### D2 - Gate state machine: wire `set_current_gate` atomically, Model B (Q3.2)

Adopt **Model B**: the gate advance is coupled **atomically into the gate's completing operation** (freeze / coherence-eval), not a separate orchestrator-level `advance_gate()` call. `set_current_gate` (`status.py:222`) is the primitive; v0.3 wires it into exactly two writers:

- **3 human gates (§18.1-18.3):** `freeze_artifact` atomically advances `current_gate` to the next stage on freeze. freeze is monotonic and is the human-gate *pass signal*, so coupling advance to it is safe (no re-freeze, no un-advance).
- **Coherence gate (§18.5, terminal):** the coherence evaluator atomically calls `set_current_gate(feature_coherence_gate)` and writes `verdict` (D4) in the same mutation.

**Lane gate (§18.4) does NOT touch `current_gate`.** Rationale: the lane gate is re-runnable - triage, the bounded fix loop, and re-collect can all re-evaluate it within one feature run (ADR-0002). Coupling advance to lane-gate-eval would advance on first pass and then need to "un-advance" on a re-fail; instead, `current_gate == lane_gate` spans the entire lane-gate + triage + fix phase, and the only terminal advance is into `feature_coherence_gate` when coherence actually runs (which by construction means the lane is resolved).

`current_gate` transition table:

| from | to | trigger | writer |
|---|---|---|---|
| `requirements_gate` (init) | `design_gate` | freeze(requirements) | `freeze_artifact` (atomic) |
| `design_gate` | `task_gate` | freeze(design) | `freeze_artifact` (atomic) |
| `task_gate` | `lane_gate` | freeze(tasks) | `freeze_artifact` (atomic) |
| `lane_gate` | `lane_gate` | lane gate eval (pass *or* fail) | (no write - lane gate doesn't touch) |
| `lane_gate` | `feature_coherence_gate` | coherence eval | coherence evaluator (atomic, +verdict) |

`freeze(lane_graph)` is part of the task gate (§4.2 freezes requirements/design/tasks/**lane-graph** at the human gates) and does **not** separately advance `current_gate` - the advance already happened on `freeze(tasks)`. The artifact-name → gate mapping and freeze ordering (tasks before lane_graph) is an implementation detail for the ticket.

### D3 - `feature.status` subsumed as a derived projection (Q3.2)

`feature.status` (§8.3 line 513) is **not an independent mutation target**. It is a *derived projection* of `(current_gate, verdict)`, recomputed atomically whenever `current_gate` or `verdict` is written (i.e. inside the same `freeze_artifact` / coherence-evaluator mutation that advances the gate or writes the verdict). Derivation map:

| `verdict` | `current_gate` | `feature.status` |
|---|---|---|
| `pass` | (any) | `done` |
| `fail` | (any) | `blocked` |
| `null` | `requirements_gate` / `design_gate` / `task_gate` | `planning` |
| `null` | `lane_gate` | `implementing` |
| `null` | `feature_coherence_gate` | *(unreachable - see note †)* |

† The `(verdict=null, current_gate=feature_coherence_gate)` cell is a disk-never-exposed transient: D2 has the coherence evaluator atomically write `current_gate = feature_coherence_gate` **and** `verdict` in the same mutation, so the `(fcg, null)` state is never observable on disk and produces no projection value. `verifying` was considered and rejected for this reason - it would name an unreachable state.

Two scope notes on the four values:

- (a) **`blocked` is strictly coherence-fail.** It does **not** cover lane-gate blocking: the lane verdict lives in `lane-decision.json` and is never written back to `feature-status.yml` (D4 - coherence evaluator is the sole `verdict` writer). When the lane gate FAILs, `current_gate` is still `lane_gate` and `verdict` is still `null`, so `feature.status` is still `implementing` - the feature is mid-implementation/fix, not blocked-at-coherence.
- (b) **Ticket must add cell-coverage assertion tests** - for each `(verdict, current_gate)` cell above, assert the projected `feature.status`; and explicitly assert the unreachable † cell is never produced by any writer.

Rationale: single source of truth for gate state. `feature.status` is a human-readable projection of gate state, never an independent field - so it can never drift from `current_gate`/`verdict`. The field stays in §8.3 (humans read the YAML) but is always derived; the spec example showing `status: planning` remains valid (it is the `verdict=null, current_gate=requirements_gate` projection at init).

### D4 - `final_verdict` → `verdict` rename + mutability (Q3.2)

- **Rename** `final_verdict` → `verdict` on `feature-status.yml` (§8.3 line 520; `status.py:90` and `_initial_feature_status`). This is **consistent with ADR-0001's Verdict ubiquitous language** - "*the pass/fail result a gate computes deterministically*" (glossary row 13). `lane-decision.json` has `verdict` (the lane gate's verdict); `feature-status.yml` has `verdict` (the coherence gate's verdict). Each gate's verdict lives on its own artifact - no field collision. The `final_` prefix is dropped because (a) it falsely implied immutability (below), and (b) it was a redundant qualifier - it is just "the coherence gate's verdict", parallel to "the lane gate's verdict".
- **Mutable.** `verdict` is re-evaluated each coherence run. A feature can go `null` → `fail` (first coherence, e.g. a P1 unresolved) → [`fix-run` per ADR-0002] → `pass` (re-coherence overwrites `fail`). This mirrors `lane-decision.json`'s `verdict`, which is re-evaluated each lane-gate run. The coherence evaluator is the **sole writer** of `feature-status.yml.verdict`; the final-report generator **reads** it and never writes (D5).

### D5 - Final report: deterministic JSON + skeleton MD, no narrative in v0.3 (Q3.4)

- **`final-report.json` (canonical):** a deterministic script aggregation of `issues/` + `decisions/` + `runs/` + `audit.log` + `status/`. Answers §2.1's five audit questions (lines 38-42) *structurally*. Generator = **deterministic script, not a model** (invariant #2 / §4.3: canonical status is script-written; `final-report.json` is canonical). Persisted to disk but **re-computable** - regenerating from the same artifacts yields the same JSON. **Not a frozen artifact**: §4.2's frozen set is {requirements, design, tasks, lane_graph}; final-report is deliberately excluded (it is a projection, not a spec).
- **`final-report.md` (human-readable):** a deterministic skeleton rendered **from** `final-report.json`. v0.3 ships JSON + skeleton MD **only** - **no narrative hook**: no model-generated section, not even an empty/stub one.
- **Spec/model isolation in the MD:** structure the MD so that deterministic spec-content (rendered from JSON) and future model-content (narrative) occupy **separate sections**. v0.3 leaves the narrative section *absent* (not stubbed). When narrative is added (v0.4+), it lands in an isolated section explicitly marked non-canonical, so a reader can never confuse model prose for canonical audit fact.

**`final-report.json` top-level skeleton (pinned now; inner field enumeration deferred to ticket):**

The top level is keyed by §2.1's five audit questions (lines 38-42) - mechanically checkable as "five keys present":

```json
{
  "meta":                    { ... },
  "verdict":                 "pass|fail",
  "code_to_requirement":     [ ... ],   // §2.1 Q1: which code corresponds to which requirement
  "requirement_coverage":    [ ... ],   // §2.1 Q2: is a requirement implemented
  "acceptance_verification": [ ... ],   // §2.1 Q3: what verifies an acceptance criterion
  "issue_dispositions":      [ ... ],   // §2.1 Q4: why was an issue accepted/rejected/overridden
  "agent_timeline":          [ ... ],   // §2.1 Q5: which profile did what when
  "failure_class":           "recoverable|terminal|null",  // D6; null when verdict==pass
  "blocking_reasons":        [ ... ]    // D6; [] when verdict==pass
}
```

Four generation constraints pinned now (per-section inner fields are ticket scope):

1. **Failure-shape is the auditable carrier of D6's classification** - `verdict` + `failure_class: recoverable|terminal` + `blocking_reasons[]`, where each blocking reason carries `issue_id` / `kind` / `resolution_path`. Present whenever `verdict == fail`; `null`/`[]` when `verdict == pass` (per constraint 2).
2. **Stable enumeration, keys always present, values may be empty.** All multi-value inputs are enumerated by stable key sort. Every top-level key is always present; values may be empty arrays/objects. This lets a validator distinguish *"absent because empty"* (legitimate) from *"absent because corrupt"* (§24.2) - the concrete mechanism for D6's defensive-on-optional.
3. **The code->requirement traceability index (Q1) must exist.** If v0.3's `runs/` do not carry changed-files, the index is left **explicitly empty as a known gap** (marked in `meta.known_gaps`) - never silently omitted. A missing Q1 key reads as corruption (constraint 2); an empty Q1 key reads as "v0.3 doesn't yet collect this".
4. **Per-section inner field enumeration is deferred to the ticket.** The ADR pins the skeleton and the four constraints above; it does not enumerate fields inside each of the five sections.

### D6 - FAIL report exists; recoverable vs terminal; defensive generation (Q3.3)

- **final report is generated for both `pass` and `fail`** verdicts (auditable record either way). The coherence `verdict` (D4) gates the *content*, not the *existence*, of the report.
- **`final-report.json` distinguishes recoverable vs terminal failure:**
  - **recoverable** - blocked pending an action that can unblock *without* changing frozen specs: pending CP fulfillment (v0.4 lifecycle), pending human triage, fix-budget exhausted but a CP path is available.
  - **terminal** - cannot reach `pass` within v0.3 without a Change Proposal v0.3 cannot fulfill: P0 rejected (disarmed, won't-fix), `request_cp` recorded as FAIL (clean deferral per ADR-0002 - v0.3 records `request_cp` + FAIL, no `CP-NNN` lifecycle), frozen-spec conflict requiring the CP lifecycle (v0.4).
- **Reinforce with §2.1:** the FAIL report still answers all five audit questions - the answers just include "blocked because …" (e.g. "ISSUE-003 P0 rejected, DEC-002, pending CP fulfillment (v0.4)").
- **Defensive generation:** the generator must not crash on missing *optional* artifacts (no `decisions/` if no triage happened; no fix-loop runs if no fix ran; no `ISSUE-NNN.json` if the bundle is empty). *Required* artifacts (issue bundle, `lane-decision.json`, `feature-status.yml`) must exist - their absence is a generator error (fail-loud, §24.2), not a silent empty report. The JSON mechanism for this is D5 constraint 2 (keys always present, values may be empty).
- **Failure-shape carrier:** the `failure_class` + `blocking_reasons[]` fields (D5 constraint 1) are the auditable carrier for this classification - each `blocking_reason` carries `issue_id` / `kind` / `resolution_path`, so a reader sees *what* blocks and *how* it could unblock.

### D7 - Two deterministic commands: `coherence-gate` then `final-report` (Q3.2/Q3.3)

Mirroring §23.5 step 20 → step 21, v0.3 ships **two** deterministic commands (not one), so the verdict writer and the report generator are separable and independently re-runnable:

- **`ai-dev coherence-gate`** (step 20): the coherence evaluator. Deterministically checks the three D1 input conditions, atomically writes `current_gate = feature_coherence_gate` + `verdict` (D2/D4) + derived `feature.status` (D3). Audited.
- **`ai-dev final-report`** (step 21): reads `verdict`, generates `final-report.json` + skeleton `final-report.md` (D5/D6). Does **not** write `verdict` or `current_gate` - pure projection. Can be re-run to regenerate the report without re-evaluating coherence.

This split keeps the canonical-state writer (`coherence-gate`) and the projection writer (`final-report`) as separate concerns, consistent with invariant #2's "canonical status only written by deterministic scripts" and ADR-0002's `ai-dev fix-run` pattern (independent, auditable driver command).

Three supplements (OQ12):

- (a) **D7 is contingent on D5.** The decisive reason for two commands is that the report is a *re-computable projection, non-canonical* (D5) - so re-running `final-report` is a non-state-changing render. If a future version (v0.4+) freezes `final-report.json` as a canonical artifact, this contingency breaks and the split must be revisited (a frozen report would acquire audited-write semantics, eroding the distinction in (b)).
- (b) **The two commands have different audit semantics - this is the substantive argument, not mere separability.** Re-running `coherence-gate` is an *audited canonical verdict write* (appends an audit event, mutates `feature-status.yml`); re-running `final-report` is a *non-audited projection render* (writes/overwrites `final-report.{json,md}`, touches no canonical state, appends no audit event). Merging them would conflate a canonical-state mutation with a pure render behind one command.
- (c) **`final-report` requires a non-null `verdict`.** If `verdict == null` (coherence has not run), `ai-dev final-report` **fail-loud refuses** (§24.2) rather than producing a meaningless report over a null verdict. The report is downstream of coherence (D1); a null verdict means coherence has not produced a verdict to consume.

## Spec amends (to apply in the v0.3 spec-update ticket)

1. **§18.5 (line 1142):** delete the bullet "- final report 是否完整；". The coherence gate verifies inputs, not the report artifact (D1).
2. **§8.3 (line 520):** rename `final_verdict: null` → `verdict: null` (D4).
3. **§8.3 (line 513):** annotate `status: planning` as a *derived projection* of `(current_gate, verdict)` per D3's table (the example value stays `planning`; the field gains a "derived, not independently set" note).
4. **§23.5 (steps 20-21):** no ordering change; annotate step 20 as "writes `current_gate` + `verdict`" and step 21 as "reads `verdict`, generates report" (D7), to make the producer/consumer relationship explicit.

## Open questions

None - ADR-0003 is Accepted.

## Resolved

- Q3.1 (§18.5 vs §23.5 ordering): §18.5 forward-reference deleted; final report is downstream of coherence verdict (D1).
- Q3.2 (gate state machine): `set_current_gate` wired atomically into `freeze_artifact` (3 human gates) + coherence evaluator (terminal + verdict); lane gate does not touch `current_gate`; Model B (advance at completion time) (D2). `feature.status` subsumed as derived projection (D3). `final_verdict` -> `verdict`, mutable, coherence-evaluator-written (D4).
- Q3.3 (FAIL report): exists for both pass/fail; recoverable-vs-terminal classification in JSON; defensive on optional artifacts (D6).
- Q3.4 (generator split): deterministic JSON + skeleton MD, no narrative in v0.3; spec/model isolation in MD; persisted but re-computable; not frozen (D5).
- OQ10 (`feature.status` values): pinned to `planning` / `implementing` / `done` / `blocked`; `verifying` deleted as unreachable (D3 note †); `blocked` is coherence-fail only, lane-gate FAIL stays `implementing` (note a); cell-coverage + unreachable-cell assertion tests required in ticket (note b).
- OQ11 (`final-report.json` skeleton): top level keyed by §2.1 five questions + `meta`; four generation constraints pinned (failure-shape carrier, stable-enumeration/keys-always-present, Q1 traceability index must-exist-or-explicit-gap, inner fields deferred to ticket) (D5).
- OQ12 (two commands): D7 confirmed; contingent on D5 (supplement a), substantive audit-semantics argument (supplement b), `final-report` fail-loud refuses on null `verdict` (supplement c).
