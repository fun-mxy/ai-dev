# 04 — Lane dependency precheck and independent lane gates

**What to build:** Enforce lane dependency DAG preconditions without building a full scheduler (ADR-0009 D4). A lane cannot begin execution until every lane in its `depends_on` list has passed its lane gate. Lane gates are evaluated independently per lane; feature-level coherence/final reporting aggregates those lane results but does not merge branches. This ticket should remove hidden `LANE-001` assumptions from gate paths and make dependency failure explicit and actionable.

**Blocked by:** 01, 03.

**Status:** done

- [x] lane start precheck rejects a lane whose dependencies are missing, cyclic, failed, pending, or not yet gate-passed
- [x] lane gate evaluator accepts an explicit lane id and evaluates only that lane's implement/review/spec-gap/verification/triage evidence
- [x] `depends_on` cycles or unknown lane ids fail during validation with clear messages
- [x] downstream lane remains blocked until upstream lane gate pass, not merely Implementer `proposed_done`
- [x] feature-level helpers aggregate lane gate states without treating a passing lane as feature integration
- [x] tests cover no-dependency, satisfied dependency, unsatisfied dependency, missing lane, and cycle cases; `uv run mypy` + `uv run pytest` green
