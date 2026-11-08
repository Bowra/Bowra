"""Requester confirmation of team MCP tool calls (``FEATURE_TEAM_TOOL_CONFIRMATION``).

GET  /api/v1/team-tool-confirmations                 — pending calls awaiting the caller
POST /api/v1/team-tool-confirmations/{id}/decision   — approve once, approve for the session, or reject

Only the member who started the task sees or decides; there is no administrator bypass.
"""

from __future__ import annotations

from typing import Any, Literal

from bowhead_core.team_tool_confirmation import default_team_tool_confirmations
from bowhead_core.user_store import User
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from bowhead_gateway.routes.auth import _current_user

router = APIRouter()


class DecisionInput(BaseModel):
    decision: Literal["approve", "approve_session", "reject"]


@router.get("")
async def pending(response: Response, user: User = Depends(_current_user)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "private, no-store"
    return {"confirmations": default_team_tool_confirmations().pending(user.id)}


@router.post("/{confirmation_id}/decision")
async def decide(confirmation_id: str, body: DecisionInput, user: User = Depends(_current_user)) -> dict[str, Any]:
    queue = default_team_tool_confirmations()
    if body.decision == "approve_session":
        recorded = queue.approve_for_session(user.id, confirmation_id)
    else:
        recorded = queue.decide(user.id, confirmation_id, body.decision == "approve")
    if not recorded:
        raise HTTPException(409, {"code": "team_tool_confirmation_unavailable"})
    return {"ok": True}
