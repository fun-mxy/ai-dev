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

v0.1 — single profile run adapter (§26.2) complete: agent-profiles.yml loader + show-profile,
prepare-run scaffold + input package, Claude Code headless wrapper (env isolation + capture + metadata),
deterministic three-check validation (schema + boundary + frozen), and end-to-end
create->prepare->run->validate integration on ark (tickets 01-05).

v0.2 - implement -> review -> gap -> verify loop (§26.3) complete: implementer leg,
code-reviewer + spec-gap-analyst checking runs (shared §15 issues contract), shell verifier,
issue normalization + bundle, and the §18.4 lane-gate evaluator, with PASS + FAIL
end-to-end evidence on ark (tickets 01-06).

v0.3 — human triage + fix loop (§26.4) complete: verdict field rename, issue status state machine
(raised|triaged|resolved|reappeared), freeze-driven current_gate advance + derived feature.status,
apply_triage command + DEC promotion, lane-gate blocking formula, bounded fix-run loop (budget ≤ 1),
coherence-gate evaluator (terminal verdict), and final-report generator, with PASS + FAIL end-to-end
evidence on ark (tickets 01-10).

Next: v0.4 — polish and dogfood (§26.5).

## Usage

```bash
uv sync                                      # one-time: set up the env (see DEVELOPMENT.md)
uv run ai-dev create-feature-run "<intent>"  # creates .ai-dev/features/FEATURE-NNN/ (§6 skeleton)
```

For environment setup, testing, typechecking, and debugging, see
[**DEVELOPMENT.md**](DEVELOPMENT.md).
