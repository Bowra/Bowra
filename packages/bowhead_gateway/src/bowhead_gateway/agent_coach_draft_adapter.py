"""Coach operations over an existing owned Canvas draft and suggestion store."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from bowhead_core.subagent_authoring_catalog import SubagentAuthoringCatalogService
from bowhead_core.subagent_draft_store import CanvasDraftSuggestion, DraftClaimOutcome, SubagentDraft, SubagentDraftVersionConflictError
from bowhead_core.user_store import UserStore
from fastapi import HTTPException
from pydantic import ValidationError

from bowhead_gateway._entitlement_gate import assert_not_readonly, is_feature_licensed_request
from bowhead_gateway.routes.team_subagent_drafts import DraftDefinitionBody, ManualDefinitionBody, PatchDraftBody, _draft_store, _review_update

if TYPE_CHECKING:
    from bowhead_core.subagent_authoring_catalog import SubagentAuthoringCatalogSnapshot

    from bowhead_gateway.subagent_draft_chat_editor import SubagentDraftChatEditor

_FIELDS = frozenset(
    {
        "name",
        "display_name",
        "description",
        "system_prompt",
        "model",
        "max_iterations",
        "output_schema",
        "tool_refs",
        "skill_grants",
        "capability_ids",
        "builtin_plugin_ids",
    }
)

_VALIDATION_SCOPE_GUIDANCE = (
    "Validation checks the complete candidate, including unchanged fields. "
    "An invalid candidate cannot be saved as a pending suggestion: propose revalidates and rejects it without saving. "
    "User confirmation cannot bypass validation. Manual editor saving can retain incomplete drafts, but Coach proposal saving cannot. "
    "Keep the user's requested edit scope. If resolving an issue requires changing other fields or adding resources, "
    "report the blocker and ask for that additional scope instead of saving a broader suggestion. "
    "A previous resource request or pending suggestion does not override a later single-field instruction."
)


def _draft_failure(error: str, cause: Exception) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "error": error}
    if isinstance(cause, SubagentDraftVersionConflictError):
        result.update(
            retry="new_user_turn",
            message=(
                "The saved draft changed after this Turn was admitted. Stop authoring this draft in this Turn. "
                "Its server-bound baseline cannot be refreshed by inspecting again or supplying another target/version. "
                "Preserve the user's newer work and ask them to review the original draft, save any local edits if present and send a new request. "
                "If there are no local edits, review and send again without an extra save. "
                "A changed saved version does not prove that local edits are unsaved. A new Turn refreshes admission but does not waive validation. "
                "Offer to recheck whether the requested suggestion can be saved in a new Turn; do not promise a submission or save. "
                "Do not repeat inspect, validate or propose in this Turn."
            ),
        )
    return result


@dataclass
class AgentCoachDraftAdapter:
    editor: SubagentDraftChatEditor

    @property
    def caller(self) -> tuple[str, str, str]:
        return self.editor.user.id, self.editor.user.team_id, str(self.editor.workspace_id or "")

    async def _current(self, *, check_version: bool = True) -> SubagentDraft:
        editor = self.editor
        if editor.has_unsaved_local_changes:
            raise ValueError("agent_coach_not_admitted")
        users = getattr(editor.request.app.state, "user_store", None)
        if not isinstance(users, UserStore):
            raise ValueError("agent_coach_not_admitted")
        current_user = await users.get_user(editor.user.id)
        team = await users.get_team(editor.user.team_id)
        if (
            current_user is None
            or current_user.status != "active"
            or team is None
            or team.status != "active"
            or not await users.user_has_active_team_membership(editor.user.id, editor.user.team_id)
        ):
            raise ValueError("agent_coach_not_admitted")
        draft = await editor._current(check_version=check_version)
        if draft.entry_mode != "canvas":
            raise ValueError("agent_coach_canvas_draft_required")
        return draft

    async def _catalog(self) -> SubagentAuthoringCatalogSnapshot:
        editor = self.editor
        return await SubagentAuthoringCatalogService().build(
            store=editor.request.app.state.user_store,
            team_id=editor.user.team_id,
            actor_user_id=editor.user.id,
            actor_active=True,
            mcp_entitled=await is_feature_licensed_request(editor.request, "mcp"),
        )

    async def inspect(self) -> dict[str, Any]:
        try:
            draft = await self._current()
            suggestions = await (await _draft_store(self.editor.request)).list_canvas_suggestions(
                draft.id,
                team_id=self.editor.user.team_id,
                owner_user_id=self.editor.user.id,
                workspace_id=str(draft.workspace_id),
            )
            await self._current()
        except (HTTPException, ValueError, SubagentDraftVersionConflictError) as exc:
            return _draft_failure("draft_unavailable_or_changed", exc)
        schema = ManualDefinitionBody.model_json_schema()
        return {
            "ok": True,
            "draft_id": draft.id,
            "version": draft.row_version,
            "purpose": draft.purpose_text,
            "definition": draft.definition,
            "patch_schema": {
                "type": "object",
                "properties": {key: value for key, value in schema["properties"].items() if key in _FIELDS},
                "required": [key for key in schema.get("required", []) if key in _FIELDS and key not in (draft.definition or {})],
                "additionalProperties": False,
            },
            "pending_proposals": [
                {"id": item.id, "kind": item.kind, "base_version": item.base_version, "created_at": item.created_at} for item in suggestions[:10]
            ],
            "pending_proposals_truncated": len(suggestions) > 10,
            "supported_actions": ["inspect", "tools", "validate", "propose", "proposal_get"],
            "proposal_effect": "pending_suggestion_only",
            "validation": {"status": draft.validation_status, "issues": draft.validation_issues},
        }

    async def tools(self, *, query: str, offset: int, limit: int) -> dict[str, Any]:
        try:
            await self._current()
            catalog = await self._catalog()
            await self._current()
        except (HTTPException, ValueError, PermissionError, SubagentDraftVersionConflictError) as exc:
            return _draft_failure("draft_unavailable_or_changed", exc)
        needle = query.casefold().strip()
        matches = [
            tool.to_public_dict()
            for tool in catalog.tools_by_ref.values()
            if tool.discoverable and (not needle or needle in f"{tool.stable_ref} {tool.display_name} {tool.description}".casefold())
        ]
        matches.sort(key=lambda item: item["stable_ref"])
        return {
            "ok": True,
            "catalog_fingerprint": catalog.fingerprint,
            "items": matches[offset : offset + limit],
            "next_offset": offset + limit if offset + limit < len(matches) else None,
        }

    async def _review(self, patch: dict[str, Any], *, allow_empty: bool = False) -> tuple[SubagentDraft, dict[str, Any]]:
        if not patch and not allow_empty or set(patch) - _FIELDS:
            raise ValueError("invalid_fields")
        draft = await self._current()
        original = draft.definition or {}
        merged = {key: value for key, value in {**original, **patch}.items() if key != "skills"}
        if "output_schema" in patch:
            merged["output_schema_text"] = json.dumps(patch["output_schema"], ensure_ascii=False) if patch["output_schema"] is not None else ""
        manual = ManualDefinitionBody.model_validate(merged, strict=True)
        body = PatchDraftBody(
            definition=DraftDefinitionBody.model_validate(
                {key: value for key, value in manual.model_dump().items() if key not in {"tool_refs", "output_schema_text"}}
            ),
            selected_tool_refs=manual.tool_refs,
        )
        result = await _review_update(
            body=body, request=self.editor.request, user=self.editor.user, draft=draft, expected_version=self.editor.expected_version, persist=False
        )
        await self._current()
        if not isinstance(result, dict):
            raise ValueError("validation_unavailable")
        result["definition"] = manual.model_dump()
        return draft, result

    async def validate(self, patch: dict[str, Any]) -> dict[str, Any]:
        try:
            draft, review = await self._review(patch, allow_empty=True)
        except ValidationError as exc:
            return {"ok": False, "error": "invalid_patch", "issues": exc.errors(include_input=False, include_context=False)}
        except (HTTPException, ValueError, PermissionError, SubagentDraftVersionConflictError) as exc:
            return _draft_failure("draft_unavailable_or_invalid_patch", exc)
        from bowhead_core.coach_validation import authoring_validation_report, candidate_digest

        return {
            "ok": True,
            "validation_report": authoring_validation_report(
                target={"kind": "agent_draft", "id": draft.id, "version": draft.row_version},
                candidate_hash=candidate_digest(review["definition"]),
                structure="failed"
                if review["validation_status"] == "invalid"
                else "warning"
                if review["validation_status"] == "warning"
                else "passed",
                dependencies=review["dependency_validation"]["status"],
                dependency_fingerprint=review["dependency_validation"]["fingerprint"],
            ),
            "draft_id": draft.id,
            "version": draft.row_version,
            "validation_status": review["validation_status"],
            "issues": review["validation_issues"],
            "warnings": review["warnings"],
            "persisted": False,
            "executed": False,
            "message": (
                (
                    "Validation failed; no suggestion was submitted. "
                    if review["validation_status"] == "invalid"
                    else "Candidate validation completed; no suggestion was submitted. "
                )
                + "This call only validated the candidate; it did not attempt to save a suggestion. "
                "Lead with the validation outcome, including the opening sentence; do not summarize it as a failed or unsuccessful save. "
                "Describe this result as validation, not a rejected or attempted save. Earlier saved receipts remain separate. "
                + (_VALIDATION_SCOPE_GUIDANCE if review["validation_status"] == "invalid" else "")
            ),
        }

    async def propose(self, patch: dict[str, Any], *, request_id: str, turn_id: str, resource_context: dict | None = None) -> dict[str, Any]:
        editor = self.editor
        try:
            await assert_not_readonly(editor.request)
            draft = await self._current(check_version=False)
            store = await _draft_store(editor.request)
            claim = await store.claim_request(
                team_id=editor.user.team_id,
                owner_user_id=editor.user.id,
                idempotency_key=f"coach:{turn_id}:{request_id}",
                request={
                    "kind": "coach_canvas_proposal",
                    **({"resource_context": resource_context} if resource_context is not None else {}),
                    "draft_id": draft.id,
                    "workspace_id": draft.workspace_id,
                    "base_version": editor.expected_version,
                    "patch": patch,
                },
            )
            if claim.outcome is DraftClaimOutcome.CONFLICT:
                return {"ok": False, "error": "idempotency_key_reused"}
            if claim.outcome is DraftClaimOutcome.IN_PROGRESS:
                return {"ok": False, "error": "proposal_in_progress", "retry_after_ms": 1000}
            if claim.outcome is DraftClaimOutcome.REPLAY:
                saved = await store.get_canvas_suggestion_for_request(
                    team_id=editor.user.team_id,
                    owner_user_id=editor.user.id,
                    workspace_id=str(draft.workspace_id),
                    request_key_hash=claim.idempotency_key_hash,
                )
                return (
                    self._receipt(saved, replayed=True)
                    if saved is not None
                    else {"ok": False, "error": claim.outcome_code or "proposal_result_unavailable"}
                )
            try:
                draft, review = await self._review(patch)
                if resource_context is not None:
                    from bowhead_gateway.agent_coach_resource_binding import validate_resource_binding
                    from bowhead_gateway.agent_coach_resources import AgentCoachResources

                    await validate_resource_binding(
                        AgentCoachResources(editor.request, editor.user, str(draft.workspace_id), ""),
                        target={"kind": "agent_draft", "id": draft.id, "version": editor.expected_version},
                        definition=review["definition"],
                        context=resource_context,
                    )
                if review["validation_status"] == "invalid":
                    await store.complete_request(
                        team_id=editor.user.team_id,
                        owner_user_id=editor.user.id,
                        idempotency_key_hash=claim.idempotency_key_hash,
                        lease_token=claim.lease_token,
                        outcome_code="proposal_invalid",
                        result_draft_id=None,
                        failed=True,
                    )
                    return {
                        "ok": False,
                        "error": "proposal_invalid",
                        "issues": review["validation_issues"],
                        "message": _VALIDATION_SCOPE_GUIDANCE,
                    }
                await assert_not_readonly(editor.request)
                saved = await store.record_canvas_suggestion(
                    draft.id,
                    team_id=editor.user.team_id,
                    owner_user_id=editor.user.id,
                    workspace_id=str(draft.workspace_id),
                    request_key_hash=claim.idempotency_key_hash,
                    lease_token=claim.lease_token,
                    base_version=editor.expected_version,
                    require_current_base=True,
                    kind="definition",
                    payload={
                        "definition": review["definition"],
                        "recommendations": review["recommendations"],
                        "warnings": review["warnings"],
                        "validation_issues": review["validation_issues"],
                        "assumptions": [],
                        "changed_fields": sorted("output_schema_text" if field == "output_schema" else field for field in patch),
                        "source": "coach",
                        "source_turn_id": turn_id,
                        **({"resource_context": resource_context} if resource_context is not None else {}),
                    },
                )
            except (HTTPException, ValueError, PermissionError, SubagentDraftVersionConflictError) as exc:
                await store.complete_request(
                    team_id=editor.user.team_id,
                    owner_user_id=editor.user.id,
                    idempotency_key_hash=claim.idempotency_key_hash,
                    lease_token=claim.lease_token,
                    outcome_code="proposal_rejected",
                    result_draft_id=None,
                    failed=True,
                )
                return _draft_failure("proposal_rejected", exc)
        except (HTTPException, ValueError, PermissionError, SubagentDraftVersionConflictError) as exc:
            return _draft_failure("draft_unavailable_or_changed", exc)
        return self._receipt(saved, replayed=False)

    async def get_proposal(self, proposal_id: str) -> dict[str, Any]:
        try:
            draft = await self._current(check_version=False)
            saved = await (await _draft_store(self.editor.request)).get_canvas_suggestion(
                draft.id, proposal_id, team_id=self.editor.user.team_id, owner_user_id=self.editor.user.id, workspace_id=str(draft.workspace_id)
            )
        except (HTTPException, ValueError, SubagentDraftVersionConflictError) as exc:
            return _draft_failure("draft_unavailable_or_changed", exc)
        return self._receipt(saved, replayed=True) if saved is not None else {"ok": False, "error": "proposal_not_found"}

    @staticmethod
    def _receipt(saved: CanvasDraftSuggestion, *, replayed: bool) -> dict[str, Any]:
        return {
            "ok": True,
            "effect": "suggestion_read" if replayed else "suggestion_saved",
            "applied": saved.status == "applied",
            "replayed": replayed,
            "proposal": asdict(saved),
            "message": "Read the proposal status before reporting its outcome. Review pending suggestions in the original draft; refresh if needed. This call did not create an Agent or apply a suggestion.",
        }
