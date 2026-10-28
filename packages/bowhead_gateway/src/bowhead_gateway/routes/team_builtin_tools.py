"""Team administration API for image-bundled third-party tools.

The provider registry is intentionally closed.  It is distinct from team MCP
sources: a provider's HTTP contract lives in the image and administrators only
provide the team-scoped credentials required to use it.
"""

from __future__ import annotations

import contextlib
import sqlite3
from typing import Any
from urllib.parse import urlsplit

from bowhead_core.audit_actions import AuditAction
from bowhead_core.builtin_datarc_scope import (
    datarc_scope_fingerprint,
    datarc_scope_from_listed_workspaces,
    normalize_datarc_scope,
    supported_datarc_operations,
)
from bowhead_core.builtin_datarc_tool import datarc_source_label
from bowhead_core.builtin_powerbi_tool import OPERATIONS as POWERBI_OPERATIONS
from bowhead_core.ssrf_guard import SsrfBlocked, validate_url
from bowhead_core.user_store import TeamBuiltinTool, User, _now
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.routes.auth import _require_team_role_dep, _store

router = APIRouter()
_REQUIRE_TEAM_ADMIN = _require_team_role_dep("owner", "team_admin")
_PROVIDER = "tavily"
_DATARC_PROVIDER = "datarc"
_POWERBI_PROVIDER = "powerbi"


class ConfigureBody(BaseModel):
    api_key: str = Field(min_length=1, max_length=512)
    enabled: bool = True


class StatusBody(BaseModel):
    enabled: bool


class ConfigureDatarcBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    endpoint: str = Field(min_length=1, max_length=2048)
    connection_key: str = Field(min_length=1, max_length=4096)
    enabled: bool = True


class PatchDatarcBody(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=60)
    endpoint: str | None = Field(None, min_length=1, max_length=2048)
    connection_key: str | None = Field(None, min_length=1, max_length=4096)


class ValidateDatarcBody(BaseModel):
    endpoint: str = Field(min_length=1, max_length=2048)
    connection_key: str = Field(min_length=1, max_length=4096)


class ConfigurePowerBiBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    tenant_id: str = Field(min_length=1, max_length=128)
    client_id: str = Field(min_length=1, max_length=128)
    client_secret: str = Field(min_length=1, max_length=4096)
    workspace_id: str = Field(default="", max_length=128)
    dataset_id: str = Field(default="", max_length=128)
    enabled: bool = True


class PatchPowerBiBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    tenant_id: str | None = Field(default=None, min_length=1, max_length=128)
    client_id: str | None = Field(default=None, min_length=1, max_length=128)
    client_secret: str | None = Field(default=None, min_length=1, max_length=4096)
    workspace_id: str | None = Field(default=None, max_length=128)
    dataset_id: str | None = Field(default=None, max_length=128)


class PowerBiTemplateBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    dax_template: str = Field(min_length=1, max_length=20_000)


def _public(tool: TeamBuiltinTool) -> dict[str, Any]:
    return {
        "id": tool.id,
        "provider": tool.provider,
        "name": tool.display_name,
        "display_name": (
            datarc_source_label(tool.display_name)
            if tool.provider == _DATARC_PROVIDER
            else f"Power BI - {tool.display_name}"
            if tool.provider == _POWERBI_PROVIDER
            else tool.display_name
        ),
        "endpoint": tool.endpoint,
        "key_hint": tool.key_hint,
        "status": tool.status,
        "configured": True,
        "created_at": tool.created_at,
        "updated_at": tool.updated_at,
        "scope_state": tool.scope_state,
        "scope": tool.scope,
        "scope_checked_at": tool.scope_checked_at,
        "supported_tools": (
            sorted(supported_datarc_operations(tool))
            if tool.provider == _DATARC_PROVIDER
            else list(POWERBI_OPERATIONS)
            if tool.provider == _POWERBI_PROVIDER and tool.status == "enabled"
            else []
        ),
    }


def _normalize_datarc_endpoint(endpoint: str) -> str:
    normalized = endpoint.strip().rstrip("/")
    parts = urlsplit(normalized)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise HTTPException(422, "datarc_endpoint_rejected")
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise HTTPException(422, "datarc_endpoint_must_be_service_root")
    try:
        validate_url(normalized)
    except (SsrfBlocked, ValueError) as exc:
        raise HTTPException(422, "datarc_endpoint_rejected") from exc
    return normalized


