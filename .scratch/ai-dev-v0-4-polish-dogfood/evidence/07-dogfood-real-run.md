# 07 - v0.4 dogfood End-to-End: real cc-glm52 / Ark run → `verdict=pass`

**Date:** 2026-07-21 (run timestamps UTC: implement RUN-001 15:52:20–15:57:40Z; review
RUN-002 15:57:56–16:01:10Z; spec-gap RUN-003 16:01:10–16:03:27Z; UTC+8 local ~23:52–00:03).
**Target:** `examples/string-utils/` (the committed v0.4 dogfood target, ticket 05).
**Profile:** `cc-glm52` (Volcengine Ark, `glm-5.2`), `claude` v2.1.207 headless.
**Verdict:** the polished `ai-dev` runs the full happy-path feature —
intent → freeze → implement → review → spec-gap → verify → collect-issues → Human Triage →
lane-gate → coherence-gate → final-report — on a **real** cc-glm52 / Ark backend and reaches
**`verdict=pass`** / `status=done`. The deterministic fake-`claude` test
(`tests/test_e2e_integration.py`) locks the same seams repeatably in CI; this file is the genuine
backend evidence, isomorphic to the v0.1/v0.2/v0.3 `evidence/*-e2e-real-run.md`.

This is the v0.4 **exit criterion C** capstone: all six polish items (01 error文案 / 02 audit
origin+elapsed / 03 read-only CLI / 04 dry-run / 05 example target / 06 coverage) are `done`, and
one dogfood run stands as the integration evidence that strings them on real code.

## The feature (dogfood intent)

> 加一个 `slugify(s)` 函数，带边界测试（空串 / unicode / 首尾连字符）

The target ships a preset `snake_case` + a green `pytest`/`mypy` baseline; the dogfood feature
**adds `slugify`** (same two-step fold as `snake_case` but `-`-separated and lower-cased) with
boundary tests. REQ-001 / AC-001..AC-004 pin: empty → `""`, `Café Noël` → `café-noël` (non-ASCII
passed through), `  Hello   World--` → `hello-world`, non-str → `TypeError`.

## How to reproduce

`.ai-dev/` is gitignored (throwaway runtime state, ticket 05), so the run lives at
`examples/string-utils/.ai-dev/`. `CC_GLM52_TOKEN` unset → the fallback `ANTHROPIC_AUTH_TOKEN`
resolves the live token, exactly as v0.1/v0.2/v0.3.

```bash
cd examples/string-utils                          # repo-root = the target (relative)
# .ai-dev/agent-profiles.yml carries the cc-glm52 profile (see v0.1 evidence §Profile)
ai-dev create-feature-run "加一个 slugify(s) 函数，带边界测试（空串 / unicode / 首尾连字符）"
# Planner fills 01-requirements / 02-design / 03-tasks / 04-lane-graph (slugify feature)
ai-dev freeze FEATURE-001 requirements            # -> design_gate
ai-dev freeze FEATURE-001 design                  # -> task_gate
ai-dev freeze FEATURE-001 tasks                   # -> lane_gate
ai-dev freeze FEATURE-001 lane_graph              # (shares task-gate window; no advance)
ai-dev implement   FEATURE-001 LANE-001 --profile cc-glm52   # RUN-001 (Implementer)
ai-dev review      FEATURE-001 LANE-001 --profile cc-glm52   # RUN-002 (Code Reviewer)
ai-dev spec-gap    FEATURE-001 LANE-001 --profile cc-glm52   # RUN-003 (Spec Gap Analyst)
ai-dev verify      FEATURE-001 LANE-001                       # deterministic shell (pytest+mypy)
ai-dev collect-issues FEATURE-001 LANE-001
ai-dev triage FEATURE-001 --issue ISSUE-001 --disposition accept --reason "..."   # Human Triage
ai-dev collect-issues FEATURE-001 LANE-001                    # re-collect refreshes the bundle
ai-dev lane-gate       FEATURE-001 LANE-001                   # 0=PASS
ai-dev coherence-gate  FEATURE-001                            # 0=PASS; writes terminal verdict
ai-dev final-report    FEATURE-001                            # 0; renders for either verdict
```

The whole chain is observed gate-by-gate with the v0.4 read-only commands (ticket 03):
`list-features` / `show-status` / `log`.

## Dry-run pre-flight (ticket 04)

