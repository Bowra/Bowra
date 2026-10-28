"""i18n v1 · /api/v1/workspace-settings routes.

镜像 user_settings.py 设计：workspace 级 KV 配置存取.

GET    /api/v1/workspace-settings/{workspace_id}        → 该 workspace 全部 settings
PUT    /api/v1/workspace-settings/{workspace_id}/{key}  → 设单个 key
DELETE /api/v1/workspace-settings/{workspace_id}/{key}  → 删单个 key

当前已支持 key:
- default_ui_language: 兜底用户未设
- default_data_language: workspace 范围 DB 内容语言（admin 一次性设置）
- week_start_day: BI 时间分组的"周"起点 ("monday" | "sunday")
- i18n_v1: workspace 级 feature flag (默认 false)

权限校验：`_check_workspace_access` — 空间必须属当前团队（否则 404 反枚举），
并且调用者必须是该空间成员；写入和 Space 常规配置仅限显式 Space admin。
（2026-07-21 概念术语审计：更正过时的「dev stub / next-week 接中间件」自述。）
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from bowhead_core.access_control import normalize_workspace_role
from bowhead_core.space_governance import SpaceGovernanceError
from bowhead_core.user_store import _UNSET
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.routes.auth import _current_user
from bowhead_gateway.routes.auth import _store as _user_store_dep

if TYPE_CHECKING:
    from bowhead_core.user_store import User, UserStore
    from bowhead_core.workspace_settings_store import WorkspaceSettingsStore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/workspace-settings", tags=["workspace-settings"])


_ALLOWED_KEYS = {
    "default_ui_language",
    "default_data_language",
    "week_start_day",
    "i18n_v1",
}
# Batch 2/c14 (PRD §3.8): BI semantics keys (key_metrics/known_dimensions/internal_definitions/
# default_time_window) are **permanently excluded** from this allowlist; Datarc owns its semantics, Bowhead
# does not duplicate them (user decision). Retire existing ghost rows via scripts/retire_bi_settings_keys.py.

_VALID_UI_LANGUAGES = {"zh-CN", "en-US", "de-DE", "fr-FR"}
_VALID_DATA_LANGUAGES = {"zh-CN", "en-US"}
_VALID_WEEK_START = {"monday", "sunday"}


def _store(request: Request) -> WorkspaceSettingsStore:
    store = getattr(request.app.state, "workspace_settings_store", None)
    if store is None:
        raise HTTPException(503, "workspace_settings_store not configured")
    return store


async def _check_workspace_access(
    *,
    user: User,
    user_store: UserStore,
    workspace_id: str,
    require_admin: bool,
) -> None:
    workspace_team = await user_store.get_workspace_team_id(workspace_id)
    if workspace_team != user.team_id:
        raise HTTPException(status_code=404, detail="workspace not found")
    if require_admin:
        workspace = await user_store.get_workspace(workspace_id)
        if workspace is None or workspace["status"] != "active":
            raise HTTPException(409, {"code": "workspace_archived"})
    role = normalize_workspace_role(await user_store.get_member_role(workspace_id, user.id))
    if role is None:
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "code": "E_PERMISSION_DENIED",
                    "message": f"user '{user.id}' is not a workspace member for '{workspace_id}'",
                }
            },
        )
    if not require_admin:
        return
    if role == "ws_admin":
        return
    raise HTTPException(
        status_code=403,
        detail={
            "error": {
                "code": "E_PERMISSION_DENIED",
                "message": f"user '{user.id}' is not a workspace admin for '{workspace_id}'",
            }
        },
    )


def _validate_value(key: str, value: str) -> None:
    """每个 key 的取值约束."""
    if key in ("default_ui_language", "default_data_language"):
        allowed = _VALID_UI_LANGUAGES if key == "default_ui_language" else _VALID_DATA_LANGUAGES
        if value not in allowed:
            raise HTTPException(
                400,
                f"invalid {key}: {value} (allowed: {sorted(allowed)})",
            )
    elif key == "week_start_day":
        if value not in _VALID_WEEK_START:
            raise HTTPException(
                400,
                f"invalid week_start_day: {value} (allowed: {sorted(_VALID_WEEK_START)})",
            )
    elif key == "i18n_v1" and value not in ("true", "false"):
        raise HTTPException(400, f"i18n_v1 must be 'true' or 'false', got: {value}")


class SettingsValue(BaseModel):
    value: str = Field(..., description="单个 setting value")


# Configuration copilot batch 1-a (tech-design-batch1 §7): removed batch 0.5's origin/proposal_id/op tuple;
# self-reported provenance is forgeable (P3-1). Chat writes/audits (workspace_settings_changed) moved to
# /api/v1/config-proposals; this route returns to batch 0 shape with no copilot traces.


class SettingsAll(BaseModel):
    settings: dict[str, str]


class WorkspaceMetadata(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(None, max_length=2000)
    revision: int = Field(..., ge=1)


class UpdateWorkspaceMetadata(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    description: str | None = Field(None, max_length=2000)
    expected_revision: int = Field(..., ge=1)


@router.get("/{workspace_id}", response_model=SettingsAll)
async def list_workspace_settings(
    workspace_id: str,
    request: Request,
    store: WorkspaceSettingsStore = Depends(_store),
    user: User = Depends(_current_user),
    user_store: UserStore = Depends(_user_store_dep),
) -> SettingsAll:
    """读取 workspace 全部设置. 需要显式 Space 成员身份。"""
    await _check_workspace_access(
        user=user,
        user_store=user_store,
        workspace_id=workspace_id,
        require_admin=False,
    )
    settings = await store.list_for_workspace(workspace_id)
    return SettingsAll(settings=settings)


@router.put(
    "/{workspace_id}/{key}",
    response_model=SettingsValue,
    dependencies=[Depends(require_not_readonly)],
)
async def set_workspace_setting(
    workspace_id: str,
    key: str,
    body: SettingsValue,
    request: Request,
    store: WorkspaceSettingsStore = Depends(_store),
    user: User = Depends(_current_user),
    user_store: UserStore = Depends(_user_store_dep),
) -> SettingsValue:
    """设置 workspace 单个 key. 需要显式 Space admin 身份。"""
    if key not in _ALLOWED_KEYS:
        raise HTTPException(400, f"unknown key: {key} (allowed: {sorted(_ALLOWED_KEYS)})")
    _validate_value(key, body.value)
    await _check_workspace_access(
        user=user,
        user_store=user_store,
        workspace_id=workspace_id,
        require_admin=True,
    )
    await store.set(workspace_id, key, body.value)
    return SettingsValue(value=body.value)


@router.delete("/{workspace_id}/{key}", dependencies=[Depends(require_not_readonly)])
async def delete_workspace_setting(
    workspace_id: str,
    key: str,
    request: Request,
    store: WorkspaceSettingsStore = Depends(_store),
    user: User = Depends(_current_user),
    user_store: UserStore = Depends(_user_store_dep),
) -> dict[str, Any]:
    if key not in _ALLOWED_KEYS:
        raise HTTPException(400, f"unknown key: {key}")
    await _check_workspace_access(
        user=user,
        user_store=user_store,
        workspace_id=workspace_id,
        require_admin=True,
    )
    await store.delete(workspace_id, key)
    return {"ok": True, "workspace_id": workspace_id, "key": key}


@router.get("/{workspace_id}/metadata", response_model=WorkspaceMetadata)
async def get_workspace_metadata(
    workspace_id: str,
    user: User = Depends(_current_user),
    user_store: UserStore = Depends(_user_store_dep),
) -> WorkspaceMetadata:
    await _check_workspace_access(
        user=user,
        user_store=user_store,
        workspace_id=workspace_id,
        require_admin=True,
    )
    workspace = await user_store.get_workspace(workspace_id)
    if workspace is None or workspace["team_id"] != user.team_id:
        raise HTTPException(404, detail="workspace not found")
    return WorkspaceMetadata(
        name=str(workspace["name"]),
        description=workspace["description"],
        revision=int(workspace["revision"]),
    )


@router.patch(
    "/{workspace_id}/metadata",
    response_model=WorkspaceMetadata,
    dependencies=[Depends(require_not_readonly)],
)
async def update_workspace_metadata(
    workspace_id: str,
    body: UpdateWorkspaceMetadata,
    user: User = Depends(_current_user),
    user_store: UserStore = Depends(_user_store_dep),
) -> WorkspaceMetadata:
    if body.name is None and "description" not in body.model_fields_set:
        raise HTTPException(422, {"code": "empty_update"})
    await _check_workspace_access(
        user=user,
        user_store=user_store,
        workspace_id=workspace_id,
        require_admin=True,
    )
    workspace = await user_store.get_workspace(workspace_id)
    if workspace is None or workspace["team_id"] != user.team_id:
        raise HTTPException(404, detail="workspace not found")
    if workspace["status"] != "active":
        raise HTTPException(409, {"code": "workspace_archived"})
    try:
        updated = await user_store.update_governed_workspace(
            team_id=user.team_id,
            actor_user_id=user.id,
            workspace_id=workspace_id,
            expected_revision=body.expected_revision,
            name=body.name,
            description=(body.description if "description" in body.model_fields_set else _UNSET),
        )
    except SpaceGovernanceError as exc:
        status_code = 409 if exc.code in {"stale_workspace_update", "duplicate_workspace_name"} else 422
        raise HTTPException(status_code, {"code": exc.code}) from exc
    return WorkspaceMetadata(
        name=str(updated["name"]),
        description=updated["description"],
        revision=int(updated["revision"]),
    )
