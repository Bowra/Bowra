"""Current administrator authority and dependency checks for shared Plugins."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bowhead_core.access_control import AccessControlStore
from bowhead_core.chat_reference_scope import read_reference_access_context_in_connection
from bowhead_core.coach_plugin_authoring import CoachPluginAuthoring
from fastapi import HTTPException

from bowhead_gateway._entitlement_gate import assert_not_readonly

if TYPE_CHECKING:
    import aiosqlite

    from bowhead_gateway.agent_coach_resources import AgentCoachResources


@dataclass(frozen=True)
class AgentCoachPlugins:
    resources: AgentCoachResources

    async def authorize(self, db: aiosqlite.Connection, write: bool) -> bool:
        resource = self.resources
        actor = await read_reference_access_context_in_connection(db, scope=resource.scope, access_store=AccessControlStore(resource.store.db_path))
        try:
            await assert_not_readonly(resource.request)
        except HTTPException:
            if write:
                raise
            return False
        if write and not actor.is_team_admin:
            raise ValueError("plugin_team_admin_required")
        return actor.is_team_admin

    async def validate_resources(self, candidate: dict, observed_tools: list[dict] | None) -> dict:
        from bowhead_gateway.agent_authoring_dependencies import compile_authoring_resources

        result = await compile_authoring_resources(self.resources, candidate, audience_mode="teamwide", observed_tools=observed_tools)
        # ``skill_grants`` carries the resolved (latest approved) revisions; the
        # authoring service stores those and removes the key from its result.
        return {key: value for key, value in result.items() if key != "effective_tool_refs"}

    @property
    def service(self) -> CoachPluginAuthoring:
        resource = self.resources
        return CoachPluginAuthoring(str(resource.store.db_path), resource.scope, self.authorize, self.validate_resources)

    async def call(self, operation: str, args: dict, *, turn_id: str) -> dict:
        service = self.service
        if "patch" in args:
            args = {**args, "patch": {key: value for key, value in args["patch"].items() if value is not None}}
        if operation == "agent_plugin_inspect":
            result = await service.inspect(**args)
        elif operation == "agent_plugin_validate":
            result = await service.validate(**args)
        elif operation == "agent_plugin_save":
            try:
                result = await service.save(**args, turn_id=turn_id)
            except sqlite3.IntegrityError as exc:
                if "team_capability.team_id, team_capability.name_key" in str(exc):
                    raise ValueError("plugin_name_conflict") from None
                raise
        elif operation == "agent_plugin_operation_get":
            result = await service.get(**args)
        else:
            raise ValueError("plugin_operation_unsupported")
        await self.resources._access()
        return result
