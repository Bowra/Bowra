"""The viewer's time zone for server-computed calendar boundaries.

Times shown in the product follow the viewer's browser zone. Most responses carry
UTC ISO timestamps and the client formats them; the few values the server derives
from calendar days (the sidebar's Today / Yesterday / Last 7 days groups) need the
viewer's zone. Clients send it as the ``X-Bowhead-Time-Zone`` header (or a ``tz``
query parameter); a missing or unknown zone falls back to UTC, which is also what
older clients get.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo

from bowhead_core.iana_time_zone import load_iana_time_zone
from fastapi import Request

TIME_ZONE_HEADER = "X-Bowhead-Time-Zone"
TIME_ZONE_QUERY = "tz"


def parse_time_zone(value: str | None) -> tzinfo:
    """Return the IANA zone named by ``value``, or UTC when it is empty or not a known zone."""
    return load_iana_time_zone(value) or UTC


def viewer_time_zone_name(request: Request) -> str:
    """The known IANA zone name the client sent (header first, then ``tz`` query), or ``""``.

    Turn admission stores this name with the turn so the model's "today" follows the
    viewer (``bowhead_core.model_clock``); ``""`` keeps the deployment clock.
    """
    raw = request.headers.get(TIME_ZONE_HEADER)
    if not raw:
        query = getattr(request, "query_params", None)
        raw = query.get(TIME_ZONE_QUERY) if query is not None else None
    zone = load_iana_time_zone(raw)
    return zone.key if zone is not None else ""


def viewer_time_zone(request: Request) -> tzinfo:
    """The zone the client asked for (header first, then ``tz`` query), else UTC."""
    return parse_time_zone(request.headers.get(TIME_ZONE_HEADER) or request.query_params.get(TIME_ZONE_QUERY))


def local_day_start(now: datetime, zone: tzinfo, *, days_back: int = 0) -> datetime:
    """Midnight of the calendar day ``days_back`` days before ``now``'s day in ``zone``, as a UTC instant.

    Built from the local date (not by subtracting 24h), so DST days of 23 or 25
    hours still start at local midnight.
    """
    local_date = now.astimezone(zone).date() - timedelta(days=days_back)
    return datetime(local_date.year, local_date.month, local_date.day, tzinfo=zone).astimezone(UTC)
