"""Tool names on the wire must match ^[a-zA-Z0-9_-]{1,64}$ and map back on inbound tool calls.

Team tools are named ``teammcp.<connection>.<operation>``; OpenAI, Anthropic and DeepSeek reject
dotted names with HTTP 400 before the model runs, which failed every turn of an Agent holding
Power BI tools on an OpenAI-compatible DeepSeek connection.
"""

from __future__ import annotations

import json
import re

import pytest
import respx
from bowhead_router.adapters.anthropic_compat import AnthropicCompatibleAdapter
from bowhead_router.tool_names import ToolNameMap, to_wire_tool_name

from bowhead_router import LLMRequest, Message, OpenAICompatibleAdapter, ToolCall, ToolSchema

BASE = "https://api.example.com/v1"
ANTHROPIC = "https://api.anthropic.com"
WIRE = re.compile(r"[A-Za-z0-9_-]{1,64}")
TEAM_TOOL = "teammcp.ibt_3ed770c16f60.pbi_execute_dax"
BUILTIN_TOOL = "subagent_return"


def _tool(name: str) -> ToolSchema:
    return ToolSchema(name=name, description="d", input_schema={"type": "object", "properties": {}})


def _request(**overrides: object) -> LLMRequest:
    fields: dict[str, object] = {
        "model": "chat-model",
        "messages": [
            Message(role="user", content="q"),
            Message(role="assistant", content="", tool_calls=[ToolCall(id="c0", name=TEAM_TOOL, arguments={})]),
            Message(role="tool", content="{}", tool_call_id="c0", name=TEAM_TOOL),
        ],
        "tools": [_tool(BUILTIN_TOOL), _tool(TEAM_TOOL)],
    }
    fields.update(overrides)
    return LLMRequest(**fields)  # type: ignore[arg-type]


def _openai(**kwargs: object) -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(
        base_url=BASE,
        api_key="sk-test",
        provider_name="test_provider",
        provider_options={"models": [{"id": "resp-model", "api_surface": "responses"}, {"id": "chat-model"}]},
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    "name",
    [TEAM_TOOL, "teammcp.src.op-name", "ns:tool/v2", "a" * 80, "x." + "y" * 70, "a__b.c"],
)
def test_wire_name_is_provider_safe(name: str) -> None:
    assert WIRE.fullmatch(to_wire_tool_name(name))


def test_compliant_names_are_unchanged() -> None:
    for name in (BUILTIN_TOOL, "pbi_execute_dax", "bowra-xlsx", "a__b"):
        assert to_wire_tool_name(name) == name


def test_long_names_stay_distinct() -> None:
    a, b = "teammcp." + "s" * 70 + ".op_a", "teammcp." + "s" * 70 + ".op_b"
    assert to_wire_tool_name(a) != to_wire_tool_name(b)


def test_map_restores_canonical_names_without_reversing_the_rewrite() -> None:
    names = ToolNameMap([TEAM_TOOL, "a__b", "x.y"])
    assert names.to_canonical(to_wire_tool_name(TEAM_TOOL)) == TEAM_TOOL
    # A canonical name that already contains `__` must not be turned into a dotted name.
    assert names.to_canonical("a__b") == "a__b"
    assert names.to_canonical("x__y") == "x.y"
    assert names.to_canonical("unknown_tool") == "unknown_tool"


def test_compliant_name_wins_a_wire_collision() -> None:
    names = ToolNameMap(["a.b", "a__b"])
    assert names.to_canonical("a__b") == "a__b"


