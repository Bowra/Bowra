"""In-process TTL cache of a Datarc workspace's name, measures and dimensions.

Used only to localize display labels (step previews, clarification option labels) for the
UI. Model-facing schema reaches the model through the BI tools, never through this cache.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bowhead_bi_bridge import DatarcClient

logger = logging.getLogger(__name__)


@dataclass
class _CachedSchema:
    workspace_name: str
    workspace_description: str
    measures: list[Any]  # list[MeasureMeta]
    dimensions: list[Any]  # list[DimensionMeta]
    cached_at: float


class DatarcSchemaCache:
    """datarc_workspace_id → (ws meta + measures + dims) 的 in-memory TTL cache.

    单进程内共享; 同一 workspace 在 TTL 内多次取 schema 走 cache, 不再调 datarc.
    并发安全: 同 workspace 的并发 fetch 通过 asyncio.Lock 串行化, 防 thundering herd.

    典型用法:
      cache = DatarcSchemaCache(ttl_sec=300)
      schema = await get_schema_with_cache(datarc_client=client, datarc_workspace_id=ws, schema_cache=cache)

    Eviction: 当前是 size-bounded LRU (max_entries 默认 32) + per-entry TTL.
    """

    def __init__(self, *, ttl_sec: float = 300.0, max_entries: int = 32) -> None:
        self.ttl_sec = ttl_sec
        self.max_entries = max_entries
        # P2 #17: OrderedDict provides O(1) LRU eviction
        self._store: OrderedDict[str, _CachedSchema] = OrderedDict()
        # P1 #7: evict locks alongside store entries to prevent unbounded growth
        self._locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        # Cache stats (for metrics emission)
        self.hits = 0
        self.misses = 0

    def _is_fresh(self, entry: _CachedSchema) -> bool:
        return (time.monotonic() - entry.cached_at) < self.ttl_sec

    def get(self, datarc_workspace_id: str, *, count_stats: bool = True) -> _CachedSchema | None:
        e = self._store.get(datarc_workspace_id)
        if e and self._is_fresh(e):
            # LRU: move-to-end marks most recent use
            self._store.move_to_end(datarc_workspace_id)
            if count_stats:
                self.hits += 1
            return e
        if count_stats:
            self.misses += 1
        if e:
            # Remove expired entry.
            self._store.pop(datarc_workspace_id, None)
        return None

    def _evict_if_full(self) -> None:
        # OrderedDict popitem(last=False) = O(1) eviction of earliest entry
        while len(self._store) >= self.max_entries:
            evicted_key, _ = self._store.popitem(last=False)
            # Remove corresponding lock too, preventing leaks.
            self._locks.pop(evicted_key, None)

    def put(self, datarc_workspace_id: str, entry: _CachedSchema) -> None:
        self._evict_if_full()
        self._store[datarc_workspace_id] = entry
        self._store.move_to_end(datarc_workspace_id)  # New writes also count as recent use.

    def lock_for(self, datarc_workspace_id: str) -> asyncio.Lock:
        lk = self._locks.get(datarc_workspace_id)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[datarc_workspace_id] = lk
        return lk

    def invalidate(self, datarc_workspace_id: str | None = None) -> None:
        """datarc_workspace_id None = 全清; 否则清单一. lock 同步清."""
        if datarc_workspace_id is None:
            self._store.clear()
            self._locks.clear()
        else:
            self._store.pop(datarc_workspace_id, None)
            self._locks.pop(datarc_workspace_id, None)

    def stats(self) -> dict[str, int]:
        return {
            "size": len(self._store),
            "hits": self.hits,
            "misses": self.misses,
        }

    def scoped(self, source_key: str) -> DatarcSchemaCache:
        """Share the bounded cache while isolating an accepted source and member."""
        return _ScopedDatarcSchemaCache(self, source_key)


class _ScopedDatarcSchemaCache(DatarcSchemaCache):
    def __init__(self, cache: DatarcSchemaCache, source_key: str) -> None:
        self._base = cache._base if isinstance(cache, _ScopedDatarcSchemaCache) else cache
        self._prefix = source_key + ":"

    def get(self, datarc_workspace_id: str, *, count_stats: bool = True) -> _CachedSchema | None:
        return self._base.get(self._prefix + datarc_workspace_id, count_stats=count_stats)

    def put(self, datarc_workspace_id: str, entry: _CachedSchema) -> None:
        self._base.put(self._prefix + datarc_workspace_id, entry)

    def lock_for(self, datarc_workspace_id: str) -> asyncio.Lock:
        return self._base.lock_for(self._prefix + datarc_workspace_id)

    def invalidate(self, datarc_workspace_id: str | None = None) -> None:
        if datarc_workspace_id is not None:
            self._base.invalidate(self._prefix + datarc_workspace_id)
            return
        for key in set(self._base._store) | set(self._base._locks):
            if key.startswith(self._prefix):
                self._base.invalidate(key)

    def stats(self) -> dict[str, int]:
        return self._base.stats()


async def _fetch_schema_uncached(
    *,
    datarc_client: DatarcClient,
    datarc_workspace_id: str,
) -> _CachedSchema:
    """实拉 workspace meta + measures + dims (并行)."""

    async def _safe_get_ws() -> tuple[str, str]:
        try:
            ws = await datarc_client.get_workspace(datarc_workspace_id)
            return ws.name or "", ws.desc or ""
        except Exception as e:
            logger.debug("datarc_schema_cache.get_workspace_failed ws=%s err=%s", datarc_workspace_id, e)
            return "", ""

    async def _safe_list_measures() -> list[Any]:
        try:
            return list(await datarc_client.list_measures(datarc_workspace_id))
        except Exception as e:
            logger.debug("datarc_schema_cache.list_measures_failed ws=%s err=%s", datarc_workspace_id, e)
            return []

    async def _safe_list_dims() -> list[Any]:
        try:
            return list(await datarc_client.list_dimensions(datarc_workspace_id))
        except Exception as e:
            logger.debug("datarc_schema_cache.list_dimensions_failed ws=%s err=%s", datarc_workspace_id, e)
            return []

    (ws_name, ws_desc), measures, dims = await asyncio.gather(
        _safe_get_ws(),
        _safe_list_measures(),
        _safe_list_dims(),
    )
    return _CachedSchema(
        workspace_name=ws_name,
        workspace_description=ws_desc,
        measures=measures,
        dimensions=dims,
        cached_at=time.monotonic(),
    )


async def get_schema_with_cache(
    *,
    datarc_client: DatarcClient,
    datarc_workspace_id: str,
    schema_cache: DatarcSchemaCache | None,
) -> _CachedSchema:
    """走 cache 拉 schema. 命中 = 0 datarc 调用; 未命中 = 3 并行调用 + cache.

    并发同 workspace 用 asyncio.Lock 串行化, 防 thundering herd.
    """
    if schema_cache is None:
        # No cache: fetch directly.
        return await _fetch_schema_uncached(
            datarc_client=datarc_client,
            datarc_workspace_id=datarc_workspace_id,
        )

    cached = schema_cache.get(datarc_workspace_id)
    if cached is not None:
        # P1 #14 metrics
        try:
            from bowhead_core import metrics as _m

            _m.inc("bowhead_schema_cache_total", labels={"result": "hit"})
        except Exception:  # noqa: S110
            pass
        return cached

    lock = schema_cache.lock_for(datarc_workspace_id)
    async with lock:
        # Double-check inside lock (another coroutine may have filled it); do not count another miss.
        cached = schema_cache.get(datarc_workspace_id, count_stats=False)
        if cached is not None:
            return cached
        fresh = await _fetch_schema_uncached(
            datarc_client=datarc_client,
            datarc_workspace_id=datarc_workspace_id,
        )
        schema_cache.put(datarc_workspace_id, fresh)
        try:
            from bowhead_core import metrics as _m

            _m.inc("bowhead_schema_cache_total", labels={"result": "miss"})
        except Exception:  # noqa: S110
            pass
        return fresh


def remember_schema(
    schema_cache: DatarcSchemaCache | None,
    datarc_workspace_id: str,
    *,
    workspace_name: str,
    measures: list[Any],
    dimensions: list[Any],
) -> None:
    """Store a schema a BI tool already fetched, so UI labels need no second Datarc call.

    Only complete listings are stored: an empty side is usually a transient Datarc hiccup,
    and caching it would hide labels for the whole TTL.
    """
    if schema_cache is None or not datarc_workspace_id or not measures or not dimensions:
        return
    schema_cache.put(
        datarc_workspace_id,
        _CachedSchema(
            workspace_name=workspace_name,
            workspace_description="",
            measures=list(measures),
            dimensions=list(dimensions),
            cached_at=time.monotonic(),
        ),
    )
