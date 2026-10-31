"""A native helper workspace picker pauses and resumes the original Chat turn."""

import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import anyio
import pytest
import respx
from bowhead_bi_bridge.types import CutResult, DimensionMeta, DownloadDataResult, MeasureMeta
from bowhead_core.agents import AgentRegistry
from bowhead_core.agents.agent_runner_adapter import manifest_to_profile
from bowhead_core.agents.clarification_tool import register_clarification_tool
from bowhead_core.datarc_connection_store import DatarcConnectionStore
from bowhead_core.datarc_selection import DatarcPreferenceScope
from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentRunner
from bowhead_core.tool_registry import apply_paradigm_v2_role_bindings
from bowhead_core.user_memory import UserMemoryStore
from bowhead_gateway.connector_context_service import ConnectorContextService
from bowhead_gateway.member_resolver import MemberUidResolver
from bowhead_gateway.routes import chat, clarifications
from test_datarc_ordinary_continuation_http import SourceClient
from test_ordinary_lead_question_http import call, response
from test_reference_http_start import enable_lead_writer, rows, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case

from bowhead_core import events
from bowhead_gateway import datarc_context
from bowhead_router import AgentBinding

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]


@pytest.mark.parametrize("restart", [False, True], ids=["after-list", "recreated-services"])
async def test_helper_native_switch_continues_its_admitted_task_after_selection(http_start, monkeypatch, restart):
    case, scope = http_start, http_start.fixture.scope
    case.client.headers["Content-Type"] = "application/json"
    enable_lead_writer(case, monkeypatch)
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], max_iterations=4))
    monkeypatch.setitem(
        PROFILES,
        "compute",
        SubagentProfile(
            name="compute",
            llm_agent_type="compute",
            max_iterations=6,
            tool_whitelist=("tool_list_workspaces", "tool_switch_context", "bi_schema", "bi_query", "subagent_return"),
            system_prompt="Inspect the selected source after the user resolves the native switch.",
        ),
    )
    source = await _prepare_source(case, monkeypatch, target_id="87")
    apply_paradigm_v2_role_bindings(source.state.orchestrator.tool_registry)
    preference = DatarcPreferenceScope(scope.team_id, source.connection.id)
    await source.memory.commit_datarc_workspace(scope.user_id, preference, context_id="1", context_name="Source A", remember=True, session_id=None)
    lead_calls, helper_calls = [], []
    switch_index = 1 if restart else 2

    def respond(request):
        body = json.loads(request.content)
        names = {tool["function"]["name"] for tool in body.get("tools", [])}
        if "subagent_spawn" in names:
            lead_calls.append(body)
            if len(lead_calls) == 1:
                return response(
                    body,
                    call("original-delegation", "subagent_spawn", {"profile": "compute", "task": "Switch to Source, then inspect it."}),
                    "tool_calls",
                )
            returned = [json.loads(message["content"]) for message in body["messages"] if message.get("name") == "subagent_spawn"]
            assert any("Source B inspected" in json.dumps(result) for result in returned)
            return response(body, {"role": "assistant", "content": "Source B inspected."}, "stop")
        if "bi_schema" in names:
            helper_calls.append(body)
            if len(helper_calls) < switch_index:
                return response(body, call("helper-list", "tool_list_workspaces", {}), "tool_calls")
            if len(helper_calls) == switch_index:
                return response(body, call("helper-switch", "tool_switch_context", {"name": "Source", "remember": False}), "tool_calls")
            if len(helper_calls) == switch_index + 1:
                outcome = json.loads(next(message["content"] for message in body["messages"] if message.get("tool_call_id") == "helper-switch"))
                assert outcome["switched"] == {"id": "87", "name": "Source B"}
                return response(body, call("selected-schema", "bi_schema", {}), "tool_calls")
            schema = json.loads(next(message["content"] for message in body["messages"] if message.get("tool_call_id") == "selected-schema"))
            assert schema["data_source"]["context_id"] == "87"
            if len(helper_calls) == switch_index + 2:
                return response(body, call("selected-query", "bi_query", {"text": "2025-09 Sales"}), "tool_calls")
            query = json.loads(next(message["content"] for message in body["messages"] if message.get("tool_call_id") == "selected-query"))
            assert query["data_source"]["context_id"] == "87"
            assert query["rows"] == [{"Sales": 1288.0}]
            return response(
                body,
                call("returned", "subagent_return", {"payload": {"delivery_surface": "chat", "exec_summary": "Source B inspected."}}),
                "tool_calls",
            )
        return response(body, {"role": "assistant", "content": "Sales"}, "stop")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post(
            "/api/v1/chat/send", json={"workspace_id": scope.workspace_id, "message": "Delegate the native source switch and inspection."}
        )
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
        card = next(row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id)
        assert card["status"] == "pending"
        assert not source.source_reads
        if restart:
            source.state.member_resolver = MemberUidResolver()
            source.state.tool_context_service = ConnectorContextService(
                user_store=source.state.user_store, user_memory_store=UserMemoryStore(case.path)
            )
            source.state.orchestrator.subagent_runner = SubagentRunner(
                router=source.state.orchestrator.router,
                tool_registry=source.state.orchestrator.tool_registry,
                team_subagent_store=source.state.user_store,
            )
        answered = await case.client.post(f"/api/v1/clarifications/{card['request_id']}/resolve", json={"chosen_value": "87", "remember": False})
        assert answered.status_code == 200, answered.text
        await settled(turn_id)
        duplicate = await case.client.post(f"/api/v1/clarifications/{card['request_id']}/resolve", json={"chosen_value": "87", "remember": False})
        assert duplicate.status_code == 200, duplicate.text

    turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
    assert turn["status"] == "completed", turn["error"]
    assert turn["execution_generation"] == 2
    assert len(lead_calls) == 2 and len(helper_calls) == switch_index + 3
    assert source.source_reads.count(("query", "87")) == 1
    assert {workspace for _, workspace in source.source_reads} == {"87"}
    assert {kind for kind, _ in source.source_reads} == {"measures", "dimensions", "query"}
    invocations = [
        json.loads(row["payload_json"]) for row in rows(case, "turn_events") if row["turn_id"] == turn_id and row["event_type"] == "tool_result"
    ]
    assert sum(item.get("tool_name") == "tool_switch_context" for item in invocations) == 1
    assert sum(item.get("tool_name") == "subagent_spawn" for item in invocations) == 2
    assert sum(item.get("tool_name") == "bi_query" for item in invocations) == 1
    helpers = [row for row in rows(case, "turn_agent_execution") if row["turn_id"] == turn_id and row["runtime_profile_name"] == "compute"]
    assert sorted(row["status"] for row in helpers) == ["blocked", "completed"]
    assert (await source.memory.get_datarc_workspace(scope.user_id, preference))["id"] == "1"
    assert (await source.memory.get_datarc_workspace(scope.user_id, preference, session_id=turn["session_id"]))["id"] == "87"


