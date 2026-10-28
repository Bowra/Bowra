"""Plugin section of the Agent authoring catalog (Capability Center tech design §6).

Pure projection: callers supply the team's unconfigured Tools, disable settings and team Plugins.
Display and selection guidance only; publication and run admission still validate on their own.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

# Readiness the shared resolver cannot check yet (Datarc connection, sandbox, email service) is
# reported as ready; publication and runs still fail closed on their own.


def _readiness(tools: Iterable[str], unconfigured: frozenset[str]) -> tuple[bool, list[str]]:
    members = list(tools)
    missing = sorted(tool for tool in members if tool in unconfigured)
    return (not members or len(missing) < len(members)), missing


def builtin_plugin_entries(*, unconfigured: frozenset[str], team_disabled: frozenset[str], my_disabled: frozenset[str]) -> list[dict[str, Any]]:
    from bowhead_core.system_plugins import builtin_plugins

    latest: dict[str, Any] = {}
    for plugin in builtin_plugins():
        if plugin.name not in latest or plugin.version > latest[plugin.name].version:
            latest[plugin.name] = plugin
    entries = []
    for plugin in latest.values():
        ready, unavailable = _readiness(plugin.tools, unconfigured)
        if plugin.family == "settings" or not plugin.selectable:
            locked_reason = "settings_assistant_only"
        elif plugin.name in team_disabled:
            locked_reason = "disabled_by_team"
        elif plugin.name in my_disabled:
            locked_reason = "disabled_by_me"
        elif not ready:
            locked_reason = "not_ready"
        else:
            locked_reason = None
        entries.append(
            {
                "id": f"builtin:{plugin.name}",
                "name": plugin.name,
                "version": plugin.version,
                "family": plugin.family,
                "listing": {locale: item.model_dump() for locale, item in (plugin.listing or {}).items()},
                "tools": list(plugin.tools),
                "prompt_modules": list(plugin.prompt_modules),
                "ready": ready,
                "unavailable_tools": unavailable,
                "selectable": locked_reason is None,
                "locked_reason": locked_reason,
                "team_disabled": plugin.name in team_disabled,
                "my_disabled": plugin.name in my_disabled,
            }
        )
    return entries


def team_plugin_entries(plugins: Iterable[Mapping[str, Any]], *, unconfigured: frozenset[str], my_disabled: frozenset[str]) -> list[dict[str, Any]]:
    entries = []
    for item in plugins:
        tools = [str(ref) for ref in item.get("tool_refs") or []]
        ready, unavailable = _readiness(tools, unconfigured)
        active = item.get("status") == "active"
        if not active:
            locked_reason = "disabled_by_team"
        elif item["id"] in my_disabled:
            locked_reason = "disabled_by_me"
        elif not ready:
            locked_reason = "not_ready"
        else:
            locked_reason = None
        entries.append(
            {
                "id": item["id"],
                "name": item.get("name", ""),
                "description": item.get("description", ""),
                "version": item.get("revision"),
                "tools": tools,
                "skill_count": len(item.get("skill_grants") or []),
                "ready": ready,
                "unavailable_tools": unavailable,
                "selectable": locked_reason is None,
                "locked_reason": locked_reason,
                "team_disabled": not active,
                "my_disabled": item["id"] in my_disabled,
            }
        )
    return entries
