"""Member-safe discovery metadata, independent from authoring and administration."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import anyio
from bowhead_core.agents._generated.tool_kinds import TOOL_KINDS
from bowhead_core.agents.registry import AgentRegistry
from bowhead_core.subagent_authoring_catalog import CatalogAccessDenied, SubagentAuthoringCatalogService
from bowhead_core.system_plugins import builtin_plugin
from bowhead_core.team_plugin_store import TeamPluginStore
from bowhead_core.user_store import User, UserStore
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from bowhead_gateway._entitlement_gate import is_feature_licensed_request
from bowhead_gateway.routes.auth import _current_user, _store

router = APIRouter()

_TOOL_FIELDS = (
    "stable_ref",
    "display_name",
    "description",
    "display_name_en",
    "description_en",
    "display_name_de",
    "description_de",
    "display_name_fr",
    "description_fr",
    "source_kind",
    "source_display_name",
    "risk",
    "selectable",
    "locked_reason",
)


@lru_cache(maxsize=1)
def _configuration_plugin_refs() -> tuple[str, ...]:
    """Cache code-owned manifest references, never deployment or team state."""
    return tuple(AgentRegistry.bootstrap().get("configuration-agent").plugins)


def _enabled_builtin_plugins() -> list[dict[str, Any]]:
    """Describe only the Plugin revisions used by the enabled system settings Agent."""
    plugins = [builtin_plugin(ref) for ref in _configuration_plugin_refs()]
    return [{"id": plugin.ref, "name": plugin.name, "tool_count": len(plugin.resources), "skill_count": 1} for plugin in plugins]


@router.get("")
async def get_catalog(
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """Use authenticated scope and explicit fields; never return source configuration or exact grants."""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        catalog = await SubagentAuthoringCatalogService().build(
            store=store,
            team_id=user.team_id,
            actor_user_id=user.id,
            actor_active=user.status == "active",
            mcp_entitled=await is_feature_licensed_request(request, "mcp"),
        )
    except CatalogAccessDenied as exc:
        raise HTTPException(403, detail="team_membership_required") from exc
    tools = []
    for tool in (*catalog.native_tools, *catalog.team_tools):
        if not tool.discoverable:
            continue
        # System parts (agent_*, configuration_*, skill_* ...) are internal and never listed as Tools.
        if tool.source_kind == "native" and TOOL_KINDS.get(tool.stable_ref) == "part":
            continue
        public = tool.to_public_dict()
        tools.append({key: public[key] for key in _TOOL_FIELDS})
    plugins = await TeamPluginStore(store.db_path).list(user.team_id, include_disabled=False)
    return {
        "tools": tools,
        "builtin_plugins": await anyio.to_thread.run_sync(_enabled_builtin_plugins),
        "capabilities": [
            {
                "id": item["id"],
                "name": item["name"],
                "description": item["description"],
                "tool_count": len(item["tool_refs"]),
                "skill_count": len(item["skill_grants"]),
            }
            for item in plugins
            if item["status"] == "active"
        ],
    }
