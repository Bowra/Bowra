"""Personal MCP APIs have no team-admin bypass and never return credentials."""

import json
import logging
import os
from typing import Any, Literal
from urllib.parse import urlencode

import httpx2
from bowhead_core.llm_config_crypto import decrypt_llm_config
from bowhead_core.personal_mcp import (
    PersonalMcpError,
    PersonalMcpStore,
    _safe,
    auth_headers,
    discover,
    normalize_endpoint,
)
from bowhead_core.tool_registry import ToolExecutionContext
from bowhead_core.user_store import User, UserStore
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, SecretStr

from bowhead_gateway._entitlement_gate import feature_license_checker
from bowhead_gateway.public_url import PublicEntryRequiredError, restore_oauth_public_access_context, team_public_app_url
from bowhead_gateway.routes.auth import _current_user, _store
from bowhead_gateway.slack.store import SlackStore

router = APIRouter()
logger = logging.getLogger(__name__)
_SLACK_MCP_ENDPOINT = "https://mcp.slack.com/mcp"
_SLACK_USER_SCOPES = "search:read.public,search:read.private,search:read.mpim,search:read.im,search:read.files,search:read.users,channels:history,groups:history,mpim:history,im:history,channels:read,groups:read,mpim:read,im:read,users:read,users:read.email"


def _registry(request: Request, store: UserStore = Depends(_store)) -> PersonalMcpStore:
    return PersonalMcpStore(store.db_path, license_check=feature_license_checker(request.app, "mcp"))


async def _enabled(request: Request, user: User = Depends(_current_user)) -> None:
    if not await feature_license_checker(request.app, "mcp")(user.team_id):
        raise HTTPException(403, detail={"error": {"code": "feature_unlicensed", "details": {"feature": "mcp"}}})


def _slack_personal_enabled() -> bool:
    return os.environ.get("FEATURE_PERSONAL_SLACK_MCP", "0") == "1"


async def _slack_connection(request: Request, user: User) -> Any:
    if not _slack_personal_enabled():
        raise HTTPException(404, "personal_slack_mcp_unavailable")
    connection = await SlackStore(request.app.state.slack_store.db_path).by_team(user.team_id)
    if connection is None or connection.status != "enabled" or not connection.client_id or not connection.client_secret_ref:
        raise HTTPException(409, "personal_slack_mcp_team_unavailable")
    return connection


def _error(exc: PersonalMcpError) -> HTTPException:
    code = str(exc)
    return HTTPException(404 if code == "personal_mcp_not_found" else 409 if code == "personal_mcp_version_conflict" else 400, code)


