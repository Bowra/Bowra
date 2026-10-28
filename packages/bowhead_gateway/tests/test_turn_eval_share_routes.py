"""Member-side routes for sharing a conversation Turn with the Agent's Owner as an evaluation-case source."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import httpx2
import pytest
from bowhead_core.agent_eval_conversation_cases import AgentEvalConversationError
from bowhead_core.user_store import UserStore
from bowhead_gateway.routes import agents, turn_eval_shares
from fastapi import FastAPI, HTTPException

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


@pytest.fixture
async def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initialize_migrated_core_database) -> FastAPI:
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")
    db = tmp_path / "bowhead.db"
    initialize_migrated_core_database(db)
    users = UserStore(db)
    await users.initialize()
    team = await users.create_team(name="Acme", max_seats=10)
    member = await users.create_user(team_id=team.id, email="member@example.com", password="Passw0rd!")

    class _Settings:
        auth_mode = "dev"

    app = FastAPI()
    app.include_router(turn_eval_shares.router)
    app.state.settings = _Settings()
    app.state.user_store = users
    app.state.ids = {"member": member.id, "team": team.id}
    return app


async def _call(app: FastAPI, method: str, path: str):
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        return await client.request(method, path, headers={"x-bowhead-user-id": app.state.ids["member"]})


async def test_the_member_shares_and_withdraws_their_own_turn(app: FastAPI, monkeypatch) -> None:
    share = AsyncMock(return_value={"turn_id": "t_1", "agent_id": "osa_1", "agent_name": "Sales", "shared": True})
    withdraw = AsyncMock(return_value={"turn_id": "t_1", "shared": False})
    monkeypatch.setattr(turn_eval_shares, "share_turn", share)
    monkeypatch.setattr(turn_eval_shares, "withdraw_turn_share", withdraw)

    shared = await _call(app, "PUT", "/api/v1/turns/t_1/eval-share")
    assert shared.status_code == 200
    assert shared.json() == {
        "kind": "turn_eval_share",
        "schema_version": 1,
        "turn_id": "t_1",
        "agent_id": "osa_1",
        "agent_name": "Sales",
        "shared": True,
    }
    # The share is always the caller's own: the user id comes from the session, never from the request.
    assert share.await_args.kwargs == {"team_id": app.state.ids["team"], "user_id": app.state.ids["member"], "turn_id": "t_1"}

    withdrawn = await _call(app, "DELETE", "/api/v1/turns/t_1/eval-share")
    assert withdrawn.status_code == 200
    assert withdrawn.json()["shared"] is False
    assert withdraw.await_args.kwargs["user_id"] == app.state.ids["member"]


async def test_a_turn_the_member_cannot_share_is_a_not_found(app: FastAPI, monkeypatch) -> None:
    monkeypatch.setattr(turn_eval_shares, "share_turn", AsyncMock(side_effect=AgentEvalConversationError("turn_eval_share_unavailable")))
    response = await _call(app, "PUT", "/api/v1/turns/t_theirs/eval-share")
    assert response.status_code == 404
    assert response.json()["detail"] == {"error": {"code": "turn_eval_share_unavailable"}}


async def test_the_session_lists_its_shareable_turns(app: FastAPI, monkeypatch) -> None:
    items = [{"turn_id": "t_1", "agent_id": "osa_1", "agent_name": "Sales", "shared": False}]
    listing = AsyncMock(return_value=items)
    monkeypatch.setattr(turn_eval_shares, "list_shareable_turns", listing)
    response = await _call(app, "GET", "/api/v1/sessions/s_1/eval-shares")
    assert response.status_code == 200
    assert response.json() == {"kind": "turn_eval_shares", "schema_version": 1, "items": items}
    assert listing.await_args.kwargs["session_id"] == "s_1"


async def test_the_member_gives_and_clears_a_thumbs_down_on_their_own_turn(app: FastAPI, monkeypatch) -> None:
    feedback = {"score": -1, "reason_codes": ["fact_wrong"], "comment": "Wrong quarter", "given_at": "2026-10-19T00:00:00+00:00"}
    record = AsyncMock(
        return_value={"turn_id": "t_1", "agent_id": "osa_1", "agent_name": "Sales", "shared": False, "shareable": True, "feedback": feedback}
    )
    clear = AsyncMock(return_value={"turn_id": "t_1", "feedback": None})
    monkeypatch.setattr(turn_eval_shares, "record_answer_feedback", record)
    monkeypatch.setattr(turn_eval_shares, "clear_answer_feedback", clear)
    transport = httpx2.ASGITransport(app=app)
    headers = {"x-bowhead-user-id": app.state.ids["member"]}
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        given = await client.put("/api/v1/turns/t_1/feedback", json={"reason_codes": ["fact_wrong"], "comment": "Wrong quarter"}, headers=headers)
        assert given.status_code == 200
        assert given.json()["feedback"] == feedback and given.json()["shared"] is False
        # The author is always the caller; a thumbs-down never shares the Turn by itself.
        assert record.await_args.kwargs == {
            "team_id": app.state.ids["team"],
            "user_id": app.state.ids["member"],
            "turn_id": "t_1",
            "reason_codes": ["fact_wrong"],
            "comment": "Wrong quarter",
        }
        too_long = await client.put("/api/v1/turns/t_1/feedback", json={"comment": "x" * 501}, headers=headers)
        assert too_long.status_code == 422
        # Only the fixed reason set; the older satisfaction codes are not accepted here.
        old_code = await client.put("/api/v1/turns/t_1/feedback", json={"reason_codes": ["missed_dim"]}, headers=headers)
        assert old_code.status_code == 422
    cleared = await _call(app, "DELETE", "/api/v1/turns/t_1/feedback")
    assert cleared.status_code == 200 and cleared.json()["feedback"] is None
    assert clear.await_args.kwargs["user_id"] == app.state.ids["member"]


def test_the_route_accepts_exactly_the_core_reason_set() -> None:
    from typing import get_args

    from bowhead_core.agent_eval_conversation_cases import ANSWER_FEEDBACK_REASONS

    assert get_args(turn_eval_shares.FeedbackReason) == ANSWER_FEEDBACK_REASONS


async def test_a_turn_the_member_cannot_rate_is_a_not_found(app: FastAPI, monkeypatch) -> None:
    monkeypatch.setattr(turn_eval_shares, "record_answer_feedback", AsyncMock(side_effect=AgentEvalConversationError("answer_feedback_unavailable")))
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.put("/api/v1/turns/t_lead/feedback", json={}, headers={"x-bowhead-user-id": app.state.ids["member"]})
    assert response.status_code == 404
    assert response.json()["detail"] == {"error": {"code": "answer_feedback_unavailable"}}


async def test_the_owner_takes_a_thumbs_down_off_pending_and_others_get_a_not_found(monkeypatch) -> None:
    from types import SimpleNamespace

    handle = AsyncMock(return_value={"turn_id": "t_1", "handled": "coach"})
    monkeypatch.setattr(agents, "handle_pending_feedback", handle)
    user = SimpleNamespace(id="usr_owner", team_id="team_1")
    store = SimpleNamespace(db_path="unused.db")
    result = await agents.handle_agent_member_feedback("osa_1", "t_1", agents.AgentPendingFeedbackBody(action="coach"), user=user, store=store)
    assert result == {"kind": "agent_member_feedback_handled", "schema_version": 1, "turn_id": "t_1", "handled": "coach"}
    assert handle.await_args.kwargs == {"team_id": "team_1", "agent_id": "osa_1", "user_id": "usr_owner", "turn_id": "t_1", "action": "coach"}

    handle.side_effect = AgentEvalConversationError("agent_answer_feedback_unavailable")
    with pytest.raises(HTTPException) as error:
        await agents.handle_agent_member_feedback("osa_1", "t_1", agents.AgentPendingFeedbackBody(action="dismissed"), user=user, store=store)
    assert error.value.status_code == 404
    assert error.value.detail == {"error": {"code": "agent_answer_feedback_unavailable"}}


async def test_only_the_agent_owner_reaches_the_conversation_candidates(monkeypatch) -> None:
    from types import SimpleNamespace

    from bowhead_core.agent_eval_store import AgentEvalStoreError

    class _DeniedProduct:
        def __init__(self, _path) -> None:
            pass

        async def manager_snapshot(self, **_kwargs):
            raise AgentEvalStoreError("agent_eval_action_denied")

    listing = AsyncMock(return_value=[])
    monkeypatch.setattr(agents, "AgentEvalProductStore", _DeniedProduct)
    monkeypatch.setattr(agents, "list_conversation_candidates", listing)
    monkeypatch.setattr(agents, "_require_eval_runtime", lambda _request: None)
    with pytest.raises(HTTPException) as error:
        await agents.list_agent_eval_conversation_candidates(
            "osa_1", SimpleNamespace(), user=SimpleNamespace(id="usr_member", team_id="team_1"), store=SimpleNamespace(db_path="unused.db")
        )
    assert error.value.status_code == 404
    # Shared Turns are never read for someone who isn't the Owner.
    listing.assert_not_awaited()
