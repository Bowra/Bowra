"""P4.5.4 — Feishu OAuth.

Flow:
  GET  /api/v1/auth/oauth/{provider}/login    → redirect to provider
  GET  /api/v1/auth/oauth/{provider}/callback → exchange code → user → JWT cookie

Provider credentials:
  BOWHEAD_OAUTH_FEISHU_APP_ID / APP_SECRET

Callback URLs come from the verified request-scoped public entry.
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import UTC
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx2
from bowhead_core.entitlements import EntitlementDenied
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from starlette.responses import RedirectResponse

from bowhead_gateway._entitlement_gate import seat_denied_to_http
from bowhead_gateway.public_entry_context import (
    bind_team_public_entry,
    current_public_access_context,
    public_access_context_for_entry,
    set_public_access_context,
)
from bowhead_gateway.routes.auth import _safe_next_path, _store

if TYPE_CHECKING:
    from bowhead_core.user_store import UserStore

router = APIRouter()
logger = logging.getLogger(__name__)


_PROVIDERS: dict[str, dict[str, str]] = {
    "feishu": {
        "auth_url": "https://open.feishu.cn/open-apis/authen/v1/authorize",
        "token_url": "https://open.feishu.cn/open-apis/authen/v2/oauth/token",
        "userinfo_url": "https://open.feishu.cn/open-apis/authen/v1/user_info",
        # contact:user.email:readonly retrieves the actual email (enable the contacts API in Feishu administration).
        # Disabled contacts API does not block login: callback falls back to {open_id}@feishu.local.
        "scope": "contact:user.email:readonly",
    },
}

_OAUTH_PUBLIC_ERROR_CODES = frozenset(
    {
        "clock_tamper",
        "federated_identity_conflict",
        "feature_disabled",
        "identity_blocked",
        "invite_accept_failed",
        "invite_activation_failed",
        "invite_consumed",
        "invite_cross_team_not_allowed",
        "invite_email_mismatch",
        "invite_expired",
        "invite_intent_missing",
        "invite_invalid",
        "invite_revoked",
        "needs_license",
        "oauth_authorization_incomplete",
        "oauth_callback_failed",
        "oauth_identity_missing",
        "oauth_not_configured",
        "oauth_provider_unsupported",
        "oauth_registration_disabled",
        "oauth_state_invalid",
        "oauth_token_exchange_failed",
        "oauth_userinfo_failed",
        "over_grace",
        "policy_blocked",
        "provider_email_untrusted",
        "public_entry_team_mismatch",
        "rate_limited",
        "seat_exceeded",
        "suspended",
        "tamper",
        "trusted_idp_required",
        "whitelist_domain_no_team_provisioned",
    },
)


@dataclass(frozen=True)
class TrustedProviderIdentity:
    provider: str
    subject: str
    email: str
    display_name: str | None
    avatar_url: str | None


def _trusted_provider_identity(
    *,
    provider: str,
    subject: str,
    email: str,
    email_verified: bool,
    display_name: str | None,
    avatar_url: str | None,
) -> TrustedProviderIdentity:
    if not subject or not email or not email_verified or email.endswith("@feishu.local"):
        raise HTTPException(403, {"code": "provider_email_untrusted"})
    return TrustedProviderIdentity(
        provider=provider,
        subject=subject,
        email=email.lower().strip(),
        display_name=display_name,
        avatar_url=avatar_url,
    )


def _client_id(provider: str) -> str:
    if provider == "feishu":
        return os.environ.get("BOWHEAD_OAUTH_FEISHU_APP_ID") or ""
    raise ValueError(provider)


def _client_secret(provider: str) -> str:
    if provider == "feishu":
        return os.environ.get("BOWHEAD_OAUTH_FEISHU_APP_SECRET") or ""
    raise ValueError(provider)


def _provider_configured(provider: str) -> bool:
    from bowhead_gateway.auth_policy import configured_oauth_providers

    return provider in configured_oauth_providers()


def _redirect_uri(provider: str) -> str:
    from bowhead_gateway.public_url import public_app_url

    return public_app_url(f"/api/v1/auth/oauth/{provider}/callback")


def _oauth_return_path(path: str) -> str:
    from bowhead_gateway.public_url import oauth_return_path

    return oauth_return_path(path, callback_path="/api/v1/auth/oauth/provider/callback")


async def _bind_login_public_entry(request: Request) -> None:
    """Select the local team's entry before creating a login callback URL."""
    if getattr(request.app.state, "deployment_mode", None) != "local":
        return
    teams = await _store(request).list_teams()
    if len(teams) == 1 and teams[0].status == "active":
        try:
            await bind_team_public_entry(request, team_id=teams[0].id)
        except Exception:
            logger.exception("oauth.public_entry_unavailable team_id=%s", teams[0].id)