Before spending tokens, `--dry-run` validated the wiring with **no state change** and **no claude
spawn**:

```
$ ai-dev freeze FEATURE-001 requirements --dry-run
FREEZE DRY-RUN - would freeze requirements and advance current_gate -> design_gate
- would_be_refused: false
- would_write: ["status/feature-status.yml", "audit.log.{md,json}"]            (exit 0)
# ... design -> task_gate, tasks -> lane_gate, lane_graph -> null (no advance)
$ ai-dev show-status FEATURE-001     # after the dry-run freezes
  current_gate: requirements_gate    # UNCHANGED — dry-run wrote nothing
$ ai-dev implement FEATURE-001 LANE-001 --profile cc-glm52 --dry-run
IMPLEMENT DRY-RUN - would prepare Implementer run for lane LANE-001 + spawn claude (no id minted, no spawn)
- profile: cc-glm52   allowed_files: [result.json, result.md, workspace/string_utils/__init__.py, ...]
- env_target_names: {"token_source": "ANTHROPIC_AUTH_TOKEN", ...}   (token by NAME only)
- would_mint_ids: ["RUN-NNN (next monotonic)"]   would_spawn: true   (exit 0)
# runs/ stayed empty; id-counters.yml stayed LANE:1 (no RUN counter) — dry-run minted nothing.
```

This is the ticket-04 payoff: the full input-package build (profile load, frozen check,
lane allowed-files, task text, claude flag set, token-source **name**) is exercised once for free
before the real Ark spend.

## Real run → `verdict=pass`

```
$ ai-dev implement FEATURE-001 LANE-001 --profile cc-glm52
IMPLEMENT PASS - RUN-001 lane=LANE-001 status=proposed_done tasks_marked=['TASK-001']     (~5m20s)
$ ai-dev review FEATURE-001 LANE-001 --profile cc-glm52
REVIEW PASS - RUN-002 lane=LANE-001 role=Code Reviewer issues=1                            (~3m14s)
$ ai-dev spec-gap FEATURE-001 LANE-001 --profile cc-glm52
SPEC-GAP PASS - RUN-003 lane=LANE-001 role=Spec Gap Analyst issues=0                       (~2m17s)
$ ai-dev verify FEATURE-001 LANE-001
VERIFY PASS - lane=LANE-001 implement_run=RUN-001 commands=2/2 passed                      (pytest + mypy)
$ ai-dev collect-issues FEATURE-001 LANE-001
COLLECT-ISSUES PASS - lane=LANE-001 issues=1 bundle=...issue-bundle.json
$ ai-dev triage FEATURE-001 --issue ISSUE-001 --disposition accept --reason "P3 dup 观察；...后续重构"
TRIAGE PASS - issue=ISSUE-001 disposition=accept severity=P3 decisions=-
$ ai-dev collect-issues FEATURE-001 LANE-001          # re-collect refreshes the bundle with the triage
COLLECT-ISSUES PASS - lane=LANE-001 issues=1
$ ai-dev lane-gate FEATURE-001 LANE-001
LANE-GATE PASS - lane=LANE-001 conditions=5/5 decision=...lane-decision.json               (exit 0)
$ ai-dev coherence-gate FEATURE-001
COHERENCE-GATE PASS - feature=FEATURE-001 verdict=pass status=done decision=...coherence-decision.json (exit 0)
$ ai-dev final-report FEATURE-001
FINAL-REPORT - feature=FEATURE-001 verdict=pass failure_class=None report=...final-report.json (exit 0)
```

### Model-written workspace (RUN-001, written by glm-5.2)

```python
# workspace/string_utils/casing.py
def slugify(s: str) -> str:
    """Convert ``s`` to a lowercase ``-``-separated slug.
    Same convention as :func:`snake_case` but using ``-`` as the separator.
    Non-``str`` input raises ``TypeError``; the empty string returns ``""``.
    """
    if not isinstance(s, str):
        raise TypeError(f"slugify() expected str, got {type(s).__name__}")
    boundary = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", s)
    collapsed = re.sub(r"[\W_]+", "-", boundary)
    trimmed = collapsed.strip("-")
    return trimmed.lower()
```

Verifier: `pytest -q -p no:cacheprovider -c /dev/null tests` → **15 passed**;
`mypy string_utils` → **Success: no issues found in 2 source files**.

### `coherence-decision.json` (all three ADR-0003 D1 conditions green)

