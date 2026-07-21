# ADR-0001: Triage disposition lives on the issue; disarming a blocking issue always produces a Decision

- **Status:** Accepted (OQ1-5 resolved). OQ4 - issue lifecycle across the fix loop -
  resolved by ADR-0002 (Decisions #1 / #2 / #3: `issues/` as SoT + bundle
  projection; `status` four-value state machine with writer per transition;
  mandatory re-triage step).
- **Date:** 2026-07-20
- **Spec:** §4.3, §5.2, §6, §15.2, §16, §17, §18.4, §2.4; invariants #2, #5, #6, #14
- **Supersedes:** none

## Context

v0.3 introduces Human Triage (§16) and the P0/P1/P2/P3 gate rule with override
(§15.2). The spec uses "decision" for three distinct concepts and never defines
where a triage verdict lives:

1. `decisions/DEC-NNN` - a cross-cutting, rationale-bearing engineering decision
   artifact (§6, §5.2, invariant #14, §2.4 digital-twin goal).
2. §16 triage `"decision": "override_issue"` - a per-issue disposition.
3. `lane-decision.json` `"decision": pass|fail` - a gate verdict (§18.4, already
   implemented in `lane_gate.py`).

`decisions/` and `issues/` are both empty skeletons today (`feature_run.py`
`_EMPTY_SKELETON_DIRS`), so triage is the first writer. The language must be
locked now or the collision bakes into the file format.

## Decision

1. **Rename to resolve the triple-collision** (`docs/glossary.md`):
   - `decisions/DEC-NNN` keeps the name **Decision** (artifact).
   - §16 triage `"decision"` -> **disposition** (field `triage.action`). Spec §16 edit + code.
   - `lane-decision.json` `"decision"` -> **verdict**. Code (`lane_gate.py`) + tests.
   Both renames are pure disambiguation, no semantic change.

2. **Disposition lives on the issue.** `triage` is a state object on
   `ISSUE-NNN.{json,md}`, not a standalone artifact. Satisfies invariant #14
   (issue is already double-product) and lets the gate read `issue.triage`
   without scanning `decisions/`.

3. **Promotion rule - lookup on (disposition x severity), not effect.** A Decision
   is produced iff the disposition *disarms a blocking issue*:
   - `override` x P1 -> DEC (P0 override refused, §5; P2/P3 n/a)
   - `reject` x {P0, P1} -> DEC
   - `reject` x P2/P3 -> no DEC (reason optional)
   - `request_change_proposal` -> CP-NNN (§17), not a DEC
   - `accept` / `defer` / `request_fix` -> no DEC
   This is a 2D matrix lookup, still mechanically decidable at write time - not
   the fuzzy "does this change the verdict" judgment. (Supersedes the earlier
   "reject never produces a DEC" justification: §2.4's stakes argument won - a
   P0/P1 false-positive denial is as capture-worthy as a real-issue waiver.)

4. **Disposition x severity legality matrix** (resolves OQ1 - chose (a): defer/accept
   are non-gate bookkeeping, legal only on P2/P3; on P0/P1 illegal and refused at
   the write layer; §15.2 untouched):

   | disposition | P0 | P1 | P2/P3 |
   | --- | --- | --- | --- |
   | `request_fix` | legal, still blocks | legal, still blocks | legal (never blocked) |
   | `request_change_proposal` | legal, still blocks | legal, still blocks | legal |
   | `override` | **refused (#5)** | legal, non-blocking + reason + DEC | n/a (already non-blocking) |
   | `reject` | legal, non-blocking + reason + DEC | legal, non-blocking + reason + DEC | legal, non-blocking (reason optional, no DEC) |
   | `defer` | illegal | illegal | legal, bookkeeping (no gate effect) |
   | `accept` | illegal | illegal | legal, bookkeeping (no gate effect) |

5. **Lane-gate blocking formula** (resolves OQ1; *corrected from the author's
   draft*, which said "severity == P0 -> 恒阻塞" unconditionally and contradicted
   the matrix. The two-layer defense targets `override`-on-P0 specifically, not
   every P0 disposition - `reject`-on-P0 is the legitimate non-blocking escape):

   ```
   issue blocks the gate iff
       (severity in {P0, P1} and triage is None)                          # untriaged blocking issue
       or (severity == P0 and triage.action in {request_fix, request_change_proposal})
       or (severity == P1 and triage.action in {request_fix, request_change_proposal})
       or (severity == P0 and triage.action == override)                  # DEFENSE: forbidden disposition
                                                                          #   sneaking in is treated as blocking
   # non-blocking: P0 x {reject}, P1 x {override, reject}, all P2/P3
   # P0/P1 x {defer, accept}: illegal -> refused at write layer; if one appears, fail-loud
   gate FAIL iff  any issue blocks  OR  any P0/P1 is untriaged (§18.4 "issue triage complete")
   ```

6. **`reject` requires a reason when it disarms.** `reject` on P0/P1 requires a
   reason AND produces a DEC (Decision #3); without the reason requirement,
   `reject` would be a DEC-free escape hatch. `reject` on P2/P3 needs no reason
   (nothing to disarm) and produces no DEC.

7. **P0 `override` refusal - two-layer defense** (resolves OQ2). Write layer
   (`apply_triage`) refuses + writes an audit `triage_refused` event + non-zero
   exit, issue stays untriaged; gate layer treats `P0 x override` as blocking
   (Decision #5) as defense against hand-edit/bug. Not a denied-DEC (a denied
   attempt is not a decision -> audit.log per §6, not `decisions/`). Not a run
   failure (illegal input != runtime failure; §24.2 fail-loud is for subprocess
   crashes / schema violations, not human input errors).

8. **Triage write path - deterministic `apply_triage`** (resolves OQ3). New
   `src/ai_dev/triage.py`: `apply_triage(repo_root, feature_id, issue_id, action,
   reason, by) -> TriageResult`. Pure, no model, schema-validating; enforces the
   matrix + reason-presence + promotion + audit in one place - same pattern as
   `evaluate_lane_gate` / `validate.py`. CLI surface: `ai-dev triage ISSUE-001
   --action ... --reason ... [--by human]`. Models may *propose* triage (§4.3
   line 142, non-canonical); only the human-triggered deterministic command
   writes canonical `triage` (propose vs apply split). **This is new v0.3 scope**
   - §26.4's bullets don't name it, but invariant #2 forces it. Amends §26.4.

### Reference data model

```jsonc
// issues/ISSUE-001.json - disposition always lives here
{
  "id": "ISSUE-001",
  "severity": "P1",
  "triage": {
    "action": "override_issue",      // accept|reject|defer|override|request_fix|request_cp
    "reason": "Known limitation acceptable for MVP v0 …",  // required: override (#6) / reject on P0|P1
    "by": "human",
    "ts": "…",
    "decision_ids": ["DEC-007"]      // present only when promoted (override x P1, reject x P0|P1)
  }
}

// decisions/DEC-007.json - produced when a blocking issue is disarmed
{
  "id": "DEC-007",
  "kind": "p1_override",             // p1_override | p0_reject | p1_reject | design | gate_policy | …
  "title": "Accept unhandled malformed result.json for MVP v0",
  "rationale": "Input package is generated by trusted wrapper; hardening deferred to v0.4.",
  "triggered_by_issue": "ISSUE-001",
  "status": "accepted",
  "by": "human",
  "ts": "…"
}
```

## Resolved open questions

- **OQ1 -> Decisions #4 / #5 / #6.** Chose (a): `defer`/`accept` are non-gate
  bookkeeping (legal only on P2/P3); on P0/P1 illegal (refused at write layer).
  `accept` on P1 is redundant with `request_fix`. §15.2 untouched.
- **OQ2 -> Decision #7.** Refuse-write + audit + non-zero exit; two-layer defense.
- **OQ3 -> Decision #8.** Deterministic `apply_triage` command; new v0.3 scope.
- **OQ4 -> ADR-0002 Decisions #1 / #2 / #3.** Issue lifecycle across the fix loop
  is fully specified there: `issues/ISSUE-NNN.json` is the single source of truth
  (bundle is a projection); `status` four-value state machine
  (`raised | triaged | resolved | reappeared`) with the writer per transition;
  re-triage is a mandatory step after re-collect. (Resolved in ADR-0002, which
  re-numbered the follow-on questions OQ6-9.)
- **OQ5a -> Decision #5.** Confirmed: gate-layer defense blocks only
  `P0 x override`; `P0 x reject` is the legitimate non-blocking escape (author's
  draft "P0 恒阻塞" was an overcorrection that would have blocked rejected P0s).
- **OQ5b -> Decision #3.** Chose (a) escalation: `{override, reject} x {P0, P1}`
  -> DEC. "Disarming a blocking issue always leaves a Decision."

## Consequences

- v0.3 real shape: `triage.py` + `ai-dev triage` subcommand + `lane_gate.py`
  retrofit (consume `triage`) + DEC writer + audit events + tests.
- Lane gate gains a prerequisite: all P0/P1 must be triaged (else FAIL). Flow
  ordering: implement -> review/gap/verify -> bundle -> **triage** -> lane gate.
- `reject` carries double semantics ("not a real bug" vs "mis-severitized"); the
  final-report renderer should surface the reason to disambiguate.
- Re-triage overwrites the `triage` object; prior disposition survives only in
  `audit.log` (relevant to the fix loop - see ADR-0002).
- **Proposes spec amendments** (fold into §29 / glossary when v0.3 lands):
  - A new invariant #15: *"Disarming a blocking issue (P0/P1) always produces a
    Decision."*
  - A spirit-faithful restatement of #5: *"P0 cannot be waived by a mere reason;
    it can only be disarmed by a recorded Decision (`reject`-as-false-positive).
    The `override` disposition on P0 is forbidden entirely."*

## Open questions

None - ADR-0001 is Accepted (OQ1-5 resolved). OQ4 (issue lifecycle across the
fix loop), which was deferred here to ADR-0002 / Grill #2, is now resolved by
ADR-0002 Decisions #1 / #2 / #3. Remaining v0.3 design questions (final report,
feature-status gate state machine) are scoped for ADR-0003 / Grill #3.