def _normalize_datarc_name(name: str) -> str:
    normalized = " ".join(name.split())
    if not normalized:
        raise HTTPException(422, "datarc_name_required")
    return normalized


def _coded_error(status_code: int, code: str) -> HTTPException:
    """Code-only detail; the gateway error handler adds the localized catalog message."""
    return HTTPException(status_code, detail={"code": code})


def _datarc_verification_error(status_code: int, code: str) -> HTTPException:
    # Code-only detail: the gateway error handler adds the localized catalog message.
    return HTTPException(status_code, detail={"code": code})


def _is_missing_route(exc: Exception) -> bool:
    """HTTP 404 from the service router, not a Datarc business "not found" envelope."""

    from bowhead_bi_bridge import BINotFoundError

    return isinstance(exc, BINotFoundError) and exc.errcode is None


def _is_key_rejection(exc: Exception) -> bool:
    from bowhead_bi_bridge import BIPermissionError, DatarcError

    return isinstance(exc, BIPermissionError) or (isinstance(exc, DatarcError) and exc.errcode in {401, 403})


async def _fetch_datarc_scope(endpoint: str, connection_key: str) -> tuple[dict[str, Any], str, str]:
    from bowhead_bi_bridge import BIConnectionError, DatarcClient

    client = DatarcClient(base_url=endpoint, auth_key=connection_key, timeout_sec=15, ssrf_guard=True)
    try:
        try:
            scope = normalize_datarc_scope(await client.get_scope())
        except Exception as exc:
            if not _is_missing_route(exc):
                raise
            # Instances older than the key-scope API answer the route itself with 404
            # (before authenticating). Verify the key against the long-standing
            # workspaces route instead and derive the scope snapshot from what it lists.
            listed = await client.list_workspaces()
            scope = datarc_scope_from_listed_workspaces([{"id": item.id, "name": item.name, "desc": item.desc} for item in listed])
    except Exception as exc:
        if _is_key_rejection(exc):
            raise _datarc_verification_error(422, "datarc_connection_key_invalid") from exc
        if _is_missing_route(exc):
            raise _datarc_verification_error(422, "datarc_endpoint_unsupported") from exc
        if isinstance(exc, BIConnectionError):
            raise _datarc_verification_error(502, "datarc_verification_unreachable") from exc
        raise _datarc_verification_error(502, "datarc_verification_failed") from exc
    finally:
        await client.aclose()
    return scope, datarc_scope_fingerprint(scope), _now()


async def _audit_builtin_tool(request: Request, user: User, action: str, tool: TeamBuiltinTool) -> None:
    # The mutation has already committed. Audit is OPS-tier best effort and must
    # never turn a successful create/update into a misleading HTTP 500.
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


async def _verify_tavily(api_key: str) -> None:
    """Use Tavily's search endpoint: a real credential check without persisting a query.

    Goes through the same client as runtime search, so verification and runtime share one auth shape.
    432/433 mean the key is valid but out of credits, and 429 means it is valid but rate limited; saving is
    allowed in all three so an admin can still manage it."""
    from bowhead_core.tavily_client import tavily_post

    _data, error = await tavily_post(api_key, "/search", {"query": "Bowhead connection verification", "max_results": 1}, timeout=15.0)
    if error is None or error.kind in {"tavily_plan_quota_exhausted", "tavily_spend_limit_reached", "tavily_rate_limited"}:
        return
    if error.kind == "tavily_key_invalid" or error.status == 403:
        raise HTTPException(422, "tavily_api_key_invalid")
    if error.status is None:
        raise HTTPException(502, "tavily_verification_unreachable")
    raise HTTPException(502, "tavily_verification_failed")


@router.get("")
async def list_builtin_tools(request: Request, user: User = Depends(_REQUIRE_TEAM_ADMIN)) -> list[dict[str, Any]]:
    store = _store(request)
    # Google Workspace has its own administration API (routes/google_workspace.py).
    from bowhead_core.builtin_shopify_tool import feature_enabled

    rows = [
        tool
        for tool in await store.list_team_builtin_tools(user.team_id)
        if tool.provider != "google_workspace" and (tool.provider != "shopify" or feature_enabled())
    ]
    return [
        await _powerbi_public(request, tool)
        if tool.provider == _POWERBI_PROVIDER
        else _shopify_public(tool)
        if tool.provider == "shopify"
        else _public(tool)
        for tool in rows
    ]