def _invite_register_path_from_next(next_path: str | None) -> str | None:
    safe_path = _safe_next_path(next_path)
    if not safe_path:
        return None
    parsed = urlsplit(safe_path)
    if parsed.path != "/invite":
        return None
    qs = parse_qs(parsed.query)
    token = (qs.get("token") or qs.get("invite") or [""])[0]
    params = {"source": "invite"}
    if token:
        params["token"] = token
    return f"/register?{urlencode(params)}"


async def _store_state(
    db_path: str,
    state: str,
    provider: str,
    next_path: str | None = None,
    public_entry_id: str | None = None,
    *,
    flow_kind: str = "normal",
    invite_intent_hash: str | None = None,
    redirect_uri: str | None = None,
) -> None:
    """P4.5.15: persist OAuth state in sqlite (multi-worker safe)."""
    from datetime import datetime, timedelta

    import aiosqlite

    now = datetime.now(UTC).isoformat()
    expires = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT OR REPLACE INTO oauth_state("
            "state,provider,next_path,public_entry_id,expires_at,created_at,"
            "flow_kind,invite_intent_hash,redirect_uri"
            ") VALUES (?,?,?,?,?,?,?,?,?)",
            (
                state,
                provider,
                next_path,
                public_entry_id,
                expires,
                now,
                flow_kind,
                invite_intent_hash,
                redirect_uri,
            ),
        )
        await db.execute("DELETE FROM oauth_state WHERE expires_at < ?", (now,))
        await db.commit()


async def _consume_state(
    db_path: str,
    state: str,
    provider: str,
) -> tuple[bool, str | None, str | None, str, str | None]:
    valid, next_path, entry_id, flow_kind, intent_hash, _ = await _consume_state_snapshot(db_path, state, provider)
    return valid, next_path, entry_id, flow_kind, intent_hash


async def _consume_state_snapshot(
    db_path: str,
    state: str,
    provider: str,
) -> tuple[bool, str | None, str | None, str, str | None, str | None]:
    """P4.5.15: verify + delete state (one-shot use)."""
    from datetime import datetime

    import aiosqlite

    now = datetime.now(UTC).isoformat()
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "DELETE FROM oauth_state WHERE state = ? RETURNING provider,expires_at,next_path,public_entry_id,flow_kind,invite_intent_hash,redirect_uri",
            (state,),
        ) as cur:
            row = await cur.fetchone()
        await db.commit()
        if not row or row[0] != provider or row[1] < now:
            return False, None, None, "normal", None, None
        next_path = _safe_next_path(row[2])
        public_entry_id = str(row[3]) if row[3] else None
        flow_kind = str(row[4] or "normal")
        invite_intent_hash = str(row[5]) if row[5] else None
        redirect_uri = str(row[6]) if row[6] else None
    return True, next_path, public_entry_id, flow_kind, invite_intent_hash, redirect_uri


