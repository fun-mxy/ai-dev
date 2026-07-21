# Glossary - 多 Agent Profile Orchestrator

Living ubiquitous-language for the orchestrator. Each entry locks a concept the
spec or code otherwise overloads. Pre-existing terms (Feature Run, Lane, Agent
Profile, Stable IDs, Issue, Change Proposal) are defined in spec §5 / §17; this
glossary only locks terms the spec **overloads or leaves ambiguous**, with a
pointer to the ADR that established the lock.

| Term | One-line | Established |
| --- | --- | --- |
| **Decision** (artifact) | A cross-cutting, rationale-bearing engineering judgment, persisted as `decisions/DEC-NNN.{json,md}`. The unit of "judgment worth referencing later" (§2.4). Not a per-issue disposition, not a gate verdict. | ADR-0001 |
| **Disposition** (triage) | The Human-Triage verdict on one `ISSUE-NNN`: `accept \| reject \| defer \| override \| request_fix \| request_change_proposal` (§16). Lives as the `triage` state object on the issue, **not** as a Decision. Renamed from the spec's overloaded `"decision"` field. | ADR-0001 |
| **Verdict** | The pass/fail result a gate computes deterministically - e.g. `lane-decision.json` `verdict: pass\|fail` (§18.4). Renamed from the code's overloaded `decision` field. | ADR-0001 |
| **Disarming** | Making a blocking issue (P0/P1) non-blocking, via `override` or `reject`. Always produces a Decision (proposed invariant #15). | ADR-0001 |
| **Invariant #5 (clarified)** | P0 cannot be waived by a mere reason; it can only be disarmed by a recorded Decision (`reject`-as-false-positive). The `override` disposition on P0 is forbidden entirely. (Spec §29 states only the letter - "P0 不可 override" - which this clarifies without contradicting.) | ADR-0001 |
| **Issue status** | Lifecycle state of an `ISSUE-NNN`: `raised \| triaged \| resolved \| reappeared`. Bookkeeping for the collector / fix-loop driver / `apply_triage`; the gate does **not** read it (it reads `severity` + `triage`). | ADR-0002 |
| **Fix-loop budget** | Feature-level `fix_loop_budget: {used, max: 1}`. The sole enforcer of §19's "at most one fix run" - `apply_triage` refuses `request_fix` once `used >= max`. Per implement-cycle (a CP resets it). | ADR-0002 |
