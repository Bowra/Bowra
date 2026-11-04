"""Uninvoked clarification text cannot become a successful ordinary Turn."""

import json
from dataclasses import replace
from types import SimpleNamespace

import anyio
import httpx2
import pytest
import respx
from bowhead_core.agents.clarification_tool import register_clarification_tool
from bowhead_core.documents.turn_policy import NATIVE_DOCUMENT_GUIDANCE, UNAVAILABLE_DOCUMENT_GUIDANCE
from bowhead_core.subagent import PROFILES
from bowhead_core.tool_registry import ToolExecutionContext, ToolSpec
from bowhead_router.runtime_notes import REJECTED_TOOL_SYNTAX_INSTRUCTION
from bowhead_router.types import ToolSchema
from test_reference_http_start import enable_lead_writer, rows, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case
from test_stray_clarification_ask import ARGUMENT_TEXT, BARE_QUESTIONS, MALFORMED_CALL, NAMED_TOOL_CALL, SELF_CLOSING_CALL, TRUNCATED_CALL

from bowhead_core import events as event_bus
from bowhead_router import runtime_notes

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]


@pytest.mark.parametrize("example_kind", ["missing", "paired", "self-closing", "named-tool"])
@pytest.mark.parametrize("prefix_order", ["0", "1"])
async def test_user_example_and_runtime_guidance_keep_separate_roles(http_start, monkeypatch, example_kind, prefix_order):
    case = http_start
    enable_lead_writer(case, monkeypatch)
    monkeypatch.setenv("BOWHEAD_CTX_PREFIX_ORDER", prefix_order)
    registry = case.app.state.orchestrator.tool_registry
    register_clarification_tool(registry)

    async def unexpected_document_call(args, ctx):
        pytest.fail("Explaining a code example must not submit a document")

    registry.register(
        ToolSpec(
            schema=ToolSchema(name="template_document_submit", description="Submit a private draft", input_schema={"type": "object"}),
            handler=unexpected_document_call,
        )
    )
    markup = (
        NAMED_TOOL_CALL
        if example_kind == "named-tool"
        else SELF_CLOSING_CALL
        if example_kind == "self-closing"
        else '<clarification_ask><questions>[{"question":"Title?"}]</questions></clarification_ask>'
    )
    example = "```xml\n" + markup + "\n```"
    task = "Explain the following XML example." + ("\n\n" + example if example_kind != "missing" else "")
    answer = "This is a quoted XML example:\n\n" + example if example_kind != "missing" else "Please provide the XML example."
    calls = []

    def respond(request):
        body = json.loads(request.content)
        if body.get("stream"):
            calls.append(body)
        message = {"role": "assistant", "content": answer}
        usage = {"prompt_tokens": 11, "completion_tokens": 7}
        if body.get("stream"):
            frames = [{"choices": [{"delta": message, "finish_reason": None}]}, {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": usage}]
            return httpx2.Response(200, text="".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n")
        return httpx2.Response(200, json={"choices": [{"message": message, "finish_reason": "stop"}], "usage": usage})

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post(
            "/api/v1/chat/send", json={"workspace_id": case.fixture.scope.workspace_id, "message": task}, headers={"Content-Type": "application/json"}
        )
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
    turn = await case.fixture.case.store.get_turn(turn_id)
    assert turn.status == "completed"
    assert turn.user_text == task
    assert turn.final_answer == answer
    assert len(calls) == 1
    user_messages = [message["content"] for message in calls[0]["messages"] if message["role"] == "user"]
    assert len(user_messages) == 1
    original, separator, responsibilities = user_messages[0].partition("\n\nCurrent collaboration responsibility cards (server data):\n")
    assert original == task and separator
    roster = json.loads(responsibilities)["roster"]
    assert len(roster) == 1 and roster[0]["assignment"] == task and roster[0]["is_owner"] is True
    owner = next(row for row in rows(case, "turn_agent_execution") if row["dispatch_key"] == "collaboration-lead:" + turn_id)
    assert roster[0]["execution_id"] == owner["id"]
    system = "\n".join(message["content"] for message in calls[0]["messages"] if message["role"] == "system")
    advertised_tools = {tool["function"]["name"] for tool in calls[0].get("tools", [])}
    native_document_available = "template_document_submit" in advertised_tools
    assert (NATIVE_DOCUMENT_GUIDANCE in system) == native_document_available
    assert (UNAVAILABLE_DOCUMENT_GUIDANCE in system) == (not native_document_available)
    assert "clarification_ask alone" in system
    assert "output_language=" in system
    assert not [row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id]


@pytest.mark.parametrize(
    "text",
    [
        ARGUMENT_TEXT,
        MALFORMED_CALL,
        SELF_CLOSING_CALL,
        TRUNCATED_CALL,
        BARE_QUESTIONS,
        "<tool_call><name>kb_add_file</name></tool_call>",
        NAMED_TOOL_CALL,
    ],
    ids=["arguments", "xml", "self-closing", "truncated", "bare-questions", "native-tool-xml", "named-tool-dsml"],
)
@pytest.mark.parametrize("limit,read_first", [(4, False), (1, False), (4, True)], ids=["budget", "iteration-limit", "after-read"])
async def test_invalid_clarification_fails_without_a_card_or_replaying_work(
    http_start: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, text: str, limit: int, read_first: bool, request
) -> None:
    from bowhead_core.agent_run_limits import get_runtime_controls

    monkeypatch.setenv("BOWHEAD_AGENT_MAX_STEPS", str(limit))
    get_runtime_controls.cache_clear()
    request.addfinalizer(get_runtime_controls.cache_clear)
    case = http_start
    enable_lead_writer(case, monkeypatch)
    registry = case.app.state.orchestrator.tool_registry
    register_clarification_tool(registry)
    reads = []

    async def read(args: dict, ctx: ToolExecutionContext) -> dict:
        reads.append(ctx.turn_id)
        return {"chunks": []}

    registry.register(
        ToolSpec(schema=ToolSchema(name="kb_search", description="Read synthetic evidence", input_schema={"type": "object"}), handler=read)
    )

    async def unexpected_workspace_call(args: dict, ctx: ToolExecutionContext) -> dict:
        pytest.fail("Textual Tool arguments must never execute")

    registry.register(
        ToolSpec(
            schema=ToolSchema(name="tool_list_workspaces", description="List synthetic data sources", input_schema={"type": "object"}),
            handler=unexpected_workspace_call,
        )
    )
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("clarification_ask", "kb_search")))
    calls = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        calls.append(body)
        message = {"role": "assistant", "content": text}
        if read_first and len(calls) == 1:
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"index": 0, "id": "read_once", "type": "function", "function": {"name": "kb_search", "arguments": "{}"}}],
            }
        reason = "tool_calls" if message.get("tool_calls") else "stop"
        usage = {"prompt_tokens": 11, "completion_tokens": 7}
        if body.get("stream"):
            frames = [{"choices": [{"delta": message, "finish_reason": None}]}, {"choices": [{"delta": {}, "finish_reason": reason}], "usage": usage}]
            return httpx2.Response(200, text="".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n")
        return httpx2.Response(200, json={"choices": [{"message": message, "finish_reason": reason}], "usage": usage})

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post(
            "/api/v1/chat/send",
            json={"workspace_id": case.fixture.scope.workspace_id, "message": "Ask me for the title."},
            headers={"Content-Type": "application/json"},
        )
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
    turn = await case.fixture.case.store.get_turn(turn_id)
    assert turn.status == "failed"
    assert len(calls) == (1 if limit == 1 else 4 if read_first else 3)
    assert reads == ([turn_id] if read_first else [])
    assert not [row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id]
    assert not any(
        marker in (turn.final_answer or "") for marker in ("questions=", "clarification_ask", "fallback_text=", "<tool_list_workspaces>", "DSML")
    )
    events = [row for row in rows(case, "turn_events") if row["turn_id"] == turn_id]
    done = [json.loads(row["payload_json"]) for row in events if row["event_type"] == "done"]
    assert len(done) == 1 and done[0]["status"] == "error"
    assert done[0]["narrative_superseded"] is True
    reason = "agent_step_limit" if limit == 1 else "tool_call_not_executed"
    assert done[0]["reason_code"] == turn.error == reason
    assert not any(row["event_type"] == "clarification_required" for row in events)


@pytest.mark.parametrize("read_first", [False, True], ids=["first-response", "after-read"])
async def test_named_tool_text_corrects_to_one_native_call(http_start: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, read_first: bool) -> None:
    case = http_start
    enable_lead_writer(case, monkeypatch)
    registry = case.app.state.orchestrator.tool_registry
    reads = []
    workspace_calls = []
    rejected_history = []
    original_projection = runtime_notes.model_messages

    def observe_projection(messages):
        rejected_history.extend(
            message for message in messages if message.role == "assistant" and message.name == runtime_notes.REJECTED_TOOL_SYNTAX_HISTORY_NAME
        )
        return original_projection(messages)

    monkeypatch.setattr(runtime_notes, "model_messages", observe_projection)

    async def read(args: dict, ctx: ToolExecutionContext) -> dict:
        reads.append(ctx.turn_id)
        return {"chunks": []}

    async def list_workspaces(args: dict, ctx: ToolExecutionContext) -> dict:
        workspace_calls.append(args)
        return {"workspaces": [{"name": "Synthetic data"}]}

    for name, handler in (("kb_search", read), ("tool_list_workspaces", list_workspaces)):
        registry.register(
            ToolSpec(schema=ToolSchema(name=name, description="Read synthetic evidence", input_schema={"type": "object"}), handler=handler)
        )
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("kb_search", "tool_list_workspaces")))
    calls = []
    answer = "The available data source is Synthetic data."

    def respond(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        usage = {"prompt_tokens": 11, "completion_tokens": 7}
        if not body.get("stream"):
            return httpx2.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "Synthetic data sources"}, "finish_reason": "stop"}], "usage": usage},
            )
        calls.append(body)
        index = len(calls) - int(read_first)
        message = {"role": "assistant", "content": NAMED_TOOL_CALL if index == 1 else answer}
        if (read_first and len(calls) == 1) or index == 2:
            name = "kb_search" if index == 0 else "tool_list_workspaces"
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": name + "_once",
                        "type": "function",
                        "function": {"name": name, "arguments": "{}" if index == 0 else '{"tool":"datarc"}'},
                    }
                ],
            }
        reason = "tool_calls" if message.get("tool_calls") else "stop"
        frames = [{"choices": [{"delta": message, "finish_reason": None}]}, {"choices": [{"delta": {}, "finish_reason": reason}], "usage": usage}]
        return httpx2.Response(200, text="".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n")

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post(
            "/api/v1/chat/send",
            json={"workspace_id": case.fixture.scope.workspace_id, "message": "List my available data sources."},
            headers={"Content-Type": "application/json"},
        )
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
    turn = await case.fixture.case.store.get_turn(turn_id)
    assert turn.status == "completed"
    assert turn.final_answer == answer
    assert len(calls) == 3 + int(read_first)
    assert any("Use the native tool-call interface" in str(message.get("content")) for message in calls[1 + int(read_first)]["messages"])
    assert workspace_calls == [{"tool": "datarc"}]
    assert reads == ([turn_id] if read_first else [])
    for body in calls[1 + int(read_first) :]:
        assistant_history = [message for message in body["messages"] if message["role"] == "assistant"]
        assert any(message.get("content") == REJECTED_TOOL_SYNTAX_INSTRUCTION for message in assistant_history)
        assert all(NAMED_TOOL_CALL not in str(message.get("content")) for message in assistant_history)
    events = [row for row in rows(case, "turn_events") if row["turn_id"] == turn_id]
    assert rejected_history and all(message.content == NAMED_TOOL_CALL for message in rejected_history)
    done = [json.loads(row["payload_json"]) for row in events if row["event_type"] == "done"]
    assert len(done) == 1 and done[0]["status"] == "completed"
    assert done[0]["narrative_superseded"] is True
    final_events = [json.loads(row["payload_json"]) for row in events if row["event_type"] == "final_answer"]
    assert final_events and "DSML" not in json.dumps(final_events)


@pytest.mark.parametrize("text", [MALFORMED_CALL, NAMED_TOOL_CALL], ids=["clarification", "named-tool"])
@pytest.mark.parametrize("repair", ["plain-question", "quoted-example", "unrelated-read", "invalid-native-call"])
async def test_protocol_correction_cannot_finish_without_the_intended_native_call(
    http_start: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, text: str, repair: str
) -> None:
    case = http_start
    live_events = []
    original_emit = event_bus.TurnEventBus.emit

    async def observe_emit(bus: event_bus.TurnEventBus, event: event_bus.OrchestratorEvent) -> bool:
        live_events.append(event)
        return await original_emit(bus, event)

    monkeypatch.setattr(event_bus.TurnEventBus, "emit", observe_emit)
    enable_lead_writer(case, monkeypatch)
    registry = case.app.state.orchestrator.tool_registry
    register_clarification_tool(registry)
    reads = []
    invalid_calls = []

    async def read(args: dict, ctx: ToolExecutionContext) -> dict:
        reads.append(ctx.turn_id)
        return {"chunks": []}

    async def unexpected_workspace_call(args: dict, ctx: ToolExecutionContext) -> dict:
        assert repair == "invalid-native-call" and args == {}
        invalid_calls.append(args)
        return {"ok": False, "error_kind": "schema_invalid", "error_message": "The tool field is required."}

    for name, handler in (("kb_search", read), ("tool_list_workspaces", unexpected_workspace_call)):
        registry.register(
            ToolSpec(
                schema=ToolSchema(
                    name=name,
                    description="Read synthetic evidence",
                    input_schema={"type": "object", "properties": {"tool": {"type": "string"}}, "required": ["tool"]}
                    if name == "tool_list_workspaces"
                    else {"type": "object"},
                ),
                handler=handler,
            )
        )
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("clarification_ask", "kb_search", "tool_list_workspaces")))
    calls = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        from test_ordinary_lead_question_http import call, response

        body = json.loads(request.content)
        if not body.get("stream"):
            return response(body, {"role": "assistant", "content": "Synthetic missing input"}, "stop")
        calls.append(body)
        content = text if len(calls) == 1 else "Please provide the required revenue amount before I create the Word document."
        if repair == "quoted-example" and len(calls) > 1:
            content = "```xml\n" + text + "\n```\n" + content
        if repair == "unrelated-read" and len(calls) == 2:
            return response(body, call("unrelated-read", "kb_search", {}), "tool_calls")
        if repair == "invalid-native-call" and len(calls) == 2:
            name = "clarification_ask" if text == MALFORMED_CALL else "tool_list_workspaces"
            return response(body, call("invalid-native-call", name, {}), "tool_calls")
        return response(body, {"role": "assistant", "content": content}, "stop")

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post(
            "/api/v1/chat/send",
            json={"workspace_id": case.fixture.scope.workspace_id, "message": "Ask for the missing revenue before creating my Word document."},
            headers={"Content-Type": "application/json"},
        )
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
    turn = await case.fixture.case.store.get_turn(turn_id)
    assert turn.status == "failed"
    assert turn.error == "tool_call_not_executed"
    for body in calls[1:]:
        assistant_history = [message for message in body["messages"] if message["role"] == "assistant"]
        assert any(message.get("content") == REJECTED_TOOL_SYNTAX_INSTRUCTION for message in assistant_history)
        assert all(text not in str(message.get("content")) for message in assistant_history)
    if repair == "invalid-native-call":
        assert 4 <= len(calls) <= 5
    else:
        assert len(calls) == (4 if repair == "unrelated-read" else 3)
    assert reads == ([turn_id] if repair == "unrelated-read" else [])
    assert invalid_calls == ([{}] if repair == "invalid-native-call" and text == NAMED_TOOL_CALL else [])
    assert not [row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id]
    events = [row for row in rows(case, "turn_events") if row["turn_id"] == turn_id]
    done = [json.loads(row["payload_json"]) for row in events if row["event_type"] == "done"]
    assert len(done) == 1 and done[0]["status"] == "error"
    assert done[0]["reason_code"] == turn.error
    assert done[0]["narrative_superseded"] is True
    assert not any(row["event_type"] in {"clarification_required", "final_answer"} for row in events)
    assert [event.payload for event in live_events if event.event_type == "done"] == done
    with anyio.fail_after(5):
        replay = await case.client.get(accepted.json()["stream_url"])
    assert replay.status_code == 200
    replayed = [json.loads(line[6:]) for line in replay.text.splitlines() if line.startswith("data: ")]
    fields = ("status", "reason_code", "narrative_superseded")
    assert [{key: value.get(key) for key in fields} for value in replayed if value.get("status") == "error"] == [
        {key: done[0][key] for key in fields}
    ]
