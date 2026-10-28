"""Google Workspace connection: team configuration and per-member authorization.

Design: ``docs/design/Google Workspace 连接-技术设计-2026-09-24.md``.

* ``owner`` / ``team_admin`` register the customer's own OAuth client (never a
  deployment-level credential). Secrets are write-only.
* Each member authorizes for themselves; nobody can read, use or re-authorize
  another member's grant.
* The callback trusts only the single-use state row, like the personal Slack flow.
"""

from __future__ import annotations

import contextlib
import logging
import re
from typing import Any, Literal
from urllib.parse import urlencode

from bowhead_core.audit_actions import AuditAction
from bowhead_core.builtin_tool_grants import BuiltinToolGrant, BuiltinToolGrantError, BuiltinToolGrantStore
from bowhead_core.google_workspace import (
    BASE_BUNDLE,
    BUNDLES,
    CALLBACK_PATH,
    PROVIDER,
    GoogleWorkspaceError,
    authorization_url,
    bundle_granted,
    enabled_bundles,
    exchange_code,
    feature_enabled,
    normalize_config,
    pkce_pair,
    revoke,
    scopes_for,
    token_broker,
    valid_domain,
    verify_identity,
)
from bowhead_core.kb.schemas import KbScopeIn
from bowhead_core.user_store import TeamBuiltinTool, User, UserStore, _now
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from bowhead_gateway._entitlement_gate import require_feature_licensed, require_not_readonly
from bowhead_gateway.public_url import PublicEntryRequiredError, team_public_app_url
from bowhead_gateway.routes.auth import _current_user, _require_team_role_dep, _store

logger = logging.getLogger(__name__)
router = APIRouter()
_REQUIRE_TEAM_ADMIN = _require_team_role_dep("owner", "team_admin")
_ADMIN_ROLES = frozenset({"owner", "team_admin"})


def _error(status: int, code: str) -> HTTPException:
    # Code-only detail: the gateway error handler adds the localized catalog message.
    return HTTPException(status, detail={"code": code})


async def _available() -> None:
    if not feature_enabled():
        raise _error(404, "google_workspace_unavailable")


def _grants(request: Request) -> BuiltinToolGrantStore:
    return BuiltinToolGrantStore(_store(request).db_path)


async def _connection(store: UserStore, team_id: str) -> TeamBuiltinTool | None:
    return await store.get_team_builtin_tool(team_id, PROVIDER)


class ConnectionBody(BaseModel):
    name: str = Field("Google Workspace", min_length=1, max_length=60)
    client_id: str = Field(min_length=1, max_length=512)
    client_secret: str | None = Field(None, min_length=1, max_length=512)
    domain: str = Field(min_length=3, max_length=253)
    bundles: dict[str, bool] = Field(default_factory=dict)
    picker_api_key: str | None = Field(None, max_length=512)
    picker_project_number: str = Field("", max_length=32)


class StatusBody(BaseModel):
    enabled: bool


async def _redirect_uri(request: Request, team_id: str) -> str:
    try:
        return await team_public_app_url(request, team_id=team_id, path=CALLBACK_PATH)
    except PublicEntryRequiredError:
        raise _error(409, "google_public_entry_required") from None


async def _javascript_origin(request: Request, team_id: str) -> str | None:
    from bowhead_gateway.public_entry_context import current_request_public_access_context

    address = current_request_public_access_context()
    if address is not None and address.prefix_known:
        return address.origin
    try:
        url = await team_public_app_url(request, team_id=team_id, path="/")
    except PublicEntryRequiredError:
        return None
    scheme, _, rest = url.partition("://")
    return f"{scheme}://{rest.split('/', 1)[0]}"


def _bundle_view(config: dict[str, Any]) -> list[dict[str, Any]]:
    bundles = normalize_config(config)["bundles"]
    return [{"id": name, "enabled": bundles[name], "required": name == BASE_BUNDLE, "scopes": list(BUNDLES[name])} for name in BUNDLES]


