"""Feature-flagged invitation intent and password activation routes."""

from __future__ import annotations

import hashlib
import hmac
from typing import Literal

from bowhead_core.entitlements import EntitlementDenied
from bowhead_core.team_openapi_flags import team_openapi_enabled
from bowhead_core.user_store import User, UserStore, hash_password
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from bowhead_gateway._entitlement_gate import seat_denied_to_http
from bowhead_gateway.auth_mutation_security import require_same_origin_json
from bowhead_gateway.auth_policy import (
    AccountCreationPolicy,
    EmailVerificationPolicy,
    invite_only_activation_enabled,
    resolve_account_policy,
)
from bowhead_gateway.invite_flow import (
    InviteFlowError,
    exchange_invite_token,
    load_invite_context,
    next_invite_action,
)
from bowhead_gateway.public_url import public_app_path
from bowhead_gateway.routes.auth import (
    _authenticated_identity,
    _build_invite_result,
    _cookie_samesite,
    _cookie_secure,
    _enqueue_verify_email,
    _issue_token_pair,
    _jwt_secret,
    _multi_team_authz_enabled,
    _store,
    _validate_password_strength,
)

router = APIRouter()

_INVITE_INTENT_COOKIE = "bowhead_invite_intent"
_INVITE_INTENT_TTL_SECONDS = 15 * 60


class IntentExchangeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(..., min_length=1, max_length=5000)


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InviteActivateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(None, max_length=120)
    password: str = Field(..., min_length=8, max_length=200)


@router.get("/capabilities")
async def auth_capabilities(request: Request) -> dict:
    policy = resolve_account_policy(
        bootstrap_required=bool(getattr(request.app.state, "bootstrap_required", False)),
        deployment_mode_override=getattr(request.app.state, "deployment_mode", None),
    )
    return {
        "deployment_mode": policy.deployment_mode,
        "account_creation_policy": policy.account_creation.value,
        "email_verification_policy": policy.email_verification.value,
        "auth_methods": list(policy.auth_methods),
        "public_registration": policy.public_registration,
        "bootstrap_required": policy.bootstrap_required,
        "invite_activation": policy.invite_activation_enabled,
        # Whether this deployment serves team-level API credentials. Reported as its own fact
        # rather than left for the client to infer from deployment_mode: SaaS can serve it too,
        # and a client that guesses would hide a working surface (or advertise a 404 one).
        "team_openapi": team_openapi_enabled(),
    }


@router.post("/invites/intent")
async def create_invite_intent(
    body: IntentExchangeBody,
    request: Request,
    response: Response,
    store: UserStore = Depends(_store),
) -> dict:
    _require_feature()
    require_same_origin_json(request)
    ip = request.client.host if request.client else "unknown"
    if not await store.rate_limit_check(
        f"invite_intent_exchange:ip:{ip}",
        limit=30,
        window_s=60,
    ):
        raise HTTPException(429, {"code": "rate_limited"})
    try:
        raw_secret, preview = await exchange_invite_token(store, body.token)
        context = await load_invite_context(store, raw_secret=raw_secret)
    except InviteFlowError as exc:
        raise HTTPException(exc.status_code, {"code": exc.code}) from exc
    await bind_invite_public_entry(request, context.team_id)
    _set_invite_cookie(response, raw_secret)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return preview


@router.post("/invites/intent/continue")
async def continue_invite_intent(
    _body: EmptyBody,
    request: Request,
    store: UserStore = Depends(_store),
) -> dict:
    _require_feature()
    require_same_origin_json(request)
    raw_secret = _require_intent_cookie(request)
    try:
        context = await load_invite_context(store, raw_secret=raw_secret)
    except InviteFlowError as exc:
        raise HTTPException(exc.status_code, {"code": exc.code}) from exc
    await bind_invite_public_entry(request, context.team_id)
    if not await store.rate_limit_check(
        f"invite_intent_continue:{context.intent_hash}",
        limit=20,
        window_s=_INVITE_INTENT_TTL_SECONDS,
    ):
        raise HTTPException(429, {"code": "rate_limited"})
    viewer = await _optional_identity(request)
    return await next_invite_action(
        store,
        context,
        resolve_account_policy(
            deployment_mode_override=getattr(request.app.state, "deployment_mode", None),
        ),
        viewer,
        multi_team_enabled=_multi_team_authz_enabled(),
    )


