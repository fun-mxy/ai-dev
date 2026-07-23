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
    build_canonical_requirements,
    promote_requirements,
    render_requirements_md,
)
from ai_dev.status import freeze_artifact

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