async def _prepare_source(case, monkeypatch, *, target_id="2"):
    scope = case.fixture.scope
    with sqlite3.connect(case.path) as db:
        db.execute("UPDATE users SET email='actor@example.test', email_verified=1 WHERE id=?", (scope.user_id,))
    memory = UserMemoryStore(case.path)
    connections = DatarcConnectionStore(case.path)
    connection = await connections.create_connection(
        team_id=scope.team_id, name="Source", base_url="https://source.datarc.example", auth_key="test-key"
    )
    state = case.app.state
    state.user_memory_store, state.datarc_connection_store = memory, connections
    source_reads = []
    denied = set()

    class Client(SourceClient):
        async def list_account_projects(self, account_uid, **kwargs):
            return [item for item in [{"id": "1", "name": "Source A"}, {"id": target_id, "name": "Source B"}] if item["id"] not in denied]

        async def download_data(self, *, datarc_workspace_id, member_uid, text):
            assert member_uid == "member_" + datarc_workspace_id
            source_reads.append(("query", datarc_workspace_id))
            return DownloadDataResult(columns=["Sales"], rows=[{"Sales": 1288.0}], total_rows=1)

        async def cut_question(self, **kwargs):
            return CutResult(tokens=[])

        async def list_measures(self, workspace_id: str) -> list[MeasureMeta]:
            source_reads.append(("measures", workspace_id))
            return [MeasureMeta(id=1, names=["Sales"], used=True)]

        async def list_dimensions(self, workspace_id: str) -> list[DimensionMeta]:
            source_reads.append(("dimensions", workspace_id))
            return [DimensionMeta(id=2, names=["Store"], used=True, is_keyword=True)]

        async def check_token_permissions(self, workspace_id: str, member_uid: str, tokens: list[dict]) -> list[str]:
            assert workspace_id in {"1", target_id}
            assert member_uid == "member_" + workspace_id
            return ["normal"] * len(tokens)

    state.datarc_client_factory = SimpleNamespace(get_client=lambda *_: Client())
    state.member_resolver = MemberUidResolver()
    state.tool_context_service = ConnectorContextService(user_store=state.user_store, user_memory_store=memory)
    monkeypatch.setattr(chat, "resolve_datarc_turn_context", datarc_context.resolve_datarc_turn_context)
    monkeypatch.setattr(events, "_default_registry", state.orchestrator.event_registry)
    case.app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    return SimpleNamespace(state=state, memory=memory, connection=connection, source_reads=source_reads, denied=denied)


