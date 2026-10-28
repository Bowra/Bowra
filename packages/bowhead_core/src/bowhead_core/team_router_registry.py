"""P4.5.2 — Team-scoped Router cache.

Lazy build per team from llm_config. Cache 100 LRU + idle 1h kill.

Build process:
  1. read team.llm_config (encrypted → decrypt)
  2. for each provider in providers[]: instantiate adapter (anthropic/openai/qwen)
  3. build agent_routing map → AgentBinding(provider_name, model)
  4. wrap as Router (复用 router.Router class)

如果 team 没配 llm_config → 返 None, caller fallback 用 app default router.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from bowhead_core.team_llm_config import build_router_from_v2_config, is_v2_config

if TYPE_CHECKING:
    from bowhead_router.router import Router

logger = logging.getLogger(__name__)

_IDLE_TIMEOUT_SEC = 3600
_MAX_CACHE = 100


class _TeamRouterEntry:
    def __init__(self, router: Router, version: int) -> None:
        self.router = router
        self.version = version
        self.last_used: float = time.time()


class TeamRouterRegistry:
    def __init__(self) -> None:
        self._cache: dict[str, _TeamRouterEntry] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task[None] | None = None

    async def start_background(self) -> None:
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reaper())

    async def stop(self) -> None:
        if self._reaper_task:
            self._reaper_task.cancel()

    async def _reaper(self) -> None:
        try:
            while True:
                await asyncio.sleep(300)  # 5 min check
                now = time.time()
                async with self._lock:
                    for oid, e in list(self._cache.items()):
                        if now - e.last_used > _IDLE_TIMEOUT_SEC:
                            del self._cache[oid]
                            logger.info("team_router.idle_evict team=%s", oid)
        except asyncio.CancelledError:
            pass

    def invalidate(self, team_id: str) -> None:
        self._cache.pop(team_id, None)

    async def get(
        self,
        team_id: str,
        llm_config: dict[str, Any],
        version: int = 0,
    ) -> Router | None:
        """Build Router from llm_config. None 如果 config 空 (caller 走 default router)."""
        if not llm_config or not llm_config.get("providers"):
            return None
        async with self._lock:
            entry = self._cache.get(team_id)
            if entry is not None and entry.version == version:
                entry.last_used = time.time()
                return entry.router
            # build new
            router = self._build_router(llm_config, team_id)
            if router is None:
                return None
            router.configuration_version = version if version > 0 else None
            # LRU evict if over cap
            if len(self._cache) >= _MAX_CACHE:
                oldest = min(self._cache.items(), key=lambda kv: kv[1].last_used)
                del self._cache[oldest[0]]
            self._cache[team_id] = _TeamRouterEntry(router, version)
            return router

    def _build_router(self, llm_config: dict[str, Any], team_id: str | None = None) -> Router | None:
        if not is_v2_config(llm_config):
            logger.info("team_router.legacy_config_ignored")
            return None
        try:
            # credit gap23 step 1: this is the **only production construction site** for team routers. Stamp team
            # identity here so the usage chokepoint can determine ownership without changing any of the 20+ callers.
            router = build_router_from_v2_config(llm_config, owner_team_id=team_id)
        except Exception as e:
            logger.warning("team_router.v2_build_failed err=%s", e)
            return None
        return router
