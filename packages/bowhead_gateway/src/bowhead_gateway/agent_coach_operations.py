"""One read projection over existing, independently authorized domain receipts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from bowhead_core.coach_operation_reference import OPERATION_REFERENCE

if TYPE_CHECKING:
    from bowhead_gateway.agent_coach_resources import AgentCoachResources


@dataclass(frozen=True)
class AgentCoachOperations:
    resources: AgentCoachResources

    async def get(self, reference: dict) -> dict:
        ref = OPERATION_REFERENCE.validate_python(reference).model_dump()
        resources = self.resources
        await resources._access()
        kind = ref["kind"]
        expires_at = None
        if kind == "skill_save":
            result = await resources.skills.call("agent_skill_operation_get", {"operation_id": ref["operation_id"], "recover": False}, turn_id="")
        elif kind == "plugin_save":
            result = await resources.plugins.call("agent_plugin_operation_get", {"operation_id": ref["operation_id"]}, turn_id="")
        elif kind == "tool_requirement":
            result = await resources.tool_handoff.call(
                "agent_tool_requirement_get", {key: value for key, value in ref.items() if key != "kind"}, turn_id=""
            )
        elif kind == "agent_change":
            result = await resources.authoring.get_change(agent_id=ref["agent_id"], proposal_id=ref["proposal_id"])
            if result["proposal"]["provenance"].get("workspace_id") != resources.workspace_id or result["proposal"]["base_working_copy_id"] is None:
                raise ValueError("coach_operation_unavailable")
        elif kind == "eval_cases":
            result = await resources.evaluations.get(agent_id=ref["agent_id"], proposal_id=ref["proposal_id"])
        elif kind == "ci_delegation":
            result = {
                "ok": True,
                "delegation": await resources.ci.get(
                    {"kind": "agent_ci_delegation", "agent_id": ref["agent_id"], "delegation_id": ref["delegation_id"]}
                ),
            }
        else:
            from bowhead_gateway.routes.team_subagent_drafts import _draft_store, _manual_draft_dto

            store = await _draft_store(resources.request)
            draft = await store.get_draft(ref["draft_id"], team_id=resources.user.team_id, owner_user_id=resources.user.id)
            if draft is None or draft.entry_mode != "canvas" or draft.workspace_id != resources.workspace_id or draft.status == "expired":
                raise ValueError("coach_operation_unavailable")
            expires_at = draft.expires_at
            if kind == "draft_suggestion":
                suggestion = await store.get_canvas_suggestion(
                    draft.id,
                    ref["suggestion_id"],
                    team_id=resources.user.team_id,
                    owner_user_id=resources.user.id,
                    workspace_id=resources.workspace_id,
                )
                if suggestion is None:
                    raise ValueError("coach_operation_unavailable")
                result = {"ok": True, "proposal": asdict(suggestion), "draft_status": draft.status, "applied_by_this_call": False}
                expires_at = suggestion.expires_at
            else:
                result = {"ok": True, "draft": _manual_draft_dto(draft, writable=False)}
        await resources._access()
        return {
            "ok": True,
            "schema_version": 1,
            "operation_ref": ref,
            "read_status": "available",
            "domain_result": result,
            "effects_applied_by_this_read": False,
            "retention": {"policy": "source_domain", "expires_at": expires_at},
            "recovery": {"mode": "read_receipt_only", "new_request_is_recovery": False},
        }