@pytest.mark.parametrize("restart", [False, True], ids=["live", "recreated-services"])
async def test_helper_workspace_picker_pauses_without_empty_result_and_resumes(http_start, monkeypatch, restart):
    case, scope = http_start, http_start.fixture.scope
    case.client.headers["Content-Type"] = "application/json"
    enable_lead_writer(case, monkeypatch)
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], max_iterations=3))
    monkeypatch.setitem(
        PROFILES,
        "compute",
        SubagentProfile(
            name="compute", llm_agent_type="compute", tool_whitelist=("bi_schema", "subagent_return"), system_prompt="Inspect the source."
        ),
    )
    source = await _prepare_source(case, monkeypatch)
    state, source_reads = source.state, source.source_reads
    lead_calls, helper_calls = [], []
    live_events = []
    original_emit = events.TurnEventBus.emit

    async def observe_emit(bus, event):
        live_events.append(event)
        return await original_emit(bus, event)

    monkeypatch.setattr(events.TurnEventBus, "emit", observe_emit)

    def respond(request):
        body = json.loads(request.content)
        names = {item["function"]["name"] for item in body.get("tools", [])}
        if "subagent_spawn" in names:
            lead_calls.append(body)
            if len(lead_calls) > 2:
                return response(body, {"role": "assistant", "content": "Source A selected."}, "stop")
            return response(body, call(f"helper-{len(lead_calls)}", "subagent_spawn", {"profile": "compute", "task": "Inspect sales."}), "tool_calls")
        if "bi_schema" in names:
            helper_calls.append(body)
            if len(helper_calls) <= 2:
                return response(body, call(f"schema-{len(helper_calls)}", "bi_schema", {}), "tool_calls")
            schema = json.loads(next(message["content"] for message in body["messages"] if message.get("tool_call_id") == "schema-2"))
            assert schema["data_source"] == {"tool": "datarc", "context_id": "1", "context_name": "Source A"}
            assert len(schema["measures"]) == len(schema["dimensions"]) == 1
            return response(
                body, call("answer", "subagent_return", {"payload": {"delivery_surface": "chat", "exec_summary": "Source A selected."}}), "tool_calls"
            )
        return response(body, {"role": "assistant", "content": "Sales"}, "stop")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post("/api/v1/chat/send", json={"workspace_id": scope.workspace_id, "message": "Show sales."})
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
        turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
        assert turn["status"] == "clarification_pending", turn["error"]
        assert len(lead_calls) == len(helper_calls) == 1
        assert source_reads == []
        assert (turn["tokens_input"], turn["tokens_output"]) == (34, 18)
        executions = [row for row in rows(case, "turn_agent_execution") if row["turn_id"] == turn_id]
        owner = next(row for row in executions if row["agent_id"] == "system:lead")
        assert owner["status"] == "running"
        assert next(row for row in executions if row["agent_id"] == "system:compute")["status"] == "blocked", executions
        saved_events = [row for row in rows(case, "turn_events") if row["turn_id"] == turn_id]
        assert not any(row["event_type"] == "peer_dispatch_failed" for row in saved_events)
        assert not any(row["event_type"] == "tool_finding" for row in saved_events)
        cards = [row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id]
        assert len(cards) == 1
        assert cards[0]["kind"] == "tool_context_select"
        assert cards[0]["multi_select"] == 0
        question = next(json.loads(row["payload_json"]) for row in saved_events if row["event_type"] == "clarification_required")
        assert question["options"] == [{"value": "1", "label": "Source A"}, {"value": "2", "label": "Source B"}]
        assert next(event for event in live_events if event.event_type == "clarification_required").payload == question
        with anyio.fail_after(5):
            replay = await case.client.get(accepted.json()["stream_url"])
        assert replay.status_code == 200
        replayed = [json.loads(line[6:]) for line in replay.text.splitlines() if line.startswith("data: ")]
        assert (
            next(item for item in replayed if item.get("clarification_id") == cards[0]["request_id"] and "options" in item)["options"]
            == question["options"]
        )
        if restart:
            state.user_memory_store = UserMemoryStore(case.path)
            state.member_resolver = MemberUidResolver()
            state.tool_context_service = ConnectorContextService(user_store=state.user_store, user_memory_store=state.user_memory_store)
            state.orchestrator.subagent_runner = SubagentRunner(
                router=state.orchestrator.router, tool_registry=state.orchestrator.tool_registry, team_subagent_store=state.user_store
            )
        answered = await case.client.post(f"/api/v1/clarifications/{cards[0]['request_id']}/resolve", json={"chosen_value": "1"})
        assert answered.status_code == 200, answered.text
        await settled(turn_id)

    turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
    assert turn["status"] == "completed", turn["error"]
    assert turn["execution_generation"] == 2
    assert len(lead_calls) in (2, 3)
    assert len(helper_calls) == 3
    assert "Source A selected." in turn["final_answer"]
    assert sorted(source_reads) == [("dimensions", "1"), ("measures", "1")]
    executions = [row for row in rows(case, "turn_agent_execution") if row["turn_id"] == turn_id]
    assert next(row for row in executions if row["id"] == owner["id"])["status"] == "completed"
    replay_answer = await case.client.post(f"/api/v1/clarifications/{cards[0]['request_id']}/resolve", json={"chosen_value": "1"})
    assert replay_answer.status_code == 200
    assert next(row for row in rows(case, "turns") if row["id"] == turn_id)["execution_generation"] == 2
    assert len(helper_calls) == 3


