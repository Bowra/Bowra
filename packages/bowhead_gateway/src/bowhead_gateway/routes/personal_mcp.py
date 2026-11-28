"""Personal MCP APIs have no team-admin bypass and never return credentials."""

import json
import logging
import os
import secrets
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

import httpx2
from bowhead_core.llm_config_crypto import decrypt_llm_config
from bowhead_core.oauth_token_broker import OAuthGrantRejected, OAuthRefreshUnavailable
from bowhead_core.personal_mcp import (
    PersonalMcpError,
    PersonalMcpStore,
    PersonalOAuthAuthStore,
    _safe,
    auth_headers,
    discover,
    normalize_endpoint,
    personal_oauth_headers,
    prime_personal_oauth,
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


# Sign-in errors carry a catalog code (config/i18n/errors) so the page shows actionable copy.
_CODED_STATUS = {
    "personal_mcp_sign_in_ended": 409,
    "personal_mcp_sign_in_renewal_unavailable": 503,
    "personal_mcp_oauth_endpoint_locked": 409,
    "personal_mcp_oauth_sign_in_required": 400,
}


def _error(exc: PersonalMcpError) -> HTTPException:
    code = str(exc)
    if code in _CODED_STATUS:
        return HTTPException(_CODED_STATUS[code], detail={"code": code})
    return HTTPException(404 if code == "personal_mcp_not_found" else 409 if code == "personal_mcp_version_conflict" else 400, code)


class ConnectionInput(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    endpoint: str = Field(min_length=1, max_length=2048)
    # "oauth" only renames or refreshes a signed-in connection; signing in uses /oauth/authorize.
    auth_type: Literal["none", "bearer", "api_key", "oauth"] = "none"
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


_OAUTH_CALLBACK_PATH = "/api/v1/personal-mcp/oauth/callback"


class OAuthAuthorizeInput(BaseModel):
    """A new connection (name + endpoint) or an existing signed-in connection (id + version)."""

    name: str | None = Field(default=None, min_length=1, max_length=40)
    endpoint: str | None = Field(default=None, min_length=1, max_length=2048)
    connection_id: str | None = Field(default=None, max_length=64)
    version: int | None = Field(default=None, ge=1)


@router.post("/oauth/authorize", dependencies=[Depends(_enabled)])
async def authorize_oauth(
    body: OAuthAuthorizeInput,
    request: Request,
    user: User = Depends(_current_user),
    registry: PersonalMcpStore = Depends(_registry),
    users: UserStore = Depends(_store),
) -> dict[str, str]:
    """Start signing in to an MCP service with the member's own account.

    Registers Bowhead with the service when needed, pins the full callback address in a single-use
    pending row (team public-entry rules) and returns the service's authorization URL. Nothing is
    sent to the service before the member opens that URL."""
    from bowhead_core.mcp_oauth import FernetTokenStorage, McpOAuthRegistrationRefused, begin_mcp_oauth
    from mcp.client.auth.oauth2 import PKCEParameters

    from bowhead_gateway.public_url import public_app_url_for_base, team_public_access_context

    try:
        public_context = await team_public_access_context(request, team_id=user.team_id)
    except PublicEntryRequiredError:
        raise HTTPException(409, detail={"code": "personal_mcp_public_entry_required"}) from None
    redirect_uri = public_app_url_for_base(public_context.base_url, _OAUTH_CALLBACK_PATH)
    created = False
    try:
        if body.connection_id:
            connection = await registry.connection(user.id, body.connection_id)
            if connection["auth_type"] != "oauth" or connection["status"] == "deleted":
                raise PersonalMcpError("personal_mcp_not_found")
            if body.version is not None and body.version != connection["version"]:
                raise PersonalMcpError("personal_mcp_version_conflict")
            identity, endpoint = str(connection["id"]), str(connection["endpoint"])
        else:
            if not body.name or not body.endpoint:
                raise HTTPException(422, "name and endpoint are required for a new connection")
            endpoint = normalize_endpoint(body.endpoint)
            identity = await registry.create_oauth_connection(user.id, _safe(body.name), endpoint)
            created = True
    except PersonalMcpError as exc:
        raise _error(exc) from None
    host = urlsplit(endpoint).hostname or ""
    pkce = PKCEParameters.generate()
    state = secrets.token_urlsafe(32)
    try:
        begun = await begin_mcp_oauth(
            endpoint,
            FernetTokenStorage(PersonalOAuthAuthStore(registry), user.id, identity),
            redirect_uri,
            state=state,
            code_challenge=pkce.code_challenge,
            # Each sign-in registers afresh: revoking Bowhead at the service may have deleted the old client.
            reuse_client=False,
        )
        await users.create_mcp_oauth_pending(
            state=state,
            team_id=user.team_id,
            source_id=identity,
            code_verifier=pkce.code_verifier,
            redirect_uri=redirect_uri,
            token_endpoint=begun.token_endpoint,
            client_id=begun.client_id,
            resource=begun.resource,
            created_by=user.id,
            public_entry_id=public_context.entry_id,
        )
    except McpOAuthRegistrationRefused as exc:
        await _discard_unstarted(registry, user.id, identity, created)
        if exc.code == "invalid_redirect_uri":
            raise HTTPException(422, detail={"code": "personal_mcp_callback_refused", "provider_host": host, "callback": redirect_uri}) from None
        raise HTTPException(422, detail={"code": "personal_mcp_admin_app_required", "provider_host": host}) from None
    except Exception as exc:
        await _discard_unstarted(registry, user.id, identity, created)
        logger.warning("personal_mcp.oauth_begin_failed error=%s", type(exc).__name__)
        raise HTTPException(502, detail={"code": "personal_mcp_sign_in_start_failed", "provider_host": host}) from None
    return {"authorize_url": begun.authorization_url, "connection_id": identity}


async def _discard_unstarted(registry: PersonalMcpStore, owner: str, identity: str, created: bool) -> None:
    """A connection created for a sign-in that never reached the service is removed again."""
    if not created:
        return
    try:
        await registry.configure(owner, identity, 1, "deleted", [])
    except Exception as exc:  # noqa: BLE001 - the original failure is what the member needs to see
        logger.warning("personal_mcp.oauth_discard_failed error=%s", type(exc).__name__)


@router.get("/oauth/callback")
async def oauth_callback(request: Request, code: str = "", state: str = "", error: str = "") -> RedirectResponse:
    """Return from the service's sign-in page: a top-level navigation authenticated only by the
    single-use state, which binds the owner, Team and connection. Always redirects to My Connections."""
    users: UserStore = request.app.state.user_store
    try:
        pending = await users.take_mcp_oauth_pending(state, personal=True) if state else None
    except Exception:  # noqa: BLE001 - a database error fails closed to the notice, never 500
        pending = None
    if pending is None:
        return _oauth_redirect("failed", "session_expired")
    restore_oauth_public_access_context(str(pending["redirect_uri"]), callback_path=_OAUTH_CALLBACK_PATH)
    owner, identity, team_id = str(pending.get("created_by") or ""), str(pending["source_id"]), str(pending["team_id"])
    license_check = feature_license_checker(request.app, "mcp")
    if not owner or not await license_check(team_id):
        return _oauth_redirect("failed", "unavailable", identity)
    if error or not code:
        return _oauth_redirect("failed", "authorization_denied", identity)
    registry = PersonalMcpStore(users.db_path, license_check=license_check)
    try:
        connection = await registry.connection(owner, identity)
    except PersonalMcpError:
        return _oauth_redirect("failed", "connection_unavailable", identity)
    if connection["auth_type"] != "oauth" or connection["status"] == "deleted":
        return _oauth_redirect("failed", "connection_unavailable", identity)
    from bowhead_core.mcp_oauth import FernetTokenStorage, exchange_mcp_oauth_code

    try:
        token = await exchange_mcp_oauth_code(
            FernetTokenStorage(PersonalOAuthAuthStore(registry), owner, identity),
            code=code,
            code_verifier=str(pending["code_verifier"]),
            redirect_uri=str(pending["redirect_uri"]),
            token_endpoint=str(pending["token_endpoint"]),
            client_id=str(pending["client_id"]),
            resource=pending.get("resource"),
        )
    except Exception as exc:  # noqa: BLE001 - provider text never reaches the page
        logger.warning("personal_mcp.oauth_exchange_failed error=%s", type(exc).__name__)
        return _oauth_redirect("failed", "oauth_exchange_failed", identity)
    prime_personal_oauth(registry, owner, identity, token.access_token, token.expires_in)
    again = connection["status"] != "authorizing"
    try:
        tools = await discover(str(connection["endpoint"]), {"Authorization": f"Bearer {token.access_token}"})
    except PersonalMcpError:
        tools = None
    try:
        latest = await registry.connection(owner, identity)
        if tools is None:
            # Signed in, but the tool list could not be read: keep the tools and selections the
            # connection already has, and let the member refresh the list later.
            kept = [row["id"] for row in await registry.rows("SELECT id FROM personal_mcp_tool WHERE connection_id=? AND selected=1", (identity,))]
            await registry.configure(owner, identity, int(latest["version"]), "ready", kept)
            return _oauth_redirect("failed", "tool_discovery_failed", identity)
        # Signing in again keeps the selection and Agent bindings of unchanged tools (decision Q1).
        await registry.save_connection(
            owner,
            str(latest["name"]),
            str(latest["endpoint"]),
            "oauth",
            None,
            tools,
            connection_id=identity,
            expected_version=int(latest["version"]),
            keep_unchanged=again,
        )
    except Exception as exc:  # noqa: BLE001 - the sign-in is stored; the page explains how to continue
        logger.warning("personal_mcp.oauth_complete_failed error=%s", type(exc).__name__)
        return _oauth_redirect("failed", "connection_unavailable", identity)
    return _oauth_redirect("connected", "signed_in_again" if again else "signed_in", identity)


def _oauth_redirect(result: Literal["connected", "failed"], reason: str, connection_id: str = "") -> RedirectResponse:
    query = {"mcp_oauth": result, "mcp_oauth_reason": reason}
    if connection_id:
        query["connection"] = connection_id
    from bowhead_gateway.public_url import oauth_return_path

    return RedirectResponse(oauth_return_path(f"/connections/?{urlencode(query)}", callback_path=_OAUTH_CALLBACK_PATH), status_code=303)


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
        credentials: dict[str, str] | None = {}
        previous: dict[str, str] = {}
        old: dict[str, Any] | None = None
        if identity:
            old = await registry.connection(user.id, identity)
            if body.version != old["version"] or old["status"] == "deleted":
                raise PersonalMcpError("personal_mcp_version_conflict")
            if body.value is None and body.auth_type == old["auth_type"] and old["auth_type"] != "oauth":
                credentials = registry.unseal(user.id, identity, "credentials", old["auth_cipher"])
            previous = {
                tool["remote_name"]: tool["schema_digest"]
                for tool in await registry.rows(
                    "SELECT remote_name,schema_digest FROM personal_mcp_tool WHERE connection_id=? AND schema_digest<>''", (identity,)
                )
            }
        endpoint = normalize_endpoint(body.endpoint)
        if body.auth_type == "oauth":
            # A signed-in connection is renamed or rediscovered here; its sign-in stays as it is.
            if old is None or old["auth_type"] != "oauth":
                raise PersonalMcpError("personal_mcp_oauth_sign_in_required")
            if endpoint != old["endpoint"]:
                raise PersonalMcpError("personal_mcp_oauth_endpoint_locked")
            try:
                headers = await personal_oauth_headers(registry, user.id, old["id"], endpoint)
            except OAuthGrantRejected:
                raise PersonalMcpError("personal_mcp_sign_in_ended") from None
            except OAuthRefreshUnavailable:
                raise PersonalMcpError("personal_mcp_sign_in_renewal_unavailable") from None
            credentials = None
        else:
            if body.value is not None:
                credentials = {"value": body.value.get_secret_value(), "header": body.header}
            if body.auth_type == "none":
                credentials = {}
            headers = auth_headers(body.auth_type, credentials or {})
        try:
            tools = await discover(endpoint, headers)
        except PersonalMcpError as exc:
            if str(exc) == "personal_mcp_discovery_failed" and body.auth_type == "none":
                await _offer_sign_in(endpoint)
            raise
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


async def _offer_sign_in(endpoint: str) -> None:
    """Without a credential, an unreachable tool list may mean the service wants a sign-in.

    Only probed when no credential was supplied: a refused token or API key keeps its own error
    (116 finding for team sources: a wrong token was reported as "needs OAuth")."""
    from bowhead_core.mcp_oauth import probe_mcp_oauth_details

    probe = await probe_mcp_oauth_details(endpoint)
    if not probe.needs_oauth:
        return
    host = probe.provider_host or ""
    if probe.registration_supported is False:
        raise HTTPException(422, detail={"code": "personal_mcp_admin_app_required", "provider_host": host})
    raise HTTPException(409, detail={"code": "personal_mcp_needs_oauth", "provider_host": host})


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


async def decide_call(
    invocation_id: str, decision: Literal["approve", "approve_session", "reject"], *, user: User, registry: PersonalMcpStore
) -> dict[str, Any]:
    """Decide one of the owner's pending calls; ``/api/v1/tool-confirmations`` is the only caller.

    Approval revalidates the binding and the tool against the Turn's manifest first; a failed
    revalidation denies the call.
    """
    await registry.expire(user.id)
    rows = await registry.rows("SELECT * FROM personal_mcp_invocation WHERE id=? AND owner_user_id=?", (invocation_id, user.id))
    if not rows:
        raise HTTPException(404, "personal_mcp_not_found")
    row = rows[0]
    if row["status"] != "pending":
        raise HTTPException(409, "personal_mcp_confirmation_unavailable")
    try:
        if decision in {"approve", "approve_session"}:
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
        if decision == "approve_session":
            success = await registry.approve_for_session(user.id, invocation_id, scope["id"])
        else:
            success = await registry.transition(user.id, invocation_id, "pending", "approved" if decision == "approve" else "rejected")
        if not success:
            raise HTTPException(409, "personal_mcp_confirmation_unavailable")
        return {"id": invocation_id, "status": "approved" if decision in {"approve", "approve_session"} else "rejected"}
    except PersonalMcpError as exc:
        await registry.transition(user.id, invocation_id, "pending", "denied")
        raise _error(exc) from None