@router.put("/tavily")
async def configure_tavily(body: ConfigureBody, request: Request, user: User = Depends(_REQUIRE_TEAM_ADMIN)) -> dict[str, Any]:
    await require_not_readonly(request)
    api_key = body.api_key.strip()
    await _verify_tavily(api_key)
    store = _store(request)
    try:
        tool = await store.upsert_team_builtin_tool(
            team_id=user.team_id, provider=_PROVIDER, auth={"api_key": api_key}, status="enabled" if body.enabled else "disabled", created_by=user.id
        )
    except RuntimeError as exc:
        raise _coded_error(503, "builtin_tool_encryption_required") from exc
    await _audit_builtin_tool(request, user, AuditAction.TEAM_TOOL_SOURCE_UPDATED, tool)
    return _public(tool)


@router.patch("/tavily/status")
async def update_tavily_status(body: StatusBody, request: Request, user: User = Depends(_REQUIRE_TEAM_ADMIN)) -> dict[str, Any]:
    await require_not_readonly(request)
    tool = await _store(request).update_team_builtin_tool_status(user.team_id, _PROVIDER, "enabled" if body.enabled else "disabled")
    if tool is None:
        raise HTTPException(404, "tavily_not_configured")
    await _audit_builtin_tool(request, user, AuditAction.TEAM_TOOL_SOURCE_UPDATED, tool)
    return _public(tool)


@router.post("/datarc/validate")
async def validate_datarc(body: ValidateDatarcBody, user: User = Depends(_REQUIRE_TEAM_ADMIN)) -> dict[str, Any]:
    endpoint = _normalize_datarc_endpoint(body.endpoint)
    scope, _, checked_at = await _fetch_datarc_scope(endpoint, body.connection_key.strip())
    return {"ok": True, "scope": scope, "scope_checked_at": checked_at}


@router.post("/datarc", dependencies=[Depends(require_not_readonly)])
async def create_datarc(body: ConfigureDatarcBody, request: Request, user: User = Depends(_REQUIRE_TEAM_ADMIN)) -> dict[str, Any]:
    endpoint = _normalize_datarc_endpoint(body.endpoint)
    name = _normalize_datarc_name(body.name)
    connection_key = body.connection_key.strip()
    if not connection_key:
        raise HTTPException(422, "datarc_connection_key_required")
    scope, scope_fingerprint, scope_checked_at = await _fetch_datarc_scope(endpoint, connection_key)
    try:
        tool = await _store(request).create_team_builtin_tool_instance(
            team_id=user.team_id,
            provider=_DATARC_PROVIDER,
            display_name=name,
            endpoint=endpoint,
            auth={"connection_key": connection_key},
            key_hint=connection_key[-4:],
            status="enabled" if body.enabled else "disabled",
            created_by=user.id,
            scope_state="verified",
            scope=scope,
            scope_fingerprint=scope_fingerprint,
            scope_checked_at=scope_checked_at,
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "datarc_name_conflict") from exc
    except RuntimeError as exc:
        raise _coded_error(503, "builtin_tool_encryption_required") from exc
    await _audit_builtin_tool(request, user, AuditAction.TEAM_TOOL_SOURCE_CREATED, tool)
    return _public(tool)


@router.patch("/datarc/{tool_id}", dependencies=[Depends(require_not_readonly)])
async def patch_datarc(
    tool_id: str,
    body: PatchDatarcBody,
    request: Request,
    user: User = Depends(_REQUIRE_TEAM_ADMIN),
) -> dict[str, Any]:
    store = _store(request)
    current = await store.get_team_builtin_tool_instance(user.team_id, tool_id)
    if current is None or current.provider != _DATARC_PROVIDER:
        raise HTTPException(404, "datarc_tool_not_found")
    endpoint = _normalize_datarc_endpoint(body.endpoint) if body.endpoint is not None else current.endpoint
    connection_key = body.connection_key.strip() if body.connection_key is not None else ""
    if body.connection_key is None:
        auth = await store.get_team_builtin_tool_instance_auth_decrypted(user.team_id, tool_id)
        connection_key = str(auth.get("connection_key") or "")
    if not connection_key:
        raise HTTPException(422, "datarc_connection_key_required")
    scope, scope_fingerprint, scope_checked_at = await _fetch_datarc_scope(endpoint, connection_key)
    try:
        tool = await store.update_team_builtin_tool_instance(
            team_id=user.team_id,
            tool_id=tool_id,
            display_name=_normalize_datarc_name(body.name) if body.name is not None else None,
            endpoint=endpoint if body.endpoint is not None else None,
            auth={"connection_key": connection_key} if body.connection_key is not None else None,
            key_hint=connection_key[-4:] if body.connection_key is not None else None,
            scope_state="verified",
            scope=scope,
            scope_fingerprint=scope_fingerprint,
            scope_checked_at=scope_checked_at,
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "datarc_name_conflict") from exc
    except RuntimeError as exc:
        raise _coded_error(503, "builtin_tool_encryption_required") from exc
    if tool is None:
        raise HTTPException(404, "datarc_tool_not_found")
    await _audit_builtin_tool(request, user, AuditAction.TEAM_TOOL_SOURCE_UPDATED, tool)
    return _public(tool)


