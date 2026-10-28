"""Admin credits routes — V3 MVP.

7 endpoints + 1 CSV. 全 credit capability gate.
聚合查询走 in-process LRU cache (1min TTL).
写操作 invalidate team cache + audit log.

设计文档: docs/V3-admin-credits-page-design.md (PRD v2)
        + docs/V3-admin-credits-tech-design.md (技术 v1).
"""

from __future__ import annotations

import copy
import csv
import io
import json
import logging
from datetime import date
from typing import TYPE_CHECKING, Any, Literal

from bowhead_core.credit_activation import (
    CreditTeamContext,
    decorate_credit_attention_actions,
)
from bowhead_core.credit_admin_store import (
    cache_get,
    cache_invalidate_team,
    cache_set,
    idempotency_check,
    idempotency_record,
)
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.routes._deps import (
    CreditAdminPrincipal,
    require_credit_capability,
    require_credit_read,
)
from bowhead_gateway.routes.auth import _store

if TYPE_CHECKING:
    from bowhead_core.user_store import UserStore

router = APIRouter()
logger = logging.getLogger(__name__)
_STORE_DEP = Depends(_store)
_CREDIT_READ_DEP = Depends(require_credit_read)
_CREDIT_GRANT_DEP = Depends(require_credit_capability("credit_grant"))
_CREDIT_PERIOD_RESET_DEP = Depends(require_credit_capability("credit_period_reset"))
_CREDIT_ATTENTION_DISMISS_DEP = Depends(require_credit_capability("credit_attention_dismiss"))


# ─── Pydantic models ─────────────────────────────


class PatchUserBody(BaseModel):
    tier: Literal["pro", "ultra"] | None = None
    credit_quota: int | None = Field(None, ge=1, le=999_999_999)
    credit_balance: int | None = Field(None, ge=0, le=999_999_999)
    reset_quota_with_tier: bool = True
    pull_balance_to_quota: bool = False


class GrantBody(BaseModel):
    amount: int = Field(..., ge=1, le=1_000_000)
    reason: str | None = Field(None, max_length=200)
    idempotency_key: str = Field(..., min_length=8, max_length=64)


class PeriodResetBody(BaseModel):
    mode: Literal["balance_only", "balance_and_quota"]
    confirm: bool = False


class AttentionStateBody(BaseModel):
    state: Literal["dismissed"]
    reason: str | None = Field(None, max_length=200)


class AttentionActionClickBody(BaseModel):
    action_key: str = Field(..., min_length=1, max_length=80)
    source_surface: str = Field(..., min_length=1, max_length=40)
    allowed: bool = True
    disabled_reason: str | None = Field(None, max_length=120)


def _credit_capability_denied(capability: str) -> HTTPException:
    return HTTPException(
        403,
        {"code": "insufficient_credit_capability", "capability": capability},
    )


def _credit_error(status_code: int, code: str) -> HTTPException:
    """Return a code-only user-facing error; the shared handler localizes it."""
    return HTTPException(status_code, {"code": code})


def _decorate_summary_for_actor(
    data: dict[str, Any],
    principal: CreditAdminPrincipal,
) -> dict[str, Any]:
    out = copy.deepcopy(data)
    team = out.get("team") or {}
    team_context = CreditTeamContext(
        team_id=principal.user.team_id,
        team_credit_balance=int(team.get("balance") or 0),
        team_credit_quota=int(team.get("quota") or 0),
    )
    out["credit_actor"] = {
        "team_role": principal.team_role,
        "capabilities": principal.capabilities.to_dict(),
    }
    out["activation_attention"] = [
        decorate_credit_attention_actions(item, principal.capabilities, team_context) for item in out.get("activation_attention", [])
    ]
    return out


async def _mark_summary_attention_shown(
    store: UserStore,
    *,
    request: Request,
    team_id: str,
    actor_user_id: str,
    summary: dict[str, Any],
) -> None:
    await _mark_attention_items_shown(
        store,
        request=request,
        team_id=team_id,
        actor_user_id=actor_user_id,
        items=list(summary.get("activation_attention") or []),
        surface="summary",
    )


async def _mark_attention_items_shown(
    store: UserStore,
    *,
    request: Request,
    team_id: str,
    actor_user_id: str,
    items: list[dict[str, Any]],
    surface: str,
) -> None:
    visible_items = [item for item in items if item and item.get("attention_item_id")]
    if not visible_items:
        return
    await store.mark_credit_attention_items_shown(
        team_id,
        [str(item["attention_item_id"]) for item in visible_items],
    )
    for item in visible_items:
        await _track_attention_event(
            request,
            "credit.attention.item_shown",
            actor_user_id=actor_user_id,
            item=item,
            surface=surface,
        )