async def _public(request: Request, tool: TeamBuiltinTool, auth: dict[str, Any]) -> dict[str, Any]:
    config = normalize_config(tool.config)
    return {
        "id": tool.id,
        "provider": tool.provider,
        "name": tool.display_name,
        "status": tool.status,
        "client_id": str(auth.get("client_id") or ""),
        "client_secret_configured": bool(auth.get("client_secret")),
        "picker_api_key_configured": bool(auth.get("picker_api_key")),
        "picker_project_number": config["picker_project_number"],
        "domain": config["domain"],
        "bundles": _bundle_view(config),
        "scopes": list(scopes_for(enabled_bundles(config))),
        "redirect_uri": await _redirect_uri_or_none(request, tool.team_id),
        "javascript_origin": await _javascript_origin(request, tool.team_id),
        "authorized_members": await _grants(request).count_active(tool.id),
        "created_at": tool.created_at,
        "updated_at": tool.updated_at,
    }


async def _redirect_uri_or_none(request: Request, team_id: str) -> str | None:
    try:
        return await team_public_app_url(request, team_id=team_id, path=CALLBACK_PATH)
    except PublicEntryRequiredError:
        return None


async def _audit(request: Request, user: User, action: str, tool: TeamBuiltinTool) -> None:
    with contextlib.suppress(Exception):
        await _store(request).write_admin_audit_log(
            actor_user_id=user.id,
            team_id=user.team_id,
            action=action,
            target_kind="team_builtin_tool",
            target_id=tool.id,
            target_team_id=user.team_id,
            scope="team",
            payload={"provider": tool.provider, "status": tool.status, "at": _now()},
        )


# --------------------------------------------------------------------------- team administration


@router.get("/connection", dependencies=[Depends(_available)])
async def get_connection(request: Request, user: User = Depends(_REQUIRE_TEAM_ADMIN)) -> dict[str, Any]:
    store = _store(request)
    tool = await _connection(store, user.team_id)
    if tool is None:
        return {
            "connection": None,
            "redirect_uri": await _redirect_uri_or_none(request, user.team_id),
            "javascript_origin": await _javascript_origin(request, user.team_id),
            "bundles": _bundle_view({}),
        }
    auth = await store.get_team_builtin_tool_instance_auth_decrypted(user.team_id, tool.id)
    return {"connection": await _public(request, tool, auth)}


@router.put("/connection", dependencies=[Depends(_available), Depends(require_not_readonly)])
async def save_connection(body: ConnectionBody, request: Request, user: User = Depends(_REQUIRE_TEAM_ADMIN)) -> dict[str, Any]:
    store = _store(request)
    domain = body.domain.strip().lower()
    if not valid_domain(domain):
        raise _error(422, "google_domain_invalid")
    project_number = body.picker_project_number.strip()
    if project_number and not project_number.isdigit():
        raise _error(422, "google_picker_project_number_invalid")
    unknown = set(body.bundles) - set(BUNDLES)
    if unknown:
        raise _error(422, "google_bundle_unknown")
    config = normalize_config({"domain": domain, "bundles": body.bundles, "picker_project_number": project_number})
    name = " ".join(body.name.split()) or "Google Workspace"
    current = await _connection(store, user.team_id)
    previous = await store.get_team_builtin_tool_instance_auth_decrypted(user.team_id, current.id) if current else {}
    client_secret = (body.client_secret or "").strip() or str(previous.get("client_secret") or "")
    if not client_secret:
        raise _error(422, "google_client_secret_required")
    picker_api_key = str(previous.get("picker_api_key") or "") if body.picker_api_key is None else body.picker_api_key.strip()
    auth = {"client_id": body.client_id.strip(), "client_secret": client_secret, "picker_api_key": picker_api_key}
    client_changed = bool(current) and (
        auth["client_id"] != previous.get("client_id")
        or client_secret != previous.get("client_secret")
        or config["domain"] != normalize_config(current.config)["domain"]
    )
    try:
        if current is None:
            tool = await store.create_team_builtin_tool_instance(
                team_id=user.team_id,
                provider=PROVIDER,
                display_name=name,
                endpoint="",
                auth=auth,
                key_hint=None,
                status="disabled",
                created_by=user.id,
                config=config,
            )
            action = AuditAction.TEAM_TOOL_SOURCE_CREATED
        else:
            if client_changed:
                # Grants belong to the old client; a new client cannot refresh them.
                grants = _grants(request)
                for token in await grants.refresh_tokens_for_tool(current.id):
                    await revoke(token)
                await grants.delete_for_tool(current.id)
            updated = await store.update_team_builtin_tool_instance(
                team_id=user.team_id,
                tool_id=current.id,
                display_name=name,
                auth=auth,
                config=config,
                status="disabled" if client_changed else None,
            )
            if updated is None:
                raise _error(404, "google_connection_not_found")
            tool = updated
            action = AuditAction.TEAM_TOOL_SOURCE_UPDATED
    except RuntimeError as exc:
        raise _error(503, "builtin_tool_encryption_required") from exc
    await _audit(request, user, action, tool)
    return {"connection": await _public(request, tool, auth)}


