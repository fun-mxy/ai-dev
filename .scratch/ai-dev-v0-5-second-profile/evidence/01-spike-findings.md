# 01 - Spike: `codex exec` on the dogfood target (de-risk ADR-0005 D6)

**Date:** 2026-07-22 (run timestamps UTC: 2026-07-22T08:25–08:33Z, UTC+8 local).
**Profile:** `codex-default` (this dev env: codex-cli 0.144.5, `crs` provider, `gpt-5.5`).
**Verdict:** **PASS** - `codex exec` honors the §13 prompt-written contract. ADR-0005 **D5
(thin adapter) holds** - no output translation needed. Two mechanism corrections surfaced
(D3, D4) and are amended in ADR-0005 before ticket 02 starts.

This is the throwaway de-risk ticket 01 asks for. The detailed run evidence + artifacts
live under the gitignored `prototype/codex-spike/` (`FINDINGS.md`, `run.sh`,
`validate.py`, `runs/RUN-001`, `runs/RUN-002`); this file is the committed summary. It is
distinct from ticket 04 (real codex end-to-end through the `ai-dev` CLI): here the run goes
through a throwaway bash runner that invokes `codex exec` directly, reusing the slugify
implementer input package from `examples/string-utils` verbatim.

## D6's one question, answered

> Does `codex exec` honor "write `result.json`/`result.md` at the declared workspace paths"
> when given the existing implementer prompt, or does it insist on emitting its native
> diff/patch?

**It honors the file contract.** Two runs (RUN-001 non-git, RUN-002 in-git-repo), both:

- exit 0; `output/result.json` schema-valid (§14.1); `output/result.md` written;
- all 5 `changed_files` within `allowed-files.txt` (§14.2 PASS) - the 3 declared workspace
  source files (`workspace/string_utils/{__init__,casing}.py`, `workspace/tests/test_casing.py`)
  plus the 2 §13.1 outputs;
- exit/stdout/stderr captured to files exactly like claude's (`subprocess.run` stdout/stderr
  redirect).

codex writes files via its internal `apply patch` tool **and** prints a `diff --git` block
to stderr. The thin adapter (D5) ignores the diff and reads the files - no translation
needed. codex did **not** insist on native-diff-only; `--output-schema` (codex native
structured output) was deliberately **not** used, honoring §13.3.

## RUN-001 (non-git, primary)

```
exit_code=0  validate=PASS  schema=OK  boundary=OK
changed_files=[output/result.json, output/result.md,
               workspace/string_utils/__init__.py,
               workspace/string_utils/casing.py,
               workspace/tests/test_casing.py]
wall ~99s (08:29:04Z..08:30:43Z)
codex banner: workdir=<run_dir>  provider=crs  model=gpt-5.5
              approval=never  sandbox=workspace-write [workdir, /tmp, $TMPDIR]
writes outside run dir: none. token in run dir: 0 matches.
```

`result.json`: `status="proposed_done"`, summary + `tasks=[{id:"TASK-001",
status:"proposed_done", evidence:[casing.py, test_casing.py]}]`. `casing.py` implements
`snake_case` + `slugify` with the exact regex spec from `task-package.md`.

## RUN-002 (in-git-repo - confinement probe)

Run dir inside a throwaway `git init` root at `.ai-dev/features/FEATURE-001/runs/RUN-002/`.
Same result (exit 0, schema+boundary OK, identical 5 files). `git status` of the throwaway
repo shows **only** `?? .ai-dev/` (the run dir we created); **no stray writes** outside the
run dir; codex made no git commit. codex's `workdir` is the run dir, not the repo root.
**`workspace-write` confines to the cwd (run dir) even inside a git repo** - the
CodexRunner can safely use `cwd = run_dir` in a git-tracked target project.

## ADR-0005 amendments (required before ticket 02)

These are mechanism corrections the spike uncovered. **D5 is unchanged (it holds).**

### D3 - `--remote-auth-token-env` is rejected by `codex exec`

ADR-0005 D3 prescribes `codex exec ... --remote-auth-token-env <profile.auth_env>`. The
spike proves this is impossible:

```
$ codex --remote-auth-token-env OPENAI_API_KEY exec - ...
Error: `--remote-auth-token-env` is only supported for interactive TUI commands, not `codex exec`
```