@pytest.mark.parametrize("switch", [False, True], ids=["saved-default", "requested-switch"])
async def test_compute_uses_saved_source_or_calls_existing_switch_tools(http_start, monkeypatch, switch):
    case, scope = http_start, http_start.fixture.scope
    case.client.headers["Content-Type"] = "application/json"
    enable_lead_writer(case, monkeypatch)
    profile = manifest_to_profile(AgentRegistry.bootstrap().get("compute"))
    monkeypatch.setitem(PROFILES, "compute", profile)
    source = await _prepare_source(case, monkeypatch)
    orch = source.state.orchestrator
    orch.router.bind(AgentBinding(agent_type="analyst", primary=("primary", "large")))
    apply_paradigm_v2_role_bindings(orch.tool_registry)
    preference = DatarcPreferenceScope(scope.team_id, source.connection.id)
    await source.memory.commit_datarc_workspace(scope.user_id, preference, context_id="1", context_name="Source A", remember=True, session_id=None)
    target_id, target_name = ("2", "Source B") if switch else ("1", "Source A")
    task = f"Use {target_name} to inspect sales." if switch else "Inspect sales."
    lead_calls, helper_calls = [], []
    expected_calls = ["tool_list_workspaces", "tool_switch_context", "bi_schema", "subagent_return"] if switch else ["bi_schema", "subagent_return"]

    def respond(request):
        body = json.loads(request.content)
        names = {item["function"]["name"] for item in body.get("tools", [])}
        if "subagent_spawn" in names:
            lead_calls.append(body)
            if len(lead_calls) == 1:
                return response(body, call("compute-source", "subagent_spawn", {"profile": "compute", "task": task}), "tool_calls")
            return response(body, {"role": "assistant", "content": target_name + " inspected."}, "stop")
        if "bi_schema" in names:
            helper_calls.append(body)
            tool = expected_calls[len(helper_calls) - 1]
            arguments = {"name": target_name} if tool == "tool_switch_context" else {}
            if tool == "subagent_return":
                arguments = {"payload": {"delivery_surface": "chat", "exec_summary": target_name + " inspected."}}
            return response(body, call("source-" + tool, tool, arguments), "tool_calls")
        return response(body, {"role": "assistant", "content": "Sales"}, "stop")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post("/api/v1/chat/send", json={"workspace_id": scope.workspace_id, "message": task})
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)

    turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
    assert turn["status"] == "completed", turn["error"]
    assert turn["execution_generation"] == 1
    assert not [row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id]
    assert len(helper_calls) == len(expected_calls)
    available = {item["function"]["name"] for item in helper_calls[0]["tools"]}
    assert {"tool_list_workspaces", "tool_switch_context"} <= available
    tool_results = [
        json.loads(row["payload_json"]) for row in rows(case, "turn_events") if row["turn_id"] == turn_id and row["event_type"] == "tool_result"
    ]
    source_results = [result for result in tool_results if result.get("tool_name") in expected_calls]
    assert [result["tool_name"] for result in source_results] == expected_calls
    assert all(result["ok"] for result in source_results)
    schema = json.loads(next(message["content"] for message in helper_calls[-1]["messages"] if message.get("tool_call_id") == "source-bi_schema"))
    assert schema["data_source"] == {"tool": "datarc", "context_id": target_id, "context_name": target_name}
    assert sorted(source.source_reads) == [("dimensions", target_id), ("measures", target_id)]
    assert (await source.memory.get_datarc_workspace(scope.user_id, preference))["id"] == "1"
    overlay = await source.memory.get_datarc_workspace(scope.user_id, preference, session_id=turn["session_id"])
    assert (overlay["id"] if overlay else None) == ("2" if switch else None)


