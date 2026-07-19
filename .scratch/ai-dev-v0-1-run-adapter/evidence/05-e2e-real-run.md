# 05 - v0.1 End-to-End Integration: real cc-glm52 / ark run evidence

**Date:** 2026-07-20 (run timestamps are UTC: 2026-07-19T17:07–17:08Z, UTC+8 local).
**Profile:** `cc-glm52` (Volcengine Ark, `glm-5.2`).
**Verdict:** **PASS** - the v0.0 + v0.1 deterministic runtime strings together end
to end on a real `claude -p` headless run against Ark. `create -> prepare -> run ->
validate` with no manual intervention; `exit_code 0`, schema-valid `result.json`,
`changed_files` within `allowed-files.txt`, `validate-run` PASS (exit 0), token
never persisted.

This is the integrated proof ticket 05 asks for. It is distinct from the prototype's
`prototype/adapter/run.sh` RUN-002 (which de-risked the *contract* through the
throwaway bash prototype): here the run goes through the **real v0.1 code** - the
`ai-dev` CLI dispatching `create_feature_run` (v0.0) -> `prepare_run` (ticket 02,
with the `--allowed-file` seam) -> `run_headless` (ticket 03) -> `validate_run`
(ticket 04). The deterministic fake-`claude` end-to-end test
(`tests/test_e2e_integration.py`) locks the same seam repeatably in CI; this file
is the genuine backend evidence.

## How to reproduce

Throwaway repo root (`.ai-dev/` is gitignored in the orchestrator's own dev repo,
so the run is against a `mktemp -d` directory, not the dev repo):

```bash
DEV=/Users/maxy1/Projects/playground/pp_8_codex_cc_cowork
DEMO=$(mktemp -d)
mkdir -p "$DEMO/.ai-dev"
# Write .ai-dev/agent-profiles.yml with the cc-glm52 profile (ark base_url,
# token by source-name only - see "Profile" below). Then:
unset CC_GLM52_TOKEN                       # use the fallback source (session token) - same as prototype RUN-002
cd "$DEV"
uv run ai-dev create-feature-run "v0.1 e2e real run: create a hello module on ark" --repo-root "$DEMO"
uv run ai-dev prepare-run FEATURE-001 --role Implementer \
  --task "Create workspace/hello.py ... answer() returns 42 ... write output/result.{md,json} ... Stop once result.json is written." \
  --allowed-file workspace/hello.py --repo-root "$DEMO"
uv run ai-dev run-headless   FEATURE-001 RUN-001 --profile cc-glm52 --repo-root "$DEMO"
uv run ai-dev validate-run   FEATURE-001 RUN-001 --repo-root "$DEMO"
```

`claude` v2.1.207 is on `PATH`; the wrapper resolves it via `shutil.which`,
strips the parent Claude-Code identity vars (§10.3), and injects
`ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL` from the
profile. `token_source` resolves to `ANTHROPIC_AUTH_TOKEN` (the fallback), exactly
as the prototype's RUN-002 did (`token_src=ANTHROPIC_AUTH_TOKEN` in its
env-snapshot header).

## Profile (`.ai-dev/agent-profiles.yml`)