`--remote-auth-token-env` is a **top-level** flag naming the env var whose value is sent as
a bearer token to a **remote app-server websocket** (`--remote` mode) - not OpenAI/crs
model API auth. The rejection is captured verbatim at
`prototype/codex-spike/d3-flag-rejection/stderr.log`. Correct codex exec auth (the flag
rejection is **observed**; the two auth paths are **inferred from codex behavior/docs** -
only the stored-cred path was actually exercised by this spike):

- **OpenAI provider** (`codex-default`'s `backend: openai`): codex reads `OPENAI_API_KEY`
  from the child env **directly**. The adapter satisfies `auth_env: OPENAI_API_KEY` by
  injecting the token value into that env var - the same env-injection pattern claude uses
  for `ANTHROPIC_AUTH_TOKEN`. No flag. **Not exercised by this spike** (`OPENAI_API_KEY`
  unset in the dev env; `run.sh` never injects it) - inferred, pending ticket 04.
- **Custom provider** (this dev env's `crs`, `requires_openai_auth = true`): codex uses the
  stored `~/.codex/auth.json` credential (`codex login --with-api-key`). This is the auth
  model ADR-0005 D3 rejected, but it is codex's native non-interactive path for custom
  providers and **is what both spike runs used** (they succeeded with `OPENAI_API_KEY`
  unset). Ticket 03 decides the profile's provider; ticket 04 wires real auth.

Either way, **`--remote-auth-token-env` is not in the argv.** Invariant #11 (token by
env-var name only) holds: the OpenAI path injects by name (like claude); the stored-cred
path carries no token in profile config.

### D4 - `cwd = run_dir`, not `workspace/`

ADR-0005 D4 says "cwd = the run's `workspace/` dir". This breaks §13: the role prompts and
`allowed-files.txt` require `output/result.json` / `output/result.md` at RUN-level
(siblings of `workspace/`, outside it). A `workspace-write` sandbox rooted at `workspace/`
would block those writes. Corrected: **`codex exec -s workspace-write` with `cwd = run_dir`**
(the RUN-NNN directory, matching the claude wrapper). The engine confines writes to
`[workdir, /tmp, $TMPDIR]`; the run dir holds both `output/` and `workspace/`, so the §13
outputs and task files are both writable and within the §14.2 RUN-dir mtime-diff. Writes to
`/tmp`/`$TMPDIR` are out-of-band harness state (same treatment as claude's transcript per
the §14.2 note).

**Hygiene note for ticket 02:** codex persists a session to `~/.codex/`; pass `--ephemeral`
to suppress on-disk session persistence (the codex analogue of the claude path's
`--settings autoMemoryEnabled=false`, §14.2). Adapter hygiene, not a contract gate.

## Ticket-01 checklist

- [x] `codex exec` runs non-interactively on the dogfood target with `workspace-write`
      (auth via stored `~/.codex/auth.json` on the `crs` custom-provider path - the path
      both runs exercised. `--remote-auth-token-env` is rejected by `codex exec`, D3
      amended. The OpenAI-provider `OPENAI_API_KEY` env-injection path is inferred, not
      exercised here - deferred to ticket 04)
- [x] confirms codex writes `result.json`/`result.md` at declared paths (§13 contract) -
      schema-valid, both runs; D5 holds, no revision needed
- [x] `changed_files` land in `workspace/`; exit/stdout/stderr capturable like claude's
- [x] findings recorded (`prototype/codex-spike/FINDINGS.md` + this file)
- [x] ADR-0005 amended before 02 starts (D3 + D4 mechanism corrections; D5 unchanged)

## Reproduce

```bash
cd /Users/maxy1/Projects/playground/pp_8_codex_cc_cowork
./prototype/codex-spike/run.sh non-git RUN-001      # primary D6 run
./prototype/codex-spike/run.sh in-git-repo RUN-002  # confinement probe
python3 prototype/codex-spike/validate.py RUN-001   # §14.1 schema + §14.2 boundary
```

The prompt mirrors `src/ai_dev/run_wrapper.py::build_prompt` verbatim; the input package is
a byte-for-byte copy of `examples/string-utils/.ai-dev/features/FEATURE-001/runs/RUN-001/input/`.