@router.post("/invites/activate")
async def activate_invited_identity(
    body: InviteActivateBody,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    store: UserStore = Depends(_store),
) -> dict:
    _require_feature()
    require_same_origin_json(request)
    policy = resolve_account_policy(
        deployment_mode_override=getattr(request.app.state, "deployment_mode", None),
    )
    if policy.account_creation is AccountCreationPolicy.ADMIN_ONLY:
        raise HTTPException(403, {"code": "policy_blocked"})
    if policy.email_verification is EmailVerificationPolicy.TRUSTED_IDP_REQUIRED:
        raise HTTPException(403, {"code": "trusted_idp_required"})
    raw_secret = _require_intent_cookie(request)
    try:
        context = await load_invite_context(store, raw_secret=raw_secret)
    except InviteFlowError as exc:
        raise HTTPException(exc.status_code, {"code": exc.code}) from exc
    await bind_invite_public_entry(request, context.team_id)
    existing = await store.get_user_by_email(context.invited_email)
    if existing is not None:
        raise HTTPException(409, {"code": "identity_already_exists"})
    ip = request.client.host if request.client else "unknown"
    email_bucket = hmac.new(
        _jwt_secret(request).encode(),
        context.invited_email.encode(),
        hashlib.sha256,
    ).hexdigest()[:32]
    for bucket in (
        f"invite_activate:intent:{context.intent_hash}",
        f"invite_activate:ip:{ip}",
        f"invite_activate:email:{email_bucket}",
    ):
        if not await store.rate_limit_check(
            bucket,
            limit=5,
            window_s=_INVITE_INTENT_TTL_SECONDS,
        ):
            raise HTTPException(429, {"code": "rate_limited"})
    _validate_password_strength(body.password)
    team = await store.get_team(context.team_id)
    if team is None:
        raise HTTPException(401, {"code": "invite_invalid"})
    requires_verification = policy.email_verification is EmailVerificationPolicy.SMTP_REQUIRED
    try:
        user = await store.activate_invited_identity_atomic(
            intent_hash=context.intent_hash,
            invite_type=context.invite_type,
            invite_id=context.invite_id,
            token_version=context.token_version,
            team_id=context.team_id,
            email=context.invited_email,
            password_hash=hash_password(body.password),
            display_name=(body.display_name or "").strip() or None,
            tier=team.default_user_tier,
            email_verified=False,
            workspace_id=context.workspace_id,
            workspace_role=str(context.row.get("role") or "member"),
            invite_after_verify_next_route=(
                _build_invite_result(
                    invite_type=context.invite_type,
                    row=context.row,
                    requires_verification=False,
                    workspace_name=context.workspace_name,
                    team_id=context.team_id,
                    team_name=context.team_name,
                )["next_route"]
                if requires_verification
                else None
            ),
        )
    except EntitlementDenied as exc:
        raise seat_denied_to_http(exc) from exc
    except ValueError as exc:
        raise HTTPException(409, {"code": _activation_error_code(str(exc))}) from exc

    from bowhead_gateway.space_assignment_notifications import (
        resolve_space_assignment_if_ready,
    )

    await resolve_space_assignment_if_ready(
        request,
        store=store,
        team_id=context.team_id,
        user_id=user.id,
    )
    has_workspace_access = bool(
        await store.list_workspaces_for_user(
            user.id,
            team_id=context.team_id,
        )
    )
    if requires_verification:
        finalized_next_route = str(
            _build_invite_result(
                invite_type=context.invite_type,
                row=context.row,
                requires_verification=False,
                workspace_name=context.workspace_name,
                team_id=context.team_id,
                team_name=context.team_name,
                has_workspace_access=has_workspace_access,
            )["next_route"]
        )
        await store.set_user_preference_key(
            user.id,
            "invite_after_verify_next_route",
            finalized_next_route,
        )
    result = _build_invite_result(
        invite_type=context.invite_type,
        row=context.row,
        requires_verification=requires_verification,
        workspace_name=context.workspace_name,
        team_id=context.team_id,
        team_name=context.team_name,
        accept_status="activated",
        has_workspace_access=has_workspace_access,
    )
    if requires_verification:
        try:
            await _enqueue_verify_email(store, user.id, user.email, team_id=None)
        except Exception as exc:
            raise HTTPException(503, {"code": "verification_delivery_failed"}) from exc
    else:
        await _issue_token_pair(user, request, response, store)
        from bowhead_gateway.post_login_hooks import schedule_post_login_hooks

        await schedule_post_login_hooks(
            background_tasks,
            request,
            user=user,
            team_id=context.team_id,
            auth_method="invite_activation",
        )
    _clear_invite_cookie(response)
    await store.log_audit(
        user_id=user.id,
        team_id=context.team_id,
        event_type="invite_activation_success",
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        payload={"invite_type": context.invite_type, "auth_method": "password"},
    )
    return {
        "ok": True,
        "result": "verification_required" if requires_verification else "activated",
        "invite_result": result,
        "requires_verification": requires_verification,
    }