@pytest.mark.parametrize(
    "selection", ["ordinary-same", "ordinary-other", "native-switch", "native-after-list", "native-invalid", "native-revoked", "native-override"]
)
async def test_workspace_answer_restores_list_switch_and_helper_reads(http_start, monkeypatch, selection):
    native = selection.startswith("native-")
    target_id = "1" if selection == "ordinary-same" else "2"
    case, scope = http_start, http_start.fixture.scope
    case.client.headers["Content-Type"] = "application/json"
    enable_lead_writer(case, monkeypatch)
    monkeypatch.setitem(
        PROFILES,
        "lead",
        replace(
            PROFILES["lead"], max_iterations=8, tool_whitelist=("tool_list_workspaces", "clarification_ask", "tool_switch_context", "subagent_spawn")
        ),
    )
    monkeypatch.setitem(
        PROFILES,
        "compute",
        SubagentProfile(
            name="compute", llm_agent_type="compute", tool_whitelist=("bi_schema", "subagent_return"), system_prompt="Inspect the requested source."
        ),
    )
    source = await _prepare_source(case, monkeypatch)
    register_clarification_tool(source.state.orchestrator.tool_registry)
    preference = DatarcPreferenceScope(scope.team_id, source.connection.id)
    await source.memory.commit_datarc_workspace(scope.user_id, preference, context_id="1", context_name="Source A", remember=True, session_id=None)
    target_name = "Source A" if target_id == "1" else "Source B"
    question = "Which source should this Chat use?"
    lead_calls, helper_calls = [], []
    expected_calls = (
        ["tool_switch_context", "tool_list_workspaces", "subagent_spawn"]
        if native
        else ["tool_list_workspaces", "clarification_ask", "tool_list_workspaces", "tool_switch_context", "subagent_spawn"]
    )
    if selection == "native-after-list":
        expected_calls.insert(0, "tool_list_workspaces")
    switch_call_id = f"source-step-{expected_calls.index('tool_switch_context') + 1}"

    def respond(request):
        body = json.loads(request.content)
        names = {item["function"]["name"] for item in body.get("tools", [])}
        if "subagent_spawn" in names:
            lead_calls.append(body)
            if native and len(lead_calls) == expected_calls.index("tool_switch_context") + 2:
                resolved = [message for message in body["messages"] if message.get("tool_call_id") == switch_call_id]
                assert len(resolved) == 1
                outcome = json.loads(resolved[0]["content"])
                assert outcome["ok"] and outcome["switched"] == {"id": target_id, "name": target_name}
                assert outcome["persistence"] == {"status": "saved", "scope": "session", "reason": ""}
                assert outcome["selection"] == {"kind": "tool_context_select", "status": "selected", "candidate_count": 2, "selected_by": "user"}
                original_call = next(call for message in body["messages"] for call in message.get("tool_calls", []) if call["id"] == switch_call_id)
                assert original_call["function"]["name"] == "tool_switch_context"
                assert json.loads(original_call["function"]["arguments"]) == {"name": "Source", "remember": selection == "native-override"}
            if len(lead_calls) > len(expected_calls):
                return response(body, {"role": "assistant", "content": target_name + " inspected."}, "stop")
            tool = expected_calls[len(lead_calls) - 1]
            arguments = {}
            if tool == "clarification_ask":
                arguments = {"questions": [{"question": question, "type": "single_select", "options": ["Source A", "Source B"]}]}
            elif tool == "tool_switch_context":
                arguments = {"name": "Source" if native else target_name, "remember": selection == "native-override"}
            elif tool == "subagent_spawn":
                arguments = {"profile": "compute", "task": "Inspect " + target_name + "."}
            return response(body, call(f"source-step-{len(lead_calls)}", tool, arguments), "tool_calls")
        if "bi_schema" in names:
            helper_calls.append(body)
            if native:
                assert not any(message.get("tool_call_id") == switch_call_id for message in body["messages"])
            if len(helper_calls) == 1:
                return response(body, call("schema-after-answer", "bi_schema", {}), "tool_calls")
            schema = json.loads(next(m["content"] for m in body["messages"] if m.get("tool_call_id") == "schema-after-answer"))
            assert schema["data_source"] == {"tool": "datarc", "context_id": target_id, "context_name": target_name}
            return response(
                body,
                call("answer", "subagent_return", {"payload": {"delivery_surface": "chat", "exec_summary": target_name + " inspected."}}),
                "tool_calls",
            )
        return response(body, {"role": "assistant", "content": "Sales"}, "stop")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post(
            "/api/v1/chat/send",
            json={"workspace_id": scope.workspace_id, "message": "Let me choose a source, then inspect it without changing my default."},
        )
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
        turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
        assert turn["status"] == "clarification_pending", turn["error"]
        cards = [row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id]
        assert len(cards) == 1
        assert (cards[0]["kind"] == "tool_context_select") == native
        if native:
            context = json.loads(cards[0]["resume_context_json"])
            assert context["datarc_switch_call"]["call_id"] == switch_call_id
        assert helper_calls == []
        source.source_reads.clear()
        source.state.user_memory_store = UserMemoryStore(case.path)
        source.state.member_resolver = MemberUidResolver()
        source.state.tool_context_service = ConnectorContextService(
            user_store=source.state.user_store, user_memory_store=source.state.user_memory_store
        )
        source.state.orchestrator.subagent_runner = SubagentRunner(
            router=source.state.orchestrator.router,
            tool_registry=source.state.orchestrator.tool_registry,
            team_subagent_store=source.state.user_store,
        )
        if selection == "native-invalid":
            rejected = await case.client.post(
                f"/api/v1/clarifications/{cards[0]['request_id']}/resolve",
                json={"chosen_value": "Unavailable source", "remember": False},
            )
            assert rejected.status_code == 422, rejected.text
            assert "clarification_context_selection_invalid" in rejected.text
            retained = next(row for row in rows(case, "clarification_requests") if row["request_id"] == cards[0]["request_id"])
            assert retained["status"] == "pending"
            assert not any(row["event_type"] == "clarification.resolved" for row in rows(case, "turn_events"))
            restored = await case.client.get(f"/api/v1/clarifications/{cards[0]['request_id']}")
            assert restored.status_code == 200 and restored.json()["status"] == "pending"
            multiple = await case.client.post(
                f"/api/v1/clarifications/{cards[0]['request_id']}/resolve", json={"chosen_values": ["1", "2"], "remember": False}
            )
            assert multiple.status_code == 422
        if selection == "native-revoked":
            source.denied.add(target_id)
            rejected = await case.client.post(
                f"/api/v1/clarifications/{cards[0]['request_id']}/resolve", json={"chosen_value": target_id, "remember": False}
            )
            assert rejected.status_code == 409 and "clarification_context_selection_unavailable" in rejected.text
            assert next(row["status"] for row in rows(case, "clarification_requests") if row["request_id"] == cards[0]["request_id"]) == "pending"
            source.denied.clear()
        answered = await case.client.post(
            f"/api/v1/clarifications/{cards[0]['request_id']}/resolve",
            json={"chosen_value": target_name if selection == "native-invalid" else target_id, "remember": False}
            if native
            else {"answers": [{"question": question, "source": "candidate", "answer": target_name, "option_index": int(target_id) - 1}]},
        )
        assert answered.status_code == 200, answered.text
        await settled(turn_id)

    turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
    assert turn["status"] == "completed", turn["error"]
    assert turn["execution_generation"] == 2
    results = [
        json.loads(row["payload_json"]) for row in rows(case, "turn_events") if row["turn_id"] == turn_id and row["event_type"] == "tool_result"
    ]
    listings = [result["result_inline"] for result in results if result.get("tool_name") == "tool_list_workspaces"]
    assert len(listings) == (1 if selection in {"native-switch", "native-invalid", "native-revoked", "native-override"} else 2)
    assert all(result["ok"] and result["total"] == 2 for result in listings)
    assert listings[-1]["current"] == (target_name if native else "Source A")
    assert len([result for result in results if result.get("tool_name") == "tool_switch_context"]) == 1
    if not native:
        switched = next(result["result_inline"] for result in results if result.get("tool_name") == "tool_switch_context")
        assert switched["ok"] and switched["persistence"]["status"] == "saved"
    assert len([row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id]) == 1
    assert sorted(source.source_reads) == [("dimensions", target_id), ("measures", target_id)]
    assert (await source.memory.get_datarc_workspace(scope.user_id, preference))["id"] == "1"
    assert (await source.memory.get_datarc_workspace(scope.user_id, preference, session_id=turn["session_id"]))["id"] == target_id