class ConnectionInput(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    endpoint: str = Field(min_length=1, max_length=2048)
    auth_type: Literal["none", "bearer", "api_key"] = "none"
    value: SecretStr | None = None
    header: str = Field(default="X-API-Key", max_length=64)
    version: int | None = None


class ConfigurationInput(BaseModel):
    version: int = Field(ge=1)
    status: Literal["ready", "disabled", "deleted"]
    selected_tool_ids: list[str] = Field(default_factory=list, max_length=100)


class BindingInput(BaseModel):
    revision_id: str
    tool_ids: list[str] = Field(max_length=20)
    version: int = Field(ge=0)


class DecisionInput(BaseModel):
    decision: Literal["approve", "approve_session", "reject"]


@router.get("/capabilities")
async def capabilities(request: Request, user: User = Depends(_current_user)) -> dict[str, bool]:
    enabled = await feature_license_checker(request.app, "mcp")(user.team_id)
    slack = False
    if enabled and _slack_personal_enabled():
        connection = await SlackStore(request.app.state.slack_store.db_path).by_team(user.team_id)
        slack = bool(connection and connection.status == "enabled" and connection.client_id and connection.client_secret_ref)
    return {"enabled": enabled, "slack_connectable": slack}


@router.post("/slack/connect", dependencies=[Depends(_enabled)])
async def connect_slack(request: Request, user: User = Depends(_current_user)) -> dict[str, str]:
    connection = await _slack_connection(request, user)
    try:
        callback = await team_public_app_url(request, team_id=user.team_id, path="/api/v1/personal-mcp/slack/oauth/callback")
    except PublicEntryRequiredError as exc:
        raise HTTPException(409, "personal_slack_mcp_public_entry_required") from exc
    state = await SlackStore(request.app.state.slack_store.db_path).create_personal_oauth_state(
        connection.id,
        user.id,
        user.team_id,
        redirect_uri=callback,
    )
    return {
        "authorize_url": f"https://slack.com/oauth/v2/authorize?{urlencode({'client_id': connection.client_id, 'user_scope': _SLACK_USER_SCOPES, 'redirect_uri': callback, 'state': state})}"
    }


@router.get("/slack/oauth/callback")
async def slack_oauth_callback(request: Request, code: str = "", state: str = "", error: str = "") -> RedirectResponse:
    store = SlackStore(request.app.state.slack_store.db_path)
    pending = await store.consume_personal_oauth_state(state)
    if pending is not None and pending.redirect_uri:
        restore_oauth_public_access_context(pending.redirect_uri, callback_path="/api/v1/personal-mcp/slack/oauth/callback")
    if not _slack_personal_enabled():
        return _slack_callback_redirect("failed", "connection_unavailable")
    if error:
        reason = "authorization_denied" if error == "access_denied" else "authorization_failed"
        return _slack_callback_redirect("failed", reason)
    if pending is None:
        return _slack_callback_redirect("failed", "session_expired")
    if not code:
        return _slack_callback_redirect("failed", "authorization_failed")
    connection = await store.by_id(pending.connection_id)
    if (
        connection is None
        or connection.team_id != pending.team_id
        or connection.status != "enabled"
        or not connection.client_secret_ref
        or not await request.app.state.user_store.user_has_active_team_membership(pending.owner_user_id, pending.team_id)
    ):
        return _slack_callback_redirect("failed", "connection_unavailable")
    failure_reason = "oauth_exchange_failed"
    try:
        callback = pending.redirect_uri or await team_public_app_url(
            request, team_id=pending.team_id, path="/api/v1/personal-mcp/slack/oauth/callback"
        )
        secret = str(decrypt_llm_config(connection.client_secret_ref).get("value") or "")
        async with httpx2.AsyncClient(timeout=15) as client:
            response = await client.post(
                "https://slack.com/api/oauth.v2.user.access",
                data={"client_id": connection.client_id, "client_secret": secret, "code": code, "redirect_uri": callback},
            )
            payload = response.json()
        token = str((payload.get("authed_user") or {}).get("access_token") or "")
        if not response.is_success or not payload.get("ok") or not token:
            raise ValueError
        failure_reason = "tool_discovery_failed"
        tools = await discover(_SLACK_MCP_ENDPOINT, auth_headers("bearer", {"value": token}))
        registry = PersonalMcpStore(request.app.state.user_store.db_path)
        existing = await registry.rows(
            "SELECT id,version FROM personal_mcp_connection WHERE owner_user_id=? AND endpoint=? AND status<>'deleted'",
            (pending.owner_user_id, _SLACK_MCP_ENDPOINT),
        )
        await registry.save_connection(
            pending.owner_user_id,
            "Slack",
            _SLACK_MCP_ENDPOINT,
            "bearer",
            {"value": token},
            tools,
            connection_id=existing[0]["id"] if existing else None,
            expected_version=existing[0]["version"] if existing else None,
        )
    except Exception:
        logger.warning("personal_slack.oauth_callback_failed reason=%s", failure_reason)
        return _slack_callback_redirect("failed", failure_reason)
    return _slack_callback_redirect("connected")


def _slack_callback_redirect(result: Literal["connected", "failed"], reason: str = "") -> RedirectResponse:
    query = {"slack": result}
    if reason:
        query["slack_reason"] = reason
    from bowhead_gateway.public_url import oauth_return_path

    return RedirectResponse(
        oauth_return_path(f"/connections/?{urlencode(query)}", callback_path="/api/v1/personal-mcp/slack/oauth/callback"), status_code=303
    )


@router.get("", dependencies=[Depends(_enabled)])
async def connections(response: Response, user: User = Depends(_current_user), registry: PersonalMcpStore = Depends(_registry)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "private, no-store"
    return {"connections": await registry.connections(user.id)}


async def _save(body: ConnectionInput, user: User, registry: PersonalMcpStore, identity: str | None = None) -> dict[str, Any]:
    try:
        credentials: dict[str, str] = {}
        previous: dict[str, str] = {}
        if identity:
            old = await registry.connection(user.id, identity)
            if body.version != old["version"] or old["status"] == "deleted":
                raise PersonalMcpError("personal_mcp_version_conflict")
            if body.value is None and body.auth_type == old["auth_type"]:
                credentials = registry.unseal(user.id, identity, "credentials", old["auth_cipher"])
            previous = {
                tool["remote_name"]: tool["schema_digest"]
                for tool in await registry.rows(
                    "SELECT remote_name,schema_digest FROM personal_mcp_tool WHERE connection_id=? AND schema_digest<>''", (identity,)
                )
            }
        if body.value is not None:
            credentials = {"value": body.value.get_secret_value(), "header": body.header}
        if body.auth_type == "none":
            credentials = {}
        endpoint = normalize_endpoint(body.endpoint)
        headers = auth_headers(body.auth_type, credentials)
        tools = await discover(endpoint, headers)
        connection_id = await registry.save_connection(
            user.id, _safe(body.name), endpoint, body.auth_type, credentials, tools, connection_id=identity, expected_version=body.version
        )
        current = {tool["remote_name"]: tool["schema_digest"] for tool in tools}
        changes = {
            "added": [_safe(name) for name in sorted(current.keys() - previous.keys())],
            "removed": [_safe(name) for name in sorted(previous.keys() - current.keys())],
            "changed": [_safe(name) for name in sorted(current.keys() & previous.keys()) if current[name] != previous[name]],
        }
        return {"connection_id": connection_id, "connections": await registry.connections(user.id), "changes": changes}
    except PersonalMcpError as exc:
        raise _error(exc) from None


@router.post("", dependencies=[Depends(_enabled)])
async def create(body: ConnectionInput, user: User = Depends(_current_user), registry: PersonalMcpStore = Depends(_registry)) -> dict[str, Any]:
    return await _save(body, user, registry)


@router.put("/{connection_id}", dependencies=[Depends(_enabled)])
async def refresh(
    connection_id: str, body: ConnectionInput, user: User = Depends(_current_user), registry: PersonalMcpStore = Depends(_registry)
) -> dict[str, Any]:
    return await _save(body, user, registry, connection_id)


@router.patch("/{connection_id}", dependencies=[Depends(_enabled)])
async def configure(
    connection_id: str, body: ConfigurationInput, user: User = Depends(_current_user), registry: PersonalMcpStore = Depends(_registry)
) -> dict[str, Any]:
    try:
        await registry.connection(user.id, connection_id)
        await registry.configure(user.id, connection_id, body.version, body.status, body.selected_tool_ids)
        return {"connections": await registry.connections(user.id)}
    except PersonalMcpError as exc:
        raise _error(exc) from None


@router.get("/agents/bindings", dependencies=[Depends(_enabled)])
async def bindings(response: Response, user: User = Depends(_current_user), registry: PersonalMcpStore = Depends(_registry)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "private, no-store"
    return {"agents": await registry.agents(user.id, user.team_id)}


@router.put("/agents/{agent_id}/binding", dependencies=[Depends(_enabled)])
async def bind(
    agent_id: str, body: BindingInput, user: User = Depends(_current_user), registry: PersonalMcpStore = Depends(_registry)
) -> dict[str, Any]:
    try:
        await registry.bind(user.id, user.team_id, agent_id, body.revision_id, body.tool_ids, body.version)
        return {"agents": await registry.agents(user.id, user.team_id)}
    except PersonalMcpError as exc:
        raise _error(exc) from None


@router.get("/calls/confirmations", dependencies=[Depends(_enabled)])
async def pending(response: Response, user: User = Depends(_current_user), registry: PersonalMcpStore = Depends(_registry)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return {"confirmations": await registry.pending(user.id)}
    except PersonalMcpError as exc:
        raise _error(exc) from None


@router.get("/calls/history", dependencies=[Depends(_enabled)])
async def history(response: Response, user: User = Depends(_current_user), registry: PersonalMcpStore = Depends(_registry)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "private, no-store"
    await registry.expire(user.id)
    calls = await registry.rows(
        """SELECT i.id,i.turn_id,i.status,i.created_at,i.duration_ms,
      c.name AS connection_name,t.remote_name AS tool_name FROM personal_mcp_invocation i
      JOIN personal_mcp_connection c ON c.id=i.connection_id JOIN personal_mcp_tool t ON t.id=i.tool_id
      WHERE i.owner_user_id=? ORDER BY i.created_at DESC LIMIT 100""",
        (user.id,),
    )
    for call in calls:
        call["tool_name"] = _safe(call["tool_name"])
    return {"calls": calls}


@router.post("/calls/{invocation_id}/decision", dependencies=[Depends(_enabled)])
async def decide(
    invocation_id: str, body: DecisionInput, user: User = Depends(_current_user), registry: PersonalMcpStore = Depends(_registry)
) -> dict[str, Any]:
    await registry.expire(user.id)
    rows = await registry.rows("SELECT * FROM personal_mcp_invocation WHERE id=? AND owner_user_id=?", (invocation_id, user.id))
    if not rows:
        raise HTTPException(404, "personal_mcp_not_found")
    row = rows[0]
    if row["status"] != "pending":
        raise HTTPException(409, "personal_mcp_confirmation_unavailable")
    try:
        if body.decision in {"approve", "approve_session"}:
            manifests = await registry.rows(
                "SELECT * FROM personal_mcp_manifest WHERE turn_id=? AND execution_id=? AND owner_user_id=?",
                (row["turn_id"], row["execution_id"], user.id),
            )
            if not manifests:
                raise PersonalMcpError("personal_mcp_binding_unavailable")
            manifest = manifests[0]
            tool = next(tool for tool in json.loads(manifest["tools_json"]) if tool["id"] == row["tool_id"])
            scopes = await registry.rows(
                """SELECT s.id,s.workspace_id,w.team_id FROM turns t JOIN sessions s ON s.id=t.session_id
              JOIN workspaces w ON w.id=s.workspace_id WHERE t.id=? AND s.user_id=?""",
                (row["turn_id"], user.id),
            )
            if not scopes:
                raise PersonalMcpError("personal_mcp_binding_unavailable")
            scope = scopes[0]
            ctx = ToolExecutionContext(
                user_id=user.id,
                personal_mcp_license_check=registry.license_check,
                turn_id=row["turn_id"],
                workspace_id=scope["workspace_id"],
                session_id=scope["id"],
                team_id=scope["team_id"],
                member_uid="",
                datarc=None,
            )
            await registry.revalidate(user.id, manifest, tool, ctx)
        if body.decision == "approve_session":
            success = await registry.approve_for_session(user.id, invocation_id, scope["id"])
        else:
            success = await registry.transition(user.id, invocation_id, "pending", "approved" if body.decision == "approve" else "rejected")
        if not success:
            raise HTTPException(409, "personal_mcp_confirmation_unavailable")
        return {"id": invocation_id, "status": "approved" if body.decision in {"approve", "approve_session"} else "rejected"}
    except PersonalMcpError as exc:
        await registry.transition(user.id, invocation_id, "pending", "denied")
        raise _error(exc) from None