@router.patch("/connection/status", dependencies=[Depends(_available), Depends(require_not_readonly)])
async def update_status(body: StatusBody, request: Request, user: User = Depends(_REQUIRE_TEAM_ADMIN)) -> dict[str, Any]:
    store = _store(request)
    tool = await _connection(store, user.team_id)
    if tool is None:
        raise _error(404, "google_connection_not_found")
    if body.enabled:
        # Enabling proves the client, redirect URI and consent screen work: the
        # administrator enabling it must hold an active grant of their own.
        grant = await _grants(request).get(tool.id, user.id)
        if grant is None or grant.status != "active":
            raise _error(409, "google_admin_authorization_required")
    updated = await store.update_team_builtin_tool_instance(team_id=user.team_id, tool_id=tool.id, status="enabled" if body.enabled else "disabled")
    if updated is None:
        raise _error(404, "google_connection_not_found")
    await _audit(request, user, AuditAction.TEAM_TOOL_SOURCE_UPDATED, updated)
    auth = await store.get_team_builtin_tool_instance_auth_decrypted(user.team_id, updated.id)
    return {"connection": await _public(request, updated, auth)}


@router.delete("/connection", status_code=204, dependencies=[Depends(_available), Depends(require_not_readonly)])
async def delete_connection(request: Request, user: User = Depends(_REQUIRE_TEAM_ADMIN)) -> None:
    store = _store(request)
    tool = await _connection(store, user.team_id)
    if tool is None:
        raise _error(404, "google_connection_not_found")
    grants = _grants(request)
    for token in await grants.refresh_tokens_for_tool(tool.id):
        await revoke(token)
    await grants.delete_for_tool(tool.id)
    if not await store.delete_team_builtin_tool_instance(user.team_id, tool.id):
        raise _error(404, "google_connection_not_found")
    await _audit(request, user, AuditAction.TEAM_TOOL_SOURCE_DELETED, tool)


# --------------------------------------------------------------------------- member authorization


def _grant_view(tool: TeamBuiltinTool, grant: BuiltinToolGrant | None) -> dict[str, Any]:
    wanted = enabled_bundles(tool.config)
    granted = grant.granted_scopes if grant else ()
    return {
        "connection_id": tool.id,
        "name": tool.display_name,
        "connection_status": tool.status,
        "status": "not_connected" if grant is None else grant.status,
        "email": grant.provider_email if grant else None,
        "bundles": [{"id": name, "enabled": name in wanted, "granted": bundle_granted(name, granted) if grant else False} for name in BUNDLES],
        "needs_update": bool(grant) and any(not bundle_granted(name, granted) for name in wanted),
        "connected_at": grant.created_at if grant else None,
        "last_used_at": grant.last_used_at if grant else None,
    }


async def _is_admin(store: UserStore, user: User) -> bool:
    return (await store.get_team_role(user.id, user.team_id)) in _ADMIN_ROLES


async def _usable_connection(store: UserStore, user: User) -> TeamBuiltinTool:
    """Members use an enabled connection; administrators may also authorize a disabled one (setup)."""
    tool = await _connection(store, user.team_id)
    if tool is None or (tool.status != "enabled" and not await _is_admin(store, user)):
        raise _error(404, "google_connection_not_found")
    return tool


@router.get("/me", dependencies=[Depends(_available)])
async def my_authorization(request: Request, user: User = Depends(_current_user)) -> dict[str, Any]:
    store = _store(request)
    tool = await _connection(store, user.team_id)
    if tool is None or (tool.status != "enabled" and not await _is_admin(store, user)):
        return {"available": False, "authorization": None}
    return {"available": True, "authorization": _grant_view(tool, await _grants(request).get(tool.id, user.id))}


