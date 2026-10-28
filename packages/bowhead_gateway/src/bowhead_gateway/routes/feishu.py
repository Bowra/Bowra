"""Feishu OpenAPI integration endpoints."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

from bowhead_core.llm_config_crypto import encrypt_llm_config
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from bowhead_gateway.feishu.bridge import FeishuTurnBridge, parse_message_event
from bowhead_gateway.feishu.client import FeishuClient
from bowhead_gateway.feishu.config import (
    LONG_CONNECTION_RESTART_FIELDS,
    DbBackedFeishuAppConfigResolver,
    EnvFeishuAppConfigResolver,
    FeishuAppConfig,
    FeishuAppConfigResolver,
    long_connection_restart_signature,
)
from bowhead_gateway.feishu.qr import QrPayloadTooLongError, render_setup_qr_svg
from bowhead_gateway.feishu.security import (
    FeishuSecurityConfig,
    FeishuSecurityError,
    extract_challenge,
    parse_callback_body,
)
from bowhead_gateway.product_identity import product_name
from bowhead_gateway.routes._deps import require_team_admin
from bowhead_gateway.routes.auth import _current_user
from bowhead_gateway.settings import get_settings

if TYPE_CHECKING:
    from bowhead_gateway.feishu.store import (
        FeishuBinding,
        FeishuBindingSession,
        FeishuEventStore,
        FeishuSetupSession,
        FeishuStoredAppConfig,
    )

logger = logging.getLogger(__name__)

router = APIRouter()
TEAM_ADMIN_DEP = Depends(require_team_admin)
CURRENT_USER_DEP = Depends(_current_user)


class CreateSetupSessionBody(BaseModel):
    workspace_id: str | None = Field(default=None, max_length=128)
    tenant_key: str | None = Field(default=None, max_length=128)
    app_type: str = Field(default="internal_guided", max_length=64)


class UpdateFeishuConfigBody(BaseModel):
    tenant_key: str | None = Field(default="", max_length=128)
    app_type: Literal["internal"] = "internal"
    enabled: bool | None = None
    event_ingress: Literal["webhook", "long_connection"] | None = None
    app_id: str | None = Field(default=None, max_length=128)
    app_secret: str | None = Field(default=None, max_length=4096)
    verification_token: str | None = Field(default=None, max_length=4096)
    encrypt_key: str | None = Field(default=None, max_length=4096)
    bot_open_id: str | None = Field(default=None, max_length=128)
    group_public_answers_enabled: bool | None = None


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    diagnostics = _diagnostics_payload(request)
    return {
        "ok": bool(diagnostics["ok"]),
        "enabled": bool(diagnostics["enabled"]),
        "degraded": bool(diagnostics["degraded"]),
    }


@router.get("/admin/diagnostics")
async def admin_diagnostics(
    request: Request,
    _admin: Any = TEAM_ADMIN_DEP,
) -> dict[str, Any]:
    from bowhead_gateway.public_url import PublicEntryRequiredError, team_public_app_url

    payload = _diagnostics_payload(request)
    try:
        payload["event_subscription_url"] = await team_public_app_url(request, team_id=str(_admin.team_id), path="/api/v1/integrations/feishu/events")
        payload["card_callback_url"] = await team_public_app_url(
            request, team_id=str(_admin.team_id), path="/api/v1/integrations/feishu/card-callback"
        )
    except PublicEntryRequiredError:
        payload["event_subscription_url"] = payload["card_callback_url"] = ""
    return payload


@router.get("/admin/config")
async def admin_config(
    request: Request,
    _admin: Any = TEAM_ADMIN_DEP,
) -> dict[str, Any]:
    store = _store(request)
    stored = await store.get_app_config()
    return _admin_config_payload(request, stored=stored, tenant_key="")


@router.patch("/admin/config")
async def update_admin_config(
    request: Request,
    body: UpdateFeishuConfigBody,
    admin: Any = TEAM_ADMIN_DEP,
) -> dict[str, Any]:
    store = _store(request)
    tenant_key = body.tenant_key or ""
    existing = await store.get_app_config(tenant_key=tenant_key, app_type=body.app_type)
    fallback = _app_config_resolver(request).resolve(tenant_key)
    changed_fields: list[str] = []
    secret_fingerprints: dict[str, str] = {}

    def pick(field: str, current: Any) -> Any:
        if field in body.model_fields_set:
            changed_fields.append(field)
            return getattr(body, field)
        return current

    app_secret_ref = existing.app_secret_ref if existing else None
    verification_token_ref = existing.verification_token_ref if existing else None
    encrypt_key_ref = existing.encrypt_key_ref if existing else None

    if "app_secret" in body.model_fields_set:
        app_secret_ref = _encrypt_config_secret("app_secret", body.app_secret)
        if body.app_secret:
            secret_fingerprints["app_secret"] = _secret_fingerprint(body.app_secret)
        changed_fields.append("app_secret")
    if "verification_token" in body.model_fields_set:
        verification_token_ref = _encrypt_config_secret(
            "verification_token",
            body.verification_token,
        )
        if body.verification_token:
            secret_fingerprints["verification_token"] = _secret_fingerprint(
                body.verification_token,
            )
        changed_fields.append("verification_token")
    if "encrypt_key" in body.model_fields_set:
        encrypt_key_ref = _encrypt_config_secret("encrypt_key", body.encrypt_key)
        if body.encrypt_key:
            secret_fingerprints["encrypt_key"] = _secret_fingerprint(body.encrypt_key)
        changed_fields.append("encrypt_key")

    stored = await store.upsert_app_config(
        team_id=str(getattr(admin, "team_id", "") or ""),
        tenant_key=tenant_key,
        app_type=body.app_type,
        enabled=bool(pick("enabled", existing.enabled if existing else fallback.enabled)),
        event_ingress=str(
            pick(
                "event_ingress",
                existing.event_ingress if existing else fallback.event_ingress,
            )
        ),
        app_id=str(pick("app_id", existing.app_id if existing else fallback.app_id) or ""),
        app_secret_ref=app_secret_ref,
        verification_token_ref=verification_token_ref,
        encrypt_key_ref=encrypt_key_ref,
        bot_open_id=str(
            pick("bot_open_id", existing.bot_open_id if existing else fallback.bot_open_id) or "",
        ),
        group_public_answers_enabled=bool(
            pick(
                "group_public_answers_enabled",
                (existing.group_public_answers_enabled if existing else fallback.group_public_answers_enabled),
            )
        ),
        actor_user_id=str(getattr(admin, "id", "") or ""),
    )
    _invalidate_feishu_config_cache(request, tenant_key=tenant_key)
    _refresh_runtime_feishu_config(request, tenant_key=tenant_key)
    _mark_long_connection_restart_requirement(
        request,
        before_config=fallback,
        after_config=_app_config_resolver(request).resolve(tenant_key),
        changed_fields=sorted(set(changed_fields)),
    )
    await _audit_feishu_config_write(
        request=request,
        admin=admin,
        tenant_key=tenant_key,
        changed_fields=sorted(set(changed_fields)),
        secret_fingerprints=secret_fingerprints,
    )
    return _admin_config_payload(request, stored=stored, tenant_key=tenant_key)


@router.get("/bindings/{session_id}")
async def get_binding_session(
    session_id: str,
    request: Request,
    user: Any = CURRENT_USER_DEP,
) -> dict[str, Any]:
    store = _store(request)
    session = await store.get_binding_session(session_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="binding session not found")
    if _is_expired(session.expires_at):
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="binding session expired")
    existing = await store.get_binding(tenant_key=session.tenant_key, open_id=session.open_id)
    return _binding_session_payload(session, user=user, existing=existing)


@router.post("/bindings/{session_id}/complete")
async def complete_binding_session(
    session_id: str,
    request: Request,
    user: Any = CURRENT_USER_DEP,
) -> dict[str, Any]:
    store = _store(request)
    session = await store.get_binding_session(session_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="binding session not found")
    if _is_expired(session.expires_at):
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="binding session expired")
    existing = await store.get_binding(tenant_key=session.tenant_key, open_id=session.open_id)
    if existing is not None and existing.bowhead_user_id != str(getattr(user, "id", "") or ""):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Feishu identity is already bound to another {product_name()} user",
        )
    if session.state == "bound" and session.bound_user_id not in {None, getattr(user, "id", None)}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"binding session was completed by another {product_name()} user",
        )
    await store.upsert_binding(
        tenant_key=session.tenant_key,
        open_id=session.open_id,
        union_id=session.union_id,
        email=str(getattr(user, "email", "") or "") or session.email,
        bowhead_user_id=str(getattr(user, "id", "") or ""),
        binding_method="manual_web",
    )
    await store.complete_binding_session(
        session_id=session.id,
        bowhead_user_id=str(getattr(user, "id", "") or ""),
    )
    completed = await store.get_binding_session(session.id) or session
    existing = await store.get_binding(tenant_key=session.tenant_key, open_id=session.open_id)
    return _binding_session_payload(completed, user=user, existing=existing)


@router.post("/admin/setup-sessions")
async def create_setup_session(
    request: Request,
    body: CreateSetupSessionBody | None = None,
    admin: Any = TEAM_ADMIN_DEP,
) -> dict[str, Any]:
    team_id = str(getattr(admin, "team_id", "") or "")
    created_by_user_id = str(getattr(admin, "id", "") or "")
    if not (team_id and created_by_user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="team admin identity unavailable",
        )
    payload = body or CreateSetupSessionBody()
    if payload.app_type not in {"internal_guided", "internal_one_click_agent", "store"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid Feishu setup app_type",
        )
    session = await _store(request).create_setup_session(
        team_id=team_id,
        workspace_id=payload.workspace_id,
        created_by_user_id=created_by_user_id,
        tenant_key=payload.tenant_key,
        app_type=payload.app_type,
    )
    return await _setup_session_payload(request, session)


@router.get("/admin/setup-sessions/{session_id}")
async def get_setup_session(
    session_id: str,
    request: Request,
    admin: Any = TEAM_ADMIN_DEP,
) -> dict[str, Any]:
    session = await _store(request).get_setup_session(session_id)
    if session is None or session.team_id != str(getattr(admin, "team_id", "") or ""):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="setup session not found")
    return await _setup_session_payload(request, session)


@router.get("/admin/setup-sessions/{session_id}/qr.svg")
async def get_setup_session_qr(
    session_id: str,
    request: Request,
    admin: Any = TEAM_ADMIN_DEP,
) -> Response:
    session = await _store(request).get_setup_session(session_id)
    if session is None or session.team_id != str(getattr(admin, "team_id", "") or ""):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="setup session not found")
    try:
        svg = render_setup_qr_svg(await _setup_session_url(request, session))
    except QrPayloadTooLongError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/events")
async def events(request: Request) -> dict[str, Any]:
    if error := _callback_config_error(request):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "feishu_disabled_or_unconfigured", "message": error}},
        )
    raw_body = await request.body()
    try:
        body = parse_callback_body(
            raw_body=raw_body,
            headers=_lower_headers(request),
            config=_config_from_app(request),
        )
    except FeishuSecurityError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"code": "feishu_security_error", "message": str(exc)}},
        ) from exc

    challenge = extract_challenge(body)
    if challenge:
        return {"challenge": challenge}

    config = _config_from_app(request)
    inbound = parse_message_event(body, bot_open_id=config.bot_open_id)
    if inbound is None:
        return {"ok": True, "status": "ignored"}
    store = _store(request)
    claimed = await store.claim_message(
        event_id=inbound.event_id,
        message_id=inbound.message_id,
    )
    if not claimed:
        return {"ok": True, "status": "duplicate"}
    await store.mark_message_status(message_id=inbound.message_id, status="accepted")

    bridge = _bridge(request)
    task = asyncio.create_task(
        _handle_message_background(bridge, store, body, inbound),
        name=f"feishu_event:{inbound.message_id}",
    )
    tasks: set[asyncio.Task[Any]] = getattr(request.app.state, "feishu_bg_tasks", set())
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    request.app.state.feishu_bg_tasks = tasks
    return {"ok": True, "status": "accepted"}


@router.post("/card-callback")
async def card_callback(request: Request) -> dict[str, Any]:
    if error := _callback_config_error(request):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "feishu_disabled_or_unconfigured", "message": error}},
        )
    raw_body = await request.body()
    try:
        body = parse_callback_body(
            raw_body=raw_body,
            headers=_lower_headers(request),
            config=_config_from_app(request),
        )
    except FeishuSecurityError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"code": "feishu_security_error", "message": str(exc)}},
        ) from exc

    challenge = extract_challenge(body)
    if challenge:
        return {"challenge": challenge}

    try:
        return await _bridge(request).handle_card_callback(body)
    except PermissionError as exc:
        return {"toast": {"type": "warning", "content": str(exc)}}
    except Exception as exc:
        logger.exception("feishu.card_callback_failed err=%s", exc)
        return {"toast": {"type": "warning", "content": f"操作失败, 请在 {product_name()} Web 查看状态。"}}


async def _handle_message_background(
    bridge: FeishuTurnBridge,
    store: FeishuEventStore,
    body: dict[str, Any],
    inbound: Any,
) -> None:
    try:
        result = await bridge.handle_message_event(body)
        await store.mark_message_status(
            message_id=inbound.message_id,
            status=str(result.get("status") or "done"),
        )
    except Exception as exc:
        logger.exception(
            "feishu.message_background_failed event_id=%s message_id=%s tenant_key=%s chat_id=%s open_id=%s err=%s",
            inbound.event_id,
            inbound.message_id,
            inbound.tenant_key,
            inbound.chat_id,
            inbound.sender_open_id,
            exc,
        )
        await store.mark_message_status(message_id=inbound.message_id, status="failed")


def _diagnostics_payload(
    request: Request,
) -> dict[str, Any]:
    settings = _settings_from_app(request)
    config = _config_from_app(request)
    app_config = _app_config_resolver(request).resolve("")
    store = getattr(request.app.state, "feishu_event_store", None)
    client = getattr(request.app.state, "feishu_client", None)
    long_connection_worker = getattr(request.app.state, "feishu_long_connection_worker", None)
    restart_state = _long_connection_restart_state(request, app_config=app_config)
    llm_secret_diagnostics = _llm_secret_diagnostics(request)
    auth_mode = str(getattr(settings, "auth_mode", "") or "").lower()
    missing_credentials: list[str] = []
    if not app_config.app_id:
        missing_credentials.append("app_id")
    if not app_config.app_secret:
        missing_credentials.append("app_secret")
    if app_config.event_ingress == "webhook" and not config.verification_token:
        missing_credentials.append("verification_token")
    if not config.bot_open_id:
        missing_credentials.append("bot_open_id")
    if app_config.event_ingress == "webhook" and str(getattr(settings, "env", "")).lower() == "prod" and not config.encrypt_key:
        missing_credentials.append("encrypt_key")

    missing: list[str] = list(missing_credentials) if app_config.enabled else []
    if app_config.enabled:
        if app_config.event_ingress == "long_connection":
            missing.extend(_long_connection_missing(long_connection_worker))
    if restart_state["restart_required"]:
        missing.append("long_connection_restart_required")
    if app_config.enabled:
        if store is None:
            missing.append("event_store")
        missing.extend(f"llm_provider_secret:{provider}" for provider in llm_secret_diagnostics["missing_referenced_provider_secrets"])
    degraded = bool(missing)
    required_event_subscriptions = _required_event_subscriptions(app_config.event_ingress)
    required_permissions = _required_permissions()
    setup_checklist = _setup_checklist(
        enabled=app_config.enabled,
        missing=missing,
        event_ingress=app_config.event_ingress,
        config=config,
        auth_mode=auth_mode,
        app_configured=bool(app_config.app_id and app_config.app_secret),
        store_configured=store is not None,
        llm_secret_diagnostics=llm_secret_diagnostics,
        long_connection_restart_required=restart_state["restart_required"],
        long_connection_restart_reason=restart_state["message"],
    )
    launch_record_template = _launch_record_template(
        app_config=app_config,
        config=config,
        degraded=degraded,
        setup_checklist=setup_checklist,
        required_event_subscriptions=required_event_subscriptions,
        required_permissions=required_permissions,
        long_connection_status=(getattr(long_connection_worker, "status", None) if long_connection_worker else None),
        missing=missing,
        auth_mode=auth_mode,
    )
    payload = {
        "ok": not degraded,
        "enabled": app_config.enabled,
        "degraded": degraded,
        "app_type": app_config.app_type,
        "event_ingress": app_config.event_ingress,
        "group_public_answers_enabled": config.group_public_answers_enabled,
        "auth_mode": auth_mode,
        "prod_auth_mode": auth_mode != "dev",
        "missing_credentials": missing_credentials,
        "missing_config": missing,
        "app_id_configured": bool(app_config.app_id),
        "app_secret_configured": bool(app_config.app_secret),
        "verification_token_configured": bool(config.verification_token),
        "encrypt_key_configured": bool(config.encrypt_key),
        "bot_open_id_configured": bool(config.bot_open_id),
        "event_store_configured": store is not None,
        "long_connection": (getattr(long_connection_worker, "status", None) if long_connection_worker else None),
        "long_connection_restart_required": restart_state["restart_required"],
        "long_connection_restart_reason": restart_state["message"],
        "long_connection_restart_changed_fields": restart_state["changed_fields"],
        "tenant_token_cache_keys": (getattr(client, "tenant_token_cache_keys", list)() if client else []),
        "llm_provider_secrets": llm_secret_diagnostics["referenced_provider_secrets"],
        "missing_llm_provider_secrets": (llm_secret_diagnostics["missing_referenced_provider_secrets"]),
        "required_event_subscriptions": required_event_subscriptions,
        "required_permissions": required_permissions,
        "setup_checklist": setup_checklist,
        "launch_record_template": launch_record_template,
    }
    return payload


async def _setup_session_payload(request: Request, session: FeishuSetupSession) -> dict[str, Any]:
    from bowhead_gateway.public_url import public_app_path

    setup_url = await _setup_session_url(request, session)
    return {
        "id": session.id,
        "state": session.state,
        "app_type": session.app_type,
        "tenant_key": session.tenant_key,
        "workspace_id": session.workspace_id,
        "expires_at": session.expires_at,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "setup_url": setup_url,
        "qr_payload": setup_url,
        "qr_svg_url": public_app_path(f"/api/v1/integrations/feishu/admin/setup-sessions/{session.id}/qr.svg"),
        "test_phrase": f"{product_name()} Feishu setup {session.id[-8:]}",
    }


def _admin_config_payload(
    request: Request,
    *,
    stored: FeishuStoredAppConfig | None,
    tenant_key: str,
) -> dict[str, Any]:
    resolved = _app_config_resolver(request).resolve(tenant_key)
    return {
        "source": "db" if stored else "env",
        "tenant_key": tenant_key,
        "app_type": resolved.app_type,
        "enabled": resolved.enabled,
        "event_ingress": resolved.event_ingress,
        "app_id": resolved.app_id,
        "bot_open_id": resolved.bot_open_id,
        "group_public_answers_enabled": resolved.group_public_answers_enabled,
        "app_secret_configured": bool(resolved.app_secret),
        "verification_token_configured": bool(resolved.verification_token),
        "encrypt_key_configured": bool(resolved.encrypt_key),
        "secret_sources": {
            "app_secret": _secret_source(stored.app_secret_ref if stored else None, resolved.app_secret),
            "verification_token": _secret_source(
                stored.verification_token_ref if stored else None,
                resolved.verification_token,
            ),
            "encrypt_key": _secret_source(stored.encrypt_key_ref if stored else None, resolved.encrypt_key),
        },
        "updated_at": stored.updated_at if stored else None,
        "updated_by_user_id": stored.updated_by_user_id if stored else None,
        "long_connection_restart_required": _long_connection_restart_state(
            request,
            app_config=resolved,
        )["restart_required"],
    }


def _binding_session_payload(
    session: FeishuBindingSession,
    *,
    user: Any,
    existing: FeishuBinding | None,
) -> dict[str, Any]:
    current_user_id = str(getattr(user, "id", "") or "")
    conflict = existing is not None and existing.bowhead_user_id != current_user_id
    return {
        "id": session.id,
        "state": session.state,
        "tenant_key": session.tenant_key,
        "feishu_email": session.email,
        "expires_at": session.expires_at,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "completed_at": session.completed_at,
        "current_user_email": getattr(user, "email", None),
        "current_user_id": current_user_id,
        "already_bound_to_current_user": (existing is not None and existing.bowhead_user_id == current_user_id),
        "conflict": conflict,
        "can_complete": not conflict and session.state in {"pending", "bound"},
    }


def _encrypt_config_secret(field: str, value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    token = encrypt_llm_config({"value": stripped})
    if token.startswith("PLAIN:"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"BOWHEAD_FERNET_KEY is required to store Feishu {field}",
        )
    return token


def _secret_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _secret_source(stored_ref: str | None, resolved_value: str) -> str:
    if stored_ref:
        return "db"
    if resolved_value:
        return "env"
    return "missing"


def _install_db_config_resolver(request: Request) -> None:
    store = _store(request)
    request.app.state.feishu_app_config_resolver = DbBackedFeishuAppConfigResolver(
        _settings_from_app(request),
        store.db_path,
    )


def _invalidate_feishu_config_cache(request: Request, *, tenant_key: str = "") -> None:
    resolver = getattr(request.app.state, "feishu_app_config_resolver", None)
    if resolver is None or not isinstance(resolver, DbBackedFeishuAppConfigResolver):
        _install_db_config_resolver(request)
        resolver = getattr(request.app.state, "feishu_app_config_resolver", None)
    invalidate = getattr(resolver, "invalidate", None)
    if callable(invalidate):
        invalidate(tenant_key)


def _refresh_runtime_feishu_config(request: Request, *, tenant_key: str = "") -> None:
    app_config = _app_config_resolver(request).resolve(tenant_key)
    security_config = FeishuSecurityConfig(
        verification_token=app_config.verification_token,
        encrypt_key=app_config.encrypt_key,
        bot_open_id=app_config.bot_open_id,
        group_public_answers_enabled=app_config.group_public_answers_enabled,
    )
    request.app.state.feishu_security_config = security_config
    request.app.state.feishu_config = security_config
    bridge = getattr(request.app.state, "feishu_bridge", None)
    if bridge is not None:
        bridge.config = security_config
    client = getattr(request.app.state, "feishu_client", None)
    if client is not None:
        clear = getattr(client, "clear_tenant_token_cache", None)
        if callable(clear):
            clear()


async def _audit_feishu_config_write(
    *,
    request: Request,
    admin: Any,
    tenant_key: str,
    changed_fields: list[str],
    secret_fingerprints: dict[str, str],
) -> None:
    user_store = getattr(request.app.state, "user_store", None)
    if user_store is None or not hasattr(user_store, "write_admin_audit_log"):
        return
    try:
        await user_store.write_admin_audit_log(
            actor_user_id=str(getattr(admin, "id", "") or ""),
            team_id=str(getattr(admin, "team_id", "") or ""),
            action="feishu_config_updated",
            target_kind="feishu_app_config",
            target_id=tenant_key or "default",
            scope="team",
            payload={
                "tenant_key": tenant_key,
                "changed_fields": changed_fields,
                "secret_fingerprints": secret_fingerprints,
            },
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception as exc:
        logger.warning("feishu.config_audit_failed err=%s", exc)


async def _setup_session_url(request: Request, session: FeishuSetupSession) -> str:
    session_id = quote(session.id, safe="")
    from bowhead_gateway.public_url import PublicEntryRequiredError, public_entry_required_detail, team_public_app_url

    try:
        return await team_public_app_url(request, team_id=session.team_id, path=f"/team/settings/integrations/feishu/?stage=02&setup={session_id}")
    except PublicEntryRequiredError as exc:
        raise HTTPException(409, public_entry_required_detail()) from exc


def _is_expired(expires_at: str) -> bool:
    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError:
        return True
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return expiry <= datetime.now(tz=UTC)


def _required_event_subscriptions(event_ingress: str) -> list[str]:
    ingress = "长连接" if event_ingress == "long_connection" else "Webhook"
    return [
        f"{ingress}: im.message.receive_v1 / 接收消息 v2.0",
        f"{ingress}: card.action.trigger / 卡片回传交互",
    ]


def _required_permissions() -> list[str]:
    return [
        "机器人能力已开启, 应用已发布到可用范围",
        "im:message.p2p_msg:readonly 或 im:message.p2p_msg - 接收用户发给机器人的单聊消息",
        "im:message.group_at_msg:readonly 或 im:message.group_at_msg - 接收群聊中 @ 机器人的消息",
        "im:message:send_as_bot 或 im:message - 以应用身份发送/回复消息和卡片",
        "更新已发送的消息卡片权限 - patch processing/status card",
        "发送仅特定人可见的消息卡片权限 - group onboarding/permission failure",
        "发送、删除消息表情回复权限 - active-turn steer acknowledgement",
        "contact:user.email:readonly - 获取用户邮箱信息, 用于邮箱自动绑定",
    ]


def _setup_checklist(
    *,
    enabled: bool,
    missing: list[str],
    event_ingress: str,
    config: FeishuSecurityConfig,
    auth_mode: str,
    app_configured: bool,
    store_configured: bool,
    llm_secret_diagnostics: dict[str, Any],
    long_connection_restart_required: bool,
    long_connection_restart_reason: str | None,
) -> list[dict[str, Any]]:
    missing_set = set(missing)
    checklist = [
        _check(
            "feishu_enabled",
            f"{product_name()} Feishu integration enabled",
            "ok" if enabled else "missing",
            "Set BOWHEAD_FEISHU_ENABLED=true before accepting Feishu traffic.",
        ),
        _check(
            "app_credentials",
            "App ID / App Secret configured",
            "ok" if app_configured else "missing",
            "Required for tenant_access_token and outbound OpenAPI calls.",
        ),
        _check(
            "bot_open_id",
            "Bot Open ID configured",
            "ok" if config.bot_open_id else "missing",
            "Required for group @bot mention filtering.",
        ),
        _check(
            "event_store",
            "Feishu event store configured",
            "ok" if store_configured else "missing",
            "Required for dedup, turn links, bindings and workspace selection.",
        ),
        _check(
            "prod_auth_mode",
            "Gateway auth mode is production-equivalent",
            "degraded" if auth_mode == "dev" else "ok",
            ("Current auth mode is dev; live smokes in this mode may use dev fallback identities and cannot approve production launch.")
            if auth_mode == "dev"
            else f"Current auth mode is {auth_mode or 'unset'}.",
        ),
        _check(
            "event_ingress",
            f"Event ingress: {event_ingress}",
            "ok" if not any(item.startswith("long_connection") for item in missing_set) else "degraded",
            "Long connection must have a running worker; webhook must pass challenge/signature checks.",
        ),
        _check(
            "long_connection_restart",
            "Long connection restart requirement",
            "degraded" if long_connection_restart_required else "ok",
            long_connection_restart_reason or "No pending long-connection restart is required.",
        ),
        _check(
            "webhook_security",
            "Webhook token/encryption configured when webhook is used",
            _webhook_security_status(event_ingress, config),
            "Webhook fallback requires verification token; production should also configure encrypt key.",
        ),
        _check(
            "llm_provider_secrets",
            "Referenced LLM provider secrets configured",
            ("missing" if llm_secret_diagnostics["missing_referenced_provider_secrets"] else "ok"),
            "Feishu can receive messages even when downstream LLM providers are not ready.",
        ),
        _check(
            "bot_capability_publish",
            "Feishu console: bot capability enabled and app version published",
            "manual",
            "Feishu OpenAPI send/reply requires an app bot, availability scope and published app version.",
        ),
        _check(
            "message_event_subscription",
            "Feishu console: im.message.receive_v1 subscribed",
            "manual",
            "Required for user messages. Prefer p2p + group @bot scopes over all group messages.",
        ),
        _check(
            "card_callback_routing",
            "Feishu console: card.action.trigger callback enabled",
            "manual",
            "Required for workspace picker, clarification, retry/skip and later approval cards.",
        ),
        _check(
            "message_permissions",
            "Feishu console: receive/send/update message permissions granted",
            "manual",
            "Covers direct chat, @bot group chat, reply cards and processing-card patch.",
        ),
        _check(
            "private_failure_permissions",
            "Feishu console: ephemeral card and reaction permissions granted",
            "manual",
            "Required for private group onboarding/permission-denied cards and active-turn acknowledgement.",
        ),
        _check(
            "contact_email_permission",
            "Feishu console: contact:user.email:readonly granted",
            "manual",
            "Required for Phase 1 email auto-binding. Without it, users fall back to onboarding/manual binding.",
        ),
    ]
    return checklist


def _launch_record_template(
    *,
    app_config: FeishuAppConfig,
    config: FeishuSecurityConfig,
    degraded: bool,
    setup_checklist: list[dict[str, Any]],
    required_event_subscriptions: list[str],
    required_permissions: list[str],
    long_connection_status: Any,
    missing: list[str],
    auth_mode: str,
) -> str:
    generated_at = datetime.now(tz=UTC).isoformat(timespec="seconds")
    checklist_rows = "\n".join(f"| {item['label']} | {item['status']} | {item['detail']} |" for item in setup_checklist)
    event_rows = "\n".join(f"- [ ] {item}" for item in required_event_subscriptions)
    permission_rows = "\n".join(f"- [ ] {item}" for item in required_permissions)
    worker_summary = (
        json.dumps(long_connection_status, ensure_ascii=False, sort_keys=True)
        if isinstance(long_connection_status, dict)
        else str(long_connection_status or "N/A")
    )
    missing_summary = ", ".join(missing) if missing else "none"
    group_public = "yes" if config.group_public_answers_enabled else "no"
    enabled = "yes" if app_config.enabled else "no"
    degraded_label = "yes" if degraded else "no"
    app_id_label = _redact_app_id(app_config.app_id)
    return f"""# {product_name()} x Feishu Launch Record