def _attention_metric_attrs(
    item: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    if item:
        for key in (
            "attention_item_id",
            "dedupe_key",
            "policy_key",
            "team_id",
            "user_id",
            "workspace_id",
            "primary_segment",
            "severity",
            "expires_at",
        ):
            value = item.get(key)
            if value is not None:
                attrs[key] = value
        flags = item.get("segment_flags")
        if isinstance(flags, list):
            attrs["flags"] = ",".join(str(flag) for flag in flags[:8])
        action = item.get("recommended_action") or {}
        action_key = action.get("key") or item.get("recommended_action_key")
        if action_key:
            attrs["action_key"] = action_key
    attrs.update({key: value for key, value in extra.items() if value is not None})
    return attrs


async def _track_attention_event(
    request: Request,
    name: str,
    *,
    actor_user_id: str,
    item: dict[str, Any] | None = None,
    **attrs: Any,
) -> None:
    metrics_log = getattr(request.app.state, "metrics_log", None)
    if metrics_log is None:
        return
    await metrics_log.track(
        name,
        user_id=actor_user_id,
        attrs=_attention_metric_attrs(item, **attrs),
    )


def _decorate_user_rows_for_actor(
    data: dict[str, Any],
    principal: CreditAdminPrincipal,
    team_context: CreditTeamContext,
) -> dict[str, Any]:
    out = copy.deepcopy(data)
    for row in out.get("items", []):
        item = row.get("activation_attention")
        if not item:
            row["primary_segment"] = None
            row["segment_flags"] = []
            row["recommended_action"] = None
            continue
        decorated = decorate_credit_attention_actions(item, principal.capabilities, team_context)
        row["activation_attention"] = decorated
        row["primary_segment"] = decorated.get("primary_segment")
        row["segment_flags"] = decorated.get("segment_flags") or []
        row["recommended_action"] = decorated.get("recommended_action")
        row["recommended_action_key"] = decorated.get("recommended_action_key")
    return out


async def _team_context_for_store(store: UserStore, team_id: str) -> CreditTeamContext:
    team = await store.get_team(team_id)
    return CreditTeamContext(
        team_id=team_id,
        team_credit_balance=int(getattr(team, "team_credit_balance", 0) if team else 0),
        team_credit_quota=int(getattr(team, "team_credit_quota", 0) if team else 0),
    )


async def _mark_attention_acted_best_effort(
    store: UserStore,
    *,
    team_id: str,
    actor_user_id: str,
    action_type: str,
    user_id: str | None = None,
    policy_keys: list[str] | None = None,
) -> dict[str, Any]:
    try:
        return await store.mark_credit_attention_acted(
            team_id=team_id,
            actor_user_id=actor_user_id,
            action_type=action_type,
            user_id=user_id,
            policy_keys=policy_keys,
        )
    except Exception as exc:
        logger.warning(
            "credit_attention.mark_acted_failed",
            extra={"team_id": team_id, "action_type": action_type, "err": str(exc)},
        )
        return {"acted_count": 0, "attention_item_ids": []}


async def _track_attention_acted_result(
    request: Request,
    store: UserStore,
    *,
    team_id: str,
    actor_user_id: str,
    action_type: str,
    result: dict[str, Any],
) -> None:
    for item_id in result.get("attention_item_ids", []):
        item = await store.get_credit_attention_item(team_id, str(item_id))
        await _track_attention_event(
            request,
            "credit.attention.acted",
            actor_user_id=actor_user_id,
            item=item or {"attention_item_id": item_id, "team_id": team_id},
            action_type=action_type,
            linked_event_type=action_type,
        )


# ─── Endpoints ───────────────────────────────────


@router.get("/summary")
async def get_summary(
    request: Request,
    principal: CreditAdminPrincipal = _CREDIT_READ_DEP,
    store: UserStore = _STORE_DEP,
) -> dict[str, Any]:
    """team credit 总览. 聚合 SQL last 30d. 缓存 1 min."""
    me = principal.user
    cache_key = f"{me.team_id}:summary"
    cached = await cache_get(cache_key)
    if cached:
        out = _decorate_summary_for_actor(cached, principal)
        await _mark_summary_attention_shown(
            store,
            request=request,
            team_id=me.team_id,
            actor_user_id=me.id,
            summary=out,
        )
        return out
    data = await store.credits_summary(me.team_id)
    await cache_set(cache_key, data)
    out = _decorate_summary_for_actor(data, principal)
    await _mark_summary_attention_shown(
        store,
        request=request,
        team_id=me.team_id,
        actor_user_id=me.id,
        summary=out,
    )
    return out


@router.get("/users/{user_id}/info")
async def get_user_info(
    user_id: str,
    principal: CreditAdminPrincipal = _CREDIT_READ_DEP,
    store: UserStore = _STORE_DEP,
) -> dict[str, Any]:
    """单 user 详情 (drill-down 用). 跨 team 404."""
    me = principal.user
    user = await store.get_user(user_id)
    if not user or user.team_id != me.team_id:
        raise _credit_error(404, "member_not_found")
    items, _ = await store.credits_users_list(
        me.team_id,
        page=1,
        size=1,
        user_id=user_id,
        include_removed=True,
    )
    if not items:
        raise _credit_error(404, "member_not_found")
    team_context = await _team_context_for_store(store, me.team_id)
    out = _decorate_user_rows_for_actor({"items": items}, principal, team_context)
    return out["items"][0]


@router.get("/users")
async def list_users(
    request: Request,
    page: int = 1,
    size: int = 20,
    sort: str = "attention",
    q: str | None = None,
    tier: str | None = None,
    workspace: str | None = None,
    include_removed: bool = False,
    principal: CreditAdminPrincipal = _CREDIT_READ_DEP,
    store: UserStore = _STORE_DEP,
) -> dict[str, Any]:
    """分页 user list with last 30d 消耗 + attention score."""
    me = principal.user
    if size < 1 or size > 200:
        raise _credit_error(422, "invalid_credit_pagination")
    if page < 1:
        raise _credit_error(422, "invalid_credit_pagination")
    cache_key = f"{me.team_id}:users:{page}:{size}:{sort}:{q}:{tier}:{workspace}:{include_removed}"
    team_context = await _team_context_for_store(store, me.team_id)
    cached = await cache_get(cache_key)
    if cached:
        out = _decorate_user_rows_for_actor(cached, principal, team_context)
        await _mark_attention_items_shown(
            store,
            request=request,
            team_id=me.team_id,
            actor_user_id=me.id,
            items=[row["activation_attention"] for row in out.get("items", []) if row.get("activation_attention")],
            surface="users",
        )
        return out
    items, total = await store.credits_users_list(
        me.team_id,
        page=page,
        size=size,
        sort=sort,
        q=q,
        tier=tier,
        workspace_id=workspace,
        include_removed=include_removed,
    )
    out = {"items": items, "total": total, "page": page, "size": size}
    await cache_set(cache_key, out)
    decorated = _decorate_user_rows_for_actor(out, principal, team_context)
    await _mark_attention_items_shown(
        store,
        request=request,
        team_id=me.team_id,
        actor_user_id=me.id,
        items=[row["activation_attention"] for row in decorated.get("items", []) if row.get("activation_attention")],
        surface="users",
    )
    return decorated


@router.patch("/users/{user_id}", dependencies=[Depends(require_not_readonly)])
async def patch_user(
    user_id: str,
    body: PatchUserBody,
    request: Request,
    principal: CreditAdminPrincipal = _CREDIT_READ_DEP,
    store: UserStore = _STORE_DEP,
) -> dict[str, Any]:
    """改 user tier/quota/balance + 联动. 跨 team 403."""
    me = principal.user
    if body.tier is None and body.credit_quota is None and body.credit_balance is None:
        raise _credit_error(400, "credit_changes_required")
    if body.tier is not None and not principal.capabilities.credit_user_tier_write:
        raise _credit_capability_denied("credit_user_tier_write")
    if (body.credit_quota is not None or body.credit_balance is not None) and not principal.capabilities.credit_user_quota_write:
        raise _credit_capability_denied("credit_user_quota_write")
    before = await store.get_user(user_id)
    if before is None or before.team_id != me.team_id:
        raise _credit_error(404, "member_not_found")
    if before.status != "active":
        raise _credit_error(409, "credit_user_inactive")
    try:
        after = await store.admin_set_user_credit(
            user_id=user_id,
            team_id=me.team_id,
            by_user_id=me.id,
            tier=body.tier,
            credit_quota=body.credit_quota,
            credit_balance=body.credit_balance,
            reset_quota_with_tier=body.reset_quota_with_tier,
            pull_balance_to_quota=body.pull_balance_to_quota,
        )
    except ValueError as e:
        msg = str(e)
        if msg == "user_not_found_or_cross_team":
            raise _credit_error(404, "member_not_found") from e
        if msg == "user_removed_cannot_edit":
            raise _credit_error(409, "credit_user_inactive") from e
        logger.exception(
            "admin_credits.patch_user_failed",
            extra={"team_id": me.team_id, "target_user_id": user_id},
        )
        raise _credit_error(500, "E_USER_VISIBLE_GENERIC") from e
    tier_changed = after["tier"] != before.tier
    quota_changed = int(after["credit_quota"]) != int(before.credit_quota)
    balance_changed = int(after["credit_balance"]) != int(before.credit_balance)
    if tier_changed or quota_changed or balance_changed:
        action_type = "edit_user_tier" if tier_changed else "edit_user_quota"
        acted_result = await _mark_attention_acted_best_effort(
            store,
            team_id=me.team_id,
            actor_user_id=me.id,
            action_type=action_type,
            user_id=user_id,
        )
        await _track_attention_acted_result(
            request,
            store,
            team_id=me.team_id,
            actor_user_id=me.id,
            action_type=action_type,
            result=acted_result,
        )
    await cache_invalidate_team(me.team_id)
    return {"ok": True, "user": after}


@router.post("/users/{user_id}/grant", dependencies=[Depends(require_not_readonly)])
async def grant_user(
    user_id: str,
    body: GrantBody,
    request: Request,
    principal: CreditAdminPrincipal = _CREDIT_GRANT_DEP,
    store: UserStore = _STORE_DEP,
) -> dict[str, Any]:
    """给 user 加 credit (从 team pool 调拨). idempotency_key 24h 去重."""
    me = principal.user
    # idempotency check
    seen = await idempotency_check(body.idempotency_key)
    if seen is not None:
        return seen

    # Check user exists + same team (admin_grant lacks strict team checks; give friendly 4xx here).
    user = await store.get_user(user_id)
    if not user or user.team_id != me.team_id:
        raise _credit_error(404, "member_not_found")
    if user.status != "active":
        raise _credit_error(409, "credit_user_inactive")

    try:
        new_balance = await store.admin_grant(
            user_id=user_id,
            team_id=me.team_id,
            amount=body.amount,
            by_user_id=me.id,
            reason_note=body.reason,
        )
    except ValueError as e:
        if str(e) == "team_pool_insufficient":
            raise _credit_error(409, "credit_team_pool_insufficient") from e
        if str(e) == "user_not_active":
            raise _credit_error(409, "credit_user_inactive") from e
        logger.exception(
            "admin_credits.grant_failed",
            extra={"team_id": me.team_id, "target_user_id": user_id},
        )
        raise _credit_error(500, "E_USER_VISIBLE_GENERIC") from e

    result = {"ok": True, "new_balance": new_balance}
    acted_result = await _mark_attention_acted_best_effort(
        store,
        team_id=me.team_id,
        actor_user_id=me.id,
        action_type="grant_credit",
        user_id=user_id,
    )
    await _track_attention_acted_result(
        request,
        store,
        team_id=me.team_id,
        actor_user_id=me.id,
        action_type="grant_credit",
        result=acted_result,
    )
    await idempotency_record(body.idempotency_key, result)
    await cache_invalidate_team(me.team_id)
    return result


@router.post("/period-reset", dependencies=[Depends(require_not_readonly)])
async def period_reset(
    body: PeriodResetBody,
    request: Request,
    principal: CreditAdminPrincipal = _CREDIT_PERIOD_RESET_DEP,
    store: UserStore = _STORE_DEP,
) -> dict[str, Any]:
    """周期 reset. confirm=False → preview; True → 真做 + audit snapshot.

    Cooldown: 24h 内同 team 第二次 → 429 + Retry-After.
    """
    me = principal.user
    try:
        out = await store.admin_period_reset(
            team_id=me.team_id,
            by_user_id=me.id,
            mode=body.mode,
            confirm=body.confirm,
        )
    except RuntimeError as e:
        msg = str(e)
        if msg.startswith("cooldown:"):
            retry_after = int(msg.split(":", 1)[1])
            raise HTTPException(
                429,
                {"code": "credit_period_reset_cooldown"},
                headers={"Retry-After": str(retry_after)},
            ) from e
        logger.exception(
            "admin_credits.period_reset_failed",
            extra={"team_id": me.team_id},
        )
        raise _credit_error(500, "E_USER_VISIBLE_GENERIC") from e
    except ValueError as e:
        if str(e) == "team_not_found":
            raise _credit_error(404, "team_not_found") from e
        logger.exception(
            "admin_credits.period_reset_invalid",
            extra={"team_id": me.team_id},
        )
        raise _credit_error(500, "E_USER_VISIBLE_GENERIC") from e

    if body.confirm:
        acted_result = await _mark_attention_acted_best_effort(
            store,
            team_id=me.team_id,
            actor_user_id=me.id,
            action_type="period_reset",
        )
        await _track_attention_acted_result(
            request,
            store,
            team_id=me.team_id,
            actor_user_id=me.id,
            action_type="period_reset",
            result=acted_result,
        )
        await cache_invalidate_team(me.team_id)
    return out


@router.post("/attention/{attention_item_id}/action-clicked")
async def track_attention_action_clicked(
    attention_item_id: str,
    body: AttentionActionClickBody,
    request: Request,
    principal: CreditAdminPrincipal = _CREDIT_READ_DEP,
    store: UserStore = _STORE_DEP,
) -> dict[str, bool]:
    """Track that an admin clicked an activation attention recommendation."""
    me = principal.user
    item = await store.get_credit_attention_item(me.team_id, attention_item_id)
    if item is None:
        raise _credit_error(404, "credit_attention_not_found")
    await _track_attention_event(
        request,
        "credit.attention.action_clicked",
        actor_user_id=me.id,
        item=item,
        action_key=body.action_key,
        source_surface=body.source_surface,
        allowed=body.allowed,
        disabled_reason=body.disabled_reason,
    )
    return {"ok": True}


@router.post("/attention/{attention_item_id}/state", dependencies=[Depends(require_not_readonly)])
async def update_attention_state(
    attention_item_id: str,
    body: AttentionStateBody,
    request: Request,
    principal: CreditAdminPrincipal = _CREDIT_ATTENTION_DISMISS_DEP,
    store: UserStore = _STORE_DEP,
) -> dict[str, Any]:
    """Dismiss an activation attention item.

    Acted/protected are outcome states: admin write routes mark acted after a real
    action, and the materializer promotes to protected only after guard checks.
    """
    me = principal.user
    before = await store.get_credit_attention_item(me.team_id, attention_item_id)
    try:
        item = await store.update_credit_attention_state(
            team_id=me.team_id,
            attention_item_id=attention_item_id,
            state=body.state,
            actor_user_id=me.id,
            reason=body.reason,
        )
    except ValueError as e:
        msg = str(e)
        if msg == "attention_item_not_found":
            raise _credit_error(404, "credit_attention_not_found") from e
        if msg == "dismiss_reason_required":
            raise _credit_error(422, "credit_attention_reason_required") from e
        logger.exception(
            "admin_credits.attention_update_failed",
            extra={
                "team_id": me.team_id,
                "attention_item_id": attention_item_id,
            },
        )
        raise _credit_error(500, "E_USER_VISIBLE_GENERIC") from e
    if before is not None and before.get("state") == "open":
        await _track_attention_event(
            request,
            "credit.attention.dismissed",
            actor_user_id=me.id,
            item=item,
            reason=body.reason,
        )
    await cache_invalidate_team(me.team_id)
    return {"ok": True, "item": item}


@router.get("/users/{user_id}/ledger")
async def get_user_ledger(
    user_id: str,
    page: int = 1,
    size: int = 50,
    from_: str | None = None,
    to: str | None = None,
    type: str | None = None,  # noqa: A002
    principal: CreditAdminPrincipal = _CREDIT_READ_DEP,
    store: UserStore = _STORE_DEP,
) -> dict[str, Any]:
    """drill-down: 单用户 ledger (team-scoped 隔离)."""
    me = principal.user
    if size < 1 or size > 500:
        raise _credit_error(422, "invalid_credit_pagination")
    items, total = await store.credits_user_ledger(
        user_id,
        team_id=me.team_id,
        page=page,
        size=size,
        from_ts=from_,
        to_ts=to,
        type_filter=type,
    )
    return {"items": items, "total": total, "page": page, "size": size}


@router.get("/export.csv")
async def export_csv(
    request: Request,
    scope: Literal["summary", "users", "user_ledger", "audit"],
    user_id: str | None = None,
    from_: str | None = None,
    to: str | None = None,
    principal: CreditAdminPrincipal = _CREDIT_READ_DEP,
    store: UserStore = _STORE_DEP,
) -> Response:
    """Stream ordinary credit CSVs or delegate audit export to the Business adapter."""
    me = principal.user
    if scope == "user_ledger" and not user_id:
        raise _credit_error(422, "credit_user_id_required")
    if scope == "audit":
        extensions = request.app.state.gateway_extensions
        if extensions.audit_csv_exporter is None:
            raise HTTPException(404, {"code": "feature_unavailable"})
        return await extensions.audit_csv_exporter(
            request=request,
            principal=principal,
            store=store,
            from_ts=from_,
            to_ts=to,
        )

    fname = f"bowhead-credits-{scope}-{(me.team_id or 'unknown')[:8]}-{date.today().isoformat()}.csv"

    async def stream_csv():
        buf = io.StringIO()
        writer = csv.writer(buf)

        def flush() -> str:
            v = buf.getvalue()
            buf.seek(0)
            buf.truncate()
            return v

        if scope == "summary":
            data = await store.credits_summary(me.team_id)
            writer.writerow(["metric", "value"])
            writer.writerow(["team_balance", data["team"]["balance"]])
            writer.writerow(["team_quota", data["team"]["quota"]])
            writer.writerow(["team_period_start", data["team"]["period_start"] or ""])
            writer.writerow(["last_reset_at", data["team"]["last_reset_at"] or ""])
            writer.writerow(["last_30d_spend", data["last_30d"]["spend"]])
            writer.writerow(["last_30d_turns", data["last_30d"]["turns"]])
            writer.writerow(["last_30d_tokens_in", data["last_30d"]["tokens_in"]])
            writer.writerow(["last_30d_tokens_out", data["last_30d"]["tokens_out"]])
            writer.writerow(["active_users_7d", data["active_users_7d"]])
            writer.writerow(["total_users_active", data["total_users_active"]])
            yield flush()

        elif scope == "users":
            writer.writerow(
                [
                    "user_id",
                    "email",
                    "display_name",
                    "tier",
                    "status",
                    "credit_balance",
                    "credit_quota",
                    "spend_30d",
                    "last_activity_at",
                ]
            )
            yield flush()
            page = 1
            while True:
                items, total = await store.credits_users_list(
                    me.team_id,
                    page=page,
                    size=200,
                    sort="email",
                    include_removed=True,
                )
                for it in items:
                    writer.writerow(
                        [
                            it["user_id"],
                            it["email"],
                            it["display_name"] or "",
                            it["tier"],
                            it["status"],
                            it["credit_balance"],
                            it["credit_quota"],
                            it["spend_30d"],
                            it["last_activity_at"] or "",
                        ]
                    )
                    yield flush()
                if page * 200 >= total:
                    break
                page += 1

        elif scope == "user_ledger":
            writer.writerow(
                [
                    "id",
                    "type",
                    "workspace_id",
                    "turn_id_short",
                    "models",
                    "model_usage_json",
                    "tokens_input",
                    "tokens_output",
                    "delta",
                    "created_at",
                ]
            )
            yield flush()
            page = 1
            while True:
                items, total = await store.credits_user_ledger(
                    user_id,
                    team_id=me.team_id,
                    page=page,
                    size=200,
                    from_ts=from_,
                    to_ts=to,
                )
                for it in items:
                    model_usage = it.get("model_usage") or []
                    models = "; ".join(
                        sorted(
                            {
                                f"{line['provider']}/{line['model']}" if line.get("provider") else str(line["model"])
                                for line in model_usage
                                if line.get("model")
                            }
                        )
                    )
                    writer.writerow(
                        [
                            it["id"],
                            it["type"],
                            it["workspace_id"] or "",
                            it["turn_id_short"] or "",
                            f"'{models}" if models.startswith(("=", "+", "-", "@")) else models,
                            json.dumps(model_usage, ensure_ascii=False, separators=(",", ":")),
                            it["tokens_input"],
                            it["tokens_output"],
                            it["delta"],
                            it["created_at"],
                        ]
                    )
                    yield flush()
                if page * 200 >= total:
                    break
                page += 1

    return StreamingResponse(
        stream_csv(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