@router.post("/connect", dependencies=[Depends(_available)])
async def connect(request: Request, user: User = Depends(_current_user)) -> dict[str, str]:
    store = _store(request)
    tool = await _usable_connection(store, user)
    auth = await store.get_team_builtin_tool_instance_auth_decrypted(user.team_id, tool.id)
    config = normalize_config(tool.config)
    if not auth.get("client_id") or not auth.get("client_secret") or not config["domain"]:
        raise _error(409, "google_connection_incomplete")
    if not user.email:
        raise _error(409, "google_email_required")
    redirect_uri = await _redirect_uri(request, user.team_id)
    grants = _grants(request)
    existing = await grants.get(tool.id, user.id)
    scopes = scopes_for(enabled_bundles(config))
    verifier, challenge = pkce_pair()
    state = await grants.create_state(
        tool_id=tool.id, team_id=user.team_id, user_id=user.id, requested_scopes=scopes, redirect_uri=redirect_uri, code_verifier=verifier
    )
    url = authorization_url(
        client_id=str(auth["client_id"]),
        redirect_uri=redirect_uri,
        state=state,
        scopes=scopes,
        login_hint=user.email,
        domain=config["domain"],
        code_challenge=challenge,
        # A refresh token is only issued with explicit consent: first grant and re-auth.
        force_consent=existing is None or existing.status != "active",
    )
    return {"authorize_url": url}


def _callback_redirect(result: Literal["connected", "failed"], reason: str = "", *, admin_setup: bool = False) -> RedirectResponse:
    query = {"google": result}
    if reason:
        query["google_reason"] = reason
    path = "/team/settings/tools/" if admin_setup else "/connections/"
    from bowhead_gateway.public_url import oauth_return_path

    return RedirectResponse(oauth_return_path(f"{path}?{urlencode(query)}", callback_path=CALLBACK_PATH), status_code=303)


@router.get("/oauth/callback")
async def oauth_callback(request: Request, code: str = "", state: str = "", error: str = "") -> RedirectResponse:
    grants = _grants(request)
    pending = await grants.consume_state(state)
    if pending is not None:
        from bowhead_gateway.public_url import restore_oauth_public_access_context

        restore_oauth_public_access_context(pending.redirect_uri, callback_path=CALLBACK_PATH)
    if not feature_enabled():
        return _callback_redirect("failed", "connection_unavailable")
    if pending is None:
        return _callback_redirect("failed", "session_expired")
    store = _store(request)
    tool = await store.get_team_builtin_tool_instance(pending.team_id, pending.tool_id)
    admin_setup = tool is not None and tool.status != "enabled"
    if error:
        return _callback_redirect("failed", "authorization_denied" if error == "access_denied" else "authorization_failed", admin_setup=admin_setup)
    if not code:
        return _callback_redirect("failed", "authorization_failed", admin_setup=admin_setup)
    user = await store.get_user(pending.user_id)
    role = await store.get_team_role(pending.user_id, pending.team_id)
    if (
        tool is None
        or tool.provider != PROVIDER
        or user is None
        or not user.email
        or not await store.user_has_active_team_membership(pending.user_id, pending.team_id)
        or (tool.status != "enabled" and role not in _ADMIN_ROLES)
    ):
        return _callback_redirect("failed", "connection_unavailable", admin_setup=admin_setup)
    auth = await store.get_team_builtin_tool_instance_auth_decrypted(pending.team_id, tool.id)
    config = normalize_config(tool.config)
    client_id = str(auth.get("client_id") or "")
    try:
        response = await exchange_code(
            client_id=client_id,
            client_secret=str(auth.get("client_secret") or ""),
            code=code,
            redirect_uri=pending.redirect_uri,
            code_verifier=pending.code_verifier,
        )
        identity = verify_identity(response.id_token, client_id=client_id, domain=config["domain"], expected_email=user.email)
        if not bundle_granted(BASE_BUNDLE, response.scopes):
            await revoke(response.refresh_token or response.access_token)
            return _callback_redirect("failed", "base_scope_denied", admin_setup=admin_setup)
        previous_grant = await grants.get(tool.id, pending.user_id)
        saved, superseded = await grants.save(
            tool_id=tool.id,
            team_id=pending.team_id,
            user_id=pending.user_id,
            provider_subject=identity.subject,
            provider_email=identity.email,
            granted_scopes=response.scopes,
            refresh_token=response.refresh_token,
        )
    except GoogleWorkspaceError as exc:
        logger.warning("google_workspace.oauth_callback_failed reason=%s", exc.code)
        reason = {
            "google_domain_mismatch": "domain_mismatch",
            "google_email_mismatch": "email_mismatch",
            "google_unreachable": "google_unreachable",
        }.get(exc.code, "token_exchange_failed")
        return _callback_redirect("failed", reason, admin_setup=admin_setup)
    except BuiltinToolGrantError as exc:
        logger.warning("google_workspace.oauth_grant_failed reason=%s", exc.code)
        return _callback_redirect("failed", "refresh_token_missing", admin_setup=admin_setup)
    if superseded and previous_grant is not None and previous_grant.provider_subject != identity.subject:
        await revoke(superseded)
    token_broker(store.db_path).forget(saved.id)
    return _callback_redirect("connected", admin_setup=admin_setup)


