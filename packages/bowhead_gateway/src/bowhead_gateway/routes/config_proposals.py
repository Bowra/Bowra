"""配置副驾轻确认条 proposal 端点（tech-design-batch1-draft-store.md §5）.

批 0.5 的 FE 编排写收拢为服务端 seam：终态真源=config_copilot_draft.status、
proposal_id=owner-scoped 落库行（apply 按 (id, team, owner) 查得才动手）、appearance
合并在 store 单事务内完成。apply/undo/ignore 只响应用户在确认条上的显式点击
（浏览器带用户凭证的 POST）——R4-2 确认不可省在此不变。

Always mounted by main.py alongside /config-drafts (the FEATURE_CONFIG_COPILOT gate was removed on 2026-08-18).
写与 proposal CAS 同库（bowhead.db）不同事务：crash 窗口=「已生效但仍 proposed」，
用户重点击即收敛（tech design §10-4 记录接受）。
"""

from __future__ import annotations

import logging
from typing import Any

from bowhead_core.audit_actions import AuditAction
from bowhead_core.config_copilot import AppliedChange, FormContext, LiteApplyError, get_form
from bowhead_core.config_draft_store import (
    KIND_LITE_PROPOSAL,
    LITE_STATUS_APPLIED,
    LITE_STATUS_IGNORED,
    LITE_STATUS_PROPOSED,
    LITE_STATUS_REVERTED,
    ConfigDraft,
    ConfigDraftStore,
)
from bowhead_core.configuration_contracts import TEAM_FORMS, domain_enabled
from bowhead_core.user_store import User, UserStore
from fastapi import APIRouter, Depends, HTTPException, Request

from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.routes.auth import _current_user
from bowhead_gateway.routes.auth import _store as _user_store_dep
from bowhead_gateway.routes.workspace_settings import _check_workspace_access

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/config-proposals", tags=["config-copilot"])


def _error(status_code: int, code: str, **details: Any) -> HTTPException:
    payload: dict[str, Any] = {"code": code}
    if details:
        payload["details"] = details
    return HTTPException(status_code, {"error": payload})


def _draft_store(request: Request) -> ConfigDraftStore:
    # Intentionally depart from team_subagent_drafts' lazy-construction precedent: lifespan is the sole constructor
    # (tolerance: tech design §10-3); missing store at route layer immediately returns 503.
    store = getattr(request.app.state, "config_draft_store", None)
    if not isinstance(store, ConfigDraftStore):
        raise _error(503, "config_draft_store_unavailable")
    return store


async def _owned_lite_proposal(
    request: Request,
    user: User,
    proposal_id: str,
) -> ConfigDraft:
    draft = await _draft_store(request).get_draft(
        proposal_id,
        team_id=user.team_id,
        owner_user_id=user.id,
    )
    # Missing / cross-team / cross-owner all 404 (anti-enumeration, same as subagent drafts).
    if draft is None:
        raise _error(404, "config_proposal_not_found")
    if draft.kind != KIND_LITE_PROPOSAL:
        raise _error(409, "proposal_kind_mismatch")
    if draft.form_id in TEAM_FORMS or draft.form_id == "workspace_metadata":
        # Current-Space metadata receipts retain the original page's admin boundary.
        await _require_apply_permission(request, user, _user_store_dep(request), draft)
    elif draft.form_id == "space_instructions":
        # Reading or dismissing the suggestion follows the page's read rule, so a card applied before the Space was
        # archived or the caller lost admin rights keeps its real status. Apply rechecks the manage rule.
        from bowhead_gateway.configuration_space_instructions import space_instructions_access

        try:
            await space_instructions_access(_user_store_dep(request), user, draft.workspace_id or "", manage=False)
        except PermissionError as exc:
            raise _error(403, "permission_denied") from exc
    return draft


def _dto(draft: ConfigDraft, *, replayed: bool | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "proposal_id": draft.id,
        "form_id": draft.form_id,
        "status": draft.status,
        "workspace_id": draft.workspace_id,
        "team_id": draft.team_id,
        "owner_user_id": draft.owner_user_id,
    }
    if replayed is not None:
        out["replayed"] = replayed
    return out


def _form_context(request: Request, user: User, draft: ConfigDraft) -> FormContext:
    locale = (request.headers.get("accept-language") or "en-US").split(",")[0].strip() or "en-US"
    return FormContext(
        user_id=user.id,
        user_email=user.email,
        team_id=user.team_id,
        locale=locale,
        current_values={},
        workspace_id=draft.workspace_id,
        deps={"request": request, "actor": user},
    )


