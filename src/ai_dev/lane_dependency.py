"""Lane dependency DAG validation and precondition checks (v0.7, ADR-0009 D4).

ADR-0009 D4 makes ``depends_on`` a start precondition: a lane cannot begin
execution until all its dependency lanes have passed their lane gates. This
module provides:

* ``validate_lane_graph`` — structural validation: cycles, unknown references,
  self-dependencies. Runs at promote time and (defensively) at lane execution
  time.
* ``check_dependency_precondition`` — runtime check: for a given lane, are all
  its dependencies gate-passed? Uses ``lane-decision.json`` verdict (NOT the
  implementer's ``proposed_done`` status — a lane is not "done" until the lane
  gate evaluates it).
* ``aggregate_lane_gate_states`` — read-only feature-level aggregation of every
  declared lane's gate state. Does NOT compute a feature-level verdict (that is
  the coherence gate's job).

No model is called here (invariant #2).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ai_dev.json_artifact import read_json_object
from ai_dev.lane_gate import LANE_DECISION_JSON
from ai_dev.paths import lane_dir
from ai_dev.status import declared_lane_ids, lane_graph_ids
from ai_dev.templates import LANE_GRAPH_YML

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DependencyPrecheckResult:
    """Outcome of checking a lane's dependency preconditions.

    ``passed=True`` when all dependencies have passing lane gate verdicts
    (or when the lane has no dependencies). ``blocked_by`` lists the lane
    ids whose gate is not yet pass; ``details`` maps each blocker to a
    human-readable reason.
    """

    lane_id: str
    passed: bool
    blocked_by: list[str]
    details: dict[str, str]

@dataclass(frozen=True)
class LaneGateState:
    """Read-only projection of one lane's gate state.

    ``passed=True`` when ``lane-decision.json`` exists and its ``decision``
    is ``"pass"``. ``passed=False`` means either no decision yet, or a
    failing decision. ``decision_path`` is the absolute path to the lane's
    ``lane-decision.json``, or ``None`` when it does not exist.
    """

    lane_id: str
    passed: bool
    decision_path: Path | None
    blocker_count: int
    failed_conditions: list[str]

# ---------------------------------------------------------------------------
# Internal: lane graph parsing and validation
# ---------------------------------------------------------------------------


def _parse_lane_graph(feature_root: Path) -> list[dict[str, Any]]:
    """Read ``04-lane-graph.yml`` and return its ``lanes`` list.

    Fail-loud (§24.2) on missing/malformed file — a feature run always has a
    valid lane-graph, so an unreadable file is genuine corruption.
    """
    path = feature_root / LANE_GRAPH_YML
    if not path.is_file():
        raise ValueError(f"{LANE_GRAPH_YML} missing at {path} (§7.5)")
    try:
        graph = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ValueError(
            f"{LANE_GRAPH_YML} at {path} is not valid YAML: {exc} (§7.5)"
        ) from exc
    if not isinstance(graph, dict):
        raise ValueError(f"{LANE_GRAPH_YML} at {path} is not a mapping (§7.5)")
    lanes = graph.get("lanes")
    if not isinstance(lanes, list):
        raise ValueError(f"{LANE_GRAPH_YML} at {path} has no 'lanes' list (§7.5)")
    return list(lanes)


def _require_str_list(raw: Any, name: str, lane_id: str) -> list[str]:
    """Coerce a lane-entry list field to ``list[str]``, fail-loud on bad shape.

    Mirrors ``implement_leg._require_str_list`` so this module does not depend
    on that private helper. The duplicate is intentional — the lane graph
    validation is a structural check that should stay self-contained.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(
            f"lane {lane_id!r} field {name!r} in {LANE_GRAPH_YML} must be a list"
        )
    return [str(item) for item in raw]


