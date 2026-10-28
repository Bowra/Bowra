"""Bounded maintenance loop for config copilot drafts (批 1-a).

三段 SQL 全部只触 TTL 非空行——lite_proposal（TTL 全 NULL，撤销无时限拍板）天然
零触碰；批 1-b form_draft 启用三段 TTL 后零改动生效。镜像 subagent_draft_maintenance。
"""

from __future__ import annotations

import logging

import anyio
from bowhead_core.config_draft_store import ConfigDraftStore

_logger = logging.getLogger(__name__)
DEFAULT_INTERVAL_SECONDS = 300.0


async def run_config_draft_maintenance(
    *,
    draft_store: ConfigDraftStore,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    limit: int = 100,
) -> None:
    """Run idempotent limited cleanup; cancellation remains immediate."""
    if interval_seconds <= 0:
        raise ValueError("maintenance interval must be positive")
    while True:
        try:
            result = await draft_store.run_maintenance(limit=limit)
            if result.expired or result.scrubbed or result.purged:
                _logger.info(
                    "config_draft.maintenance expired=%d scrubbed=%d purged=%d",
                    result.expired,
                    result.scrubbed,
                    result.purged,
                )
        except Exception:
            _logger.exception("config_draft.maintenance_failed")
        await anyio.sleep(interval_seconds)


__all__ = ["DEFAULT_INTERVAL_SECONDS", "run_config_draft_maintenance"]
