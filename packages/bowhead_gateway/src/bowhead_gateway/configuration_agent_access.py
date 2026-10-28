"""Invocation-time permissions for system Configuration Agent domain Tools."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from bowhead_core.access_control import normalize_workspace_role
from bowhead_core.agent_run_limits import agent_run_limits_enabled
from bowhead_core.configuration_contracts import TEAM_FORMS, domain_enabled
from bowhead_core.tool_registry import ToolExecutionContext
from fastapi import FastAPI

from bowhead_gateway._entitlement_gate import _requires_resolver
from bowhead_gateway.configuration_space_instructions import space_instructions_access
from bowhead_gateway.routes.workspace_settings import _check_workspace_access


def configuration_access_resolver(app: FastAPI):
    async def resolve(ctx: ToolExecutionContext, form_id: str, write: bool) -> dict[str, Any]:
        store = getattr(app.state, "user_store", None)
        user = await store.get_user(ctx.user_id) if store is not None else None
        if user is None or user.status != "active" or not ctx.team_id or not await store.user_has_active_team_membership(ctx.user_id, ctx.team_id):
            raise PermissionError("configuration actor unavailable")
        user = replace(user, team_id=ctx.team_id, active_team_id=ctx.team_id)
        access: dict[str, Any] = {"configuration_readonly": False, "workspace_role": None}
        if form_id in TEAM_FORMS:
            if not domain_enabled(form_id) or (form_id == "team_run_limits" and not agent_run_limits_enabled()):
                raise PermissionError("configuration domain unavailable")
            role = await store.get_team_role(ctx.user_id, ctx.team_id)
            if role not in {"owner", "team_admin"}:
                raise PermissionError("team administrator required")
            access.update(team_role=role, is_team_admin=True)
        elif form_id in {"workspace_i18n", "workspace_metadata"}:
            await _check_workspace_access(
                user=user, user_store=store, workspace_id=ctx.workspace_id, require_admin=write or form_id == "workspace_metadata"
            )
            access["workspace_role"] = normalize_workspace_role(await store.get_member_role(ctx.workspace_id, ctx.user_id))
        elif form_id == "space_instructions":
            # Same rule as the Space instructions page: members read, Space admins and Team managers save.
            can_manage, archived = await space_instructions_access(store, user, ctx.workspace_id, manage=write)
            access["workspace_role"] = normalize_workspace_role(await store.get_member_role(ctx.workspace_id, ctx.user_id))
            access["team_role"] = await store.get_team_role(ctx.user_id, ctx.team_id)
            access["space_instructions_editable"] = can_manage and not archived
            access["space_archived"] = archived

        async def check_entitlement() -> None:
            resolver = getattr(app.state, "entitlements", None)
            if resolver is None:
                if _requires_resolver(app.state):
                    raise PermissionError("configuration entitlement unavailable")
                return
            # Use the trusted execution Team, never a synthetic HTTP auth lookup.
            await resolver.assert_can_run_turn(ctx.team_id)

        if write:
            await check_entitlement()
        else:
            try:
                await check_entitlement()
            except Exception:
                access["configuration_readonly"] = True
        return access

    return resolve
