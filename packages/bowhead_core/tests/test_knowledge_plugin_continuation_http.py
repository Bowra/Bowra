"""A direct KB read survives the real clarification route without repeating searches."""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import respx
from bowhead_core.agent_knowledge_policy import require_supported_knowledge_access
from bowhead_core.agents.clarification_tool import register_clarification_tool
from bowhead_core.subagent import PROFILES
from bowhead_core.tool_registry import ToolSpec
from bowhead_gateway.routes import clarifications
from bowhead_router.types import ToolSchema
from test_ordinary_lead_question_http import call, response
from test_reference_http_start import enable_lead_writer, rows, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case

from bowhead_core import events

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("restricted,revoked", [(False, False), (True, False), (True, True)])
async def test_direct_search_question_resume_retains_policy_budget_and_results(http_start, monkeypatch, parallel, restricted, revoked):
    case = http_start
    enable_lead_writer(case, monkeypatch)
    monkeypatch.setenv("FEATURE_KNOWLEDGE_PLUGIN_DIRECT_USE", "1")
    monkeypatch.setenv("FEATURE_CHAT_MULTI_HELPER_QUESTION", "1")
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "1")
    monkeypatch.setitem(
        PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("kb_search", "clarification_ask", "skill_list"), max_iterations=12)
    )
    orchestrator = case.app.state.orchestrator
    if restricted:
        monkeypatch.setenv("FEATURE_KNOWLEDGE_SOURCE_BINDINGS", "1")
        policy = require_supported_knowledge_access({"knowledge_access": {"schema_version": 1, "mode": "selected_files", "file_ids": ["kbf_source"]}})
        monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], knowledge_access=policy))
        file = SimpleNamespace(
            file_id="kbf_source",
            scope="space",
            scope_id=case.fixture.scope.workspace_id,
            team_id=case.fixture.scope.team_id,
            soft_deleted_at=None,
            active_generation_id=None,
        )
        orchestrator.kb_files_store = SimpleNamespace(get=AsyncMock(return_value=file))
        orchestrator.kb_file_authorizer = AsyncMock(return_value=True)
    searches = []

    async def search(args, _ctx):
        searches.append(args["query"])
        return {"chunks": [{"file_id": "kbf_source", "content": "Revenue is ten."}]} if restricted else {"chunks": []}

    orchestrator.tool_registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="kb_search", description="Search knowledge.", input_schema={"type": "object", "properties": {"query": {"type": "string"}}}
            ),
            handler=search,
        )
    )
    register_clarification_tool(orchestrator.tool_registry)
    monkeypatch.setattr(events, "_default_registry", orchestrator.event_registry)
    case.app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    requests = []
    question = {"question": "Which audience?", "type": "free_text", "options": []}

    def respond(request):
        body = json.loads(request.content)
        names = {tool["function"]["name"] for tool in body.get("tools", [])}
        if "clarification_ask" not in names:
            return response(body, {"role": "assistant", "content": "Knowledge lookup"}, "stop")
        requests.append(body)
        step = len(requests)
        if step in {1, 2, 4, 5}:
            message = call(f"search-{step}", "kb_search", {"query": f"topic-{step}"})
            if parallel and step <= 2:
                message["tool_calls"].extend(call(f"parallel-{step}", "kb_search", {"query": f"related-{step}"})["tool_calls"])
                message["tool_calls"][1]["index"] = 1
            return response(body, message, "tool_calls")
        if step == 3:
            return response(body, call("question", "clarification_ask", {"questions": [question]}), "tool_calls")
        if step == 6:
            return response(body, call("skills", "skill_list", {}), "tool_calls")
        assert step == 7
        return response(
            body, {"role": "assistant", "content": "The Skills are listed. No supporting KB evidence was found for the board's question."}, "stop"
        )

    case.client.headers["Content-Type"] = "application/json"
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post(
            "/api/v1/chat/send",
            json={"workspace_id": case.fixture.scope.workspace_id, "message": "Read the knowledge base, ask which audience, then list Skills."},
        )
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
        cards = [row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id]
        assert len(cards) == 1, [(row.get("event_type"), row.get("payload_json")) for row in rows(case, "turn_events") if row["turn_id"] == turn_id]
        card = cards[0]
        assert next(row for row in rows(case, "turns") if row["id"] == turn_id)["knowledge_user_request"] == 1
        if restricted:
            monkeypatch.setenv("FEATURE_KNOWLEDGE_SOURCE_BINDINGS", "0")
            if revoked:
                file.soft_deleted_at = "deleted"
        # Use the public route to exercise its sealed-state reader and current-access checks.
        monkeypatch.setenv("FEATURE_KNOWLEDGE_PLUGIN_DIRECT_USE", "0")
        answered = await case.client.post(
            f"/api/v1/clarifications/{card['request_id']}/resolve",
            json={"answers": [{"question": question["question"], "source": "custom", "answer": "Board members"}]},
        )
        assert answered.status_code == 200, answered.text
        assert answered.json()["resume_turn_id"] == turn_id
        await settled(turn_id)
    if revoked:
        assert len(requests) == 3
        turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
        assert turn["status"] != "completed"
        return
    expected = ["topic-1", "related-1", "topic-2", "related-2", "topic-4"] if parallel else ["topic-1", "topic-2", "topic-4"]
    if restricted:
        expected.append("topic-5")
    assert sorted(searches) == sorted(expected)
    assert len(requests) == 7
    assert "topic-1" in json.dumps(requests[3]["messages"])
    assert "kb_search" in {tool["function"]["name"] for tool in requests[3]["tools"]}
    assert "kb_search" not in {tool["function"]["name"] for tool in requests[5 if restricted else 4]["tools"]}
    assert "skill_list" in {tool["function"]["name"] for tool in requests[5]["tools"]}
    turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
    assert turn["status"] == "completed", turn


