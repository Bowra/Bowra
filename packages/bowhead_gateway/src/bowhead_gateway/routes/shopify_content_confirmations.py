"""Requester-only review decisions for exact Shopify content updates."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from bowhead_core.builtin_shopify_tool import feature_enabled
from bowhead_core.shopify_content import content_writes_enabled, default_shopify_content_confirmations
from bowhead_core.user_store import User
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict

from bowhead_gateway.routes.auth import _current_user, require_not_readonly

router = APIRouter()


class DecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    decision: Literal["approve", "reject"]


def _pending(user: User) -> list[dict[str, Any]]:
    if not feature_enabled() or not content_writes_enabled():
        raise HTTPException(404, "shopify_content_unavailable")
    return [item for item in default_shopify_content_confirmations().pending(user.id) if item["team_id"] == user.team_id]


@router.get("")
async def pending(response: Response, user: User = Depends(_current_user)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "private, no-store"
    return {"confirmations": _pending(user)}


@router.post("/{confirmation_id}/decision", dependencies=[Depends(require_not_readonly)])
async def decide(confirmation_id: str, body: DecisionInput, user: User = Depends(_current_user)) -> dict[str, Any]:
    item = next((item for item in _pending(user) if item["id"] == confirmation_id), None)
    if (
        item is None
        or datetime.fromisoformat(item["expires_at"]) <= datetime.now(UTC)
        or not default_shopify_content_confirmations().decide(user.id, confirmation_id, body.decision == "approve")
    ):
        raise HTTPException(409, "shopify_content_confirmation_unavailable")
    return {"ok": True}