@pytest.mark.unit
@respx.mock
async def test_openai_chat_sends_safe_names_and_maps_tool_calls_back() -> None:
    wire_team = to_wire_tool_name(TEAM_TOOL)
    route = respx.post(f"{BASE}/chat/completions").respond(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": wire_team, "arguments": "{}"}}],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )
    adapter = _openai()
    try:
        resp = await adapter.invoke(_request())
    finally:
        await adapter.close()

    body = json.loads(route.calls.last.request.content)
    assert [t["function"]["name"] for t in body["tools"]] == [BUILTIN_TOOL, wire_team]
    assistant = next(m for m in body["messages"] if m["role"] == "assistant")
    assert assistant["tool_calls"][0]["function"]["name"] == wire_team
    tool_message = next(m for m in body["messages"] if m["role"] == "tool")
    assert tool_message.get("name") in (None, wire_team)
    assert [tc.name for tc in resp.tool_calls] == [TEAM_TOOL]


@pytest.mark.unit
@respx.mock
async def test_openai_chat_stream_maps_tool_calls_back() -> None:
    wire_team = to_wire_tool_name(TEAM_TOOL)
    frames = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": wire_team, "arguments": "{}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
    ]
    respx.post(f"{BASE}/chat/completions").respond(
        200,
        text="".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n",
        headers={"content-type": "text/event-stream"},
    )
    adapter = _openai()
    try:
        calls = [c.tool_call_delta async for c in adapter.stream(_request()) if c.tool_call_delta is not None]
    finally:
        await adapter.close()
    assert [c.name for c in calls] == [TEAM_TOOL]


@pytest.mark.unit
@respx.mock
async def test_openai_responses_sends_safe_names_and_maps_function_calls_back() -> None:
    wire_team = to_wire_tool_name(TEAM_TOOL)
    route = respx.post(f"{BASE}/responses").respond(
        200,
        json={
            "id": "resp_1",
            "object": "response",
            "status": "completed",
            "model": "resp-model",
            "output": [{"type": "function_call", "id": "fc_1", "call_id": "c1", "name": wire_team, "arguments": "{}"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )
    adapter = _openai()
    try:
        resp = await adapter.invoke(_request(model="resp-model"))
    finally:
        await adapter.close()

    body = json.loads(route.calls.last.request.content)
    assert [t["name"] for t in body["tools"]] == [BUILTIN_TOOL, wire_team]
    history_calls = [i for i in body["input"] if i.get("type") == "function_call"]
    assert [i["name"] for i in history_calls] == [wire_team]
    assert [tc.name for tc in resp.tool_calls] == [TEAM_TOOL]


@pytest.mark.unit
@respx.mock
async def test_openai_explicit_opt_out_sends_names_verbatim() -> None:
    route = respx.post(f"{BASE}/chat/completions").respond(
        200,
        json={"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}], "usage": {}},
    )
    adapter = _openai(sanitize_tool_dots=False)
    try:
        await adapter.invoke(_request())
    finally:
        await adapter.close()
    body = json.loads(route.calls.last.request.content)
    assert [t["function"]["name"] for t in body["tools"]] == [BUILTIN_TOOL, TEAM_TOOL]


@pytest.mark.unit
@respx.mock
async def test_anthropic_sends_safe_names_by_default_and_maps_tool_use_back() -> None:
    wire_team = to_wire_tool_name(TEAM_TOOL)
    route = respx.post(f"{ANTHROPIC}/v1/messages").respond(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude",
            "content": [{"type": "tool_use", "id": "tu_1", "name": wire_team, "input": {}}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )
    adapter = AnthropicCompatibleAdapter(base_url=ANTHROPIC, api_key="sk-test")
    try:
        resp = await adapter.invoke(_request(model="claude"))
    finally:
        await adapter.close()

    body = json.loads(route.calls.last.request.content)
    assert [t["name"] for t in body["tools"]] == [BUILTIN_TOOL, wire_team]
    tool_uses = [b for m in body["messages"] for b in (m["content"] if isinstance(m["content"], list) else []) if b.get("type") == "tool_use"]
    assert [b["name"] for b in tool_uses] == [wire_team]
    assert [tc.name for tc in resp.tool_calls] == [TEAM_TOOL]


@pytest.mark.unit
@respx.mock
async def test_anthropic_stream_maps_tool_use_back() -> None:
    wire_team = to_wire_tool_name(TEAM_TOOL)
    events = [
        ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 1, "output_tokens": 0}}}),
        (
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "tu_1", "name": wire_team, "input": {}}},
        ),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{}"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    respx.post(f"{ANTHROPIC}/v1/messages").respond(
        200,
        text="".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events),
        headers={"content-type": "text/event-stream"},
    )
    adapter = AnthropicCompatibleAdapter(base_url=ANTHROPIC, api_key="sk-test")
    try:
        calls = [c.tool_call_delta async for c in adapter.stream(_request(model="claude")) if c.tool_call_delta is not None]
    finally:
        await adapter.close()
    assert [c.name for c in calls] == [TEAM_TOOL]
