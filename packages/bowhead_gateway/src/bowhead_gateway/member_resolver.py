"""把 (account identity, datarc_workspace_id) 解成 datarc memberUid，带进程内缓存.

datarc 的 memberUid 是 workspace-scoped（同一 account 在不同 workspace 对应不同
memberUid）；切 workspace 必须换 memberUid，否则 datarc 返
"Member matching query does not exist".

解析流程:
    缓存命中 → O(1) 返
    未命中   → POST /openapi/v1/workspaces/{ws}/members/fetch/ {username: identity}
              数据 data.uid 即 memberUid；500 错抛 BI*Error；400 软失败返 None，
              本层包装成 MemberNotFoundError 交调用方把上下文展给用户.

线程安全：`asyncio.Lock` 保证同一 (identity, workspace) 并发请求只打一次下游.
缓存 TTL：默认无 —— member-workspace 关系变更频率极低，进程重启自然失效够用.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bowhead_bi_bridge import DatarcClient


class MemberNotFoundError(Exception):
    """identity 不是指定 workspace 的成员；提示切账号或加入 workspace."""

    def __init__(self, identity: str, datarc_workspace_id: str) -> None:
        super().__init__(
            f"datarc 账号 {identity!r} 不是北极九章工作区 {datarc_workspace_id!r} 的成员 "
            "(POST /members/fetch/ 返未命中). "
            "解决: 使用属于该北极九章工作区的已验证 Bowhead 账号，"
            "或请管理员把当前账号加入该北极九章工作区."
        )
        self.identity = identity
        self.datarc_workspace_id = datarc_workspace_id


class MemberUidResolver:
    """resolve(identity, datarc_workspace_id) → memberUid，带并发 dedupe + 缓存."""

    def __init__(self, datarc: DatarcClient | None = None) -> None:
        self._datarc = datarc
        self._cache: dict[tuple[int, str, str], str] = {}
        self._locks: dict[tuple[int, str, str], asyncio.Lock] = {}

    def _lock_for(self, key: tuple[int, str, str]) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def resolve(self, identity: str, datarc_workspace_id: str, *, datarc: DatarcClient | None = None, refresh: bool = False) -> str:
        """返 memberUid；未命中抛 :class:`MemberNotFoundError`；其他下游错原样抛."""
        client = datarc or self._datarc
        if client is None:
            raise RuntimeError("team Datarc client is not available")
        key = (id(client), identity, str(datarc_workspace_id))
        hit = self._cache.get(key)
        if hit is not None and not refresh:
            return hit
        async with self._lock_for(key):
            # Double-check: the first concurrent lock holder already filled it.
            hit = self._cache.get(key)
            if hit is not None and not refresh:
                return hit
            if refresh:
                self._cache.pop(key, None)
            data = await client.fetch_workspace_member(
                datarc_workspace_id=str(datarc_workspace_id),
                username=identity,
            )
            if not data or not data.get("uid"):
                raise MemberNotFoundError(identity, str(datarc_workspace_id))
            member_uid = str(data["uid"])
            self._cache[key] = member_uid
            return member_uid

    def invalidate(self, identity: str | None = None, datarc_workspace_id: str | None = None) -> None:
        """清缓存. 参数都 None → 清全部；其一 None 则按另一个过滤；都给则清一条."""
        if identity is None and datarc_workspace_id is None:
            self._cache.clear()
            return
        for key in list(self._cache):
            _client_id, k_id, k_ws = key
            if identity is not None and k_id != identity:
                continue
            if datarc_workspace_id is not None and k_ws != str(datarc_workspace_id):
                continue
            self._cache.pop(key, None)

    def snapshot(self) -> dict[tuple[int, str, str], str]:
        """debug 用：返缓存副本."""
        return dict(self._cache)
