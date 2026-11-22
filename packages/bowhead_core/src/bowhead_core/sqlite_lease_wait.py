"""Record statements that waited for a cross-file authority lease.

``DomainConnection._operation`` lets a statement that starts outside any transaction wait for a
domain's authority lease in 10 ms steps, up to the connection's busy timeout, while another
connection holds that lease exclusively. Nothing logged that wait, so a slow request could not be told
apart from CPU saturation (in-process data service design, section 6.7). The runtime calls
:func:`record_wait` once per statement that waited, never on a statement that did not, and keeps the
in-process exclusive holders here so a wait can name what it waited for.

Events, value-free like ``sqlite.operation_failed``:

- ``sqlite.lease_wait``: one statement that waited at least :data:`LOG_THRESHOLD_MS`, or whose wait
  ended without the lease.
- ``sqlite.lease_wait_summary``: every wait, aggregated per lease domain and call site over a window of
  :data:`SUMMARY_WINDOW_S`. A window is written by the first wait after it ends, or by
  :func:`flush_summary` (the gateway calls it at shutdown); a quiet period therefore writes nothing. Its
  ``window_ended`` is at most :data:`SUMMARY_WINDOW_S` after ``window_started``, never the time of the wait
  that closed it.

The holder registry records exclusive holders only. ``holder_known=false`` therefore means either a holder in
another process or a statement that asked for an exclusive lease while connections of this process held it
shared. A connection collected while holding a lease leaves its entry until the next exclusive holder of that
domain replaces it.
"""

from __future__ import annotations

import contextvars
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: A wait at least this long is logged on its own; shorter waits appear only in the summary.
LOG_THRESHOLD_MS = 50.0
#: A lease held at least this long is logged when it is released, with the call site that took it. A shared
#: lease blocks every exclusive writer of its domain for as long as it is held; see the lock hold-time spike.
HOLD_LOG_THRESHOLD_MS = 1000.0
#: Length of one summary window.
SUMMARY_WINDOW_S = 60.0
#: Call sites per window written as their own summary line; the rest are folded into one ``other`` line.
SUMMARY_MAX_KEYS = 50

_SAFE_VERSION = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


@dataclass(frozen=True, slots=True)
class LeaseHolder:
    """The in-process connection holding a domain's lease exclusively."""

    connection_id: str
    callsite: str
    since: float


# Keyed by domain. The authority gate is a file lock, so at most one connection of this process holds a
# domain exclusively; a holder in another process (a CLI, a second worker) is not visible here.
_holders: dict[str, LeaseHolder] = {}


def note_exclusive(domain: str, connection_id: str, callsite: str) -> None:
    """Remember the connection that has just taken ``domain``'s lease exclusively."""
    _holders[domain] = LeaseHolder(connection_id, callsite, time.monotonic())


def clear_exclusive(domain: str, connection_id: str) -> None:
    """Forget ``connection_id`` as ``domain``'s holder; called before the lock is released."""
    holder = _holders.get(domain)
    if holder is not None and holder.connection_id == connection_id:
        _holders.pop(domain, None)


def holder_of(domain: str) -> LeaseHolder | None:
    """The in-process exclusive holder of ``domain``, if any."""
    return _holders.get(domain)


@dataclass(slots=True)
class _Totals:
    count: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    timeouts: int = 0


_lock = threading.Lock()
_window_started: float | None = None
_window_started_at: datetime | None = None
_totals: dict[tuple[str, str], _Totals] = {}


def record_wait(
    *,
    lease_domain: str,
    lease_mode: int,
    waited_ms: float,
    attempts: int,
    outcome: str,
    fields: dict[str, Any],
    holder: LeaseHolder | None,
    holder_seen_at: float | None,
) -> None:
    """Log one statement that waited, and add it to the current summary window.

    ``fields`` carries the waiting statement's diagnostic context (database, connection, call site,
    statement summary, request identifiers). ``holder`` is the in-process exclusive holder seen when the
    wait began, and ``holder_seen_at`` the monotonic time it was seen.
    """
    callsite = str(fields.get("callsite") or "unavailable")
    if waited_ms >= LOG_THRESHOLD_MS or outcome != "proceeded":
        event = {
            "lease_domain": lease_domain,
            "lease_mode": "exclusive" if lease_mode == 2 else "shared",
            "waited_ms": round(waited_ms, 3),
            "attempts": attempts,
            "outcome": outcome,
            **fields,
            "holder_known": holder is not None,
            "holder_callsite": holder.callsite if holder is not None else None,
            "holder_connection_id": holder.connection_id if holder is not None else None,
            "holder_held_ms": round((holder_seen_at - holder.since) * 1000, 3) if holder is not None and holder_seen_at is not None else None,
            "app_version": _app_version(),
        }
        # Do not merge unrelated context variables into the event.
        contextvars.Context().run(log.info, "sqlite.lease_wait", **event)
    _add_to_summary(lease_domain, callsite, waited_ms, outcome != "proceeded")