@pytest.mark.parametrize("metadata_changed", [False, True])
async def test_source_navigation_only_question_retains_current_metadata_authority(
    http_start, initialize_migrated_kb_files_database, monkeypatch, tmp_path, metadata_changed
):
    from bowhead_core.kb.files_store import KbFilesStore
    from bowhead_core.kb.generations import KbGenerationStore
    from bowhead_core.kb.source_navigation_store import SourceNavigationStore
    from bowhead_gateway.kb_authorization import kb_file_authorizer

    case = http_start
    enable_lead_writer(case, monkeypatch)
    monkeypatch.setenv("FEATURE_KNOWLEDGE_PLUGIN_DIRECT_USE", "1")
    monkeypatch.setenv("FEATURE_CHAT_MULTI_HELPER_QUESTION", "1")
    monkeypatch.setitem(
        PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("kb_search", "kb_list_sources", "clarification_ask"), max_iterations=8)
    )
    scope = case.fixture.scope
    initialize_migrated_kb_files_database(tmp_path / "navigation-files.db")
    files = KbFilesStore(tmp_path / "navigation-files.db")
    await files.initialize()
    identity = await files.create(
        artifact_id="art_navigation",
        filename="Travel policy.md",
        mime_type="text/markdown",
        size_bytes=100,
        scope="space",
        scope_id=scope.workspace_id,
        workspace_id=scope.workspace_id,
        team_id=scope.team_id,
        uploaded_by=scope.user_id,
    )
    file = await files.get(identity)
    generations = KbGenerationStore(files._db_path)
    generation = await generations.begin(file, "a" * 64, 1, "index-a")
    assert await generations.publish(file, generation, layer="L0", duration_ms=1, summary_json=None)
    file = await files.get(identity)
    nav = SourceNavigationStore(files._db_path)
    await nav.edit(file, revision=0, actor_user_id=scope.user_id, manual_text="Travel approvals and reimbursement.", edit_text=True)
    orchestrator = case.app.state.orchestrator
    orchestrator.kb_files_store = case.app.state.kb_files_store = files
    orchestrator.kb_file_authorizer = kb_file_authorizer(case.app.state.user_store, case.app.state.access_control_store)
    register_clarification_tool(orchestrator.tool_registry)
    monkeypatch.setattr(events, "_default_registry", orchestrator.event_registry)
    case.app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    requests = []
    question = {"question": "Which audience?", "type": "free_text", "options": []}

    def respond(request):
        body = json.loads(request.content)
        if "clarification_ask" not in {tool["function"]["name"] for tool in body.get("tools", [])}:
            return response(body, {"role": "assistant", "content": "Navigation question"}, "stop")
        requests.append(body)
        if len(requests) == 1:
            assert "Travel approvals and reimbursement." in json.dumps(body)
            return response(body, call("question", "clarification_ask", {"questions": [question]}), "tool_calls")
        return response(body, {"role": "assistant", "content": "The intended audience is board members."}, "stop")

    case.client.headers["Content-Type"] = "application/json"
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post(
            "/api/v1/chat/send", json={"workspace_id": scope.workspace_id, "message": "Read the knowledge base, ask which audience, then stop."}
        )
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
        cards = [row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id]
        assert cards, {
            "requests": len(requests),
            "tool_results": [message["content"] for request in requests for message in request["messages"] if message["role"] == "tool"][-2:],
        }
        card = cards[0]
        if metadata_changed:
            await nav.edit(file, revision=1, actor_user_id=scope.user_id, manual_text="Revised travel scope.", edit_text=True)
            assert (await files.get(identity)).active_generation_id == generation
        answered = await case.client.post(
            f"/api/v1/clarifications/{card['request_id']}/resolve",
            json={"answers": [{"question": question["question"], "source": "custom", "answer": "Board members"}]},
        )
        assert answered.status_code == 200, answered.text
        await settled(turn_id)
    turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
    assert len(requests) == (1 if metadata_changed else 2)
    assert (turn["status"] == "completed") is not metadata_changed
