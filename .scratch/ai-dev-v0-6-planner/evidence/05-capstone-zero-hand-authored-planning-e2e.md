# 05 - v0.6 Capstone: zero hand-authored planning -> intent … final-report, real cc-glm52 / Ark `verdict=pass`

**Date:** 2026-07-23 (run timestamps UTC: requirements RUN-001 11:26:36-11:27:47Z, design RUN-002
11:27:55-11:29:52Z, tasks RUN-003 11:29:56-11:33:05Z, implement RUN-006 11:43:01-11:45:25Z, review
RUN-007 11:45:30-11:49:06Z, spec-gap RUN-008 11:49:15-11:51:51Z, verify 11:51:58-11:51:59Z shell;
UTC+8 local ~19:26 onward).
**Target:** `examples/string-utils/` - a **fresh** `FEATURE-004` (same greet intent as FEATURE-003)
driven from `create-feature-run` through `final-report`. FEATURE-003 is left as the 02/03/04 evidence
(its lane-graph froze before the verify-command work landed and cannot be re-promoted); this capstone
re-drives the whole loop so every artifact - including the verify command set - is model-generated.
**Profile:** Planner = `cc-glm52` (claude CLI headless; GLM provider via injected
`ANTHROPIC_BASE_URL` -> `glm-5.2`; ADR-0008), resolved through `role_defaults[planner] = cc-glm52`.
Implementer = `codex-default`; reviewer / spec-gap = `cc-glm52`. Every Planner metadata.json records
`profile=cc-glm52 model=glm-5.2 backend=glm` - a **real** Ark backend, not the pytest fake-claude path
([[e2e-tickets-need-real-ark-run]] bar).
**Token:** resolved through `auth_env_fallback = ANTHROPIC_AUTH_TOKEN` (token-by-env-var-name only,
never persisted; token-safety invariant #11 holds).
**Verdict:** `verdict=pass` end-to-end. **All four planning artifacts (requirements, design, tasks,
lane-graph) are Planner-generated + `promote`-stitched + human-frozen - zero hand-authored REQ/AC/DES/
TASK/verify content.** The `final-report.json` Q2 (`requirement_coverage`) and Q3
(`acceptance_verification`) populate from the Planner-originated `REQ-001/002/003` and
`AC-001/002/003` ids.

## The zero-hand-authored blocker this capstone exposes - and closes

The v0.4 dogfood features (FEATURE-001/002) had the lane `verification_commands` **hand-authored**
into `04-lane-graph.yml`. The shell Verifier (`src/ai_dev/shell_verifier.py`) **fails loud** when a
lane declares no `verification_commands`. So reaching `verify` -> `verdict=pass` with *zero*
hand-authored planning required making the verify command set itself **Planner-generated and
promoted**. That is the real code work of this ticket, landed across three files:

1. **`src/ai_dev/planner_schemas.py`** - added an *optional* top-level `verification_commands`
   property to `TASKS_PROPOSAL_SCHEMA`: a list of `{name: non-empty, command: non-empty}` objects
   (the `_VERIFY_COMMAND_ITEM_SCHEMA`). Optional, not `required` - consistent with the
   "a proposal is expected to be incomplete while being refined" ethos.
2. **`src/ai_dev/promote.py`** - `build_canonical_tasks` now parses + validates the proposal's
   `verification_commands` (fails loud §24.2 on a non-`{name, command}` entry) and
   `_populate_lane_graph_from_doc` writes them onto the lane entry alongside
   `purpose`/`tasks`/files, deriving `verification_scope = [name ...]`. Verify commands are NOT
   written to `03-tasks.json` - they are a lane-level concern; the lane-graph is their canonical home.
3. **`src/ai_dev/planner_leg.py::_tasks_task_text`** - instructs the Planner to emit
   `verification_commands` with the exact workspace-relative pytest/mypy convention, plus
   `workspace/`-prefixed `expected_files`/`exclusive_files`.

**Proof the commands are model-generated:** RUN-003 (glm-5.2) emitted, and `promote` wrote into
`04-lane-graph.yml`:

```yaml
verification_commands:
  - {name: pytest, command: "PYTHONPATH=. python -m pytest -q -p no:cacheprovider -c /dev/null tests"}
  - {name: mypy, command: "python -m mypy string_utils"}
verification_scope: [pytest, mypy]
```

The Verifier read these (not hand-written) commands and ran them - `commands=2/2 passed`
(`verification-report.json`, verdict=pass). pytest: `2 passed`; mypy: `Success: no issues found in 2
source files`.

## The `.pyc` boundary exclusion (general fix surfaced by the real run)

RUN-006 (codex-default implementer) first **failed** the §14.2 boundary check: importing the module
during implementation emitted Python bytecode cache
(`workspace/.../__pycache__/*.cpython-312.pyc`, including a
`test_greet.cpython-312-pytest-9.1.1.pyc` stamping the pytest version). These are compiler-emitted
build artifacts, never agent-authored source. **Fix (general, adapter-agnostic):**
`src/ai_dev/run_wrapper.py` adds `_BUILD_ARTIFACT_RE = __pycache__/.*\.pyc` and `compute_changed_files`
subtracts it alongside the adapter's wrapper-owned set - keeping bytecode out of `changed_files`, the
boundary check, and the final-report Q1 traceability index. Covered by
`tests/test_run_wrapper.py::TestComputeChangedFiles::test_subtracts_python_bytecode_artifacts`.
After the fix, RUN-006 implement **passed**.

## Full run transcript (FEATURE-004, repo-root `examples/string-utils`)

| Step | RUN / cmd | Backend | Outcome |
|------|-----------|---------|---------|
| `create-feature-run "<greet intent>"` | - | - | FEATURE-004 created |
| `generate-requirements` | RUN-001 | glm-5.2 | PASS - REQ-001/002/003, AC-001/002/003 |
| `freeze requirements` | - | - | coverage precheck -> frozen |
| `generate-design` | RUN-002 | glm-5.2 | PASS - DES allocated |
| `freeze design` | - | - | coverage precheck -> frozen |
| `generate-tasks` | RUN-003 | glm-5.2 | PASS - TASK-003/004, **verification_commands emitted** |
| `freeze tasks` | - | - | REQ+DES coverage precheck -> frozen |
| `freeze lane_graph` | - | - | frozen (verify commands on lane) |
| `implement LANE-001` | RUN-006 | codex-default | PASS (after `.pyc` fix) - TASK-003/004 done |
| `review LANE-001` | RUN-007 | glm-5.2 | PASS - issues=3 |
| `spec-gap LANE-001` | RUN-008 | glm-5.2 | PASS - issues=0 |
| `verify LANE-001` | shell | (model-gen cmds) | PASS - commands=2/2 (pytest+mypy green) |
| `collect-issues LANE-001` | - | - | PASS - bundle written |
| `lane-gate LANE-001` | - | - | PASS - conditions=5/5 |
| `coherence-gate` | - | - | PASS - **verdict=pass**, status=done |
| `final-report` | - | - | verdict=pass, failure_class=None |

## ADR-0007 Q2/Q3 seam confirmation (IDs flow from Planner)

`final-report.json`:

- **Q2 `requirement_coverage`** - all three Planner-originated REQ ids, `implemented: true`, evidence
  `RUN-006`: `REQ-001`, `REQ-002`, `REQ-003`.
- **Q3 `acceptance_verification`** - all three Planner-originated AC ids, `verified: true`, evidence
  `RUN-006`, `lane_verification: pass`: `AC-001`, `AC-002`, `AC-003`.

These populate from `01-requirements.json` (REQ/AC ids allocated by `promote_requirements`) matched
against the implement-result's `related_requirements` / `related_acceptance_criteria` (preserved from
the Planner-derived task-status rows) - the Planner-originated id scheme, not hand-written ids.

## Quality gate

- `uv run mypy src` - clean (33 source files).
- `uv run pytest` - **1002 passed** (incl. new `TestTasksProposalSchema` verify facets,
  `TestPromoteTasksVerificationCommands` / `TestPromoteTasksMalformedVerifyCommands`, the extended
  fake-claude tasks leg, and the `.pyc` boundary-exclusion test).

## Files modified

- `src/ai_dev/planner_schemas.py` (optional `verification_commands` on tasks schema)
- `src/ai_dev/planner_leg.py` (`_tasks_task_text`: verify-command + `workspace/`-prefix instructions)
- `src/ai_dev/promote.py` (parse + write `verification_commands` into lane-graph; 4-tuple
  `build_canonical_tasks`)
- `src/ai_dev/run_wrapper.py` (`_BUILD_ARTIFACT_RE` + `compute_changed_files` `.pyc` exclusion)
- `tests/test_planner_schemas.py`, `tests/test_promote.py`, `tests/test_planner_leg.py`,
  `tests/test_run_wrapper.py`
