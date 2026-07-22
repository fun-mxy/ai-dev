# 05 - Q2/Q3 traceability closure (ADR-0007)

**What to build:** Close the v0.4 "Q2/Q3 honestly empty" gap per ADR-0007. The machinery already exists
(§13 result-contract slots for `related_requirements`/`related_acceptance_criteria`; `_requirement_coverage`/
`_acceptance_verification` in `final_report.py`); the gap is the implementer not declaring. (1) Update
the implementer prompt to **require** declaring `related_requirements`/`related_acceptance_criteria`
(the REQs/ACs the lane actually addressed - not all reqs, so a partial-scope lane declares just its own).
(2) Add a §14 well-formedness validation: declaration present + references real REQ/AC ids; **fail the
run** if missing/malformed (D2). (3) Verify `final_report`'s Q2/Q3 populate from the declarations (no
new computation - D1). The Spec Gap Analyst remains the honesty cross-check, unchanged (D3). Retire
`final_report`'s Q2/Q3 `known_gaps` note on runs that declare. **No orchestrator inference** (D4 - the
rejected alternative; record why: a req->file map doesn't exist, ACs aren't executable, self-declaration
+ spec-gap reuses existing machinery). Profile-agnostic - works for both claude and codex.

**Blocked by:** none - independent (implementer prompt + §14 + `final_report`; profile-agnostic).
Ideally lands before 04 so 04 evidences populated Q2/Q3.

**Status:** pending

- [ ] implementer prompt requires `related_requirements`/`related_acceptance_criteria`
- [ ] §14 well-formedness check: present + real REQ/AC ids; fail if missing/malformed (D2)
- [ ] `final_report` Q2/Q3 populate from declarations (existing compute; verify) (D1)
- [ ] Spec Gap Analyst cross-check unchanged (D3)
- [ ] `known_gaps` Q2/Q3 note retires on declaring runs
- [ ] no orchestrator inference (D4 - rejected alternative, documented)
- [ ] tests: declaring run -> populated Q2/Q3; non-declaring -> §14 fail
- [ ] `uv run mypy` + `uv run pytest` green