```json
{
  "feature": "FEATURE-001", "verdict": "pass",
  "conditions": [
    {"name": "status_consistent",              "passed": true, "reason": "feature.status='implementing' matches derive('lane_gate', None)"},
    {"name": "lane_passed_and_p0_p1_handled",  "passed": true, "reason": "all 1 issue(s) resolved or disarmed; 1 lane gate(s) passed"},
    {"name": "decisions_recorded",             "passed": true, "reason": "all 0 disarmed P0/P1 issue(s) have a DEC-NNN file"}
  ],
  "lane_decision_count": 1, "issue_count": 1
}
```

### `final-report.json` (verdict=pass, all five §2.1 audit questions present)

`verdict=pass`, `failure_class=null`, `blocking_reasons=[]`. All five §2.1 keys present:
`code_to_requirement` / `requirement_coverage` / `acceptance_verification` /
`issue_dispositions` / `agent_timeline`. `agent_timeline` = 3 entries (RUN-001 Implementer,
RUN-002 Code Reviewer, RUN-003 Spec Gap Analyst). `issue_dispositions` records
`ISSUE-001 [P3] disposition=accept decision_ids=[]`.

`requirement_coverage`/`acceptance_verification` are honestly empty/`implemented:false`
(`lane_verification=pass` for AC-001..004) — the known v0.3 gap: the implementer did not declare
`related_requirements`/`related_acceptance_criteria` on RUN-001, so Q2/Q3 traceability is empty
and `meta.known_gaps` records it. The verdict is driven by the issue/coherence state, not Q2/Q3
(same as v0.3).

### `current_gate` progression (ADR-0003 D2/D4)

```
create                  current_gate=requirements_gate  status=planning
freeze requirements     -> design_gate                  status=planning
freeze design           -> task_gate                     status=planning
freeze tasks            -> lane_gate                     status=implementing
freeze lane_graph        (no advance; shares task-gate window)
coherence-gate          -> feature_coherence_gate        verdict=pass  status=done   (atomic D4 write)
```

`feature-status.yml` (final): `current_gate=feature_coherence_gate`, `verdict=pass`,
`status=done`, `fix_loop_budget={used: 0, max: 1}`.

### The Code Reviewer issue (real model finding, triaged accept)

RUN-002 raised one P3 — `snake_case` and `slugify` duplicate an identical fold algorithm — citing
exact lines (`workspace/string_utils/casing.py:14` and `:28`) with a concrete refactor
recommendation (extract `_convert(s, sep)`). A genuine, non-blocking maintainability observation;
Human Triage `accept`ed it (no `DEC-NNN` — `accept` is not disarming). The Spec Gap Analyst
(RUN-003) raised **0** issues — the requirement (slugify contract) matches the code, so no
requirement↔code gap.

## ID scoping (v0.0–v0.4 continuity, no re-issue / mis-scope)

`id-counters.yml`: `ISSUE: 1` / `LANE: 1` / `RUN: 3` (no `DEC:` — `accept` is non-disarming).
`runs/` holds exactly `RUN-001` (implement), `RUN-002` (review), `RUN-003` (spec-gap); verify /
collect / triage / lane-gate / coherence-gate / final-report are deterministic and allocate no
RUN. `LANE-001` is the seeded single lane; `ISSUE-001` the single review finding. No `CP-NNN` /
`DEC-NNN` on the happy path.

## Token safety (§10.2 / invariant #11)

An independent `grep -rlF "$ANTHROPIC_AUTH_TOKEN" .ai-dev/features/FEATURE-001` returns
**0 files**. The token appears in no `result.json` / `result.md` / `metadata.json` /
`stdout.log` (7.8 MB stream-json) / `stderr.log` / `env-snapshot.txt` / `.run-settings.json` /
lane / feature / issue artifact. `env-snapshot.txt` redacts every value to `<set>` (names only):

```
# profile=cc-glm52 base_url=https://ark.cn-beijing.volces.com/api/coding model=glm-5.2 token_src=ANTHROPIC_AUTH_TOKEN
ANTHROPIC_AUTH_TOKEN=<set>
ANTHROPIC_BASE_URL=<set>
ANTHROPIC_MODEL=<set>
```

The token is resolved by env-var **name** (`auth_env: CC_GLM52_TOKEN` / fallback
`ANTHROPIC_AUTH_TOKEN`), never inlined.

