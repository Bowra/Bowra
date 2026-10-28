"""Authenticate an exact creation-draft start before paid Chat preparation."""

from bowhead_core.agent_draft_send_target import AgentDraftSendTarget
from bowhead_core.subagent_draft_store import SubagentDraftVersionConflictError
from fastapi import HTTPException, Request

from bowhead_gateway.routes.auth import _current_user
from bowhead_gateway.subagent_draft_chat_editor import SubagentDraftChatEditor


async def admit_agent_draft_target(
    request: Request, target: AgentDraftSendTarget, *, user_id: str, team_id: str, workspace_id: str
) -> SubagentDraftChatEditor:
    user = await _current_user(request)
    try:
        if not team_id or not workspace_id or user.id != user_id or user.team_id != team_id:
            raise ValueError("draft scope unavailable")
        editor = SubagentDraftChatEditor(request, user, target.id, target.version, workspace_id=workspace_id)
        await editor._current()
        return editor
    except (HTTPException, SubagentDraftVersionConflictError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": {"code": "agent_draft_target_unavailable", "admission": "not_started"}},
        ) from exc
