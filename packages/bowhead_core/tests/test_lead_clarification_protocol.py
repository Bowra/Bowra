"""Uninvoked clarification text cannot become a successful ordinary Turn."""

import json
from dataclasses import replace
from types import SimpleNamespace

import httpx2
import pytest
import respx
from bowhead_core.agents.clarification_tool import register_clarification_tool
from bowhead_core.documents.turn_policy import NATIVE_DOCUMENT_GUIDANCE, UNAVAILABLE_DOCUMENT_GUIDANCE
from bowhead_core.subagent import PROFILES
from bowhead_core.tool_registry import ToolExecutionContext, ToolSpec
from bowhead_router.types import ToolSchema
from test_reference_http_start import enable_lead_writer, rows, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case
from test_stray_clarification_ask import ARGUMENT_TEXT, BARE_QUESTIONS, MALFORMED_CALL, SELF_CLOSING_CALL, TRUNCATED_CALL

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]


@pytest.mark.parametrize("example_kind", ["missing", "paired", "self-closing"])
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
        SELF_CLOSING_CALL
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
    [ARGUMENT_TEXT, MALFORMED_CALL, SELF_CLOSING_CALL, TRUNCATED_CALL, BARE_QUESTIONS, "<tool_call><name>kb_add_file</name></tool_call>"],
    ids=["arguments", "xml", "self-closing", "truncated", "bare-questions", "native-tool-xml"],
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
    assert not any(marker in (turn.final_answer or "") for marker in ("questions=", "clarification_ask", "fallback_text="))
    events = [row for row in rows(case, "turn_events") if row["turn_id"] == turn_id]
    done = [json.loads(row["payload_json"]) for row in events if row["event_type"] == "done"]
    assert len(done) == 1 and done[0]["status"] == "error"
    assert done[0]["narrative_superseded"] is True
    reason = "agent_step_limit" if limit == 1 else "lead_internal_error"
    assert done[0]["reason_code"] == turn.error == reason
    assert not any(row["event_type"] == "clarification_required" for row in events)
