"""配置副驾草稿端点.

POST /api/v1/config-drafts —— 无副作用的生成端点（批 0）：不落库、不审计、无幂等键。
批 1-b（tech-design-batch1b §2.2）：+= form_draft 消费面——GET /{id}（抽屉取数 +
chip 状态 hydrate）、POST /{id}/consume（保存回执）、POST /{id}/discard。保存本身
走现有表单 API（scheduled 等），consume/discard 只动草稿状态机。
路由常驻；集合型配置面的注册由各自开关控制。
"""

from __future__ import annotations

import os
from dataclasses import asdict
from typing import Any, Literal

from bowhead_core.config_copilot import (
    ConfigDraftInputRejected,
    FormContext,
    generate_config_draft,
    get_form,
    readable_by,
)
from bowhead_core.config_draft_store import (
    FORM_STATUS_CONSUMED,
    FORM_STATUS_DISCARDED,
    FORM_STATUS_DRAFT,
    KIND_FORM_DRAFT,
    ConfigDraft,
    ConfigDraftStore,
)
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.routes._deps import team_router_or_409
from bowhead_gateway.routes.auth import _current_user
from bowhead_gateway.routes.scheduled import _user_id

router = APIRouter(prefix="/api/v1/config-drafts")


class ConfigDraftBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    form_id: str = Field(..., min_length=1, max_length=80)
    text: str = Field(..., min_length=1, max_length=4000)
    pasted_material: str = Field("", max_length=32768)
    current_values: dict[str, Any] = Field(default_factory=dict)
    # Batch 1-d (§3-2): explicitly request extracted raw secrets for bypass direct entry, effective only for opt-in forms.
    include_secrets: bool = False


async def _access_snapshot(request: Request, user: Any) -> dict[str, Any] | None:
    """团队域权限快照。返回 None = 证明不了权限（admin 两档据此 fail-closed）。

    直调 `build_access_context` 而不是 request 包装版——后者把 `AccessContextError` 转成
    `HTTPException` 后再吞就成了死分支（R3 review P1-3：我上一版的 `except AccessContextError`
    永不触发，行为一个字没变）。分三档：
      - `user_store` 缺失 = **真故障** → 503，不伪装成「无权限」（不然用户对着一条错误
        文案永远修不好）；
      - 存储层无 RBAC 钩子（legacy/最小 harness）→ None，证明不了 admin 身份就 fail-closed；
      - `AccessContextError`（含用户失效）→ None。**不往上抛 401**：从设置页把人强制登出
        （FE 的 `clearInvalidSessionAndRedirect`）比拒绝这一次生成糟糕得多。
    """
    from bowhead_gateway.access_context import (
        AccessContextError,
        build_access_context,
        has_workspace_access_source,
    )

    user_store = getattr(request.app.state, "user_store", None)
    if user_store is None:
        raise HTTPException(503, {"error": {"code": "authz_unavailable"}})
    if not has_workspace_access_source(request):
        return None
    try:
        ctx = await build_access_context(
            user_store=user_store,
            access_store=getattr(request.app.state, "access_control_store", None),
            user_id=user.id,
            workspace_id="",
            allow_missing_workspace=True,
        )
    except AccessContextError:
        return None
    return {"team_role": ctx.team_role, "is_team_admin": ctx.is_team_admin, "workspace_role": ctx.workspace_role}


