# 04 - v0.6 Planner End-to-End: real cc-glm52 / Ark run -> `generate-tasks` PASS + task/lane gate (REQ+DES coverage precheck) -> `lane_gate`

**Date:** 2026-07-23 (run timestamp UTC: generate RUN-005 10:16:41-10:19:58Z; UTC+8 local ~18:16-18:20).
**Target:** `examples/string-utils/` (the committed v0.4/v0.5/v0.6 dogfood target, re-used;
FEATURE-003 was left at `current_gate=task_gate` with requirements AND design frozen by tickets 02/03 -
exactly the two-frozen-upstream precondition the tasks leg needs, ADR-0008 D2).
**Profile:** `cc-glm52` (claude CLI headless; GLM provider via injected `ANTHROPIC_BASE_URL` ->
`glm-5.2`; ADR-0008). Resolved through `role_defaults[planner] = cc-glm52` (no `--profile` given) -
the same role policy tickets 01-03 extended to the Planner.
**Token:** `CC_GLM52_TOKEN` unset in this env -> resolved through the `auth_env_fallback` =
`ANTHROPIC_AUTH_TOKEN` path (token-by-env-var-name only, never persisted; the run's
`env-snapshot.txt` redacts it to `ANTHROPIC_AUTH_TOKEN=<set>` and records
`token_src=ANTHROPIC_AUTH_TOKEN` + the injected ark `base_url` - token-safety invariant #11 holds).
**Verdict:** the third and largest planning gate runs end-to-end on a **real** glm-5.2 / Ark backend -
`generate-tasks` (input package = intent + **frozen** `01-requirements.json` + **frozen**
`02-design.json` - the first stage stitching against TWO upstreams) -> auto-`promote` (the **four-file
write**: canonical-unfrozen `03-tasks.json` + rendered `03-tasks.md` + seeded `task-status.yml` (all
`pending`) + populated `04-lane-graph.yml` (single lane), TASK ids allocated + `related_requirements` /
`related_design` stitched against the frozen REQ AND DES upstreams via `add_upstream("REQ", ...) +
add_upstream("DES", ...)` - the first exercise of the generic resolver with two upstreams) ->
human `freeze tasks` running the **freeze-gate coverage precheck** (every REQ AND DES referenced by
>=1 task, ADR-0008 D3 / section 18.2) advancing `current_gate` `task_gate -> lane_gate` -> human
`freeze lane_graph` (no precheck, no advance - the two-command freeze model). The coverage-**refusal**
path is demonstrated on the real promoted artifact (inject a DES gap -> `freeze tasks` exits 1, gate
holds). The deterministic fake-`claude` test (`tests/test_planner_leg.py` + `tests/test_dry_run.py` +
`tests/test_coverage.py`) locks the prepare->run->validate->promote->coverage->freeze seam repeatably
in CI; this file is the genuine backend evidence. **Per the [[e2e-tickets-need-real-ark-run]] bar, this
ticket is NOT done on the fake-claude test alone** - the run below is the evidence of record.

## Model de-risk - PASS, no ADR-0008 amendment needed

The ticket carries the v0.5/01-spike analogue: *can glm-5.2 emit a schema-valid id-free tasks proposal
that references the frozen REQ AND DES upstreams by canonical id, plus a single-lane assignment with
expected/exclusive files, or does it need retries / fail?* **Answer: it honors the tasks proposal
schema on the first pass.** RUN-005 exited 0 and `promote` fired automatically (validation passed) -
`GENERATE-TASKS PASS - RUN-005 ... TASK=['TASK-001', 'TASK-002']`. The model authored genuinely id-free
content (local `key` handles `t1`/`t2`; `related_requirements` / `related_design` spelled as the
canonical `REQ-006/007/008` and `DES-004/005` it read from the frozen upstreams), a non-empty top-level
`lane_purpose`, single-lane `LANE-001` assignment on every task, and `expected_files` /
`exclusive_files` as lists of non-empty strings. `promote` allocated the canonical `TASK-001/002` ids
and stitched every `related_requirements` to a frozen REQ id and every `related_design` to a frozen DES
id (reference-integrity with TWO upstreams, ADR-0008 D3, held with no `UnresolvedRefError`).
**No Planner-specific prompt variant is required; ADR-0008 is unchanged.**

## The two-upstream coverage split, demonstrated live (ADR-0008 D3)

This is the ticket's structural contribution - tasks stitch against two upstreams, so the coverage
precheck is the union of REQ and DES coverage, split across the two gate actions:

* **reference-integrity at promote-time (two upstreams).** `build_canonical_tasks` registers the
  proposal's local task keys (`register_local`) and adds BOTH frozen upstreams
  (`add_upstream("REQ", frozen_req_ids(req_doc))` + `add_upstream("DES", frozen_des_ids(des_doc))` -
  the first two-upstream exercise of the generic resolver), then `resolve`s each task's
  `related_requirements` (against the frozen REQ set) and `related_design` (against the frozen DES
  set). Every ref resolves or promote fails loud. RUN-005 stitched cleanly:
  `TASK-001 -> {REQ-006, DES-004}`, `TASK-002 -> {REQ-006/007/008, DES-005}`. The AC->REQ derivation
  also ran at promote: each `task-status.yml` row carries `related_acceptance_criteria` derived via the
  TASK->REQ->AC chain (`TASK-001 -> AC-008/009` via REQ-006; `TASK-002 -> AC-008..011` via
  REQ-006/007/008).

* **coverage-completeness at freeze-time (REQ union DES).** `freeze_gate_coverage("tasks", ...)`
  (via `tasks_coverage`) reads the frozen REQ set + the frozen DES set + the canonical `03-tasks.json`
  and collects every `related_requirements` + `related_design` ref across tasks; any frozen REQ OR DES
  not so referenced is uncovered -> the freeze refuses (no self-heal: back to
  `generate-tasks --feedback` or Human Triage). Demonstrated both ways below.

## The four-file promote write, demonstrated live (ADR-0008 D2)

`promote_tasks` is the sole canonical writer and writes **four** files in one atomic step - the largest
promote surface in v0.6:

1. **`03-tasks.json`** (NEW in v0.6) - canonical-unfrozen (`frozen: false`, the same creation-time
   default the frozen requirements/design JSON carry; the authoritative frozen state lives in
   `feature-status.yml`'s `frozen_artifacts` map). Carries the non-empty top-level `lane_purpose` +
   the two allocated tasks (`TASK-001`/`TASK-002`) with id/key/lane/summary/related_requirements/
   related_design/expected_files/exclusive_files + description/verification.
2. **`03-tasks.md`** - the rendered mirror. The `## Lane purpose (single lane)` H2 section precedes
   `## Tasks (TASK-NNN)` (the LAST section - nothing follows it, so the Implementer reads a clean task
   list); each `### TASK-NNN` block lists lane/refs/expected/exclusive files + description +
   verification.
3. **`status/task-status.yml`** - seeded with every task `status: pending`, `accepted_done: false`,
   `owner_run: null`, plus the stitched `related_requirements` and the derived
   `related_acceptance_criteria` (the TASK->REQ->AC chain). Runtime state (`pending -> proposed_done`)
   lives here, not on the task doc.
4. **`04-lane-graph.yml`** - the single seeded `LANE-001` populated: `purpose` set from the proposal's
   `lane_purpose`, `tasks: [TASK-001, TASK-002]` (sorted), `expected_files` / `exclusive_files` as the
   sorted union across the lane's tasks, and the preserved `id` / `depends_on` / `provides` /
   `consumes` / `verification_scope` / `merge_policy` fields.

## How to reproduce

`.ai-dev/` is gitignored (throwaway runtime state), so the run lives at
`examples/string-utils/.ai-dev/`. The module is invoked from the repo root (the example dir is its own
uv project without `ai_dev`), `--repo-root` pointing at the target. FEATURE-003 is already at
`task_gate` with requirements AND design frozen (tickets 02/03), so the tasks leg starts clean:

```bash
# from repo root
# 1. generate -> promote (Planner = cc-glm52 via role_defaults; auto-promote gated on validation).
#    Input package = intent + frozen 01-requirements.json + frozen 02-design.json (two upstreams).
uv run python -m ai_dev generate-tasks FEATURE-003 --repo-root examples/string-utils
# GENERATE-TASKS PASS - RUN-005 feature=FEATURE-003 stage=tasks promoted=03-tasks.json TASK=['TASK-001','TASK-002']
# validate-run: VALIDATE PASS - RUN-005 (schema + boundary + frozen OK); exit_code=0; ~3m17s wall

# 2. coverage-refusal demonstration (inject a DES gap on the real promoted artifact):
#    strip DES-004 from TASK-001 (the only task referencing it) -> DES-004 uncovered, then attempt freeze.
uv run python -c "import json;p='examples/string-utils/.ai-dev/features/FEATURE-003/03-tasks.json';d=json.load(open(p));[t.update(related_design=[x for x in t['related_design'] if x!='DES-004']) for t in d['tasks'] if t['id']=='TASK-001'];open(p,'w').write(json.dumps(d,indent=2,ensure_ascii=False))"
uv run python -m ai_dev freeze FEATURE-003 tasks --repo-root examples/string-utils
# error: freeze-gate coverage precheck FAILED for 'tasks': 1 REQ/DES id(s) not referenced in any task - DES-004.
# Refine the proposal (generate-tasks --feedback) to cover them, or route to Human Triage (ADR-0008 D3 / §18.2).
#   [exit 1; tasks stays unfrozen, current_gate stays task_gate]

# 3. restore the gap + human freeze tasks -> gate advance (two-command freeze model)
uv run python -c "import json;p='examples/string-utils/.ai-dev/features/FEATURE-003/03-tasks.json';d=json.load(open(p));[t.update(related_design=['DES-004']) for t in d['tasks'] if t['id']=='TASK-001'];open(p,'w').write(json.dumps(d,indent=2,ensure_ascii=False))"
uv run python -m ai_dev freeze FEATURE-003 tasks --repo-root examples/string-utils
# FEATURE-003: froze tasks   ->  current_gate: lane_gate   [exit 0]

# 4. freeze lane_graph (no precheck, no advance - the second command of the two-command model)
uv run python -m ai_dev freeze FEATURE-003 lane_graph --repo-root examples/string-utils
# FEATURE-003: froze lane_graph   [exit 0; current_gate stays lane_gate]
```

## Evidence captured

**RUN-005 (generate-tasks, first pass, no feedback):** `validate` PASS (schema + boundary + frozen);
`exit_code=0`; `metadata.json` records `profile=cc-glm52`, `model=glm-5.2`,
`started_at=2026-07-23T10:16:41Z`, `ended_at=2026-07-23T10:19:58Z` (~3m17s wall). Promoted the
four-file write: `03-tasks.json` (`frozen=false`) with `lane_purpose` + 2 tasks; `TASK-001` (pure
greeting-formatter module `string_utils/greet.py` exporting `compose_greeting`, refs
`{REQ-006, DES-004}`, expected/exclusive `string_utils/greet.py`) and `TASK-002` (`greet` CLI
entrypoint `string_utils/cli.py` + `pyproject.toml` `[project.scripts]`, refs
`{REQ-006/007/008, DES-005}`, expected/exclusive `string_utils/cli.py, pyproject.toml`); `03-tasks.md`
rendered (lane-purpose H2 before the tasks H2); `task-status.yml` seeded (both `pending`, with derived
`related_acceptance_criteria`: `TASK-001 -> AC-008/009`, `TASK-002 -> AC-008..011`);
`04-lane-graph.yml` `LANE-001` populated (purpose, `tasks=[TASK-001,TASK-002]`, expected/exclusive as
sorted union). The TASK id counter is monotonic (`TASK-001/002`); RUN counter is monotonic (RUN-005,
continuing RUN-003/004 from ticket 03).

**Coverage-refusal (injected DES gap):** with `DES-004` stripped from `TASK-001`'s `related_design`
in the canonical `03-tasks.json`, `freeze FEATURE-003 tasks` exits **1** with `error: freeze-gate
coverage precheck FAILED for 'tasks': 1 REQ/DES id(s) not referenced in any task - DES-004. Refine the
proposal (generate-tasks --feedback) to cover them, or route to Human Triage (ADR-0008 D3 / §18.2).`
The status is unchanged (`frozen_artifacts.tasks=false`, `current_gate=task_gate`) - the gate refused
without self-healing, exactly as ADR-0008 D3 requires, and the message names the uncovered id and the
two recovery channels. (The gap was injected by editing the promoted artifact to exercise the gate on a
real canonical file; the deterministic refusal path is also locked by the fake-`claude` coverage
variants in `tests/test_planner_leg.py::TestFreezeTasksGateAndCoverage` and the dry-run
`would be REFUSED: tasks coverage gap` case in `tests/test_dry_run.py::TestFreezeDryRun`.)

**Freeze (two-command model):** with `DES-004` restored, `freeze FEATURE-003 tasks` exits **0** ->
`status/feature-status.yml` now has `frozen_artifacts.tasks=true`, `current_gate=lane_gate`. Then
`freeze FEATURE-003 lane_graph` exits **0** -> `frozen_artifacts.lane_graph=true`, `current_gate`
stays `lane_gate` (lane_graph has no advance target), `status` advanced `planning -> implementing`.
The audit log records the tasks `promote` event (`stage=tasks, artifact=tasks, allocated={"TASK":
["TASK-001","TASK-002"]}`) and the two `freeze` events (`{artifact: tasks}`, `{artifact: lane_graph}`).

## Conclusion

All ticket-04 checkboxes are met: the `generate-tasks` command runs the Planner tasks leg against the
frozen requirements AND design upstreams (input package = intent + frozen `01-requirements.json` +
frozen `02-design.json`, fail-loud if either is not frozen - ADR-0008 D2, the first two-upstream
stage); the run emits an id-free ticket-04-schema tasks proposal (lane_purpose + tasks with REQ+DES
refs + single-lane assignment + expected/exclusive files) in `output/`; promote fires automatically
after the run writing the **four** canonical files (`03-tasks.json` NEW + `03-tasks.md` +
seeded `task-status.yml` + populated `04-lane-graph.yml`) with TASK ids allocated + refs stitched
against the frozen REQ AND DES upstreams (the first two-upstream `add_upstream` exercise,
reference-integrity held, AC->REQ derived into task-status rows); `--feedback` carries refinement
(the refinement channel, ADR-0008 D4, is shared with the requirements/design legs); the freeze-gate
coverage precheck (every REQ AND DES in >=1 task's refs, section 18.2) refuses on a gap (exit 1, no
self-heal) and the human `freeze tasks` advances `task_gate -> lane_gate` followed by `freeze
lane_graph` (two-command freeze model); real cc-glm52/Ark evidence is captured (model emits a
schema-valid tasks proposal on the first pass, no ADR-0008 amendment needed); and `uv run mypy` +
`uv run pytest` are green (33 source files clean; the new `tests/test_coverage.py` tasks cases,
tasks cases in `tests/test_planner_leg.py` / `tests/test_promote.py`, and tasks dry-run cases in
`tests/test_dry_run.py` all pass).
