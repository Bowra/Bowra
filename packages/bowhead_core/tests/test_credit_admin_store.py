"""Tests for credit_admin_store: in-process LRU cache + idempotency dedup.

V3 admin credits 数据基建. 多 pod 时换 redis 这层 reimplement 不动测试.
"""

from __future__ import annotations

import asyncio

import pytest
from bowhead_core.credit_admin_store import (
    _reset_for_tests,
    cache_get,
    cache_invalidate_team,
    cache_set,
    idempotency_check,
    idempotency_record,
)


@pytest.fixture(autouse=True)
async def _reset() -> None:
    await _reset_for_tests()


async def test_cache_set_get_roundtrip() -> None:
    await cache_set("team_a:summary", {"spend": 100})
    val = await cache_get("team_a:summary")
    assert val == {"spend": 100}


async def test_cache_miss_returns_none() -> None:
    val = await cache_get("missing:key")
    assert val is None


async def test_cache_invalidate_team_removes_prefix() -> None:
    await cache_set("team_a:summary", {"x": 1})
    await cache_set("team_a:users:1:20", {"y": 2})
    await cache_set("team_b:summary", {"z": 3})
    await cache_invalidate_team("team_a")
    assert await cache_get("team_a:summary") is None
    assert await cache_get("team_a:users:1:20") is None
    # Other teams remain unchanged.
    assert await cache_get("team_b:summary") == {"z": 3}


async def test_idempotency_check_miss_then_record() -> None:
    assert await idempotency_check("key1") is None
    await idempotency_record("key1", {"new_balance": 5000})
    assert await idempotency_check("key1") == {"new_balance": 5000}


async def test_idempotency_replay_returns_same() -> None:
    """同 idempotency_key 调 2 次, 第二次返第一次的 result. (caller 不该再做副作用.)"""
    await idempotency_record("k", {"id": "led_001", "amount": 5000})
    a = await idempotency_check("k")
    b = await idempotency_check("k")
    assert a == b == {"id": "led_001", "amount": 5000}


async def test_concurrent_cache_set_safe() -> None:
    """多 task 并发 cache_set 同 key — 末写赢, 不丢锁."""

    async def writer(i: int) -> None:
        await cache_set("team_a:k", {"i": i})

    await asyncio.gather(*[writer(i) for i in range(50)])
    val = await cache_get("team_a:k")
    assert val is not None
    assert "i" in val and 0 <= val["i"] < 50


async def test_cache_ttl_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    """1min TTL 到期后下次 cache_get 返 None."""
    import bowhead_core.credit_admin_store as mod

    # Write one entry.
    await cache_set("team_a:k", "v")
    assert await cache_get("team_a:k") == "v"

    # Simulate 61s later; monkeypatch _TTL_SEC for a fast test.
    # Directly mutate _read_cache's timestamp to make the entry appear written 60.5s ago.
    async with mod._lock:
        ts, val = mod._read_cache["team_a:k"]
        mod._read_cache["team_a:k"] = (ts - 61, val)

    assert await cache_get("team_a:k") is None
