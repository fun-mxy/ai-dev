"""audit.append_audit_record — minimal inline appender (§2.1 / §4.4, ticket 01).

Ticket 02 owns the structured, append-only, md+json double-product audit
component. Ticket 01 only needs a tiny inline markdown appender so the
``create`` event lands in ``audit.log.md``; the signature below is the seam
ticket 02 will replace behind.
"""

from __future__ import annotations

from pathlib import Path

from ai_dev.audit import append_audit_record


class TestAppendAuditRecord:
    def test_creates_file_with_first_record(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.log.md"

        append_audit_record(
            audit,
            event="create",
            fields={"feature": "FEATURE-001"},
            timestamp="2026-07-19T10:00:00Z",
        )

        text = audit.read_text()
        assert "create" in text
        assert "FEATURE-001" in text
        assert "2026-07-19T10:00:00Z" in text

    def test_is_append_only(self, tmp_path: Path) -> None:
        # Second record must not clobber the first (§2.1 traceability backbone).
        audit = tmp_path / "audit.log.md"
        append_audit_record(
            audit, event="create", fields={"feature": "FEATURE-001"},
            timestamp="2026-07-19T10:00:00Z",
        )
        append_audit_record(
            audit, event="create", fields={"feature": "FEATURE-002"},
            timestamp="2026-07-19T10:01:00Z",
        )

        text = audit.read_text()
        assert "FEATURE-001" in text
        assert "FEATURE-002" in text
        assert text.count("feature: FEATURE-001") == 1
        assert text.count("feature: FEATURE-002") == 1

    def test_renders_all_fields(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.log.md"
        append_audit_record(
            audit,
            event="create",
            fields={"feature": "FEATURE-001", "intent": "add a thing"},
            timestamp="2026-07-19T10:00:00Z",
        )
        text = audit.read_text()
        assert "feature: FEATURE-001" in text
        assert "intent: add a thing" in text

    def test_timestamp_defaults_to_now_when_omitted(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.log.md"
        append_audit_record(audit, event="create", fields={"feature": "FEATURE-001"})
        # No exact value to assert (time is non-deterministic), but a UTC
        # ISO-8601 stamp must be present.
        import re

        assert re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", audit.read_text())