@router.delete("/me", status_code=204, dependencies=[Depends(_available)])
async def disconnect(request: Request, user: User = Depends(_current_user)) -> None:
    tool = await _connection(_store(request), user.team_id)
    if tool is None:
        return
    grants = _grants(request)
    grant = await grants.get(tool.id, user.id)
    if grant is not None:
        token_broker(grants.db_path).forget(grant.id)
    token = await grants.delete(tool.id, user.id)
    if token:
        await revoke(token)


@router.get("/picker-config", dependencies=[Depends(_available)])
async def picker_config(request: Request, user: User = Depends(_current_user)) -> dict[str, str]:
    store = _store(request)
    tool = await _usable_connection(store, user)
    grant = await _grants(request).get(tool.id, user.id)
    if grant is None or grant.status != "active":
        raise _error(409, "google_not_connected")
    auth = await store.get_team_builtin_tool_instance_auth_decrypted(user.team_id, tool.id)
    config = normalize_config(tool.config)
    if not auth.get("picker_api_key") or not config["picker_project_number"]:
        raise _error(409, "google_picker_unavailable")
    return {
        "client_id": str(auth["client_id"]),
        "api_key": str(auth["picker_api_key"]),
        "project_number": config["picker_project_number"],
        # GIS account hints so the browser token lands on the member's connected account.
        "login_hint": grant.provider_email,
        "hosted_domain": config["domain"],
    }


# --------------------------------------------------------------------------- Drive → knowledge base


# Google Workspace export limit for Docs/Sheets/Slides (Drive files.export).
_EXPORT_MAX_BYTES = 10 * 1024 * 1024
_DRIVE_ID = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
# Google-native type → (export MIME, file extension) the knowledge base can parse.
_EXPORTS = {
    "application/vnd.google-apps.document": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
}


class KbImportBody(BaseModel):
    file_ids: list[str] = Field(min_length=1, max_length=20)
    scope: KbScopeIn
    scope_id: str = Field(max_length=200)
    use_llm: bool = False


class _DriveFileError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _drive_error(response: Any) -> _DriveFileError:
    reason = ""
    try:
        errors = (response.json().get("error") or {}).get("errors") or []
        reason = str(errors[0].get("reason") or "") if errors else ""
    except (ValueError, AttributeError, IndexError):
        reason = ""
    if reason == "exportSizeLimitExceeded":
        return _DriveFileError("google_export_too_large")
    if response.status_code == 401:
        return _DriveFileError("google_reauth_required")
    if response.status_code == 404:
        return _DriveFileError("google_file_not_found")
    if response.status_code == 403 and reason not in {"rateLimitExceeded", "userRateLimitExceeded"}:
        return _DriveFileError("google_file_not_granted")
    return _DriveFileError("google_unreachable")


