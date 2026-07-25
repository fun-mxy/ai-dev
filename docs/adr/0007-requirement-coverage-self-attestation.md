# ADR-0007: Requirement coverage is agent self-attestation (required + §14-validated + spec-gap cross-check; no orchestrator inference)

- **Status:** Accepted
- **Date:** 2026-07-22
- **Supersedes / amends:** amends spec §2.1 (Q2 `requirement_coverage` / Q3 `acceptance_verification`) from "honestly empty in v0.4" to "populated from implementer declarations in v0.5." Relies on §13 (result-contract `related_requirements` / `related_acceptance_criteria`), §14 (validation), §9 (Spec Gap Analyst). Enables the v0.5 `compare-profiles` quality axis.

## Context

v0.4's `final-report` honestly left Q2/Q3 empty: the implementer (glm-5.2) did not declare
`related_requirements` / `related_acceptance_criteria`, even though the §13 result-contract slots and
the `final_report` computation (`_requirement_coverage` / `_acceptance_verification`) already existed
and correctly reported "0 requirements declared as covered." v0.5's `compare-profiles` needs a real
quality axis, which requires Q2/Q3 populated. The decision: how is coverage established - agent
self-declaration (the mechanism already built) or orchestrator inference (not built)?

## Decisions

### D1 - Mechanism: agent self-attestation (already built)

The implementer declares `related_requirements` / `related_acceptance_criteria` in `result.json`
(§13 slots, already present). `final_report`'s existing `_requirement_coverage` /
`_acceptance_verification` compute Q2/Q3 from those declarations. **No new computation** - the gap was
the agent not declaring, not the code not computing.

### D2 - Enforcement: required + §14 well-formedness

The implementer prompt **requires** the declaration; §14 validation checks it is present and
references real REQ/AC ids, **failing the run** if missing or malformed. It does **not** require
declaring *all* reqs - only the ones the lane addressed (a lane scoped to REQ-001 declares
`[REQ-001]`), so partial-scope lanes are not penalized.

### D3 - Honesty cross-check: the Spec Gap Analyst

The Spec Gap Analyst (§9, which already checks requirement↔code gaps) is the cross-check on whether
the declaration is honest. If the implementer overclaims (declares a REQ it did not address), spec-gap
raises a gap. No separate verification machinery is added.

### D4 - No orchestrator inference (rejected alternative)

The orchestrator does **not** infer coverage from `changed_files -> requirement` mappings or by
running ACs as executable tests. Rejected because (a) a req→file map does not exist and would be a new
artifact to maintain; (b) ACs are not currently executable; (c) self-declaration + spec-gap
cross-check reuses existing machinery and matches the §13 contract. Inference remains a possible
future enhancement if self-attestation proves unreliable.

## Consequences

- Q2/Q3 move from honestly-empty (v0.4) to populated-from-declarations (v0.5). `final-report`'s
  `known_gaps` note for Q2/Q3 retires on runs where the implementer declares.
- The `compare-profiles` quality axis is requirement coverage - a real signal, with the stated caveat
  that it is self-attested and spec-gap-cross-checked, **not** objectively verified.
- §14 gains a well-formedness check on traceability declarations - a new fail-loud validation rule.
- **Trust boundary, stated:** the orchestrator trusts the agent's self-declaration for coverage,
  validated for *form* by §14 and for *honesty* by the Spec Gap Analyst. A future reader who proposes
  objective inference should read D4 first.