async def _require_apply_permission(
    request: Request,
    user: User,
    user_store: UserStore,
    draft: ConfigDraft,
) -> None:
    """点击时重查（propose 到 apply 之间角色可能已变）；user 域 owner-scope 已由查询保证。"""
    reg = get_form(draft.form_id)
    if reg is None or reg.lite_applier is None:
        # Orphan after deployment rollback / unregistered form -> honest 409; FE stays retryable.
        raise _error(409, "form_unavailable")
    if draft.form_id in TEAM_FORMS:
        if not domain_enabled(draft.form_id):
            raise _error(409, "form_unavailable")
        if not await user_store.user_has_active_team_membership(user.id, user.team_id) or await user_store.get_team_role(
            user.id, user.team_id
        ) not in {"owner", "team_admin"}:
            raise _error(403, "permission_denied")
        return
    if reg.write_scope != "user":
        if not draft.workspace_id:
            raise _error(409, "form_unavailable")
        if draft.form_id == "space_instructions":
            # Space admins and Team managers save Space instructions (the page's own rule), not Space admins only.
            from bowhead_gateway.configuration_space_instructions import space_instructions_access

            try:
                await space_instructions_access(user_store, user, draft.workspace_id, manage=True)
            except PermissionError as exc:
                raise _error(403, "permission_denied") from exc
            return
        await _check_workspace_access(
            user=user,
            user_store=user_store,
            workspace_id=draft.workspace_id,
            require_admin=True,
        )


async def _audit_changes(
    request: Request,
    user: User,
    user_store: UserStore,
    draft: ConfigDraft,
    changes: list[AppliedChange],
    op: str,
) -> None:
    """applier 返回的每条都落审计（含等值收敛写，review P2-2c）；失败不阻断（auth.py 纪律）。"""
    ip = request.client.host if request.client else None
    for change in changes:
        try:
            if change.scope == "workspace":
                await user_store.write_admin_audit_log(
                    actor_user_id=user.id,
                    team_id=user.team_id,
                    action=AuditAction.WORKSPACE_SETTINGS_CHANGED,
                    target_kind="workspace_settings",
                    target_id=change.key,
                    target_team_id=user.team_id,
                    scope="workspace",
                    payload={
                        "workspace_id": draft.workspace_id,
                        "key": change.key,
                        "before": change.before,
                        "after": change.after,
                        "operation": change.operation,
                        "origin": "chat",
                        "proposal_id": draft.id,
                        "op": op,
                    },
                    ip=ip,
                )
            else:
                await user_store.log_audit(
                    user_id=user.id,
                    team_id=user.team_id,
                    event_type="user_setting_changed",
                    ip=ip,
                    payload={
                        "key": change.key,
                        "before": change.before,
                        "after": change.after,
                        "origin": "chat",
                        "proposal_id": draft.id,
                        "op": op,
                    },
                )
        except Exception:
            logger.warning("config_proposal.audit_failed proposal=%s key=%s", draft.id, change.key, exc_info=True)


