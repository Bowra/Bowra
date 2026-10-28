"""Current authorized editor projections over original resource identities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from bowhead_core.access_control import AccessControlStore
from bowhead_core.resource_access import builtin_capability_ids_for_skill
from fastapi import HTTPException, Request

from bowhead_gateway._entitlement_gate import assert_not_readonly

if TYPE_CHECKING:
    from bowhead_gateway.agent_coach_resources import AgentCoachResources


@dataclass(frozen=True)
class AgentCoachEditors:
    resources: AgentCoachResources

    async def read(self, reference: dict, parent: dict | None = None, *, exact: bool = False) -> dict:
        resource = self.resources
        actor = await resource._access()
        # Domain readers use the admitted Space, never a browser's unrelated
        # personal-settings override or a stale header from another editor.
        headers = [
            (key, value)
            for key, value in resource.request.scope.get("headers", [])
            if key.lower() not in {b"x-space-id", b"x-workspace-id", b"x-team-id"}
        ]
        headers.extend([(b"x-space-id", resource.workspace_id.encode()), (b"x-team-id", resource.user.team_id.encode())])
        request = Request({**resource.request.scope, "headers": headers, "query_string": b"", "state": dict(resource.request.scope.get("state", {}))})
        if reference["kind"] not in {"skill", "capability", "agent"}:
            raise ValueError("coach_resource_editor_unsupported")
        if parent is not None:
            if parent["kind"] == "agent_draft":
                await resource.authoring.draft_adapter(parent)
            else:
                from bowhead_core.agent_release_read_store import AgentReleaseReadStore

                value = await AgentReleaseReadStore(resource.store.db_path).working_copy(
                    team_id=resource.user.team_id, agent_id=parent["agent_id"], actor_user_id=resource.user.id
                )
                if (value.get("working_copy") or {}).get("id") != parent["working_copy_id"]:
                    raise ValueError("coach_resource_parent_unavailable")
        # Opening verifies a captured version; returning to an existing editor reads
        # the same current identity. The original editor owns its saved baseline.
        current = {key: value for key, value in reference.items() if value is not None}
        writable = True
        try:
            await assert_not_readonly(request)
        except HTTPException:
            writable = False
        if current["kind"] == "agent":
            items, _ = await resource._items("agent")
            item = next((item for item in items if item["reference"]["id"] == current["id"]), None)
            if item is None or exact and item["reference"] != current:
                raise ValueError("coach_resource_unavailable_or_changed")
            result = await resource.get(reference=item["reference"])
            if not result.get("ok") or result["resource"].get("working_copy") is None:
                raise ValueError("coach_resource_unavailable_or_changed")
            detail = result["resource"]["working_copy"]
            return {
                "ok": True,
                "schema_version": 1,
                "reference": item["reference"],
                "parent": parent,
                "editor_kind": "agent",
                "title": item["name"],
                "target": {"kind": "agent", "agent_id": current["id"], "detail": detail},
                "read_only": not writable or not detail["actions"]["edit"],
            }
        if current["kind"] == "capability":
            from bowhead_gateway.routes.team_plugins import get_plugin

            value = await get_plugin(current["id"], resource.user, resource.store)
            if exact and str(value["revision"]) != current["revision"]:
                raise ValueError("coach_resource_unavailable_or_changed")
            actor = await resource._access()
            return {
                "ok": True,
                "schema_version": 1,
                "reference": {**current, "revision": str(value["revision"])},
                "parent": parent,
                "editor_kind": "plugin",
                "title": value["name"],
                "target": {"kind": "plugin", "plugin": value},
                "read_only": not writable or not actor.is_team_admin,
            }
        if current["source_kind"] == "team":
            from bowhead_gateway.routes.team_skills import _assert_content_editable, _visible_or_404, get_team_skill_route

            skill, is_admin = await _visible_or_404(request, resource.store, resource.user, current["id"])
            required = [("team_skill", skill.id, "use")]
            required.extend(("builtin_capability", identity, "use") for identity in builtin_capability_ids_for_skill(skill.name))
            if not await resource._allowed(actor, required, owned_team_skill=skill.created_by == resource.user.id):
                raise ValueError("coach_resource_unavailable_or_changed")
            editable = writable
            try:
                await _assert_content_editable(resource.store, resource.user, skill, is_admin=is_admin)
            except HTTPException:
                editable = False
            value = await get_team_skill_route(current["id"], request, resource.user, resource.store)
            target = {
                "kind": "team",
                "skill": value,
                "canManage": actor.is_team_admin or value["created_by"] == resource.user.id,
                "canEdit": editable,
            }
        else:
            from bowhead_gateway.routes.skills import _require_skill_capability_for_request, _require_skill_grant_for_request, get_skill

            instances = resource.request.app.state.skill_instance_store
            if current["source_kind"] == "user":
                instance = await instances.get_by_id(user_id=resource.user.id, skill_id=current["id"])
                if instance is None or instance.source_type == "builtin":
                    raise ValueError("coach_resource_unavailable_or_changed")
                name = instance.name
                access = AccessControlStore(resource.store.db_path)
                required = [("skill", name), ("builtin_capability", "skill_invoke")]
                required.extend(("builtin_capability", identity) for identity in builtin_capability_ids_for_skill(name))
                for kind, identity in required:
                    if not (await access.explain_decision(actor, kind, identity, "use")).get("allowed"):
                        raise ValueError("coach_resource_unavailable_or_changed")
                for kind, identity in [("skill", name), ("builtin_capability", "skill_invoke")]:
                    if not (await access.explain_decision(actor, kind, identity, "write")).get("allowed"):
                        writable = False
            else:
                result = await resource.get(reference=current)
                if not result.get("ok"):
                    raise ValueError("coach_resource_unavailable_or_changed")
                name = result["resource"]["skill_name"]
            value = (await get_skill(name, request, resource.user, instances)).model_dump()
            if (
                current["source_kind"] == "user"
                and value.get("id") != current["id"]
                or current["source_kind"] == "builtin"
                and value["source_type"] != "builtin"
            ):
                raise ValueError("coach_resource_unavailable_or_changed")
            editable = writable and current["source_kind"] == "user"
            if editable:
                try:
                    await _require_skill_grant_for_request(request, resource.user, name, permission="write")
                    await _require_skill_capability_for_request(request, resource.user, permission="write")
                except HTTPException:
                    editable = False
            target = {"kind": "skill", "skill": value}
        if current["source_kind"] != "builtin":
            digest = value.get("content_sha256")
            if not digest:
                raise ValueError("coach_resource_unavailable_or_changed")
            current = {**current, "revision": "sha256:" + digest}
            if exact and current["revision"] != reference["revision"]:
                raise ValueError("coach_resource_unavailable_or_changed")
        await resource._access()
        return {
            "ok": True,
            "schema_version": 1,
            "reference": current,
            "parent": parent,
            "editor_kind": "skill",
            "title": value.get("display_name") or value.get("title") or value["name"],
            "target": target,
            "read_only": not editable,
        }
