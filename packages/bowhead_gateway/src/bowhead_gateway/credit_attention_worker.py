"""Background materializer for credit activation attention state."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from bowhead_core.credit_admin_store import cache_invalidate_team

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from bowhead_core.user_store import UserStore

logger = logging.getLogger(__name__)


def env_interval_s() -> float:
    raw = os.environ.get("BOWHEAD_CREDIT_ATTENTION_MATERIALIZE_INTERVAL_S", "300")
    try:
        return max(30.0, float(raw))
    except ValueError:
        return 300.0


def env_materializer_enabled() -> bool:
    if os.environ.get("BOWHEAD_CREDIT_ATTENTION_WORKER_DISABLED") == "1":
        return False
    raw = os.environ.get("BOWHEAD_CREDIT_ATTENTION_MATERIALIZER_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


async def materialize_credit_attention_once(
    user_store: UserStore,
    *,
    team_limit: int = 500,
    team_offset: int = 0,
    metrics_log: Any | None = None,
    cache_invalidator: Callable[[str], Awaitable[Any]] | None = cache_invalidate_team,
) -> dict[str, Any]:
    team_ids = await user_store.list_active_team_ids(limit=team_limit, offset=team_offset)
    ok = 0
    failed = 0
    for team_id in team_ids:
        try:
            await user_store.materialize_credit_attention(
                team_id,
                limit=20,
                metrics_log=metrics_log,
            )
            if cache_invalidator is not None:
                await cache_invalidator(team_id)
            ok += 1
        except Exception as exc:
            failed += 1
            logger.warning(
                "credit_attention.materialize_team_failed",
                extra={"team_id": team_id, "err": str(exc)},
            )
    return {"team_count": len(team_ids), "ok": ok, "failed": failed}


async def run_credit_attention_materializer(
    user_store: UserStore,
    *,
    interval_s: float,
    metrics_log: Any | None = None,
    cache_invalidator: Callable[[str], Awaitable[Any]] | None = cache_invalidate_team,
) -> None:
    team_limit = 500
    team_offset = 0
    while True:
        try:
            result = await materialize_credit_attention_once(
                user_store,
                team_limit=team_limit,
                team_offset=team_offset,
                metrics_log=metrics_log,
                cache_invalidator=cache_invalidator,
            )
            team_count = int(result.get("team_count") or 0)
            team_offset = 0 if team_count < team_limit else team_offset + team_count
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            team_offset = 0
            logger.warning(
                "credit_attention.materializer_failed",
                extra={"err": str(exc)},
            )
        await asyncio.sleep(interval_s)
