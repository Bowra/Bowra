"""Team-managed Plugin bundles for Agent authoring."""

from __future__ import annotations

import sqlite3
from typing import Any

from bowhead_core.team_plugin_store import TeamPluginError, TeamPluginStore, skill_grant_identity
from bowhead_core.user_store import User, UserStore
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.routes._deps import require_team_admin
from bowhead_gateway.routes.auth import _current_user, _store

router = APIRouter()


class PluginBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=1000)
    tool_refs: list[str] = Field(default_factory=list, max_length=128)
    skill_grants: list[dict[str, Any]] = Field(default_factory=list, max_length=64)


class PluginUpdateBody(PluginBody):
    row_version: int = Field(ge=1)


class PluginStatusBody(BaseModel):
    enabled: bool
    row_version: int = Field(ge=1)


def _coded_error(status: int, code: str) -> HTTPException:
    """Code-only detail; the gateway error handler adds the localized catalog message."""
    return HTTPException(status, detail={"code": code})


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, TeamPluginError):
        status = 404 if exc.code == "capability_not_found" else 409 if exc.code in {"capability_version_conflict", "capability_disabled"} else 422
        return _coded_error(status, exc.code)
    if isinstance(exc, sqlite3.IntegrityError):
        return _coded_error(409, "capability_name_conflict")
    raise exc


async def _check_resources(body: PluginBody, request: Request, user: User, store: UserStore) -> list[dict[str, Any]]:
    """Check the selection against the authoring catalog and return the Skill grants to store.

    Skills follow their latest approved revision (agent-skill-gates D2), so a
    grant matches the catalog by Skill identity and permissions; the stored
    grant takes the catalog's current revision.
    """
    # Reuse exactly the catalog Agent authors use. Team plugins cannot
    # contain private Skills or tools unavailable for Agent selection.
    from bowhead_gateway.routes.agents import agent_authoring_catalog

    catalog = await agent_authoring_catalog(request, Response(), user, store)
    selectable_tools = {str(tool["stable_ref"]) for tool in catalog["tools"] if tool["selectable"]}
    if any(ref not in selectable_tools for ref in body.tool_refs):
        raise _coded_error(422, "capability_tool_unavailable")
    selectable_skills = {skill_grant_identity(skill["grant"]): skill["grant"] for skill in catalog["skills"] if skill["source_kind"] != "user"}
    grants: list[dict[str, Any]] = []
    for grant in body.skill_grants:
        current = selectable_skills.get(skill_grant_identity(grant))
        if current is None:
            raise _coded_error(422, "capability_skill_unavailable")
        grants.append(dict(current))
    return grants


# Keep route names stable for generated OpenAPI operation IDs.
@router.get("", name="list_capabilities")
async def list_plugins(user: User = Depends(_current_user), store: UserStore = Depends(_store)) -> list[dict[str, Any]]:
    return await TeamPluginStore(store.db_path).list(user.team_id, include_disabled=True)


@router.get("/{capability_id}", name="get_capability")
async def get_plugin(capability_id: str, user: User = Depends(_current_user), store: UserStore = Depends(_store)) -> dict[str, Any]:
    item = await TeamPluginStore(store.db_path).get(user.team_id, capability_id)
    if item is None or item["status"] != "active" and not await store.is_team_admin(user.id, user.team_id):
        raise _coded_error(404, "capability_not_found")
    return item


@router.post("", name="create_capability", dependencies=[Depends(require_not_readonly)])
async def create_plugin(
    body: PluginBody, request: Request, user: User = Depends(require_team_admin), store: UserStore = Depends(_store)
) -> dict[str, Any]:
    skill_grants = await _check_resources(body, request, user, store)
    try:
        return await TeamPluginStore(store.db_path).create(
            team_id=user.team_id,
            actor_user_id=user.id,
            name=body.name,
            description=body.description,
            tool_refs=body.tool_refs,
            skill_grants=skill_grants,
        )
    except (TeamPluginError, sqlite3.IntegrityError) as exc:
        raise _error(exc) from exc


@router.put("/{capability_id}", name="update_capability", dependencies=[Depends(require_not_readonly)])
async def update_plugin(
    capability_id: str, body: PluginUpdateBody, request: Request, user: User = Depends(require_team_admin), store: UserStore = Depends(_store)
) -> dict[str, Any]:
    skill_grants = await _check_resources(body, request, user, store)
    try:
        return await TeamPluginStore(store.db_path).update(
            team_id=user.team_id,
            plugin_id=capability_id,
            actor_user_id=user.id,
            expected_row_version=body.row_version,
            name=body.name,
            description=body.description,
            tool_refs=body.tool_refs,
            skill_grants=skill_grants,
        )
    except (TeamPluginError, sqlite3.IntegrityError) as exc:
        raise _error(exc) from exc


@router.patch("/{capability_id}/status", name="change_capability_status", dependencies=[Depends(require_not_readonly)])
async def change_plugin_status(
    capability_id: str, body: PluginStatusBody, user: User = Depends(require_team_admin), store: UserStore = Depends(_store)
) -> dict[str, Any]:
    try:
        return await TeamPluginStore(store.db_path).set_enabled(
            team_id=user.team_id,
            plugin_id=capability_id,
            enabled=body.enabled,
            expected_row_version=body.row_version,
        )
    except TeamPluginError as exc:
        raise _error(exc) from exc


@router.delete("/{capability_id}", name="delete_capability", status_code=204, dependencies=[Depends(require_not_readonly)])
async def delete_plugin(
    capability_id: str,
    row_version: int = Query(ge=1),
    user: User = Depends(require_team_admin),
    store: UserStore = Depends(_store),
) -> Response:
    try:
        await TeamPluginStore(store.db_path).delete(team_id=user.team_id, plugin_id=capability_id, expected_row_version=row_version)
    except TeamPluginError as exc:
        raise _error(exc) from exc
    return Response(status_code=204)