Generated: {generated_at}

## Metadata

| Field | Value |
|---|---|
| Launch date | YYYY-MM-DD |
| Environment | dev / staging / prod |
| {product_name()} gateway version / commit |  |
| Tester / approver |  |
| Feishu tenant name |  |
| Feishu tenant key |  |
| Feishu app id | {app_id_label} |
| Feishu app type | {app_config.app_type} |
| Event ingress | {app_config.event_ingress} |
| Feishu enabled | {enabled} |
| Diagnostics degraded | {degraded_label} |
| Missing config | {missing_summary} |
| Gateway auth mode | {auth_mode or "unset"} |
| Group public answers enabled | {group_public} |
| Long connection status | {worker_summary} |

## {product_name()} Checklist Snapshot

| Check | Status | Detail |
|---|---|---|
{checklist_rows}

## Feishu Event Subscriptions To Verify

{event_rows}

## Feishu Permissions To Verify

{permission_rows}

## Smoke Tests

| Smoke | Expected | Result | Evidence |
|---|---|---|---|
| Single chat text message | Starts exactly one {product_name()} turn and replies final answer card | pending |  |
| Processing card | Appears quickly and patches step/progress state | pending |  |
| Final answer markdown/table | Renders in Feishu card; tables use native table component where possible | pending |  |
| Workspace picker | Multi-workspace user must choose before turn starts | pending |  |
| Manual binding | Email-miss user can bind via {product_name()} Web and retry | pending |  |
| Clarification HITL | Card click resolves clarification and resumes same turn | pending |  |
| Retry / skip buttons | Return ack-only copy; no false rerun claim | pending |  |
| Permission deny | Does not start turn; group failure is private where possible | pending |  |
| Duplicate replay | Does not start or bill duplicate turn | pending |  |
| Group @bot smoke | Public answer behavior verified, if group policy enabled | pending |  |

