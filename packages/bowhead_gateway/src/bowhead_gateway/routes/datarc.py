"""Datarc passthrough 端点（W2 免登录鉴权）.

POST /api/v1/datarc/authorize
  Body: {"card_url": "https://datarc.cn/#/..."}
  Resp: {"authorized_url": "..."} — 带免登录 token 的 URL（iframe 可直接嵌入）

当前实现：根据前端传入的 card_url，调 datarc 的 /login/auth_data/ 拿 token，
拼接到 URL 末尾（`&auth=<token>` 或 datarc 侧要求的 query 形式）。

⚠ datarc 的 /login/auth_data/ 具体 schema 本 POC 未完全 spec 化，
这里做 safe fallback：若 datarc 无 /login/auth_data/，前端不加 token 裸嵌（用户会看到 datarc 登录墙）。
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from bowhead_core.scrub import scrub
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from bowhead_gateway.dtos import AuthorizeResponseDTO, GuideQuestionsDTO
from bowhead_gateway.routes.auth import _current_user

if TYPE_CHECKING:
    from bowhead_core.user_store import User, UserStore

    from bowhead_bi_bridge import DatarcClient

router = APIRouter()


async def _datarc(request: Request, user: User) -> DatarcClient:
    from bowhead_gateway.datarc_context import resolve_team_datarc_client

    client = await resolve_team_datarc_client(request, user.team_id)
    if client is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "team datarc connection not configured")
    return client


def _user_store(request: Request) -> UserStore:
    store: UserStore | None = getattr(request.app.state, "user_store", None)
    if store is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "user_store not configured")
    return store


async def _resolve_user_datarc_workspace(
    *,
    user: User,
    store: UserStore,
    workspace_id: str,
) -> tuple[str, str | None]:
    rows = await store.list_workspaces_for_user(user.id)
    for row in rows:
        canonical_ws_id = str(row.get("workspace_id") or "")
        datarc_ws_id = row.get("datarc_workspace_id")
        datarc_ws_id = str(datarc_ws_id) if datarc_ws_id else None
        if workspace_id == canonical_ws_id or (datarc_ws_id and workspace_id == datarc_ws_id):
            return canonical_ws_id, datarc_ws_id
    raise HTTPException(status.HTTP_404_NOT_FOUND, "workspace not found")


class AuthorizeRequest(BaseModel):
    card_url: str


# authData cache (identity+project_id -> authData): Datarc regeneration invalidates the previous value.
# Reuse within a short TTL to prevent one user's multiple iframes from invalidating each other.
_AUTHDATA_CACHE: dict[tuple[str, str, str], tuple[float, str]] = {}
_AUTHDATA_TTL_SEC = 60 * 30  # 30 min


def _identity_for_iframe(_request: Request, user: User) -> str:
    """iframe 免登录用的 identity（datarc 账号的 登录名/手机/邮箱）.

    Always use the authenticated Bowhead user's email. A deployment-wide
    development identity would grant every caller the same Datarc access.
    """
    return user.email


def _append_auth_data_to_url(card_url: str, auth_data: str) -> str:
    """把 authData 塞进 datarc URL 的 hash fragment.

    datarc 的 URL 形态是 `https://demo.datarc.cn/#/discover/?key=...&projectId=1`.
    免登录规则：`#/...?...&authData=<encoded>`（authData 必须在 hash 里，不是 query）.
    `#` 必须保留. auth_data 已由 datarc urlencoded.
    """
    if "#" not in card_url:
        # No hash -> nonstandard Datarc URL; append as query fallback (many old cards lack a hash).
        sep = "&" if "?" in card_url else "?"
        return f"{card_url}{sep}authData={auth_data}"
    base, _, hash_part = card_url.partition("#")
    if "?" in hash_part:
        return f"{base}#{hash_part}&authData={auth_data}"
    # No query in hash (e.g. #/discover/) -> append ?.
    # Strip trailing slash first? Keeping unchanged is safer: #/discover/?authData=...
    if hash_part.endswith("/"):
        return f"{base}#{hash_part}?authData={auth_data}"
    return f"{base}#{hash_part}?authData={auth_data}"


def _extract_project_id(card_url: str) -> str | None:
    """从 card_url 里抓 `projectId=X` —— 生成 authData 时要带以切到目标 workspace."""
    import re

    m = re.search(r"projectId=(\d+)", card_url)
    return m.group(1) if m else None


@router.post("/authorize", response_model=AuthorizeResponseDTO)
async def authorize(
    body: AuthorizeRequest,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_user_store),
) -> AuthorizeResponseDTO:
    """免登录 URL 生成 —— 遵循 datarc 单点登录流程:

    1. 从 card_url 提取 projectId（可选）
    2. 调 `POST /openapi/v1/auth/auth-data/ {identity, project_id, urlencode:true}` 取 authData
    3. 拼到 card_url 的 hash 后面 `#/?authData=<encoded>`
    4. TTL 缓存 authData（30 min），同用户多卡片场景复用（datarc 重生成会废旧的）
    """
    import time

    import structlog

    log = structlog.get_logger()
    datarc = await _datarc(request, user)
    project_id = _extract_project_id(body.card_url)
    if not project_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "datarc projectId required")
    _canonical_ws_id, datarc_ws_id = await _resolve_user_datarc_workspace(
        user=user,
        store=store,
        workspace_id=project_id,
    )
    if datarc_ws_id != project_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "datarc workspace not available")
    identity = _identity_for_iframe(request, user)
    cache_key = (user.id, identity, project_id)

    # Check cache first.
    auth_data: str | None = None
    cached = _AUTHDATA_CACHE.get(cache_key)
    if cached and time.time() - cached[0] < _AUTHDATA_TTL_SEC:
        auth_data = cached[1]

    # Cache miss -> call Datarc.
    if auth_data is None:
        payload: dict[str, Any] = {"identity": identity, "urlencode": True}
        if project_id:
            with contextlib.suppress(ValueError):
                payload["project_id"] = int(project_id)
        try:
            resp = await datarc._request(
                "POST",
                "openapi/v1/auth/auth-data/",
                json_body=payload,
            )
            if isinstance(resp.data, dict):
                auth_data = resp.data.get("authData")
            if auth_data:
                # Validate: identity outside the project makes Datarc **silently fall back to the user's default workspace**.
                # authData's project.id then differs from our URL's projectId, displaying the wrong workspace's data
                # in the iframe. Explicitly validate and reject rather than show wrong data.
                try:
                    import json as _json
                    import urllib.parse as _url

                    raw = _url.unquote(auth_data)
                    obj = _json.loads(raw)
                    got_pid = obj.get("project", {}).get("id") if isinstance(obj.get("project"), dict) else None
                    if project_id and got_pid is not None and str(got_pid) != str(project_id):
                        log.error(
                            "datarc.authorize.workspace_mismatch",
                            identity=identity,
                            requested_project=project_id,
                            got_project=got_pid,
                            hint=(
                                f"identity 不是 project {project_id} 的成员；datarc 悄悄换成了 project {got_pid}. "
                                f"换成真实属于 workspace {project_id} 的用户登录名/手机/邮箱"
                            ),
                        )
                        auth_data = None  # Reject; use fallback.
                    else:
                        _AUTHDATA_CACHE[cache_key] = (time.time(), auth_data)
                except (ValueError, TypeError) as parse_err:
                    # Parse failure: conservatively accept without blocking, but log.
                    log.warning("datarc.authorize.authdata_parse_failed", err=str(parse_err))
                    _AUTHDATA_CACHE[cache_key] = (time.time(), auth_data)
            else:
                log.warning("datarc.authorize.no_auth_data", resp=scrub(str(resp.data)[:200]))
        except Exception as e:
            log.warning("datarc.authorize.auth_data_failed", error=scrub(str(e)))

    if auth_data:
        authorized = _append_auth_data_to_url(body.card_url, auth_data)
        return AuthorizeResponseDTO(
            authorized_url=authorized,
            strategy="sso_auth_data",
            identity=identity,
        )

    # Fallback: no authData -> original URL + autoLogin=1 (login wall, but navigation remains possible).
    sep = "&" if "?" in body.card_url else "?"
    return AuthorizeResponseDTO(
        authorized_url=f"{body.card_url}{sep}autoLogin=1",
        strategy="w1_autologin_fallback",
        identity=None,
    )


@router.get("/guide-questions", response_model=GuideQuestionsDTO)
async def guide_questions(
    request: Request,
    workspace_id: str | None = None,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_user_store),
) -> GuideQuestionsDTO:
    """Read optional guide questions for the authenticated user's saved data context."""
    from bowhead_core.datarc_selection import DatarcPreferenceScope
    from bowhead_core.space_governance import SpaceOperation

    from bowhead_gateway.access_context import AccessContextError, build_access_context
    from bowhead_gateway.datarc_context import resolve_team_datarc_binding

    canonical_ws_id = workspace_id or ""
    if not canonical_ws_id:
        return GuideQuestionsDTO(workspace_id=canonical_ws_id, items=[])
    try:
        access = await build_access_context(
            user_store=store,
            access_store=getattr(request.app.state, "access_control_store", None),
            user_id=user.id,
            workspace_id=canonical_ws_id,
            operation=SpaceOperation.READ_HISTORY,
        )
    except AccessContextError as error:
        raise HTTPException(status_code=error.status_code, detail={"code": error.code}) from error
    binding = await resolve_team_datarc_binding(request, access.team_id)
    umem = getattr(request.app.state, "user_memory_store", None)
    chosen = (
        await umem.get_datarc_workspace(user.id, DatarcPreferenceScope(binding.team_id, binding.connection_id))
        if umem is not None and binding is not None
        else None
    )
    datarc_ws_id = str((chosen or {}).get("id") or "")
    datarc = binding.client if binding else None
    if datarc_ws_id and binding:
        refs = await request.app.state.tool_context_service.accessible_workspaces(datarc, user.id)
        if datarc_ws_id not in {item.id for item in refs}:
            return GuideQuestionsDTO(workspace_id=canonical_ws_id, items=[])
    if not datarc_ws_id:
        return GuideQuestionsDTO(workspace_id=canonical_ws_id, items=[])
    try:
        items = await datarc.list_guide_questions(datarc_ws_id)
        return GuideQuestionsDTO(workspace_id=canonical_ws_id, items=items)
    except Exception as e:
        import structlog

        safe = scrub(str(e))
        structlog.get_logger().warning("datarc.guide_questions_failed", error=safe)
        return GuideQuestionsDTO(workspace_id=canonical_ws_id, items=[], error=safe)
