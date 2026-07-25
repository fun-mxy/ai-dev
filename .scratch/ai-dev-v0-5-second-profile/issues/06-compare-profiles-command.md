# 06 - `compare-profiles` command + projection

**What to build:** A new read-only `compare-profiles FEATURE-NNN --profiles cc-glm52,codex-default`
command (decision #3) that projects a side-by-side comparison of two feature-runs (identical intent,
one profile each, full pipeline each) into `projections/profile-comparison.{json,md}` - a non-canonical
projection like `final-report` (ADR-0003). The lane/RUN model is untouched: comparison reads two
existing `final-report`s + audit timelines (two parallel feature-runs, the chosen orchestration).
Metrics: per-leg `elapsed_ms`, verifier pass/fail, final verdict + `failure_class`, issue count by
severity (reviewer profile noted), and **requirement coverage** (Q2/Q3 - the quality axis, real via
ticket 05 / ADR-0007). `meta.known_gaps` records caveats (e.g. reviewer-variance, planner
non-determinism from independent planning). Read-only, non-canonical, re-computable; no gate effect.

**Blocked by:** 04 (two runnable profiles), 05 (quality axis = requirement coverage).

**Status:** done

- [x] `compare-profiles` read-only command; reads two feature `final-report`s + audit timelines
- [x] emits `projections/profile-comparison.{json,md}` (non-canonical, ADR-0003-style)
- [x] metrics: per-leg `elapsed_ms`, verifier pass/fail, verdict + `failure_class`, issue count by severity, requirement coverage
- [x] lane/RUN model untouched (two parallel feature-runs)
- [x] `meta.known_gaps` for caveats (reviewer-variance, planner non-determinism)
- [x] global `--json` support (v0.4 ticket 03)
- [x] tests at the public seam; `uv run mypy` green
