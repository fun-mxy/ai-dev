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

v0.4 — polish and dogfood (§26.5) complete: actionable error messages + top-level `--debug`
(ticket 01), audit `elapsed_ms` + `origin` fields (ticket 02), read-only `list-features` /
`show-status` / `log` + global `--json` (ticket 03), `--dry-run` mode (ticket 04 / ADR-0004),
example target repo (ticket 05), pytest-cov 91% baseline (ticket 06), and a dogfood happy-path
end-to-end run on `examples/string-utils/` reaching verdict=pass on real cc-glm52/Ark (ticket 07).

## Test coverage

`uv run pytest` reports line + branch coverage of `ai_dev` (via `pytest-cov`, configured in
`pyproject.toml`). The committed v0.4 baseline is **91%** (683 tests).

Coverage is a **soft target**, not a gate: there is no `--cov-fail-under` threshold, so a missed
line never breaks the build. The goal is to keep this number honest and trending up — prefer a real
test at a genuine seam over a synthetic one that only moves the gauge. Regressions that drop
meaningful coverage should be called out in review, not blocked by CI.

## Usage

```bash
uv sync                                      # one-time: set up the env (see DEVELOPMENT.md)
uv run ai-dev create-feature-run "<intent>"  # creates .ai-dev/features/FEATURE-NNN/ (§6 skeleton)
```

For environment setup, testing, typechecking, and debugging, see
[**DEVELOPMENT.md**](DEVELOPMENT.md).
