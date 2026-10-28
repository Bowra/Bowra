"""Per-turn removal of disabled Plugins and Tools.

docs/design/capability-center-plugins-tech-design.md §8: disabling a Plugin removes only what that
Plugin contributed, while disabling a Tool removes it from every source. This module is pure: callers
supply where each Tool came from (provenance) and the disable state read from settings.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from bowhead_core.agents._generated.tool_kinds import TOOL_RISKS

logger = logging.getLogger(__name__)

DisabledBy = Literal["team", "user"]


@dataclass(frozen=True)
class PluginContribution:
    """What one Plugin added to an Agent for this turn."""

    kind: Literal["builtin", "team"]
    id: str
    tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    # A team Plugin that was not active when the run was admitted. Its contribution is removed even
    # when no per-turn disable state was loaded, so admitting a disabled Plugin never leaks it.
    inactive: bool = False


@dataclass(frozen=True)
class ResourceProvenance:
    """Where an Agent's Tools and Skills came from: selected directly or brought by Plugins."""

    direct_tools: tuple[str, ...] = ()
    direct_skills: tuple[str, ...] = ()
    plugins: tuple[PluginContribution, ...] = ()


@dataclass(frozen=True)
class DisableState:
    """Disable settings that apply to one user in one team."""

    team_builtin_plugins: frozenset[str] = frozenset()
    user_builtin_plugins: frozenset[str] = frozenset()
    inactive_team_plugins: frozenset[str] = frozenset()
    user_team_plugins: frozenset[str] = frozenset()
    # Built-in Tools whose team state is disabled, including default-off external_write Tools.
    team_tools: frozenset[str] = frozenset()

    def plugin_disabled_by(self, plugin: PluginContribution) -> DisabledBy | None:
        if plugin.kind == "builtin":
            if plugin.id in self.team_builtin_plugins:
                return "team"
            if plugin.id in self.user_builtin_plugins:
                return "user"
            return None
        if plugin.inactive or plugin.id in self.inactive_team_plugins:
            return "team"
        if plugin.id in self.user_team_plugins:
            return "user"
        return None


@dataclass(frozen=True)
class RemovedResource:
    kind: Literal["plugin", "tool"]
    id: str
    by: DisabledBy


@dataclass(frozen=True)
class TurnResources:
    tools: frozenset[str]
    skills: frozenset[str]
    removed: tuple[RemovedResource, ...] = field(default=())


# High-risk (external_write) built-in Tools start disabled until an administrator turns them on.
# Off: Lead can still send email and write to Google (user decision 2026-10-08, option A).
HIGH_RISK_DEFAULT_OFF = False


def high_risk_tools() -> frozenset[str]:
    return frozenset(name for name, risk in TOOL_RISKS.items() if risk == "external_write")


def default_disabled_tools() -> frozenset[str]:
    """Built-in Tools disabled for a team that has not set their state."""
    return high_risk_tools() if HIGH_RISK_DEFAULT_OFF else frozenset()


def team_disabled_tools(states: dict[str, dict[str, object]]) -> frozenset[str]:
    """Resolve `builtin_tool_states` against the defaults; unknown keys and bad values are ignored."""
    disabled = set(default_disabled_tools())
    for name, state in states.items():
        if name not in TOOL_RISKS or not isinstance(state, dict) or not isinstance(state.get("enabled"), bool):
            continue
        if state["enabled"]:
            disabled.discard(name)
        else:
            disabled.add(name)
    return frozenset(disabled)


def compute_turn_resources(provenance: ResourceProvenance, state: DisableState) -> TurnResources:
    """kept = direct ∪ contributions of enabled Plugins; turn = kept − team-disabled Tools."""
    tools = set(provenance.direct_tools)
    skills = set(provenance.direct_skills)
    removed: list[RemovedResource] = []
    for plugin in provenance.plugins:
        by = state.plugin_disabled_by(plugin)
        if by is None:
            tools.update(plugin.tools)
            skills.update(plugin.skills)
            continue
        removed.append(RemovedResource("plugin", plugin.id, by))
    # Report only Tools the Agent would otherwise have had; a disabled Plugin's Tools are covered by its entry.
    disabled_tools = {runtime_tool_name(name) for name in state.team_tools}
    blocked = {tool for tool in tools if runtime_tool_name(tool) in disabled_tools}
    tools -= blocked
    removed.extend(RemovedResource("tool", name, "team") for name in sorted(blocked))
    return TurnResources(tools=frozenset(tools), skills=frozenset(skills), removed=tuple(removed))


