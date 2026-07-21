"""Structured audit log appender — the §2.1 traceability backbone (ticket 02).

Every canonical-state change, gate verdict, decision and run lifecycle event
flows through ``append_audit_event`` as a timestamped ``event`` with a
structured ``payload``; later tickets reuse this one seam. One append writes the
§4.4 double product — ``audit.log.md`` (human) + ``audit.log.json`` (machine) —
from a single record; the log is content append-only (entries never mutated or
removed). The timestamp defaults to the spec's ISO-8601 UTC shape (cf. §13.2)
and is injectable for deterministic tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from ai_dev.timeutil import utc_now_iso

AUDIT_LOG_MD = "audit.log.md"
AUDIT_LOG_JSON = "audit.log.json"


def _render_value(value: Any) -> str:
    """Render a payload value for the markdown product.

    Strings render bare (``- key: value``); any other JSON-serialisable type
    renders as compact JSON, so the markdown stays single-line while the JSON
    product keeps the native type (§2.1 structured records, §4.4).
    """
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _read_records(json_path: Path) -> list[dict[str, Any]]:
    """Load the existing JSON record array, or start empty when no file yet."""
    if not json_path.exists():
        return []
    return json.loads(json_path.read_text())


def append_audit_event(
    audit_dir: Path,
    event: str,
    payload: Mapping[str, Any],
    *,
    timestamp: str | None = None,
    origin: str | None = None,
) -> None:
    """Append one timestamped ``event`` record to the audit log in ``audit_dir``.

    Writes ``audit.log.md`` and ``audit.log.json`` from the same record so the
    two stay consistent. ``payload`` values may be any JSON-serialisable type —
    strings render bare in the markdown, other types as compact JSON, while the
    JSON product keeps their native type.

    ``origin`` (v0.4 ticket 02) is the canonical driver tag — *which* driver
    triggered the event (``cli`` / ``implement-leg`` / ``fix-run-driver`` / …),
    threaded in explicitly by the caller (never inferred). When supplied it
    lands as a top-level record field (a peer of ``event``/``payload``) in both
    products; when ``None`` it is omitted so legacy callers and the appender's
    own mechanics tests are unaffected. Payload is reserved for the event's
    factual detail — ``elapsed_ms`` lives there, ``origin`` does not.

    Append-only is a content invariant: the markdown is byte-appended, and the
    JSON array is only ever lengthened — existing records are read back verbatim
    and a new one pushed. v0 is single-writer with no crash recovery (§23.3), so
    the array rewrite is safe; byte-level durability/concurrency is deferred.
    """
    audit_dir.mkdir(parents=True, exist_ok=True)
    md_path = audit_dir / AUDIT_LOG_MD
    json_path = audit_dir / AUDIT_LOG_JSON

    stamp = timestamp if timestamp is not None else utc_now_iso()
    record: dict[str, Any] = {
        "timestamp": stamp,
        "event": event,
        "payload": dict(payload),
    }
    if origin is not None:
        record["origin"] = origin

    header = f"## {stamp} · {event}"
    if origin is not None:
        header += f" · origin={origin}"
    lines = [header, ""]
    for key, value in payload.items():
        lines.append(f"- {key}: {_render_value(value)}")
    lines.append("")
    with md_path.open("a") as f:
        f.write("\n".join(lines) + "\n")

    records = _read_records(json_path)
    records.append(record)
    with json_path.open("w") as f:
        json.dump(records, f, indent=2)
        f.write("\n")
