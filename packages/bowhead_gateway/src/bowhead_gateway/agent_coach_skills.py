"""Gateway-bound personal Skill authoring; no implicit Space or Team fallback."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bowhead_core.access_control import AccessControlStore
from bowhead_core.chat_reference_scope import read_reference_access_context_in_connection
from bowhead_core.coach_skill_authoring import CoachSkillAuthoring
from bowhead_core.resource_access import builtin_capability_ids_for_skill
from bowhead_core.skill_import.hook import apply_import_inventory

from bowhead_gateway._entitlement_gate import assert_not_readonly
from bowhead_gateway.routes.skills import _user_dir_for
from bowhead_gateway.skill_import_sources import build_resolution_sources

if TYPE_CHECKING:
    import aiosqlite

    from bowhead_gateway.agent_coach_resources import AgentCoachResources


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentCoachSkills:
    resources: AgentCoachResources

    async def authorize(self, db: aiosqlite.Connection, name: str, write: bool) -> None:
        resource = self.resources
        if write:
            await assert_not_readonly(resource.request)
        access = AccessControlStore(resource.store.db_path)
        actor = await read_reference_access_context_in_connection(db, scope=resource.scope, access_store=access)
        permission = "write" if write else "use"
        targets = [("builtin_capability", "skill_invoke", permission), ("skill", name, permission)]
        targets.extend(("builtin_capability", identity, "use") for identity in builtin_capability_ids_for_skill(name))
        for kind, identity, verb in targets:
            if not (await access.explain_decision(actor, kind, identity, verb, connection=db)).get("allowed"):
                raise ValueError("resource_grant_denied")

    @property
    def service(self) -> CoachSkillAuthoring:
        resource = self.resources
        return CoachSkillAuthoring(str(resource.store.db_path), _user_dir_for(resource.user.id), resource.scope, self.authorize)

    async def call(self, operation: str, args: dict, *, turn_id: str) -> dict:
        service = self.service
        if operation == "agent_skill_inspect":
            result = await service.inspect(**args)
        elif operation == "agent_skill_read":
            result = await service.read(**args)
        elif operation == "agent_skill_validate":
            result = await service.validate(**args)
        elif operation == "agent_skill_save":
            result = await service.save(**args, turn_id=turn_id)
        elif operation == "agent_skill_cancel":
            result = await service.cancel(**args)
        elif operation == "agent_skill_operation_get":
            result = await service.get(**args)
        else:
            raise ValueError("skill_operation_unsupported")
        await self.resources._access()
        if result.get("state") == "succeeded" and (operation in {"agent_skill_save", "agent_skill_cancel"} or args.get("recover")):
            target = result["result"]["target"]
            try:
                current = await service.inspect(name=target["name"], limit=1)
                if current["target"] == target and current["import_status"] == "pending":
                    instance = await service.instances.get(user_id=service.scope.user_id, name=target["name"])
                    sources = await build_resolution_sources(self.resources.request, self.resources.user)
                    inventory = await apply_import_inventory(
                        service.instances,
                        user_id=service.scope.user_id,
                        name=target["name"],
                        skill_dir=service._path(target["name"], instance),
                        sources=sources,
                        expected_content_hash=target["revision"],
                    )
                    result["import_inspection"] = "complete" if inventory is not None else "not_completed"
                else:
                    result["import_inspection"] = "read_current_target"
            except Exception:
                result["import_inspection"] = "not_completed"
        if result.get("state") == "succeeded":
            target = result["result"]["target"]
            # This is a current read beside the immutable historical save receipt.
            # A later edit/recreation must never supply findings for the saved bytes.
            result["current_inspection"] = {"status": "unavailable", "target": target}
            try:
                current = await service.inspect(name=target["name"], limit=1)
                if current["target"] == target:
                    result["current_inspection"] = {
                        "status": "read_current_target",
                        "target": target,
                        "enabled": current["enabled"],
                        "import_status": current["import_status"],
                        "import_review": current["import_review"],
                        "inspection_status": "complete" if current["import_review"]["report_recorded"] else "not_established",
                        "trial": "not_run",
                        "trigger_verification": "not_run",
                    }
                else:
                    result["current_inspection"]["status"] = "target_changed"
            except Exception:
                logger.warning("coach_skill.current_inspection_unavailable operation=%s", result["operation_id"])
        await self.resources._access()
        return result
