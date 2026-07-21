# string-utils — ai-dev v0.4 dogfood target

A deliberately **tiny** Python package that serves two roles in v0.4 (§26.5):

1. **The "example feature" deliverable** — a committed, self-contained target
   project an operator can point `ai-dev` at.
2. **The dogfood run target (ticket 07)** — the repo `ai-dev` develops *against*
   on a real cc-glm52 / Ark run, so the whole intent → final-report loop is
   exercised on real code instead of a synthetic fixture.

It is intentionally minimal: one module, one pre-existing function
(`snake_case`), and a green `pytest` / `mypy` baseline. The v0.4 dogfood run
(ticket 07) treats *adding a new function* (`slugify`, with boundary tests) as
its feature, so the starting state must already have a real passing test suite
for the verifier to regress against.

## Layout

```
examples/string-utils/
├── pyproject.toml          # minimal runnable config (pytest + mypy + setuptools)
├── README.md               # this file
├── .gitignore              # ignores throwaway .ai-dev/ runtime state
├── string_utils/
│   ├── __init__.py         # re-exports the public surface
│   └── casing.py           # snake_case(s) — the preset function
└── tests/
    └── test_casing.py      # preset pytest suite (green baseline)
```

## Verify commands (what the Verifier leg runs)

The package is built so the §9.5 shell verifier needs **zero new toolchain** —
plain `pytest` and `mypy` from the dev environment:

```bash
pytest     # preset suite under tests/ — must stay green
mypy       # disallow_untyped_defs keeps the package honest
```

Both pass against the preset `snake_case` + its tests. Run them from this
directory:

```bash
cd examples/string-utils
uv run pytest    # or: python -m pytest
uv run mypy      # or: python -m mypy string_utils
```

## Using it as a dogfood target

`ai-dev` operates with this directory as the **repo root** — it creates its
`.ai-dev/` runtime state here (gitignored; see `.gitignore`). A feature run
targeting the §26.5 example feature looks like:

```bash
cd examples/string-utils

# 1. start a feature run from the dogfood intent (ticket 07 runs this for real)
ai-dev create-feature-run "Add a slugify(s) function with boundary tests (empty / unicode / leading-trailing hyphens)"

# 2. freeze the human-gated artifacts once the Planner has elaborated them
ai-dev freeze FEATURE-001 requirements
ai-dev freeze FEATURE-001 design
ai-dev freeze FEATURE-001 tasks
ai-dev freeze FEATURE-001 lane-graph

# 3. scaffold the implementer run (ticket 07 runs implement->verify for real)
ai-dev prepare-run FEATURE-001 --role Implementer \
    --task "Implement slugify in string_utils/casing.py with boundary tests" \
    --allowed-file string_utils/casing.py \
    --allowed-file tests/test_casing.py
```

> **Read-only observation** (`list-features` / `show-status` / `log`) is v0.4
> ticket-03 surface and is not part of this target. Ticket 07's dogfood run
> uses those commands to watch the feature advance gate by gate once 03 lands.

`create-feature-run` + `freeze` + `prepare-run` starting cleanly from this
directory is the ticket-05 acceptance check; the **real** end-to-end run
(implement → review → spec-gap → verify → collect → triage → lane-gate →
coherence-gate → final-report, to `verdict=pass`) is ticket 07.

## Note on `.ai-dev/`

This target is a dogfood **victim**, so its `.ai-dev/` is throwaway runtime
state and is gitignored — the same convention the orchestrator's own repo uses,
and an explicit exception to spec §4.1's "`.ai-dev/` is committed source of
truth" rule (which applies to real product repos, not to example targets).