@router.get("/{provider}/login")
async def oauth_login(
    provider: str,
    request: Request,
    next_path: str | None = Query(default=None, alias="next"),
    flow: str = Query(default="normal"),
) -> dict:
    if provider not in _PROVIDERS:
        raise HTTPException(404, {"code": "oauth_provider_unsupported"})
    cfg = _PROVIDERS[provider]
    if not _provider_configured(provider):
        raise HTTPException(503, {"code": "oauth_not_configured"})
    cid = _client_id(provider)
    if flow not in {"normal", "invite"}:
        raise HTTPException(422, {"code": "invalid_oauth_flow"})
    state = secrets.token_urlsafe(16)
    store: UserStore = request.app.state.user_store
    flow_kind = "invite" if flow == "invite" else "normal"
    invite_intent_hash: str | None = None
    if flow_kind == "invite":
        from bowhead_gateway.auth_policy import invite_only_activation_enabled
        from bowhead_gateway.invite_flow import InviteFlowError, load_invite_context
        from bowhead_gateway.routes.invite_activation import (
            bind_invite_public_entry,
            invite_cookie_value,
        )

        if not invite_only_activation_enabled():
            raise HTTPException(404, {"code": "feature_disabled"})
        try:
            invite_context = await load_invite_context(
                store,
                raw_secret=invite_cookie_value(request),
            )
        except InviteFlowError as exc:
            raise HTTPException(exc.status_code, {"code": exc.code}) from exc
        await bind_invite_public_entry(request, invite_context.team_id)
        invite_intent_hash = invite_context.intent_hash
        if not await store.rate_limit_check(
            f"invite_oauth_start:{invite_intent_hash}",
            limit=10,
            window_s=15 * 60,
        ):
            raise HTTPException(429, {"code": "rate_limited"})
        next_path = None
    if flow_kind == "normal":
        await _bind_login_public_entry(request)
    public_context = current_public_access_context()
    from bowhead_gateway.public_url import PublicEntryRequiredError, public_entry_required_detail

    try:
        redirect_uri = _redirect_uri(provider)
    except PublicEntryRequiredError as exc:
        raise HTTPException(409, public_entry_required_detail()) from exc
    await _store_state(
        store.db_path,
        state,
        provider,
        _safe_next_path(next_path),
        public_context.entry_id if public_context else None,
        flow_kind=flow_kind,
        invite_intent_hash=invite_intent_hash,
        redirect_uri=redirect_uri,
    )
    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
    }
    if cfg["scope"]:
        params["scope"] = cfg["scope"]
    # quote_via=quote → space encodes as %20 (not "+"). Restores the pre-invite behavior;
    # avoids any provider that mishandles "+" in a space-delimited multi-scope value.
    return {"redirect_url": f"{cfg['auth_url']}?{urlencode(params, quote_via=quote)}"}


async def _complete_invite_oauth(
    identity: TrustedProviderIdentity,
    *,
    invite_intent_hash: str | None,
    request: Request,
    background_tasks: BackgroundTasks,
    store: UserStore,
) -> RedirectResponse:
    from bowhead_gateway.auth_policy import (
        AccountCreationPolicy,
        resolve_account_policy,
    )
    from bowhead_gateway.invite_flow import InviteFlowError, load_invite_context
    from bowhead_gateway.routes.auth import (
        _build_invite_result,
        _issue_token_pair,
        _multi_team_authz_enabled,
    )
    from bowhead_gateway.routes.invite_activation import (
        bind_invite_public_entry,
        clear_invite_cookie,
    )

    if not invite_intent_hash:
        raise HTTPException(400, {"code": "invite_intent_missing"})
    try:
        context = await load_invite_context(
            store,
            intent_hash=invite_intent_hash,
        )
    except InviteFlowError as exc:
        raise HTTPException(exc.status_code, {"code": exc.code}) from exc
    await bind_invite_public_entry(request, context.team_id)
    if identity.email != context.invited_email:
        raise HTTPException(403, {"code": "invite_email_mismatch"})

    by_subject = await store.get_user_by_federated_identity(
        identity.provider,
        identity.subject,
    )
    by_email = await store.get_user_by_email(identity.email)
    if by_subject is not None and by_email is not None and by_subject.id != by_email.id:
        raise HTTPException(409, {"code": "federated_identity_conflict"})
    user = by_subject or by_email
    if user is not None:
        if user.status != "active":
            raise HTTPException(403, {"code": "identity_blocked"})
        try:
            accepted = await store.accept_invite_from_intent_atomic(
                intent_hash=context.intent_hash,
                invite_type=context.invite_type,
                invite_id=context.invite_id,
                token_version=context.token_version,
                user_id=user.id,
                target_team_id=context.team_id,
                workspace_id=context.workspace_id,
                workspace_role=str(context.row.get("role") or "member"),
                multi_team_enabled=_multi_team_authz_enabled(),
                federated_provider=identity.provider,
                federated_subject=identity.subject,
            )
        except EntitlementDenied as exc:
            raise seat_denied_to_http(exc) from exc
        except ValueError as exc:
            code = "invite_cross_team_not_allowed" if "cross-team" in str(exc) else "invite_accept_failed"
            raise HTTPException(409, {"code": code}) from exc
        accept_status = str(accepted.get("status") or "accepted")
    else:
        policy = resolve_account_policy(
            deployment_mode_override=getattr(request.app.state, "deployment_mode", None),
        )
        if policy.account_creation is AccountCreationPolicy.ADMIN_ONLY:
            raise HTTPException(403, {"code": "policy_blocked"})
        team = await store.get_team(context.team_id)
        if team is None:
            raise HTTPException(401, {"code": "invite_invalid"})
        try:
            user = await store.activate_invited_identity_atomic(
                intent_hash=context.intent_hash,
                invite_type=context.invite_type,
                invite_id=context.invite_id,
                token_version=context.token_version,
                team_id=context.team_id,
                email=identity.email,
                password_hash=None,
                display_name=identity.display_name,
                tier=team.default_user_tier,
                email_verified=True,
                workspace_id=context.workspace_id,
                workspace_role=str(context.row.get("role") or "member"),
                federated_provider=identity.provider,
                federated_subject=identity.subject,
                avatar_url=identity.avatar_url,
            )
        except EntitlementDenied as exc:
            raise seat_denied_to_http(exc) from exc
        except ValueError as exc:
            raise HTTPException(409, {"code": "invite_activation_failed"}) from exc
        accept_status = "activated"

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
    result = _build_invite_result(
        invite_type=context.invite_type,
        row=context.row,
        requires_verification=False,
        workspace_name=context.workspace_name,
        team_id=context.team_id,
        team_name=context.team_name,
        accept_status=accept_status,
        has_workspace_access=has_workspace_access,
    )
    redirect = RedirectResponse(
        url=_oauth_return_path(str(result["next_route"])),
        status_code=303,
    )
    await _issue_token_pair(user, request, redirect, store)
    from bowhead_gateway.post_login_hooks import schedule_post_login_hooks

    await schedule_post_login_hooks(
        background_tasks,
        request,
        user=user,
        team_id=context.team_id,
        auth_method=f"oauth:{identity.provider}",
    )
    clear_invite_cookie(redirect)
    await store.log_audit(
        user_id=user.id,
        team_id=context.team_id,
        event_type="oauth_invite_activation",
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        payload={"provider": identity.provider, "invite_type": context.invite_type},
    )
    return redirect