## Seam notes (ticket 07: integration friction fixed in-ticket)

The dogfood surfaced three real seams on the `examples/string-utils` target that the
absolute-`mktemp -d` v0.1–v0.3 runs never hit. All three are fixed in-ticket:

- **Relative `--repo-root` broke the claude `--settings` lookup (product fix).** Ticket 05's README
  drives the target with `cd examples/string-utils` + a **relative** `--repo-root`, unlike the
  absolute `mktemp -d` of prior evidence. `run_wrapper` built the `--settings` path relative to
  that repo-root, but `claude -p` is spawned with `cwd = <run-dir>` and re-resolves `--settings`
  *relative to its own cwd* → `Settings file not found`, exit 1 in <1 s, no output. Fixed in
  `src/ai_dev/run_wrapper.py`: `run_root` is resolved to absolute up front, so `cwd` and
  `--settings` are absolute regardless of the caller's path. Semantics unchanged (snapshots still
  compute RUN-relative `changed_files`); `tests/test_run_wrapper.py` green.
- **Lane `expected_files` must use the `workspace/` prefix (v0.2 path convention).** The §9.5
  Verifier runs commands in the implement run's `workspace/` (cwd), but a lane declaring
  `expected_files: [string_utils/casing.py]` (no prefix) lets the model write at the RUN root —
  §14.2 validation passes (the literal path is allowed) yet the verifier finds an empty
  `workspace/`. The Planner must declare `workspace/<path>` (as v0.3's `workspace/hello.py` did);
  the lane-graph here now uses `workspace/string_utils/{__init__,casing}.py` +
  `workspace/tests/test_casing.py`, and the task brief names those exact paths.
- **The Verifier must confine pytest to the isolated workspace.** The run's `workspace/` is
  *nested inside* the real target repo, so `pytest` walks up, discovers
  `examples/string-utils/pyproject.toml` (`[tool.pytest.ini_options] pythonpath=["."]`), and
  imports the **preset** `string_utils` (no `slugify`) → `ImportError: cannot import name
  'slugify'`. The declared verify command pins pytest to the workspace:
  `PYTHONPATH=. python -m pytest -q -p no:cacheprovider -c /dev/null tests` — `-c /dev/null` stops
  the upward config walk (rootdir = workspace), so the workspace's own `string_utils` resolves.
  `mypy string_utils` was unaffected (explicit package arg, cwd = workspace). 15/15 tests pass.

The implementer is also told **not** to self-run `pytest`/`mypy`/`python` (the §9.5 Verifier owns
verification, per `input/system.md`); doing so writes `.mypy_cache/` / `__pycache__/` outside
`allowed-files.txt` and trips §14.2.

## Ticket-07 checklist

- [x] `examples/string-utils/` — `create-feature-run` from the dogfood intent; Planner fills the
      four §7 artifacts (REQ-001/AC-001..004, DES-001, TASK-001, LANE-001 with verify commands)
- [x] freeze → implement → review → spec-gap → verify → collect-issues → [triage] → lane-gate →
      coherence-gate → final-report, in sequence, reaching `verdict=pass`
- [x] `--dry-run` (ticket 04) pre-flighted the whole chain (freeze + implement) with no spawn /
      no state change before the real Ark spend
- [x] `list-features` / `show-status` / `log` (ticket 03) observed the feature advance gate by
      gate (origin + elapsed_ms from ticket 02 surface in the audit timeline)
- [x] evidence captured here: command sequence, each gate verdict, `final-report.{json,md}`
      summary, token-not-on-disk grep, `log` output sample
- [x] token never persisted (`grep -rlF` of the feature tree → 0 matches; env-snapshot redacted)
- [x] `final-report.{json,md}` carry `verdict=pass` + the five §2.1 audit-question answers
- [x] IDs continuous across v0.0–v0.4 (RUN-001/002/003, LANE-001, ISSUE-001; no DEC/CP) — no
      re-issue / mis-scope
- [ ] (stretch) failure-path run (P1 → triage → fix-run → re-coherence) — not run; happy-path
      PASS is the exit hard requirement (Q3 decision); the deterministic fake-`claude`
      `test_fix_loop_resolves_to_pass` pins that seam in CI
- [x] integration-seam friction fixed in-ticket (relative-repo-root `--settings`, lane
      `workspace/` prefix, pytest `-c /dev/null` isolation) — no leftovers