@router.patch("/datarc/{tool_id}/status", dependencies=[Depends(require_not_readonly)])
async def update_datarc_status(
    tool_id: str,
    body: StatusBody,
    request: Request,
    user: User = Depends(_REQUIRE_TEAM_ADMIN),
) -> dict[str, Any]:
    current = await _store(request).get_team_builtin_tool_instance(user.team_id, tool_id)
    if current is None or current.provider != _DATARC_PROVIDER:
        raise HTTPException(404, "datarc_tool_not_found")
    scope: dict[str, Any] | None = None
    scope_fingerprint: str | None = None
    scope_checked_at: str | None = None
    if body.enabled:
        auth = await _store(request).get_team_builtin_tool_instance_auth_decrypted(user.team_id, tool_id)
        connection_key = str(auth.get("connection_key") or "")
        if not connection_key:
            raise HTTPException(422, "datarc_connection_key_required")
        scope, scope_fingerprint, scope_checked_at = await _fetch_datarc_scope(current.endpoint, connection_key)
    tool = await _store(request).update_team_builtin_tool_instance(
        team_id=user.team_id,
        tool_id=tool_id,
        status="enabled" if body.enabled else "disabled",
        scope_state="verified" if body.enabled else None,
        scope=scope,
        scope_fingerprint=scope_fingerprint,
        scope_checked_at=scope_checked_at,
    )
    assert tool is not None
    await _audit_builtin_tool(request, user, AuditAction.TEAM_TOOL_SOURCE_UPDATED, tool)
    return _public(tool)


@router.post("/datarc/{tool_id}/scope/refresh", dependencies=[Depends(require_not_readonly)])
async def refresh_datarc_scope(
    tool_id: str,
    request: Request,
    user: User = Depends(_REQUIRE_TEAM_ADMIN),
) -> dict[str, Any]:
    store = _store(request)
    current = await store.get_team_builtin_tool_instance(user.team_id, tool_id)
    if current is None or current.provider != _DATARC_PROVIDER:
        raise HTTPException(404, "datarc_tool_not_found")
    auth = await store.get_team_builtin_tool_instance_auth_decrypted(user.team_id, tool_id)
    connection_key = str(auth.get("connection_key") or "")
    if not connection_key:
        raise HTTPException(422, "datarc_connection_key_required")
    try:
        scope, fingerprint, checked_at = await _fetch_datarc_scope(current.endpoint, connection_key)
    except HTTPException as exc:
        if exc.status_code == 422:
            await store.update_team_builtin_tool_instance(team_id=user.team_id, tool_id=tool_id, scope_state="invalid")
        raise
    tool = await store.update_team_builtin_tool_instance(
        team_id=user.team_id,
        tool_id=tool_id,
        scope_state="verified",
        scope=scope,
        scope_fingerprint=fingerprint,
        scope_checked_at=checked_at,
    )
    assert tool is not None
    await _audit_builtin_tool(request, user, AuditAction.TEAM_TOOL_SOURCE_UPDATED, tool)
    return _public(tool)


@router.delete("/datarc/{tool_id}", status_code=204, dependencies=[Depends(require_not_readonly)])
async def delete_datarc(tool_id: str, request: Request, user: User = Depends(_REQUIRE_TEAM_ADMIN)) -> None:
    store = _store(request)
    tool = await store.get_team_builtin_tool_instance(user.team_id, tool_id)
    if tool is None or tool.provider != _DATARC_PROVIDER:
        raise HTTPException(404, "datarc_tool_not_found")
    if not await store.delete_team_builtin_tool_instance(user.team_id, tool_id):
        raise HTTPException(404, "datarc_tool_not_found")
    await _audit_builtin_tool(request, user, AuditAction.TEAM_TOOL_SOURCE_DELETED, tool)


