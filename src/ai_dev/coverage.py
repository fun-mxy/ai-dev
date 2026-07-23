"""coverage - the freeze-gate coverage precheck (v0.6 ticket 03, ADR-0008 D3).

Coverage-completeness is the **freeze-gate** half of ADR-0008's coverage split
(reference-integrity is the promote-time half, in ``promote``). A planning
proposal is *expected* to be incomplete while being refined, so completeness is
checked at the **freeze** action, not on draft promotes: a gap refuses to freeze
(-> back to refinement or Human Triage) and does **not** self-heal.

This module is the reusable helper the freeze gate calls. Each stage with an
upstream coverage invariant gets a ``<stage>_coverage`` function returning a
:class:`CoverageResult`; :func:`freeze_gate_coverage` dispatches by artifact.
Ticket 03 wires the **design** stage (every REQ in >=1 design
``requirement_mapping``, §18.2 "design 是否覆盖 REQ/AC"); ticket 04 will add the
tasks variant (every REQ+DES in some task's refs) with no change to this
dispatch shape.

The human gate keeps the *semantic* judgment (does this DES realize REQ-003's
intent?); *structural* coverage - every upstream id referenced at least once -
is the machine's job, computed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ai_dev.json_artifact import read_json_object
from ai_dev.promote import DESIGN_ARTIFACT, frozen_req_ids, read_frozen_requirements_doc
from ai_dev.templates import DESIGN_JSON


@dataclass(frozen=True)
class CoverageResult:
    """The freeze-gate coverage verdict for one artifact.

    ``uncovered`` is the sorted tuple of upstream ids the proposal failed to
    cover; empty iff coverage passes (``ok``). ``covered`` is the set of upstream
    ids the proposal referenced at least once. The CLI prints ``uncovered`` on a
    gap so the human knows which REQs to map (or route to Triage).
    """

    artifact: str
    # The §5.2 id type of the upstream items (``"REQ"`` for design).
    upstream_type: str
    covered: frozenset[str]
    uncovered: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """Whether coverage passes (no upstream id left uncovered)."""
        return not self.uncovered

    def refusal_message(self, artifact: str, *, would: bool = False) -> str:
        """The freeze-gate coverage-refusal message for this result.

        One source for the message both the real CLI (``would=False`` - an actual
        failure) and the dry-run plan (``would=True`` - a preview) print, so the
        wording cannot drift between the two freeze paths (ADR-0008 D3 / §18.2).
        """
        uncovered = ", ".join(self.uncovered) or "(none)"
        verb = "would FAIL" if would else f"FAILED for {artifact!r}"
        return (
            f"freeze-gate coverage precheck {verb}: {len(self.uncovered)} "
            f"{self.upstream_type} id(s) not referenced in any {artifact} mapping "
            f"- {uncovered}. Refine the proposal (generate-{artifact} --feedback) "
            f"to cover them, or route to Human Triage (ADR-0008 D3 / §18.2)."
        )


def design_coverage(feature_root: Path) -> CoverageResult:
    """Every frozen REQ must appear in >=1 design ``requirement_mapping`` (§18.2).

    Reads the frozen ``01-requirements.json`` (the upstream REQ id set - fails
    loud if requirements is not frozen, via :func:`read_frozen_requirements_doc`)
    and the canonical ``02-design.json`` (the proposal promote wrote). Collects
    every ``requirement`` ref across ``requirement_mapping`` entries; any frozen
    REQ not so referenced is uncovered -> the freeze gate refuses. Only refs that
    are real frozen REQ ids count as covered (promote's reference-integrity
    already guaranteed every mapping ``requirement`` resolves to a real REQ, so
    intersecting with the upstream set is belt-and-braces against a hand-edited
    canonical file).
    """
    req_doc = read_frozen_requirements_doc(feature_root)
    req_ids: set[str] = set(frozen_req_ids(req_doc))

    design_doc = read_json_object(feature_root / DESIGN_JSON)
    if design_doc is None:
        raise ValueError(
            f"{DESIGN_JSON} missing or unreadable at {feature_root}; nothing to "
            f"freeze (promote a design proposal first, §24.2)"
        )
    covered: set[str] = set()
    for entry in design_doc.get("requirement_mapping", []) or []:
        if not isinstance(entry, Mapping):
            continue
        ref = entry.get("requirement")
        if isinstance(ref, str) and ref in req_ids:
            covered.add(ref)
    uncovered = sorted(req_ids - covered)
    return CoverageResult(
        artifact=DESIGN_ARTIFACT,
        upstream_type="REQ",
        covered=frozenset(covered),
        uncovered=tuple(uncovered),
    )


# Artifact -> coverage function. ``design`` wired in ticket 03; ``tasks`` (ticket
# 04) adds its entry. Artifacts with no coverage precheck (``requirements`` = the
# root, no upstream; ``lane_graph`` shares the task gate's freeze window) are
# absent -> :func:`freeze_gate_coverage` returns ``None`` (no precheck).
_FREEZE_GATE_COVERAGE: dict[str, Any] = {
    DESIGN_ARTIFACT: design_coverage,
}


def freeze_gate_coverage(
    artifact: str, feature_root: Path
) -> CoverageResult | None:
    """The coverage precheck for ``artifact``, or ``None`` when the stage has none.

    The freeze gate calls this before flipping the frozen flag; a returned result
    with ``uncovered`` gaps refuses the freeze (ADR-0008 D3). Returns ``None`` for
    artifacts with no upstream coverage invariant: ``requirements`` (the root -
    no upstream) and ``lane_graph`` (shares the task gate's window). ``tasks``
    gets its own precheck in ticket 04.
    """
    fn = _FREEZE_GATE_COVERAGE.get(artifact)
    if fn is None:
        return None
    return fn(feature_root)
