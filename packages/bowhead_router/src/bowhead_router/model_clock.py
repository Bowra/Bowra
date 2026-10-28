"""The model's clock: the single source of "today" and "now" given to models and tool advice.

Every date a prompt or a tool's advisory text states as the current date reads
``model_now()`` / ``model_today()``. Two independent computations used to
disagree inside one turn: the subagent ``<current_date>`` anchor and BI advice
used the deployment offset, while the Router's ``<runtime_context>`` banner used
UTC, so around midnight the model was told two different "todays".

Zone selection:

1. A turn started by a user request that carried a valid viewer time zone (the
   browser's IANA zone, ``X-Bowhead-Time-Zone``) runs inside
   ``model_clock_zone(zone)``; everything in that turn, including subagents,
   tools and Router calls, reads that zone. The zone is part of the turn's
   admitted snapshot, so resumed, continued and queued follow-up executions
   re-enter the same zone. A scheduled run uses its job's saved zone the same way.
2. Model calls that serve a user request before or outside a turn (the
   @-reference receiver choice in chat send, plan revise/replan previews) bind
   the request's viewer zone for the duration of that call.
3. Otherwise (IM channels, background tasks, missing or invalid zone) the
   deployment clock: a fixed UTC offset from ``BOWHEAD_TZ_OFFSET_HOURS``
   (default +8).

This module lives in the Router package because the Router is the lowest layer
that states a date to the model; ``bowhead_core.model_clock`` re-exports it and
adds request/name validation.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone, tzinfo

_OFFSET_ENV = "BOWHEAD_TZ_OFFSET_HOURS"
_DEFAULT_OFFSET_H = 8.0

_TURN_ZONE: ContextVar[tzinfo | None] = ContextVar("bowhead_model_clock_zone", default=None)


def deploy_tz_offset_hours() -> float:
    """Deployment offset in hours; invalid values fall back to +8.

    Zero (UTC) and negative offsets are valid, so this does not use the
    positive-only ``env_float`` helper.
    """
    raw = os.environ.get(_OFFSET_ENV, "").strip()
    try:
        offset_h = float(raw) if raw else _DEFAULT_OFFSET_H
    except (ValueError, TypeError):
        return _DEFAULT_OFFSET_H
    # timedelta(hours=...) must stay strictly within +-24h; out-of-range, inf and nan
    # (every comparison is False) fall back. Raising here would stop every model call.
    if not (-24.0 < offset_h < 24.0):
        return _DEFAULT_OFFSET_H
    return offset_h


def deploy_time_zone() -> tzinfo:
    """The deployment clock's fixed-offset zone."""
    return timezone(timedelta(hours=deploy_tz_offset_hours()))


def turn_time_zone() -> tzinfo | None:
    """The viewer zone bound to the current turn, or None outside a viewer turn."""
    return _TURN_ZONE.get()


def model_time_zone() -> tzinfo:
    """The zone the model's dates use: the turn's viewer zone, else the deployment clock."""
    return _TURN_ZONE.get() or deploy_time_zone()


def model_now() -> datetime:
    """The current moment in :func:`model_time_zone` (timezone-aware)."""
    return datetime.now(model_time_zone())


def model_today() -> date:
    """Today's date in :func:`model_time_zone`."""
    return model_now().date()


@contextmanager
def model_clock_zone(zone: tzinfo | None) -> Iterator[None]:
    """Bind ``zone`` as the model clock for the enclosed execution (None = deployment clock).

    Tasks created inside the block inherit the binding through context
    variables, so subagents and tool calls started by the turn share it.
    """
    token = _TURN_ZONE.set(zone)
    try:
        yield
    finally:
        _TURN_ZONE.reset(token)
