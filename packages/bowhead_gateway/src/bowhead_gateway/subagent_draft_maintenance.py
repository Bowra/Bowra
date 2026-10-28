"""Lightweight bounded maintenance loop for conversational Subagent drafts."""

from __future__ import annotations

import logging

import anyio
from bowhead_core.subagent_authoring_context_store import (
    SubagentAuthoringContextStore,
)
from bowhead_core.subagent_draft_accounting import (
    SubagentDraftCreditAccountant,
)
from bowhead_core.subagent_draft_store import SubagentDraftStore

_logger = logging.getLogger(__name__)
DEFAULT_INTERVAL_SECONDS = 300.0


async def run_subagent_draft_maintenance(
    *,
    draft_store: SubagentDraftStore,
    context_store: SubagentAuthoringContextStore,
    accountant: SubagentDraftCreditAccountant,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    limit: int = 100,
) -> None:
    """Run idempotent limited cleanup; cancellation remains immediate."""
    if interval_seconds <= 0:
        raise ValueError("maintenance interval must be positive")
    while True:
        try:
            draft_result = await draft_store.run_maintenance(limit=limit)
            expired_contexts = await context_store.purge_expired(limit=limit)
            settled_usage = await accountant.settle_pending(limit=limit)
            released_reservations = await accountant.release_stale(
                limit=limit,
            )
            if (
                draft_result.interrupted
                or draft_result.expired
                or draft_result.scrubbed
                or draft_result.purged
                or draft_result.request_claims_purged
                or expired_contexts
                or settled_usage
                or released_reservations
            ):
                _logger.info(
                    "subagent_draft.maintenance interrupted=%d expired=%d scrubbed=%d purged=%d claims=%d contexts=%d settled=%d reservations=%d",
                    draft_result.interrupted,
                    draft_result.expired,
                    draft_result.scrubbed,
                    draft_result.purged,
                    draft_result.request_claims_purged,
                    expired_contexts,
                    settled_usage,
                    released_reservations,
                )
        except Exception:
            _logger.exception("subagent_draft.maintenance_failed")
        await anyio.sleep(interval_seconds)


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "run_subagent_draft_maintenance",
]
