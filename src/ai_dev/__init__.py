"""ai-dev — multi-Agent Profile orchestrator (v0 walking skeleton).

Package layout is sized so the v0.0 tracer-bullet tickets each expand one module:

- ``feature_ids`` — stable ID allocation. v0.0 ticket 01 only allocates the
  ``FEATURE-NNN`` run id; ticket 03 generalizes to all 12 §5.2 id types.
- ``status`` — canonical status writer. v0.0 ticket 01 writes the initial
  ``feature-status.yml`` only; ticket 04 adds freeze + lane/task status.
- ``audit`` — audit log appender. v0.0 ticket 02 writes the structured
  ``audit.log.md`` + ``audit.log.json`` double product (§4.4) every later ticket
  reuses to record canonical-state / gate / decision / run events.
- ``feature_run`` — the ``create-feature-run`` orchestration (this ticket).
- ``cli`` — ``ai-dev`` console entry point.
"""

__version__ = "0.0.1"
