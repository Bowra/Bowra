"""Scoped Tool requirements, administrator handoff and exact result lookup."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import aiosqlite
from bowhead_core.access_control import AccessControlStore
from bowhead_core.agent_change_proposal_store import _owned_working_conn, _require_owner_conn
from bowhead_core.chat_reference_scope import read_reference_access_context_in_connection
from bowhead_core.coach_operation_reference import with_operation_reference
from bowhead_core.coach_plugin_authoring import _target as plugin_target
from bowhead_core.coach_skill_authoring import _hash as package_hash
from bowhead_core.coach_tool_requirements import CoachToolRequirements
from bowhead_core.skill_bundle_lock import skill_bundle_lock
from bowhead_core.subagent_authoring_catalog import SubagentAuthoringCatalogService
from bowhead_core.subagent_authoring_rollout import resolve_subagent_authoring_capabilities
from bowhead_core.tool_creator_trigger import _scrub_spec_url, tool_creator_enabled

from bowhead_gateway._entitlement_gate import assert_not_readonly, is_feature_licensed_request

if TYPE_CHECKING:
    from bowhead_gateway.agent_coach_resources import AgentCoachResources


@dataclass(frozen=True)
class AgentCoachToolHandoff:
    resources: AgentCoachResources

    async def actor(self, db: aiosqlite.Connection):
        return await read_reference_access_context_in_connection(
            db, scope=self.resources.scope, access_store=AccessControlStore(self.resources.store.db_path)
        )

    async def parent(self, db: aiosqlite.Connection, target: dict | None, exact: bool) -> dict:
        resource = self.resources
        await self.actor(db)
        if exact:
            await assert_not_readonly(resource.request)
        if target is None:
            return {"baseline_matches": True, "current_target": None, "binding_available": False, "audience": None}
        db.row_factory = aiosqlite.Row
        available, binding_authorized, audience = True, True, None
        if target["kind"] == "agent_draft":
            cursor = await db.execute(
                "SELECT * FROM team_subagent_draft WHERE id=? AND team_id=? AND owner_user_id=? AND workspace_id=? AND entry_mode='canvas'",
                (target["id"], resource.user.team_id, resource.user.id, resource.workspace_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise ValueError("tool_requirement_parent_unavailable")
            current = {"kind": "agent_draft", "id": row["id"], "version": row["row_version"]}
            available = row["status"] in {"editing", "ready", "blocked"} and row["expires_at"] > datetime.now(UTC).isoformat()
            audience = {"effective_mode": "private"}
        elif target["kind"] == "agent_working_copy":
            await _require_owner_conn(db, team_id=resource.user.team_id, agent_id=target["agent_id"], actor_user_id=resource.user.id)
            row, _ = await _owned_working_conn(db, team_id=resource.user.team_id, agent_id=target["agent_id"], actor_user_id=resource.user.id)
            current = {
                "kind": "agent_working_copy",
                "agent_id": target["agent_id"],
                "working_copy_id": row["id"],
                "version": row["row_version"],
                "base_release_id": row["base_release_id"] or None,
            }
            audience = json.loads(row["audience_json"])
        elif target["kind"] == "personal_skill":
            service = resource.skills.service
            await resource.skills.authorize(db, target["name"], False)
            instance = await service._current(db, target["name"])
            if instance is None:
                raise ValueError("tool_requirement_parent_unavailable")
            path = service._path(target["name"], instance)
            with skill_bundle_lock(path):
                revision = package_hash(path)
            current = {"kind": "personal_skill", "name": target["name"], "id": instance.id, "revision": revision}
            available = revision is not None
            audience = {"effective_mode": "private"}
        elif target["kind"] == "team_plugin":
            binding_authorized = await resource.plugins.authorize(db, False)
            current = plugin_target(await resource.plugins.service._current(db, target["id"]))
            audience = {"effective_mode": "teamwide"}
        else:
            raise ValueError("tool_requirement_parent_unsupported")
        matches = current == target
        if exact and (not matches or not available):
            raise ValueError("tool_requirement_parent_changed")
        return {
            "baseline_matches": matches,
            "current_target": current,
            "binding_available": matches and available and binding_authorized,
            "audience": audience,
        }

    @property
    def store(self) -> CoachToolRequirements:
        return CoachToolRequirements(str(self.resources.store.db_path), self.resources.scope, self.parent)

    async def authorize_open(self, db: aiosqlite.Connection) -> None:
        actor = await self.actor(db)
        await assert_not_readonly(self.resources.request)
        if not actor.is_team_admin:
            raise ValueError("tool_onboarding_team_admin_required")
        if not tool_creator_enabled() or not await is_feature_licensed_request(self.resources.request, "mcp"):
            raise ValueError("tool_onboarding_unavailable")

    async def verify(self, db: aiosqlite.Connection, reference: dict) -> None:
        await self.actor(db)
        await assert_not_readonly(self.resources.request)
        result = await self.resources.get(reference=reference)
        detail = result.get("resource") or {}
        if reference["kind"] != "tool" or not result.get("ok") or not detail.get("can_select") or not isinstance(detail.get("input_schema"), dict):
            raise ValueError("tool_resource_unavailable_or_unselectable")

    async def project(self, result: dict) -> dict:
        resource = self.resources
        actor = await resource._access()
        requirement = result["requirement"]
        if requirement["workflow"] == "engineering":
            result["next_action"] = "engineering_delivery"
        elif not tool_creator_enabled() or not await is_feature_licensed_request(resource.request, "mcp"):
            result["next_action"] = "onboarding_unavailable"
        else:
            result["next_action"] = "open_authenticated_onboarding" if actor.is_team_admin else "team_admin_onboarding"
        observation = result.pop("observation", None)
        if observation:
            reference = json.loads(observation["reference_json"])
            current = await resource.get(reference=reference)
            detail = current.get("resource") or {}
            permitted = current.get("ok") and detail.get("can_select") and isinstance(detail.get("input_schema"), dict)
            result["observation"] = {
                "id": observation["id"],
                "observed_at": observation["observed_at"],
                "current_status": "available" if permitted else "unavailable_or_changed",
                "reference": reference if permitted else None,
                "creation_receipt": "not_available",
                "business_trial": "not_run",
            }
            if permitted:
                catalog = await SubagentAuthoringCatalogService().build(
                    store=resource.store,
                    team_id=resource.user.team_id,
                    actor_user_id=resource.user.id,
                    actor_active=True,
                    mcp_entitled=await is_feature_licensed_request(resource.request, "mcp"),
                )
                tool = catalog.tools_by_ref.get(reference["id"])
                result["observation"]["connection_id"] = tool.source_id if tool else None
                result["observation"]["input_schema"] = detail["input_schema"]
                result["binding_suggestion"] = {
                    "target": result["parent_status"]["current_target"],
                    "tool_ref": reference["id"],
                    "observed_revision": reference["revision"],
                    "ready_to_propose": result["parent_status"]["binding_available"],
                    "requires": ["requirement_semantic_review", "current_dependency_validation", "owner_acceptance"],
                    "revision_binding": "compiler_owned",
                }
        latest = await self.store.get(requirement_id=result["requirement_id"])
        result["parent_status"] = latest["parent_status"]
        if "binding_suggestion" in result:
            result["binding_suggestion"]["target"] = latest["parent_status"]["current_target"]
            parent = latest["parent_status"]["current_target"]
            capabilities = resolve_subagent_authoring_capabilities(resource.user.team_id)
            action = (
                "agent_authoring_propose"
                if parent and parent["kind"] == "agent_draft" and capabilities.unified_draft and capabilities.draft_chat_edit
                else None
            )
            if parent and parent["kind"] == "agent_working_copy":
                action = "agent_change_propose"
            if parent and parent["kind"] == "team_plugin":
                action = "agent_plugin_validate"
                result["binding_suggestion"]["requires"] = [
                    "requirement_semantic_review",
                    "current_plugin_inspection",
                    "shared_impact_review",
                    "current_dependency_validation",
                    "authorized_shared_save",
                ]
                result["binding_suggestion"]["binding_effect"] = "shared_plugin_selection"
            result["binding_suggestion"]["supported_binding_action"] = action
            result["binding_suggestion"]["ready_to_propose"] = bool(action and latest["parent_status"]["binding_available"])
        await resource._access()
        result = with_operation_reference("agent_tool_requirement_get", result)
        result["view_action"] = {"tool": "agent_operation_open", "arguments": {"reference": result["operation_ref"]}}
        return result

    async def call(self, operation: str, args: dict, *, turn_id: str) -> dict:
        if operation == "agent_tool_requirement_create":
            requirement = dict(args["requirement"])
            requirement["source_url_hint"] = _scrub_spec_url(requirement.get("source_url_hint") or "") or None
            result = await self.store.create(parent=args["parent"], requirement=requirement, request_id=args["request_id"], turn_id=turn_id)
        elif operation == "agent_tool_requirement_get":
            result = await self.store.get(**args)
        elif operation == "agent_tool_requirement_open":
            try:
                result = await self.store.request_open(**args, authorize_open=self.authorize_open)
            except ValueError as exc:
                if str(exc) != "tool_engineering_delivery_required":
                    raise
                # Reauthorize the original record before returning a read-only recovery pointer.
                receipt = await self.project(await self.store.get(**args))
                return {
                    "ok": False,
                    "error": "tool_engineering_delivery_required",
                    "operation_ref": receipt["operation_ref"],
                    "view_action": receipt["view_action"],
                }
        elif operation == "agent_tool_requirement_reconcile":
            result = await self.store.observe(**args, turn_id=turn_id, verify=self.verify)
        else:
            raise ValueError("tool_requirement_operation_unsupported")
        return await self.project(result)
