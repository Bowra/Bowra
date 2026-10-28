"""Members share one of their conversation Turns with an Agent's Owner as an evaluation-case source.

GET    /api/v1/sessions/{session_id}/eval-shares   the requester's team-Agent Turns in that session,
                                                    with share and thumbs-down state
PUT    /api/v1/turns/{turn_id}/eval-share          share one Turn with the Owner of the Agent that answered it
DELETE /api/v1/turns/{turn_id}/eval-share          withdraw that share
PUT    /api/v1/turns/{turn_id}/feedback            give or update a thumbs-down (reasons, comment)
DELETE /api/v1/turns/{turn_id}/feedback            clear that thumbs-down

Only the Turn's author can share, withdraw or rate it, and only for a team Agent's answer; sharing
also needs an Agent someone else owns. The Owner reads shared Turns, and their thumbs-down, through
the Agent's conversation candidates and Pending list, which recheck every condition. A Turn the
requester can't share or rate is a 404, the same as a missing one. Consent stays separate: giving a
thumbs-down never shares the Turn; the chat's dialog calls the share route when the member ticks it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, NoReturn

from bowhead_core.agent_eval_conversation_cases import (
    ANSWER_FEEDBACK_REASONS,
    FEEDBACK_COMMENT_MAX_CHARS,
    AgentEvalConversationError,
    clear_answer_feedback,
    list_shareable_turns,
    record_answer_feedback,
    share_turn,
    withdraw_turn_share,
)
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.routes.auth import _current_user
from bowhead_gateway.routes.auth import _store as _user_store_dep

if TYPE_CHECKING:
    from bowhead_core.user_store import User, UserStore

router = APIRouter(prefix="/api/v1", tags=["turn-eval-shares"])


def _raise(exc: AgentEvalConversationError) -> NoReturn:
    status = 404 if exc.code in {"turn_eval_share_unavailable", "answer_feedback_unavailable"} else 422
    raise HTTPException(status, detail={"error": {"code": exc.code}}) from exc


@router.get("/sessions/{session_id}/eval-shares")
async def list_session_eval_shares(
    session_id: str,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_user_store_dep),
) -> dict[str, Any]:
    items = await list_shareable_turns(store.db_path, team_id=user.team_id, user_id=user.id, session_id=session_id)
    return {"kind": "turn_eval_shares", "schema_version": 1, "items": items}


@router.put("/turns/{turn_id}/eval-share", dependencies=[Depends(require_not_readonly)])
async def share_turn_for_eval(
    turn_id: str,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_user_store_dep),
) -> dict[str, Any]:
    try:
        item = await share_turn(store.db_path, team_id=user.team_id, user_id=user.id, turn_id=turn_id)
    except AgentEvalConversationError as exc:
        _raise(exc)
    return {"kind": "turn_eval_share", "schema_version": 1, **item}


@router.delete("/turns/{turn_id}/eval-share", dependencies=[Depends(require_not_readonly)])
async def withdraw_turn_eval_share(
    turn_id: str,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_user_store_dep),
) -> dict[str, Any]:
    item = await withdraw_turn_share(store.db_path, team_id=user.team_id, user_id=user.id, turn_id=turn_id)
    return {"kind": "turn_eval_share", "schema_version": 1, **item}


FeedbackReason = Literal["fact_wrong", "not_answered", "missing_content", "too_brief", "too_verbose", "presentation", "other"]


class AnswerFeedbackBody(BaseModel):
    """A thumbs-down: optional reasons from the fixed set and an optional comment."""

    reason_codes: list[FeedbackReason] = Field(default_factory=list, max_length=len(ANSWER_FEEDBACK_REASONS))
    comment: str | None = Field(default=None, max_length=FEEDBACK_COMMENT_MAX_CHARS)


@router.put("/turns/{turn_id}/feedback", dependencies=[Depends(require_not_readonly)])
async def give_answer_feedback(
    turn_id: str,
    body: AnswerFeedbackBody,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_user_store_dep),
) -> dict[str, Any]:
    try:
        item = await record_answer_feedback(
            store.db_path, team_id=user.team_id, user_id=user.id, turn_id=turn_id, reason_codes=body.reason_codes, comment=body.comment
        )
    except AgentEvalConversationError as exc:
        _raise(exc)
    return {"kind": "turn_answer_feedback", "schema_version": 1, **item}


@router.delete("/turns/{turn_id}/feedback", dependencies=[Depends(require_not_readonly)])
async def clear_turn_answer_feedback(
    turn_id: str,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_user_store_dep),
) -> dict[str, Any]:
    item = await clear_answer_feedback(store.db_path, team_id=user.team_id, user_id=user.id, turn_id=turn_id)
    return {"kind": "turn_answer_feedback", "schema_version": 1, **item}
