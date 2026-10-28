"""Built-in Plugin and Tool state: team and personal disabling (Capability Center tech design §8.5).

Endpoints require their existing authorization and resource guards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from bowhead_core.agents._generated.tool_kinds import TOOL_RISKS, USER_VISIBLE_TOOLS
from bowhead_core.audit_actions import AuditAction
from bowhead_core.resource_disable import (
    KEY_BUILTIN_PLUGINS_DISABLED,
    KEY_TEAM_PLUGINS_DISABLED,
    KEY_TEAM_TOOL_STATES,
    disabled_names,
    tool_states,
    with_name,
    with_tool_state,
)
from bowhead_core.system_plugins import BuiltinPlugin, builtin_plugins
from bowhead_core.team_plugin_store import TeamPluginStore
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.routes.auth import _current_user, _require_team_role_dep
from bowhead_gateway.routes.auth import _store as _user_store

if TYPE_CHECKING:
    from bowhead_core.team_settings_store import TeamSettingsStore
    from bowhead_core.user_settings_store import UserSettingsStore
    from bowhead_core.user_store import User, UserStore


router = APIRouter()

_REQUIRE_TEAM_ADMIN = _require_team_role_dep("owner", "team_admin")
_CAS_ATTEMPTS = 3


class DisabledBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    disabled: bool


class ToolStateBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool
    acknowledge_risk: bool = False


def _team_store(request: Request) -> TeamSettingsStore:
    store = getattr(request.app.state, "team_settings_store", None)
    if store is None:
        raise HTTPException(503, "team_settings_store not configured")
    return store


def _user_settings(request: Request) -> UserSettingsStore:
    store = getattr(request.app.state, "user_settings_store", None)
    if store is None:
        raise HTTPException(503, "user_settings_store not configured")
    return store


def _latest_plugins() -> dict[str, BuiltinPlugin]:
    latest: dict[str, BuiltinPlugin] = {}
    for plugin in builtin_plugins():
        if plugin.name not in latest or plugin.version > latest[plugin.name].version:
            latest[plugin.name] = plugin
    return latest


def _plugin(name: str) -> BuiltinPlugin:
    plugin = _latest_plugins().get(name)
    if plugin is None:
        raise HTTPException(404, detail={"error": {"code": "plugin_not_found"}})
    return plugin


async def _team_update(store: TeamSettingsStore, team_id: str, key: str, user_id: str, mutate: Any) -> tuple[str | None, str]:
    """Read-modify-write one team setting with compare-and-set; concurrent edits retry, then 409."""
    for _ in range(_CAS_ATTEMPTS):
        before = await store.get(team_id, key)
        after = mutate(before)
        if await store.compare_and_set(team_id, key, expected_value=before, value=after, updated_by=user_id):
            return before, after
    raise HTTPException(409, detail={"error": {"code": "setting_changed_concurrently"}})


async def _audit(request: Request, user_store: UserStore, actor: User, key: str, target: str, before: str | None, after: str) -> None:
    await user_store.write_admin_audit_log(
        actor_user_id=actor.id,
        team_id=actor.team_id,
        action=AuditAction.TEAM_SETTINGS_CHANGED,
        target_kind="team_settings",
        target_id=key,
        target_team_id=actor.team_id,
        scope="team",
        payload={"setting_key": key, "target": target, "operation": "put", "before": before, "after": after},
        ip=request.client.host if request.client else None,
    )


def _plugin_view(
    plugin: BuiltinPlugin, team_disabled: frozenset[str], my_disabled: frozenset[str], unconfigured: frozenset[str] = frozenset()
) -> dict[str, Any]:
    unavailable = sorted(tool for tool in plugin.tools if tool in unconfigured)
    return {
        "name": plugin.name,
        "version": plugin.version,
        "family": plugin.family,
        "selectable": plugin.selectable,
        "listing": {locale: item.model_dump() for locale, item in (plugin.listing or {}).items()},
        "tools": list(plugin.tools),
        "tool_count": len(plugin.tools) + len(plugin.parts),
        "skill_count": 1 if plugin.skill_revision else 0,
        "prompt_modules": list(plugin.prompt_modules),
        "availability": list(plugin.availability),
        "team_disabled": plugin.name in team_disabled,
        "my_disabled": plugin.name in my_disabled,
        # Same readiness rule as the authoring catalog: ready while at least one Tool is available.
        "ready": not plugin.tools or len(unavailable) < len(plugin.tools),
        "unavailable_tools": unavailable,
    }


@router.get("/builtin-plugins")
async def list_builtin_plugins(
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "private, no-store"
    team_disabled = disabled_names(await _team_store(request).get(user.team_id, KEY_BUILTIN_PLUGINS_DISABLED), KEY_BUILTIN_PLUGINS_DISABLED)
    settings = _user_settings(request)
    my_disabled = disabled_names(await settings.get(user.id, KEY_BUILTIN_PLUGINS_DISABLED), KEY_BUILTIN_PLUGINS_DISABLED)
    my_team_disabled = disabled_names(await settings.get(user.id, KEY_TEAM_PLUGINS_DISABLED), KEY_TEAM_PLUGINS_DISABLED)
    from bowhead_core.capability_resolver import resolve_unconfigured_capability_tools

    unconfigured = await resolve_unconfigured_capability_tools(getattr(request.app.state, "orchestrator", None), user.team_id)
    return {
        "plugins": [_plugin_view(plugin, team_disabled, my_disabled, unconfigured) for plugin in _latest_plugins().values()],
        # Team Plugins this user disabled for themselves; team-level state stays on the team Plugin itself.
        "team_plugins_my_disabled": sorted(my_team_disabled),
    }


@router.put("/builtin-plugins/{name}/team-state", dependencies=[Depends(require_not_readonly)])
async def put_builtin_plugin_team_state(
    name: str,
    body: DisabledBody,
    request: Request,
    user_store: UserStore = Depends(_user_store),
    user: User = Depends(_REQUIRE_TEAM_ADMIN),
) -> dict[str, Any]:
    _plugin(name)
    key = KEY_BUILTIN_PLUGINS_DISABLED
    before, after = await _team_update(_team_store(request), user.team_id, key, user.id, lambda raw: with_name(raw, key, name, body.disabled))
    await _audit(request, user_store, user, key, name, before, after)
    return {"name": name, "team_disabled": body.disabled}


@router.put("/builtin-plugins/{name}/my-state", dependencies=[Depends(require_not_readonly)])
async def put_builtin_plugin_my_state(
    name: str,
    body: DisabledBody,
    request: Request,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    _plugin(name)
    key = KEY_BUILTIN_PLUGINS_DISABLED
    if not body.disabled and name in disabled_names(await _team_store(request).get(user.team_id, key), key):
        raise HTTPException(409, detail={"error": {"code": "disabled_by_team"}})
    await _user_settings(request).update_value(user.id, key, lambda raw: with_name(raw, key, name, body.disabled))
    return {"name": name, "my_disabled": body.disabled}


@router.put("/team-capabilities/{plugin_id}/my-state", dependencies=[Depends(require_not_readonly)])
async def put_team_plugin_my_state(
    plugin_id: str,
    body: DisabledBody,
    request: Request,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    plugin = await TeamPluginStore(_user_store(request).db_path).get(user.team_id, plugin_id)
    if plugin is None:
        raise HTTPException(404, detail={"error": {"code": "plugin_not_found"}})
    if not body.disabled and plugin["status"] != "active":
        raise HTTPException(409, detail={"error": {"code": "disabled_by_team"}})
    key = KEY_TEAM_PLUGINS_DISABLED
    await _user_settings(request).update_value(user.id, key, lambda raw: with_name(raw, key, plugin_id, body.disabled))
    return {"id": plugin_id, "my_disabled": body.disabled}


@router.get("/builtin-tools")
async def list_builtin_tools(
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "private, no-store"
    states = tool_states(await _team_store(request).get(user.team_id, KEY_TEAM_TOOL_STATES))
    return {"tools": [{"name": name, "risk": TOOL_RISKS[name], **state} for name, state in states.items()]}


@router.put("/builtin-tools/{name}/team-state", dependencies=[Depends(require_not_readonly)])
async def put_builtin_tool_team_state(
    name: str,
    body: ToolStateBody,
    request: Request,
    user_store: UserStore = Depends(_user_store),
    user: User = Depends(_REQUIRE_TEAM_ADMIN),
) -> dict[str, Any]:
    if name not in USER_VISIBLE_TOOLS:
        raise HTTPException(404, detail={"error": {"code": "tool_not_found"}})
    store = _team_store(request)
    key = KEY_TEAM_TOOL_STATES
    current = tool_states(await store.get(user.team_id, key))[name]
    turning_on_high_risk = body.enabled and not current["enabled"] and TOOL_RISKS[name] == "external_write"
    if turning_on_high_risk and not body.acknowledge_risk:
        raise HTTPException(422, detail={"error": {"code": "risk_acknowledgement_required"}})
    before, after = await _team_update(
        store,
        user.team_id,
        key,
        user.id,
        lambda raw: with_tool_state(raw, name, enabled=body.enabled),
    )
    await _audit(request, user_store, user, key, name, before, after)
    return {"name": name, "risk": TOOL_RISKS[name], "enabled": body.enabled}


@router.get("/resources/impact")
async def get_disable_impact(
    kind: Literal["builtin_plugin", "team_plugin", "builtin_tool"],
    request: Request,
    response: Response,
    resource_id: str = Query(alias="id", min_length=1, max_length=200),
    user: User = Depends(_REQUIRE_TEAM_ADMIN),
) -> dict[str, Any]:
    """Agents and active scheduled tasks affected by a team-level disable (tech design §8.6); display only."""
    from bowhead_core.resource_disable_impact import disable_impact

    response.headers["Cache-Control"] = "private, no-store"
    impact = await disable_impact(_user_store(request).db_path, user.team_id, kind, resource_id)
    if impact is None:
        raise HTTPException(404, detail={"error": {"code": "resource_not_found"}})
    return impact
