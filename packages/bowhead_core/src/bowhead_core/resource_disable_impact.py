"""Impact preview before disabling a built-in Plugin, team Plugin or built-in Tool.

Capability Center tech design §8.6: which Agents (system and team) and active scheduled tasks would
lose something. Display only; never used as an authorization decision.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

from bowhead_core.resource_disable import runtime_tool_name

ImpactKind = Literal["builtin_plugin", "team_plugin", "builtin_tool"]


@dataclass(frozen=True)
class _SystemAgent:
    name: str
    tools: frozenset[str]
    plugins: frozenset[str]


@lru_cache(maxsize=1)
def _system_agents() -> tuple[_SystemAgent, ...]:
    from bowhead_core.agents.registry import AgentRegistry

    registry = AgentRegistry.bootstrap()
    agents = []
    for name in registry.list_all():
        manifest = registry.get(name)
        plugins = frozenset(ref.removeprefix("builtin:").split("@", 1)[0] for ref in manifest.plugins)
        allowed = set(manifest.tools.allow)
        if name == "lead":
            # Lead's per-run deny list removes these even though the manifest allows them.
            from bowhead_core.agents.lead_orchestrator import _LEAD_TOOL_DENY

            allowed -= _LEAD_TOOL_DENY
        # Compare the names a run compares (renames; python_exec counts as the python_sandbox Tool).
        tools = frozenset(runtime_tool_name(tool) for tool in allowed)
        agents.append(_SystemAgent(name=name, tools=tools, plugins=plugins))
    return tuple(agents)


def _builtin_plugin_tools(name: str) -> tuple[frozenset[str], str] | None:
    from bowhead_core.system_plugins import builtin_plugins

    versions = [plugin for plugin in builtin_plugins() if plugin.name == name]
    if not versions:
        return None
    latest = max(versions, key=lambda plugin: plugin.version)
    return frozenset(runtime_tool_name(tool) for tool in latest.tools), latest.family


def _affected_system_agents(kind: ImpactKind, ident: str) -> list[str] | None:
    if kind == "team_plugin":
        return []
    if kind == "builtin_tool":
        return [agent.name for agent in _system_agents() if ident in agent.tools]
    found = _builtin_plugin_tools(ident)
    if found is None:
        return None
    tools, family = found
    if family == "settings":
        return [agent.name for agent in _system_agents() if ident in agent.plugins]
    # Option C (§7): a system Agent's Tool that belongs to the Plugin counts as coming from it.
    return [agent.name for agent in _system_agents() if agent.tools & tools]


def _selected_builtin_names(definition: dict[str, Any]) -> set[str]:
    """Built-in Plugin names a team Agent selected (``builtin_plugin_ids``); malformed values are ignored."""
    value = definition.get("builtin_plugin_ids")
    if not isinstance(value, list):
        return set()
    return {item.removeprefix("builtin:") for item in value if isinstance(item, str) and item.startswith("builtin:")}


async def _team_plugin_tools(db: Any, team_id: str, plugin_ids: set[str]) -> dict[str, set[str]]:
    contents: dict[str, set[str]] = {}
    for plugin_id in plugin_ids:
        async with db.execute(
            """SELECT resource.resource_ref FROM team_capability capability
               JOIN team_capability_resource resource
                 ON resource.capability_id=capability.id AND resource.revision=capability.current_revision
               WHERE capability.team_id=? AND capability.id=? AND resource.kind='tool'""",
            (team_id, plugin_id),
        ) as cursor:
            contents[plugin_id] = {runtime_tool_name(str(row[0])) for row in await cursor.fetchall()}
    return contents


async def disable_impact(db_path: str, team_id: str, kind: ImpactKind, ident: str) -> dict[str, Any] | None:
    """Agents and active scheduled tasks affected by disabling one resource; None if it does not exist."""
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.agents._generated.tool_kinds import USER_VISIBLE_TOOLS
    from bowhead_core.team_plugin_store import selected_plugin_ids

    if kind == "builtin_tool" and ident not in USER_VISIBLE_TOOLS:
        return None
    if kind == "builtin_tool":
        ident = runtime_tool_name(ident)
    system_agents = _affected_system_agents(kind, ident)
    if system_agents is None:
        return None
    async with open_shared_ctx(db_path) as db:
        if kind == "team_plugin":
            async with db.execute("SELECT 1 FROM team_capability WHERE team_id=? AND id=?", (team_id, ident)) as cursor:
                if await cursor.fetchone() is None:
                    return None
        async with db.execute(
            """SELECT agent.id, agent.name, agent.display_name, revision.definition_json
               FROM team_subagent agent
               JOIN team_subagent_revision revision ON revision.id=agent.live_revision_id AND revision.team_id=agent.team_id
               WHERE agent.team_id=? AND agent.status='enabled'
               ORDER BY agent.display_name, agent.name""",
            (team_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        live: list[tuple[str, str, str, dict[str, Any]]] = []
        for agent_id, name, display_name, definition_json in rows:
            try:
                definition = json.loads(definition_json)
            except (TypeError, ValueError):
                continue
            if isinstance(definition, dict):
                live.append((str(agent_id), str(name), str(display_name or name), definition))
        plugin_tools: dict[str, set[str]] = {}
        if kind == "builtin_tool":
            wanted = {plugin_id for *_rest, definition in live for plugin_id in selected_plugin_ids(definition)}
            plugin_tools = await _team_plugin_tools(db, team_id, wanted)
        team_agents = []
        for agent_id, name, display_name, definition in live:
            plugins = selected_plugin_ids(definition)
            builtin_selected = _selected_builtin_names(definition)
            if kind == "team_plugin":
                hit = ident in plugins
            elif kind == "builtin_tool":
                direct = {runtime_tool_name(str(tool)) for tool in definition.get("tool_refs") or [] if isinstance(tool, str)}
                hit = (
                    ident in direct
                    or any(ident in plugin_tools.get(plugin_id, set()) for plugin_id in plugins)
                    or any(ident in (_builtin_plugin_tools(selected) or (frozenset(), ""))[0] for selected in builtin_selected)
                )
            else:
                # Team Agents that selected the built-in Plugin lose what it brought; Tools they also
                # selected directly survive, but the Agent still counts as affected.
                hit = ident in builtin_selected
            if hit:
                team_agents.append({"id": agent_id, "name": name, "display_name": display_name})
        agent_ids = [f"system:{name}" for name in system_agents] + [item["id"] for item in team_agents]
        schedules: list[dict[str, str]] = []
        if agent_ids:
            placeholders = ",".join("?" for _ in agent_ids)
            async with db.execute(
                f"""SELECT job.id, job.name, binding.agent_id
                    FROM scheduled_job_agent_binding binding
                    JOIN scheduled_jobs job ON job.id=binding.job_id
                    JOIN workspaces workspace ON workspace.id=job.workspace_id
                    WHERE workspace.team_id=? AND binding.state='ready' AND job.status='active'
                      AND binding.agent_id IN ({placeholders})
                    ORDER BY job.name, job.id""",  # noqa: S608 -- placeholders only
                (team_id, *agent_ids),
            ) as cursor:
                schedules = [{"id": str(row[0]), "name": str(row[1]), "agent_id": str(row[2])} for row in await cursor.fetchall()]
    return {
        "kind": kind,
        "id": ident,
        "system_agents": [{"name": name} for name in system_agents],
        "team_agents": team_agents,
        "scheduled_tasks": schedules,
    }