def _oauth_error_redirect(
    *,
    provider: str | None,
    code: str,
    flow_kind: str = "normal",
    next_path: str | None = None,
) -> RedirectResponse:

    safe_code = code if code in _OAUTH_PUBLIC_ERROR_CODES else "oauth_callback_failed"
    params = {"oauth_error": safe_code}
    if provider in _PROVIDERS:
        params["oauth_provider"] = provider
    if flow_kind == "invite":
        params["flow"] = "invite"
        params["next"] = "/invite/?resume=1"
    elif safe_next := _safe_next_path(next_path):
        params["next"] = safe_next
    return RedirectResponse(
        url=_oauth_return_path(f"/login/?{urlencode(params)}"),
        status_code=303,
    )


def _http_exception_code(exc: HTTPException) -> str:
    detail = exc.detail
    if not isinstance(detail, dict):
        return "oauth_callback_failed"
    direct = detail.get("code")
    if isinstance(direct, str):
        return direct
    nested = detail.get("error")
    if isinstance(nested, dict) and isinstance(nested.get("code"), str):
        return str(nested["code"])
    return "oauth_callback_failed"


@router.get("/{provider}/callback")
async def oauth_callback(
    provider: str,
    request: Request,
    background_tasks: BackgroundTasks,
    code: str | None = None,
    state: str | None = None,
    provider_error: str | None = Query(default=None, alias="error"),
    store: UserStore = Depends(_store),
) -> RedirectResponse:
    if provider not in _PROVIDERS:
        return _oauth_error_redirect(
            provider=None,
            code="oauth_provider_unsupported",
        )
    if not state:
        return _oauth_error_redirect(
            provider=provider,
            code="oauth_state_invalid",
        )
    state_ok, next_path, public_entry_id, flow_kind, invite_intent_hash, redirect_uri = await _consume_state_snapshot(
        store.db_path,
        state,
        provider,
    )
    if not state_ok:
        return _oauth_error_redirect(
            provider=provider,
            code="oauth_state_invalid",
        )
    if redirect_uri:
        from bowhead_gateway.public_url import restore_oauth_public_access_context

        restore_oauth_public_access_context(redirect_uri, callback_path=f"/api/v1/auth/oauth/{provider}/callback")
    if public_entry_id:
        public_entry_store = request.app.state.public_entry_store
        try:
            entry = await public_entry_store.get(public_entry_id)
        except Exception:
            logger.exception("oauth.entry_metadata_unavailable")
            entry = None
        context = current_public_access_context()
        if entry is not None and entry.status == "active":
            from bowhead_gateway.public_url import public_app_url_for_base

            redirect_uri = redirect_uri or public_app_url_for_base(entry.base_url, f"/api/v1/auth/oauth/{provider}/callback")
            if context is None or (context.entry_id is None and context.base_url == entry.base_url):
                set_public_access_context(public_access_context_for_entry(entry))
    if flow_kind == "normal" and redirect_uri is None:
        await _bind_login_public_entry(request)
    if provider_error or not code:
        return _oauth_error_redirect(
            provider=provider,
            code="oauth_authorization_incomplete",
            flow_kind=flow_kind,
            next_path=next_path,
        )
    try:
        return await _complete_oauth_callback(
            provider=provider,
            code=code,
            next_path=next_path,
            flow_kind=flow_kind,
            invite_intent_hash=invite_intent_hash,
            request=request,
            background_tasks=background_tasks,
            store=store,
            redirect_uri=redirect_uri,
        )
    except HTTPException as exc:
        error_code = _http_exception_code(exc)
        logger.warning(
            "OAuth callback rejected provider=%s code=%s status=%s",
            provider,
            error_code,
            exc.status_code,
        )
        return _oauth_error_redirect(
            provider=provider,
            code=error_code,
            flow_kind=flow_kind,
            next_path=next_path,
        )
    except Exception as exc:
        logger.error(
            "OAuth callback failed provider=%s exception_type=%s",
            provider,
            type(exc).__name__,
        )
        return _oauth_error_redirect(
            provider=provider,
            code="oauth_callback_failed",
            flow_kind=flow_kind,
            next_path=next_path,
        )