Seeded from the verified prototype profile. Token is declared by **source variable
name only** (§10.2 / invariant #11) - no value in the file.

```yaml
agent_profiles:
  cc-glm52:
    cli: claude
    backend: glm
    base_url: "https://ark.cn-beijing.volces.com/api/coding"
    auth_env: "CC_GLM52_TOKEN"
    auth_env_fallback: "ANTHROPIC_AUTH_TOKEN"
    auth_target: "ANTHROPIC_AUTH_TOKEN"
    model: "glm-5.2"
    invocation: headless
    extra_env:
      ANTHROPIC_BASE_URL: "https://ark.cn-beijing.volces.com/api/coding"
      ANTHROPIC_MODEL: "glm-5.2"
    env_strip_pattern: "^(CLAUDE_CODE_|CLAUDECODE$|AI_AGENT$|CLAUDE_EFFORT$)"
```

`ai-dev show-profile cc-glm52` reports `token_source: ANTHROPIC_AUTH_TOKEN`,
`token_set: true`, `token_value: <redacted>`.

## CLI output (verbatim)

```
$ ai-dev create-feature-run "..." --repo-root "$DEMO"
FEATURE-001
$ ai-dev prepare-run FEATURE-001 --role Implementer --task "..." --allowed-file workspace/hello.py --repo-root "$DEMO"
RUN-001
$ ai-dev run-headless FEATURE-001 RUN-001 --profile cc-glm52 --repo-root "$DEMO"
RUN-001: profile=cc-glm52 exit_code=0 changed_files=3
$ ai-dev validate-run FEATURE-001 RUN-001 --repo-root "$DEMO"
VALIDATE PASS - RUN-001 (schema + boundary + frozen OK)
```

Wall time for `run-headless`: ~25s (prototype RUN-002 was ~22s).

## Artifact chain (RUN-001)

Run directory: `.ai-dev/features/FEATURE-001/runs/RUN-001/` - RUN-NNN correctly
nested under the feature run's `runs/` (v0.0 skeleton <-> v0.1 run path integration).
Tree:

```
input/{role,system,task-package,output-schema,allowed-files}.txt + context/run-context.md
output/{result.json, result.md, stdout.log, stderr.log, metadata.json, env-snapshot.txt, .run-settings.json}
workspace/hello.py
```

### `output/metadata.json` (wrapper-written, §13.2 - field-complete)

```json
{
  "run_id": "RUN-001",
  "profile": "cc-glm52",
  "cli": "claude",
  "backend": "glm",
  "model": "glm-5.2",
  "started_at": "2026-07-19T17:07:46Z",
  "ended_at": "2026-07-19T17:08:11Z",
  "exit_code": 0,
  "changed_files": ["output/result.json", "output/result.md", "workspace/hello.py"],
  "commits": [],
  "checks": []
}
```

`changed_files` matches the actual workspace changes (the two §13.1 mandatory
outputs plus the declared workspace file) - wrapper-owned artifacts
(stdout/stderr/metadata/env-snapshot/.run-settings) correctly subtracted.

### `output/result.json` (agent-written, §13.1 - schema-valid)

```json
{
  "status": "proposed_done",
  "summary": "Created workspace/hello.py with a module docstring and an answer() function returning the integer 42.",
  "tasks": [
    {"id": "TASK-001", "status": "proposed_done", "evidence": ["workspace/hello.py"]}
  ]
}
```

Conforms to `input/output-schema.json` (§14.1 - confirmed by `VALIDATE PASS`).

### `output/result.md` (agent-written, §13.1)

```
Created workspace/hello.py (module docstring + answer() returning 42); TASK-001 proposed_done.
```

### `workspace/hello.py` (the task's workspace output)

```python
"""Hello module for FEATURE-001 (RUN-001)."""


def answer() -> int:
    """Return the answer to everything."""
    return 42
```

### `input/allowed-files.txt` (the §14.2 boundary - with the ticket-05 seam)

```
# Agent may ONLY create or modify these paths (relative to the RUN directory).
# Anything else is a file-boundary violation (spec §14.2).
# Task-specific workspace files must be added here before the run.
output/result.json
output/result.md
workspace/hello.py
```

`workspace/hello.py` was added via `prepare-run --allowed-file workspace/hello.py`
(the integration seam fixed in this ticket). Without it, the §14.2 boundary check
would reject the run even though schema and exit code are clean - that failure
mode is pinned by `tests/test_e2e_integration.py::TestAllowedFilesSeam`.

### `output/env-snapshot.txt` (§10.3 evidence - names only, values redacted)

```
# Child claude env snapshot (names only; values redacted) - 2026-07-19T17:07:46Z
# profile=cc-glm52 base_url=https://ark.cn-beijing.volces.com/api/coding model=glm-5.2 token_src=ANTHROPIC_AUTH_TOKEN
ANTHROPIC_AUTH_TOKEN=<set>
ANTHROPIC_BASE_URL=<set>
ANTHROPIC_MODEL=<set>
```

Exactly the three target vars; every value redacted to `<set>`. No parent
Claude-Code identity or model-alias contamination survived the strip.

### `output/stdout.log` final `result` line (run telemetry)

```json
{
  "type": "result",
  "subtype": "success",
  "is_error": false,
  "duration_ms": 22425,
  "num_turns": 11,
  "total_cost_usd": 0.184916,
  "stop_reason": "end_turn"
}
```

`stderr.log`: 0 bytes (clean). `stdout.log`: 1387 lines of well-formed stream-json.

## Audit trail (§2.1 - full lifecycle, in order)

```
2026-07-19T17:07:41Z  create       {feature: FEATURE-001}
2026-07-19T17:07:41Z  allocate_id  {}                         (LANE-001, feature skeleton)
2026-07-19T17:07:41Z  allocate_id  {}                         (RUN-001, the run)
2026-07-19T17:07:41Z  prepare_run  {feature: FEATURE-001, run: RUN-001, role: Implementer}
2026-07-19T17:08:11Z  run          {feature: FEATURE-001, run: RUN-001, profile: cc-glm52, exit_code: 0}
2026-07-19T17:08:11Z  validate     {feature: FEATURE-001, run: RUN-001, attempt: 1, passed: true, failed_check: null, issue_count: 0}
```

## Token safety (§10.2 / invariant #11)

`grep -r` of the run directory for the live token value (36 chars) returns **0
files**. The token value appears in no `result.json` / `result.md` /
`metadata.json` / `stdout.log` / `stderr.log` / `env-snapshot.txt` /
`.run-settings.json`. `metadata.json` carries no `*token*` key
(keys: `backend, changed_files, checks, cli, commits, ended_at, exit_code, model,
profile, run_id, started_at`). The env snapshot redacts every value to `<set>`.

## Frozen-artifact seam (§14.3)

All four frozen artifacts are `false` in `status/feature-status.yml`
(`design, lane_graph, requirements, tasks`), so the §14.3 check does not fire -
correct for v0.1 (nothing is frozen by default). The check path is exercised by
`tests/test_validate.py` (a frozen artifact touched -> FAIL).

## Delta vs prototype RUN-002

The prototype proved the Agent Run Contract through `prototype/adapter/run.sh`
(hardcoded cc-glm52 constants, bash). This run proves the **integrated v0.1
runtime** - the `ai-dev` CLI driving `run_prepare` + `run_wrapper` + `validate` -
holds the same contract on Ark. Same backend, same model, same flag set, same env
isolation, same artifact shape. Run telemetry is in the same band as RUN-002
(duration ~22s, ~$0.18, ~11 turns, `end_turn`, empty stderr).

## Ticket-05 checklist

- [x] `.ai-dev/agent-profiles.yml` configures cc-glm52 (ark base_url, token by name only)
- [x] From one intent: create -> prepare -> run -> validate, no manual intervention
- [x] Real run: exit_code 0, result.json schema-valid, changed_files all in allowed-files, validate-run PASS (exit 0)
- [x] RUN-NNN allocated under the feature run's `runs/`
- [x] metadata.json field-complete; changed_files match workspace changes
- [x] Token never persisted (grep of run dir finds 0 matches for the token value)
- [x] Integration seam (allowed-files path/ID/interface alignment) fixed in-ticket (`prepare_run(allowed_files=...)` + `--allowed-file`)