@router.post("")
async def create_config_draft(
    body: ConfigDraftBody,
    request: Request,
    user=Depends(_current_user),
) -> dict[str, Any]:
    registration = get_form(body.form_id)
    if registration is None:
        raise HTTPException(404, {"error": {"code": "config_form_unknown"}})

    # Batch 1-d review P1-6: read_scope previously ran only in chat tools; this endpoint had no permission gate.
    # Adding team_admin forms (llm_provider) let ordinary members fetch whole-team configuration via context_provider.
    #
    # ★ Both ws scopes are **structurally unsupported** here: body has no workspace_id; _access_snapshot
    # only passes empty string, so build_access_context never sets workspace_role (access_context.py's
    # if workspace_id). Even valid space members hit permission_denied, looking like authorization
    # while the channel cannot express which space. Give a distinct code so a future AiFillPanel integrator
    # on space settings does not spend hours diagnosing 403 (currently unreachable: AiFillPanel mounts only
    # scheduled_job and llm_provider formIds).
    if registration.read_scope in ("ws_member", "ws_admin"):
        raise HTTPException(400, {"error": {"code": "workspace_scope_unsupported_here"}})
    # User scope always admits; skip the snapshot so authorization outages do not 500 surfaces needing no team permission
    # (scheduled_job; R3 review P2-5: before batch 1-d this depended on no authorization snapshot).
    access = None if registration.read_scope == "user" else await _access_snapshot(request, user)
    allowed, deny_code = readable_by(registration, access)
    if not allowed:
        raise HTTPException(403, {"error": {"code": deny_code}})

    # Direct unbilled LLM endpoints need rate limiting (same rationale as kb_matrix S-P2-3).
    rate_limiter = getattr(getattr(request.app.state, "user_store", None), "rate_limit_check", None)
    if callable(rate_limiter):
        try:
            limit = int(os.environ.get("BOWHEAD_CONFIG_DRAFT_RL", "") or 60)
        except ValueError:  # R3 review P2-10: invalid env value defaults, never 500.
            limit = 60
        if not await rate_limiter(f"config_draft:{user.id}", limit=limit, window_s=3600):
            raise HTTPException(429, {"error": {"code": "rate_limited"}})

    llm_router = await team_router_or_409(
        request.app.state,
        user.team_id,
        site="config_draft",
        entrypoint="tool_continuation",
    )

    ctx = FormContext(
        user_id=_user_id(request),
        user_email=user.email,
        team_id=user.team_id,
        locale=(request.headers.get("accept-language") or "en-US").split(",")[0].strip() or "en-US",
        current_values=body.current_values,
    )
    from bowhead_router.usage import usage_context

    try:
        # Batch 2-b attribution: batch 2-a's nine-site sweep missed this copilot draft route, so every AI fill
        # across **five live configuration surfaces was unattributed** despite real token spend, not charged to its team.
        # Pin reason to _SITE_REASONS' static guard.
        with usage_context(team_id=user.team_id, user_id=user.id, reason="config_draft"):
            result = await generate_config_draft(
                llm_router,
                registration,
                ctx,
                body.text,
                body.pasted_material,
            )
    except ConfigDraftInputRejected:
        raise HTTPException(422, {"error": {"code": "config_draft_input_rejected"}}) from None

    # Batch 1-d wrap-up: **allowlist projection**, never asdict + denylist pops.
    # Not about today's omissions (FE consumes all 7 fields), but drift direction: denylisting automatically
    # exposes a new DraftResult field (secret bypass nearly opened that flow); with allowlisting,
    # omitted projection visibly breaks FE on the safe side.
    payload: dict[str, Any] = {
        "form_id": registration.form_id,
        "status": result.status,
        "fields": result.fields,
        "field_meta": result.field_meta,
        "issues": [asdict(issue) for issue in result.issues],
        "clarification": result.clarification,
        "secret_redacted": result.secret_redacted,
    }
    # Return raw secrets only with form opt-in **and** explicit caller request (even status != "ok":
    # degraded users most need direct entry).
    if registration.secret_bypass_supported and body.include_secrets:
        payload["secrets"] = result.secrets
    return payload


# ─── Batch 1-b: form_draft consumer (tech-design-batch1b §2.2) ───


def _error(status_code: int, code: str, **details: Any) -> HTTPException:
    payload: dict[str, Any] = {"code": code}
    if details:
        payload["details"] = details
    return HTTPException(status_code, {"error": payload})


def _draft_store(request: Request) -> ConfigDraftStore:
    store = getattr(request.app.state, "config_draft_store", None)
    if not isinstance(store, ConfigDraftStore):
        raise _error(503, "config_draft_store_unavailable")
    return store


async def _owned_form_draft(request: Request, user: Any, draft_id: str) -> ConfigDraft:
    draft = await _draft_store(request).get_draft(draft_id, team_id=user.team_id, owner_user_id=user.id)
    # Missing / cross-team / cross-owner all 404 (anti-enumeration).
    if draft is None:
        raise _error(404, "config_draft_not_found")
    if draft.kind != KIND_FORM_DRAFT:
        raise _error(409, "draft_kind_mismatch")
    if draft.form_id == "team_subagent" and get_form("team_subagent") is None:
        raise _error(404, "config_draft_not_found")
    return draft


