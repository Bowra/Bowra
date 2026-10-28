"""cca block ④d — per-team tool-auth cache (tech-design §6.1).

Runtime cache that turns an team tool source's stored auth (Fernet ciphertext in
``team_tool_source.auth_ref``) into outbound HTTP auth headers, mirroring
``TeamRouterRegistry`` (LRU + idle-evict + invalidate). The ④c executor's
source-config resolver calls ``get_headers(team_id, source_id)`` to inject auth on
each outbound call; decrypted credentials live only in this in-memory cache + the
request header — never in the store as plaintext (F14 guard at write time), nor in
logs / ctx / LLM.

The cache key is ``(team_id, source_id)``. ``invalidate(team_id)`` drops a whole team
(auth rotation / team delete); ``invalidate_source`` drops one source (auth re-fill
/ delete). Callers (⑤ endpoints) invalidate on any auth/source mutation.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_IDLE_TIMEOUT_SEC = 1800  # 30 min — drop idle team/source auth from memory
_MAX_CACHE = 500  # (team, source) entries; LRU-evict over cap
_REAP_INTERVAL_SEC = 300  # 5 min idle sweep


class _AuthStore(Protocol):
    """The two store reads ToolAuthRegistry needs (UserStore satisfies this)."""

    async def get_team_tool_source(self, team_id: str, source_id: str) -> Any: ...
    async def get_team_tool_auth_decrypted(self, team_id: str, source_id: str) -> dict[str, Any]: ...


def _build_auth_headers(auth_type: str, creds: dict[str, Any]) -> dict[str, str]:
    """Build outbound HTTP auth headers from a source's ``auth_type`` + decrypted creds.

    Incomplete / unknown auth → ``{}`` (the upstream then 401s, classified as
    ``upstream_auth`` by ④c — we never raise here, so a malformed cred can't leak via
    an exception). header-based only in P1 (no auth-in-query → avoids leaking creds
    into URLs / SSRF logs).
    """
    if auth_type == "bearer":
        token = creds.get("value") or creds.get("token")
        return {"Authorization": f"Bearer {token}"} if token else {}
    if auth_type == "api_key":
        value = creds.get("value")
        if not value:
            return {}
        header_name = str(creds.get("header_name") or "X-API-Key")
        prefix = creds.get("prefix") or ""
        return {header_name: f"{prefix}{value}"}
    if auth_type == "basic":
        user, pw = creds.get("username"), creds.get("password")
        if user is None or pw is None:
            return {}
        token = base64.b64encode(f"{user}:{pw}".encode()).decode()
        return {"Authorization": f"Basic {token}"}
    return {}  # "none" / unknown


class _AuthEntry:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers
        self.last_used: float = time.time()


class ToolAuthRegistry:
    """Per-(team, source) decrypted-auth-header cache. Mirror of TeamRouterRegistry."""

    def __init__(self, store: _AuthStore) -> None:
        self._store = store
        self._cache: dict[tuple[str, str], _AuthEntry] = {}
        self._lock = asyncio.Lock()
        # tools sprint WP1 (review P2-1): monotonic generation, bumped by every invalidate under the
        # lock. get_headers snapshots it before its lock-free store read and re-checks before the
        # write-back — so an invalidate that races a mid-flight (cache-miss) decrypt can't poison the
        # cache with stale/rotated-away creds (which would survive until idle-evict). Closes the
        # concurrency residual of the PATCH-invalidate credential-rotation fix.
        self._gen = 0
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
                await asyncio.sleep(_REAP_INTERVAL_SEC)
                now = time.time()
                async with self._lock:
                    for key, e in list(self._cache.items()):
                        if now - e.last_used > _IDLE_TIMEOUT_SEC:
                            del self._cache[key]
                            logger.info("tool_auth.idle_evict team=%s src=%s", key[0], key[1])
        except asyncio.CancelledError:
            pass

    def invalidate(self, team_id: str) -> None:
        """Drop every cached source for an team (auth rotation / team delete)."""
        self._gen += 1  # review P2-1: fence a racing mid-flight get_headers write-back
        for key in [k for k in self._cache if k[0] == team_id]:
            self._cache.pop(key, None)

    def invalidate_source(self, team_id: str, source_id: str) -> None:
        """Drop one source's cached auth (auth re-fill / source delete)."""
        self._gen += 1  # review P2-1: fence a racing mid-flight get_headers write-back
        self._cache.pop((team_id, source_id), None)

    async def get_headers(self, team_id: str, source_id: str) -> dict[str, str]:
        """Return outbound auth headers for one team source (cached; copy returned).

        fail-closed: a falsy team_id, an unknown source, or auth_type 'none' → ``{}``
        (no auth injected). The decrypt is an team-bound store read (cross-team reads
        return nothing).
        """
        if not team_id or not source_id:
            return {}
        key = (team_id, source_id)
        async with self._lock:
            e = self._cache.get(key)
            if e is not None:
                e.last_used = time.time()
                return dict(e.headers)  # copy — never hand out the cached mutable
            gen_at_read = self._gen  # review P2-1: snapshot before the lock-free store read

        # build outside the lock (decrypt does DB I/O; don't serialize all teams).
        src = await self._store.get_team_tool_source(team_id, source_id)
        if src is None:
            return {}
        auth_type = getattr(src, "auth_type", "none")
        if auth_type == "none":
            headers: dict[str, str] = {}
        else:
            creds = await self._store.get_team_tool_auth_decrypted(team_id, source_id)
            headers = _build_auth_headers(auth_type, creds)

        async with self._lock:
            # review P2-1: if an invalidate raced our store read, the headers we just read may be
            # stale (rotated-away) — return them for THIS call only, but do NOT cache them (that
            # would survive until idle-evict). The next call re-reads fresh.
            if self._gen != gen_at_read:
                return dict(headers)
            if len(self._cache) >= _MAX_CACHE:
                oldest = min(self._cache.items(), key=lambda kv: kv[1].last_used)
                del self._cache[oldest[0]]
            self._cache[key] = _AuthEntry(headers)
            return dict(headers)
