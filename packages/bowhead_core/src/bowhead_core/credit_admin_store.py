"""Admin credits 缓存 + idempotency 去重 (in-process).

V3 单 pod 部署: in-memory dict + asyncio.Lock 即够. 多 pod 时换 redis,
接口签名保持兼容 (cache_get/set/invalidate_team + idempotency_check/record).

设计见 docs/V3-admin-credits-tech-design.md §1.2.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

# Read-cache TTL: 1 min; repeated admin refreshes avoid SQLite; <1min staleness is acceptable to admins.
_TTL_SEC = 60

# Idempotency dedup TTL: 24h; replaying an accidental grant click with the same key within 24h returns the same result.
_DEDUP_TTL_SEC = 24 * 3600

# (timestamp, value) tuple. timestamp is used for TTL garbage collection.
_read_cache: dict[str, tuple[float, Any]] = {}
_idempotency: dict[str, tuple[float, Any]] = {}

# One simple lock: lock all in-process data-structure mutations to prevent interleaved reads/writes.
# (Cache reads are not a hot path; with 1min cache hits + Python GIL, lock overhead is negligible.)
_lock = asyncio.Lock()


def _gc(d: dict[str, tuple[float, Any]], ttl: int) -> None:
    """逐项删过期. 调用方负责加锁."""
    now = time.time()
    expired = [k for k, (ts, _) in d.items() if now - ts > ttl]
    for k in expired:
        d.pop(k, None)


# ─── read cache ──────────────────────────────────


async def cache_get(key: str) -> Any | None:
    """缓存命中返值, miss 或过期返 None."""
    async with _lock:
        _gc(_read_cache, _TTL_SEC)
        ent = _read_cache.get(key)
        if not ent:
            return None
        ts, val = ent
        return val if time.time() - ts <= _TTL_SEC else None


async def cache_set(key: str, val: Any) -> None:
    async with _lock:
        _read_cache[key] = (time.time(), val)


async def cache_invalidate_team(team_id: str) -> None:
    """team 内任 write 后调. 清掉所有 prefix=team_id 的 cache entry.

    cache key 约定: f"{team_id}:..."  e.g. "team_abc:summary" / "team_abc:users:1:20:..."
    """
    async with _lock:
        prefix = f"{team_id}:"
        keys = [k for k in _read_cache if k.startswith(prefix)]
        for k in keys:
            _read_cache.pop(k, None)


# ─── idempotency dedup ───────────────────────────


async def idempotency_check(key: str) -> Any | None:
    """已有 key → 返之前 result (重放). 没有 → None (caller 接着做并 record)."""
    async with _lock:
        _gc(_idempotency, _DEDUP_TTL_SEC)
        ent = _idempotency.get(key)
        return ent[1] if ent else None


async def idempotency_record(key: str, result: Any) -> None:
    async with _lock:
        _idempotency[key] = (time.time(), result)


# ─── Test helper (unit-test reset) ────────────────────────────────────────────


async def _reset_for_tests() -> None:
    """单测专用: 清空两个 dict. prod 不调."""
    async with _lock:
        _read_cache.clear()
        _idempotency.clear()
