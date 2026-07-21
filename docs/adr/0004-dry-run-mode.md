# ADR-0004: `--dry-run` mode (plan + check, skip the side effect)

- **Status:** Accepted
- **Date:** 2026-07-21
- **Supersedes / amends:** amends spec §26.5 (v0.4 polish item "dry-run mode"); adds a glossary pin for `dry-run`. Relies on the existing §24.2 precondition / legality seams; no earlier ADR is amended.
- **v0.4 scope:** the **only new capability** of v0.4 polish (§26.5). Every other v0.4 item sharpens an existing surface; this one adds a flag.

## Context

Every non-read command in v0.0–v0.3 is a *commit*: it either spawns a `claude`
subprocess (expensive — a real model call against a paid backend) or writes
canonical state (irreversible — frozen flags, gate verdicts, triage records,
stable ids). An operator cannot ask "what *would* `implement LANE-001` / `triage
ISSUE-007 --disposition override` / `coherence-gate` do?" without paying that
cost. §26.5 asks for a dry-run mode that runs the command's full *planning +
§24.2 precondition + legality check* but skips the expensive/irreversible step.

The locked design (ticket 04, option B): attach a `--dry-run` flag to the
**side-effect** commands only. Agent commands (`run-headless` / `implement` /
`review` / `spec-gap` / `fix-run`) skip the claude spawn; deterministic commands
(`freeze` / `triage` / `coherence-gate` / `final-report` / `lane-gate`) skip the
canonical-state write but **still run every legality check** (so dry-run
`triage` validates a disposition without recording it). Whatever is computed
before the skip point — the input package, the resolved profile, the exact claude
invocation + allowed-files boundary, the would-be verdict — is printed as a plan
and the command exits 0.

Already-pure / read-only commands (`show-profile`, `validate-run`, the v0.4
read-only commands of ticket 03) get **no** flag: a dry-run flag on a command
with no side effects is noise.

## Decisions

### D1 - Dry-run never mints a stable id; render the would-be package to a temp dir

**The critical invariant (glossary pin `dry-run`):** dry-run never mints a stable
id. It must **not** flow through `prepare_run` / `allocate_id`, because that
path consumes the monotonic `RUN-NNN` counter (`id-counters.yml`) and writes
`runs/RUN-NNN/input/`. Instead the *would-be* §12.2 input package is rendered
into a `tempfile.mkdtemp()` directory; the feature-run tree is left
byte-for-byte unchanged and the `RUN` / `DEC` counters do not move.

The alternative — *allocate-and-skip* (call `prepare_run`, then skip the spawn)
— is rejected on principle:

| | allocate-and-skip | **temp-dir (chosen)** |
| --- | --- | --- |
| lines of code | fewer (reuses `prepare_run` verbatim) | one extra render helper |
| monotonic `RUN` counter | **burned** — every dry-run wastes an id | untouched |
| feature-run tree | **orphans a `runs/RUN-NNN/` dir** | unchanged |
| "dry" semantics | violated (a write happened) | holds |

Burning a monotonic id on a preview corrupts the `RUN-NNN` sequence (the next
*real* run gets `RUN-002` after a dry-run, leaving a gap), and an orphaned
`runs/RUN-NNN/` directory with a full input package but no result is
indistinguishable from a crashed run. Both violate traceability. The temp-dir
path renders the same package via the newly-public `write_input_package_to`
(`run_prepare.py`) — a thin wrapper over the existing `_write_input_package` — so
dry-run reuses the exact package content without the allocation.

`run-headless` is the exception: the run is *already* prepared (`RUN-NNN` exists
from a prior `prepare-run`), so there is no package to render and no temp dir —
dry-run builds the exact claude argv against the real run directory and stops.

### D2 - Skip-point table (per command: what runs vs what is skipped)

| Command | Runs (planning + precondition + legality) | Skips |
| --- | --- | --- |
| `run-headless` | run dir exists; token source set (name only); argv + boundary computed | claude spawn; `metadata.json` / logs |
| `implement` | frozen tasks+lane_graph; task text; lane entry; allowed-files; package→temp dir; argv | `RUN-NNN` mint; spawn; `proposed_done` writeback; `implement-result` rollup |
| `review` / `spec-gap` | frozen; implement-result exists; issues-schema package→temp dir; argv | `RUN-NNN` mint; spawn; lane report |
| `fix-run` | feature exists; budget not exhausted; ≥1 `request_fix` target | the whole implement→review→spec-gap→verify→collect chain; budget consumption |
| `freeze` | artifact known; not already frozen | the flag flip; gate advance |
| `triage` | issue exists; severity valid; legality matrix; reason-presence; fix-loop budget | `triage` write; `DEC-NNN` mint; lifecycle advance |
| `coherence-gate` | sequencing guard; the three D1 conditions; verdict | `current_gate`/`verdict`/`feature.status` write; `coherence-decision` |
| `lane-gate` | precondition reads; the five conditions; decision | `lane-decision.{md,json}` |
| `final-report` | verdict present + pass/fail; coherence-decision + lane bundles exist; full projection | `final-report.{md,json}` |

### D3 - Refusal is reported, not raised; preconditions still fail loud

A §24.2 **precondition** failure (missing feature/lane/issue, unknown
disposition, unfrozen artifacts, bad gate sequencing) raises `ValueError`
exactly as the real command does — surfaced as a clean `error:` line, exit 1.
Dry-run does not weaken fail-loud.

A **legality refusal** the real command would raise (illegal triage cell,
disarming-without-reason, exhausted fix-loop budget, already-frozen artifact) is
*reported inside the plan* (`would be refused: …`) and exits **0**. Dry-run
answers "what would happen?", and "this would be refused" is a successful answer.
The two are distinguishable: a precondition breach means the operator called the
wrong command; a refusal means the command ran its checks and the law said no.

### D4 - Dry-run writes nothing — including no audit append

**Dry-run writes nothing to the feature-run tree.** No canonical state, no
report artifacts, and **no `audit.log` append** — the strongest reading of the
ticket's "feature 树零改动 / 不写任何 canonical state". This is the locked
decision (the ticket's `origin=dry-run` audit item is a forward-reference to
ticket 02's `origin` audit field, which is parallel and not yet landed).

The alternative — emit one `origin=dry-run` audit event per dry-run, treating
`audit.log` as traceability rather than canonical state — is **deferred**. It
would require landing ticket 02's `origin` parameter on `append_audit_event`
first; doing it here would overlap with that ticket and risk a merge conflict on
the audit schema. When ticket 02 lands, a follow-up can add an optional dry-run
audit emission tagged `origin=dry-run` without revisiting this ADR's skip points.
ADR-0004 records the deferral so the gap is visible, not silent.

### D5 - Architecture: a dedicated `dry_run.py` module, not threaded `dry_run=` params

Each side-effect command's planning/precondition logic is reached through a
dedicated `plan_*` helper in a new `src/ai_dev/dry_run.py`, which **reuses the
existing pure read/compute helpers** (`frozen_artifacts_status`,
`read_task_text`, `read_lane_entry`, `lane_allowed_files`, `_matrix_cell`,
`fix_loop_budget_exhausted`, `token_source_var`, `build_prompt`, `build_cli_flags`,
…) and stops before the write/spawn. The alternative — threading a `dry_run=True`
parameter through every `run_*_leg` / `evaluate_*` / `apply_triage` function and
guarding each write — is rejected: it re-tests every well-trodden path and
entangles the dry-run concern with the execution concern.

To share the *verdict computation* between the real writer and the planner
without duplication, three small pure-compute helpers were extracted (the
writers now call them, then write):
- `coherence_gate.compute_coherence(feature_root) -> CoherenceCompute`
- `lane_gate.compute_lane_decision(repo_root, feature_id, lane_id) -> LaneGateCompute`
- `final_report.compute_final_report(feature_root) -> FinalReportCompute`

The planner and the writer can never diverge because they call the same function.

## Consequences

- An operator can preview any side-effect command at zero cost (no model call,
  no state mutation, no id burned). The monotonic id space and the feature-run
  tree are safe to dry-run against repeatedly.
- Dry-run triage is a real *legality validator*: it answers "is this disposition
  legal for this severity, and would it mint a DEC?" without recording it.
- The audit trail does **not** record dry-runs (D4). If an operator needs to know
  "was a dry-run performed?", that is invisible until ticket 02's `origin` field
  lands. This is an accepted, documented gap.
- Three gate/report modules gained a public pure-compute helper each. Existing
  tests stay green (the writers delegate to them unchanged in behaviour); the
  surface area added is read-only compute, no new writes.
