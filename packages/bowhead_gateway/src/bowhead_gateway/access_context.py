"""Gateway helpers for building and enforcing Bowhead access context."""

from __future__ import annotations

import logging
import os
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from bowhead_core.access_control import (
    AccessContext,
    AccessControlStore,
    normalize_workspace_role,
)
from bowhead_core.space_governance import SpaceOperation
from fastapi import HTTPException, Request, status

from bowhead_gateway.ws_authz import authorize_ws_access

if TYPE_CHECKING:
    from bowhead_core.user_store import User, UserStore

logger = logging.getLogger(__name__)


class AccessContextError(Exception):
    def __init__(self, code: str, status_code: int = status.HTTP_403_FORBIDDEN) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


_REQUIRED_USER_STORE_METHODS = (
    "get_user_team_id",
    "get_workspace_team_id",
    "get_member_role",
)

_TRUTHY = frozenset({"1", "true", "yes", "on"})
#: Space isolation batch 4: fail-closed artifact attribution (which space owns it, separate from read authorization).
_ARTIFACT_WORKSPACE_FAIL_CLOSE_ENV = "FEATURE_ARTIFACT_WORKSPACE_FAIL_CLOSE"

# Membership denial (authorization) must stay separate from archived-space rejection (lifecycle):
# routes may rewrite the former to their existing denial semantics (turns' anti-enumeration 404 / canvas'
# report_unavailable); the latter's 409 must propagate unchanged, or an archived space is presented
# as nonexistent.
_AUTHORIZATION_DENIAL_CODES = frozenset({"workspace_role_unknown", "team_access_denied"})


def artifact_workspace_fail_close_enabled() -> bool:
    """``FEATURE_ARTIFACT_WORKSPACE_FAIL_CLOSE`` —— 默认 **关**（空间隔离批4）。

    - **关** = 逐字节今天的行为: ``files.py`` 只按四级 provenance **现场推导**产物归属,
      推不出就早退、根本不进闸（fail-open）。``artifacts.workspace_id`` 这一列
      **根本不读**——所以列与推导不一致时行为也不变。
    - **开** = 按 ``artifacts.workspace_id`` 分四档判定（``artifact-workspace-backfill.md`` §7.4）:
      真实 ``ws_*`` 按空间判｜``__team__`` 走**团队级授权**｜推不出归属且创建于**切点之后**
      ⇒ fail-close（那是写入点的 bug）｜推不出归属但创建于**切点之前** ⇒ **保持今天行为**。

    ⚠ 这是**行为改变面**——切点后推不出归属的产物会 403 ⇒ 按 CLAUDE.md 默认关、灰度不少于
    24 小时; 回滚 = 置 0 重启, 不需要回镜像, 也不需要回滚数据库（水位戳留在库里无副作用）。
    ⚠ 翻闸时**必须同步更新** ``docs/design/团队角色功能表.md`` 与 ``空间角色功能表.md``。
    """
    return (os.environ.get(_ARTIFACT_WORKSPACE_FAIL_CLOSE_ENV) or "").strip().lower() in _TRUTHY


def is_space_authorization_denial(code: str | None) -> bool:
    """这个错误码是「授权没过」(可被路由改写口径) 还是别的 (必须原样透出)？"""
    if not code:
        return False
    return code.startswith("ws_access_denied:") or code in _AUTHORIZATION_DENIAL_CODES


def space_denial_code(exc: HTTPException) -> str | None:
    """从统一入口抛出的 ``HTTPException`` 里取回 ``{"error": {"code": ...}}``."""
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        error = detail.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            if isinstance(code, str):
                return code
    return None


def has_workspace_access_source(request: Request) -> bool:
    """Return whether request.app.state has the user-store hooks needed for RBAC.

    Several legacy route tests mount KB routers with tiny fake app state objects.
    Production gateway startup installs the full UserStore, so callers can use
    this helper to keep old test harnesses working without weakening real RBAC.
    """
    user_store = getattr(request.app.state, "user_store", None)
    return all(callable(getattr(user_store, method, None)) for method in _REQUIRED_USER_STORE_METHODS)