async def _verified_powerbi(config: dict[str, str]) -> dict[str, str]:
    from bowhead_core.pbi_client import PbiClient, PbiTokenProvider

    from bowhead_gateway.powerbi_discovery import _resolved_config

    resolved = await _resolved_config(config)
    result = await PbiClient(
        workspace_id=resolved["workspace_id"],
        dataset_id=resolved["dataset_id"],
        token_provider=PbiTokenProvider(resolved),
    ).execute_dax('EVALUATE ROW("bowhead_connection_test", 1)')
    if result.error_code:
        raise _coded_error(422, result.error_code)
    return resolved


def _powerbi_config(body: ConfigurePowerBiBody) -> dict[str, str]:
    return {
        "tenant_id": body.tenant_id.strip(),
        "client_id": body.client_id.strip(),
        "client_secret": body.client_secret,
        "workspace_id": body.workspace_id.strip(),
        "dataset_id": body.dataset_id.strip(),
    }


async def _powerbi_public(request: Request, tool: TeamBuiltinTool) -> dict[str, Any]:
    auth = await _store(request).get_team_builtin_tool_instance_auth_decrypted(tool.team_id, tool.id)
    return {
        **_public(tool),
        "tenant_id": auth.get("tenant_id", ""),
        "client_id": auth.get("client_id", ""),
        "workspace_id": auth.get("workspace_id", ""),
        "dataset_id": auth.get("dataset_id", ""),
    }


@router.post("/powerbi", dependencies=[Depends(require_not_readonly)])
async def create_powerbi(body: ConfigurePowerBiBody, request: Request, user: User = Depends(_REQUIRE_TEAM_ADMIN)) -> dict[str, Any]:
    name = " ".join(body.name.split())
    if not name:
        raise _coded_error(422, "powerbi_name_required")
    config = await _verified_powerbi(_powerbi_config(body))
    try:
        tool = await _store(request).create_team_builtin_tool_instance(
            team_id=user.team_id,
            provider=_POWERBI_PROVIDER,
            display_name=name,
            endpoint="",
            auth=config,
            key_hint=config["client_secret"][-4:],
            status="enabled" if body.enabled else "disabled",
            created_by=user.id,
        )
    except sqlite3.IntegrityError as exc:
        raise _coded_error(409, "powerbi_name_conflict") from exc
    except RuntimeError as exc:
        raise _coded_error(503, "builtin_tool_encryption_required") from exc
    await _audit_builtin_tool(request, user, AuditAction.TEAM_TOOL_SOURCE_CREATED, tool)
    return await _powerbi_public(request, tool)


@router.patch("/powerbi/{tool_id}", dependencies=[Depends(require_not_readonly)])
async def patch_powerbi(tool_id: str, body: PatchPowerBiBody, request: Request, user: User = Depends(_REQUIRE_TEAM_ADMIN)) -> dict[str, Any]:
    store = _store(request)
    current = await store.get_team_builtin_tool_instance(user.team_id, tool_id)
    if current is None or current.provider != _POWERBI_PROVIDER:
        raise _coded_error(404, "powerbi_tool_not_found")
    name = " ".join(body.name.split()) if body.name is not None else None
    if name == "":
        raise _coded_error(422, "powerbi_name_required")
    config = await store.get_team_builtin_tool_instance_auth_decrypted(user.team_id, tool_id)
    for field in ("tenant_id", "client_id", "client_secret", "workspace_id", "dataset_id"):
        value = getattr(body, field)
        if value is not None:
            config[field] = value.strip() if field != "client_secret" else value
    config = await _verified_powerbi(config)
    try:
        tool = await store.update_team_builtin_tool_instance(
            team_id=user.team_id,
            tool_id=tool_id,
            display_name=name,
            auth=config,
            key_hint=config["client_secret"][-4:],
        )
    except sqlite3.IntegrityError as exc:
        raise _coded_error(409, "powerbi_name_conflict") from exc
    assert tool is not None
    await _audit_builtin_tool(request, user, AuditAction.TEAM_TOOL_SOURCE_UPDATED, tool)
    return await _powerbi_public(request, tool)


