"""Owner-only confirmation of emails the Lead is about to send (``send_email``).

GET  /api/v1/email-send-confirmations                 — pending emails awaiting the caller
POST /api/v1/email-send-confirmations/{id}/decision   — approve or reject one of them

Only the user who started the turn sees or decides; there is no admin bypass.
"""

from __future__ import annotations

from typing import Any, Literal

from bowhead_core.email_send_confirmation import default_email_send_confirmations
from bowhead_core.user_store import User
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from bowhead_gateway.routes.auth import _current_user

router = APIRouter()


class DecisionInput(BaseModel):
    decision: Literal["approve", "reject"]


@router.get("")
async def pending(response: Response, user: User = Depends(_current_user)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "private, no-store"
    return {"confirmations": default_email_send_confirmations().pending(user.id)}


@router.post("/{confirmation_id}/decision")
async def decide(confirmation_id: str, body: DecisionInput, user: User = Depends(_current_user)) -> dict[str, Any]:
    if not default_email_send_confirmations().decide(user.id, confirmation_id, body.decision == "approve"):
        raise HTTPException(409, {"code": "email_confirmation_unavailable"})
    return {"ok": True}
