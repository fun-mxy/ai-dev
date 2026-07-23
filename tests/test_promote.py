"""promote — the deterministic requirements stitcher/renderer (v0.6 ticket 01).

ADR-0008 D1/D2/D3: ``promote`` is the sole deterministic writer that turns a
Planner run's id-free proposal into the canonical unfrozen artifact. It (1)
allocates REQ/AC ids from the counter, (2) stitches each AC's local REQ ref to
its allocated id via the generic :class:`RefResolver`, (3) writes
``01-requirements.json`` and renders ``01-requirements.md`` (the sole md
renderer), and enforces reference-integrity (D3 — fail loud on an unresolvable
ref). These tests exercise the seam with synthetic proposals; no model runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ai_dev.feature_run import create_feature_run
from ai_dev.promote import (
    FrozenArtifactWriteError,
    PromoteResult,
    RefResolver,
    UnresolvedRefError,
    build_canonical_design,
    build_canonical_requirements,
    build_canonical_tasks,
    promote_design,
    promote_requirements,
    promote_tasks,
    read_frozen_design_doc,
    read_frozen_requirements_doc,
    render_design_md,
    render_requirements_md,
    render_tasks_md,
)
from ai_dev.status import freeze_artifact
from ai_dev.templates import (
    DESIGN_JSON,
    DESIGN_MD,
    LANE_GRAPH_YML,
    REQUIREMENTS_JSON,
    TASKS_JSON,
    TASKS_MD,
)

FEATURE_ID = "FEATURE-001"


def _feature_root(tmp_path: Path) -> Path:
    """Create a real feature run (status + counters + seeded templates)."""
    create_feature_run(tmp_path, "build the foo")
    return tmp_path / ".ai-dev" / "features" / FEATURE_ID


def _proposal() -> dict:
    """A synthetic id-free requirements proposal with local refs."""
    return {
        "requirements": [
            {
                "key": "r1",
                "statement": "The system shall foo.",
                "priority": "must",
                "rationale": "users need foo",
            },
            {"key": "r2", "statement": "The system shall bar."},
        ],
        "acceptance_criteria": [
            {"key": "a1", "requirement": "r1", "criterion": "foo is observable"},
            {"key": "a2", "requirement": "r2", "criterion": "bar is observable"},
            {"key": "a3", "requirement": "r1", "criterion": "foo again"},
        ],
        "priority": "P0",
        "scope": {"in_scope": ["foo"], "out_of_scope": ["baz"]},
        "constraints": ["no network at runtime"],
        "open_questions": ["how fast is foo?"],
    }


# ---------------------------------------------------------------------------
# RefResolver — the generic local-ref resolver (reused by 03/04 unchanged).
# ---------------------------------------------------------------------------


class TestRefResolver:
    def test_resolve_local_key_to_allocated_id(self) -> None:
        r = RefResolver()
        r.register_local("REQ", "r1", "REQ-001")
        assert r.resolve("REQ", "r1") == "REQ-001"

    def test_resolve_upstream_canonical_id_to_itself(self) -> None:
        # Cross-stage: a design element referencing frozen REQ-001 resolves
        # directly because REQ-001 is in the upstream set (requirements is the
        # root and never registers upstream; design/tasks do).
        r = RefResolver()
        r.add_upstream("REQ", ["REQ-001", "REQ-002"])
        assert r.resolve("REQ", "REQ-001") == "REQ-001"
        assert r.resolve("REQ", "REQ-002") == "REQ-002"

    def test_local_takes_precedence_over_upstream(self) -> None:
        r = RefResolver()
        r.add_upstream("REQ", ["REQ-001"])
        r.register_local("REQ", "REQ-001", "REQ-099")
        # A local key that happens to look like an id still maps to its allocation.
        assert r.resolve("REQ", "REQ-001") == "REQ-099"

    def test_unresolvable_ref_fails_loud(self) -> None:
        r = RefResolver()
        r.register_local("REQ", "r1", "REQ-001")
        with pytest.raises(UnresolvedRefError, match="r9"):
            r.resolve("REQ", "r9")

    def test_per_type_namespaces_do_not_collide(self) -> None:
        r = RefResolver()
        r.register_local("REQ", "x", "REQ-001")
        r.register_local("DES", "x", "DES-001")
        assert r.resolve("REQ", "x") == "REQ-001"
        assert r.resolve("DES", "x") == "DES-001"

    def test_duplicate_local_key_fails_loud(self) -> None:
        r = RefResolver()
        r.register_local("REQ", "r1", "REQ-001")
        with pytest.raises(ValueError, match="duplicate local key"):
            r.register_local("REQ", "r1", "REQ-002")
        # Same id is idempotent (re-registration with the same mapping is fine).
        r.register_local("REQ", "r1", "REQ-001")

    def test_resolve_list_preserves_order_and_dedupes(self) -> None:
        r = RefResolver()
        r.register_local("REQ", "r1", "REQ-001")
        r.register_local("REQ", "r2", "REQ-002")
        assert r.resolve_list("REQ", ["r2", "r1", "r2"]) == ["REQ-002", "REQ-001"]

    def test_resolve_list_rejects_non_list(self) -> None:
        r = RefResolver()
        r.register_local("REQ", "r1", "REQ-001")
        with pytest.raises(UnresolvedRefError, match="expected a list"):
            r.resolve_list("REQ", "r1")

    def test_can_resolve(self) -> None:
        r = RefResolver()
        r.register_local("REQ", "r1", "REQ-001")
        r.add_upstream("DES", ["DES-001"])
        assert r.can_resolve("REQ", "r1")
        assert r.can_resolve("DES", "DES-001")
        assert not r.can_resolve("REQ", "r9")


# ---------------------------------------------------------------------------
# build_canonical_requirements — the pure id-allocate + stitch core.
# ---------------------------------------------------------------------------


class TestBuildCanonicalRequirements:
    def test_allocates_req_then_ac_ids_in_order(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        doc, allocated = build_canonical_requirements(
            root, FEATURE_ID, _proposal(), origin=None
        )
        assert allocated["REQ"] == ["REQ-001", "REQ-002"]
        assert allocated["AC"] == ["AC-001", "AC-002", "AC-003"]
        assert [r["id"] for r in doc["requirements"]] == ["REQ-001", "REQ-002"]
        assert [a["id"] for a in doc["acceptance_criteria"]] == [
            "AC-001",
            "AC-002",
            "AC-003",
        ]

    def test_stitches_ac_requirement_ref_to_allocated_req_id(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        doc, _ = build_canonical_requirements(
            root, FEATURE_ID, _proposal(), origin=None
        )
        # a1/a3 -> r1 -> REQ-001 ; a2 -> r2 -> REQ-002
        req_of = {a["key"]: a["requirement"] for a in doc["acceptance_criteria"]}
        assert req_of["a1"] == "REQ-001"
        assert req_of["a3"] == "REQ-001"
        assert req_of["a2"] == "REQ-002"
        # No local key leaks into the canonical ref — it is the stitched id.
        assert all(a["requirement"].startswith("REQ-") for a in doc["acceptance_criteria"])

    def test_carries_optional_facets_and_keeps_local_key_as_provenance(
        self, tmp_path: Path
    ) -> None:
        root = _feature_root(tmp_path)
        doc, _ = build_canonical_requirements(
            root, FEATURE_ID, _proposal(), origin=None
        )
        assert doc["feature"] == FEATURE_ID
        assert doc["frozen"] is False
        assert doc["priority"] == "P0"
        assert doc["scope"] == {"in_scope": ["foo"], "out_of_scope": ["baz"]}
        assert doc["constraints"] == ["no network at runtime"]
        assert doc["open_questions"] == ["how fast is foo?"]
        # The model's local key is retained as provenance on each entry.
        assert doc["requirements"][0]["key"] == "r1"
        assert doc["requirements"][0]["statement"] == "The system shall foo."

    def test_unresolvable_ac_ref_fails_loud(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        proposal = _proposal()
        proposal["acceptance_criteria"][0]["requirement"] = "r9"  # no such REQ
        with pytest.raises(UnresolvedRefError, match="r9"):
            build_canonical_requirements(root, FEATURE_ID, proposal, origin=None)

    def test_omits_optional_facets_when_absent(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        proposal = {
            "requirements": [{"key": "r1", "statement": "foo."}],
            "acceptance_criteria": [
                {"key": "a1", "requirement": "r1", "criterion": "foo works"}
            ],
        }
        doc, allocated = build_canonical_requirements(
            root, FEATURE_ID, proposal, origin=None
        )
        assert allocated == {"REQ": ["REQ-001"], "AC": ["AC-001"]}
        for facet in ("priority", "scope", "constraints", "open_questions"):
            assert facet not in doc
        # Optional prose on a requirement is omitted when absent.
        assert "priority" not in doc["requirements"][0]


# ---------------------------------------------------------------------------
# render_requirements_md — the sole md renderer (ADR-0008 D2).
# ---------------------------------------------------------------------------


class TestRenderRequirementsMd:
    def test_renders_ids_and_stitched_refs(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        doc, _ = build_canonical_requirements(
            root, FEATURE_ID, _proposal(), origin=None
        )
        md = render_requirements_md(FEATURE_ID, doc)
        # REQ + AC ids appear.
        assert "REQ-001" in md and "REQ-002" in md
        assert "AC-001" in md and "AC-002" in md and "AC-003" in md
        # The stitched canonical ref (not the local key) appears in the AC line.
        assert "(REQ-001): foo is observable" in md
        # Statements render as headings.
        assert "The system shall foo." in md
        # Frozen state mirrored from canonical JSON.
        assert "Frozen: false" in md

    def test_renders_empty_artifact_without_ids(self) -> None:
        doc = {
            "feature": FEATURE_ID,
            "frozen": False,
            "requirements": [],
            "acceptance_criteria": [],
        }
        md = render_requirements_md(FEATURE_ID, doc)
        assert "_None yet._" in md
        # No allocated-id headings/lines render for an empty artifact (the
        # header prose mentions "REQ-NNN" as a description, not a real id).
        assert "### REQ-" not in md
        assert "- **AC-" not in md

    def test_render_is_deterministic(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        doc, _ = build_canonical_requirements(
            root, FEATURE_ID, _proposal(), origin=None
        )
        assert render_requirements_md(FEATURE_ID, doc) == render_requirements_md(
            FEATURE_ID, doc
        )


# ---------------------------------------------------------------------------
# promote_requirements — the write/render/audit shell.
# ---------------------------------------------------------------------------


class TestPromoteRequirements:
    def test_writes_canonical_json_and_md_mirror(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        result = promote_requirements(root, FEATURE_ID, _proposal(), origin="cli")

        assert isinstance(result, PromoteResult)
        assert result.stage == "requirements"
        assert result.artifact == "requirements"
        assert result.json_path == root / "01-requirements.json"
        assert result.md_path == root / "01-requirements.md"
        assert result.json_path.is_file()
        assert result.md_path.is_file()

        doc = json.loads(result.json_path.read_text())
        assert doc["frozen"] is False
        assert [r["id"] for r in doc["requirements"]] == ["REQ-001", "REQ-002"]

    def test_allocates_from_counter_and_persists_it(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        promote_requirements(root, FEATURE_ID, _proposal(), origin="cli")
        counters = yaml.safe_load((root / "id-counters.yml").read_text())
        assert counters["REQ"] == 2
        assert counters["AC"] == 3

    def test_appends_promote_audit_event(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        promote_requirements(root, FEATURE_ID, _proposal(), origin="cli")
        records = json.loads((root / "audit.log.json").read_text())
        promote_records = [r for r in records if r["event"] == "promote"]
        assert len(promote_records) == 1
        payload = promote_records[0]["payload"]
        assert payload["stage"] == "requirements"
        assert payload["artifact"] == "requirements"
        assert payload["allocated"] == {"REQ": ["REQ-001", "REQ-002"], "AC": ["AC-001", "AC-002", "AC-003"]}
        assert promote_records[0]["origin"] == "cli"

    def test_unresolvable_ref_writes_nothing(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        # Seed a known canonical doc so we can assert it is untouched on failure.
        seed = json.loads((root / "01-requirements.json").read_text())
        proposal = _proposal()
        proposal["acceptance_criteria"][0]["requirement"] = "r9"
        with pytest.raises(UnresolvedRefError, match="r9"):
            promote_requirements(root, FEATURE_ID, proposal, origin="cli")
        # The canonical artifact and the audit log are unchanged — fail loud,
        # preserve artifacts (§24.2), no partial promote.
        assert json.loads((root / "01-requirements.json").read_text()) == seed
        records = json.loads((root / "audit.log.json").read_text())
        assert not any(r["event"] == "promote" for r in records)

    def test_timestamp_threads_to_every_audit_record(self, tmp_path: Path) -> None:
        # The injected timestamp lands on the promote record AND every id-
        # allocation record inside it, so a deterministic test sees one stamp
        # across the whole promote (not wall-clock for the allocate_id calls).
        root = _feature_root(tmp_path)
        stamp = "2026-07-23T00:00:00Z"
        promote_requirements(
            root, FEATURE_ID, _proposal(), origin="cli", timestamp=stamp
        )
        records = json.loads((root / "audit.log.json").read_text())
        # The promote's own records: the promote event + the REQ/AC allocations
        # it made (exclude the LANE allocation from feature-run creation).
        promote_records = [
            r
            for r in records
            if r["event"] == "promote"
            or (r["event"] == "allocate_id" and r["payload"].get("type") in ("REQ", "AC"))
        ]
        assert len(promote_records) == 6  # 1 promote + 2 REQ + 3 AC allocations
        assert {r["timestamp"] for r in promote_records} == {stamp}

    def test_refuses_to_overwrite_a_frozen_artifact(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        freeze_artifact(root, "requirements", origin="cli")
        with pytest.raises(FrozenArtifactWriteError):
            promote_requirements(root, FEATURE_ID, _proposal(), origin="cli")

    def test_re_promote_overwrites_and_reallocates_ids(self, tmp_path: Path) -> None:
        # CONTEXT Q7: re-running an unfrozen stage overwrites the canonical file
        # and re-allocates ids clean (counter is monotonic, gaps are acceptable).
        root = _feature_root(tmp_path)
        first = promote_requirements(root, FEATURE_ID, _proposal(), origin="cli")
        assert list(first.allocated["REQ"]) == ["REQ-001", "REQ-002"]
        second = promote_requirements(root, FEATURE_ID, _proposal(), origin="cli")
        # New ids allocated from the bumped counter; canonical file overwritten.
        assert list(second.allocated["REQ"]) == ["REQ-003", "REQ-004"]
        doc = json.loads((root / "01-requirements.json").read_text())
        assert [r["id"] for r in doc["requirements"]] == ["REQ-003", "REQ-004"]

    def test_re_promote_idempotent_md(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        promote_requirements(root, FEATURE_ID, _proposal(), origin="cli")
        first_md = (root / "01-requirements.md").read_text()
        # A second promote of the same proposal re-allocates ids but the md
        # structure is identical modulo the allocated ids.
        promote_requirements(root, FEATURE_ID, _proposal(), origin="cli")
        second_md = (root / "01-requirements.md").read_text()
        assert first_md.replace("REQ-001", "REQ-003").replace(
            "REQ-002", "REQ-004"
        ).replace("AC-001", "AC-004").replace("AC-002", "AC-005").replace(
            "AC-003", "AC-006"
        ) == second_md

    def test_rejects_empty_feature_id(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        with pytest.raises(ValueError, match="feature_id"):
            promote_requirements(root, "", _proposal(), origin="cli")


class TestPromoteMalformedProposalFailLoud:
    """§24.2: a malformed proposal fails loud rather than being silently coerced.

    The §14.1 schema check (validate-run) catches these before promote runs in
    the real flow, but promote is defensive — each shape surfaces a clean error
    and writes no canonical artifact.
    """

    def test_requirements_not_a_list(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        with pytest.raises(ValueError, match="'requirements' must be a list"):
            promote_requirements(
                root, FEATURE_ID, {"requirements": {}, "acceptance_criteria": []},
                origin="cli",
            )

    def test_requirement_entry_not_an_object(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        with pytest.raises(ValueError, match="requirements\\[0\\] must be an object"):
            promote_requirements(
                root,
                FEATURE_ID,
                {"requirements": ["not an object"], "acceptance_criteria": []},
                origin="cli",
            )

    def test_requirement_missing_key(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        with pytest.raises(ValueError, match="non-empty string 'key'"):
            promote_requirements(
                root,
                FEATURE_ID,
                {"requirements": [{"statement": "x"}], "acceptance_criteria": []},
                origin="cli",
            )

    def test_requirement_missing_statement(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        with pytest.raises(ValueError, match="non-empty string 'statement'"):
            promote_requirements(
                root,
                FEATURE_ID,
                {"requirements": [{"key": "r1"}], "acceptance_criteria": []},
                origin="cli",
            )

    def test_ac_missing_criterion(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        proposal = {
            "requirements": [{"key": "r1", "statement": "x"}],
            "acceptance_criteria": [{"key": "a1", "requirement": "r1"}],
        }
        with pytest.raises(ValueError, match="non-empty string 'criterion'"):
            promote_requirements(root, FEATURE_ID, proposal, origin="cli")

    def test_ac_missing_requirement_ref(self, tmp_path: Path) -> None:
        root = _feature_root(tmp_path)
        proposal = {
            "requirements": [{"key": "r1", "statement": "x"}],
            "acceptance_criteria": [{"key": "a1", "criterion": "c"}],
        }
        with pytest.raises(ValueError, match="non-empty string 'requirement'"):
            promote_requirements(root, FEATURE_ID, proposal, origin="cli")


class TestRenderRequirementsMdEdgeCases:
    def test_renders_non_list_constraints_as_inline(self) -> None:
        # constraints/open_questions are normally lists; a non-list value still
        # renders deterministically rather than crashing.
        doc = {
            "feature": FEATURE_ID,
            "frozen": True,
            "requirements": [],
            "acceptance_criteria": [],
            "constraints": "a single string constraint",
        }
        md = render_requirements_md(FEATURE_ID, doc)
        assert "a single string constraint" in md
        assert "Frozen: true" in md

    def test_renders_empty_constraints_list(self) -> None:
        doc = {
            "feature": FEATURE_ID,
            "frozen": False,
            "requirements": [],
            "acceptance_criteria": [],
            "constraints": [],
        }
        md = render_requirements_md(FEATURE_ID, doc)
        assert "_None._" in md


# ---------------------------------------------------------------------------
# Design stage (ticket 03): promote_design stitches requirement_mapping
# against the FROZEN requirements upstream (first live use of RefResolver's
# add_upstream path) + allocates DES ids + renders 02-design.{json,md}.
# ---------------------------------------------------------------------------


def _feature_with_frozen_requirements(
    tmp_path: Path, req_proposal: dict | None = None
) -> tuple[Path, list[str]]:
    """Create a feature run, promote + freeze requirements, return (root, req_ids).

    Design may only stitch against a *frozen* requirements artifact (ADR-0008
    D2), so every design test needs a feature whose requirements are promoted
    then frozen. Returns the allocated REQ ids (the upstream set design refs).
    """
    create_feature_run(tmp_path, "build the foo")
    root = tmp_path / ".ai-dev" / "features" / FEATURE_ID
    if req_proposal is None:
        req_proposal = {
            "requirements": [
                {"key": "r1", "statement": "The system shall foo."},
                {"key": "r2", "statement": "The system shall bar."},
            ],
            "acceptance_criteria": [
                {"key": "a1", "requirement": "r1", "criterion": "foo observable"}
            ],
        }
    promote_requirements(root, FEATURE_ID, req_proposal, origin="test")
    freeze_artifact(root, "requirements", origin="test")
    doc = json.loads((root / REQUIREMENTS_JSON).read_text())
    return root, [r["id"] for r in doc["requirements"]]


def _design_proposal(req_ids: list[str]) -> dict:
    """A synthetic id-free design proposal referencing frozen REQ ids + local DES keys."""
    return {
        "design_elements": [
            {
                "key": "d1",
                "name": "Greeting module",
                "description": "formats the greeting string",
            },
            {"key": "d2", "name": "Exit-code handling", "type": "module"},
        ],
        "requirement_mapping": [
            {
                "key": "m1",
                "requirement": req_ids[0],
                "design_elements": ["d1"],
                "rationale": "d1 realizes the greeting REQ",
            },
            {"key": "m2", "requirement": req_ids[1], "design_elements": ["d1", "d2"]},
        ],
        "architecture_decision": "Single-module CLI",
        "data_model": {"Greeting": {"name": "str"}},
        "file_layout": ["src/greet.py"],
        "invariants": ["greeting is deterministic"],
        "risks": ["locale differences"],
        "dependencies": ["stdlib only"],
    }


class TestReadFrozenRequirementsDoc:
    def test_returns_doc_when_requirements_frozen(self, tmp_path: Path) -> None:
        root, _ = _feature_with_frozen_requirements(tmp_path)
        doc = read_frozen_requirements_doc(root)
        assert doc["feature"] == FEATURE_ID
        assert [r["id"] for r in doc["requirements"]] == ["REQ-001", "REQ-002"]

    def test_fails_loud_when_requirements_not_frozen(self, tmp_path: Path) -> None:
        # A fresh feature run: requirements promoted but NOT frozen.
        create_feature_run(tmp_path, "build the foo")
        root = tmp_path / ".ai-dev" / "features" / FEATURE_ID
        promote_requirements(
            root,
            FEATURE_ID,
            {
                "requirements": [{"key": "r1", "statement": "foo."}],
                "acceptance_criteria": [],
            },
            origin="test",
        )
        with pytest.raises(ValueError, match="not frozen"):
            read_frozen_requirements_doc(root)


class TestBuildCanonicalDesign:
    def test_allocates_des_ids_in_order(self, tmp_path: Path) -> None:
        root, req_ids = _feature_with_frozen_requirements(tmp_path)
        doc, allocated = build_canonical_design(
            root, FEATURE_ID, _design_proposal(req_ids), origin=None
        )
        assert allocated == {"DES": ["DES-001", "DES-002"]}
        assert [el["id"] for el in doc["design_elements"]] == ["DES-001", "DES-002"]

    def test_stitches_mapping_requirement_to_frozen_req_ids(
        self, tmp_path: Path
    ) -> None:
        # The first live use of RefResolver.add_upstream: mapping `requirement`
        # refs (canonical REQ-NNN) resolve against the frozen upstream set.
        root, req_ids = _feature_with_frozen_requirements(tmp_path)
        doc, _ = build_canonical_design(
            root, FEATURE_ID, _design_proposal(req_ids), origin=None
        )
        req_of = {m["key"]: m["requirement"] for m in doc["requirement_mapping"]}
        assert req_of["m1"] == "REQ-001"
        assert req_of["m2"] == "REQ-002"
        # No local key leaks into the canonical ref - it is the frozen REQ id.
        assert all(m["requirement"].startswith("REQ-") for m in doc["requirement_mapping"])

    def test_stitches_mapping_design_elements_to_local_des_ids(
        self, tmp_path: Path
    ) -> None:
        root, req_ids = _feature_with_frozen_requirements(tmp_path)
        doc, _ = build_canonical_design(
            root, FEATURE_ID, _design_proposal(req_ids), origin=None
        )
        des_of = {m["key"]: m["design_elements"] for m in doc["requirement_mapping"]}
        # m1 -> [d1] -> [DES-001]; m2 -> [d1, d2] -> [DES-001, DES-002].
        assert des_of["m1"] == ["DES-001"]
        assert des_of["m2"] == ["DES-001", "DES-002"]

    def test_carries_optional_facets_and_local_keys_as_provenance(
        self, tmp_path: Path
    ) -> None:
        root, req_ids = _feature_with_frozen_requirements(tmp_path)
        doc, _ = build_canonical_design(
            root, FEATURE_ID, _design_proposal(req_ids), origin=None
        )
        assert doc["feature"] == FEATURE_ID
        assert doc["frozen"] is False
        assert doc["architecture_decision"] == "Single-module CLI"
        assert doc["data_model"] == {"Greeting": {"name": "str"}}
        assert doc["invariants"] == ["greeting is deterministic"]
        assert doc["risks"] == ["locale differences"]
        assert doc["dependencies"] == ["stdlib only"]
        # The model's local keys are retained as provenance.
        assert doc["design_elements"][0]["key"] == "d1"
        assert doc["requirement_mapping"][0]["key"] == "m1"

    def test_unresolvable_req_ref_fails_loud(self, tmp_path: Path) -> None:
        # A mapping ref to a REQ that was never frozen (REQ-099) is a malformed
        # proposal -> reference-integrity (D3) fails loud, nothing written.
        root, _ = _feature_with_frozen_requirements(tmp_path)
        proposal = _design_proposal(["REQ-001", "REQ-002"])
        proposal["requirement_mapping"][1]["requirement"] = "REQ-099"
        with pytest.raises(UnresolvedRefError, match="REQ-099"):
            build_canonical_design(root, FEATURE_ID, proposal, origin=None)

    def test_unresolvable_des_local_ref_fails_loud(self, tmp_path: Path) -> None:
        root, req_ids = _feature_with_frozen_requirements(tmp_path)
        proposal = _design_proposal(req_ids)
        proposal["requirement_mapping"][0]["design_elements"] = ["d9"]  # no such DES
        with pytest.raises(UnresolvedRefError, match="d9"):
            build_canonical_design(root, FEATURE_ID, proposal, origin=None)

    def test_fails_loud_when_requirements_not_frozen(self, tmp_path: Path) -> None:
        create_feature_run(tmp_path, "build the foo")
        root = tmp_path / ".ai-dev" / "features" / FEATURE_ID
        with pytest.raises(ValueError, match="not frozen"):
            build_canonical_design(
                root, FEATURE_ID, _design_proposal(["REQ-001", "REQ-002"]), origin=None
            )

    def test_omits_optional_facets_when_absent(self, tmp_path: Path) -> None:
        root, req_ids = _feature_with_frozen_requirements(tmp_path)
        proposal = {
            "design_elements": [{"key": "d1", "name": "only"}],
            "requirement_mapping": [
                {"key": "m1", "requirement": req_ids[0], "design_elements": ["d1"]}
            ],
        }
        doc, allocated = build_canonical_design(
            root, FEATURE_ID, proposal, origin=None
        )
        assert allocated == {"DES": ["DES-001"]}
        for facet in (
            "architecture_decision",
            "data_model",
            "api_cli_contract",
            "file_layout",
            "invariants",
            "risks",
            "dependencies",
        ):
            assert facet not in doc


class TestRenderDesignMd:
    def test_renders_des_ids_and_stitched_mapping(self, tmp_path: Path) -> None:
        root, req_ids = _feature_with_frozen_requirements(tmp_path)
        doc, _ = build_canonical_design(
            root, FEATURE_ID, _design_proposal(req_ids), origin=None
        )
        md = render_design_md(FEATURE_ID, doc)
        # DES ids appear.
        assert "DES-001" in md and "DES-002" in md
        # The stitched mapping: REQ id + DES ids (not local keys).
        assert "REQ-001" in md and "REQ-002" in md
        assert "REQ **REQ-001** <- [DES-001]" in md
        assert "REQ **REQ-002** <- [DES-001, DES-002]" in md
        # Design-element names render as headings; frozen state mirrored.
        assert "Greeting module" in md
        assert "Frozen: false" in md

    def test_renders_empty_artifact_without_ids(self) -> None:
        doc = {
            "feature": FEATURE_ID,
            "frozen": False,
            "design_elements": [],
            "requirement_mapping": [],
        }
        md = render_design_md(FEATURE_ID, doc)
        assert "_None yet._" in md
        assert "### DES-" not in md

    def test_render_is_deterministic(self, tmp_path: Path) -> None:
        root, req_ids = _feature_with_frozen_requirements(tmp_path)
        doc, _ = build_canonical_design(
            root, FEATURE_ID, _design_proposal(req_ids), origin=None
        )
        assert render_design_md(FEATURE_ID, doc) == render_design_md(FEATURE_ID, doc)


class TestPromoteDesign:
    def test_writes_canonical_json_and_md_mirror(self, tmp_path: Path) -> None:
        root, req_ids = _feature_with_frozen_requirements(tmp_path)
        result = promote_design(root, FEATURE_ID, _design_proposal(req_ids), origin="cli")

        assert isinstance(result, PromoteResult)
        assert result.stage == "design"
        assert result.artifact == "design"
        assert result.json_path == root / DESIGN_JSON
        assert result.md_path == root / DESIGN_MD
        assert result.json_path.is_file()
        assert result.md_path.is_file()

        doc = json.loads(result.json_path.read_text())
        assert doc["frozen"] is False
        assert [el["id"] for el in doc["design_elements"]] == ["DES-001", "DES-002"]

    def test_allocates_from_counter_and_persists_it(self, tmp_path: Path) -> None:
        root, req_ids = _feature_with_frozen_requirements(tmp_path)
        promote_design(root, FEATURE_ID, _design_proposal(req_ids), origin="cli")
        counters = yaml.safe_load((root / "id-counters.yml").read_text())
        assert counters["DES"] == 2

    def test_appends_promote_audit_event(self, tmp_path: Path) -> None:
        root, req_ids = _feature_with_frozen_requirements(tmp_path)
        promote_design(root, FEATURE_ID, _design_proposal(req_ids), origin="cli")
        records = json.loads((root / "audit.log.json").read_text())
        promote_records = [r for r in records if r["event"] == "promote" and r["payload"]["stage"] == "design"]
        assert len(promote_records) == 1
        payload = promote_records[0]["payload"]
        assert payload["artifact"] == "design"
        assert payload["allocated"] == {"DES": ["DES-001", "DES-002"]}
        assert promote_records[0]["origin"] == "cli"

    def test_unresolvable_ref_writes_nothing(self, tmp_path: Path) -> None:
        root, _ = _feature_with_frozen_requirements(tmp_path)
        seed = (root / DESIGN_JSON).read_text()
        proposal = _design_proposal(["REQ-001", "REQ-002"])
        proposal["requirement_mapping"][1]["requirement"] = "REQ-099"
        with pytest.raises(UnresolvedRefError, match="REQ-099"):
            promote_design(root, FEATURE_ID, proposal, origin="cli")
        # The canonical artifact + audit log are unchanged - fail loud, no partial promote.
        assert (root / DESIGN_JSON).read_text() == seed
        records = json.loads((root / "audit.log.json").read_text())
        assert not any(
            r["event"] == "promote" and r["payload"].get("stage") == "design"
            for r in records
        )

    def test_refuses_to_overwrite_a_frozen_design(self, tmp_path: Path) -> None:
        root, req_ids = _feature_with_frozen_requirements(tmp_path)
        promote_design(root, FEATURE_ID, _design_proposal(req_ids), origin="cli")
        freeze_artifact(root, "design", origin="cli")
        with pytest.raises(FrozenArtifactWriteError):
            promote_design(root, FEATURE_ID, _design_proposal(req_ids), origin="cli")

    def test_re_promote_overwrites_and_reallocates_ids(self, tmp_path: Path) -> None:
        root, req_ids = _feature_with_frozen_requirements(tmp_path)
        first = promote_design(root, FEATURE_ID, _design_proposal(req_ids), origin="cli")
        assert list(first.allocated["DES"]) == ["DES-001", "DES-002"]
        second = promote_design(root, FEATURE_ID, _design_proposal(req_ids), origin="cli")
        # New ids from the bumped counter; canonical file overwritten.
        assert list(second.allocated["DES"]) == ["DES-003", "DES-004"]
        doc = json.loads((root / DESIGN_JSON).read_text())
        assert [el["id"] for el in doc["design_elements"]] == ["DES-003", "DES-004"]

    def test_rejects_empty_feature_id(self, tmp_path: Path) -> None:
        root, req_ids = _feature_with_frozen_requirements(tmp_path)
        with pytest.raises(ValueError, match="feature_id"):
            promote_design(root, "", _design_proposal(req_ids), origin="cli")

    def test_refuses_if_requirements_not_frozen(self, tmp_path: Path) -> None:
        create_feature_run(tmp_path, "build the foo")
        root = tmp_path / ".ai-dev" / "features" / FEATURE_ID
        with pytest.raises(ValueError, match="not frozen"):
            promote_design(
                root, FEATURE_ID, _design_proposal(["REQ-001", "REQ-002"]), origin="cli"
            )


class TestPromoteDesignMalformedProposal:
    """§24.2: a malformed design proposal fails loud rather than being silently coerced."""

    def test_design_elements_not_a_list(self, tmp_path: Path) -> None:
        root, _ = _feature_with_frozen_requirements(tmp_path)
        with pytest.raises(ValueError, match="'design_elements' must be a list"):
            promote_design(
                root,
                FEATURE_ID,
                {"design_elements": {}, "requirement_mapping": []},
                origin="cli",
            )

    def test_design_element_missing_key(self, tmp_path: Path) -> None:
        root, _ = _feature_with_frozen_requirements(tmp_path)
        proposal = {
            "design_elements": [{"name": "no key"}],
            "requirement_mapping": [],
        }
        with pytest.raises(ValueError, match="non-empty string 'key'"):
            promote_design(root, FEATURE_ID, proposal, origin="cli")

    def test_design_element_missing_name(self, tmp_path: Path) -> None:
        root, _ = _feature_with_frozen_requirements(tmp_path)
        proposal = {
            "design_elements": [{"key": "d1"}],
            "requirement_mapping": [],
        }
        with pytest.raises(ValueError, match="non-empty string 'name'"):
            promote_design(root, FEATURE_ID, proposal, origin="cli")

    def test_mapping_missing_requirement_ref(self, tmp_path: Path) -> None:
        root, _ = _feature_with_frozen_requirements(tmp_path)
        proposal = {
            "design_elements": [{"key": "d1", "name": "n"}],
            "requirement_mapping": [{"key": "m1", "design_elements": ["d1"]}],
        }
        with pytest.raises(ValueError, match="non-empty string 'requirement'"):
            promote_design(root, FEATURE_ID, proposal, origin="cli")


# ---------------------------------------------------------------------------
# Tasks stage (ticket 04): promote_tasks stitches each task's REQ+DES refs
# against the FROZEN requirements AND design upstreams (first stage with two
# frozen upstreams) + allocates TASK ids + renders 03-tasks.{json,md} + seeds
# status/task-status.yml + populates the single lane in 04-lane-graph.yml.
# ---------------------------------------------------------------------------


def _feature_with_frozen_requirements_and_design(
    tmp_path: Path, req_proposal: dict | None = None
) -> tuple[Path, list[str], list[str]]:
    """Create a feature run, promote+freeze requirements AND design, return ids.

    Tasks may only stitch against *frozen* requirements AND design (ADR-0008 D2),
    so every tasks test needs a feature whose requirements and design are both
    promoted then frozen. Returns ``(root, req_ids, des_ids)`` - the two upstream
    id sets a tasks proposal's ``related_requirements`` / ``related_design`` refs
    resolve against.
    """
    root, req_ids = _feature_with_frozen_requirements(tmp_path, req_proposal)
    promote_design(root, FEATURE_ID, _design_proposal(req_ids), origin="test")
    freeze_artifact(root, "design", origin="test")
    des_doc = json.loads((root / DESIGN_JSON).read_text())
    des_ids = [el["id"] for el in des_doc["design_elements"]]
    return root, req_ids, des_ids


def _tasks_proposal(req_ids: list[str], des_ids: list[str]) -> dict:
    """A synthetic id-free tasks proposal referencing frozen REQ+DES ids.

    Two tasks across the single MVP lane: t1 realizes REQ-001 via DES-001, t2
    realizes REQ-002 via DES-001+DES-002. Each declares its expected/exclusive
    files (the lane file boundary the Implementer later enforces).
    """
    return {
        "lane_purpose": "Implement the greet CLI end to end.",
        "tasks": [
            {
                "key": "t1",
                "summary": "Implement greeting formatter",
                "related_requirements": [req_ids[0]],
                "related_design": [des_ids[0]],
                "expected_files": ["src/greet.py"],
                "exclusive_files": ["src/greet.py"],
                "description": "Formats the greeting string.",
            },
            {
                "key": "t2",
                "summary": "Wire greet CLI entrypoint",
                "related_requirements": [req_ids[1]],
                "related_design": [des_ids[1], des_ids[0]],
                "expected_files": ["src/cli.py"],
                "exclusive_files": ["src/cli.py"],
            },
        ],
    }


class TestReadFrozenDesignDoc:
    def test_returns_doc_when_design_frozen(self, tmp_path: Path) -> None:
        root, _, _ = _feature_with_frozen_requirements_and_design(tmp_path)
        doc = read_frozen_design_doc(root)
        assert doc["feature"] == FEATURE_ID
        assert [el["id"] for el in doc["design_elements"]] == ["DES-001", "DES-002"]

    def test_fails_loud_when_design_not_frozen(self, tmp_path: Path) -> None:
        # Requirements frozen but design only promoted (not frozen).
        root, _ = _feature_with_frozen_requirements(tmp_path)
        promote_design(
            root, FEATURE_ID, _design_proposal(["REQ-001", "REQ-002"]), origin="test"
        )
        with pytest.raises(ValueError, match="not frozen"):
            read_frozen_design_doc(root)


class TestBuildCanonicalTasks:
    def test_returns_four_tuple_doc_allocated_status_rows_verify_commands(
        self, tmp_path: Path
    ) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        result = build_canonical_tasks(
            root, FEATURE_ID, _tasks_proposal(req_ids, des_ids), origin=None
        )
        # The tasks build core returns (doc, allocated, task_status_rows,
        # verification_commands) - the runtime rows it seeds + the lane verify
        # command set it carries out of band to the lane-graph writer (ticket 05).
        assert isinstance(result, tuple) and len(result) == 4
        doc, allocated, rows, verify_commands = result
        assert isinstance(doc, dict)
        assert isinstance(allocated, dict)
        assert isinstance(rows, dict)
        assert isinstance(verify_commands, list)

    def test_allocates_task_ids_in_order(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        doc, allocated, _, _ = build_canonical_tasks(
            root, FEATURE_ID, _tasks_proposal(req_ids, des_ids), origin=None
        )
        assert allocated == {"TASK": ["TASK-001", "TASK-002"]}
        assert [t["id"] for t in doc["tasks"]] == ["TASK-001", "TASK-002"]

    def test_stitches_related_requirements_to_frozen_req_ids(
        self, tmp_path: Path
    ) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        doc, _, _, _ = build_canonical_tasks(
            root, FEATURE_ID, _tasks_proposal(req_ids, des_ids), origin=None
        )
        reqs_of = {t["key"]: t["related_requirements"] for t in doc["tasks"]}
        assert reqs_of["t1"] == ["REQ-001"]
        assert reqs_of["t2"] == ["REQ-002"]
        # No local key leaks - the stitched canonical frozen REQ id.
        for t in doc["tasks"]:
            assert all(r.startswith("REQ-") for r in t["related_requirements"])

    def test_stitches_related_design_to_frozen_des_ids(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        doc, _, _, _ = build_canonical_tasks(
            root, FEATURE_ID, _tasks_proposal(req_ids, des_ids), origin=None
        )
        des_of = {t["key"]: t["related_design"] for t in doc["tasks"]}
        # t2 -> [d2, d1] -> [DES-002, DES-001] (order preserved, no dedupe needed).
        assert des_of["t1"] == ["DES-001"]
        assert des_of["t2"] == ["DES-002", "DES-001"]
        for t in doc["tasks"]:
            assert all(d.startswith("DES-") for d in t["related_design"])

    def test_assigns_single_seeded_lane_to_each_task(self, tmp_path: Path) -> None:
        # The lane is structural (allocated at feature-run creation, seeded into
        # 04-lane-graph.yml); promote assigns every task to that one lane (§5.3).
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        doc, _, _, _ = build_canonical_tasks(
            root, FEATURE_ID, _tasks_proposal(req_ids, des_ids), origin=None
        )
        assert all(t["lane"] == "LANE-001" for t in doc["tasks"])
        assert doc["lane_purpose"] == "Implement the greet CLI end to end."

    def test_carries_optional_facets_and_local_key_as_provenance(
        self, tmp_path: Path
    ) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        doc, _, _, _ = build_canonical_tasks(
            root, FEATURE_ID, _tasks_proposal(req_ids, des_ids), origin=None
        )
        assert doc["feature"] == FEATURE_ID
        assert doc["frozen"] is False
        # The model's local key is retained as provenance on each task.
        assert doc["tasks"][0]["key"] == "t1"
        # Optional prose carried through when present...
        assert doc["tasks"][0]["description"] == "Formats the greeting string."
        # ...and omitted when absent (t2 has no description).
        assert "description" not in doc["tasks"][1]
        # expected/exclusive files carried as declared (not resolved - file paths).
        assert doc["tasks"][0]["expected_files"] == ["src/greet.py"]
        assert doc["tasks"][0]["exclusive_files"] == ["src/greet.py"]

    def test_derives_related_acceptance_criteria_via_req_ac_chain(
        self, tmp_path: Path
    ) -> None:
        # TASK -> REQ -> AC: a task's related_acceptance_criteria are the ACs
        # whose `requirement` traces to one of the task's related_requirements.
        # Richer reqs: r1 has a1+a2, r2 has a3.
        req_proposal = {
            "requirements": [
                {"key": "r1", "statement": "shall foo."},
                {"key": "r2", "statement": "shall bar."},
            ],
            "acceptance_criteria": [
                {"key": "a1", "requirement": "r1", "criterion": "foo1"},
                {"key": "a2", "requirement": "r1", "criterion": "foo2"},
                {"key": "a3", "requirement": "r2", "criterion": "bar1"},
            ],
        }
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(
            tmp_path, req_proposal
        )
        doc, _, rows, _ = build_canonical_tasks(
            root, FEATURE_ID, _tasks_proposal(req_ids, des_ids), origin=None
        )
        # t1 -> REQ-001 -> [AC-001, AC-002]; t2 -> REQ-002 -> [AC-003].
        acs_of = {t["key"]: t.get("related_acceptance_criteria") for t in doc["tasks"]}
        # related_acceptance_criteria is NOT stored on the task doc (it lives in
        # the runtime task-status rows); the doc task carries REQ/DES refs only.
        assert "related_acceptance_criteria" not in doc["tasks"][0]
        # The derived ACs land on the runtime status rows promote seeds.
        assert rows["TASK-001"]["related_acceptance_criteria"] == ["AC-001", "AC-002"]
        assert rows["TASK-002"]["related_acceptance_criteria"] == ["AC-003"]

    def test_status_rows_all_pending_with_refs(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        _, _, rows, _ = build_canonical_tasks(
            root, FEATURE_ID, _tasks_proposal(req_ids, des_ids), origin=None
        )
        # Every task seeds a pending runtime row (§8.1) carrying its stitched
        # REQ refs + derived AC refs + the assigned lane.
        assert set(rows) == {"TASK-001", "TASK-002"}
        for row in rows.values():
            assert row["status"] == "pending"
            assert row["lane"] == "LANE-001"
            assert row["owner_run"] is None
            assert row["proposed_done_by"] is None
            assert row["accepted_done"] is False
        assert rows["TASK-001"]["related_requirements"] == ["REQ-001"]
        assert rows["TASK-002"]["related_requirements"] == ["REQ-002"]

    def test_unresolvable_req_ref_fails_loud(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        proposal = _tasks_proposal(req_ids, des_ids)
        proposal["tasks"][0]["related_requirements"] = ["REQ-099"]  # no such REQ
        with pytest.raises(UnresolvedRefError, match="REQ-099"):
            build_canonical_tasks(root, FEATURE_ID, proposal, origin=None)

    def test_unresolvable_des_ref_fails_loud(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        proposal = _tasks_proposal(req_ids, des_ids)
        proposal["tasks"][0]["related_design"] = ["DES-099"]  # no such DES
        with pytest.raises(UnresolvedRefError, match="DES-099"):
            build_canonical_tasks(root, FEATURE_ID, proposal, origin=None)

    def test_fails_loud_when_design_not_frozen(self, tmp_path: Path) -> None:
        # Requirements frozen, design promoted but NOT frozen.
        root, req_ids = _feature_with_frozen_requirements(tmp_path)
        promote_design(
            root, FEATURE_ID, _design_proposal(req_ids), origin="test"
        )
        with pytest.raises(ValueError, match="not frozen"):
            build_canonical_tasks(
                root, FEATURE_ID, _tasks_proposal(req_ids, ["DES-001", "DES-002"]),
                origin=None,
            )

    def test_fails_loud_when_requirements_not_frozen(self, tmp_path: Path) -> None:
        create_feature_run(tmp_path, "build the foo")
        root = tmp_path / ".ai-dev" / "features" / FEATURE_ID
        with pytest.raises(ValueError, match="not frozen"):
            build_canonical_tasks(
                root,
                FEATURE_ID,
                _tasks_proposal(["REQ-001", "REQ-002"], ["DES-001", "DES-002"]),
                origin=None,
            )


class TestRenderTasksMd:
    def test_renders_task_ids_and_stitched_refs(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        doc, _, _, _ = build_canonical_tasks(
            root, FEATURE_ID, _tasks_proposal(req_ids, des_ids), origin=None
        )
        md = render_tasks_md(FEATURE_ID, doc)
        # TASK ids appear.
        assert "TASK-001" in md and "TASK-002" in md
        # Stitched canonical REQ/DES refs (not local keys) render.
        assert "related_requirements: REQ-001" in md
        assert "related_requirements: REQ-002" in md
        assert "related_design: DES-001" in md
        assert "related_design: DES-002, DES-001" in md
        # The assigned lane + summaries render; frozen state mirrored.
        assert "lane: LANE-001" in md
        assert "Implement greeting formatter" in md
        assert "Frozen: false" in md

    def test_lane_purpose_section_precedes_tasks_section(self, tmp_path: Path) -> None:
        # The Implementer reads everything after `## Tasks` verbatim, so that
        # section must be LAST; the lane purpose renders in its own section above.
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        doc, _, _, _ = build_canonical_tasks(
            root, FEATURE_ID, _tasks_proposal(req_ids, des_ids), origin=None
        )
        md = render_tasks_md(FEATURE_ID, doc)
        assert md.index("## Lane purpose") < md.index("## Tasks (TASK-NNN)")
        # Nothing follows the tasks section - `## Tasks` is the last level-2
        # header (the `### TASK-NNN` task headings nest under it, not after it).
        h2 = [ln for ln in md.splitlines() if ln.startswith("## ")]
        assert h2[-1] == "## Tasks (TASK-NNN)"

    def test_renders_empty_artifact_without_ids(self) -> None:
        doc = {
            "feature": FEATURE_ID,
            "frozen": False,
            "lane_purpose": None,
            "tasks": [],
        }
        md = render_tasks_md(FEATURE_ID, doc)
        assert "_None yet._" in md
        assert "### TASK-" not in md

    def test_render_is_deterministic(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        doc, _, _, _ = build_canonical_tasks(
            root, FEATURE_ID, _tasks_proposal(req_ids, des_ids), origin=None
        )
        assert render_tasks_md(FEATURE_ID, doc) == render_tasks_md(FEATURE_ID, doc)


class TestPromoteTasks:
    def test_writes_canonical_json_and_md_mirror(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        result = promote_tasks(
            root, FEATURE_ID, _tasks_proposal(req_ids, des_ids), origin="cli"
        )

        assert isinstance(result, PromoteResult)
        assert result.stage == "tasks"
        assert result.artifact == "tasks"
        assert result.json_path == root / TASKS_JSON
        assert result.md_path == root / TASKS_MD
        assert result.json_path.is_file()
        assert result.md_path.is_file()

        doc = json.loads(result.json_path.read_text())
        assert doc["frozen"] is False
        assert [t["id"] for t in doc["tasks"]] == ["TASK-001", "TASK-002"]
        assert doc["lane_purpose"] == "Implement the greet CLI end to end."

    def test_seeds_task_status_yml_all_pending(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        promote_tasks(root, FEATURE_ID, _tasks_proposal(req_ids, des_ids), origin="cli")
        status = yaml.safe_load((root / "status" / "task-status.yml").read_text())
        assert set(status["tasks"]) == {"TASK-001", "TASK-002"}
        for row in status["tasks"].values():
            assert row["status"] == "pending"
            assert row["accepted_done"] is False
        # Derived ACs (default frozen reqs: AC-001 traces to REQ-001).
        assert status["tasks"]["TASK-001"]["related_acceptance_criteria"] == ["AC-001"]
        assert status["tasks"]["TASK-002"]["related_acceptance_criteria"] == []

    def test_populates_single_lane_in_lane_graph(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        promote_tasks(root, FEATURE_ID, _tasks_proposal(req_ids, des_ids), origin="cli")
        graph = yaml.safe_load((root / LANE_GRAPH_YML).read_text())
        lane = graph["lanes"][0]
        # purpose + tasks (in allocation order) + union of files (sorted).
        assert lane["purpose"] == "Implement the greet CLI end to end."
        assert lane["tasks"] == ["TASK-001", "TASK-002"]
        assert lane["expected_files"] == ["src/cli.py", "src/greet.py"]
        assert lane["exclusive_files"] == ["src/cli.py", "src/greet.py"]
        # The lane's structural fields are preserved (id never re-allocated).
        assert lane["id"] == "LANE-001"
        assert "merge_policy" in lane
        assert lane["depends_on"] == []

    def test_allocates_from_counter_and_persists_it(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        promote_tasks(root, FEATURE_ID, _tasks_proposal(req_ids, des_ids), origin="cli")
        counters = yaml.safe_load((root / "id-counters.yml").read_text())
        assert counters["TASK"] == 2

    def test_appends_promote_audit_event(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        promote_tasks(root, FEATURE_ID, _tasks_proposal(req_ids, des_ids), origin="cli")
        records = json.loads((root / "audit.log.json").read_text())
        promote_records = [
            r
            for r in records
            if r["event"] == "promote" and r["payload"]["stage"] == "tasks"
        ]
        assert len(promote_records) == 1
        payload = promote_records[0]["payload"]
        assert payload["artifact"] == "tasks"
        assert payload["allocated"] == {"TASK": ["TASK-001", "TASK-002"]}
        assert promote_records[0]["origin"] == "cli"

    def test_unresolvable_ref_writes_nothing(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        seed_json = (root / TASKS_JSON).read_text()
        seed_status = (root / "status" / "task-status.yml").read_text()
        seed_graph = (root / LANE_GRAPH_YML).read_text()
        proposal = _tasks_proposal(req_ids, des_ids)
        proposal["tasks"][0]["related_requirements"] = ["REQ-099"]
        with pytest.raises(UnresolvedRefError, match="REQ-099"):
            promote_tasks(root, FEATURE_ID, proposal, origin="cli")
        # All four write targets are unchanged - fail loud, no partial promote.
        assert (root / TASKS_JSON).read_text() == seed_json
        assert (root / "status" / "task-status.yml").read_text() == seed_status
        assert (root / LANE_GRAPH_YML).read_text() == seed_graph
        records = json.loads((root / "audit.log.json").read_text())
        assert not any(
            r["event"] == "promote" and r["payload"].get("stage") == "tasks"
            for r in records
        )

    def test_refuses_to_overwrite_a_frozen_tasks(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        promote_tasks(root, FEATURE_ID, _tasks_proposal(req_ids, des_ids), origin="cli")
        freeze_artifact(root, "tasks", origin="cli")
        with pytest.raises(FrozenArtifactWriteError):
            promote_tasks(root, FEATURE_ID, _tasks_proposal(req_ids, des_ids), origin="cli")

    def test_refuses_to_overwrite_a_frozen_lane_graph(self, tmp_path: Path) -> None:
        # tasks and lane_graph freeze TOGETHER at the task gate (§18.3); a frozen
        # lane_graph must also block tasks promote (both written in one step).
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        promote_tasks(root, FEATURE_ID, _tasks_proposal(req_ids, des_ids), origin="cli")
        freeze_artifact(root, "lane_graph", origin="cli")
        with pytest.raises(FrozenArtifactWriteError):
            promote_tasks(root, FEATURE_ID, _tasks_proposal(req_ids, des_ids), origin="cli")

    def test_re_promote_overwrites_and_reallocates_ids(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        first = promote_tasks(root, FEATURE_ID, _tasks_proposal(req_ids, des_ids), origin="cli")
        assert list(first.allocated["TASK"]) == ["TASK-001", "TASK-002"]
        second = promote_tasks(root, FEATURE_ID, _tasks_proposal(req_ids, des_ids), origin="cli")
        # New ids from the bumped counter; canonical file overwritten.
        assert list(second.allocated["TASK"]) == ["TASK-003", "TASK-004"]
        doc = json.loads((root / TASKS_JSON).read_text())
        assert [t["id"] for t in doc["tasks"]] == ["TASK-003", "TASK-004"]
        # The lane graph's tasks list reflects the re-allocated ids.
        graph = yaml.safe_load((root / LANE_GRAPH_YML).read_text())
        assert graph["lanes"][0]["tasks"] == ["TASK-003", "TASK-004"]

    def test_rejects_empty_feature_id(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        with pytest.raises(ValueError, match="feature_id"):
            promote_tasks(root, "", _tasks_proposal(req_ids, des_ids), origin="cli")

    def test_refuses_if_design_not_frozen(self, tmp_path: Path) -> None:
        root, req_ids = _feature_with_frozen_requirements(tmp_path)
        promote_design(root, FEATURE_ID, _design_proposal(req_ids), origin="test")
        with pytest.raises(ValueError, match="not frozen"):
            promote_tasks(
                root,
                FEATURE_ID,
                _tasks_proposal(req_ids, ["DES-001", "DES-002"]),
                origin="cli",
            )

    def test_refuses_if_requirements_not_frozen(self, tmp_path: Path) -> None:
        create_feature_run(tmp_path, "build the foo")
        root = tmp_path / ".ai-dev" / "features" / FEATURE_ID
        with pytest.raises(ValueError, match="not frozen"):
            promote_tasks(
                root,
                FEATURE_ID,
                _tasks_proposal(["REQ-001", "REQ-002"], ["DES-001", "DES-002"]),
                origin="cli",
            )


class TestPromoteTasksMalformedProposal:
    """§24.2: a malformed tasks proposal fails loud rather than being silently coerced."""

    def test_tasks_not_a_list(self, tmp_path: Path) -> None:
        root, _, _ = _feature_with_frozen_requirements_and_design(tmp_path)
        with pytest.raises(ValueError, match="'tasks' must be a list"):
            promote_tasks(
                root,
                FEATURE_ID,
                {"lane_purpose": "p", "tasks": {}},
                origin="cli",
            )

    def test_missing_lane_purpose(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        proposal = _tasks_proposal(req_ids, des_ids)
        del proposal["lane_purpose"]
        with pytest.raises(ValueError, match="non-empty string 'lane_purpose'"):
            promote_tasks(root, FEATURE_ID, proposal, origin="cli")

    def test_task_missing_key(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        proposal = _tasks_proposal(req_ids, des_ids)
        del proposal["tasks"][0]["key"]
        with pytest.raises(ValueError, match="non-empty string 'key'"):
            promote_tasks(root, FEATURE_ID, proposal, origin="cli")

    def test_task_missing_summary(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        proposal = _tasks_proposal(req_ids, des_ids)
        del proposal["tasks"][0]["summary"]
        with pytest.raises(ValueError, match="non-empty string 'summary'"):
            promote_tasks(root, FEATURE_ID, proposal, origin="cli")

    def test_task_missing_related_requirements(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        proposal = _tasks_proposal(req_ids, des_ids)
        del proposal["tasks"][0]["related_requirements"]
        with pytest.raises(UnresolvedRefError, match="expected a list"):
            promote_tasks(root, FEATURE_ID, proposal, origin="cli")

    def test_task_missing_related_design(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        proposal = _tasks_proposal(req_ids, des_ids)
        del proposal["tasks"][0]["related_design"]
        with pytest.raises(UnresolvedRefError, match="expected a list"):
            promote_tasks(root, FEATURE_ID, proposal, origin="cli")

    def test_task_missing_expected_files(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        proposal = _tasks_proposal(req_ids, des_ids)
        del proposal["tasks"][0]["expected_files"]
        with pytest.raises(ValueError, match="must be a list"):
            promote_tasks(root, FEATURE_ID, proposal, origin="cli")

    def test_task_expected_files_not_all_strings(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        proposal = _tasks_proposal(req_ids, des_ids)
        proposal["tasks"][0]["expected_files"] = ["src/greet.py", 42]
        with pytest.raises(ValueError, match="non-empty string"):
            promote_tasks(root, FEATURE_ID, proposal, origin="cli")


def _tasks_proposal_with_verify(req_ids: list[str], des_ids: list[str]) -> dict:
    """A tasks proposal that also carries the lane verify command set (ticket 05).

    The zero-hand-authored-planning contract: the Planner emits the verify
    command set (pytest + mypy, workspace-relative) so promote can write it onto
    the lane and the Verifier runs model-generated commands.
    """
    proposal = _tasks_proposal(req_ids, des_ids)
    proposal["verification_commands"] = [
        {
            "name": "pytest",
            "command": "PYTHONPATH=. python -m pytest -q -p no:cacheprovider -c /dev/null tests",
        },
        {"name": "mypy", "command": "python -m mypy greet"},
    ]
    return proposal


class TestPromoteTasksVerificationCommands:
    """v0.6 capstone (ticket 05): the Planner-generated lane verify command set.

    promote writes the proposal's optional ``verification_commands`` onto the
    single lane in ``04-lane-graph.yml`` so the shell Verifier (§9.5) runs
    model-generated commands - the zero-hand-authored-planning bar (the v0.4
    dogfood hand-authored them). The verify commands are a lane-level concern, so
    they must NOT leak into ``03-tasks.json`` (task content).
    """

    def test_writes_verify_commands_onto_lane(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        promote_tasks(
            root,
            FEATURE_ID,
            _tasks_proposal_with_verify(req_ids, des_ids),
            origin="cli",
        )
        lane = yaml.safe_load((root / LANE_GRAPH_YML).read_text())["lanes"][0]
        assert lane["verification_commands"] == [
            {
                "name": "pytest",
                "command": "PYTHONPATH=. python -m pytest -q -p no:cacheprovider -c /dev/null tests",
            },
            {"name": "mypy", "command": "python -m mypy greet"},
        ]
        # verification_scope is derived from the command names so the two stay in sync.
        assert lane["verification_scope"] == ["pytest", "mypy"]

    def test_verify_commands_do_not_leak_into_tasks_json(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        promote_tasks(
            root,
            FEATURE_ID,
            _tasks_proposal_with_verify(req_ids, des_ids),
            origin="cli",
        )
        doc = json.loads((root / TASKS_JSON).read_text())
        # Verify commands are a lane-level concern -> they travel out of band to
        # the lane-graph, never into the task-content doc.
        assert "verification_commands" not in doc

    def test_proposal_without_verify_commands_leaves_lane_empty(
        self, tmp_path: Path
    ) -> None:
        # Backward compatible: a refinement draft that omits the verify command
        # set still promotes; the seeded lane entry has no verification_commands.
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        promote_tasks(root, FEATURE_ID, _tasks_proposal(req_ids, des_ids), origin="cli")
        lane = yaml.safe_load((root / LANE_GRAPH_YML).read_text())["lanes"][0]
        assert not lane.get("verification_commands")

    def test_build_canonical_tasks_returns_verify_commands(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        doc, _, _, verify_commands = build_canonical_tasks(
            root,
            FEATURE_ID,
            _tasks_proposal_with_verify(req_ids, des_ids),
            origin=None,
        )
        assert [vc["name"] for vc in verify_commands] == ["pytest", "mypy"]
        # The doc (03-tasks.json content) does not carry the verify commands.
        assert "verification_commands" not in doc


class TestPromoteTasksMalformedVerifyCommands:
    """§24.2: a malformed verification_commands field fails loud."""

    def test_not_a_list(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        proposal = _tasks_proposal(req_ids, des_ids)
        proposal["verification_commands"] = {"name": "pytest"}
        with pytest.raises(ValueError, match="'verification_commands' must be a list"):
            promote_tasks(root, FEATURE_ID, proposal, origin="cli")

    def test_entry_not_an_object(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        proposal = _tasks_proposal(req_ids, des_ids)
        proposal["verification_commands"] = ["pytest"]
        with pytest.raises(ValueError, match="must be an object"):
            promote_tasks(root, FEATURE_ID, proposal, origin="cli")

    def test_entry_missing_name(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        proposal = _tasks_proposal(req_ids, des_ids)
        proposal["verification_commands"] = [{"command": "python -m pytest"}]
        with pytest.raises(ValueError, match="no non-empty 'name'"):
            promote_tasks(root, FEATURE_ID, proposal, origin="cli")

    def test_entry_missing_command(self, tmp_path: Path) -> None:
        root, req_ids, des_ids = _feature_with_frozen_requirements_and_design(tmp_path)
        proposal = _tasks_proposal(req_ids, des_ids)
        proposal["verification_commands"] = [{"name": "pytest"}]
        with pytest.raises(ValueError, match="no non-empty 'command'"):
            promote_tasks(root, FEATURE_ID, proposal, origin="cli")