async def _fetch_drive_file(token: str, file_id: str, max_bytes: int) -> tuple[str, bytes, str]:
    """(filename, bytes, mime) of one Picker-selected Drive file, Google formats exported to Office."""
    import httpx2
    from bowhead_core.google_workspace import http_client

    base = f"https://www.googleapis.com/drive/v3/files/{file_id}"
    headers = {"Authorization": f"Bearer {token}"}

    async def download(client: Any, url: str, params: dict[str, str], limit: int, too_large: str) -> bytes:
        # Stream with a hard cap: metadata size can be missing or stale, and the body must never
        # be buffered past the limit.
        async with client.stream("GET", url, params=params, headers=headers) as response:
            if not response.is_success:
                await response.aread()
                raise _drive_error(response)
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > limit:
                    raise _DriveFileError(too_large)
                chunks.append(chunk)
            return b"".join(chunks)

    try:
        async with http_client() as client:
            meta_response = await client.get(base, params={"fields": "name,mimeType,size", "supportsAllDrives": "true"}, headers=headers)
            if not meta_response.is_success:
                raise _drive_error(meta_response)
            meta = meta_response.json()
            if not isinstance(meta, dict):
                raise _DriveFileError("google_unreachable")
            name = str(meta.get("name") or file_id)
            mime = str(meta.get("mimeType") or "")
            if mime in _EXPORTS:
                export_mime, extension = _EXPORTS[mime]
                content = await download(client, f"{base}/export", {"mimeType": export_mime}, _EXPORT_MAX_BYTES, "google_export_too_large")
                filename = name if name.lower().endswith(extension) else f"{name}{extension}"
                return filename, content, export_mime
            if mime.startswith("application/vnd.google-apps."):
                raise _DriveFileError("kb_file_type_unsupported")  # forms, drawings, folders, shortcuts
            if int(meta.get("size") or 0) > max_bytes:
                raise _DriveFileError("kb_file_too_large")
            content = await download(client, base, {"alt": "media", "supportsAllDrives": "true"}, max_bytes, "kb_file_too_large")
            return name, content, mime
    except (httpx2.HTTPError, ValueError):
        # ValueError: a non-JSON metadata answer; never turn one bad file into a 500 for the batch.
        raise _DriveFileError("google_unreachable") from None


def _http_error_code(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, dict):
        return str(detail.get("code") or "kb_import_failed")
    return str(detail or "kb_import_failed")


@router.post(
    "/kb-import",
    dependencies=[Depends(_available), Depends(require_not_readonly), Depends(require_feature_licensed("kb"))],
)
async def kb_import(body: KbImportBody, request: Request, background_tasks: BackgroundTasks, user: User = Depends(_current_user)) -> dict[str, Any]:
    """Import Picker-selected Drive files into the knowledge base as the caller.

    Uses the caller's own Google grant and the same authorization and ingestion path as
    the knowledge-base upload. The Google file ID is not stored, so there is no re-import.
    """
    from bowhead_gateway.routes.kb import _SINGLE_MAX_SIZE, authorize_kb_scope_write, ingest_kb_file_bytes

    ctx = await authorize_kb_scope_write(request, user, scope=body.scope, scope_id=body.scope_id)
    store = _store(request)
    tool = await _connection(store, user.team_id)
    if tool is None or tool.status != "enabled":
        raise _error(404, "google_connection_not_found")
    file_ids = list(dict.fromkeys(item.strip() for item in body.file_ids))
    if not all(_DRIVE_ID.match(item) for item in file_ids):
        raise _error(422, "google_file_reference_invalid")
    auth = await store.get_team_builtin_tool_instance_auth_decrypted(user.team_id, tool.id)
    try:
        token, grant = await token_broker(store.db_path).access_token(
            tool=tool, auth=auth, user_id=user.id, required_bundles=frozenset({BASE_BUNDLE})
        )
    except GoogleWorkspaceError as exc:
        raise _error(409, exc.code) from None

    results: list[dict[str, Any]] = []
    for file_id in file_ids:
        try:
            filename, raw, mime = await _fetch_drive_file(token, file_id, _SINGLE_MAX_SIZE)
            dto = await ingest_kb_file_bytes(
                request,
                background_tasks,
                user,
                ctx,
                raw=raw,
                filename=filename,
                client_mime=mime,
                scope=body.scope,
                scope_id=body.scope_id,
                use_llm=body.use_llm,
                route="kb_google_import",
            )
            results.append({"file_id": file_id, "name": dto.filename, "ok": True, "kb_file_id": dto.file_id})
        except _DriveFileError as exc:
            if exc.code == "google_reauth_required":
                token_broker(store.db_path).forget(grant.id)
            results.append({"file_id": file_id, "name": None, "ok": False, "error": exc.code})
        except HTTPException as exc:
            results.append({"file_id": file_id, "name": None, "ok": False, "error": _http_error_code(exc)})
    return {"results": results}