def access_context_to_dict(ctx: AccessContext | None) -> dict[str, Any] | None:
    if ctx is None:
        return None
    data = asdict(ctx)
    data["group_ids"] = list(ctx.group_ids)
    return data


async def require_space_lifecycle_without_authorization(
    *,
    user_store: UserStore,
    workspace_id: str,
    operation: SpaceOperation,
) -> None:
    """Check space lifecycle after the caller has authorized membership.

    This helper checks active or archived state only. It does not authorize team
    or space membership. Call build_access_context when both membership and
    lifecycle admission are required; that function performs them in that order.
    """

    get_workspace = getattr(user_store, "get_workspace", None)
    if not callable(get_workspace):
        raise AccessContextError(
            "authz_unavailable",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    try:
        workspace = await get_workspace(workspace_id)
    except Exception as exc:
        raise AccessContextError(
            "authz_lookup_failed",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from exc
    if workspace is None:
        # A stale membership row must not make an unknown Space distinguishable
        # from the existing non-member/cross-Team denial family.
        raise AccessContextError(
            "ws_access_denied:not_a_member",
            status.HTTP_403_FORBIDDEN,
        )
    workspace_status = str(workspace.get("status") or "")
    if workspace_status == "archived":
        if operation is not SpaceOperation.READ_HISTORY:
            raise AccessContextError(
                "workspace_archived",
                status.HTTP_409_CONFLICT,
            )
        return
    if workspace_status != "active":
        raise AccessContextError(
            "workspace_unavailable",
            status.HTTP_409_CONFLICT,
        )


async def build_access_context(
    *,
    user_store: UserStore,
    access_store: AccessControlStore | None,
    user_id: str,
    workspace_id: str,
    allow_missing_workspace: bool = False,
    operation: SpaceOperation = SpaceOperation.WRITE,
    allow_team_admin_override: bool = False,
    expected_team_id: str | None = None,
) -> AccessContext:
    """Build a fail-closed access snapshot —— 授权 + 生命周期的**唯一入口**.

    ``allow_missing_workspace`` is reserved for explicitly team/personal-scoped
    operations. Workspace-scoped callers keep the default hard requirement.

    ``allow_team_admin_override``: 对本团队 owner / team_admin 放过「空间成员资格」这一关
    (PRD ``prd-space-isolation.md`` §5.5 选 A —— 保留今天活着的管理员通路; 理由不是
    「管理员理应有特权」, 而是没有人拍板要收紧它)。**默认 False**, 由各路由按自己既有的
    语义显式传入 —— 豁免必须是签名上看得见的一个决定, 不许藏在实现里。
    只对 ``not_a_member`` 生效: ``no_team`` / ``cross_team`` 仍然 fail-closed,
    生命周期闸也照常执行。
    """
    if not workspace_id and not allow_missing_workspace:
        raise AccessContextError("workspace_required", status.HTTP_400_BAD_REQUEST)
    if expected_team_id is not None:
        if not expected_team_id:
            raise AccessContextError(
                "team_required",
                status.HTTP_400_BAD_REQUEST,
            )
        user = await user_store.get_user(user_id)
        expected_role = await user_store.get_team_role(
            user_id,
            expected_team_id,
        )
        if user is None or getattr(user, "status", None) != "active":
            raise AccessContextError(
                "user_not_active",
                status.HTTP_401_UNAUTHORIZED,
            )
        if expected_role is None:
            raise AccessContextError(
                "team_access_denied",
                status.HTTP_403_FORBIDDEN,
            )
        fresh_team_id = expected_team_id
    else:
        expected_role = None
        fresh_team_id = await user_store.get_user_team_id(user_id)
    if not fresh_team_id:
        raise AccessContextError("user_not_active", status.HTTP_401_UNAUTHORIZED)

    normalized_role = ""
    if workspace_id:
        ws_team_id = await user_store.get_workspace_team_id(workspace_id)
        role = await user_store.get_member_role(workspace_id, user_id)
        deny = authorize_ws_access(
            enforce=True,
            existing_role=role,
            ws_team_id=ws_team_id,
            caller_team_id=fresh_team_id,
        )
        team_admin_admitted = False
        if deny == "not_a_member" and allow_team_admin_override:
            # PRD §5.5-A: admit this team's owner / team_admin through the space-membership gate.
            # Override only not_a_member; no_team / cross_team retain the 403 denial above.
            override_role = await user_store.get_team_role(user_id, ws_team_id or fresh_team_id)
            if override_role in {"owner", "team_admin"}:
                team_admin_admitted = True
                deny = None
        if deny:
            raise AccessContextError(f"ws_access_denied:{deny}", status.HTTP_403_FORBIDDEN)
        normalized_role = normalize_workspace_role(role) or ""
        if not normalized_role and not team_admin_admitted:
            raise AccessContextError("workspace_role_unknown", status.HTTP_403_FORBIDDEN)
        await require_space_lifecycle_without_authorization(
            user_store=user_store,
            workspace_id=workspace_id,
            operation=operation,
        )

    team_role: str | None = None
    is_team_admin = False
    try:
        team_role = expected_role or await user_store.get_team_role(
            user_id,
            fresh_team_id,
        )
        # SP-AUTH-3: recognize roles only within this ctx's team; no teamless is_team_admin() fallback
        # (it checks users.team_id's primary team and would import another team's admin identity into this ctx).
        is_team_admin = team_role in {"owner", "team_admin"}
    except Exception as exc:
        logger.warning("access_context.team_role_lookup_failed user=%s err=%s", user_id, exc)

    group_ids: tuple[str, ...] = ()
    policy_epoch = 0
    if access_store is not None:
        group_ids = await access_store.principal_group_ids(fresh_team_id, user_id, workspace_id)
        policy_epoch = await access_store.get_policy_epoch(fresh_team_id)

    return AccessContext(
        user_id=user_id,
        team_id=fresh_team_id,
        workspace_id=workspace_id,
        workspace_role=normalized_role,
        team_role=team_role,
        group_ids=group_ids,
        policy_epoch=policy_epoch,
        is_team_admin=is_team_admin,
    )


async def build_access_context_from_request(
    request: Request,
    *,
    user: User,
    workspace_id: str,
    allow_missing_workspace: bool = False,
    operation: SpaceOperation = SpaceOperation.WRITE,
    allow_team_admin_override: bool = False,
) -> AccessContext:
    return await build_access_context_for_user_from_request(
        request,
        user_id=user.id,
        workspace_id=workspace_id,
        allow_missing_workspace=allow_missing_workspace,
        operation=operation,
        allow_team_admin_override=allow_team_admin_override,
    )


async def build_access_context_for_user_from_request(
    request: Request,
    *,
    user_id: str,
    workspace_id: str,
    allow_missing_workspace: bool = False,
    operation: SpaceOperation = SpaceOperation.WRITE,
    allow_team_admin_override: bool = False,
    expected_team_id: str | None = None,
) -> AccessContext:
    """Build a request-scoped context when the caller was already resolved."""

    user_store: UserStore | None = getattr(request.app.state, "user_store", None)
    if user_store is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"error": {"code": "authz_unavailable"}},
        )
    access_store: AccessControlStore | None = getattr(
        request.app.state,
        "access_control_store",
        None,
    )
    try:
        return await build_access_context(
            user_store=user_store,
            access_store=access_store,
            user_id=user_id,
            workspace_id=workspace_id,
            allow_missing_workspace=allow_missing_workspace,
            operation=operation,
            allow_team_admin_override=allow_team_admin_override,
            expected_team_id=expected_team_id,
        )
    except AccessContextError as exc:
        raise HTTPException(
            exc.status_code,
            {"error": {"code": exc.code, "workspace_id": workspace_id}},
        ) from exc
