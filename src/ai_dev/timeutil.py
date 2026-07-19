"""Shared time formatting.

One place for the ISO-8601 UTC stamp (second precision, ``Z`` suffix) so every
writer — audit records, intent capture, and (later) run metadata — renders
timestamps identically. Hoisted out of the individual writers to stop the format
string from being copy-pasted across modules as more writers arrive.
"""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now_iso() -> str:
    """Current UTC time as ``YYYY-MM-DDTHH:MM:SSZ`` (cf. §13.2 metadata.json stamp)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
