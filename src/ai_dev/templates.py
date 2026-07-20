"""§7 artifact template seeder (ticket 05).

When a feature run is created (ticket 01), the four §7 artifacts are seeded as
structured templates so the v0.2 Planner has well-formed scaffolding to fill
rather than a blank directory:

* ``01-requirements.md`` / ``.json``  (§7.2)
* ``02-design.md``       / ``.json``  (§7.3)
* ``03-tasks.md``                      (§7.4 — markdown only, no machine mirror)
* ``04-lane-graph.yml``                (§7.5)

Two cross-cutting requirements shape every template:

* **Frozen marker (§4.2).** Each carries ``frozen: false`` at creation. The
  authoritative machine-readable frozen state lives in the json/yaml product
  (flipped by ticket 04's deterministic writer) and is aggregated in
  ``status/feature-status.yml``; the markdown mirrors it for humans.
* **Stable-id placeholders (§5.2).** REQ / AC / DES / TASK slots are seeded
  empty — those ids are allocated by the Planner during the requirements /
  design / task gates (ticket 03's allocator), not invented at creation. Only
  the lane is structural: every feature run gets exactly one lane (§5.3 MVP),
  so ``04-lane-graph.yml`` is seeded with a *real* lane id allocated upstream
  by ticket 03 and passed in here — never a placeholder string.

``seed_artifact_templates`` is a pure writer: it takes the already-allocated
``lane_id`` rather than allocating itself, keeping the allocation side-effect
(counter write + audit) in ``feature_ids.allocate_id`` where it belongs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ai_dev.json_artifact import write_json

REQUIREMENTS_MD = "01-requirements.md"
REQUIREMENTS_JSON = "01-requirements.json"
DESIGN_MD = "02-design.md"
DESIGN_JSON = "02-design.json"
TASKS_MD = "03-tasks.md"
LANE_GRAPH_YML = "04-lane-graph.yml"

# §7.5 merge_policy default for a freshly seeded lane. The §7.5 *example* shows
# a filled lane (``auto_merge: true``, ``["format-only", "lockfile-refresh"]``);
# this is a template, so content the Planner decides starts empty/conservative —
# ``auto_merge: false`` (opt in at the lane gate) and no pre-baked mechanical
# resolutions. ``human_triage`` is the only semantic-conflict policy §7.5 names,
# so it stands as the safe default.
_DEFAULT_MERGE_POLICY: dict[str, Any] = {
    "auto_merge": False,
    "allowed_mechanical_resolutions": [],
    "semantic_conflict_policy": "human_triage",
}


def _requirements_payload(feature_id: str) -> dict[str, Any]:
    """§7.2 requirements document, in spec field order, seeded unfrozen.

    ``requirements`` / ``acceptance_criteria`` are the REQ-NNN / AC-NNN
    placeholder slots (empty until the Planner allocates ids).
    """
    return {
        "feature": feature_id,
        "frozen": False,
        "requirements": [],
        "acceptance_criteria": [],
        "priority": None,
        "scope": None,
        "constraints": [],
        "open_questions": [],
    }


def _requirements_md(feature_id: str) -> str:
    return (
        f"# Requirements — {feature_id}\n"
        f"\n"
        f"Frozen: false\n"
        f"\n"
        f"> Stable IDs (REQ-NNN / AC-NNN) are allocated by the Planner via the\n"
        f"> stable-id allocator and recorded in `{REQUIREMENTS_JSON}`. This\n"
        f"> markdown is the human mirror; the JSON is canonical for machines.\n"
        f"\n"
        f"## Requirements (REQ-NNN)\n"
        f"\n"
        f"_Placeholder — the Planner fills these during the requirements gate._\n"
        f"\n"
        f"## Acceptance criteria (AC-NNN)\n"
        f"\n"
        f"_Placeholder._\n"
        f"\n"
        f"## Priority\n"
        f"\n"
        f"_TBD._\n"
        f"\n"
        f"## Scope\n"
        f"\n"
        f"_TBD._\n"
        f"\n"
        f"## Constraints\n"
        f"\n"
        f"_None yet._\n"
        f"\n"
        f"## Open questions\n"
        f"\n"
        f"_None yet._\n"
    )


def _design_payload(feature_id: str) -> dict[str, Any]:
    """§7.3 design document, in spec field order, seeded unfrozen.

    ``design_elements`` is the DES-NNN placeholder slot.
    """
    return {
        "feature": feature_id,
        "frozen": False,
        "design_elements": [],
        "architecture_decision": None,
        "data_model": None,
        "api_cli_contract": None,
        "file_layout": None,
        "invariants": [],
        "risks": [],
        "dependencies": [],
        "requirement_mapping": [],
    }


def _design_md(feature_id: str) -> str:
    return (
        f"# Design — {feature_id}\n"
        f"\n"
        f"Frozen: false\n"
        f"\n"
        f"> Design elements (DES-NNN) are allocated by the Planner via the\n"
        f"> stable-id allocator and recorded in `{DESIGN_JSON}`. This markdown\n"
        f"> is the human mirror; the JSON is canonical for machines.\n"
        f"\n"
        f"## Design elements (DES-NNN)\n"
        f"\n"
        f"_Placeholder — the Planner fills these during the design gate._\n"
        f"\n"
        f"## Architecture decision\n"
        f"\n"
        f"_TBD._\n"
        f"\n"
        f"## Data model\n"
        f"\n"
        f"_TBD._\n"
        f"\n"
        f"## API / CLI contract\n"
        f"\n"
        f"_TBD._\n"
        f"\n"
        f"## File layout\n"
        f"\n"
        f"_TBD._\n"
        f"\n"
        f"## Invariants\n"
        f"\n"
        f"_None yet._\n"
        f"\n"
        f"## Risks\n"
        f"\n"
        f"_None yet._\n"
        f"\n"
        f"## Dependencies\n"
        f"\n"
        f"_None yet._\n"
        f"\n"
        f"## Requirement mapping (→ REQ / AC)\n"
        f"\n"
        f"_None yet._\n"
    )


def _tasks_md(feature_id: str) -> str:
    # §7.4: markdown is the human task list; checkboxes are NOT canonical state.
    return (
        f"# Tasks — {feature_id}\n"
        f"\n"
        f"Frozen: false\n"
        f"\n"
        f"> Canonical task state lives in `status/task-status.yml`, written by\n"
        f"> the orchestrator only (§4.3). Checkboxes below are for human\n"
        f"> convenience and are **not** canonical state. Task IDs (TASK-NNN)\n"
        f"> are allocated by the Planner via the stable-id allocator.\n"
        f"\n"
        f"## Tasks (TASK-NNN)\n"
        f"\n"
        f"_Placeholder — the Planner breaks work into TASK-NNN entries during\n"
        f"the task gate._\n"
        f"\n"
        f"- [ ] _none yet_\n"
    )


def _lane_graph_payload(feature_id: str, lane_id: str) -> dict[str, Any]:
    """§7.5 lane graph: one lane (§5.3 MVP) carrying the full lane-entry shape.

    The single lane's ``id`` is the real id allocated upstream by ticket 03 —
    the graph references it rather than inventing a string. Content fields the
    Planner fills (purpose, tasks, files, scope…) seed empty; the lane-entry
    structure stays complete so the format is extensible to more lanes later.

    The top-level ``feature``/``frozen`` keys are provenance/metadata sat above
    the §7.5 ``lanes:`` content — the lane *entry* shape (the part §7.5 fixes)
    is preserved exactly. ``feature`` mirrors the owning-feature key the other
    machine artifacts carry (and ``final-report.json`` set the precedent for),
    so all feature-run machine files are self-describing about their owner.
    """
    return {
        "feature": feature_id,
        "frozen": False,
        "lanes": [
            {
                "id": lane_id,
                "purpose": None,
                "tasks": [],
                "depends_on": [],
                "expected_files": [],
                "exclusive_files": [],
                "provides": [],
                "consumes": [],
                "verification_scope": [],
                "merge_policy": dict(_DEFAULT_MERGE_POLICY),
            }
        ],
    }


def seed_artifact_templates(
    feature_root: Path, feature_id: str, lane_id: str
) -> list[Path]:
    """Seed the four §7 artifact templates under ``feature_root``.

    Writes ``01-requirements`` (md+json), ``02-design`` (md+json),
    ``03-tasks.md`` and ``04-lane-graph.yml``, all unfrozen with empty stable-id
    placeholders, and ``04-lane-graph.yml`` referencing the supplied (allocated)
    ``lane_id``. Returns the paths written, in spec order.
    """
    paths: list[Path] = []

    (feature_root / REQUIREMENTS_MD).write_text(_requirements_md(feature_id))
    paths.append(feature_root / REQUIREMENTS_MD)
    write_json(feature_root / REQUIREMENTS_JSON, _requirements_payload(feature_id))
    paths.append(feature_root / REQUIREMENTS_JSON)

    (feature_root / DESIGN_MD).write_text(_design_md(feature_id))
    paths.append(feature_root / DESIGN_MD)
    write_json(feature_root / DESIGN_JSON, _design_payload(feature_id))
    paths.append(feature_root / DESIGN_JSON)

    (feature_root / TASKS_MD).write_text(_tasks_md(feature_id))
    paths.append(feature_root / TASKS_MD)

    lane_graph_path = feature_root / LANE_GRAPH_YML
    with lane_graph_path.open("w") as f:
        yaml.safe_dump(
            _lane_graph_payload(feature_id, lane_id),
            f,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )
    paths.append(lane_graph_path)

    return paths