@router.patch("/powerbi/{tool_id}/status", dependencies=[Depends(require_not_readonly)])
async def update_powerbi_status(tool_id: str, body: StatusBody, request: Request, user: User = Depends(_REQUIRE_TEAM_ADMIN)) -> dict[str, Any]:
    store = _store(request)
    current = await store.get_team_builtin_tool_instance(user.team_id, tool_id)
    if current is None or current.provider != _POWERBI_PROVIDER:
        raise _coded_error(404, "powerbi_tool_not_found")
    if body.enabled:
        config = await store.get_team_builtin_tool_instance_auth_decrypted(user.team_id, tool_id)
        await _verified_powerbi(config)
    tool = await store.update_team_builtin_tool_instance(team_id=user.team_id, tool_id=tool_id, status="enabled" if body.enabled else "disabled")
    assert tool is not None
    await _audit_builtin_tool(request, user, AuditAction.TEAM_TOOL_SOURCE_UPDATED, tool)
    return await _powerbi_public(request, tool)


@router.delete("/powerbi/{tool_id}", status_code=204, dependencies=[Depends(require_not_readonly)])
async def delete_powerbi(tool_id: str, request: Request, user: User = Depends(_REQUIRE_TEAM_ADMIN)) -> None:
    store = _store(request)
    tool = await store.get_team_builtin_tool_instance(user.team_id, tool_id)
    if tool is None or tool.provider != _POWERBI_PROVIDER:
        raise _coded_error(404, "powerbi_tool_not_found")
    if not await store.delete_team_builtin_tool_instance(user.team_id, tool_id):
        raise _coded_error(404, "powerbi_tool_not_found")
    await _audit_builtin_tool(request, user, AuditAction.TEAM_TOOL_SOURCE_DELETED, tool)


@router.get("/powerbi/{tool_id}/templates")
async def list_powerbi_templates(tool_id: str, request: Request, user: User = Depends(_REQUIRE_TEAM_ADMIN)) -> list[dict[str, str]]:
    tool = await _store(request).get_team_builtin_tool_instance(user.team_id, tool_id)
    if tool is None or tool.provider != _POWERBI_PROVIDER:
        raise _coded_error(404, "powerbi_tool_not_found")
    return await _store(request).list_builtin_pbi_templates(user.team_id, tool_id)


@router.post("/powerbi/{tool_id}/templates", dependencies=[Depends(require_not_readonly)])
async def create_powerbi_template(
    tool_id: str, body: PowerBiTemplateBody, request: Request, user: User = Depends(_REQUIRE_TEAM_ADMIN)
) -> dict[str, str]:
    store = _store(request)
    tool = await store.get_team_builtin_tool_instance(user.team_id, tool_id)
    if tool is None or tool.provider != _POWERBI_PROVIDER:
        raise _coded_error(404, "powerbi_tool_not_found")
    name = body.name.strip()
    if not name:
        raise _coded_error(422, "powerbi_template_name_required")
    try:
        return await store.create_builtin_pbi_template(user.team_id, tool_id, name, body.dax_template)
    except sqlite3.IntegrityError as exc:
        raise _coded_error(409, "powerbi_template_name_conflict") from exc


@router.delete("/powerbi/{tool_id}/templates/{template_id}", status_code=204, dependencies=[Depends(require_not_readonly)])
async def delete_powerbi_template(tool_id: str, template_id: str, request: Request, user: User = Depends(_REQUIRE_TEAM_ADMIN)) -> None:
    if not await _store(request).delete_builtin_pbi_template(user.team_id, tool_id, template_id):
        raise _coded_error(404, "powerbi_template_not_found")


# Shopify uses the existing native Team registry; no separate MCP server is needed.
class ConfigureShopifyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=60)
    shop_domain: str = Field(min_length=1, max_length=128)
    client_id: str = Field(min_length=1, max_length=128)
    client_secret: str = Field(min_length=1, max_length=4096, repr=False)
    enabled: bool = True


class PatchShopifyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=60)
    client_id: str | None = Field(default=None, min_length=1, max_length=128)
    client_secret: str | None = Field(default=None, min_length=1, max_length=4096, repr=False)


class ShopifyStatusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_revision: int = Field(ge=1)
    enabled: bool


def _shopify_gate() -> None:
    from bowhead_core.builtin_shopify_tool import feature_enabled

    if not feature_enabled():
        raise _coded_error(404, "shopify_feature_unavailable")


def _shopify_public(tool: TeamBuiltinTool) -> dict[str, Any]:
    from bowhead_core.builtin_shopify_tool import supported_operations

    return {
        **_public(tool),
        "display_name": f"Shopify - {tool.display_name}",
        "shop_domain": tool.config.get("shop_domain", ""),
        "client_id": tool.config.get("client_id", ""),
        "shop_name": tool.config.get("shop_name", ""),
        "granted_scopes": tool.config.get("granted_scopes", []),
        "connection_revision": tool.connection_revision,
        "supported_tools": list(supported_operations(tool)),
    }


