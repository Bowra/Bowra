"""组织模板库端点族（kb-matrix-sharing tech-design §4-§7）。

发布一个空间里的 `kind=template` 列组模板到团队库，同团队其他空间成员发现 + 复制
一份进自己空间（copy 语义，定格快照，数据永不跨空间）。

所有端点：`require_feature_licensed("kb")`；mutation 另挂 `require_not_readonly`。

authz 哲学（§10）：
- 发布/复制走 `_require_workspace_member_strict`（**无 fail-open**，复用 kb_matrix）；
  发布再叠 `_require_admin_or_creator`（strict 已保证 access source，其 fail-open 分支
  不可达）；复制叠 `_require_member_rank`（viewer 拒——会在目标空间建 doc_type）。
- 撤销是 **team 级**操作：**不建 workspace access context**（否则 build_access_context
  对非成员先 raise 403、挡死不在源空间的 team_admin）——走 published_by ∨ team_admin
  ∨（防御式、吞非成员 403 的）源空间 ws_admin。
- get/list/revoke 的 store 方法均 team_id-keyed（IDOR-safe by construction）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import aiosqlite
from bowhead_core.kb.doc_type_store import (
    _fields_to_schema_json,
    _schema_json_to_fields,
    derive_doc_type_kind,
)
from bowhead_core.kb.telemetry import emit_audit
from bowhead_core.space_governance import SpaceOperation
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from bowhead_gateway._entitlement_gate import require_feature_licensed, require_not_readonly
from bowhead_gateway.access_context import build_access_context_from_request, has_workspace_access_source
from bowhead_gateway.routes.auth import _current_user

# Reuse existing authz/helpers (tech-design explicitly mirrors them; cross-module underscore imports are an existing pattern,
# as with auth._current_user / kb_doc_types._require_admin_or_creator).
from bowhead_gateway.routes.kb_doc_types import _require_admin_or_creator
from bowhead_gateway.routes.kb_matrix import (
    _require_member_rank,
    _require_workspace_member_strict,
    _today_str,
    _ws_from_request,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from bowhead_core.kb.doc_type_store import KbDocTypeStore
    from bowhead_core.kb.team_template_store import KbTeamTemplateStore, TeamTemplateRow
    from bowhead_core.user_store import User, UserStore

# schema_json size cap (O5: bound covert channels through uncapped fields such as dimension; ample for normal templates).
_MAX_SCHEMA_JSON_LEN = 100_000


router = APIRouter(
    prefix="/api/v1/kb",
    tags=["knowledge-base"],
    dependencies=[Depends(require_feature_licensed("kb"))],
)


# ─── DTOs ───────────────────────────────────────────────────


class _PublishIn(BaseModel):
    workspace_id: str
    doc_type_id: str


class _CopyIn(BaseModel):
    workspace_id: str
    name: str | None = Field(None, max_length=200)


# ─── DI getters ─────────────────────────────────────────────


def _get_dt_store(request: Request) -> KbDocTypeStore:
    s: KbDocTypeStore | None = getattr(request.app.state, "kb_doc_type_store", None)
    if s is None:
        raise _team_template_error(503, "team_template_service_unavailable")
    return s


def _get_team_tpl_store(request: Request) -> KbTeamTemplateStore:
    s: KbTeamTemplateStore | None = getattr(request.app.state, "kb_team_template_store", None)
    if s is None:
        raise _team_template_error(503, "team_template_service_unavailable")
    return s


def _get_user_store(request: Request) -> UserStore:
    s: UserStore | None = getattr(request.app.state, "user_store", None)
    if s is None:
        raise _team_template_error(503, "team_template_service_unavailable")
    return s


# ─── Endpoints ──────────────────────────────────────────────


@router.post("/team-templates", dependencies=[Depends(require_not_readonly)])
async def publish_team_template(
    body: _PublishIn,
    request: Request,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """发布一个 `kind=template` doc_type 到组织库（幂等 per source_doc_type_id）。"""
    ws = _ws_from_request(request, body.workspace_id)
    try:
        _ws, ctx = await _require_workspace_member_strict(request, user, ws)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to verify access for team-template publish")
        raise _team_template_error(503, "team_template_service_unavailable") from exc
    dt_store = _get_dt_store(request)
    try:
        dt = await dt_store.get(body.doc_type_id)
    except Exception as exc:
        logger.exception("Failed to load doc type for team-template publish")
        raise _team_template_error(503, "team_template_service_unavailable") from exc
    # get() has no ws filter: confused-deputy guard requires doc_type in the caller's membership-verified space.
    if dt is None or dt.workspace_id != ctx.workspace_id:
        raise _team_template_error(404, "team_template_not_found")
    # strict guarantees has_workspace_access_source, making _require_admin_or_creator's fail-open unreachable.
    try:
        await _require_admin_or_creator(request, user, workspace_id=ctx.workspace_id, creator_id=dt.created_by)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to verify management access for team-template publish")
        raise _team_template_error(503, "team_template_service_unavailable") from exc
    # Hard kind gate (defense in depth; Q2's structural boundary is the read-only schema itself).
    if derive_doc_type_kind(dt) != "template":
        raise _team_template_error(422, "not_publishable")
    if not dt.fields:
        raise _team_template_error(422, "empty_template")
    schema_json = _fields_to_schema_json(dt.fields)  # Lossless (fields round-tripped through get()).
    if len(schema_json) > _MAX_SCHEMA_JSON_LEN:
        raise _team_template_error(422, "schema_too_large")
    team_tpl_store = _get_team_tpl_store(request)
    try:
        entry, was_republish = await team_tpl_store.publish(
            team_id=ctx.team_id,
            source_doc_type_id=dt.doc_type_id,
            source_workspace_id=ctx.workspace_id,
            name=dt.name,
            description=dt.description,
            schema_json=schema_json,
            field_count=len(dt.fields),
            published_by=user.id,
        )
    except Exception as exc:
        logger.exception("Failed to publish team template")
        raise _team_template_error(503, "team_template_service_unavailable") from exc
    emit_audit(
        "kb.team_template_republished" if was_republish else "kb.team_template_published",
        {
            "entry_id": entry.entry_id,
            "team_id": ctx.team_id,
            "source_workspace_id": ctx.workspace_id,
            "doc_type_id": dt.doc_type_id,
            "field_count": entry.field_count,
        },
    )
    return _entry_to_dict(entry, field_preview=_field_preview(entry.schema_json))


@router.get("/team-templates")
async def list_team_templates(
    request: Request,
    user: User = Depends(_current_user),
    workspace_id: str = "",
) -> dict[str, Any]:
    """列出调用者组织内所有未撤销的已发布模板（纯 team 作用域）。

    可选 `?workspace_id=` = 当前空间：附带 is_own_space / already_copied 便利标记
    （需该空间成员——防跨空间探测 copied_from 关联）。
    """
    copied_set: set[str] = set()
    with_flags = bool(workspace_id)
    if with_flags:
        # Compute convenience flags only for spaces the caller belongs to (strict: nonmembers → 403; no foreign-space relation leaks).
        try:
            await _require_workspace_member_strict(
                request,
                user,
                workspace_id,
                operation=SpaceOperation.READ_HISTORY,
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Failed to verify access for team-template list")
            raise _team_template_error(503, "team_template_service_unavailable") from exc
        dt_store = _get_dt_store(request)
        try:
            doc_types = await dt_store.list_for_ws(workspace_id)
        except Exception as exc:
            logger.exception("Failed to load copied team-template state")
            raise _team_template_error(503, "team_template_service_unavailable") from exc
        for d in doc_types:
            if d.copied_from_team_template_id:
                copied_set.add(d.copied_from_team_template_id)

    team_tpl_store = _get_team_tpl_store(request)
    try:
        rows = await team_tpl_store.list_live(user.team_id)
    except Exception as exc:
        logger.exception("Failed to list team templates")
        raise _team_template_error(503, "team_template_service_unavailable") from exc
    user_store = _get_user_store(request)

    items: list[dict[str, Any]] = []
    try:
        for e in rows:
            items.append(
                _entry_to_dict(
                    e,
                    field_preview=_field_preview(e.schema_json),
                    source_workspace_name=await _safe_ws_name(user_store, e.source_workspace_id),
                    published_by_name=await _safe_user_name(user_store, e.published_by),
                    is_own_space=(e.source_workspace_id == workspace_id) if with_flags else None,
                    already_copied=(e.entry_id in copied_set) if with_flags else None,
                ),
            )
    except ValueError as exc:
        logger.exception("Failed to parse team-template schema while listing")
        raise _team_template_error(503, "team_template_service_unavailable") from exc
    return {"items": items}


@router.delete("/team-templates/{entry_id}", dependencies=[Depends(require_not_readonly)])
async def revoke_team_template(
    entry_id: str,
    request: Request,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """撤销一个已发布模板（team-scoped 授权：publisher ∨ team_admin ∨ 源空间 ws_admin）。"""
    caller_team = user.team_id
    team_tpl_store = _get_team_tpl_store(request)
    try:
        entry = await team_tpl_store.get(entry_id, team_id=caller_team)  # team-keyed: cross-organization → 404.
    except Exception as exc:
        logger.exception("Failed to load team template for revoke")
        raise _team_template_error(503, "team_template_service_unavailable") from exc
    if entry is None or entry.revoked_at is not None:
        raise _team_template_error(404, "team_template_not_found")
    allowed = entry.published_by == user.id
    if not allowed:
        user_store = _get_user_store(request)
        try:
            allowed = await user_store.is_team_admin(user.id, caller_team)
        except Exception as exc:
            logger.exception("Failed to verify team-template revoke role")
            raise _team_template_error(503, "team_template_service_unavailable") from exc
    # Defensive source-space ws_admin branch: swallow nonmember/deleted-space 403, denying only this branch, never the whole request.
    if not allowed and has_workspace_access_source(request):
        try:
            src_ctx = await build_access_context_from_request(
                request,
                user=user,
                workspace_id=entry.source_workspace_id,
                operation=SpaceOperation.READ_HISTORY,
            )
            allowed = src_ctx.workspace_role == "ws_admin"
        except HTTPException:
            allowed = False
        except Exception as exc:
            logger.exception("Failed to verify source-space role for team-template revoke")
            raise _team_template_error(503, "team_template_service_unavailable") from exc
    if not allowed:
        raise _team_template_error(403, "revoke_not_allowed")
    try:
        await team_tpl_store.revoke(entry_id, team_id=caller_team)
    except Exception as exc:
        logger.exception("Failed to revoke team template")
        raise _team_template_error(503, "team_template_service_unavailable") from exc
    emit_audit(
        "kb.team_template_revoked",
        {
            "entry_id": entry_id,
            "team_id": caller_team,
            "source_workspace_id": entry.source_workspace_id,
            "doc_type_id": entry.source_doc_type_id,
        },
    )
    return {"revoked": True, "entry_id": entry_id}


@router.post("/team-templates/{entry_id}/copy", dependencies=[Depends(require_not_readonly)])
async def copy_team_template(
    entry_id: str,
    body: _CopyIn,
    request: Request,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """复制一个库条目进目标空间（物化 kind=template + copied_from 血缘）。"""
    caller_team = user.team_id
    team_tpl_store = _get_team_tpl_store(request)
    try:
        entry = await team_tpl_store.get(entry_id, team_id=caller_team)  # team-keyed: cross-organization → 404.
    except Exception as exc:
        logger.exception("Failed to load team template for copy")
        raise _team_template_error(503, "team_template_service_unavailable") from exc
    if entry is None or entry.revoked_at is not None:
        raise _team_template_error(404, "team_template_not_found")
    ws = _ws_from_request(request, body.workspace_id)
    try:
        _ws, ctx = await _require_workspace_member_strict(request, user, ws)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to verify access for team-template copy")
        raise _team_template_error(503, "team_template_service_unavailable") from exc
    _require_member_rank(ctx)  # Deny viewers (creates doc_type).
    if ctx.team_id != caller_team:  # Defensive: target space must belong to caller's team.
        raise _team_template_error(404, "team_template_not_found")
    if ctx.workspace_id == entry.source_workspace_id:
        raise _team_template_error(422, "copy_into_source_space")
    dt_store = _get_dt_store(request)
    name = (body.name or entry.name).strip() or entry.name
    try:
        name_taken = await dt_store.name_exists(ctx.workspace_id, name)
    except Exception as exc:
        logger.exception("Failed to check team-template copy name")
        raise _team_template_error(503, "team_template_service_unavailable") from exc
    if name_taken:
        try:
            suggestion = await _suggested_name(dt_store, ctx.workspace_id, name)
        except Exception as exc:
            logger.exception("Failed to suggest a team-template copy name")
            raise _team_template_error(503, "team_template_service_unavailable") from exc
        raise _team_template_error(409, "doc_type_name_taken", suggestion=suggestion)
    try:
        fields = _schema_json_to_fields(entry.schema_json)
        new_dt_id = await dt_store.create(
            workspace_id=ctx.workspace_id,
            name=name,
            created_by=user.id,
            description=entry.description,
            fields=fields,
            cardinality="many",
            auto_extract_enabled=False,
            graduated_mode="template",
            copied_from_team_template_id=entry.entry_id,
        )
    except aiosqlite.IntegrityError:  # Concurrent same-name copies pass name_exists (UNIQUE(ws,name)).
        try:
            suggestion = await _suggested_name(dt_store, ctx.workspace_id, name)
        except Exception as exc:
            logger.exception("Failed to suggest a team-template copy name after conflict")
            raise _team_template_error(503, "team_template_service_unavailable") from exc
        raise _team_template_error(409, "doc_type_name_taken", suggestion=suggestion) from None
    except ValueError:  # Corrupt schema_json / validate() raises.
        raise _team_template_error(422, "invalid_template_schema") from None
    except Exception as exc:
        logger.exception("Failed to copy team template")
        raise _team_template_error(503, "team_template_service_unavailable") from exc
    emit_audit(
        "kb.team_template_copied",
        {
            "entry_id": entry_id,
            "source_workspace_id": entry.source_workspace_id,
            "target_workspace_id": ctx.workspace_id,
            "doc_type_id": new_dt_id,
            "field_count": len(fields),
        },
    )
    try:
        new_dt = await dt_store.get(new_dt_id)
    except Exception as exc:
        logger.exception("Copied team template but failed to reload it")
        raise _team_template_error(503, "team_template_copy_refresh_required") from exc
    if new_dt is None:
        raise _team_template_error(503, "team_template_copy_refresh_required")
    return new_dt.to_dict()


# ─── helpers ────────────────────────────────────────────────


def _team_template_error(status_code: int, code: str, **payload: Any) -> HTTPException:
    return HTTPException(status_code, {"error": {"code": code, **payload}})


def _field_preview(schema_json: str) -> list[dict[str, str]]:
    """定格 schema → 发现面字段预览（名+类型；无 hint/options 细节）。"""
    return [{"name": f.name, "type": f.type} for f in _schema_json_to_fields(schema_json)]


def _entry_to_dict(
    entry: TeamTemplateRow,
    *,
    field_preview: list[dict[str, str]],
    source_workspace_name: str | None = None,
    published_by_name: str | None = None,
    is_own_space: bool | None = None,
    already_copied: bool | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "entry_id": entry.entry_id,
        "name": entry.name,
        "description": entry.description,
        "field_count": entry.field_count,
        "field_preview": field_preview,
        # M5 design review P1: FE matches My Templates doc_types to organization entries (published/changed status,
        # entry_id for revoke/republish); rename-proof, unlike fragile name matching. Opaque ids leak nothing.
        "source_doc_type_id": entry.source_doc_type_id,
        "source_workspace_id": entry.source_workspace_id,
        "source_workspace_name": source_workspace_name,
        "published_by": entry.published_by,
        "published_by_name": published_by_name,
        "published_at": entry.published_at,
        "updated_at": entry.updated_at,
    }
    if is_own_space is not None:
        out["is_own_space"] = is_own_space
    if already_copied is not None:
        out["already_copied"] = already_copied
    return out


async def _suggested_name(dt_store: KbDocTypeStore, workspace_id: str, name: str) -> str:
    """撞名建议名（复刻 M4 kb_matrix graduate 内联逻辑）。"""
    suggestion = f"{name} · {_today_str()}"
    for i in range(2, 10):
        cand = f"{name} · {i}"
        if not await dt_store.name_exists(workspace_id, cand):
            suggestion = cand
            break
    return suggestion


async def _safe_ws_name(user_store: UserStore, workspace_id: str) -> str | None:
    try:
        ws = await user_store.get_workspace(workspace_id)
    except Exception:
        return None
    if not ws:
        return None
    name = ws.get("name")
    return str(name) if name else None


async def _safe_user_name(user_store: UserStore, user_id: str) -> str | None:
    try:
        u = await user_store.get_user(user_id)
    except Exception:
        return None
    if u is None:
        return None
    return u.display_name or u.email
