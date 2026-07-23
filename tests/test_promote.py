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
    promote_design,
    promote_requirements,
    read_frozen_requirements_doc,
    render_design_md,
    render_requirements_md,
)
from ai_dev.status import freeze_artifact
from ai_dev.templates import DESIGN_JSON, DESIGN_MD, REQUIREMENTS_JSON

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