async def _complete_oauth_callback(
    *,
    provider: str,
    code: str,
    next_path: str | None,
    flow_kind: str,
    invite_intent_hash: str | None,
    request: Request,
    background_tasks: BackgroundTasks,
    store: UserStore,
    redirect_uri: str | None = None,
) -> RedirectResponse:
    cfg = _PROVIDERS[provider]
    # exchange code → access_token
    async with httpx2.AsyncClient(timeout=10.0) as c:
        token_resp = await c.post(
            cfg["token_url"],
            data={
                "code": code,
                "client_id": _client_id(provider),
                "client_secret": _client_secret(provider),
                "redirect_uri": redirect_uri or _redirect_uri(provider),
                "grant_type": "authorization_code",
            },
        )
        if token_resp.status_code >= 400:
            raise HTTPException(401, {"code": "oauth_token_exchange_failed"})
        token_data = token_resp.json()
        access_token = token_data.get("access_token") or token_data.get("data", {}).get("access_token")
        if not access_token:
            raise HTTPException(401, {"code": "oauth_token_exchange_failed"})
        # fetch userinfo
        ui = await c.get(cfg["userinfo_url"], headers={"Authorization": f"Bearer {access_token}"})
        if ui.status_code >= 400:
            raise HTTPException(401, {"code": "oauth_userinfo_failed"})
        ui_data = ui.json()
        # feishu: {data: {open_id, email, name, ...}} or {open_id, email, name} v2
        info = ui_data.get("data") or ui_data
        email = (info.get("email") or "").lower().strip()
        name = info.get("name") or info.get("given_name") or ""
        avatar_url = info.get("avatar_url") or info.get("avatar_big") or info.get("avatar_middle") or info.get("avatar_thumb") or None
        # P4.5.4b: Feishu contacts API disabled → no actual email; fall back to open_id
        # to allow login, then let users supply their actual email at /profile.
        if not email and provider == "feishu":
            open_id = info.get("open_id") or info.get("union_id") or info.get("user_id") or ""
            if open_id:
                email = f"{open_id}@feishu.local".lower()
    if not email:
        raise HTTPException(401, {"code": "oauth_identity_missing"})

    subject = str(
        info.get("sub") or info.get("union_id") or info.get("open_id") or info.get("user_id") or "",
    ).strip()
    email_verified = bool(email and not email.endswith("@feishu.local"))
    if flow_kind == "invite":
        trusted = _trusted_provider_identity(
            provider=provider,
            subject=subject,
            email=email,
            email_verified=email_verified,
            display_name=name or None,
            avatar_url=avatar_url,
        )
        return await _complete_invite_oauth(
            trusted,
            invite_intent_hash=invite_intent_hash,
            request=request,
            background_tasks=background_tasks,
            store=store,
        )

    # Sprint α G6 r4 (P0-S-1 fix): white-list domain → MEMBER only, NOT admin.
    # OAuth path now aligns with register flow — whitelist domain users
    # are pre-provisioned team members; first OAuth user becomes creator only if
    # the team was auto-created here (truly empty).
    from bowhead_gateway.routes.auth import is_auto_member_email

    is_auto_member = is_auto_member_email(email)

    user = await store.get_user_by_federated_identity(provider, subject) if subject else None
    if user is None:
        user = await store.get_user_by_email(email)
    if not user:
        invite_register_path = _invite_register_path_from_next(next_path)
        if invite_register_path:
            return RedirectResponse(url=_oauth_return_path(invite_register_path), status_code=303)
        from bowhead_gateway.auth_policy import (
            AccountCreationPolicy,
            resolve_account_policy,
        )

        policy = resolve_account_policy(
            deployment_mode_override=getattr(request.app.state, "deployment_mode", None),
        )
        if policy.account_creation is not AccountCreationPolicy.OPEN:
            raise HTTPException(403, {"code": "oauth_registration_disabled"})
        # OAuth ingress for whitelist domain — match find_default team behavior
        if is_auto_member:
            target_team = await store.find_default_team_for_domain(email)
            if target_team is None:
                # Reject — same invariant as register flow (no auto-create for whitelist)
                raise HTTPException(
                    400,
                    {"code": "whitelist_domain_no_team_provisioned"},
                )
            team = target_team
        else:
            # Non-whitelist OAuth: auto-create new team (user is creator of their own team)
            team = await store.create_team(name=name or email.split("@")[0], domain=email.split("@")[-1])
        final_tier = "ultra" if is_auto_member else team.default_user_tier
        try:
            user = await store.create_user(
                team_id=team.id,
                email=email,
                display_name=name,
                tier=final_tier,
                oauth_providers=[provider],
                email_verified=True,
                avatar_url=avatar_url,
            )
            # Seed automatic language preferences and preserve a pre-signup UI choice.
            with contextlib.suppress(Exception):
                from bowhead_core.new_user_defaults import seed_new_user_settings

                await seed_new_user_settings(
                    getattr(request.app.state, "user_settings_store", None),
                    user.id,
                    ui_language=request.cookies.get("NEXT_LOCALE"),
                )
        except EntitlementDenied as exc:  # OAuth first-login seat path — §4.2
            raise seat_denied_to_http(exc) from exc
        # Bootstrap creator: only for non-whitelist (self-create) OR first whitelist user.
        # Atomic via set_team_creator (idempotent WHERE IS NULL).
        if not is_auto_member:
            # Self-create new team — definitely first user, becomes creator
            await store.set_team_creator(team.id, user.id)
        else:
            # Whitelist: bootstrap creator only if team has no creator yet (race-safe)
            existing_creator = await store.get_team_creator_id(team.id)
            if existing_creator is None:
                await store.set_team_creator(team.id, user.id)
            # else: user is member, NOT admin (race-lost or team already had creator)
        # Return value previously triggered automatic public-entry registration for the first creator; 32cdc5562
        # removed that block (activation entry no longer a prerequisite). Retain only set_team_creator's side effect.
    elif avatar_url and user.avatar_url != avatar_url:
        # Existing user with a changed OAuth avatar: update it.
        import aiosqlite as _aio

        async with _aio.connect(store.db_path) as db:
            await db.execute(
                "UPDATE users SET avatar_url = ? WHERE id = ?",
                (avatar_url, user.id),
            )
            await db.commit()
        user.avatar_url = avatar_url
    if subject and email_verified:
        try:
            await store.link_federated_identity(
                user_id=user.id,
                provider=provider,
                subject=subject,
                verified_email=email,
            )
        except ValueError as exc:
            raise HTTPException(
                409,
                {"code": "federated_identity_conflict"},
            ) from exc
    await store.update_last_active(user.id)
    await store.log_audit(
        user_id=user.id,
        team_id=user.team_id,
        event_type="oauth_login",
        ip=request.client.host if request.client else None,
        payload={"provider": provider},
    )
    # P4.5.27: dual cookie (access + refresh)
    from bowhead_gateway.routes.auth import _issue_token_pair

    redirect = RedirectResponse(url=_oauth_return_path(next_path or "/"), status_code=303)
    await _issue_token_pair(user, request, redirect, store)
    from bowhead_gateway.post_login_hooks import schedule_post_login_hooks

    await schedule_post_login_hooks(
        background_tasks,
        request,
        user=user,
        team_id=user.active_team_id or user.team_id,
        auth_method=f"oauth:{provider}",
    )
    return redirect
