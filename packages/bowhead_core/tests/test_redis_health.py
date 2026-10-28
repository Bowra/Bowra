"""Sprint multi-agent-v2 PR-#1 — RedisHealthMonitor 单测."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bowhead_core.agents.redis_health import RedisHealthMonitor


@pytest.mark.asyncio
async def test_default_optimistic_until_check() -> None:
    """初始 _last_healthy=True (optimistic) — 但首次 healthy() 必 ping."""
    monitor = RedisHealthMonitor("redis://no-such-host:6379/0")
    # First call always pings (no cached check). no-such-host → unhealthy.
    healthy = await monitor.healthy()
    assert healthy is False


@pytest.mark.asyncio
async def test_force_set_overrides_cache() -> None:
    monitor = RedisHealthMonitor("redis://localhost:6379/0")
    monitor.force_set(False)
    assert await monitor.healthy() is False
    monitor.force_set(True)
    assert await monitor.healthy() is True


@pytest.mark.asyncio
async def test_cache_skips_redundant_ping() -> None:
    """cache_ttl 内重复调 healthy() 不 ping 多次."""
    monitor = RedisHealthMonitor(
        "redis://localhost:6379/0",
        cache_ttl_s=10.0,
    )
    monitor.force_set(True)  # primes cache + sets _last_check_ts

    # Second call should NOT touch redis (cache hit)
    with patch(
        "bowhead_core.agents.redis_health.RedisHealthMonitor._ping",
        new=AsyncMock(return_value=False),
    ) as mock_ping:
        healthy = await monitor.healthy()
        assert healthy is True
        mock_ping.assert_not_called()


@pytest.mark.asyncio
async def test_cache_expires_repings() -> None:
    monitor = RedisHealthMonitor(
        "redis://localhost:6379/0",
        cache_ttl_s=0.0,  # immediate expiry
    )
    monitor.force_set(True)

    with patch(
        "bowhead_core.agents.redis_health.RedisHealthMonitor._ping",
        new=AsyncMock(return_value=False),
    ) as mock_ping:
        healthy = await monitor.healthy()
        assert healthy is False
        mock_ping.assert_called_once()


@pytest.mark.asyncio
async def test_redis_url_env_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_REDIS_URL", "redis://env-host:6379/0")
    monitor = RedisHealthMonitor()
    assert monitor.redis_url == "redis://env-host:6379/0"


@pytest.mark.asyncio
async def test_explicit_url_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_REDIS_URL", "redis://env-host:6379/0")
    monitor = RedisHealthMonitor("redis://explicit:6379/0")
    assert monitor.redis_url == "redis://explicit:6379/0"


@pytest.mark.asyncio
async def test_ping_success_path() -> None:
    """Mock redis client returns successful ping."""
    monitor = RedisHealthMonitor("redis://localhost:6379/0")

    fake_client = MagicMock()
    fake_client.ping = AsyncMock(return_value=True)
    fake_client.aclose = AsyncMock()

    fake_aioredis = MagicMock()
    fake_aioredis.from_url = MagicMock(return_value=fake_client)

    fake_redis_pkg = MagicMock()
    fake_redis_pkg.asyncio = fake_aioredis

    with patch.dict(
        "sys.modules",
        {"redis": fake_redis_pkg, "redis.asyncio": fake_aioredis},
    ):
        healthy = await monitor.healthy()

    assert healthy is True
    fake_client.ping.assert_called_once()


@pytest.mark.asyncio
async def test_ping_unhealthy_on_redis_missing() -> None:
    """No redis-py installed → unhealthy (graceful fallback)."""
    import builtins

    monitor = RedisHealthMonitor("redis://localhost:6379/0")
    # Force ImportError by making import fail
    real_import = builtins.__import__

    def _fail_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "redis.asyncio":
            raise ImportError("redis not installed")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    with patch("builtins.__import__", side_effect=_fail_import):
        healthy = await monitor.healthy()

    assert healthy is False