async def _verified_shopify(domain: str, auth: dict[str, Any]) -> dict[str, Any]:
    from bowhead_core.shopify_client import ShopifyClient, ShopifyClientError, ShopifyCredential

    try:
        credential = ShopifyCredential(shop_domain=domain, client_id=auth.get("client_id", ""), client_secret=auth.get("client_secret", ""))
        result = await ShopifyClient(credential).read_shop()
    except ShopifyClientError as exc:
        raise _coded_error(422, exc.code) from None
    shop = result.data["shop"]
    return {
        "shop_domain": credential.shop_domain,
        "client_id": credential.client_id,
        "shop_gid": shop["id"],
        "shop_name": shop["name"],
        "currency": shop["currencyCode"],
        "timezone": shop["ianaTimezone"],
        "granted_scopes": sorted(result.granted_scopes),
        "api_version": result.api_version,
    }


async def _shopify_current(request: Request, user: User, tool_id: str) -> TeamBuiltinTool:
    _shopify_gate()
    tool = await _store(request).get_team_builtin_tool_instance(user.team_id, tool_id)
    if tool is None or tool.provider != "shopify":
        raise _coded_error(404, "shopify_connection_unavailable")
    return tool


@router.post("/shopify", dependencies=[Depends(require_not_readonly)])
async def create_shopify(body: ConfigureShopifyBody, request: Request, user: User = Depends(_REQUIRE_TEAM_ADMIN)) -> dict[str, Any]:
    _shopify_gate()
    name = " ".join(body.name.split())
    if not name:
        raise _coded_error(422, "shopify_name_required")
    auth = {"client_id": body.client_id.strip(), "client_secret": body.client_secret}
    config = await _verified_shopify(body.shop_domain, auth)
    try:
        tool = await _store(request).create_team_builtin_tool_instance(
            team_id=user.team_id,
            provider="shopify",
            display_name=name,
            endpoint=f"https://{config['shop_domain']}",
            auth=auth,
            key_hint=None,
            status="enabled" if body.enabled else "disabled",
            created_by=user.id,
            scope_state="verified",
            scope_checked_at=_now(),
            config=config,
        )
    except sqlite3.IntegrityError:
        raise _coded_error(409, "shopify_name_conflict") from None
    except RuntimeError:
        raise _coded_error(503, "builtin_tool_encryption_required") from None
    await _audit_builtin_tool(request, user, AuditAction.TEAM_TOOL_SOURCE_CREATED, tool)
    return _shopify_public(tool)


@router.patch("/shopify/{tool_id}", dependencies=[Depends(require_not_readonly)])
async def patch_shopify(tool_id: str, body: PatchShopifyBody, request: Request, user: User = Depends(_REQUIRE_TEAM_ADMIN)) -> dict[str, Any]:
    from bowhead_core.llm_config_crypto import decrypt_llm_config

    current = await _shopify_current(request, user, tool_id)
    if current.connection_revision != body.connection_revision:
        raise _coded_error(409, "shopify_connection_changed")
    name = " ".join(body.name.split()) if body.name is not None else current.display_name
    if not name:
        raise _coded_error(422, "shopify_name_required")
    auth = decrypt_llm_config(current.auth_ref)
    for key in ("client_id", "client_secret"):
        value = getattr(body, key)
        if value is not None:
            auth[key] = value.strip() if key == "client_id" else value
    config = await _verified_shopify(current.config["shop_domain"], auth)
    if config["shop_gid"] != current.config["shop_gid"]:
        raise _coded_error(409, "shopify_shop_identity_changed")
    try:
        tool = await _store(request).update_team_builtin_tool_instance(
            team_id=user.team_id,
            tool_id=tool_id,
            expected_revision=body.connection_revision,
            display_name=name,
            auth=auth,
            config=config,
            scope_checked_at=_now(),
        )
    except sqlite3.IntegrityError:
        raise _coded_error(409, "shopify_name_conflict") from None
    except RuntimeError:
        raise _coded_error(503, "builtin_tool_encryption_required") from None
    if tool is None:
        raise _coded_error(409, "shopify_connection_changed")
    await _audit_builtin_tool(request, user, AuditAction.TEAM_TOOL_SOURCE_UPDATED, tool)
    return _shopify_public(tool)