def attribute_system_tools(tools: Iterable[str]) -> ResourceProvenance:
    """System Agents keep their manifests; a Tool belonging to a general built-in Plugin counts as from it (§7)."""
    from bowhead_core.system_plugins import builtin_plugins

    latest: dict[str, tuple[int, tuple[str, ...]]] = {}
    for plugin in builtin_plugins():
        if plugin.family == "general" and (plugin.name not in latest or plugin.version > latest[plugin.name][0]):
            latest[plugin.name] = (plugin.version, plugin.tools)
    owner = {tool: name for name, (_version, members) in latest.items() for tool in members}
    direct: list[str] = []
    by_plugin: dict[str, list[str]] = {}
    for tool in dict.fromkeys(tools):
        name = owner.get(tool)
        if name is None:
            direct.append(tool)
        else:
            by_plugin.setdefault(name, []).append(tool)
    return ResourceProvenance(
        direct_tools=tuple(direct),
        plugins=tuple(PluginContribution("builtin", name, tuple(members)) for name, members in by_plugin.items()),
    )


KEY_TEAM_TOOL_STATES = "builtin_tool_states"
KEY_BUILTIN_PLUGINS_DISABLED = "builtin_plugins_disabled"
KEY_TEAM_PLUGINS_DISABLED = "team_plugins_disabled"


def _json_value(raw: str | None, expected: type, key: str) -> Any:
    if raw is None:
        return expected()
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("resource_disable.bad_setting key=%s", key)
        return expected()
    if not isinstance(value, expected):
        logger.warning("resource_disable.bad_setting key=%s", key)
        return expected()
    return value


def _names(raw: str | None, key: str) -> frozenset[str]:
    return frozenset(item for item in _json_value(raw, list, key) if isinstance(item, str) and item)


async def load_disable_state(db_path: str, team_id: str, user_id: str | None) -> DisableState:
    """Read the team's and the user's disable settings once per turn.

    A setting that cannot be read or parsed falls back to its default and logs a warning; the
    defaults keep high-risk built-in Tools disabled.
    """
    from bowhead_core._db import open_shared_ctx

    try:
        async with open_shared_ctx(db_path) as db:
            team: dict[str, str] = {}
            async with db.execute(
                "SELECT key, value FROM team_settings WHERE team_id=? AND key IN (?, ?)",
                (team_id, KEY_TEAM_TOOL_STATES, KEY_BUILTIN_PLUGINS_DISABLED),
            ) as cursor:
                team = {str(key): str(value) for key, value in await cursor.fetchall()}
            user: dict[str, str] = {}
            if user_id:
                async with db.execute(
                    "SELECT key, value FROM user_settings WHERE user_id=? AND key IN (?, ?)",
                    (user_id, KEY_BUILTIN_PLUGINS_DISABLED, KEY_TEAM_PLUGINS_DISABLED),
                ) as cursor:
                    user = {str(key): str(value) for key, value in await cursor.fetchall()}
            async with db.execute("SELECT id FROM team_capability WHERE team_id=? AND status!='active'", (team_id,)) as cursor:
                inactive = frozenset(str(row[0]) for row in await cursor.fetchall())
    except Exception as exc:  # noqa: BLE001 — a settings read must never fail the turn.
        logger.warning("resource_disable.load_failed team=%s err=%s", team_id, exc)
        return DisableState(team_tools=default_disabled_tools())
    return DisableState(
        team_builtin_plugins=_names(team.get(KEY_BUILTIN_PLUGINS_DISABLED), KEY_BUILTIN_PLUGINS_DISABLED),
        user_builtin_plugins=_names(user.get(KEY_BUILTIN_PLUGINS_DISABLED), KEY_BUILTIN_PLUGINS_DISABLED),
        inactive_team_plugins=inactive,
        user_team_plugins=_names(user.get(KEY_TEAM_PLUGINS_DISABLED), KEY_TEAM_PLUGINS_DISABLED),
        team_tools=team_disabled_tools(_json_value(team.get(KEY_TEAM_TOOL_STATES), dict, KEY_TEAM_TOOL_STATES)),
    )


def disabled_names(raw: str | None, key: str) -> frozenset[str]:
    """Parse a stored disable list (JSON array of names); invalid content reads as empty."""
    return _names(raw, key)


def with_name(raw: str | None, key: str, name: str, disabled: bool) -> str:
    """Return the stored disable list with ``name`` added or removed, as canonical JSON."""
    names = set(_names(raw, key))
    if disabled:
        names.add(name)
    else:
        names.discard(name)
    return json.dumps(sorted(names), ensure_ascii=False, separators=(",", ":"))