def _form_draft_dto(draft: ConfigDraft, *, replayed: bool | None = None) -> dict[str, Any]:
    payload = draft.payload or {}
    out: dict[str, Any] = {
        "draft_id": draft.id,
        "form_id": draft.form_id,
        "status": draft.status,
        "workspace_id": draft.workspace_id,
        "fields": payload.get("fields") or {},
        "field_meta": payload.get("field_meta") or {},
        "notes": payload.get("notes") or {},
        "result": payload.get("result"),
        "row_version": draft.row_version,
    }
    # Batch 3 (3-0c, §4.5): collection-surface keys project **only when present**; missing them from the allowlist
    # loses the drawer's edit target after refresh/reconnect (r1-P1-5). Singleton payloads never contain these keys
    # (3-0b envelope writes only non-None values), preserving existing DTO bytes.
    for key in ("target", "changed_fields_cumulative"):
        value = payload.get(key)
        if value is not None:
            out[key] = value
    if replayed is not None:
        out["replayed"] = replayed
    return out


class ConsumeDraftBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    class Result(BaseModel):
        model_config = ConfigDict(extra="forbid")

        job_id: str | None = Field(None, min_length=1, max_length=64)
        kind: Literal["team_subagent"] | None = None
        stage: Literal["working_copy"] | None = None
        ref_id: str | None = Field(None, min_length=1, max_length=64)
        label: str | None = Field(None, max_length=120)
        name: str | None = Field(None, max_length=120)
        human_schedule: str | None = Field(None, max_length=200)
        next_run_at: str | None = Field(None, max_length=64)

    result: Result


@router.get("/{draft_id}")
async def get_form_draft(
    draft_id: str,
    request: Request,
    user=Depends(_current_user),
) -> dict[str, Any]:
    """抽屉挂载取数 + chip 状态 hydrate（渲染数据在事件 payload 也有，这里是服务端真源）。"""
    return _form_draft_dto(await _owned_form_draft(request, user, draft_id))


@router.post("/{draft_id}/consume", dependencies=[Depends(require_not_readonly)])
async def consume_form_draft(
    draft_id: str,
    body: ConsumeDraftBody,
    request: Request,
    user=Depends(_current_user),
) -> dict[str, Any]:
    """保存成功后的回执标记。result 是展示性指针（保存已过原写 API 全部校验，
    伪造只污染自己的草稿行——tech-design-batch1b §2.2 拍板）。"""
    draft = await _owned_form_draft(request, user, draft_id)
    if draft.form_id == "team_subagent":
        if body.result.kind != "team_subagent" or body.result.ref_id != (draft.payload or {}).get("target", {}).get("id"):
            raise _error(422, "draft_result_target_mismatch")
    elif draft.form_id == "scheduled_job" and not body.result.job_id:
        raise _error(422, "draft_result_job_missing")
    if draft.status == FORM_STATUS_CONSUMED:
        return _form_draft_dto(draft, replayed=True)
    if draft.status != FORM_STATUS_DRAFT:
        raise _error(409, "draft_state_conflict", status=draft.status)
    result = await _draft_store(request).transition(
        draft.id,
        team_id=user.team_id,
        owner_user_id=user.id,
        to_status=FORM_STATUS_CONSUMED,
        from_statuses=(FORM_STATUS_DRAFT,),
        result_patch=body.result.model_dump(exclude_none=True),
    )
    if result.outcome in {"ok", "replay"} and result.draft is not None:
        return _form_draft_dto(result.draft, replayed=result.outcome == "replay")
    if result.outcome == "conflict" and result.draft is not None:
        raise _error(409, "draft_state_conflict", status=result.draft.status)
    raise _error(404, "config_draft_not_found")


@router.post("/{draft_id}/discard", dependencies=[Depends(require_not_readonly)])
async def discard_form_draft(
    draft_id: str,
    request: Request,
    user=Depends(_current_user),
) -> dict[str, Any]:
    draft = await _owned_form_draft(request, user, draft_id)
    if draft.status == FORM_STATUS_DISCARDED:
        return _form_draft_dto(draft, replayed=True)
    if draft.status != FORM_STATUS_DRAFT:
        raise _error(409, "draft_state_conflict", status=draft.status)
    result = await _draft_store(request).transition(
        draft.id,
        team_id=user.team_id,
        owner_user_id=user.id,
        to_status=FORM_STATUS_DISCARDED,
        from_statuses=(FORM_STATUS_DRAFT,),
    )
    if result.outcome in {"ok", "replay"} and result.draft is not None:
        return _form_draft_dto(result.draft, replayed=result.outcome == "replay")
    if result.outcome == "conflict" and result.draft is not None:
        raise _error(409, "draft_state_conflict", status=result.draft.status)
    raise _error(404, "config_draft_not_found")
