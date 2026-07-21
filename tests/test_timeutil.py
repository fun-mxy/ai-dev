"""timeutil.elapsed_ms_between — the audit ``elapsed_ms`` convention (v0.4 ticket 02)."""

from __future__ import annotations

from ai_dev.timeutil import elapsed_ms_between


class TestElapsedMsBetween:
    def test_whole_second_delta_in_milliseconds(self) -> None:
        # 90 seconds -> 90000 ms.
        assert (
            elapsed_ms_between("2026-07-19T10:00:00Z", "2026-07-19T10:01:30Z")
            == 90_000
        )

    def test_sub_second_rounds_to_zero(self) -> None:
        # The stamps share utc_now_iso's second precision, so a sub-second span
        # is honestly 0 (not a missing value).
        assert elapsed_ms_between("2026-07-19T10:00:00Z", "2026-07-19T10:00:00Z") == 0

    def test_minute_delta(self) -> None:
        assert (
            elapsed_ms_between("2026-07-19T10:00:00Z", "2026-07-19T10:05:00Z")
            == 300_000
        )

    def test_inverted_pair_clamps_to_zero(self) -> None:
        # A swapped/inverted pair clamps to 0 rather than reporting a negative
        # duration (defensive — the convention is ended - started >= 0).
        assert elapsed_ms_between("2026-07-19T10:01:00Z", "2026-07-19T10:00:00Z") == 0

    def test_cross_day_boundary(self) -> None:
        assert (
            elapsed_ms_between("2026-07-19T23:59:00Z", "2026-07-20T00:01:00Z")
            == 120_000
        )