## Launch Decision

| Field | Value |
|---|---|
| Approved to enable Feishu for target tenant/users | yes / no |
| Approved by |  |
| Approval time |  |
| Follow-up blockers |  |
"""


def _redact_app_id(app_id: str) -> str:
    if not app_id:
        return "missing"
    if len(app_id) <= 10:
        return app_id[:4] + "***"
    return app_id[:8] + "***" + app_id[-4:]


def _webhook_security_status(event_ingress: str, config: FeishuSecurityConfig) -> str:
    if event_ingress != "webhook":
        return "ok"
    if not config.verification_token:
        return "missing"
    if not config.encrypt_key:
        return "degraded"
    return "ok"


def _check(key: str, label: str, status: str, detail: str) -> dict[str, str]:
    return {
        "key": key,
        "label": label,
        "status": status,
        "detail": detail,
    }


def _llm_secret_diagnostics(request: Request) -> dict[str, Any]:
    loaded = getattr(request.app.state, "routing_config_loaded", None)
    providers = getattr(loaded, "providers", None)
    bindings = getattr(loaded, "agent_bindings", None)
    if not providers or not bindings:
        return {
            "referenced_provider_secrets": [],
            "missing_referenced_provider_secrets": [],
        }
    providers_by_name = {getattr(cfg, "name", ""): cfg for cfg in providers}
    referenced: set[str] = set()
    for binding in bindings:
        primary = getattr(binding, "primary", None)
        if isinstance(primary, tuple) and primary:
            referenced.add(str(primary[0]))
        for fallback in getattr(binding, "fallback", []) or []:
            if isinstance(fallback, tuple) and fallback:
                referenced.add(str(fallback[0]))

    statuses: list[dict[str, Any]] = []
    missing: list[str] = []
    for provider_name in sorted(referenced):
        cfg = providers_by_name.get(provider_name)
        if cfg is None:
            continue
        configured = _provider_secret_configured(cfg)
        secret_ref = _provider_secret_ref_label(cfg)
        if getattr(cfg, "type", "") != "mock" and not configured:
            missing.append(provider_name)
        statuses.append(
            {
                "provider": provider_name,
                "type": getattr(cfg, "type", ""),
                "secret_ref": secret_ref,
                "configured": configured,
            }
        )
    return {
        "referenced_provider_secrets": statuses,
        "missing_referenced_provider_secrets": missing,
    }


def _provider_secret_configured(cfg: Any) -> bool:
    if getattr(cfg, "api_key", None):
        return True
    ref = str(getattr(cfg, "api_key_ref", "") or "")
    if not ref:
        return getattr(cfg, "type", "") == "mock"
    if not ref.startswith("env://"):
        return False
    name = ref[len("env://") :]
    if ":" in name:
        var, default = name.split(":", 1)
        return bool(os.environ.get(var, default))
    return bool(os.environ.get(name))


def _provider_secret_ref_label(cfg: Any) -> str | None:
    if getattr(cfg, "api_key", None):
        return "inline"
    ref = str(getattr(cfg, "api_key_ref", "") or "")
    if not ref:
        return None
    if ref.startswith("env://"):
        name = ref[len("env://") :]
        var = name.split(":", 1)[0]
        return f"env://{var}"
    return ref.split("://", 1)[0] + "://..." if "://" in ref else ref


def _long_connection_missing(worker: Any) -> list[str]:
    if worker is None:
        return ["long_connection_worker"]
    status_payload = getattr(worker, "status", None)
    if not isinstance(status_payload, dict):
        return ["long_connection_status"]
    if status_payload.get("start_error"):
        return ["long_connection_start_error"]
    if not status_payload.get("running") and not status_payload.get("thread_alive"):
        return ["long_connection_not_running"]
    return []


def _long_connection_restart_state(
    request: Request,
    *,
    app_config: FeishuAppConfig,
) -> dict[str, Any]:
    runtime = getattr(request.app.state, "feishu_long_connection_runtime", None)
    if not isinstance(runtime, dict):
        return {
            "restart_required": False,
            "message": None,
            "changed_fields": [],
        }
    worker_signature = runtime.get("worker_signature")
    current_signature = long_connection_restart_signature(app_config)
    restart_required = bool(runtime.get("restart_required"))
    if worker_signature and current_signature == worker_signature and app_config.event_ingress == "long_connection":
        restart_required = False
    if app_config.event_ingress != "long_connection" and not worker_signature:
        restart_required = False
    return {
        "restart_required": restart_required,
        "message": runtime.get("message") if restart_required else None,
        "changed_fields": list(runtime.get("changed_fields") or []) if restart_required else [],
    }


def _mark_long_connection_restart_requirement(
    request: Request,
    *,
    before_config: FeishuAppConfig,
    after_config: FeishuAppConfig,
    changed_fields: list[str],
) -> None:
    runtime = getattr(request.app.state, "feishu_long_connection_runtime", None)
    if not isinstance(runtime, dict):
        runtime = {}
    changed = sorted(set(changed_fields))
    restart_sensitive = bool(set(changed) & LONG_CONNECTION_RESTART_FIELDS)
    long_connection_involved = before_config.event_ingress == "long_connection" or after_config.event_ingress == "long_connection"
    after_signature = long_connection_restart_signature(after_config)
    worker_signature = runtime.get("worker_signature")
    if worker_signature and worker_signature == after_signature and after_config.event_ingress == "long_connection":
        runtime.update(
            {
                "restart_required": False,
                "message": None,
                "changed_fields": [],
            }
        )
        request.app.state.feishu_long_connection_runtime = runtime
        return
    if after_config.event_ingress != "long_connection" and not worker_signature:
        runtime.update(
            {
                "restart_required": False,
                "message": None,
                "changed_fields": [],
            }
        )
        request.app.state.feishu_long_connection_runtime = runtime
        return
    if restart_sensitive and long_connection_involved:
        runtime.update(
            {
                "restart_required": True,
                "message": ("Long-connection settings changed. Restart the gateway/worker before claiming production readiness."),
                "changed_fields": changed,
                "required_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
            }
        )
        request.app.state.feishu_long_connection_runtime = runtime


def _callback_config_error(request: Request) -> str | None:
    settings = _settings_from_app(request)
    config = _config_from_app(request)
    app_config = _app_config_resolver(request).resolve("")
    if not app_config.enabled:
        return "Feishu integration is disabled"
    if app_config.event_ingress != "webhook":
        return f"Feishu webhook ingress is disabled; current ingress is {app_config.event_ingress}"
    if not config.verification_token:
        return "Feishu verification token is not configured"
    if str(getattr(settings, "env", "")).lower() == "prod" and not config.encrypt_key:
        return "Feishu encrypt key is required in prod"
    return None


def _bridge(request: Request) -> FeishuTurnBridge:
    bridge = getattr(request.app.state, "feishu_bridge", None)
    if bridge is not None:
        return bridge
    config = _config_from_app(request)
    client = getattr(request.app.state, "feishu_client", None)
    if client is None:
        client = FeishuClient(
            _app_config_resolver(request),
            base_url=_settings_from_app(request).feishu_openapi_base_url,
        )
        request.app.state.feishu_client = client
    bridge = FeishuTurnBridge(
        app=request.app,
        client=client,
        store=_store(request),
        config=config,
        app_config_resolver=_app_config_resolver(request),
    )
    request.app.state.feishu_bridge = bridge
    return bridge


def _store(request: Request) -> FeishuEventStore:
    store = getattr(request.app.state, "feishu_event_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="feishu event store not configured",
        )
    return store


def _config_from_app(request: Request) -> FeishuSecurityConfig:
    cached = getattr(request.app.state, "feishu_security_config", None)
    if cached is not None:
        return cached
    app_config = _app_config_resolver(request).resolve("")
    return FeishuSecurityConfig(
        verification_token=app_config.verification_token,
        encrypt_key=app_config.encrypt_key,
        bot_open_id=app_config.bot_open_id,
        group_public_answers_enabled=app_config.group_public_answers_enabled,
    )


def _app_config_resolver(request: Request) -> FeishuAppConfigResolver:
    resolver = getattr(request.app.state, "feishu_app_config_resolver", None)
    if resolver is not None:
        return resolver
    store = getattr(request.app.state, "feishu_event_store", None)
    if store is not None and getattr(store, "db_path", None):
        resolver = DbBackedFeishuAppConfigResolver(
            _settings_from_app(request),
            str(store.db_path),
        )
    else:
        resolver = EnvFeishuAppConfigResolver(_settings_from_app(request))
    request.app.state.feishu_app_config_resolver = resolver
    return resolver


def _settings_from_app(request: Request) -> Any:
    return getattr(request.app.state, "settings", None) or get_settings()


def _lower_headers(request: Request) -> dict[str, str]:
    return {k.lower(): v for k, v in request.headers.items()}
