"""audit.append_audit_event — structured md+json double product (§4.4, ticket 02).

The audit log is the §2.1 traceability backbone: every canonical-state change,
gate, decision and run event flows through this one appender. Each call writes
both ``audit.log.md`` (human) and ``audit.log.json`` (machine) from the same
record, so the two products stay consistent and the log stays append-only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ai_dev.audit import AUDIT_LOG_JSON, AUDIT_LOG_MD, append_audit_event


def _md(audit_dir: Path) -> str:
    return (audit_dir / AUDIT_LOG_MD).read_text()


def _json_records(audit_dir: Path) -> list[dict]:
    return json.loads((audit_dir / AUDIT_LOG_JSON).read_text())


class TestAppendAuditEvent:
    def test_first_event_creates_both_products(self, tmp_path: Path) -> None:
        append_audit_event(
            tmp_path,
            event="create",
            payload={"feature": "FEATURE-001"},
            timestamp="2026-07-19T10:00:00Z",
        )

        # Human-readable product.
        md = _md(tmp_path)
        assert "create" in md
        assert "feature: FEATURE-001" in md
        assert "2026-07-19T10:00:00Z" in md

        # Machine-readable mirror, an exact record of the same event.
        assert _json_records(tmp_path) == [
            {
                "timestamp": "2026-07-19T10:00:00Z",
                "event": "create",
                "payload": {"feature": "FEATURE-001"},
            }
        ]

    def test_md_is_append_only(self, tmp_path: Path) -> None:
        # §2.1: a later event must never clobber an earlier one.
        append_audit_event(
            tmp_path, event="create", payload={"feature": "FEATURE-001"},
            timestamp="2026-07-19T10:00:00Z",
        )
        append_audit_event(
            tmp_path, event="create", payload={"feature": "FEATURE-002"},
            timestamp="2026-07-19T10:01:00Z",
        )

        md = _md(tmp_path)
        assert "feature: FEATURE-001" in md
        assert "feature: FEATURE-002" in md
        assert md.count("feature: FEATURE-001") == 1
        assert md.count("feature: FEATURE-002") == 1

    def test_json_is_append_only(self, tmp_path: Path) -> None:
        # Existing records must survive verbatim — never mutated, never deleted.
        append_audit_event(
            tmp_path, event="create", payload={"feature": "FEATURE-001"},
            timestamp="2026-07-19T10:00:00Z",
        )
        first_snapshot = _json_records(tmp_path)[0]

        append_audit_event(
            tmp_path, event="create", payload={"feature": "FEATURE-002"},
            timestamp="2026-07-19T10:01:00Z",
        )
        append_audit_event(
            tmp_path, event="gate", payload={"name": "requirements_gate"},
            timestamp="2026-07-19T10:02:00Z",
        )

        after = _json_records(tmp_path)
        assert len(after) == 3
        assert after[0] == first_snapshot

    def test_renders_all_payload_fields(self, tmp_path: Path) -> None:
        append_audit_event(
            tmp_path,
            event="create",
            payload={"feature": "FEATURE-001", "intent": "add a thing"},
            timestamp="2026-07-19T10:00:00Z",
        )
        md = _md(tmp_path)
        assert "feature: FEATURE-001" in md
        assert "intent: add a thing" in md
        assert _json_records(tmp_path)[0]["payload"]["intent"] == "add a thing"

    def test_timestamp_defaults_to_now_in_both_products(self, tmp_path: Path) -> None:
        append_audit_event(tmp_path, event="create", payload={"feature": "FEATURE-001"})
        stamp_re = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"

        assert re.search(stamp_re, _md(tmp_path))
        assert re.fullmatch(stamp_re, _json_records(tmp_path)[0]["timestamp"])

    def test_non_string_payload_values_keep_native_type_in_json(
        self, tmp_path: Path
    ) -> None:
        # §2.1 "结构化记录" / §4.4 machine-readable: structured (non-string)
        # payload values round-trip with their native type in JSON rather than
        # being stringified, while still rendering readably in the markdown.
        append_audit_event(
            tmp_path,
            event="run",
            payload={
                "run_id": "RUN-001",
                "exit_code": 0,
                "changed_files": ["src/a.py", "tests/test_a.py"],
            },
            timestamp="2026-07-19T10:00:00Z",
        )

        payload = _json_records(tmp_path)[0]["payload"]
        assert payload["exit_code"] == 0
        assert payload["changed_files"] == ["src/a.py", "tests/test_a.py"]

        md = _md(tmp_path)
        assert "exit_code: 0" in md
        assert "changed_files" in md

    def test_md_and_json_stay_consistent_across_mixed_event_types(
        self, tmp_path: Path
    ) -> None:
        # Ticket 02 acceptance: consecutive appends of different event types
        # keep md and json consistent and complete (§4.4 double product).
        events = [
            ("create", {"feature": "FEATURE-001"}),
            ("gate", {"name": "requirements_gate", "verdict": "pass"}),
            ("decision", {"id": "DEC-001", "status": "accepted"}),
            ("run", {"run_id": "RUN-001", "profile": "cc-glm52"}),
        ]
        for event, payload in events:
            append_audit_event(tmp_path, event=event, payload=payload)

        records = _json_records(tmp_path)
        assert len(records) == len(events)  # nothing dropped on either side

        md = _md(tmp_path)
        for event, payload in events:
            # Every event type and every payload fact appears in the markdown too.
            assert f"· {event}" in md
            for value in payload.values():
                assert value in md

        # And the JSON array mirrors the same events, in order, intact.
        for (event, payload), record in zip(events, records):
            assert record["event"] == event
            assert record["payload"] == payload

    def test_json_file_is_a_single_valid_json_document(self, tmp_path: Path) -> None:
        append_audit_event(
            tmp_path, event="create", payload={"feature": "FEATURE-001"},
            timestamp="2026-07-19T10:00:00Z",
        )
        # A consumer must be able to json.load the whole file — it is a JSON
        # array document, not newline-delimited JSON.
        loaded = json.loads((tmp_path / AUDIT_LOG_JSON).read_text())
        assert isinstance(loaded, list)

    def test_origin_is_a_top_level_field_in_both_products(self, tmp_path: Path) -> None:
        # v0.4 ticket 02: ``origin`` (the canonical driver tag) is a top-level
        # record field — a peer of event/payload, not a payload entry — so it
        # never collides with or pollutes the event's factual detail.
        append_audit_event(
            tmp_path,
            event="run",
            payload={"run": "RUN-001", "exit_code": 0},
            timestamp="2026-07-19T10:00:00Z",
            origin="implement-leg",
        )

        record = _json_records(tmp_path)[0]
        assert record["origin"] == "implement-leg"
        # origin stays out of the payload bag.
        assert "origin" not in record["payload"]

        md = _md(tmp_path)
        assert "origin=implement-leg" in md

    def test_origin_omitted_when_none_keeps_legacy_shape(self, tmp_path: Path) -> None:
        # A caller that does not pass origin must produce the legacy record
        # shape (no origin key) so older consumers are unaffected.
        append_audit_event(
            tmp_path,
            event="create",
            payload={"feature": "FEATURE-001"},
            timestamp="2026-07-19T10:00:00Z",
        )
        assert _json_records(tmp_path)[0] == {
            "timestamp": "2026-07-19T10:00:00Z",
            "event": "create",
            "payload": {"feature": "FEATURE-001"},
        }
        assert "origin=" not in _md(tmp_path)

    def test_elapsed_ms_payload_field_appears_in_both_products(self, tmp_path: Path) -> None:
        # v0.4 ticket 02: a duration event carries ``elapsed_ms`` in its payload
        # (native int in JSON, rendered in markdown); the two products stay in
        # sync just like every other payload fact.
        append_audit_event(
            tmp_path,
            event="run",
            payload={"run": "RUN-001", "exit_code": 0, "elapsed_ms": 42000},
            timestamp="2026-07-19T10:00:00Z",
            origin="cli",
        )

        record = _json_records(tmp_path)[0]
        assert record["payload"]["elapsed_ms"] == 42000
        assert record["origin"] == "cli"

        md = _md(tmp_path)
        assert "elapsed_ms: 42000" in md
        assert "origin=cli" in md

    def test_non_duration_event_carries_no_elapsed_ms(self, tmp_path: Path) -> None:
        # ``elapsed_ms`` absence is meaningful (the event has no duration
        # semantics), so a non-duration event must not carry a placeholder 0.
        append_audit_event(
            tmp_path,
            event="create",
            payload={"feature": "FEATURE-001"},
            timestamp="2026-07-19T10:00:00Z",
            origin="cli",
        )
        assert "elapsed_ms" not in _json_records(tmp_path)[0]["payload"]
