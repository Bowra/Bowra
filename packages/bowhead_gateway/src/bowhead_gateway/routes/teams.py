"""Microsoft Teams P0: a single-tenant, personal-chat Bot endpoint."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from typing import Any
from urllib.parse import quote, urlparse
from uuid import uuid4

import httpx2
import jwt
from bowhead_core.llm_config_crypto import decrypt_llm_config, encrypt_llm_config
from bowhead_core.space_governance import SpaceOperation
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from bowhead_gateway.access_context import access_context_to_dict, build_access_context_for_user_from_request
from bowhead_gateway.im.copy_locale import resolve_channel_copy_locale
from bowhead_gateway.im.error_messages import im_interface_message
from bowhead_gateway.routes._deps import require_team_admin
from bowhead_gateway.routes.auth import _current_user
from bowhead_gateway.teams.identity import pairing_card, pairing_claims, verify_connector, verify_sso
from bowhead_gateway.teams.store import ActivityInProgress, InvalidTeamsConfig, TeamsConnection, TeamsStore

logger = logging.getLogger(__name__)
router = APIRouter()
TEAM_ADMIN_DEP = Depends(require_team_admin)


class UpdateTeamsConfig(BaseModel):
    entra_tenant_id: str | None = Field(default=None, max_length=128)
    microsoft_app_id: str | None = Field(default=None, max_length=128)
    app_type: str | None = None
    app_secret: str | None = Field(default=None, max_length=4096)
    managed_identity_client_id: str | None = Field(default=None, max_length=128)
    sso_auth_app_id: str | None = Field(default=None, max_length=128)
    sso_audience: str | None = Field(default=None, max_length=512)
    sso_connection_name: str | None = Field(default=None, max_length=128)
    teams_manifest_id: str | None = Field(default=None, max_length=128)
    status: str | None = None
    secret_expires_at: str | None = Field(default=None, max_length=64)


def _store(request: Request) -> TeamsStore:
    store = getattr(request.app.state, "teams_store", None)
    if store is None:
        raise HTTPException(503, {"error": {"code": "teams_not_ready"}})
    return store


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    _store(request)
    enabled = False
    # Public health intentionally reveals no tenant/app identifiers.
    return {"ok": enabled, "enabled": enabled, "degraded": False}


@router.get("/admin/config")
async def admin_config(request: Request, admin: Any = TEAM_ADMIN_DEP) -> dict[str, Any]:
    connection = await _store(request).get_connection_for_team(admin.team_id)
    return _config_payload(connection)


@router.get("/admin/install")
async def admin_install(request: Request, admin: Any = TEAM_ADMIN_DEP) -> dict[str, str]:
    connection = await _store(request).get_connection_for_team(admin.team_id)
    if connection is None or not connection.teams_manifest_id:
        raise HTTPException(409, {"error": {"code": "teams_manifest_not_configured"}})
    return {"install_url": f"https://teams.microsoft.com/l/app/{connection.teams_manifest_id}"}


@router.patch("/admin/config")
async def update_config(request: Request, body: UpdateTeamsConfig, admin: Any = TEAM_ADMIN_DEP) -> dict[str, Any]:
    if body.app_type is not None and body.app_type not in {"SingleTenant", "UserAssignedMSI"}:
        raise HTTPException(422, {"error": {"code": "teams_invalid_app_type"}})
    if body.status is not None and body.status not in {"draft", "verified", "enabled", "disabled"}:
        raise HTTPException(422, {"error": {"code": "teams_invalid_status"}})
    values = {name: getattr(body, name) for name in body.model_fields_set if name != "app_secret"}
    if "app_secret" in body.model_fields_set:
        values["app_secret_ref"] = encrypt_llm_config({"value": body.app_secret or ""}) if body.app_secret else None
    store = _store(request)
    try:
        connection = await store.upsert_connection(team_id=admin.team_id, actor_id=admin.id, values=values)
    except sqlite3.IntegrityError as exc:
        if "teams_connections.microsoft_app_id" not in str(exc):
            raise
        raise HTTPException(409, {"error": {"code": "teams_connection_identifiers_in_use"}}) from exc
    except InvalidTeamsConfig as exc:
        raise HTTPException(422, {"error": {"code": "teams_missing_configuration", "fields": exc.fields}}) from exc
    return _config_payload(connection)


class PairAccount(BaseModel):
    token: str = Field(min_length=1, max_length=4096)


@router.post("/bindings/confirm")
async def confirm_binding(request: Request, body: PairAccount, user: Any = Depends(_current_user)):
    authorization = request.headers.get("Authorization", "")
    if not request.cookies.get("bowhead_jwt") and not (authorization.startswith("Bearer ") and authorization[7:].strip()):
        raise HTTPException(401, {"error": {"code": "teams_pairing_signin_required"}})
    claims = pairing_claims(body.token)
    store = _store(request)
    connection = await store.get_enabled_connection(claims["app"], claims["tid"])
    if (
        connection is None
        or connection.team_id != claims["team"]
        or claims["sub"] != user.id
        or not await request.app.state.user_store.user_has_active_team_membership(user.id, connection.team_id)
    ):
        raise HTTPException(403, {"error": {"code": "teams_pairing_account_mismatch"}})
    try:
        await store.bind_identity(tenant_id=claims["tid"], oid=claims["oid"], user_id=user.id, team_id=connection.team_id)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, {"error": {"code": "teams_identity_already_bound"}}) from exc
    return {"ok": True}


def _activity_key(connection, activity):
    fields = [
        connection.id,
        connection.microsoft_app_id,
        connection.entra_tenant_id,
        activity["conversation"]["id"],
        activity["from"]["aadObjectId"],
        activity["id"],
    ]
    return hashlib.sha256(json.dumps(fields).encode()).hexdigest()


@router.post("/messages")
async def messages(request: Request) -> dict[str, Any]:
    activity = await request.json()
    if not isinstance(activity, dict):
        raise HTTPException(400, {"error": {"code": "teams_invalid_activity"}})
    connection, tenant_id = await _authenticate_activity(request, activity)
    activity_type = activity.get("type")
    if activity_type not in {"message", "invoke"}:
        return {}
    conversation = activity["conversation"] if isinstance(activity.get("conversation"), dict) else {}
    sender = activity["from"] if isinstance(activity.get("from"), dict) else {}
    oid = str(sender.get("aadObjectId") or "")
    text = str(activity.get("text") or "").strip()
    if conversation.get("conversationType") != "personal":
        if activity_type == "message":
            locale = await _copy_locale(request, tenant_id, oid, team_id=connection.team_id, text=text, client_language=_client_language(activity))
            await _send_activity(
                request,
                activity,
                connection,
                _card(im_interface_message("teams_personal_only_title", locale), im_interface_message("teams_personal_only", locale)),
            )
        return {}
    if not all(isinstance(value, str) and value.strip() for value in (sender.get("aadObjectId"), conversation.get("id"), activity.get("id"))):
        raise HTTPException(400, {"error": {"code": "teams_missing_activity_identity"}})
    invoke = activity.get("name") if activity_type == "invoke" else None
    if invoke == "adaptiveCard/action":
        value = activity.get("value")
        action = value.get("action") if isinstance(value, dict) else None
        if not isinstance(action, dict) or not isinstance(action.get("data", {}), dict):
            raise HTTPException(400, {"error": {"code": "teams_invalid_activity"}})
        if action.get("verb") == "bowhead.clarification.answer":
            from bowhead_gateway.teams.clarification import handle_answer_action

            return await handle_answer_action(request, activity, connection, tenant_id, oid, action.get("data") or {})
        if action.get("verb") != "bowhead.workspace.select":
            return {}
    elif activity_type == "invoke" and invoke != "signin/tokenExchange":
        return {}
    try:
        async with _store(request).process_activity(_activity_key(connection, activity), activity["id"]) as claimed:
            if claimed and activity_type == "message":
                await _handle_message(request, activity, connection, tenant_id, oid, conversation, text)
            elif claimed and invoke == "signin/tokenExchange":
                await _handle_token_exchange(request, activity, connection, tenant_id)
            elif invoke == "adaptiveCard/action":
                if claimed:
                    receipt = await _handle_workspace_selection(request, activity, connection, tenant_id)
                    return {"statusCode": 200, "type": "application/vnd.microsoft.card.adaptive", "value": receipt}
                return {
                    "statusCode": 200,
                    "type": "application/vnd.microsoft.activity.message",
                    "value": im_interface_message("choice_submitted", _client_language(activity)),
                }
    except ActivityInProgress as exc:
        raise HTTPException(503, {"error": {"code": "teams_activity_in_progress"}}, headers={"Retry-After": "3"}) from exc
    return {}


async def _handle_message(request, activity, connection, tenant_id, oid, conversation, text):
    store = _store(request)
    sender = activity["from"]
    if not oid:
        raise HTTPException(401, {"error": {"code": "teams_missing_user_identity"}})
    identity = await store.get_identity(tenant_id, oid, connection.team_id)
    if identity is None:
        email = _activity_email(sender)
        if email:
            await _offer_binding(request, activity, connection, tenant_id, oid, email)
            return None
        locale = await resolve_channel_copy_locale(
            request.app, user_id=None, message_text=text, team_id=connection.team_id, client_language=_client_language(activity)
        )
        await _send_activity(request, activity, connection, _signin_card(connection, locale))
        return {}
    user_id, team_id = identity
    locale = await resolve_channel_copy_locale(
        request.app, user_id=user_id, message_text=text, team_id=team_id, client_language=_client_language(activity)
    )
    if team_id != connection.team_id or not await request.app.state.user_store.user_has_active_team_membership(user_id, team_id):
        await _send_activity(
            request,
            activity,
            connection,
            _card(im_interface_message("teams_membership_lost_title", locale), im_interface_message("teams_membership_lost", locale)),
        )
        return {}
    if not text:
        await _send_activity(
            request,
            activity,
            connection,
            _card(im_interface_message("teams_unsupported_title", locale), im_interface_message("teams_text_only", locale)),
        )
        return {}
    conversation_id = str(conversation.get("id") or "")
    workspace_id = await store.get_workspace(tenant_id, conversation_id, oid, team_id)
    spaces = await request.app.state.user_store.list_workspaces_for_user(user_id, team_id=team_id)
    stale = workspace_id is not None and workspace_id not in {str(space["workspace_id"]) for space in spaces}
    if stale or text.casefold() == "/space" or (len(spaces) != 1 and workspace_id is None):
        if not spaces:
            await _send_activity(
                request,
                activity,
                connection,
                _card(im_interface_message("no_spaces_title", locale), im_interface_message("teams_no_spaces", locale)),
            )
        else:
            await _send_activity(request, activity, connection, _workspace_picker_card(spaces, locale))
        return {}
    workspace_id = workspace_id or str(spaces[0]["workspace_id"])
    access = await build_access_context_for_user_from_request(
        request, user_id=user_id, workspace_id=workspace_id, expected_team_id=team_id, operation=SpaceOperation.EXECUTE
    )
    session_id = await personal_session_id(request.app, connection, conversation_id, user_id, workspace_id)
    key = _activity_key(connection, activity)
    turn_id = "t_" + key[:32]
    if await request.app.state.orchestrator.session_store.get_turn(turn_id):
        return {}
    await _start_turn(
        request=request,
        activity=activity,
        connection=connection,
        user_id=user_id,
        team_id=team_id,
        workspace_id=workspace_id,
        text=text,
        session_id=session_id,
        turn_id=turn_id,
        access_context=access_context_to_dict(access),
        activity_key=key,
    )
    return {}


async def _authenticate_activity(request: Request, activity: dict[str, Any]) -> tuple[TeamsConnection, str]:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, {"error": {"code": "teams_missing_connector_token"}})
    token = authorization[7:]
    try:
        claims = jwt.decode(token, options={"verify_signature": False, "verify_aud": False})
        audience = str(claims.get("aud") or "")
    except jwt.PyJWTError as exc:
        raise HTTPException(401, {"error": {"code": "teams_invalid_connector_token"}}) from exc
    tenant = _tenant(activity)
    connection = await _store(request).get_enabled_connection(audience, tenant)
    if connection is None:
        raise HTTPException(403, {"error": {"code": "teams_connection_not_enabled"}})
    await verify_connector(token, connection, activity)
    return connection, tenant


async def _handle_token_exchange(request: Request, activity: dict[str, Any], connection: TeamsConnection, tenant: str) -> None:
    value = activity.get("value") if isinstance(activity.get("value"), dict) else {}
    token = str(value.get("token") or "")
    if not token or not connection.sso_audience:
        raise HTTPException(401, {"error": {"code": "teams_sso_not_configured"}})
    claims = await verify_sso(token, connection, tenant, activity)
    await _offer_binding(request, activity, connection, tenant, claims["oid"], str(claims.get("preferred_username") or claims.get("email") or ""))


async def _offer_binding(request, activity, connection, tenant, oid, email):
    user = await request.app.state.user_store.get_user_by_email(email) if email else None
    locale = await resolve_channel_copy_locale(
        request.app,
        user_id=None,
        team_id=connection.team_id,
        message_text=str(activity.get("text") or ""),
        client_language=_client_language(activity),
    )
    if user is None or not await request.app.state.user_store.user_has_active_team_membership(user.id, connection.team_id):
        card = _card(im_interface_message("teams_account_unavailable_title", locale), im_interface_message("teams_email_not_member", locale))
    else:
        card = await pairing_card(request, connection, tenant, oid, user.id, locale)
    await _send_activity(request, activity, connection, card)


async def _handle_workspace_selection(request: Request, activity: dict[str, Any], connection: TeamsConnection, tenant: str) -> dict[str, Any]:
    value = activity.get("value") if isinstance(activity.get("value"), dict) else {}
    action = value.get("action") if isinstance(value.get("action"), dict) else value
    if str(action.get("verb") or "") != "bowhead.workspace.select":
        raise HTTPException(400, {"error": {"code": "teams_invalid_activity"}})
    # Universal Actions (Action.Execute) deliver the card's data under ``value.action.data``.
    data = action.get("data") if isinstance(action.get("data"), dict) else action
    sender = activity.get("from") if isinstance(activity.get("from"), dict) else {}
    oid = str(sender.get("aadObjectId") or "")
    identity = await _store(request).get_identity(tenant, oid, connection.team_id)
    conversation = activity.get("conversation") if isinstance(activity.get("conversation"), dict) else {}
    workspace_id = str(data.get("workspace_id") or "")
    if identity is None or not workspace_id or not str(conversation.get("id") or ""):
        raise HTTPException(403, {"error": {"code": "teams_workspace_selection_denied"}})
    user_id, team_id = identity
    spaces = await request.app.state.user_store.list_workspaces_for_user(user_id, team_id=team_id)
    if team_id != connection.team_id or workspace_id not in {str(space["workspace_id"]) for space in spaces}:
        raise HTTPException(403, {"error": {"code": "teams_workspace_selection_denied"}})
    await build_access_context_for_user_from_request(
        request, user_id=user_id, workspace_id=workspace_id, expected_team_id=team_id, operation=SpaceOperation.EXECUTE
    )
    await _store(request).set_workspace(tenant, str(conversation["id"]), oid, workspace_id, team_id)
    # The picker button carries the locale the card was rendered in (the triggering message's language).
    locale = await resolve_channel_copy_locale(
        request.app,
        user_id=user_id,
        rendered_locale=data.get("locale"),
        team_id=team_id,
        workspace_id=workspace_id,
        client_language=_client_language(activity),
    )
    return _card(im_interface_message("channel_space_switched_title", locale), im_interface_message("teams_space_switched", locale))


def _client_language(activity: dict[str, Any]) -> str | None:
    """The Teams client language from the signed activity; the resolver ignores unsupported values."""
    value = activity.get("locale")
    return str(value) if isinstance(value, str) and value.strip() else None


async def _copy_locale(request: Request, tenant_id: str, oid: str, *, team_id: str, text: str = "", client_language: str | None = None) -> str:
    """Copy locale before identity checks, via the shared channel resolver."""
    identity: tuple[str, str] | None = None
    if oid:
        try:
            identity = await _store(request).get_identity(tenant_id, oid, team_id)
        except Exception:  # noqa: BLE001 - The identity lookup only selects the copy language.
            logger.warning("teams.copy_locale_identity_failed tenant=%s", tenant_id)
    user_id = identity[0] if identity else None
    return await resolve_channel_copy_locale(request.app, user_id=user_id, message_text=text, team_id=team_id, client_language=client_language)


def _activity_email(sender: dict[str, Any]) -> str:
    """Return only the signed Teams Activity's explicit user email fields.

    A Connector-authenticated Activity protects the complete payload.  We do
    not infer an email from display name, UPN-like IDs, or a client-supplied
    card value; if Teams does not supply an explicit field, SSO remains the
    verified fallback.
    """
    for field in ("userPrincipalName", "email"):
        value = str(sender.get(field) or "").strip().lower()
        if value and "@" in value:
            return value
    return ""


def _tenant(activity: dict[str, Any]) -> str:
    channel = activity.get("channelData") if isinstance(activity.get("channelData"), dict) else {}
    tenant = channel.get("tenant") if isinstance(channel.get("tenant"), dict) else {}
    value = str(tenant.get("id") or "")
    if not value:
        raise HTTPException(400, {"error": {"code": "teams_missing_tenant"}})
    return value


async def _send_activity(request: Request, inbound: dict[str, Any], connection: TeamsConnection, card: dict[str, Any]) -> None:
    service_url = str(inbound.get("serviceUrl") or "")
    parsed = urlparse(service_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.hostname.endswith(".trafficmanager.net")
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(400, {"error": {"code": "teams_invalid_service_url"}})
    secret = decrypt_llm_config(connection.app_secret_ref).get("value") if connection.app_secret_ref else ""
    if not secret:
        raise HTTPException(503, {"error": {"code": "teams_credentials_missing"}})
    async with httpx2.AsyncClient(timeout=15) as client:
        token_response = await client.post(
            f"https://login.microsoftonline.com/{connection.entra_tenant_id}/oauth2/v2.0/token",
            data={
                "client_id": connection.microsoft_app_id,
                "client_secret": secret,
                "grant_type": "client_credentials",
                "scope": "https://api.botframework.com/.default",
            },
        )
        token_response.raise_for_status()
        conversation = inbound.get("conversation") if isinstance(inbound.get("conversation"), dict) else {}
        attachment = (
            {"contentType": "application/vnd.microsoft.card.oauth", "content": card["_oauth"]}
            if "_oauth" in card
            else {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
        )
        response = await client.post(
            f"{service_url.rstrip('/')}/v3/conversations/{quote(conversation['id'], safe='')}/activities",
            headers={"Authorization": f"Bearer {token_response.json()['access_token']}"},
            json={"type": "message", "attachments": [attachment]},
        )
        response.raise_for_status()


def _card(title: str, body: str) -> dict[str, Any]:
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.5",
        "body": [{"type": "TextBlock", "weight": "Bolder", "text": title}, {"type": "TextBlock", "wrap": True, "text": body}],
    }


def _workspace_picker_card(spaces: list[dict[str, Any]], locale: str) -> dict[str, Any]:
    card = _card(im_interface_message("channel_space_picker_title", locale), im_interface_message("teams_space_picker_intro", locale))
    if len(spaces) > 6:
        title = im_interface_message("channel_space_picker_title", locale)
        card["body"].append(
            {
                "type": "Input.ChoiceSet",
                "style": "compact",
                "id": "workspace_id",
                "label": title,
                "isRequired": True,
                "choices": [{"title": str(space.get("name") or space["workspace_id"])[:60], "value": str(space["workspace_id"])} for space in spaces],
            }
        )
        card["actions"] = [{"type": "Action.Execute", "title": title, "verb": "bowhead.workspace.select", "data": {"locale": locale}}]
        return card
    card["actions"] = [
        {
            "type": "Action.Execute",
            "title": str(space.get("name") or space["workspace_id"])[:60],
            "verb": "bowhead.workspace.select",
            "data": {"workspace_id": str(space["workspace_id"]), "locale": locale},
        }
        for space in spaces
    ]
    return card


def _signin_card(connection: TeamsConnection, locale: str) -> dict[str, Any]:
    return {
        "_oauth": {
            "connectionName": connection.sso_connection_name,
            "text": im_interface_message("teams_signin", locale),
            "tokenExchangeResource": {
                "id": f"teams-sso-{uuid4().hex}",
                "connectionName": connection.sso_connection_name,
                "uri": connection.sso_audience,
            },
        }
    }


def _config_payload(connection: TeamsConnection | None) -> dict[str, Any]:
    if connection is None:
        return {"configured": False, "status": "draft", "allowed_scopes": ["personal"]}
    return {
        "configured": True,
        "id": connection.id,
        "entra_tenant_id": connection.entra_tenant_id,
        "microsoft_app_id": connection.microsoft_app_id,
        "app_type": connection.app_type,
        "sso_auth_app_id": connection.sso_auth_app_id,
        "sso_audience": connection.sso_audience,
        "sso_connection_name": connection.sso_connection_name,
        "teams_manifest_id": connection.teams_manifest_id,
        "status": connection.status,
        "secret_expires_at": connection.secret_expires_at,
        "app_secret_configured": bool(connection.app_secret_ref),
        "allowed_scopes": ["personal"],
        "updated_at": connection.updated_at,
    }


async def personal_session_id(app, connection, conversation_id, user_id, workspace_id):
    legacy = f"teams:{connection.entra_tenant_id}:{conversation_id}:{user_id}"
    existing = await app.state.orchestrator.session_store.get_session(legacy)
    if existing and existing.user_id == user_id and existing.workspace_id == workspace_id:
        return legacy
    scope = [connection.id, connection.microsoft_app_id, connection.entra_tenant_id, conversation_id, user_id, workspace_id]
    return "teams:" + hashlib.sha256(json.dumps(scope).encode()).hexdigest()


async def _start_turn(
    *,
    request: Request,
    activity: dict[str, Any],
    connection: TeamsConnection,
    user_id: str,
    team_id: str,
    workspace_id: str,
    text: str,
    session_id: str,
    turn_id: str,
    access_context: dict,
    activity_key: str,
) -> None:
    """Use the same team-scoped runtime resolution as web and Feishu turns."""
    from bowhead_core import TurnRequest
    from bowhead_gateway.channel_language import resolve_channel_turn_language
    from bowhead_gateway.datarc_context import DatarcTurnContext, recovered_datarc_source, resolve_datarc_turn_context
    from bowhead_gateway.turn_service import (
        admit_legacy_chat_execution,
        is_session_busy_rejection,
        is_session_deleted,
        resolve_team_model_runtime,
        session_has_active_turn,
        spawn_turn,
    )

    language_context = await resolve_channel_turn_language(
        request.app,
        user_id=user_id,
        workspace_id=workspace_id,
        team_id=team_id,
        session_id=session_id,
        message_text=text,
        client_language=_client_language(activity),
    )
    # Refusals, the processing notice, and mirrored cards all use this turn's language.
    locale = language_context.ui_language
    orchestrator = getattr(request.app.state, "orchestrator", None)
    user = await request.app.state.user_store.get_user(user_id)
    workspace = await request.app.state.user_store.get_workspace(workspace_id)
    if orchestrator is None or user is None or workspace is None:
        await _send_activity(
            request,
            activity,
            connection,
            _card(im_interface_message("teams_not_ready_title", locale), im_interface_message("teams_not_ready", locale)),
        )
        return
    from bowhead_gateway.credit_admission import check_credit_admission
    from bowhead_gateway.im.error_messages import im_error_message

    credit_denial = await check_credit_admission(
        request.app.state.user_store,
        team_id=team_id,
        user_id=user_id,
        workspace_id=workspace_id,
        metrics_log=getattr(request.app.state, "metrics_log", None),
    )
    if credit_denial is not None:
        await _send_activity(
            request,
            activity,
            connection,
            _card(im_interface_message("teams_credits_exhausted_title", locale), im_error_message(credit_denial, locale)),
        )
        return
    runtime = await resolve_team_model_runtime(request.app.state, team_id, entrypoint="tool_continuation")
    if not runtime.available:
        await _send_activity(
            request,
            activity,
            connection,
            _card(im_interface_message("teams_model_unconfigured_title", locale), im_interface_message("teams_model_unconfigured", locale)),
        )
        return
    context = await resolve_datarc_turn_context(request.app, team_id=team_id, workspace_id=workspace_id, user_id=user_id, session_id=session_id)
    member_uid = ""
    if context.datarc_project_for_calls:
        if not user.email or not getattr(user, "email_verified", False):
            context = DatarcTurnContext(datarc_unavailable=True, datarc_absence_reason="no_identity")
        else:
            try:
                member_uid = await request.app.state.member_resolver.resolve(
                    user.email, context.datarc_project_for_calls, datarc=context.datarc_client
                )
            except Exception:
                logger.warning("teams.datarc_member_unavailable team=%s user=%s", team_id, user_id)
                context = DatarcTurnContext(datarc_unavailable=True, datarc_absence_reason="member_unavailable")
    if await is_session_deleted(orchestrator, session_id):
        raise HTTPException(410, {"error": {"code": "session_deleted"}})
    await orchestrator.session_store.ensure_session(session_id=session_id, workspace_id=workspace_id, user_id=user_id)
    busy_card = _card(im_interface_message("session_busy_title", locale), im_interface_message("channel_session_busy", locale))
    # Teams has no steer and one Session per personal chat: any message during a running Turn lands here.
    if await session_has_active_turn(orchestrator, session_id):
        await _send_activity(request, activity, connection, busy_card)
        return
    turn = TurnRequest(
        session_id=session_id,
        workspace_id=workspace_id,
        active_datarc_project=context.active_datarc_project,
        datarc_absence_reason=context.datarc_absence_reason,
        accessible_datarc_workspaces=context.accessible_datarc_workspaces,
        active_datarc_workspace_name=context.active_datarc_workspace_name,
        access_context=access_context,
        datarc_binding=context.connection_binding,
        datarc_source_evidence=recovered_datarc_source(context, user_id, team_id, member_uid),
        team_kb_retriever=getattr(runtime, "kb_retriever", None),
        datarc_client=context.datarc_client,
        datarc_unavailable=context.datarc_unavailable,
        vector_client=context.vector_client,
        team_router=runtime.router,
        user_id=user_id,
        member_uid=member_uid,
        text=text,
        turn_id=turn_id,
        user_profile={"display_name": str(getattr(user, "display_name", "") or "")},
        language_context=language_context,
        team_id=team_id,
    )
    from bowhead_gateway.teams.delivery import track_delivery

    await track_delivery(request.app, activity_key, activity, connection, turn, locale, language_context.output_language)
    bus = await admit_legacy_chat_execution(orchestrator, turn)
    try:
        await spawn_turn(
            orchestrator,
            turn,
            user_store=request.app.state.user_store,
            team_id=team_id,
            entitlements=getattr(request.app.state, "entitlements", None),
            name_prefix="turn_run_teams",
            admitted_legacy_bus=bus,
        )
    except ValueError as exc:
        # Another Turn started after the pre-check; spawn_turn already released this one.
        if not is_session_busy_rejection(exc):
            raise
        await _send_activity(request, activity, connection, busy_card)
        return
    try:
        await _send_activity(request, activity, connection, _card(im_interface_message("processing_title", locale), text[:1000]))
    except (HTTPException, httpx2.HTTPError):
        logger.info("teams.processing_notice_failed turn=%s", turn_id)


async def teams_answer_body(
    neutral: Any, text: str, app: Any, *, locale: str, answer_locale: str | None = None, max_length: int | None = 12000
) -> str:
    """Teams answer-card body rendered like Feishu: [cite:N] becomes [N] and machine markers are removed.

    Knowledge-base citations are listed at the end, filtered by the asker's read access
    (Teams P0 only has one-to-one chats). Sending the raw text would expose [cite:N] markers.
    """
    from bowhead_gateway.im.blocks import format_source_lines, resolve_markers
    from bowhead_gateway.im.turn_render import kb_sources_block, refresh_kb_citation_labels

    # The empty-answer fallback follows the answer language; the source heading follows the interface.
    body = resolve_markers(text) or im_interface_message("completed", answer_locale or locale)
    await refresh_kb_citation_labels(neutral, app, private_chat=True)
    kb_sources = kb_sources_block(neutral, locale)
    if kb_sources is not None:
        heading, lines = format_source_lines(kb_sources.entries, kb_sources.locale)
        body = f"{body}\n\n**{heading}**\n" + "\n".join(lines)
    return body[:max_length]


def _turn_event_title_and_body(event_type: str, locale: str, answer_locale: str) -> tuple[str, str] | None:
    """Fixed Teams copy for mirrored non-answer turn events; None when the event is not shown.

    Clarify-in-web is tied to the answer and follows the answer language; failure follows the interface.
    """
    if event_type == "clarification_required":
        return (
            im_interface_message("channel_clarify_in_web_title", answer_locale),
            im_interface_message("teams_clarify_in_web", answer_locale),
        )
    if event_type in {"error", "turn_failed", "turn_aborted"}:
        return im_interface_message("status_failed", locale), im_interface_message("channel_turn_incomplete", locale)
    return None


async def event_card(app, neutral, event, locale, answer_locale):
    payload = event.payload if isinstance(event.payload, dict) else {}
    if event.event_type == "final_answer":
        body = await teams_answer_body(neutral, str(payload.get("text") or ""), app, locale=locale, answer_locale=answer_locale)
        return _card(im_interface_message("answer_title", answer_locale), body)
    if event.event_type == "clarification_required":
        from bowhead_gateway.teams.clarification import question_card

        card = await question_card(app, payload.get("request_id"), neutral.turn_id, answer_locale)
        if card:
            return card
    fixed = _turn_event_title_and_body(event.event_type, locale, answer_locale)
    return _card(*fixed) if fixed else None