def _detect_cycles(lane_ids: list[str], adjacency: dict[str, list[str]]) -> list[str] | None:
    """Return a cycle path if the dependency graph contains a cycle, else ``None``.

    Uses iterative three-color DFS: WHITE=unvisited, GRAY=in-current-path,
    BLACK=fully-explored. On a back-edge to a GRAY node, reconstructs and
    returns the cycle path as a list of lane ids (e.g.
    ``["LANE-001", "LANE-002", "LANE-001"]``). The cycle always starts and
    ends with the same lane id so the message reads naturally (``->`` join).
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {lid: WHITE for lid in lane_ids}
    parent: dict[str, str | None] = {lid: None for lid in lane_ids}

    for start in lane_ids:
        if color[start] != WHITE:
            continue
        stack: list[str] = [start]
        color[start] = GRAY
        # Iteration state: (node, neighbour_index) so we can resume after
        # returning from a recursive DFS branch without recursion.
        it_index: dict[str, int] = {lid: 0 for lid in lane_ids}

        while stack:
            node = stack[-1]
            neighbors = adjacency.get(node, [])
            idx = it_index.get(node, 0)
            if idx < len(neighbors):
                neighbor = neighbors[idx]
                it_index[node] = idx + 1
                if color.get(neighbor) == GRAY:
                    # Back-edge: cycle found. Reconstruct path from the
                    # stack *up to* the neighbour, then close the cycle.
                    cycle_start = stack.index(neighbor)
                    cycle = stack[cycle_start:] + [neighbor]
                    return cycle
                if color.get(neighbor) == WHITE:
                    color[neighbor] = GRAY
                    parent[neighbor] = node
                    stack.append(neighbor)
            else:
                color[node] = BLACK
                stack.pop()
    return None


def _validate_lane_entries(lanes: list[dict[str, Any]]) -> list[str]:
    """Validate lane entries for cycles, unknown refs, and self-dependencies.

    Operates on the in-memory lane dict list (rather than reading the file)
    so promote can validate *before* writing. Returns the sorted list of lane
    ids on success. Raises ``ValueError`` (§24.2) on:
    * duplicate lane ids
    * a lane depending on itself
    * a lane depending on an undeclared lane id
    * a dependency cycle
    """
    # Build id set + depends_on adjacency.
    lane_ids: list[str] = []
    seen: set[str] = set()
    depends_on: dict[str, list[str]] = {}

    for i, lane in enumerate(lanes):
        if not isinstance(lane, dict):
            raise ValueError(
                f"{LANE_GRAPH_YML} lane[{i}] is not a mapping (§7.5)"
            )
        lid = lane.get("id")
        if not isinstance(lid, str) or not lid:
            raise ValueError(
                f"{LANE_GRAPH_YML} lane[{i}] has no string id (§7.5)"
            )
        if lid in seen:
            raise ValueError(
                f"{LANE_GRAPH_YML} duplicate lane id {lid!r} (§7.5)"
            )
        seen.add(lid)
        lane_ids.append(lid)
        deps = _require_str_list(lane.get("depends_on"), "depends_on", lid)
        depends_on[lid] = deps

    # Check: self-dependency.
    for lid in lane_ids:
        if lid in depends_on.get(lid, []):
            raise ValueError(
                f"lane {lid!r} cannot depend on itself (§7.5)"
            )

    # Check: unknown references.
    known = set(lane_ids)
    for lid in lane_ids:
        for dep_id in depends_on.get(lid, []):
            if dep_id not in known:
                raise ValueError(
                    f"lane {lid!r} depends_on {dep_id!r} which is not declared "
                    f"in {LANE_GRAPH_YML}; known lanes: {sorted(known)} (§7.5)"
                )

    # Check: cycles.
    cycle = _detect_cycles(lane_ids, depends_on)
    if cycle is not None:
        raise ValueError(
            f"dependency cycle detected in {LANE_GRAPH_YML}: "
            f"{' -> '.join(cycle)} (§7.5)"
        )

    return sorted(lane_ids)


# ---------------------------------------------------------------------------
# Public: lane graph validation
# ---------------------------------------------------------------------------


def validate_lane_graph(feature_root: Path) -> list[str]:
    """Validate ``04-lane-graph.yml`` for structural correctness.

    Reads the lane graph, then runs the same checks as ``_validate_lane_entries``:
    cycles, unknown ``depends_on`` references, and self-dependencies all fail
    loud (§24.2 ``ValueError``). Returns the sorted list of lane ids on success.

    Call this at promote time (after writing the graph) and defensively at lane
    execution time (before ``check_dependency_precondition``). The validation is
    cheap (O(V+E)) and catches hand-edited corruption before it propagates.
    """
    lanes = _parse_lane_graph(feature_root)
    return _validate_lane_entries(lanes)


# ---------------------------------------------------------------------------
# Public: dependency precondition check
# ---------------------------------------------------------------------------


def check_dependency_precondition(
    repo_root: Path, feature_id: str, lane_id: str
) -> DependencyPrecheckResult:
    """Check whether ``lane_id``'s dependencies are all gate-passed.

    ADR-0009 D4: a lane cannot begin execution until every lane in its
    ``depends_on`` list has a **passing** ``lane-decision.json``. This function
    checks the lane-gate verdict (not the implementer's ``proposed_done``
    status — a lane is not "done" until the lane gate evaluates it, ADR-0009
    D5).

    First validates the lane graph (defense-in-depth against hand-edited
    corruption), then reads the lane's ``depends_on`` from ``04-lane-graph.yml``
    via ``read_lane_entry``. For each dependency lane:
    * no ``lane-decision.json`` → blocking (``"not yet executed"``)
    * ``decision != "pass"`` → blocking (``"lane gate verdict is '{decision}'"``)
    * ``decision == "pass"`` → satisfied

    Returns a ``DependencyPrecheckResult`` — the caller (implementer leg, CLI)
    decides whether to raise or handle gracefully. The check is a pure read;
    it writes nothing and appends no audit record.
    """
    feature_root = repo_root / ".ai-dev" / "features" / feature_id
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id} not found under {repo_root}")

    # Defense-in-depth: validate the lane graph before checking preconditions.
    # A hand-edited graph with cycles or unknown refs must not silently proceed.
    validate_lane_graph(feature_root)

    from ai_dev.implement_leg import read_lane_entry
    lane = read_lane_entry(feature_root, lane_id)
    deps = lane.depends_on
    if not deps:
        return DependencyPrecheckResult(
            lane_id=lane_id,
            passed=True,
            blocked_by=[],
            details={},
        )

    blocked_by: list[str] = []
    details: dict[str, str] = {}

    for dep_id in deps:
        decision_path = lane_dir(repo_root, feature_id, dep_id) / LANE_DECISION_JSON
        decision_doc = read_json_object(decision_path)
        if decision_doc is None:
            blocked_by.append(dep_id)
            details[dep_id] = "not yet executed (no lane-decision.json)"
            continue
        verdict = decision_doc.get("decision")
        if verdict == "pass":
            details[dep_id] = "lane gate passed"
        else:
            blocked_by.append(dep_id)
            details[dep_id] = f"lane gate verdict is {verdict!r}"

    return DependencyPrecheckResult(
        lane_id=lane_id,
        passed=len(blocked_by) == 0,
        blocked_by=blocked_by,
        details=details,
    )


# ---------------------------------------------------------------------------
# Public: feature-level lane gate state aggregation
# ---------------------------------------------------------------------------


def aggregate_lane_gate_states(
    repo_root: Path, feature_id: str
) -> dict[str, LaneGateState]:
    """Return a read-only aggregation of every declared lane's gate state.

    For each lane declared in ``04-lane-graph.yml`` + ``lane-status.yml``
    (the canonical lane set via ``declared_lane_ids``), reads the lane's
    ``lane-decision.json`` (if it exists) and builds a ``LaneGateState``.

    This is purely a read-side aggregation helper for feature-level
    visibility. It does **not** compute a feature-level verdict — that is the
    coherence gate's job (§18.5). A feature with all lanes passing is not
    automatically "done"; it still requires human integration (ADR-0009 D1).

    Writes nothing, appends no audit record.
    """
    feature_root = repo_root / ".ai-dev" / "features" / feature_id
    if not feature_root.is_dir():
        raise ValueError(f"feature run {feature_id} not found under {repo_root}")

    lane_ids = declared_lane_ids(feature_root)
    states: dict[str, LaneGateState] = {}

    for lid in lane_ids:
        decision_path = lane_dir(repo_root, feature_id, lid) / LANE_DECISION_JSON
        decision_doc = read_json_object(decision_path)
        if decision_doc is None:
            states[lid] = LaneGateState(
                lane_id=lid,
                passed=False,
                decision_path=None,
                blocker_count=0,
                failed_conditions=[],
            )
            continue
        verdict = decision_doc.get("decision")
        conditions = decision_doc.get("conditions")
        conditions_list = conditions if isinstance(conditions, list) else []
        failed = [
            str(c.get("name", "")) for c in conditions_list
            if isinstance(c, dict) and not c.get("passed")
        ]
        states[lid] = LaneGateState(
            lane_id=lid,
            passed=verdict == "pass",
            decision_path=decision_path.resolve(),
            blocker_count=decision_doc.get("blocking_issue_count", 0),
            failed_conditions=failed,
        )

    return states
