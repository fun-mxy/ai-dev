# ai-dev

Multi-Agent Profile orchestrator — a thin Git/filesystem orchestration layer that
drives Spec-Driven Development through multiple Coding-Agent profiles with
auditable artifacts, canonical status, gates, and decisions.

See [`docs/multi-agent-profile-orchestrator-spec.md`](docs/multi-agent-profile-orchestrator-spec.md)
for the full design. This package is the **v0 walking skeleton** (§23): the
minimal intent → final-report loop, built up ticket by ticket under
`.scratch/ai-dev-v0-skeleton/issues/`.

## Status

v0.0 — local artifact skeleton (§26.1). Currently implements ticket 01: the
`create-feature-run` tracer bullet.

## Usage

```bash
# After `pip install -e .[dev]` (or run via module with src on the path):
ai-dev create-feature-run "<intent text>"

# …creates .ai-dev/features/FEATURE-NNN/ with the §6 directory skeleton.
```
