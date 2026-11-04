"""Content-free model-call diagnostics use the persisted/live event seam."""

from __future__ import annotations

import json
from dataclasses import asdict

import httpx2
import pytest
import respx
from bowhead_core.events import OrchestratorEvent, TurnEventBus
from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec, build_default_registry
from bowhead_router.adapters.anthropic_compat import AnthropicCompatibleAdapter

from bowhead_router import (
    AgentBinding,
    LLMResponse,
    MockAdapter,
    OpenAICompatibleAdapter,
    ProviderConfig,
    ProviderRegistry,
    Router,
    ToolCall,
    ToolSchema,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invoke_and_stream_timing_are_persisted_with_distinct_first_chunk_values() -> None:
    adapter = MockAdapter(provider_name="p1")
    adapter.scripted_responses = [
        LLMResponse(content="", tool_calls=[ToolCall(id="c1", name="no_such_tool", arguments={})]),
        LLMResponse(content="已完成"),
    ]
    providers = ProviderRegistry()
    providers.load([ProviderConfig(name="p1", type="mock")])
    providers._adapters["p1"] = adapter  # type: ignore[attr-defined]
    router = Router(providers)
    for agent_type in ("lead", "analyst", "compute", "narrative", "echo"):
        router.bind(AgentBinding(agent_type=agent_type, primary=("p1", "mock")))

    recorded: list[OrchestratorEvent] = []

    async def sink(event: OrchestratorEvent) -> None:
        recorded.append(event)

    bus = TurnEventBus(log_sink=sink, persist_before_live=True)
    runner = SubagentRunner(router=router, tool_registry=build_default_registry(), backend="legacy")
    ctx = ToolExecutionContext(workspace_id="w", member_uid="m", user_id="u", turn_id="t", datarc=None)  # type: ignore[arg-type]
    result = await runner.run(profile_name="compute", task="记录耗时", parent_turn_id="t", parent_bus=bus, parent_ctx=ctx)

    assert result.ok, result.error
    timings = [event.payload for event in recorded if event.event_type == "llm.call_timing"]
    assert len(timings) == 2
    assert [timing["mode"] for timing in timings] == ["invoke", "stream"]
    assert timings[0]["first_chunk_ms"] is None
    assert timings[0]["first_text_ms"] is None
    assert timings[1]["first_chunk_ms"] is not None
    assert timings[1]["first_text_ms"] is not None
    assert 0 <= timings[1]["first_chunk_ms"] <= timings[1]["first_text_ms"] <= timings[1]["duration_ms"]
    assert all(timing["ok"] for timing in timings)
    assert all(timing["started_at"] <= timing["ended_at"] for timing in timings)
    assert timings[0]["seq"] < timings[1]["seq"]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invoke", "stream"])
@pytest.mark.parametrize("native", [True, False], ids=["native", "text-salvage"])
@respx.mock
async def test_anthropic_call_observations_match_saved_live_events_before_salvage(mode, native, monkeypatch):
    secret = "private-response-and-argument-sentinel"
    model_calls = 0
    bodies = []

    def respond(request):
        nonlocal model_calls
        body = json.loads(request.content)
        bodies.append(body)
        first = model_calls == 0
        model_calls += 1
        text = (
            f'<｜DSML｜invoke name="diagnostic_echo"><｜DSML｜parameter name="private">{secret}</｜DSML｜parameter>'
            if first and not native
            else secret
        )
        blocks = [{"type": "text", "text": text}]
        if first and native:
            blocks.append({"type": "tool_use", "id": "native-original", "name": "diagnostic_echo", "input": {"private": secret}})
        reason = "tool_use" if first and native else "end_turn"
        usage = {"input_tokens": 5, "output_tokens": 2}
        if not body.get("stream"):
            return httpx2.Response(200, json={"content": blocks, "stop_reason": reason, "usage": usage})
        events = [{"type": "message_start", "message": {"usage": {"input_tokens": 5}}}]
        for index, block in enumerate(blocks):
            start = dict(block)
            if block["type"] == "tool_use":
                start["input"] = {}
                delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
            else:
                start["text"] = ""
                delta = {"type": "text_delta", "text": block["text"]}
            events.extend(
                [
                    {"type": "content_block_start", "index": index, "content_block": start},
                    {"type": "content_block_delta", "index": index, "delta": delta},
                    {"type": "content_block_stop", "index": index},
                ]
            )
        events.extend([{"type": "message_delta", "delta": {"stop_reason": reason}, "usage": usage}, {"type": "message_stop"}])
        return httpx2.Response(200, text="".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events))

    respx.post("https://api.anthropic.com/v1/messages").mock(side_effect=respond)
    adapter = AnthropicCompatibleAdapter(base_url="https://api.anthropic.com", api_key="private-test-key", provider_name="p1")
    providers = ProviderRegistry()
    providers.load([ProviderConfig(name="p1", type="mock")])
    providers._adapters["p1"] = adapter
    router = Router(providers)
    router.bind(AgentBinding(agent_type="lead", primary=("p1", "synthetic")))
    profile = "compute" if mode == "invoke" else "lead"
    monkeypatch.setitem(
        PROFILES,
        profile,
        SubagentProfile(
            name=profile, llm_agent_type="lead", tool_whitelist=("diagnostic_echo",), system_prompt="Use synthetic evidence.", max_iterations=3
        ),
    )
    saved = []

    async def sink(event):
        saved.append(json.loads(json.dumps(asdict(event))))

    async def callback(_text):
        pass

    registry = ToolRegistry()
    executed = []

    async def echo(args, _ctx):
        executed.append(args)
        return {"receipt": "original-result"}

    registry.register(
        ToolSpec(schema=ToolSchema(name="diagnostic_echo", description="Synthetic echo", input_schema={"type": "object"}), handler=echo)
    )
    runner = SubagentRunner(router=router, tool_registry=registry, backend="legacy")
    bus = TurnEventBus(log_sink=sink, persist_before_live=True)
    ctx = ToolExecutionContext(workspace_id="w", member_uid="m", user_id="u", turn_id="t", datarc=None)
    try:
        result = await runner.run(
            profile_name=profile, task="Run one synthetic action.", parent_turn_id="t", parent_bus=bus, parent_ctx=ctx, stream_callback=callback
        )
    finally:
        await adapter.close()
        await bus.close()
    live = [json.loads(json.dumps(asdict(event))) async for event in bus.subscribe()]
    timings = [event for event in saved if event["event_type"] == "llm.call_timing"]
    assert timings == [event for event in live if event["event_type"] == "llm.call_timing"]
    assert secret not in json.dumps(timings) and "private-test-key" not in json.dumps(timings)
    assert result.ok, result.error
    assert model_calls == 2 and executed == [{"private": secret}]
    first = timings[0]["payload"]
    assert first["mode"] == mode
    assert first["upstream_tool_call_count"] == first["tool_call_count"] == int(native)
    assert first["upstream_finish_reason"] == ("tool_use" if native else "end_turn")
    assert first["finish_reason"] == ("tool_use" if native else "stop")
    assert "original-result" in json.dumps(bodies[-1]["messages"])
    assert timings[-1]["payload"]["upstream_tool_call_count"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "invoke",
        "stream",
        pytest.param("fallback", marks=pytest.mark.slow),
        pytest.param("fallback_failure", marks=pytest.mark.slow),
        pytest.param("legacy_function", marks=pytest.mark.slow),
    ],
)
@respx.mock
async def test_native_call_diagnostics_preserve_tools_and_match_live_replay(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the real adapter, Router fallback and runner with synthetic HTTP."""
    base = "https://private-model.example.com/v1"
    bodies: list[dict] = []
    model_calls = 0
    secret = "private-response-and-argument-sentinel"

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal model_calls
        body = json.loads(request.content)
        bodies.append(body)
        if body["stream"] and mode in {"fallback", "fallback_failure", "legacy_function"}:
            return httpx2.Response(500, json={"error": {"message": secret}})
        if mode == "fallback_failure":
            return httpx2.Response(401, json={"error": {"message": secret}})
        first = model_calls == 0
        model_calls += 1
        calls = (
            [{"id": "c1", "type": "function", "function": {"name": "diagnostic_echo", "arguments": json.dumps({"private": secret})}}] if first else []
        )
        finish = "tool_calls" if calls else "stop"
        message = {"content": secret, "tool_calls": calls}
        if mode == "legacy_function":
            message = {"content": secret, "function_call": {"name": "diagnostic_echo", "arguments": secret}}
            finish = "function_call"
        if not body["stream"]:
            return httpx2.Response(
                200,
                json={
                    "choices": [{"message": message, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "completion_tokens_details": {"reasoning_tokens": 1}},
                },
            )
        delta = {"content": secret, "tool_calls": [dict(tc, index=i) for i, tc in enumerate(calls)]}
        if first:
            # An incomplete second native call must remain visible in the count,
            # although the existing parser cannot dispatch a nameless call.
            delta["tool_calls"].append({"index": 1, "id": "c2", "function": {"arguments": "{}"}})
        payload = {
            "choices": [{"delta": delta, "finish_reason": finish}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "completion_tokens_details": {"reasoning_tokens": 1}},
        }
        return httpx2.Response(
            200,
            content="data: " + json.dumps(payload) + "\n\ndata: " + json.dumps({"choices": [], "usage": payload["usage"]}) + "\n\ndata: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )

    respx.post(f"{base}/chat/completions").mock(side_effect=respond)
    adapter = OpenAICompatibleAdapter(base_url=base, api_key="sk-private-test-key", provider_name="p1")
    providers = ProviderRegistry()
    providers.load([ProviderConfig(name="p1", type="mock")])
    providers._adapters["p1"] = adapter
    router = Router(providers)
    # The scripted stream fails twice before the invoke fallback: one configured retry.
    router.max_retries = 1
    for agent_type in ("lead", "compute", "analyst"):
        router.bind(AgentBinding(agent_type=agent_type, primary=("p1", "model")))
    saved: list[dict] = []

    async def sink(event: OrchestratorEvent) -> None:
        saved.append(json.loads(json.dumps(asdict(event))))

    async def callback(_text: str) -> None:
        pass

    bus = TurnEventBus(log_sink=sink, persist_before_live=True)
    executed: list[dict] = []
    registry = ToolRegistry()

    async def echo(args: dict, _ctx: ToolExecutionContext) -> dict:
        executed.append(args)
        return {"confirmation": "synthetic-handler-result"}

    registry.register(
        ToolSpec(schema=ToolSchema(name="diagnostic_echo", description="Synthetic echo", input_schema={"type": "object"}), handler=echo)
    )
    for name in ("lead", "compute"):
        monkeypatch.setitem(
            PROFILES,
            name,
            SubagentProfile(
                name=name,
                llm_agent_type="lead" if name == "lead" else "analyst",
                tool_whitelist=("diagnostic_echo",),
                system_prompt="Use the supplied synthetic tool.",
                max_iterations=3,
            ),
        )
    runner = SubagentRunner(router=router, tool_registry=registry, backend="legacy")
    ctx = ToolExecutionContext(workspace_id="w", member_uid="m", user_id="u", turn_id="t", datarc=None)  # type: ignore[arg-type]
    try:
        result = await runner.run(
            profile_name="compute" if mode == "invoke" else "lead",
            task="Run the synthetic diagnostic",
            parent_turn_id="t",
            parent_bus=bus,
            parent_ctx=ctx,
            stream_callback=callback,
        )
    finally:
        await adapter.close()
        await bus.close()
    live = [json.loads(json.dumps(asdict(event))) async for event in bus.subscribe()]
    timings = [event for event in saved if event["event_type"] == "llm.call_timing"]
    assert timings == [event for event in live if event["event_type"] == "llm.call_timing"]
    assert secret not in json.dumps(timings)
    assert "sk-private-test-key" not in json.dumps(timings)
    first_timing = timings[0]["payload"]
    assert first_timing["requested_mode"] == ("invoke" if mode == "invoke" else "stream")
    assert first_timing["mode"] == ("stream" if mode == "stream" else "invoke")
    if mode != "fallback_failure":
        assert first_timing["text_chars"] == len(secret)
        assert first_timing["thinking_chars"] == 0
        assert first_timing["reasoning_tokens"] == 1
        assert first_timing["output_tokens"] == 2
        assert first_timing["output_token_limit"] == bodies[0]["max_tokens"]
        assert first_timing["output_limit_reached"] is False
        assert first_timing["empty_response"] is False
        assert first_timing["closeout_attempt"] is False
    if mode in {"fallback", "fallback_failure", "legacy_function"}:
        assert first_timing["fallback_reason"] == "upstream"
        assert first_timing["fallback_attempt_count"] == 2
    else:
        assert "fallback_reason" not in first_timing
    if mode == "fallback_failure":
        assert not result.ok
        assert first_timing["ok"] is False
        assert "tool_call_count" not in first_timing
        assert first_timing["failure_kind"] == "auth"
        assert model_calls == 0
        assert executed == []
        return
    assert result.ok, result.error
    assert first_timing["ok"] is True
    assert first_timing["finish_reason"] == ("tool_calls" if mode == "stream" else "tool_use")
    if mode == "legacy_function":
        assert first_timing["upstream_finish_reason"] == "function_call"
        assert first_timing["legacy_function_call"] is True
        assert first_timing["upstream_tool_call_count"] == first_timing["tool_call_count"] == 0
        assert model_calls == 1
        assert executed == []
        return
    assert first_timing["tool_call_count"] == 1
    assert first_timing["upstream_tool_call_count"] == (2 if mode == "stream" else 1)
    assert first_timing["upstream_finish_reason"] == "tool_calls"
    assert first_timing["legacy_function_call"] is False
    assert model_calls == 2
    assert executed == [{"private": secret}]
    assert "synthetic-handler-result" in json.dumps(bodies[-1]["messages"])
    assert any(m["role"] == "tool" and m["tool_call_id"] == "c1" for m in bodies[-1]["messages"])
    assert timings[-1]["payload"]["tool_call_count"] == 0
    if mode != "stream":
        assert first_timing["first_chunk_ms"] is None
        assert first_timing["first_text_ms"] is None
