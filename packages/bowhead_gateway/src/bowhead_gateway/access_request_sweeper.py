"""Hourly access-request lifecycle sweeper.

Each tick expires pending requests past ``expires_at`` (14 days) and escalates real
requests that their owner left unanswered for 72 hours. The work itself lives in
``routes.access_requests.sweep_access_requests_once``. The loop starts only while the
access-request flow (``BOWHEAD_ACCESS_REQUESTS``) is on.
Disabled with ``BOWHEAD_ACCESS_REQUEST_SWEEPER_DISABLED=1`` (tests + emergency).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

_DEFAULT_INTERVAL_S = 3_600


async def run_access_request_sweeper(
    app: Any,
    *,
    interval_s: int = _DEFAULT_INTERVAL_S,
    stop_event: asyncio.Event | None = None,
) -> None:
    from bowhead_gateway.routes.access_requests import sweep_access_requests_once

    log.info("access_request_sweeper.started interval_s=%s", interval_s)
    try:
        while True:
            try:
                await sweep_access_requests_once(app)
            except Exception as exc:
                log.warning("access_request_sweeper.tick_failed err=%s", exc)
            if stop_event is not None:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
                    break
                except TimeoutError:
                    pass
            else:
                await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        log.info("access_request_sweeper.cancelled")
        raise
    finally:
        log.info("access_request_sweeper.stopped")


def sweeper_disabled() -> bool:
    return os.environ.get("BOWHEAD_ACCESS_REQUEST_SWEEPER_DISABLED") == "1"
