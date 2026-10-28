"""Read-only summaries of the resources an Agent definition uses (Capability Center brief §4.11–4.12).

Used by the release review change view (which Plugins, Tools and Skills were added or removed) and by
market details (which Plugins and Tools the Agent uses, and why the viewer cannot use some of them).
Display only; never an authorization decision.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from typing import Any

import aiosqlite


def _tool_refs(definition: Mapping[str, Any]) -> list[str]:
    value = definition.get("tool_refs")
    return [str(item) for item in value] if isinstance(value, list) else []


def _skill_names(definition: Mapping[str, Any]) -> list[str]:
    grants = definition.get("skill_grants")
    if not isinstance(grants, list):
        return []
    names = []
    for grant in grants:
        ref = grant.get("skill_ref") if isinstance(grant, dict) else None
        if isinstance(ref, dict) and ref.get("name"):
            names.append(str(ref["name"]))
    return names


def _changes(before: list[str], after: list[str]) -> dict[str, list[str]]:
    return {"added": sorted(set(after) - set(before)), "removed": sorted(set(before) - set(after))}


def tool_and_skill_changes(base: Mapping[str, Any], definition: Mapping[str, Any]) -> dict[str, dict[str, list[str]]]:
    return {
        "tool_changes": _changes(_tool_refs(base), _tool_refs(definition)),
        "skill_changes": _changes(_skill_names(base), _skill_names(definition)),
    }


def _builtin_item(ref: str) -> dict[str, Any]:
    from bowhead_core.system_plugins import latest_builtin_plugin

    name = ref.removeprefix("builtin:")
    plugin = latest_builtin_plugin(name)
    if plugin is None:
        return {"kind": "builtin", "id": ref, "name": name, "listing": {}, "version": None, "tools": []}
    return {
        "kind": "builtin",
        "id": ref,
        "name": name,
        "listing": {locale: item.model_dump() for locale, item in (plugin.listing or {}).items()},
        "version": plugin.version,
        "tools": list(plugin.tools),
    }


async def _team_item(db: aiosqlite.Connection, team_id: str, plugin_id: str) -> dict[str, Any]:
    async with db.execute(
        """SELECT c.current_revision, c.status, r.name FROM team_capability c
           JOIN team_capability_revision r ON r.capability_id=c.id AND r.revision=c.current_revision
           WHERE c.team_id=? AND c.id=?""",
        (team_id, plugin_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return {"kind": "team", "id": plugin_id, "name": plugin_id, "listing": {}, "version": None, "tools": [], "status": "deleted"}
    async with db.execute(
        "SELECT resource_ref FROM team_capability_resource WHERE capability_id=? AND revision=? AND kind='tool' ORDER BY resource_ref",
        (plugin_id, int(row[0])),
    ) as cursor:
        tools = [str(item[0]) for item in await cursor.fetchall()]
    return {"kind": "team", "id": plugin_id, "name": str(row[2]), "listing": {}, "version": int(row[0]), "tools": tools, "status": str(row[1])}


async def plugin_items(db: aiosqlite.Connection, team_id: str, refs: list[str]) -> list[dict[str, Any]]:
    """Display data for selected Plugin references (``builtin:<name>`` or team ``tcap_*`` ids)."""
    items = []
    for ref in refs:
        items.append(_builtin_item(ref) if ref.startswith("builtin:") else await _team_item(db, team_id, ref))
    return items


async def plugin_change_items(db: aiosqlite.Connection, team_id: str, changes: Mapping[str, list[str]]) -> list[dict[str, Any]]:
    added = [{**item, "change": "added"} for item in await plugin_items(db, team_id, list(changes.get("added") or []))]
    removed = [{**item, "change": "removed"} for item in await plugin_items(db, team_id, list(changes.get("removed") or []))]
    return added + removed


async def agent_resources(db: aiosqlite.Connection, *, team_id: str, user_id: str, definition: Mapping[str, Any]) -> dict[str, Any]:
    """Plugins and Tools a team Agent's live definition uses, with why the viewer cannot use some of them."""
    from bowhead_core.resource_disable import (
        KEY_BUILTIN_PLUGINS_DISABLED,
        KEY_TEAM_PLUGINS_DISABLED,
        KEY_TEAM_TOOL_STATES,
        disabled_names,
        tool_states,
    )

    async def _setting(table: str, owner_column: str, owner: str, key: str) -> str | None:
        async with db.execute(f"SELECT value FROM {table} WHERE {owner_column}=? AND key=?", (owner, key)) as cursor:  # noqa: S608 -- fixed names
            row = await cursor.fetchone()
        return str(row[0]) if row else None

    team_builtin_off = disabled_names(await _setting("team_settings", "team_id", team_id, KEY_BUILTIN_PLUGINS_DISABLED), KEY_BUILTIN_PLUGINS_DISABLED)
    my_builtin_off = disabled_names(await _setting("user_settings", "user_id", user_id, KEY_BUILTIN_PLUGINS_DISABLED), KEY_BUILTIN_PLUGINS_DISABLED)
    my_team_off = disabled_names(await _setting("user_settings", "user_id", user_id, KEY_TEAM_PLUGINS_DISABLED), KEY_TEAM_PLUGINS_DISABLED)
    states = tool_states(await _setting("team_settings", "team_id", team_id, KEY_TEAM_TOOL_STATES))

    refs = [*(definition.get("builtin_plugin_ids") or []), *(definition.get("capability_ids") or [])]
    plugins = []
    for item in await plugin_items(db, team_id, [str(ref) for ref in refs]):
        if item["kind"] == "builtin":
            reason = "disabled_by_team" if item["name"] in team_builtin_off else "disabled_by_me" if item["name"] in my_builtin_off else None
        else:
            reason = "disabled_by_team" if item.get("status") != "active" else "disabled_by_me" if item["id"] in my_team_off else None
        plugins.append({**item, "unavailable_reason": reason})
    tools = []
    for ref in _tool_refs(definition):
        state = states.get(ref)
        tools.append({"ref": ref, "unavailable_reason": "disabled_by_team" if state is not None and not state["enabled"] else None})
    return {"plugins": plugins, "tools": tools}


@lru_cache(maxsize=1)
def _system_registry() -> Any:
    """System manifests are static; parse them once per process instead of on every details request."""
    from bowhead_core.agents.registry import AgentRegistry

    return AgentRegistry.bootstrap()


def system_agent_definition(name: str) -> dict[str, Any] | None:
    """A definition-like view of a system Agent for ``agent_resources``.

    System manifests list Tools directly; a Tool that belongs to a general built-in Plugin counts as
    coming from it (Capability Center tech design §7, option C). Internal parts are left out.
    """
    from bowhead_core.agents._generated.tool_kinds import USER_VISIBLE_TOOLS
    from bowhead_core.resource_disable import attribute_system_tools, runtime_tool_name

    try:
        manifest = _system_registry().get(name)
    except (KeyError, ValueError):
        return None
    provenance = attribute_system_tools(runtime_tool_name(tool) for tool in manifest.tools.allow)
    return {
        "builtin_plugin_ids": [f"builtin:{plugin.id}" for plugin in provenance.plugins],
        "tool_refs": [tool for tool in provenance.direct_tools if tool in USER_VISIBLE_TOOLS],
    }
