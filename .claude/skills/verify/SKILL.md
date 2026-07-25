# Project verify

This repo is a Python CLI package (`ai-dev`). Runtime verification should drive the CLI with `uv run ai-dev ...`; do not import internal modules as the observation surface.

Useful pattern for status/gate changes:

1. Create an isolated repo with `repo=$(mktemp -d)`.
2. Seed a run: `uv run ai-dev create-feature-run "<intent>" --repo-root "$repo"`.
3. If the feature under verification needs a later pipeline state, edit the fixture files under `$repo/.ai-dev/features/FEATURE-001/` to create the smallest canonical state that reaches the CLI command.
4. Drive the real command, e.g. `uv run ai-dev show-status FEATURE-001 --repo-root "$repo" --json` or `uv run ai-dev coherence-gate FEATURE-001 --repo-root "$repo"`.
5. Include stdout/stderr and exit code in the verification report.

For multi-lane status/coherence changes, the smallest fixture is: add extra entries to `status/lane-status.yml`, create lane directories under `lanes/<LANE-ID>/`, and write lane-level `lane-decision.json` files before running `show-status` / `coherence-gate`.
