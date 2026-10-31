"""The real batch-A writer retains maintenance intent before worker dispatch."""

import asyncio
import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.events import OrchestratorEvent
from bowhead_gateway.routes import chat
from bowhead_gateway.routes.auth import _auth_session_generation_hash
from test_chat_upload_integration import case as case

from bowhead_gateway import turn_service

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_legacy_maintenance_persists_intent_and_replays_one_receipt(case, monkeypatch):
    async def model(*args, **kwargs):
        router = SimpleNamespace(supports_context_maintenance=lambda *args: True)
        return turn_service.TeamModelRuntimeResolution(router=router, result="team_safe_success", reason="test_provider")

    async def worker(_orchestrator, request, **kwargs):
        source = await case.sessions.get_chat_turn_user_source(turn_id=request.turn_id, user_id=request.user_id, workspace_id=request.workspace_id)
        case.captured.append((request, source))
        await case.sessions.mark_completed(turn_id=request.turn_id, intent=None, final_answer="", tokens_input=0, tokens_output=0, cost_usd=0.0)
        await kwargs["admitted_legacy_bus"].emit(OrchestratorEvent(event_type="done", turn_id=request.turn_id, payload={"status": "completed"}))
        await case.sessions.finalize_turn(session_id=request.session_id, current_turn_id=request.turn_id, next_turn_id=None)

    monkeypatch.setattr(turn_service, "resolve_team_model_runtime", model)
    monkeypatch.setattr(chat, "_run_turn_and_cleanup", worker)
    session = await case.sessions.ensure_session(session_id=None, user_id=case.alice.id, workspace_id=case.spaces[0])
    headers = case.headers()
    token = headers["Authorization"].removeprefix("Bearer ")
    generation = "legacy_" + hashlib.sha256(f"bowhead-auth-session-legacy.v1\0{token}".encode()).hexdigest()[:32]
    await case.sessions.register_chat_auth_session_generation(
        user_id=case.alice.id, auth_session_generation_hash=_auth_session_generation_hash(generation)
    )
    action = {"contract": "context-maintenance.v1", "expected_generation": None}
    payload = {
        "session_id": session.id,
        "workspace_id": case.spaces[0],
        "message": "",
        "context_maintenance": action,
        "client_submission_id": str(uuid4()),
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "planning_mode": "off",
    }
    response = await case.client.post("/api/v1/chat/send", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    accepted = response.json()
    assert accepted["context_maintenance"] == action
    for _ in range(100):
        if case.captured and await case.sessions.get_chat_spawn_dispatch_projection_state(turn_id=accepted["turn_id"]) == "settled":
            break
        await asyncio.sleep(0.01)
    request, source = case.captured[0]
    assert source.context_maintenance is not None
    assert source.context_maintenance.model_dump() == action
    assert request.context_maintenance == source.context_maintenance
    async with open_shared_ctx(case.path) as db:
        row = await (await db.execute("SELECT operation_kind,final_answer FROM turns WHERE id=?", (accepted["turn_id"],))).fetchone()
    assert tuple(row) == ("context_maintenance", "")
    replay = await case.client.post("/api/v1/chat/send", headers=headers, json=payload)
    assert replay.status_code == 200 and replay.json() == accepted
    lookup = await case.client.get(
        f"/api/v1/chat/submissions/{payload['client_submission_id']}",
        headers=headers,
        params={"workspace_id": case.spaces[0], "requested_session_id": session.id},
    )
    assert lookup.status_code == 200 and lookup.json()["response"] == accepted
    assert len(case.captured) == 1
    assert await case.sessions.list_turns(session_id=session.id) == []