@router.post("/invites/intent/cancel")
async def cancel_invite_intent(
    _body: EmptyBody,
    request: Request,
    response: Response,
    store: UserStore = Depends(_store),
) -> dict:
    _require_feature()
    require_same_origin_json(request)
    raw_secret = request.cookies.get(_INVITE_INTENT_COOKIE)
    if raw_secret:
        await store.invalidate_invite_intent(raw_secret)
    _clear_invite_cookie(response)
    return {"ok": True}


def invite_cookie_value(request: Request) -> str:
    return _require_intent_cookie(request)


def clear_invite_cookie(response: Response) -> None:
    _clear_invite_cookie(response)


async def bind_invite_public_entry(
    request: Request,
    team_id: str,
) -> None:
    """Bind a verified invitation to its registered browser-visible entry."""
    from bowhead_gateway.public_entry_context import (
        bind_team_public_entry,
        current_public_access_context,
    )

    if current_public_access_context() is None:
        return
    await bind_team_public_entry(request, team_id=team_id)


async def _optional_identity(request: Request) -> User | None:
    try:
        return await _authenticated_identity(request)
    except HTTPException:
        return None


def _require_feature() -> None:
    if not invite_only_activation_enabled():
        raise HTTPException(404, {"code": "feature_disabled"})


def _require_intent_cookie(request: Request) -> str:
    raw_secret = request.cookies.get(_INVITE_INTENT_COOKIE)
    if not raw_secret:
        raise HTTPException(401, {"code": "invite_intent_missing"})
    return raw_secret


def _set_invite_cookie(response: Response, raw_secret: str) -> None:
    response.set_cookie(
        key=_INVITE_INTENT_COOKIE,
        value=raw_secret,
        httponly=True,
        secure=_cookie_secure(),
        samesite=_cookie_samesite(),  # type: ignore[arg-type]
        max_age=_INVITE_INTENT_TTL_SECONDS,
        path=public_app_path("/"),
    )


def _clear_invite_cookie(response: Response) -> None:
    response.delete_cookie(
        key=_INVITE_INTENT_COOKIE,
        path=public_app_path("/"),
        secure=_cookie_secure(),
        samesite=_cookie_samesite(),  # type: ignore[arg-type]
    )


def _activation_error_code(
    message: str,
) -> Literal[
    "invite_invalid",
    "invite_consumed",
    "identity_already_exists",
]:
    if "already registered" in message:
        return "identity_already_exists"
    if "consumed" in message:
        return "invite_consumed"
    return "invite_invalid"
