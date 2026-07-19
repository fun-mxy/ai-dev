# Development

How to set up and work in this repo. This is the **inherited** reference — every
contributor (human or AFK agent) should be able to get a working environment and
run/test/debug the code from this file alone.

The design lives in [`docs/multi-agent-profile-orchestrator-spec.md`](docs/multi-agent-profile-orchestrator-spec.md);
tickets live under [`.scratch/`](docs/agents/issue-tracker.md).

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)** — manages the Python environment and lockfile.
  Install via `brew install uv` (macOS) or the [official installer](https://docs.astral.sh/uv/getting-started/installation/).
- Python **>= 3.10** (uv will fetch a suitable interpreter if one isn't present).

## First-time setup (and after pulling)

```bash
uv sync
```

This creates `.venv/`, installs the `ai-dev` package (editable, from `src/`) and
the `dev` tooling group, all pinned by the committed **`uv.lock`**. Everyone
working on the repo gets the same environment. No manual activation needed —
prefix commands with `uv run`.

> `uv.lock` is **committed** for reproducibility. `.venv/` is not (it's
> machine-local). Never hand-edit the lockfile; regenerate it with `uv sync`
> (or `uv lock`) after changing `pyproject.toml`.

## Daily commands

All commands run from the repo root, prefixed with `uv run` so they execute
inside the managed venv.

| Task | Command |
| --- | --- |
| Full test suite | `uv run pytest` |
| One test file | `uv run pytest tests/test_feature_ids.py` |
| One test case | `uv run pytest tests/test_feature_ids.py::TestNextFeatureId::test_empty_features_dir_yields_001` |
| Typecheck (whole package) | `uv run mypy` |
| Typecheck one module | `uv run mypy src/ai_dev/status.py` |
| Run the CLI (current dir) | `uv run ai-dev create-feature-run "<intent>"` |
| Run the CLI (throwaway dir) | `uv run ai-dev create-feature-run "<intent>" --repo-root "$(mktemp -d)"` |

`uv run pytest -q` is the quick loop check; run it after every red-green cycle.
Run `uv run pytest` (full) and `uv run mypy` before committing.

### Optional: activate the venv

`source .venv/bin/activate` lets you drop the `uv run` prefix (`pytest`, `mypy`,
`ai-dev` directly). Not required — `uv run` works without activation.

## Debugging

- **Isolate a failure** — run the single failing test with `-q` (quiet) and `-s`
  (no output capture) so prints/`breakpoint()` work:
  `uv run pytest tests/test_feature_run.py::TestCreateFeatureRun::test_allocates_001_then_002 -q -s`
- **Drop into the debugger** on failure: `uv run pytest --pdb tests/...`
- **Localize a type error**: `uv run mypy src/ai_dev/<module>.py`
- **Observe real CLI output without polluting the repo** — always run the CLI
  against a throwaway directory (the repo's `.ai-dev/` is gitignored precisely
  because local runs are throwaway; see "Why .ai-dev/ is gitignored" below):
  ```bash
  DEMO="$(mktemp -d)"
  uv run ai-dev create-feature-run "my intent" --repo-root "$DEMO"
  find "$DEMO/.ai-dev/features/FEATURE-001" | sort     # inspect the skeleton
  cat "$DEMO/.ai-dev/features/FEATURE-001/status/feature-status.yml"
  rm -rf "$DEMO"
  ```
- **Editable install means live reload** — changes under `src/ai_dev/` take
  effect immediately in `uv run` commands; no reinstall needed.

## Adding dependencies

- Runtime dependency (imported by `src/ai_dev/`): `uv add <pkg>`
- Dev-only tool (tests/typecheck): `uv add --group dev <pkg>`

Both update `pyproject.toml` **and** `uv.lock` — commit both together.

## Project layout

```
src/ai_dev/          deterministic runtime — the Python data plane (spec §25.3)
  paths.py           .ai-dev path resolution               (leaf helper)
  timeutil.py        shared ISO-8601 UTC stamp             (leaf helper)
  feature_ids.py     FEATURE-NNN allocation    → expanded by ticket 03
  status.py          feature-status.yml writer → expanded by ticket 04
  audit.py           audit.log.md appender     → replaced  by ticket 02
  feature_run.py     create_feature_run orchestration
  cli.py             `ai-dev` console entry
tests/               pytest suite; run from repo root via `uv run pytest`
.scratch/            issue tracker — tickets & specs (docs/agents/issue-tracker.md)
docs/                design spec + agent-skill conventions
prototype/           gitignored throwaway de-risk artifact (NOT project code)
```

Each v0.0 ticket expands one module — the `→` lines above name the ticket that
builds that concern out for real. When picking up a ticket, read the existing
module it touches before writing.

## Workflow for a ticket

1. Pick an **unblocked** ticket under `.scratch/ai-dev-v0-skeleton/issues/`
   (check its `Blocked by:` line; a ticket is unblocked when every file it lists
   is done).
2. Read the ticket **and** the spec sections it cites.
3. Read the existing module the ticket expands — its docstring says what the
   ticket is allowed to change.
4. Implement **TDD** (red → green) at the public seams; run `uv run pytest` and
   `uv run mypy` regularly. In Claude Code, `/implement <ticket-path>` follows
   this loop and `/code-review` reviews the diff.
5. Commit to `main` (repo convention — history is linear on `main`), using
   [Conventional Commits](https://www.conventionalcommits.org/) scope tags
   (`feat(ai-dev-v0): …`, `spec(§6): …`, `tickets(…): …`).

## Conventions

- **Fully typed** — `mypy` runs with `disallow_untyped_defs`; every function has
  annotations. Keep it green.
- **Tests at public seams** — exercise behaviour through the public functions
  (`create_feature_run`, `next_feature_id`, …), never internals. Use the
  `tmp_path`/`repo_root` fixture so tests never touch a real `.ai-dev/`.
- **Secrets by env-var name only** (spec §10.2, invariant #11) — never inline a
  token value in config.
- **Markdown + JSON/YAML double product** for important artifacts (spec §4.4,
  invariant #14).

## Why `.ai-dev/` is gitignored here

This repo is the orchestrator's **own** development repo. `.ai-dev/` (feature
runs created by local CLI runs) is throwaway demo state here, so it's gitignored
to prevent accidental commits. The spec's rule that *".ai-dev/ is the committed
source of truth"* (§4.1) applies to the **target project** the orchestrator runs
inside — not to this repo.
