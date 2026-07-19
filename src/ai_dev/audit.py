"""Audit log appender — v0.0 minimal slice (ticket 01).

Ticket 02 will replace this with a structured, append-only, Markdown+JSON
double-product component (§4.4). For the tracer bullet we only need to land a
human-readable ``create`` line in ``audit.log.md``; this tiny appender is the
seam ticket 02 swaps out.

The timestamp is injectable so callers (and tests) get determinism; the default
is the current UTC time in the spec's ISO-8601 UTC shape (cf. the
``metadata.json`` stamp shown in §13.2 — the closest spec precedent; §13.2 does
not itself define an audit timestamp format).
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from ai_dev.timeutil import utc_now_iso


def append_audit_record(
    audit_path: Path,
    event: str,
    fields: Mapping[str, str],
    *,
    timestamp: str | None = None,
) -> None:
    """Append one ``event`` record to ``audit_path`` as markdown.

    The file is opened in append mode so prior records are never rewritten
    (append-only). Each record is a ``## <timestamp> · <event>`` heading
    followed by one ``- key: value`` bullet per field, in insertion order.
    """
    stamp = timestamp if timestamp is not None else utc_now_iso()
    lines = [f"## {stamp} · {event}", ""]
    for key, value in fields.items():
        lines.append(f"- {key}: {value}")
    lines.append("")  # blank line separating records

    with audit_path.open("a") as f:
        f.write("\n".join(lines) + "\n")
