"""Gateway 侧解析源装配（tech-design r3 §3.2）：与 turn 内共用 `hook.build_sources`，只多装团队工具 wire 名.

集合是团队域的；「防枚举」靠 findings 只给上传者本人看。任何一块装不出来只让对应规则不判，绝不让写失败。
"""

from __future__ import annotations

import logging
from typing import Any

from bowhead_core.skill_import.hook import build_sources
from bowhead_core.skill_import.inventory import ResolutionSources
from bowhead_core.subagent_authoring_catalog import SubagentAuthoringCatalogService
from fastapi import Request

from bowhead_gateway._entitlement_gate import is_feature_licensed_request

logger = logging.getLogger(__name__)


async def build_resolution_sources(request: Request, user: Any) -> ResolutionSources:
    state = request.app.state
    user_store = getattr(state, "user_store", None)
    team_id = getattr(user, "team_id", None)
    team_tool_names: frozenset[str] | None = None
    if user_store is not None and team_id:
        try:
            catalog = await SubagentAuthoringCatalogService().build(
                store=user_store,
                team_id=team_id,
                actor_user_id=user.id,
                actor_active=getattr(user, "status", "active") == "active",
                mcp_entitled=await is_feature_licensed_request(request, "mcp"),
            )
            team_tool_names = frozenset(t.stable_ref for t in catalog.team_tools)
        except Exception:  # noqa: BLE001 — No team/access/catalog: do not evaluate teammcp.* references.
            logger.info("skill_import.sources.team_tools_unavailable")
    return await build_sources(
        registry=getattr(state, "tool_registry", None),
        skill_catalog=getattr(state, "skill_catalog", None),
        personal_store=getattr(state, "skill_instance_store", None),
        user_id=getattr(user, "id", None),
        team_store=user_store,
        team_id=team_id,
        team_tool_names=team_tool_names,
    )