def _add_to_summary(lease_domain: str, callsite: str, waited_ms: float, failed: bool) -> None:
    global _window_started, _window_started_at
    now = time.monotonic()
    ready: tuple[datetime, float, dict[tuple[str, str], _Totals]] | None = None
    with _lock:
        if _window_started is not None and now - _window_started >= SUMMARY_WINDOW_S:
            ready = _take_window(now)
        if _window_started is None:
            _window_started = now
            _window_started_at = datetime.now(UTC)
        totals = _totals.setdefault((lease_domain, callsite), _Totals())
        totals.count += 1
        totals.total_ms += waited_ms
        totals.max_ms = max(totals.max_ms, waited_ms)
        totals.timeouts += int(failed)
    if ready is not None:
        _write_summary(*ready)


def _take_window(now: float) -> tuple[datetime, float, dict[tuple[str, str], _Totals]] | None:
    """Detach the current window; the caller holds ``_lock``."""
    global _window_started, _window_started_at, _totals
    if _window_started is None or _window_started_at is None:
        return None
    # A window closed by a wait long after it ended still covers only its own span, not the quiet gap.
    window = (_window_started_at, min(now, _window_started + SUMMARY_WINDOW_S) - _window_started, _totals)
    _window_started, _window_started_at, _totals = None, None, {}
    return window


def flush_summary() -> None:
    """Write the current window now, for shutdown and tests."""
    with _lock:
        ready = _take_window(time.monotonic())
    if ready is not None:
        _write_summary(*ready)


def _write_summary(started_at: datetime, seconds: float, totals: dict[tuple[str, str], _Totals]) -> None:
    try:
        ranked = sorted(totals.items(), key=lambda item: item[1].total_ms, reverse=True)
        kept, folded = ranked[:SUMMARY_MAX_KEYS], ranked[SUMMARY_MAX_KEYS:]
        lines = [(domain, callsite, value) for (domain, callsite), value in kept]
        if folded:
            other = _Totals()
            for _, value in folded:
                other.count += value.count
                other.total_ms += value.total_ms
                other.max_ms = max(other.max_ms, value.max_ms)
                other.timeouts += value.timeouts
            lines.append(("*", "other", other))
        window = {
            "window_started": started_at.isoformat(),
            "window_ended": (started_at + timedelta(seconds=seconds)).isoformat(),
            "window_seconds": round(seconds, 3),
            "callsites_folded": len(folded),
            "app_version": _app_version(),
        }
        for domain, callsite, value in lines:
            contextvars.Context().run(
                log.info,
                "sqlite.lease_wait_summary",
                lease_domain=domain,
                callsite=callsite,
                count=value.count,
                total_ms=round(value.total_ms, 3),
                max_ms=round(value.max_ms, 3),
                timeouts=value.timeouts,
                **window,
            )
    except Exception:  # noqa: S110 -- A failing log sink must not break the statement that waited.
        pass


def _app_version() -> str:
    version = os.environ.get("BOWHEAD_VERSION", "")
    return version if _SAFE_VERSION.fullmatch(version) else "unavailable"


def record_hold(
    *,
    lease_domain: str,
    lease_mode: int,
    held_ms: float,
    context: Any,
    database: str | None,
    connection_id: str | None,
) -> None:
    """Log one lease that was held for at least ``HOLD_LOG_THRESHOLD_MS``, named by the code that took it.

    ``context`` is the operation context captured when the lease was taken (call site and request
    identifiers). Called after the lease is released, so logging never lengthens the hold.
    """
    if held_ms < HOLD_LOG_THRESHOLD_MS:
        return
    event = {
        "lease_domain": lease_domain,
        "lease_mode": "exclusive" if lease_mode == 2 else "shared",
        "held_ms": round(held_ms, 3),
        "database": database,
        "connection_id": connection_id,
        "callsite": str(getattr(context, "callsite", None) or "unavailable"),
        **dict(getattr(context, "request_ids", None) or ()),
        "app_version": _app_version(),
    }
    # Do not merge unrelated context variables into the event.
    contextvars.Context().run(log.info, "sqlite.lease_held_long", **event)
