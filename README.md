# ai-dev

Multi-Agent Profile orchestrator — a thin Git/filesystem orchestration layer that
drives Spec-Driven Development through multiple Coding-Agent profiles with
auditable artifacts, canonical status, gates, and decisions.

See [`docs/multi-agent-profile-orchestrator-spec.md`](docs/multi-agent-profile-orchestrator-spec.md)
for the full design. This package is the **v0 walking skeleton** (§23): the
minimal intent → final-report loop, built up ticket by ticket under
`.scratch/ai-dev-v0-skeleton/issues/`.

## Status

v0.0 — local artifact skeleton (§26.1) complete: feature-run directory generator, structured audit appender,
stable ID allocator, canonical status writer + freeze, and
requirements/design/tasks/lane-graph templates (tickets 01-05).

Next: v0.1 — single profile run adapter (§26.2).

## Usage

```bash
uv sync                                      # one-time: set up the env (see DEVELOPMENT.md)
uv run ai-dev create-feature-run "<intent>"  # creates .ai-dev/features/FEATURE-NNN/ (§6 skeleton)
```

For environment setup, testing, typechecking, and debugging, see
[**DEVELOPMENT.md**](DEVELOPMENT.md).
