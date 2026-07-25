# 05 — Capstone: pure intent → final-report, zero hand-authored planning, real cc-glm52/Ark `verdict=pass` (ADR-0008)

**What to build:** The v0.6 milestone capstone — the full SDD loop end-to-end with **all planning
model-generated, none hand-authored** (closes the v0.4 gap that ADR-0008 Context names). Drive one
feature from `create-feature-run "<intent>"` through: `generate-requirements` → freeze →
`generate-design` → freeze → `generate-tasks` → freeze → the existing implement → review → gap →
verify → lane-gate → coherence-gate → `final-report`. Every planning artifact (requirements, design,
tasks, lane-graph) is produced by the Planner + `promote`, refined via feedback where needed, and
frozen by a human gate — **no human authors REQ/AC/DES/TASK content**. The run must reach
`verdict=pass` on **real cc-glm52/Ark** (the [[e2e-tickets-need-real-ark-run]] bar — the pytest
fake-claude path is not sufficient). **ID-scheme alignment (cross-milestone seam):** the model-generated
REQ/AC/DES/TASK ids must flow cleanly into ADR-0007's coverage self-attestation in `final-report`
(`related_requirements` / `related_acceptance_criteria`) — verify the final report's Q2/Q3 coverage
populates from the Planner-originated ids, not hand-written ones. Record evidence under
`.scratch/ai-dev-v0-6-planner/evidence/`.

**Blocked by:** 02, 03, 04 (all three planning gates must exist and chain into the existing implement→…→final-report flow).

**Status:** done

- [x] one feature driven intent → … → final-report with **zero hand-authored** planning content
- [x] all four planning artifacts Planner-generated + promoted + human-frozen
- [x] `verdict=pass` on real cc-glm52/Ark (not the fake-claude test)
- [x] ADR-0007 coverage self-attestation in `final-report` populates from the Planner-originated ids (Q2/Q3)
- [x] evidence recorded under `.scratch/ai-dev-v0-6-planner/evidence/`; `uv run mypy` + `uv run pytest` green
