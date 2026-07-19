# Agents

## Development environment

Before writing or running code, set up the env and learn the test/debug loop:
see [`DEVELOPMENT.md`](DEVELOPMENT.md). Short version: `uv sync`, then prefix
every command with `uv run` (`uv run pytest`, `uv run mypy`, `uv run ai-dev …`).

## Agent skills

### Issue tracker

Issues and specs live as markdown files under `.scratch/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five canonical roles (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context — one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
