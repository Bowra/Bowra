"""一次请求内「空间授权判定」要读的数据的只读缓存——给一次要判很多空间的列表类请求用（全局搜索）。

为什么需要：全局搜索的会话 / 报告 / 文件三个来源要逐条判空间可读性。判定本身（`build_access_context`）
每判一个空间约 5–7 次开库（用户所属团队、空间所属团队、成员角色、空间状态、团队角色、所在组、策略代次）；
结果分散在几十个空间、又有加宽重取时，一次搜索上千次开库，挤进 4 秒来源超时就整组消失（2026-09-24 评审实测）。

做法：**判定逻辑一行不改**，只换它读数据的地方。`request_with_cached_access(request)` 返回一个请求代理，
它的 ``app.state.user_store`` / ``access_control_store`` 是下面两个缓存层，其它一切照原请求；把代理传给各来源
既有的逐条判定函数即可。缓存层第一次被问到时一次性取全量（这个人的全部有效成员身份、本团队全部空间、他在
团队里的全部组），之后都从内存答；问到的东西不在全量里（例如别的团队的空间）就照常逐条查一次并记住。

⚠ 只在一次请求内有效，不跨请求：成员资格变化在下一次请求立即生效。
⚠ 同一个值被并发问到时只查一次（三个来源是并发跑的）；查询出错时，这次请求里所有等它的判定都得到同一个异常
  （判定出错 ⇒ 来源报错 ⇒ 对外 unavailable，fail-closed，与不缓存时同向）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from bowhead_gateway.access_context import has_workspace_access_source


class _Once:
    """同一个 key 只算一次（并发安全）。"""

    def __init__(self) -> None:
        self._tasks: dict[Any, asyncio.Task[Any]] = {}

    async def get(self, key: Any, compute: Callable[[], Awaitable[Any]]) -> Any:
        task = self._tasks.get(key)
        if task is None:
            task = asyncio.ensure_future(compute())
            # If all waiters timed out/cancelled and it later fails, retrieve its exception to avoid asyncio's `never retrieved` noise.
            task.add_done_callback(lambda done: done.cancelled() or done.exception())
            self._tasks[key] = task
        return await asyncio.shield(task)


class CachedUserStore:
    def __init__(self, store: Any, *, team_id: str | None) -> None:
        self._store = store
        self._team_id = team_id
        self._once = _Once()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    async def get_user_team_id(self, user_id: str) -> str | None:
        return await self._once.get(("user_team", user_id), lambda: self._store.get_user_team_id(user_id))

    async def get_user(self, user_id: str) -> Any:
        return await self._once.get(("user", user_id), lambda: self._store.get_user(user_id))

    async def get_team_role(self, user_id: str, team_id: str) -> str | None:
        return await self._once.get(("team_role", user_id, team_id), lambda: self._store.get_team_role(user_id, team_id))

    async def resolve_workspace_id_for_team(self, team_id: str, workspace_id: str) -> str | None:
        # Single-row SQL: WHERE team_id=? AND (id=? OR datarc_workspace_id=?) LIMIT 1. Answer from the full team set when available:
        # one match -> that row; none -> None (SQL cannot find one either); multiple -> SQL choice is unspecified, delegate to original method.
        if team_id and workspace_id and team_id == self._team_id:
            team_rows = await self._once.get(("team_workspaces", team_id), lambda: self._store.workspaces_for_team(team_id))
            matches = [row["id"] for row in team_rows.values() if workspace_id in (row["id"], row.get("datarc_workspace_id"))]
            if len(matches) <= 1:
                return str(matches[0]) if matches else None
        return await self._once.get(
            ("resolve", team_id, workspace_id),
            lambda: self._store.resolve_workspace_id_for_team(team_id, workspace_id),
        )

    async def get_member_role(self, workspace_id: str, user_id: str) -> str | None:
        roles = await self._once.get(("roles", user_id), lambda: self._store.active_member_roles_for_user(user_id))
        return roles.get(workspace_id)

    async def _workspace_row(self, workspace_id: str) -> dict[str, Any] | None:
        if self._team_id:
            team_rows = await self._once.get(("team_workspaces", self._team_id), lambda: self._store.workspaces_for_team(self._team_id))
            if workspace_id in team_rows:
                return team_rows[workspace_id]
        return await self._once.get(("workspace", workspace_id), lambda: self._store.get_workspace(workspace_id))

    async def get_workspace(self, workspace_id: str) -> dict[str, Any] | None:
        row = await self._workspace_row(workspace_id)
        return dict(row) if row is not None else None  # Copy so caller mutation cannot taint the cache.

    async def get_workspace_team_id(self, workspace_id: str) -> str | None:
        row = await self._workspace_row(workspace_id)
        return row["team_id"] if row is not None else None


class CachedAccessControlStore:
    def __init__(self, store: Any) -> None:
        self._store = store
        self._once = _Once()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    async def principal_group_ids(self, team_id: str, user_id: str, workspace_id: str | None = None, **kwargs: Any) -> tuple[str, ...]:
        if kwargs:  # Caller's transaction connection: retain transaction semantics, bypass cache.
            return await self._store.principal_group_ids(team_id, user_id, workspace_id, **kwargs)
        scopes = await self._once.get(("groups", team_id, user_id), lambda: self._store.principal_group_scopes(team_id, user_id))
        wanted = workspace_id or ""
        # Single-row SQL g.workspace_id='' OR g.workspace_id=?: NULL space in old DB matches neither; exclude here too.
        return tuple(group_id for group_id, group_workspace in scopes if group_workspace is not None and group_workspace in ("", wanted))

    async def get_policy_epoch(self, team_id: str, **kwargs: Any) -> int:
        if kwargs:
            return await self._store.get_policy_epoch(team_id, **kwargs)
        return await self._once.get(("epoch", team_id), lambda: self._store.get_policy_epoch(team_id))


class _State:
    def __init__(self, real: Any, overrides: dict[str, Any]) -> None:
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_overrides", overrides)

    def __getattr__(self, name: str) -> Any:
        overrides = object.__getattribute__(self, "_overrides")
        if name in overrides:
            return overrides[name]
        return getattr(object.__getattribute__(self, "_real"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        # Write through to real app.state, not just this request's proxy (invisible to original state).
        setattr(object.__getattribute__(self, "_real"), name, value)


class _App:
    def __init__(self, real: Any, state: _State) -> None:
        self._real = real
        self.state = state

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _Request:
    def __init__(self, real: Any, app: _App) -> None:
        self._real = real
        self.app = app

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def request_with_cached_access(request: Any, *, team_id: str | None) -> Any:
    """返回判定数据带请求级缓存的请求代理；没有完整的用户库（测试里的精简夹具）就原样返回。"""

    state = request.app.state
    user_store = getattr(state, "user_store", None)
    if isinstance(user_store, CachedUserStore):
        return request  # Already a cached request: one snapshot per request.
    if not has_workspace_access_source(request) or not all(
        callable(getattr(user_store, name, None)) for name in ("active_member_roles_for_user", "workspaces_for_team")
    ):
        return request
    overrides: dict[str, Any] = {"user_store": CachedUserStore(user_store, team_id=team_id)}
    access_store = getattr(state, "access_control_store", None)
    if access_store is not None and callable(getattr(access_store, "principal_group_scopes", None)):
        overrides["access_control_store"] = CachedAccessControlStore(access_store)
    return _Request(request, _App(request.app, _State(state, overrides)))
