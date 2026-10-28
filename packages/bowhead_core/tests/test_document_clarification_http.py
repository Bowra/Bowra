"""Real HTTP/authorization/card writers before the shared resume dispatch seam."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2
import jwt
import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.access_control import AccessControlStore
from bowhead_core.clarification_store import ClarificationCandidate, ClarificationStore
from bowhead_core.document_chat_continuation import DocumentTurnOwner
from bowhead_core.document_clarification_lifecycle import prior_document_answers_in_tx
from bowhead_core.events import EventBusRegistry
from bowhead_core.terminal_protocol_store import TerminalProtocolStore
from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.routes import clarifications
from fastapi import FastAPI
from test_document_clarification_lifecycle import pause, prepare, rows
from test_document_planning_continuation import QUESTION, checkpoint
from test_document_store import SCOPE
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db

from bowhead_gateway import turn_service

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
SECRET = "document-http-fixture-secret-32-bytes"


@pytest.fixture
async def http_case(ctx, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", SECRET)
    monkeypatch.delenv("BOWHEAD_JWT_SECRETS", raising=False)
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "1")
    app = FastAPI()
    app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    app.state.settings = SimpleNamespace(auth_mode="prod")
    app.state.session_store = ctx.sessions
    app.state.user_store = UserStore(ctx.path)
    app.state.access_control_store = AccessControlStore(ctx.path)
    app.state.clarification_store = ClarificationStore(ctx.path)
    app.state.terminal_protocol_store = TerminalProtocolStore(ctx.path)
    app.state.turn_log_store = TurnLogStore(str(ctx.path))
    registry = EventBusRegistry(default_log_sink=OrderedTurnLogSink(app.state.turn_log_store))
    app.state.orchestrator = SimpleNamespace(session_store=ctx.sessions, event_registry=registry)
    monkeypatch.setattr(clarifications, "get_event_registry", lambda: registry)
    monkeypatch.setattr(
        turn_service,
        "resolve_team_model_runtime",
        AsyncMock(return_value=turn_service.TeamModelRuntimeResolution(router=object(), result="team_safe_success", reason="fixture_provider")),
    )
    from bowhead_gateway import datarc_context

    datarc = AsyncMock(side_effect=AssertionError("Document continuation must not resolve Datarc or rerun Lead"))
    monkeypatch.setattr(datarc_context, "resolve_datarc_turn_context", datarc)
    spawn = AsyncMock(return_value=SimpleNamespace(execution_generation=2))
    monkeypatch.setattr(turn_service, "spawn_resume_turn", spawn)
    token = jwt.encode({"sub": SCOPE.user_id, "team_id": SCOPE.team_id, "pwd_v": 0}, SECRET, algorithm="HS256")
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer " + token}
    ) as client:
        yield SimpleNamespace(client=client, app=app, spawn=spawn, datarc=datarc, ctx=ctx)


def payload(text="Compare Q2 with Q1"):
    return {"answers": [{"question": QUESTION, "source": "custom", "answer": text, "skipped": False, "option_index": None}]}


async def test_single_choice_http_answer_is_available_to_original_document_planning(http_case):
    from bowhead_gateway.datarc_context import DatarcTurnContext

    ctx = http_case.ctx
    # This is an ordinary pre-document answer, so it retains ordinary resume.
    http_case.datarc.side_effect = None
    http_case.datarc.return_value = DatarcTurnContext()
    await http_case.app.state.user_store.admin_set_user_credit(
        user_id=SCOPE.user_id,
        team_id=SCOPE.team_id,
        by_user_id=SCOPE.user_id,
        credit_balance=1000,
    )
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("UPDATE teams SET team_credit_balance=1000 WHERE id=?", (SCOPE.team_id,))
        await db.commit()
    question = "Total parcels 用哪个口径?(West=3810、East=2190 已按您的指示固定)"
    value = "按数据相加:6000"
    store = http_case.app.state.clarification_store
    assert await ctx.sessions.claim_chat_spawn_dispatch_for_worker(turn_id="t_doc", owner_id="ordinary-worker", lease_seconds=600) == "claimed"
    card = await store.put(
        turn_id="t_doc",
        session_id="s_doc",
        user_id=SCOPE.user_id,
        workspace_id=SCOPE.workspace_id,
        kind="ambiguous_turn",
        token="",
        rationale=question,
        candidates=[ClarificationCandidate(value=value, label=value)],
        resume_context={"user_text": ctx.source.user_text, "session_id": "s_doc"},
        bounded_question={"generation": 1, "owner_id": "ordinary-worker", "questions": [], "fallback": question},
    )
    assert await ctx.sessions.mark_clarification_pending(turn_id="t_doc", intent="clarify")
    assert await http_case.app.state.turn_log_store.record(
        turn_id="t_doc",
        event_type="done",
        payload={"turn_id": "t_doc", "status": "clarification_pending", "execution_generation": 1, "seq": 1},
    )
    assert await ctx.sessions.mark_chat_spawn_dispatch_terminal(turn_id="t_doc", owner_id="ordinary-worker")
    typed = {"question": question, "source": "candidate", "answer": value, "option_index": 0}
    response = await http_case.client.post(f"/api/v1/clarifications/{card.request_id}/resolve", json={"chosen_value": value, "answers": [typed]})
    assert response.status_code == 200, response.text
    retained = await store.get(card.request_id)
    assert retained.answers == [{**typed, "skipped": False}]
    worker = DocumentTurnOwner(**SCOPE.model_dump(), session_id="s_doc", turn_id="t_doc", generation=2, owner_id="resumed-worker")
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN")
        assert await prior_document_answers_in_tx(db, worker=worker) == retained.answers
    replay = await http_case.client.post(f"/api/v1/clarifications/{card.request_id}/resolve", json={"chosen_value": "6200"})
    assert replay.status_code == 200, replay.text
    assert (await store.get(card.request_id)).answers == retained.answers


async def test_http_answer_dispatches_original_checkpoint_without_lead_inputs(http_case):
    ctx = http_case.ctx
    case = await checkpoint(ctx)
    store, card = await pause(ctx, case)
    response = await http_case.client.post(f"/api/v1/clarifications/{card.request_id}/resolve", json=payload())
    assert response.status_code == 200, response.text
    assert response.json()["action"] == "turn_resume"
    request = http_case.spawn.await_args.args[1]
    assert request.turn_id == case.worker.turn_id and request.document_checkpoint == case.ref
    assert request.document_clarification_id == card.request_id
    assert request.text == ctx.source.user_text and request.attachments == [] and request.resume_clarification_id is None
    assert (await store.get(card.request_id)).answers == payload()["answers"]
    http_case.datarc.assert_not_awaited()
    retry = await http_case.client.post(f"/api/v1/clarifications/{card.request_id}/resolve", json=payload("A changed answer"))
    assert retry.status_code == 200, retry.text
    assert (await store.get(card.request_id)).answers == payload()["answers"]


@pytest.mark.parametrize("missing_session_store", [False, True])
async def test_prepared_but_unsettled_card_is_not_projected(http_case, missing_session_store):
    case = await checkpoint(http_case.ctx)
    _, card = await prepare(http_case.ctx, case)
    if missing_session_store:
        http_case.app.state.orchestrator.session_store = None
    response = await http_case.client.get(f"/api/v1/clarifications/turn/{case.worker.turn_id}")
    expected_status = 503 if missing_session_store else 409
    expected_code = "clarification_orchestrator_unavailable" if missing_session_store else "clarification_pause_pending"
    assert response.status_code == expected_status, response.text
    assert response.json()["detail"]["code"] == expected_code
    assert response.json()["detail"]["diagnostics"]["retryable"] is (not missing_session_store)
    assert "items" not in response.json()
    detail = await http_case.client.get(f"/api/v1/clarifications/{card.request_id}")
    assert detail.status_code == expected_status, detail.text
    assert detail.json()["detail"]["code"] == expected_code
    http_case.spawn.assert_not_awaited()


async def test_pending_list_defers_until_original_dispatch_settles(http_case, monkeypatch):
    ctx = http_case.ctx
    case = await checkpoint(ctx)
    endpoint = f"/api/v1/clarifications/turn/{case.worker.turn_id}"
    mark_terminal = ctx.sessions.mark_chat_spawn_dispatch_terminal

    async def read_before_dispatch_settles(**kwargs):
        # The production writer has emitted both the question and done here.
        assert await rows(ctx, "SELECT 1 FROM turn_events WHERE event_type='done'") == [(1,)]
        assert not await ctx.sessions.is_turn_resume_ready(case.worker.turn_id)
        response = await http_case.client.get(endpoint)
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "clarification_pause_pending"
        assert response.json()["detail"]["diagnostics"]["retryable"] is True
        card = (await http_case.app.state.clarification_store.get_pending_for_turn(case.worker.turn_id))[0]
        refused = await http_case.client.post(f"/api/v1/clarifications/{card.request_id}/resolve", json=payload())
        assert refused.status_code == 409, refused.text
        http_case.spawn.assert_not_awaited()
        assert (await http_case.app.state.clarification_store.get(card.request_id)).status == "pending"
        return await mark_terminal(**kwargs)

    monkeypatch.setattr(ctx.sessions, "mark_chat_spawn_dispatch_terminal", read_before_dispatch_settles)
    _, card = await pause(ctx, case)
    ready = await http_case.client.get(endpoint)
    assert ready.status_code == 200, ready.text
    assert [item["request_id"] for item in ready.json()["items"]] == [card.request_id]
    resolved = await http_case.client.post(f"/api/v1/clarifications/{card.request_id}/resolve", json=payload())
    assert resolved.status_code == 200, resolved.text
    assert http_case.spawn.await_count == 1
    assert http_case.spawn.await_args.args[1].document_checkpoint == case.ref
    after = await http_case.client.get(endpoint)
    assert after.status_code == 200, after.text
    assert after.json()["items"] == []


@pytest.mark.parametrize("access", ["wrong_owner", "revoked_membership"])
async def test_pending_list_checks_access_before_exposing_unsettled_pause(http_case, access):
    case = await checkpoint(http_case.ctx)
    _, card = await prepare(http_case.ctx, case)
    headers = {}
    if access == "wrong_owner":
        token = jwt.encode({"sub": "u_other", "team_id": SCOPE.team_id, "pwd_v": 0}, SECRET, algorithm="HS256")
        headers = {"Authorization": "Bearer " + token}
    else:
        async with open_shared_ctx(http_case.ctx.path) as db:
            await db.execute("DELETE FROM workspace_members WHERE workspace_id=? AND user_id=?", (SCOPE.workspace_id, SCOPE.user_id))
            await db.commit()
    response = await http_case.client.get(f"/api/v1/clarifications/turn/{case.worker.turn_id}", headers=headers)
    assert response.status_code in {403, 404}, response.text
    assert card.request_id not in response.text
    assert "clarification_resume_unavailable" not in response.text
    http_case.spawn.assert_not_awaited()


async def test_http_skip_settles_same_turn_without_dispatch(http_case):
    case = await checkpoint(http_case.ctx)
    store, card = await pause(http_case.ctx, case)
    response = await http_case.client.post(f"/api/v1/clarifications/{card.request_id}/resolve", json={"skip": True})
    assert response.status_code == 200, response.text
    assert response.json()["action"] == "turn_settled" and response.json()["resume_turn_id"] == case.worker.turn_id
    assert (await store.get(card.request_id)).status == "dismissed"
    assert (await http_case.ctx.sessions.get_turn(case.worker.turn_id)).status == "cancelled"
    assert await rows(http_case.ctx, "SELECT state FROM document_executions") == [("cancelled",)]
    assert await rows(http_case.ctx, "SELECT count(*) FROM turns") == [(1,)]
    http_case.spawn.assert_not_awaited()


@pytest.mark.parametrize("change", ["wrong_owner", "question", "plain_answer", "multiple_answers"])
async def test_http_invalid_answer_never_consumes_original_card(http_case, change):
    case = await checkpoint(http_case.ctx)
    store, card = await pause(http_case.ctx, case)
    body = payload()
    headers = {}
    if change == "wrong_owner":
        token = jwt.encode({"sub": "u_other", "team_id": SCOPE.team_id, "pwd_v": 0}, SECRET, algorithm="HS256")
        headers = {"Authorization": "Bearer " + token}
    elif change == "question":
        body["answers"][0]["question"] = "An unrelated question"
    elif change == "plain_answer":
        body = {"chosen_value": "Unbound free text"}
    else:
        body["answers"] *= 2
    response = await http_case.client.post(f"/api/v1/clarifications/{card.request_id}/resolve", json=body, headers=headers)
    assert response.status_code in {404, 422}, response.text
    assert (await store.get(card.request_id)).status == "pending"
    assert await store.get_resolution_work_state(card.request_id) is None
    http_case.spawn.assert_not_awaited()