async def _execute(
    request: Request,
    user: User,
    user_store: UserStore,
    draft: ConfigDraft,
    *,
    op: str,
    from_status: str,
    to_status: str,
    replay_status: str,
) -> dict[str, Any]:
    if draft.status == replay_status:
        return _dto(draft, replayed=True)
    if draft.status != from_status:
        raise _error(409, "proposal_state_conflict", status=draft.status)
    await _require_apply_permission(request, user, user_store, draft)
    rows = (draft.payload or {}).get("rows") if isinstance(draft.payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise _error(409, "proposal_content_missing")
    reg = get_form(draft.form_id)
    assert reg is not None and reg.lite_applier is not None  # _require_apply_permission already guards this.
    fctx = _form_context(request, user, draft)
    try:
        changes = await reg.lite_applier(fctx, rows, op)
    except LiteApplyError as exc:
        raise _error(409, exc.code) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("config_proposal.%s_failed proposal=%s", op, draft.id, exc_info=True)
        raise _error(503, f"{op}_failed") from exc
    await _audit_changes(request, user, user_store, draft, changes, op)
    result = await _draft_store(request).transition(
        draft.id,
        team_id=user.team_id,
        owner_user_id=user.id,
        to_status=to_status,
        from_statuses=(from_status,),
    )
    if result.outcome in {"ok", "replay"} and result.draft is not None:
        # Lost concurrent CAS also returns replay: writes set an idempotent target value; duplicate writes harmless (tech design §5).
        return _dto(result.draft, replayed=result.outcome == "replay")
    if result.outcome == "conflict" and result.draft is not None:
        raise _error(409, "proposal_state_conflict", status=result.draft.status)
    raise _error(404, "config_proposal_not_found")


@router.get("/{proposal_id}")
async def get_proposal(
    proposal_id: str,
    request: Request,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """FE 终态 hydration：只回状态，渲染数据在事件 payload。"""
    return _dto(await _owned_lite_proposal(request, user, proposal_id))


@router.get("/{proposal_id}/model-review")
async def get_model_review(proposal_id: str, request: Request, user: User = Depends(_current_user)) -> dict[str, Any]:
    """Load a saved suggestion into the original model editor, without writing settings."""
    from bowhead_core.team_llm_config import redact_config

    from bowhead_gateway.configuration_team_models import merge_config, saved_config

    draft = await _owned_lite_proposal(request, user, proposal_id)
    if draft.form_id != "team_model_config" or draft.status != "proposed":
        raise _error(409, "proposal_state_conflict", status=draft.status)
    ctx = _form_context(request, user, draft)
    ctx.deps["user_store"] = _user_store_dep(request)
    config, version = await saved_config(ctx)
    rows = draft.payload["rows"]
    if not rows or any(row.get("baseline_revision") != version for row in rows):
        raise _error(409, "config_version_conflict")
    candidate = merge_config(config, {row["field"]: row["to"] for row in rows})
    return {"proposal_id": draft.id, "version": version, "config": redact_config(candidate), "rows": rows}


@router.get("/{proposal_id}/settings-review")
async def get_settings_review(
    proposal_id: str,
    request: Request,
    user: User = Depends(_current_user),
    user_store: UserStore = Depends(_user_store_dep),
) -> dict[str, Any]:
    """Read an owned Space suggestion and baseline; staging performs no writes."""

    from bowhead_gateway.config_copilot_forms import WorkspaceI18nDraftDTO
    from bowhead_gateway.configuration_workspace_metadata import WorkspaceMetadataDraftDTO

    draft = await _owned_lite_proposal(request, user, proposal_id)
    if draft.form_id not in {"workspace_i18n", "workspace_metadata"}:
        raise _error(409, "configuration_form_not_admitted")
    await _require_apply_permission(request, user, user_store, draft)
    payload = draft.payload or {}
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows or len(rows) > 2:
        raise _error(409, "proposal_content_missing")
    try:
        patch = {row.get("field"): row.get("to") for row in rows if isinstance(row, dict)}
        if len(patch) != len(rows):
            raise ValueError("duplicate settings fields")
        if any(
            not isinstance(row.get("to"), str)
            or type(row.get("from_unset")) is not bool
            or (row.get("from") is not None and not isinstance(row.get("from"), str))
            for row in rows
        ):
            raise ValueError("invalid settings review row")
        if draft.form_id == "workspace_metadata":
            WorkspaceMetadataDraftDTO.model_validate(patch)
            if any(type(row.get("baseline_revision")) is not int or row["baseline_revision"] < 1 for row in rows):
                raise ValueError("Space metadata requires its original revision")
        else:
            WorkspaceI18nDraftDTO.model_validate(patch)
    except Exception as exc:
        raise _error(409, "proposal_content_invalid") from exc
    return {
        **_dto(draft),
        "session_id": draft.session_id,
        "turn_id": draft.turn_id,
        "workspace_name": payload.get("workspace_name"),
        "rows": rows,
        "notes": payload.get("notes") or {},
        "settings_target": payload.get("settings_target"),
    }


@router.post("/{proposal_id}/apply", dependencies=[Depends(require_not_readonly)])
async def apply_proposal(
    proposal_id: str,
    request: Request,
    user: User = Depends(_current_user),
    user_store: UserStore = Depends(_user_store_dep),
) -> dict[str, Any]:
    draft = await _owned_lite_proposal(request, user, proposal_id)
    return await _execute(
        request,
        user,
        user_store,
        draft,
        op="apply",
        from_status=LITE_STATUS_PROPOSED,
        to_status=LITE_STATUS_APPLIED,
        replay_status=LITE_STATUS_APPLIED,
    )


@router.post("/{proposal_id}/undo", dependencies=[Depends(require_not_readonly)])
async def undo_proposal(
    proposal_id: str,
    request: Request,
    user: User = Depends(_current_user),
    user_store: UserStore = Depends(_user_store_dep),
) -> dict[str, Any]:
    draft = await _owned_lite_proposal(request, user, proposal_id)
    return await _execute(
        request,
        user,
        user_store,
        draft,
        op="undo",
        from_status=LITE_STATUS_APPLIED,
        to_status=LITE_STATUS_REVERTED,
        replay_status=LITE_STATUS_REVERTED,
    )


@router.post("/{proposal_id}/ignore", dependencies=[Depends(require_not_readonly)])
async def ignore_proposal(
    proposal_id: str,
    request: Request,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    draft = await _owned_lite_proposal(request, user, proposal_id)
    if draft.status == LITE_STATUS_IGNORED:
        return _dto(draft, replayed=True)
    if draft.status != LITE_STATUS_PROPOSED:
        raise _error(409, "proposal_state_conflict", status=draft.status)
    result = await _draft_store(request).transition(
        draft.id,
        team_id=user.team_id,
        owner_user_id=user.id,
        to_status=LITE_STATUS_IGNORED,
        from_statuses=(LITE_STATUS_PROPOSED,),
    )
    if result.outcome in {"ok", "replay"} and result.draft is not None:
        return _dto(result.draft, replayed=result.outcome == "replay")
    if result.outcome == "conflict" and result.draft is not None:
        raise _error(409, "proposal_state_conflict", status=result.draft.status)
    raise _error(404, "config_proposal_not_found")


__all__ = ["router"]