def tool_states(raw: str | None) -> dict[str, dict[str, bool]]:
    """Every user-visible built-in Tool's effective team state: ``enabled``.

    A stored ``always_confirm`` from before the setting was removed (owner decision 2026-10-21: once a
    user grants access, nothing asks again) is ignored.
    """
    from bowhead_core.agents._generated.tool_kinds import USER_VISIBLE_TOOLS

    stored = _json_value(raw, dict, KEY_TEAM_TOOL_STATES)
    disabled = team_disabled_tools(stored)
    states: dict[str, dict[str, bool]] = {}
    for name in sorted(USER_VISIBLE_TOOLS):
        states[name] = {"enabled": name not in disabled}
    return states


def with_tool_state(raw: str | None, name: str, *, enabled: bool) -> str:
    """Return the stored Tool states with one Tool's state replaced, as canonical JSON."""
    stored = {key: value for key, value in _json_value(raw, dict, KEY_TEAM_TOOL_STATES).items() if key in TOOL_RISKS}
    stored[name] = {"enabled": enabled}
    return json.dumps(stored, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _grant_ids(definition: dict[str, Any]) -> tuple[str, ...]:
    grants = definition.get("skill_grants")
    if not isinstance(grants, list):
        return ()
    return tuple(str(grant["skill_ref"]["id"]) for grant in grants if isinstance(grant, dict) and isinstance(grant.get("skill_ref"), dict))


async def team_plugin_provenance(db_path: str, authored: dict[str, Any], resolved: dict[str, Any]) -> ResourceProvenance | None:
    """Direct resources from the authored definition, each team Plugin's frozen revision and each built-in Plugin version.

    Returns None when the frozen revisions cannot be read; the runner then refuses the run with
    ``capability_unavailable`` so a disabled team Plugin can never leak.
    """
    from bowhead_core._db import open_shared_ctx

    tool_refs = authored.get("tool_refs")
    direct_tools = tuple(str(name) for name in tool_refs) if isinstance(tool_refs, list) else ()
    contributions: list[PluginContribution] = []
    try:
        async with open_shared_ctx(db_path) as db:
            for item in resolved.get("resolved_capabilities") or []:
                async with db.execute("SELECT status FROM team_capability WHERE id=?", (str(item["id"]),)) as cursor:
                    status_row = await cursor.fetchone()
                async with db.execute(
                    "SELECT kind,resource_ref FROM team_capability_resource WHERE capability_id=? AND revision=?",
                    (str(item["id"]), int(item["revision"])),
                ) as cursor:
                    rows = await cursor.fetchall()
                contributions.append(
                    PluginContribution(
                        "team",
                        str(item["id"]),
                        tools=tuple(str(ref) for kind, ref in rows if kind == "tool"),
                        skills=tuple(str(ref) for kind, ref in rows if kind != "tool"),
                        inactive=status_row is None or str(status_row[0]) != "active",
                    )
                )
        from bowhead_core.system_plugins import builtin_plugin

        for item in resolved.get("resolved_builtin_plugins") or []:
            plugin = builtin_plugin(f"builtin:{item['name']}@{int(item['version'])}")
            contributions.append(PluginContribution("builtin", plugin.name, tools=plugin.tools))
    except Exception as exc:  # noqa: BLE001 — provenance must never fail the run.
        logger.warning("resource_disable.team_provenance_failed err=%s", exc)
        return None
    return ResourceProvenance(direct_tools=direct_tools, direct_skills=_grant_ids(authored), plugins=tuple(contributions))


# Built-in Tools that used to be Lead-only or fenced from team Agents are open to them.
# User principle 2026-10-08: an Agent may change whatever the person using it may change.
# Email and Google writes (owner decision 2026-10-20): selecting them into a custom Agent is the
# Owner's grant, so its runs send or write without asking each time; Lead keeps its own in-turn
# confirmation (``email_send_confirmation``).
_OPENED_TO_TEAM_AGENTS = frozenset(
    {"remove_chart", "tool_list_workspaces", "send_email", "google_doc_write", "google_sheet_write", "google_slides_write"}
)


def team_agent_opened_tools() -> frozenset[str]:
    return _OPENED_TO_TEAM_AGENTS


def runtime_tool_name(name: str) -> str:
    """The name disabling compares: renamed Tools resolve to their current name, and ``python_exec``
    (the flat runtime entry of the sandbox) counts as the ``python_sandbox`` Tool users see."""
    from bowhead_core.agents._generated.tool_renames import canonical_tool_name

    current = canonical_tool_name(name)
    return "python_sandbox" if current == "python_exec" else current
