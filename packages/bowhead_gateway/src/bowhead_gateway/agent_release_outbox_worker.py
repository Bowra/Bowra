"""Background delivery loop for Agent release notifications."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bowhead_core.agent_release_outbox import AgentReleaseNotificationOutbox

log = logging.getLogger(__name__)


async def run_agent_release_outbox_worker(
    *,
    outbox: AgentReleaseNotificationOutbox,
    worker_id: str,
    interval_seconds: float = 1.0,
) -> None:
    if interval_seconds <= 0:
        raise ValueError("Agent release outbox interval must be positive")
    try:
        while True:
            try:
                result = await outbox.drain_once(worker_id=worker_id)
                if result.failed:
                    log.error(
                        "agent_release_outbox.delivery_failed count=%s",
                        result.failed,
                    )
                if result.claimed >= 50:
                    await asyncio.sleep(0)
                    continue
            except Exception:
                log.exception("agent_release_outbox.tick_failed")
            await asyncio.sleep(interval_seconds)
    except asyncio.CancelledError:
        log.info("agent_release_outbox.cancelled")
        raise


__all__ = ["run_agent_release_outbox_worker"]
