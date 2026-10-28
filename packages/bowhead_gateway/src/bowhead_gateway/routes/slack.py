"""Customer-managed Slack App integration for local deployments.

All Slack-facing requests are authenticated with Slack's signing secret before
they can create work.  The only public endpoints are callback endpoints; all
connection management remains behind the team-admin dependency.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

import httpx2
from bowhead_core.llm_config_crypto import decrypt_llm_config, encrypt_llm_config
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from bowhead_gateway.im.copy_locale import resolve_channel_copy_locale
from bowhead_gateway.im.error_messages import im_interface_message
from bowhead_gateway.product_identity import product_name
from bowhead_gateway.public_url import PublicEntryRequiredError, restore_oauth_public_access_context, team_public_app_url
from bowhead_gateway.routes._deps import require_team_admin
from bowhead_gateway.slack.store import SlackConnection, SlackStore

logger = logging.getLogger(__name__)
router = APIRouter()
TEAM_ADMIN_DEP = Depends(require_team_admin)
_BOT_SCOPES = "app_mentions:read,chat:write,commands,im:history,users:read,users:read.email"
# The space picker's actions block carries the locale it was rendered in, so the
# interaction reply can follow the triggering message's language.
_WORKSPACE_BLOCK_PREFIX = "bowhead.workspace:"


class UpdateSlackConfig(BaseModel):
    client_id: str | None = Field(default=None, max_length=128)
    client_secret: str | None = Field(default=None, max_length=4096)
    signing_secret: str | None = Field(default=None, max_length=4096)
    channel_answers_enabled: bool | None = None
    status: str | None = None


def _store(request: Request) -> SlackStore:
    store = getattr(request.app.state, "slack_store", None)
    if store is None:
        raise HTTPException(503, {"error": {"code": "slack_not_ready"}})
    return store


def _local_only(request: Request) -> None:
    if getattr(request.app.state, "deployment_mode", "saas") != "local":
        raise HTTPException(404, {"error": {"code": "slack_local_only"}})


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    _local_only(request)
    return {"ok": True, "enabled": False, "degraded": False}


@router.get("/admin/config")
async def admin_config(request: Request, admin: Any = TEAM_ADMIN_DEP) -> dict[str, Any]:
    _local_only(request)
    return await _config_payload(request, await _store(request).by_team(str(admin.team_id)))


@router.patch("/admin/config")
async def update_config(request: Request, body: UpdateSlackConfig, admin: Any = TEAM_ADMIN_DEP) -> dict[str, Any]:
    _local_only(request)
    if body.status is not None and body.status not in {"draft", "verified", "enabled", "disabled"}:
        raise HTTPException(422, {"error": {"code": "slack_invalid_status"}})
    values: dict[str, object] = {name: getattr(body, name) for name in body.model_fields_set if name not in {"client_secret", "signing_secret"}}
    if "channel_answers_enabled" in values:
        values["channel_answers_enabled"] = int(bool(values["channel_answers_enabled"]))
    if "client_secret" in body.model_fields_set:
        values["client_secret_ref"] = encrypt_llm_config({"value": body.client_secret or ""}) if body.client_secret else None
    if "signing_secret" in body.model_fields_set:
        values["signing_secret_ref"] = encrypt_llm_config({"value": body.signing_secret or ""}) if body.signing_secret else None
    connection = await _store(request).upsert(team_id=str(admin.team_id), actor_id=str(admin.id), values=values)
    if connection.status == "enabled":
        _require_enabled(connection)
    return await _config_payload(request, connection)


@router.post("/admin/install")
async def begin_install(request: Request, admin: Any = TEAM_ADMIN_DEP) -> dict[str, str]:
    _local_only(request)
    connection = await _store(request).by_team(str(admin.team_id))
    if connection is None or not connection.client_id or not connection.client_secret_ref or not connection.signing_secret_ref:
        raise HTTPException(422, {"error": {"code": "slack_missing_configuration"}})
    try:
        callback = await team_public_app_url(request, team_id=str(admin.team_id), path="/api/v1/integrations/slack/oauth/callback")
    except PublicEntryRequiredError as exc:
        raise HTTPException(409, {"error": {"code": "public_entry_required"}}) from exc
    state = await _store(request).create_oauth_state(connection.id, redirect_uri=callback)
    query = urlencode({"client_id": connection.client_id, "scope": _BOT_SCOPES, "redirect_uri": callback, "state": state})
    return {"install_url": f"https://slack.com/oauth/v2/authorize?{query}"}


@router.get("/oauth/callback")
async def oauth_callback(request: Request, code: str = "", state: str = "", error: str = "") -> RedirectResponse:
    pending = await _store(request).consume_oauth_state(state)
    if pending is None:
        return await _install_result_redirect(request, team_id=None, result="failed")
    if pending.redirect_uri:
        restore_oauth_public_access_context(pending.redirect_uri, callback_path="/api/v1/integrations/slack/oauth/callback")
    connection = await _store(request).by_id(pending.connection_id)
    if connection is None:
        return await _install_result_redirect(request, team_id=None, result="failed")
    if error or not code or not connection.client_secret_ref:
        return await _install_result_redirect(request, team_id=connection.team_id, result="failed")
    callback = pending.redirect_uri or await team_public_app_url(
        request, team_id=connection.team_id, path="/api/v1/integrations/slack/oauth/callback"
    )
    secret = _secret(connection.client_secret_ref)
    async with httpx2.AsyncClient(timeout=15) as client:
        response = await client.post(
            "https://slack.com/api/oauth.v2.access",
            data={"client_id": connection.client_id, "client_secret": secret, "code": code, "redirect_uri": callback},
        )
        payload = response.json()
    if not response.is_success or not payload.get("ok"):
        logger.warning("slack.oauth_exchange_failed connection=%s error=%s", connection.id, payload.get("error"))
        return await _install_result_redirect(request, team_id=connection.team_id, result="failed")
    team = payload.get("team") if isinstance(payload.get("team"), dict) else {}
    bot_user_id = str(payload.get("bot_user_id") or "")
    values = {
        "slack_team_id": str(team.get("id") or ""),
        "slack_app_id": str(payload.get("app_id") or ""),
        "bot_user_id": bot_user_id,
        "bot_token_ref": encrypt_llm_config({"value": str(payload.get("access_token") or "")}),
        "status": "verified",
    }
    await _store(request).upsert(team_id=connection.team_id, actor_id=connection.updated_by_user_id, values=values)
    return await _install_result_redirect(request, team_id=connection.team_id, result="success")


async def _install_result_redirect(request: Request, *, team_id: str | None, result: str) -> RedirectResponse:
    path = f"/team/settings/integrations/slack/?install={result}"
    from bowhead_gateway.public_entry_context import current_request_public_access_context
    from bowhead_gateway.public_url import oauth_return_path, public_app_url_for_base, team_public_entry

    address = current_request_public_access_context()
    if address is not None and address.prefix_known:
        return RedirectResponse(oauth_return_path(path, callback_path="/api/v1/integrations/slack/oauth/callback"), status_code=303)
    if team_id:
        try:
            entry = await team_public_entry(request, team_id=team_id)
            return RedirectResponse(public_app_url_for_base(entry.base_url, path), status_code=303)
        except PublicEntryRequiredError:
            pass
        except Exception:
            logger.exception("slack.return_entry_unavailable")
    from bowhead_gateway.public_url import oauth_return_path

    return RedirectResponse(oauth_return_path(path, callback_path="/api/v1/integrations/slack/oauth/callback"), status_code=303)


@router.post("/events/{connection_id}")
async def events(connection_id: str, request: Request) -> dict[str, Any]:
    connection = await _event_connection(request, connection_id)
    body = await _json_body(request)
    if body.get("type") == "url_verification":
        return {"challenge": str(body.get("challenge") or "")}
    if body.get("type") != "event_callback" or not await _store(request).claim_event(connection.id, str(body.get("event_id") or "")):
        return {"ok": True}
    event = body.get("event") if isinstance(body.get("event"), dict) else {}
    if str(body.get("team_id") or "") != connection.slack_team_id:
        raise HTTPException(403, {"error": {"code": "slack_team_mismatch"}})
    task = asyncio.create_task(_handle_event(request.app, connection, event), name=f"slack_event:{body.get('event_id')}")
    _track_task(request.app, task)
    return {"ok": True}


@router.post("/commands/{connection_id}")
async def command(connection_id: str, request: Request) -> dict[str, Any]:
    connection = await _authenticated_connection(request, connection_id)
    form = dict(await request.form())
    if str(form.get("team_id") or "") != connection.slack_team_id:
        raise HTTPException(403, {"error": {"code": "slack_team_mismatch"}})
    # Same session as the turn this command starts, so an evidence-free command follows the previous answer.
    locale = await _copy_locale(
        request.app,
        connection,
        str(form.get("user_id") or ""),
        text=str(form.get("text") or ""),
        channel_id=str(form.get("channel_id") or ""),
        thread_ts=str(form.get("thread_ts") or ""),
    )
    task = asyncio.create_task(
        _handle_inbound(
            request.app,
            connection,
            user_id=str(form.get("user_id") or ""),
            channel_id=str(form.get("channel_id") or ""),
            thread_ts=str(form.get("thread_ts") or ""),
            text=str(form.get("text") or ""),
            is_channel=False,
        ),
        name="slack_command",
    )
    _track_task(request.app, task)
    return {"response_type": "ephemeral", "text": im_interface_message("slack_command_ack", locale)}


@router.post("/interactions/{connection_id}")
async def interactions(connection_id: str, request: Request) -> dict[str, Any]:
    connection = await _authenticated_connection(request, connection_id)
    form = await request.form()
    try:
        payload = json.loads(str(form.get("payload") or "{}"))
    except json.JSONDecodeError as exc:
        raise HTTPException(400, {"error": {"code": "slack_invalid_interaction"}}) from exc
    team = payload.get("team") if isinstance(payload.get("team"), dict) else {}
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    if str(team.get("id") or "") != connection.slack_team_id:
        raise HTTPException(403, {"error": {"code": "slack_team_mismatch"}})
    actions = payload.get("actions") if isinstance(payload.get("actions"), list) else []
    action = actions[0] if actions and isinstance(actions[0], dict) else {}
    value = str(action.get("value") or "")
    block_id = str(action.get("block_id") or "")
    rendered_locale = block_id.removeprefix(_WORKSPACE_BLOCK_PREFIX) if block_id.startswith(_WORKSPACE_BLOCK_PREFIX) else None
    if action.get("action_id") == "bowhead.workspace" and value:
        channel = payload.get("channel") if isinstance(payload.get("channel"), dict) else {}
        await _select_workspace(request.app, connection, str(user.get("id") or ""), str(channel.get("id") or ""), value)
        # The space is now selected and validated, so its default can apply to the receipt.
        locale = await _copy_locale(request.app, connection, str(user.get("id") or ""), rendered_locale=rendered_locale, workspace_id=value)
        return {
            "replace_original": True,
            "blocks": _card(im_interface_message("channel_space_switched_title", locale), im_interface_message("slack_space_switched", locale)),
        }
    locale = await _copy_locale(request.app, connection, str(user.get("id") or ""), rendered_locale=rendered_locale)
    return {"response_action": "errors", "errors": {"action": im_interface_message("slack_action_expired", locale)}}


async def _handle_event(app: Any, connection: SlackConnection, event: dict[str, Any]) -> None:
    kind = str(event.get("type") or "")
    if kind == "app_mention":
        await _handle_inbound(
            app,
            connection,
            str(event.get("user") or ""),
            str(event.get("channel") or ""),
            str(event.get("thread_ts") or event.get("ts") or ""),
            _strip_mention(str(event.get("text") or "")),
            True,
        )
    elif kind == "message" and str(event.get("channel_type") or "") == "im" and not event.get("bot_id") and not event.get("subtype"):
        await _handle_inbound(
            app,
            connection,
            str(event.get("user") or ""),
            str(event.get("channel") or ""),
            str(event.get("thread_ts") or event.get("ts") or ""),
            str(event.get("text") or ""),
            False,
        )


async def _handle_inbound(app: Any, connection: SlackConnection, user_id: str, channel_id: str, thread_ts: str, text: str, is_channel: bool) -> None:
    if not user_id or not channel_id or not text.strip():
        return
    if is_channel and not connection.channel_answers_enabled:
        locale = await _copy_locale(app, connection, user_id, text=text)
        await _post(connection, channel_id, im_interface_message("slack_channel_answers_disabled", locale), thread_ts)
        return
    bound_user = await _resolve_user(app, connection, user_id)
    if bound_user is None:
        locale = await resolve_channel_copy_locale(app, user_id=None, message_text=text, team_id=connection.team_id)
        await _post(connection, channel_id, im_interface_message("slack_account_not_found", locale), thread_ts)
        return
    spaces = await app.state.user_store.list_workspaces_for_user(bound_user.id, team_id=connection.team_id)
    workspace_id = await _store_for_app(app).workspace(connection.id, channel_id, user_id)
    if workspace_id not in {str(space["workspace_id"]) for space in spaces}:
        workspace_id = None
    if workspace_id is None and len(spaces) != 1:
        locale = await resolve_channel_copy_locale(app, user_id=bound_user.id, message_text=text, team_id=connection.team_id)
        if not spaces:
            await _post(connection, channel_id, im_interface_message("slack_no_spaces", locale), thread_ts)
            return
        await _post_blocks(connection, channel_id, _workspace_card(spaces, locale), thread_ts)
        return
    await _start_turn(app, connection, bound_user, str(workspace_id or spaces[0]["workspace_id"]), channel_id, thread_ts, text.strip())


async def _resolve_user(app: Any, connection: SlackConnection, slack_user_id: str) -> Any | None:
    store = _store_for_app(app)
    bound_id = await store.get_identity(connection.id, slack_user_id)
    if bound_id:
        user = await app.state.user_store.get_user(bound_id)
        if user and await app.state.user_store.user_has_active_team_membership(user.id, connection.team_id):
            return user
    email = await _user_email(connection, slack_user_id)
    user = await app.state.user_store.get_user_by_email(email) if email else None
    if user is None or not await app.state.user_store.user_has_active_team_membership(user.id, connection.team_id):
        return None
    await store.bind_identity(connection.id, slack_user_id, user.id)
    return user


async def _start_turn(app: Any, connection: SlackConnection, user: Any, workspace_id: str, channel_id: str, thread_ts: str, text: str) -> None:
    from bowhead_core import TurnRequest
    from bowhead_gateway._turn_runtime import _resolve_turn_datarc_runtime
    from bowhead_gateway.channel_language import resolve_channel_turn_language
    from bowhead_gateway.turn_service import admit_legacy_chat_execution, resolve_team_model_runtime, spawn_turn

    session_id = _session_id(connection, channel_id, thread_ts, user.id)
    language_context = await resolve_channel_turn_language(
        app,
        user_id=user.id,
        workspace_id=workspace_id,
        team_id=connection.team_id,
        session_id=session_id,
        message_text=text,
    )
    # Refusals, the processing notice, and mirrored cards all use this turn's language.
    locale = language_context.ui_language
    orchestrator = getattr(app.state, "orchestrator", None)
    workspace = await app.state.user_store.get_workspace(workspace_id)
    if orchestrator is None or workspace is None:
        await _post(connection, channel_id, im_interface_message("slack_not_ready", locale), thread_ts)
        return
    from bowhead_gateway.credit_admission import check_credit_admission
    from bowhead_gateway.im.error_messages import im_error_message

    credit_denial = await check_credit_admission(
        app.state.user_store,
        team_id=connection.team_id,
        user_id=user.id,
        workspace_id=workspace_id,
        metrics_log=getattr(app.state, "metrics_log", None),
    )
    if credit_denial is not None:
        await _post(connection, channel_id, im_error_message(credit_denial, locale), thread_ts)
        return
    runtime = await resolve_team_model_runtime(app.state, connection.team_id, entrypoint="tool_continuation")
    if not runtime.available:
        await _post(connection, channel_id, im_interface_message("slack_model_unconfigured", locale), thread_ts)
        return
    datarc_client, datarc_unavailable, vector_client = await _resolve_turn_datarc_runtime(SimpleNamespace(app=app), connection.team_id)
    datarc_workspace_id = str(workspace.get("datarc_workspace_id") or workspace_id)
    if datarc_workspace_id.startswith("ws_") or datarc_client is None:
        await _post(connection, channel_id, im_interface_message("channel_datarc_missing", locale), thread_ts)
        return
    try:
        member_uid = await app.state.member_resolver.resolve(user.email, datarc_workspace_id, datarc=datarc_client)
    except Exception:
        logger.warning("slack.member_uid_unavailable team=%s user=%s", connection.team_id, user.id, exc_info=True)
        await _post(connection, channel_id, im_interface_message("slack_data_permissions_unavailable", locale), thread_ts)
        return
    await orchestrator.session_store.ensure_session(session_id=session_id, workspace_id=workspace_id, user_id=user.id)
    turn = TurnRequest(
        session_id=session_id,
        workspace_id=workspace_id,
        active_datarc_project=datarc_workspace_id,
        datarc_client=datarc_client,
        datarc_unavailable=datarc_unavailable,
        vector_client=vector_client,
        team_router=runtime.router,
        user_id=user.id,
        member_uid=member_uid,
        text=text,
        turn_id=f"t_{uuid4().hex[:12]}",
        user_profile={"display_name": str(getattr(user, "display_name", "") or "")},
        language_context=language_context,
        team_id=connection.team_id,
    )
    bus = await admit_legacy_chat_execution(orchestrator, turn)
    await _post(connection, channel_id, im_interface_message("slack_processing", locale), thread_ts)
    await spawn_turn(
        orchestrator,
        turn,
        user_store=app.state.user_store,
        team_id=connection.team_id,
        entitlements=getattr(app.state, "entitlements", None),
        name_prefix="turn_run_slack",
        admitted_legacy_bus=bus,
    )
    task = asyncio.create_task(
        _mirror_turn(connection, channel_id, thread_ts, turn.turn_id, locale, language_context.output_language), name=f"slack_mirror:{turn.turn_id}"
    )
    _track_task(app, task)


async def _mirror_turn(connection: SlackConnection, channel_id: str, thread_ts: str, turn_id: str, locale: str, answer_locale: str) -> None:
    from bowhead_core import get_event_registry

    bus = await get_event_registry().get_or_create(turn_id, producer=False)
    async for event in bus.subscribe():
        payload = event.payload if isinstance(event.payload, dict) else {}
        card = _turn_event_card(event.event_type, payload, locale, answer_locale)
        if card is not None:
            await _post_blocks(connection, channel_id, card, thread_ts)
        if event.event_type in {"done", "error", "turn_failed", "turn_aborted"}:
            return


async def _authenticated_connection(request: Request, connection_id: str) -> SlackConnection:
    _local_only(request)
    connection = await _store(request).enabled(connection_id)
    if connection is None or not connection.signing_secret_ref:
        raise HTTPException(404, {"error": {"code": "slack_connection_not_enabled"}})
    raw = await request.body()
    _verify_signature(request, raw, _secret(connection.signing_secret_ref))
    request.state.slack_raw_body = raw
    return connection


async def _event_connection(request: Request, connection_id: str) -> SlackConnection:
    """Accept Slack URL verification after OAuth but before administrator enablement."""
    _local_only(request)
    connection = await _store(request).by_id(connection_id)
    if connection is None or connection.status not in {"verified", "enabled"} or not connection.signing_secret_ref:
        raise HTTPException(404, {"error": {"code": "slack_connection_not_enabled"}})
    raw = await request.body()
    _verify_signature(request, raw, _secret(connection.signing_secret_ref))
    request.state.slack_raw_body = raw
    return connection


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        value = json.loads(getattr(request.state, "slack_raw_body", b"").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, {"error": {"code": "slack_invalid_json"}}) from exc
    if not isinstance(value, dict):
        raise HTTPException(400, {"error": {"code": "slack_invalid_json"}})
    return value


def _verify_signature(request: Request, raw: bytes, secret: str) -> None:
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    try:
        stale = abs(time.time() - int(timestamp)) > 300
    except ValueError:
        stale = True
    expected = "v0=" + hmac.new(secret.encode(), b"v0:" + timestamp.encode() + b":" + raw, hashlib.sha256).hexdigest()
    if stale or not signature or not hmac.compare_digest(expected, signature):
        raise HTTPException(401, {"error": {"code": "slack_invalid_signature"}})


def _secret(reference: str) -> str:
    return str(decrypt_llm_config(reference).get("value") or "")


def _require_enabled(connection: SlackConnection) -> None:
    missing = [
        name
        for name, value in {
            "client_id": connection.client_id,
            "client_secret": connection.client_secret_ref,
            "signing_secret": connection.signing_secret_ref,
            "bot_token": connection.bot_token_ref,
            "slack_team_id": connection.slack_team_id,
            "bot_user_id": connection.bot_user_id,
        }.items()
        if not value
    ]
    if missing:
        raise HTTPException(422, {"error": {"code": "slack_missing_configuration", "fields": missing}})


async def _config_payload(request: Request, connection: SlackConnection | None) -> dict[str, Any]:
    if connection is None:
        return {"configured": False, "status": "draft", "channel_answers_enabled": False}
    personal_mcp_enabled = os.environ.get("FEATURE_PERSONAL_SLACK_MCP", "0") == "1"
    try:
        base = await team_public_app_url(request, team_id=connection.team_id, path=f"/api/v1/integrations/slack/events/{connection.id}")
        command = await team_public_app_url(request, team_id=connection.team_id, path=f"/api/v1/integrations/slack/commands/{connection.id}")
        interaction = await team_public_app_url(request, team_id=connection.team_id, path=f"/api/v1/integrations/slack/interactions/{connection.id}")
        oauth_callback = await team_public_app_url(request, team_id=connection.team_id, path="/api/v1/integrations/slack/oauth/callback")
        personal_mcp_oauth_callback = (
            await team_public_app_url(request, team_id=connection.team_id, path="/api/v1/personal-mcp/slack/oauth/callback")
            if personal_mcp_enabled
            else None
        )
    except PublicEntryRequiredError:
        base = command = interaction = oauth_callback = ""
        personal_mcp_oauth_callback = "" if personal_mcp_enabled else None
    return {
        "configured": True,
        "id": connection.id,
        "client_id": connection.client_id,
        "slack_team_id": connection.slack_team_id,
        "slack_app_id": connection.slack_app_id,
        "bot_user_id": connection.bot_user_id,
        "status": connection.status,
        "channel_answers_enabled": bool(connection.channel_answers_enabled),
        "client_secret_configured": bool(connection.client_secret_ref),
        "signing_secret_configured": bool(connection.signing_secret_ref),
        "bot_token_configured": bool(connection.bot_token_ref),
        "event_url": base,
        "command_url": command,
        "interaction_url": interaction,
        "oauth_callback_url": oauth_callback,
        "personal_mcp_oauth_callback_url": personal_mcp_oauth_callback,
        "required_scopes": _BOT_SCOPES.split(","),
        "slack_settings": _slack_settings_urls(connection),
        "updated_at": connection.updated_at,
    }


def _slack_settings_urls(connection: SlackConnection) -> dict[str, str] | None:
    if not connection.slack_app_id:
        return None
    base = f"https://api.slack.com/apps/{connection.slack_app_id}"
    return {
        "events": f"{base}/event-subscriptions",
        "slash_commands": f"{base}/slash-commands",
        "interactivity": f"{base}/interactive-messages",
    }


async def _user_email(connection: SlackConnection, user_id: str) -> str:
    token = _secret(connection.bot_token_ref or "")
    async with httpx2.AsyncClient(timeout=12) as client:
        response = await client.get("https://slack.com/api/users.info", params={"user": user_id}, headers={"Authorization": f"Bearer {token}"})
        payload = response.json()
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    profile = user.get("profile") if isinstance(user.get("profile"), dict) else {}
    return str(profile.get("email") or "").strip().lower()


async def _post(connection: SlackConnection, channel: str, text: str, thread_ts: str) -> None:
    await _post_blocks(connection, channel, _card(product_name(), text), thread_ts)


async def _post_blocks(connection: SlackConnection, channel: str, blocks: list[dict[str, Any]], thread_ts: str) -> None:
    token = _secret(connection.bot_token_ref or "")
    async with httpx2.AsyncClient(timeout=20) as client:
        response = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel, "thread_ts": thread_ts or None, "text": product_name(), "blocks": blocks},
        )
    if not response.is_success or not response.json().get("ok"):
        logger.warning("slack.post_failed connection=%s", connection.id)


def _turn_event_card(event_type: str, payload: dict[str, Any], locale: str, answer_locale: str) -> list[dict[str, Any]] | None:
    """Slack card for one mirrored turn event; None when the event is not shown.

    Cards tied to the answer (answer title, empty-answer fallback, clarify-in-web) follow the
    turn's answer language; the failure card follows the interface language.
    """
    if event_type == "final_answer":
        return _card(
            im_interface_message("answer_title", answer_locale),
            str(payload.get("text") or im_interface_message("completed", answer_locale)),
        )
    if event_type == "clarification_required":
        return _card(
            im_interface_message("channel_clarify_in_web_title", answer_locale),
            im_interface_message("slack_clarify_in_web", answer_locale),
        )
    if event_type in {"error", "turn_failed", "turn_aborted"}:
        return _card(im_interface_message("status_failed", locale), im_interface_message("channel_turn_incomplete", locale))
    return None


def _card(title: str, body: str) -> list[dict[str, Any]]:
    return [
        {"type": "header", "text": {"type": "plain_text", "text": title[:150]}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body[:12000]}},
    ]


def _workspace_card(spaces: list[dict[str, Any]], locale: str) -> list[dict[str, Any]]:
    blocks = _card(im_interface_message("channel_space_picker_title", locale), im_interface_message("slack_space_picker_intro", locale))
    blocks.append(
        {
            "type": "actions",
            "block_id": f"{_WORKSPACE_BLOCK_PREFIX}{locale}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": str(space.get("name") or space["workspace_id"])[:75]},
                    "action_id": "bowhead.workspace",
                    "value": str(space["workspace_id"]),
                }
                for space in spaces[:5]
            ],
        }
    )
    return blocks


async def _select_workspace(app: Any, connection: SlackConnection, slack_user_id: str, channel_id: str, workspace_id: str) -> None:
    user = await _resolve_user(app, connection, slack_user_id)
    spaces = await app.state.user_store.list_workspaces_for_user(user.id, team_id=connection.team_id) if user else []
    if workspace_id not in {str(space["workspace_id"]) for space in spaces}:
        raise HTTPException(403, {"error": {"code": "slack_workspace_selection_denied"}})
    await _store_for_app(app).set_workspace(connection.id, channel_id, slack_user_id, workspace_id)


async def _copy_locale(
    app: Any,
    connection: SlackConnection,
    slack_user_id: str,
    *,
    text: str = "",
    rendered_locale: object = None,
    workspace_id: str | None = None,
    channel_id: str = "",
    thread_ts: str = "",
) -> str:
    """Copy locale before a turn exists, via the shared channel resolver (card snapshot first)."""
    user_id: str | None = None
    if slack_user_id:
        try:
            user_id = await _store_for_app(app).get_identity(connection.id, slack_user_id)
        except Exception:  # noqa: BLE001 - The identity lookup only selects the copy language.
            logger.warning("slack.copy_locale_identity_failed connection=%s", connection.id)
    session_id = _session_id(connection, channel_id, thread_ts, user_id) if user_id and channel_id else None
    return await resolve_channel_copy_locale(
        app,
        user_id=user_id,
        message_text=text,
        rendered_locale=rendered_locale,
        team_id=connection.team_id,
        workspace_id=workspace_id,
        session_id=session_id,
    )


def _session_id(connection: SlackConnection, channel_id: str, thread_ts: str, user_id: str) -> str:
    """Bowhead session for one Slack thread and user; shared by turns and their receipts."""
    return f"slack:{connection.id}:{channel_id}:{thread_ts}:{user_id}"


def _store_for_app(app: Any) -> SlackStore:
    store = getattr(app.state, "slack_store", None)
    if store is None:
        raise RuntimeError("Slack store is unavailable")
    return store


def _track_task(app: Any, task: asyncio.Task[Any]) -> None:
    tasks: set[asyncio.Task[Any]] = getattr(app.state, "slack_background_tasks", set())
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    app.state.slack_background_tasks = tasks


def _strip_mention(text: str) -> str:
    return " ".join(part for part in text.split() if not (part.startswith("<@") and part.endswith(">"))).strip()
