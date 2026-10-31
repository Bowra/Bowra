"""Original question/answer/worker continuation retains the accepted Datarc source."""

import sqlite3
from types import SimpleNamespace

import pytest
from bowhead_core.datarc_connection_store import DatarcConnectionStore
from bowhead_core.datarc_selection import DatarcPreferenceScope
from bowhead_core.subagent import SubagentRunner
from bowhead_core.user_memory import UserMemoryStore
from bowhead_gateway.connector_context_service import ConnectorContextService
from bowhead_gateway.member_resolver import MemberUidResolver
from bowhead_gateway.routes import chat, clarifications
from test_ordinary_lead_question_http import test_original_answer_continues_non_document_tools_without_replaying_helper as original_flow
from test_reference_http_start import http_start as http_start
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case

from bowhead_gateway import datarc_context, turn_service

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]


class SourceClient:
    async def fetch_account(self, email):
        return {"uid": "account_one"}

    async def list_account_projects(self, account_uid, **kwargs):
        return [{"id": "1", "name": "Source A"}, {"id": "2", "name": "Source B"}]

    async def fetch_workspace_member(self, *, datarc_workspace_id, username):
        return {"uid": "member_" + datarc_workspace_id}


async def test_native_http_resume_ignores_a_new_default_and_recreates_source_services(http_start, monkeypatch, caplog):
    case, scope = http_start, http_start.fixture.scope
    with sqlite3.connect(case.path) as db:
        db.execute("UPDATE users SET email='actor@example.test', email_verified=1 WHERE id=?", (scope.user_id,))
    memory = UserMemoryStore(case.path)
    connection_store = DatarcConnectionStore(case.path)
    connection = await connection_store.create_connection(
        team_id=scope.team_id, name="Source", base_url="https://source.datarc.example", auth_key="test-key"
    )
    preference = DatarcPreferenceScope(scope.team_id, connection.id)
    assert await memory.commit_datarc_workspace(scope.user_id, preference, context_id="1", remember=True, session_id=None)
    client = SourceClient()
    state = case.app.state
    state.user_memory_store, state.datarc_connection_store = memory, connection_store
    state.datarc_client_factory = SimpleNamespace(get_client=lambda *_: client)
    state.member_resolver = MemberUidResolver()
    state.tool_context_service = ConnectorContextService(user_store=state.user_store, user_memory_store=memory)
    monkeypatch.setattr(chat, "resolve_datarc_turn_context", datarc_context.resolve_datarc_turn_context)
    original_resume, original_spawn = clarifications.resume_turn, turn_service.spawn_resume_turn
    saved, admitted = [], []

    async def recreate_before_resume(request_id, request, response, clarify):
        if not saved:
            card = await clarify.get(request_id)
            snapshot = datarc_context.clarification_runtime_context(card, db_path=case.path)
            saved.append(snapshot["datarc_selection"])
            assert saved[0]["datarc_workspace_id"] == "1"
            assert await memory.commit_datarc_workspace(scope.user_id, preference, context_id="2", remember=True, session_id="another_chat")
            state.user_memory_store = UserMemoryStore(case.path)
            state.member_resolver = MemberUidResolver()
            state.tool_context_service = ConnectorContextService(user_store=state.user_store, user_memory_store=state.user_memory_store)
            state.orchestrator.subagent_runner = SubagentRunner(
                router=state.orchestrator.router,
                tool_registry=state.orchestrator.tool_registry,
                team_subagent_store=state.user_store,
            )
        return await original_resume(request_id, request, response, clarify)

    async def observe_spawn(orchestrator, turn_request, **kwargs):
        admitted.append((turn_request.active_datarc_project, turn_request.member_uid, turn_request.datarc_source_evidence))
        return await original_spawn(orchestrator, turn_request, **kwargs)

    monkeypatch.setattr(clarifications, "resume_turn", recreate_before_resume)
    monkeypatch.setattr(turn_service, "spawn_resume_turn", observe_spawn)
    await original_flow(case, monkeypatch, caplog, "custom")
    assert len(admitted) == 1
    assert admitted[0][:2] == ("1", "member_1")
    assert admitted[0][2] == saved[0]
    assert (await memory.get_datarc_workspace(scope.user_id, preference))["id"] == "2"
