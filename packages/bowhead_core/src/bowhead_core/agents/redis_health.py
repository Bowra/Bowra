"""RedisHealthMonitor — broker liveness gate for CeleryTaskRunner (v2 PR-#1).

CeleryTaskRunner.dispatch checks `await monitor.healthy()` before fan-out;
unhealthy → fall back to LocalTaskRunner (graceful degrade).

设计:
- 真 PING via redis-py asyncio client (lazy import, optional dep)
- Result cached for `cache_ttl_s` to avoid hammering broker on every dispatch
- Default `redis_url` 可被 ctor 或 env BOWHEAD_REDIS_URL 覆盖
- timeout 默认 1s — broker 卡 → 视为 unhealthy 立即 fallback
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time

logger = logging.getLogger(__name__)

_DEFAULT_REDIS_URL = "redis://localhost:6379/0"
_DEFAULT_CACHE_TTL_S = 5.0
_DEFAULT_PING_TIMEOUT_S = 1.0


class RedisHealthMonitor:
    """Cheap PING-based redis broker health probe.

    Threadsafe note: 单 process 1 instance 通常足够; 不是线程安全 (cache 字段裸读写).
    """

    def __init__(
        self,
        redis_url: str | None = None,
        *,
        cache_ttl_s: float = _DEFAULT_CACHE_TTL_S,
        ping_timeout_s: float = _DEFAULT_PING_TIMEOUT_S,
    ) -> None:
        self.redis_url = redis_url or os.environ.get("BOWHEAD_REDIS_URL") or _DEFAULT_REDIS_URL
        self.cache_ttl_s = cache_ttl_s
        self.ping_timeout_s = ping_timeout_s
        self._last_check_ts: float = 0.0
        self._last_healthy: bool = True  # optimistic default

    async def healthy(self) -> bool:
        """Return True if last cached check (or fresh ping) succeeded.

        Cache TTL controls ping frequency. First call always pings.
        """
        now = time.monotonic()
        if self._last_check_ts > 0 and now - self._last_check_ts < self.cache_ttl_s:
            return self._last_healthy
        self._last_healthy = await self._ping()
        self._last_check_ts = now
        return self._last_healthy

    async def _ping(self) -> bool:
        try:
            import redis.asyncio as aioredis
        except ImportError:
            logger.warning(
                "redis-py not installed; RedisHealthMonitor reporting unhealthy",
            )
            return False

        client = None
        try:
            client = aioredis.from_url(
                self.redis_url,
                socket_timeout=self.ping_timeout_s,
            )
            await asyncio.wait_for(client.ping(), timeout=self.ping_timeout_s)
            return True
        except TimeoutError as e:
            logger.warning("redis ping timeout (%s): %s", self.redis_url, e)
            return False
        except Exception as e:
            logger.warning("redis ping failed (%s): %s", self.redis_url, e)
            return False
        finally:
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.aclose()

    def force_set(self, healthy: bool) -> None:
        """Test-only: force cached state. Resets TTL window."""
        self._last_healthy = healthy
        self._last_check_ts = time.monotonic()
