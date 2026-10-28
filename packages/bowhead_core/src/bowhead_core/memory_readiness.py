"""Permanent memory-policy storage readiness for supported legacy migration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bowhead_core.user_memory import UserMemoryStore

logger = logging.getLogger(__name__)
_STORAGE_READY = True


async def validate_memory_storage(user_memory_store: UserMemoryStore) -> str | None:
    """Keep unclassified legacy memories readable until policy migration completes."""
    global _STORAGE_READY
    _STORAGE_READY = False
    ready, reason = await user_memory_store.v2_readiness()
    _STORAGE_READY = ready
    if not ready:
        logger.error("memory_storage.not_ready reason=%s", reason)
    logger.info("memory_storage.effective ready=%s", ready)
    return None if ready else reason


def memory_storage_ready() -> bool:
    """Whether the initialized store supports canonical and downstream policy reads."""
    return _STORAGE_READY