@router.patch("/shopify/{tool_id}/status", dependencies=[Depends(require_not_readonly)])
async def set_shopify_status(tool_id: str, body: ShopifyStatusBody, request: Request, user: User = Depends(_REQUIRE_TEAM_ADMIN)) -> dict[str, Any]:
    from bowhead_core.llm_config_crypto import decrypt_llm_config

    current = await _shopify_current(request, user, tool_id)
    if current.connection_revision != body.connection_revision:
        raise _coded_error(409, "shopify_connection_changed")
    config = current.config
    if body.enabled:
        config = await _verified_shopify(current.config["shop_domain"], decrypt_llm_config(current.auth_ref))
        if config["shop_gid"] != current.config["shop_gid"]:
            raise _coded_error(409, "shopify_shop_identity_changed")
    tool = await _store(request).update_team_builtin_tool_instance(
        team_id=user.team_id,
        tool_id=tool_id,
        expected_revision=body.connection_revision,
        status="enabled" if body.enabled else "disabled",
        config=config,
    )
    if tool is None:
        raise _coded_error(409, "shopify_connection_changed")
    await _audit_builtin_tool(request, user, AuditAction.TEAM_TOOL_SOURCE_UPDATED, tool)
    return _shopify_public(tool)


@router.delete("/shopify/{tool_id}", status_code=204, dependencies=[Depends(require_not_readonly)])
async def delete_shopify(tool_id: str, request: Request, user: User = Depends(_REQUIRE_TEAM_ADMIN)) -> None:
    tool = await _shopify_current(request, user, tool_id)
    if not await _store(request).delete_team_builtin_tool_instance(user.team_id, tool_id):
        raise _coded_error(404, "shopify_connection_unavailable")
    await _audit_builtin_tool(request, user, AuditAction.TEAM_TOOL_SOURCE_DELETED, tool)


@router.post("/shopify/{tool_id}/plugin", dependencies=[Depends(require_not_readonly)])
async def create_shopify_plugin(tool_id: str, request: Request, user: User = Depends(_REQUIRE_TEAM_ADMIN)) -> dict[str, Any]:
    return await _create_shopify_plugin(tool_id, request, user, content=False)


@router.post("/shopify/{tool_id}/content-plugin", dependencies=[Depends(require_not_readonly)])
async def create_shopify_content_plugin(tool_id: str, request: Request, user: User = Depends(_REQUIRE_TEAM_ADMIN)) -> dict[str, Any]:
    return await _create_shopify_plugin(tool_id, request, user, content=True)


async def _create_shopify_plugin(tool_id: str, request: Request, user: User, *, content: bool) -> dict[str, Any]:
    from bowhead_core.builtin_shopify_tool import shopify_tool_ref, supported_operations
    from bowhead_core.shopify_content import CONTENT_OPERATIONS
    from fastapi import Response

    from bowhead_gateway.routes.agents import agent_authoring_catalog
    from bowhead_gateway.routes.team_plugins import PluginBody, create_plugin

    tool = await _shopify_current(request, user, tool_id)
    if tool.status != "enabled":
        raise _coded_error(422, "shopify_connection_unavailable")
    operations = supported_operations(tool)
    if content and "shopify_preview_content_update" not in operations:
        raise _coded_error(422, "shopify_content_unavailable")
    store = _store(request)
    catalog = await agent_authoring_catalog(request, Response(), user, store)
    skill_id = "builtin:shopify-content-optimization" if content else "builtin:shopify-analysis"
    skill = next((item for item in catalog["skills"] if item["grant"]["skill_ref"]["id"] == skill_id), None)
    if skill is None:
        raise _coded_error(422, "capability_skill_unavailable")
    selected = (
        [op for op in operations if op in CONTENT_OPERATIONS or op in {"shopify_shop_context", "shopify_list_products", "shopify_list_pages"}]
        if content
        else [op for op in operations if op not in CONTENT_OPERATIONS]
    )
    return await create_plugin(
        PluginBody(
            name=f"Shopify content - {tool.display_name}" if content else f"Shopify - {tool.display_name}",
            description="Shopify content optimization with exact requester review before each update."
            if content
            else "Read-only Shopify operational analysis and store content.",
            tool_refs=[shopify_tool_ref(tool.id, op) for op in selected],
            skill_grants=[skill["grant"]],
        ),
        request,
        user,
        store,
    )
