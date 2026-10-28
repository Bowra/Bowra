"""知识库引用的「看的人读权限」（知识库回答引用技术设计 §3）。

与知识库页面同一判定：personal 看上传者、team 看团队、space 看**文件所在空间**的成员资格
（走 `build_access_context`，与 `/kb/files/{id}/chunks` 带该空间头时同一闸——可查看的引用
前端据 `kb_space_id` 去取片段必然取得到）；资源授权按 `BOWHEAD_RESOURCE_GRANTS_ENFORCE`
与知识库路由同口径（只拒不记——投影不是一次访问尝试）。

看的人由 `_current_user` 留在 `request.state.bowhead_user`；取不到 ⇒ 不注入判定 ⇒ 受限。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

from bowhead_core.access_control import resource_grants_enforce_enabled
from bowhead_core.source_attribution import KbFileAccess
from bowhead_core.space_governance import SpaceOperation

from bowhead_gateway.access_context import access_context_to_dict, build_access_context_from_request

logger = logging.getLogger(__name__)

_MISSING = KbFileAccess(state="missing")
_FORBIDDEN = KbFileAccess(state="forbidden")


async def _grant_allows(request: Any, access_ctx: Any, resource_kind: str, resource_id: str) -> bool:
    access_store = getattr(request.app.state, "access_control_store", None)
    if access_store is None or access_ctx is None:
        return True
    decision = await access_store.explain_decision(access_context_to_dict(access_ctx), resource_kind, resource_id, "read")
    return bool(decision.get("allowed")) or not resource_grants_enforce_enabled()


async def _judge(request: Any, user: Any, files_store: Any, file_id: str) -> KbFileAccess:
    file = await files_store.get(file_id)
    if file is None or getattr(file, "soft_deleted_at", None):
        return _MISSING
    scope = str(getattr(file, "scope", "") or "")
    scope_id = str(getattr(file, "scope_id", "") or "")
    if scope == "personal":
        # Personal-file ownership follows scope_id (same as KbPermission.owner_of). After an admin moves someone else's file
        # into their personal scope, uploaded_by remains the original uploader; using it restricts the new owner and exposes it to the old uploader.
        if scope_id != user.id:
            return _FORBIDDEN
        space_id = None
        workspace_for_grants = ""
    elif scope in ("space", "workspace"):  # Accept both read spellings (same as KbPermission).
        space_id = scope_id
        workspace_for_grants = scope_id
    elif scope == "team":
        if not scope_id or scope_id != getattr(user, "team_id", None):
            return _FORBIDDEN
        space_id = None
        workspace_for_grants = ""
    else:
        return _FORBIDDEN
    try:
        access_ctx = await build_access_context_from_request(
            request,
            user=user,
            workspace_id=workspace_for_grants,
            allow_missing_workspace=not workspace_for_grants,
            operation=SpaceOperation.READ_HISTORY,
        )
    except Exception:
        # Nonmember / archived space / unavailable authorization service: always restricted.
        return _FORBIDDEN
    if space_id and not await _grant_allows(request, access_ctx, "kb_scope", f"workspace:{space_id}"):
        return _FORBIDDEN
    if scope == "team" and not await _grant_allows(request, access_ctx, "kb_scope", f"team:{scope_id}"):
        return _FORBIDDEN
    if not await _grant_allows(request, access_ctx, "kb_file", file_id):
        return _FORBIDDEN
    return KbFileAccess(
        state="readable",
        file_name=str(getattr(file, "filename", "") or "") or None,
        scope="space" if space_id else scope,
        space_id=space_id,
    )


def kb_file_access_for_request(request: Any) -> Callable[[str], Awaitable[KbFileAccess]] | None:
    """绑定本请求看的人的判定函数；缺知识库库句柄时返回 None（⇒ 受限）。

    看的人优先取 `_current_user` 留下的 `request.state.bowhead_user`；画布等旧路由不走它
    （`resolve_user_id_sync` 同步取 JWT），此时首次判定时按同一身份现取用户（116 1.3.53 实测：
    画布详情里文件所有者的知识库引用被判受限）。取不到用户 ⇒ 每次都判受限。
    """
    files_store = getattr(request.app.state, "kb_files_store", None)
    if files_store is None:
        return None
    user = getattr(getattr(request, "state", None), "bowhead_user", None)
    if user is not None:
        return _bind(request, user, files_store)
    resolved: list[Any] = []

    async def check(file_id: str) -> KbFileAccess:
        if not resolved:
            resolved.append(await _resolve_request_user(request))
        viewer = resolved[0]
        if viewer is None:
            return _FORBIDDEN
        return await _judge(request, viewer, files_store, file_id)

    return check


async def _resolve_request_user(request: Any) -> Any | None:
    try:
        from bowhead_gateway.routes.auth import resolve_user_id_sync

        user_id = resolve_user_id_sync(request)
        user_store = getattr(request.app.state, "user_store", None)
        user = await user_store.get_user(user_id) if user_store is not None else None
    except Exception:
        return None
    if user is None or getattr(user, "status", "active") != "active":
        return None
    return user


def kb_file_access_for_user(app: Any, user: Any) -> Callable[[str], Awaitable[KbFileAccess]] | None:
    """没有 HTTP 请求的上下文（飞书 / Teams 推送）：以指定用户为看的人，同一套判定。"""
    files_store = getattr(app.state, "kb_files_store", None)
    if user is None or files_store is None:
        return None
    request = SimpleNamespace(app=app, state=SimpleNamespace(bowhead_user=user), headers={}, url=SimpleNamespace(path="/im"))
    return _bind(request, user, files_store)


def _bind(request: Any, user: Any, files_store: Any) -> Callable[[str], Awaitable[KbFileAccess]]:

    async def check(file_id: str) -> KbFileAccess:
        return await _judge(request, user, files_store, file_id)

    return check
