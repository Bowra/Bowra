"""Viewer time zone parsing and local day boundaries (session list groups)."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from bowhead_gateway.viewer_time_zone import local_day_start, parse_time_zone


@pytest.mark.parametrize("value", ["America/Los_Angeles", "Europe/Berlin", "Asia/Shanghai", " UTC "])
def test_known_iana_zones_are_used(value: str) -> None:
    assert parse_time_zone(value) == ZoneInfo(value.strip())


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", "Mars/Base", "../etc/passwd", "/etc/localtime", "UTC+8", "z" * 100, "America", "Etc", "Europe", "posixrules"],
)
def test_missing_or_unknown_zones_fall_back_to_utc(value: str | None) -> None:
    assert parse_time_zone(value) is UTC


def test_day_start_is_local_midnight_as_a_utc_instant() -> None:
    now = datetime(2026, 10, 8, 3, 0, tzinfo=UTC)
    assert local_day_start(now, ZoneInfo("America/Los_Angeles")) == datetime(2026, 10, 7, 7, 0, tzinfo=UTC)
    assert local_day_start(now, ZoneInfo("Asia/Shanghai")) == datetime(2026, 10, 7, 16, 0, tzinfo=UTC)
    assert local_day_start(now, UTC, days_back=7) == datetime(2026, 10, 1, tzinfo=UTC)


def test_day_starts_across_a_dst_change_stay_on_local_midnight() -> None:
    # Berlin leaves summer time on 2026-10-25: that day has 25 hours.
    now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
    zone = ZoneInfo("Europe/Berlin")
    assert local_day_start(now, zone) == datetime(2026, 10, 25, 23, 0, tzinfo=UTC)  # 00:00 CET
    assert local_day_start(now, zone, days_back=1) == datetime(2026, 10, 24, 22, 0, tzinfo=UTC)  # 00:00 CEST


@pytest.mark.parametrize(
    ("zone", "today_start", "yesterday_start"),
    [
        # 2026-10-08 03:00Z is 08:30 on Oct 8 in Kolkata (+5:30) and 16:45 on Oct 8 in Chatham (+13:45 in summer).
        ("Asia/Kolkata", datetime(2026, 10, 7, 18, 30, tzinfo=UTC), datetime(2026, 10, 6, 18, 30, tzinfo=UTC)),
        ("Pacific/Chatham", datetime(2026, 10, 7, 10, 15, tzinfo=UTC), datetime(2026, 10, 6, 10, 15, tzinfo=UTC)),
    ],
)
def test_fractional_offset_zones_start_days_at_local_midnight(zone, today_start, yesterday_start) -> None:
    now = datetime(2026, 10, 8, 3, 0, tzinfo=UTC)
    assert local_day_start(now, parse_time_zone(zone)) == today_start
    assert local_day_start(now, parse_time_zone(zone), days_back=1) == yesterday_start
