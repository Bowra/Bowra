"""Owner-scoped, one-version editor for a draft explicitly attached to Chat."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from bowhead_core.subagent_authoring_rollout import resolve_subagent_authoring_capabilities
from bowhead_core.subagent_draft_store import SubagentDraft, SubagentDraftVersionConflictError
from bowhead_core.user_store import User, UserStore
from fastapi import HTTPException, Request
from pydantic import ValidationError

from bowhead_gateway._entitlement_gate import assert_not_readonly
from bowhead_gateway.routes.team_subagent_drafts import (
    DraftDefinitionBody,
    ManualDefinitionBody,
    PatchDraftBody,
    _draft_store,
    _owned_draft,
    _review_update,
)

_EDITABLE_FIELDS = frozenset(
    {
        "name",
        "display_name",
        "description",
        "system_prompt",
        "model",
        "output_schema",
        "selected_tool_refs",
        "skill_grants",
        "capability_ids",
        "builtin_plugin_ids",
    }
)
_APPENDABLE_FIELDS = frozenset({"name", "display_name", "description", "system_prompt"})


if TYPE_CHECKING:
    from bowhead_core.agent_coach import CoachDraftAdapter


@dataclass(slots=True)
class SubagentDraftChatEditor:
    request: Request
    user: User
    draft_id: str
    expected_version: int
    has_unsaved_local_changes: bool = False
    workspace_id: str | None = None

    def coach_adapter(self) -> CoachDraftAdapter:
        from bowhead_gateway.agent_coach_draft_adapter import AgentCoachDraftAdapter

        return AgentCoachDraftAdapter(replace(self))

    async def _current(self, *, check_version: bool = True) -> SubagentDraft:
        if not resolve_subagent_authoring_capabilities(self.user.team_id).draft_chat_edit:
            raise ValueError("draft_chat_edit_unavailable")
        draft = await _owned_draft(self.request, self.user, self.draft_id)
        if check_version and draft.row_version != self.expected_version:
            raise SubagentDraftVersionConflictError(draft.row_version)
        if draft.entry_mode == "canvas":
            if not resolve_subagent_authoring_capabilities(self.user.team_id).unified_draft:
                raise ValueError("draft_not_editable")
            if draft.status not in {"editing", "ready", "blocked"} or draft.workspace_id != self.workspace_id:
                raise ValueError("draft_not_editable")
            store = getattr(self.request.app.state, "user_store", None)
            if not isinstance(store, UserStore):
                raise ValueError("draft_space_unavailable")
            memberships = await store.list_workspaces_for_user(self.user.id, team_id=self.user.team_id)
            if not any(row.get("workspace_id") == self.workspace_id for row in memberships):
                raise ValueError("draft_space_unavailable")
        elif draft.entry_mode != "conversational" or draft.status not in {"ready", "blocked"} or not draft.definition:
            raise ValueError("draft_not_editable")
        return draft

    async def inspect(self) -> dict[str, Any]:
        if self.has_unsaved_local_changes:
            return {"ok": False, "message": "右侧有未保存修改。请先保存或放弃右侧修改，再继续对话编辑。"}
        try:
            draft = await self._current()
        except (HTTPException, SubagentDraftVersionConflictError, ValueError):
            return {"ok": False, "outcome": "draft_unavailable_or_changed"}
        return {
            "ok": True,
            "outcome": "draft_inspected",
            "draft_id": draft.id,
            "version": draft.row_version,
            "definition": draft.definition,
            "available_tools": [
                {
                    "tool_ref": item.get("tool_ref"),
                    "display_name": item.get("display_name"),
                    "selected": item.get("selected"),
                    "locked_reason": item.get("locked_reason"),
                }
                for item in draft.recommendations
            ],
        }

    async def edit(
        self,
        edits: dict[str, Any],
        text_operations: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if self.has_unsaved_local_changes:
            return {
                "ok": False,
                "applied": False,
                "message": "右侧有未保存修改。请先保存或放弃右侧修改，再继续对话编辑。",
            }
        operations = text_operations or []
        if (not edits and not operations) or set(edits) - _EDITABLE_FIELDS or len(operations) > 8:
            return {"ok": False, "applied": False, "outcome": "invalid_fields"}
        operation_fields: set[str] = set()
        for operation in operations:
            if (
                not isinstance(operation, dict)
                or operation.get("operation") != "append"
                or not isinstance(operation.get("field"), str)
                or operation.get("field") not in _APPENDABLE_FIELDS
                or not isinstance(operation.get("text"), str)
                or not operation["text"]
                or len(operation["text"]) > 2000
                or set(operation) != {"field", "operation", "text"}
                or operation["field"] in edits
                or operation["field"] in operation_fields
            ):
                return {"ok": False, "applied": False, "outcome": "invalid_fields"}
            operation_fields.add(operation["field"])
        try:
            await assert_not_readonly(self.request)
            draft = await self._current()
            original = draft.definition or {}
            merged_edits = {key: value for key, value in edits.items() if key != "selected_tool_refs"}
            for operation in operations:
                field = operation["field"]
                current_value = original.get(field)
                if not isinstance(current_value, str):
                    return {"ok": False, "applied": False, "outcome": "invalid_fields"}
                merged_edits[field] = current_value + operation["text"]
            selected = edits.get("selected_tool_refs", original.get("tool_refs", []))
            if draft.entry_mode == "canvas":
                manual = {
                    "name": "",
                    "display_name": "",
                    "description": "",
                    "system_prompt": "",
                    "model": "analyst",
                    "max_iterations": 12,
                    "output_schema": None,
                    "output_schema_text": "",
                    "visibility": "creator_only",
                    **original,
                    **merged_edits,
                    "tool_refs": selected,
                }
                manual.pop("skills", None)
                if "output_schema" in merged_edits:
                    manual["output_schema_text"] = (
                        json.dumps(merged_edits["output_schema"], ensure_ascii=False) if merged_edits["output_schema"] else ""
                    )
                definition = ManualDefinitionBody.model_validate(manual).model_dump()
                updated = await (await _draft_store(self.request)).save_manual_draft(
                    draft.id,
                    team_id=self.user.team_id,
                    owner_user_id=self.user.id,
                    workspace_id=draft.workspace_id,
                    expected_version=self.expected_version,
                    purpose=draft.purpose_text or "",
                    definition=definition,
                    text_only_confirmed=(
                        not selected and any(warning.get("code") == "subagent_text_only_confirmation" for warning in draft.warnings)
                    ),
                )
            else:
                definition = DraftDefinitionBody.model_validate(
                    {
                        **{key: value for key, value in original.items() if key in _EDITABLE_FIELDS and key != "selected_tool_refs"},
                        **merged_edits,
                        "visibility": "creator_only",
                    }
                )
                body = PatchDraftBody(
                    definition=definition,
                    selected_tool_refs=selected,
                    selected_skills=[],
                )
                updated = await _review_update(
                    body=body,
                    request=self.request,
                    user=self.user,
                    draft=draft,
                    expected_version=self.expected_version,
                )
        except SubagentDraftVersionConflictError:
            return {"ok": False, "applied": False, "outcome": "version_conflict"}
        except (HTTPException, ValidationError, ValueError, TypeError) as exc:
            return {
                "ok": False,
                "applied": False,
                "outcome": "edit_rejected",
                "reason": getattr(exc, "detail", None) or type(exc).__name__,
            }
        if not isinstance(updated, SubagentDraft):
            return {"ok": False, "applied": False, "outcome": "edit_rejected"}
        return {
            "ok": True,
            "applied": True,
            "outcome": "draft_updated",
            "draft_id": updated.id,
            "draft_status": updated.status,
            "draft_title": updated.definition.get("display_name") if updated.definition else None,
            "draft_description": updated.definition.get("description") if "description" in merged_edits and updated.definition else None,
            "changed_fields": sorted(set(edits) | operation_fields),
            "version": updated.row_version,
            "validation_status": updated.validation_status,
            "message": "右侧草稿已更新。请核对配置，并在确认后创建智能体。",
        }
