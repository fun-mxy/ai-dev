"""Shared time formatting.

One place for the ISO-8601 UTC stamp (second precision, ``Z`` suffix) so every
writer — audit records, intent capture, and (later) run metadata — renders
timestamps identically. Hoisted out of the individual writers to stop the format
string from being copy-pasted across modules as more writers arrive.
"""

from __future__ import annotations

from datetime import datetime, timezone

_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utc_now_iso() -> str:
    """Current UTC time as ``YYYY-MM-DDTHH:MM:SSZ`` (cf. §13.2 metadata.json stamp)."""
    return datetime.now(timezone.utc).strftime(_ISO_FORMAT)


def _parse_iso(stamp: str) -> datetime:
    """Parse an ``utc_now_iso``-shaped stamp back to an aware ``datetime``."""
    return datetime.strptime(stamp, _ISO_FORMAT).replace(tzinfo=timezone.utc)


def elapsed_ms_between(started_at: str, ended_at: str) -> int:
    """Whole-millisecond delta between two ``utc_now_iso`` stamps (≥ 0).

    The audit ``elapsed_ms`` convention (v0.4 ticket 02): events with duration
    semantics (``run`` / ``verify`` / ``lane_gate`` / ``coherence_gate`` /
    ``fix_run``) carry ``ended - started`` in milliseconds. The stamps share
    ``utc_now_iso``'s second precision, so sub-second work rounds to 0 — that is
    honest (the gate *was* sub-second), not a missing value. Stamps are
    injectable end to end, so tests get deterministic non-zero deltas.
    """
    delta = _parse_iso(ended_at) - _parse_iso(started_at)
    return max(0, int(delta.total_seconds() * 1000))
