"""Test Responses transport and capability probing with sanitized protocol fixtures.

Fixtures retain provider framing, usage fields, completion semantics and optional
field shapes. Request IDs and timestamps are synthetic; streamed reasoning text
is authored test data. No private historical document is needed to run the suite.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest
import respx
from bowhead_router.adapters.openai_compat import OpenAICompatError
from bowhead_router.errors import ProviderFailureKind
from bowhead_router.invocation import physical_invocation_scope
from bowhead_router.outbound import rejected_param
from bowhead_router.probe import CompatibleProbeRegistry, NotTestable
from bowhead_router.transport import (
    build_provider_transport_plan,
    model_api_surface,
    model_request_shape,
)

from bowhead_router import (
    LLMRequest,
    Message,
    OpenAICompatibleAdapter,
    ToolCall,
    ToolSchema,
)

BASE = "https://api.example.com/v1"


def _probe_mock_factory(handler):
    def factory(**kwargs):
        return httpx2.AsyncClient(transport=httpx2.MockTransport(handler), **kwargs)

    return factory


_TOOL = ToolSchema(
    name="get_weather",
    description="Get weather for a city",
    input_schema={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
)


def _adapter() -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(
        base_url=BASE,
        api_key="sk-test",
        provider_name="test_provider",
        provider_options={
            "models": [
                {"id": "resp-model", "api_surface": "responses"},
                {"id": "chat-model"},
            ],
        },
    )


def _responses_ok(**overrides) -> dict:
    """最小合法 Responses 响应（形状按 deepseek-nonstream.json）。"""
    payload = {
        "id": "resp_1",
        "object": "response",
        "status": "completed",
        "model": "resp-model-v9",  # Gateways commonly rename models (observed with DeepSeek).
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "phase": "final_answer",  # Tolerate this DeepSeek extension field.
                "content": [{"type": "output_text", "annotations": [], "text": "pong"}],
            }
        ],
        "usage": {
            "input_tokens": 10,
            "input_tokens_details": {"cached_tokens": 4},
            "output_tokens": 2,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 12,
        },
    }
    payload.update(overrides)
    return payload


# ─────────────────────────────────────────────────────────────
# §8.10-1 / §8.10-2: request shape + positive control
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
@respx.mock
async def test_responses_invoke_posts_input_shape_to_responses_path() -> None:
    route = respx.post(f"{BASE}/responses").respond(200, json=_responses_ok())
    adapter = _adapter()
    try:
        resp = await adapter.invoke(
            LLMRequest(
                model="resp-model",
                system="You are terse.",
                messages=[Message(role="user", content="ping")],
                stop=["\n\n"],
                response_format={"type": "json_object"},
                tools=[_TOOL],
            )
        )
    finally:
        await adapter.close()

    body = json.loads(route.calls.last.request.content)
    assert body["input"] == [{"role": "user", "content": "ping"}]
    assert body["instructions"] == "You are terse."
    assert body["max_output_tokens"] == 32000
    assert body["store"] is False  # DashScope defaults to true (provider-side retention); explicitly disable it.
    assert body["text"] == {"format": {"type": "json_object"}}
    # Flat tools (Responses shape), rather than Chat Completions' nested function objects.
    assert body["tools"][0]["name"] == "get_weather"
    assert "function" not in body["tools"][0]
    assert "messages" not in body
    assert "max_tokens" not in body
    assert "stop" not in body  # Drop this parameter, which Responses does not support.
    assert "stream" not in body  # Omit for non-streaming requests (false is the documented default).

    assert resp.content == "pong"
    assert resp.model == "resp-model-v9"
    assert resp.usage.input_tokens == 10
    assert resp.usage.output_tokens == 2
    assert resp.usage.cached_tokens == 4
    assert resp.finish_reason == "stop"


@pytest.mark.unit
@respx.mock
async def test_chat_surface_unchanged_positive_control() -> None:
    """正对照：同 provider 的 chat 模型仍走 /chat/completions、body 是 messages 形状。"""
    route = respx.post(f"{BASE}/chat/completions").respond(
        200,
        json={
            "model": "chat-model",
            "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        },
    )
    adapter = _adapter()
    try:
        resp = await adapter.invoke(LLMRequest(model="chat-model", messages=[Message(role="user", content="ping")], stop=["x"]))
    finally:
        await adapter.close()

    body = json.loads(route.calls.last.request.content)
    assert body["messages"] == [{"role": "user", "content": "ping"}]
    assert body["max_tokens"] == 32000
    assert body["stop"] == ["x"]
    assert body["stream"] is False
    assert "input" not in body
    assert "store" not in body
    assert resp.content == "hi"


# ─────────────────────────────────────────────────────────────
# §8.10-3: both legs of a tool round trip
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
@respx.mock
async def test_responses_tool_call_parsed_with_call_id_and_commentary_kept() -> None:
    """解析腿：message(commentary)+function_call 混合 item（deepseek-toolcall.json 形状）；
    ToolCall.id 必须取 call_id（DashScope 的 item id 是 msg_ 前缀）。"""
    respx.post(f"{BASE}/responses").respond(
        200,
        json=_responses_ok(
            output=[
                {
                    "type": "message",
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": "I'll check."}],
                },
                {
                    "type": "function_call",
                    "id": "msg_490faae4",  # ← Not the pairing key.
                    "call_id": "call_7a262e78",
                    "name": "get_weather",
                    "arguments": '{"city": "Paris"}',
                    "status": "completed",
                },
            ]
        ),
    )
    adapter = _adapter()
    try:
        resp = await adapter.invoke(LLMRequest(model="resp-model", messages=[Message(role="user", content="weather?")], tools=[_TOOL]))
    finally:
        await adapter.close()

    assert resp.finish_reason == "tool_use"
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].id == "call_7a262e78"
    assert resp.tool_calls[0].name == "get_weather"
    assert resp.tool_calls[0].arguments == {"city": "Paris"}
    assert resp.content == "I'll check."  # Preserve commentary.
    assert resp.diagnostics is not None
    assert resp.diagnostics.trace_fields() == {
        "mode": "invoke",
        "upstream_tool_call_count": 1,
        "upstream_finish_reason": "completed",
        "legacy_function_call": None,
        "incomplete_reason": None,
        "reasoning_tokens": 0,
        "output_token_limit": 32000,
    }


@pytest.mark.unit
@respx.mock
async def test_responses_tool_round_trip_items_parallel_and_out_of_order() -> None:
    """回传腿：assistant(tool_calls=[a,b]) + 乱序 tool 结果 → fc/fco items。
    乱序是生产常态（pre-check 合成结果先 append）；两家真端点实测收这个形状。"""
    route = respx.post(f"{BASE}/responses").respond(200, json=_responses_ok())
    adapter = _adapter()
    try:
        await adapter.invoke(
            LLMRequest(
                model="resp-model",
                messages=[
                    Message(role="user", content="compare"),
                    Message(
                        role="assistant",
                        content="checking both",
                        tool_calls=[
                            ToolCall(id="call_a", name="get_weather", arguments={"city": "Paris"}),
                            ToolCall(id="call_b", name="get_weather", arguments={"city": "Tokyo"}),
                        ],
                    ),
                    Message(role="tool", content='{"t": 31}', tool_call_id="call_b", name="get_weather"),
                    Message(role="tool", content='{"t": 21}', tool_call_id="call_a", name="get_weather"),
                ],
                tools=[_TOOL],
            )
        )
    finally:
        await adapter.close()

    items = json.loads(route.calls.last.request.content)["input"]
    assert items[0] == {"role": "user", "content": "compare"}
    assert items[1] == {"role": "assistant", "content": "checking both"}
    assert items[2] == {"type": "function_call", "call_id": "call_a", "name": "get_weather", "arguments": '{"city": "Paris"}'}
    assert items[3]["call_id"] == "call_b"
    # Preserve out-of-order results; fco has no name field (the item has no place for it).
    assert items[4] == {"type": "function_call_output", "call_id": "call_b", "output": '{"t": 31}'}
    assert items[5] == {"type": "function_call_output", "call_id": "call_a", "output": '{"t": 21}'}
    # fc items omit id (accepted in observed calls; id is a response-side field).
    assert "id" not in items[2]


# ─────────────────────────────────────────────────────────────
# §8.10-4: reasoning → thinking; keys may be absent or null
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
@respx.mock
async def test_responses_reasoning_summary_text_to_thinking() -> None:
    """summary block 只有 type/text 两键，summary_text 是 type 的**值**
    （dashscope-toolreturn-qwen36.json 实测；初版设计把它当键名写、review 逮回）。"""
    respx.post(f"{BASE}/responses").respond(
        200,
        json=_responses_ok(
            output=[
                {
                    "type": "reasoning",
                    "id": "msg_r1",
                    "summary": [{"type": "summary_text", "text": "User wants pong."}],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "pong"}],
                },
            ]
        ),
    )
    adapter = _adapter()
    try:
        resp = await adapter.invoke(LLMRequest(model="resp-model", messages=[Message(role="user", content="ping")]))
    finally:
        await adapter.close()
    assert resp.thinking == "User wants pong."
    assert resp.content == "pong"


@pytest.mark.unit
@respx.mock
async def test_responses_tolerates_missing_and_null_keys() -> None:
    """DashScope 省略未设键、DeepSeek 置 null——双防（§2.6 第二轮表）。"""
    respx.post(f"{BASE}/responses").respond(
        200,
        json={
            "object": "response",
            # status and incomplete_details are absent; usage is null.
            "model": None,
            "usage": None,
            "output": [
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]},
            ],
        },
    )
    adapter = _adapter()
    try:
        resp = await adapter.invoke(LLMRequest(model="resp-model", messages=[Message(role="user", content="x")]))
    finally:
        await adapter.close()
    assert resp.content == "ok"
    assert resp.usage.input_tokens == 0
    assert resp.model == "resp-model"  # A null model falls back to the requested name.
    assert resp.finish_reason == "stop"


# ─────────────────────────────────────────────────────────────
# §8.10-5: apply the overrides / outbound processing stage on the Responses surface
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
@respx.mock
async def test_responses_overrides_and_outbound_family_applied() -> None:
    """尾巴不许静默失效（handoff 红线）：request_body_overrides 合并、declared omit
    消掉 temperature、fixed max_tokens 写的是**线上名 max_output_tokens**。"""
    route = respx.post(f"{BASE}/responses").respond(200, json=_responses_ok())
    adapter = OpenAICompatibleAdapter(
        base_url=BASE,
        api_key="sk-test",
        provider_name="test_provider",
        provider_options={
            "models": [
                {
                    "id": "resp-model",
                    "api_surface": "responses",
                    "request_body_overrides": {"chat": {"reasoning": {"effort": "low"}}},
                    "outbound_params": {
                        "temperature": {"mode": "omit"},
                        "max_tokens": {"mode": "fixed", "value": 512},
                    },
                },
            ],
        },
    )
    try:
        await adapter.invoke(LLMRequest(model="resp-model", messages=[Message(role="user", content="x")]))
    finally:
        await adapter.close()

    body = json.loads(route.calls.last.request.content)
    assert body["reasoning"] == {"effort": "low"}  # The final overrides stage is applied.
    assert "temperature" not in body  # omit takes effect.
    assert body["max_output_tokens"] == 512  # Map fixed values to the wire parameter name.
    assert "max_tokens" not in body


# ─────────────────────────────────────────────────────────────
# §8.10-6: system rules + merging adjacent user messages
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
@respx.mock
async def test_responses_non_leading_system_downgraded_to_user_and_merged() -> None:
    """腰部 system（历史中段被注入 system 的形状）降级 user——DashScope 实测 400
    「no more than one system message… must be the first」；降级产生的相邻 user 合并。"""
    route = respx.post(f"{BASE}/responses").respond(200, json=_responses_ok())
    adapter = _adapter()
    try:
        await adapter.invoke(
            LLMRequest(
                model="resp-model",
                system="lead prompt",
                messages=[
                    Message(role="user", content="q1"),
                    Message(role="assistant", content="a1"),
                    Message(role="system", content="[digest 3 early steps]: summarized."),
                    Message(role="user", content="q2"),
                ],
            )
        )
    finally:
        await adapter.close()

    items = json.loads(route.calls.last.request.content)["input"]
    assert [i.get("role") for i in items] == ["user", "assistant", "user"]
    assert items[2]["content"] == "[digest 3 early steps]: summarized.\n\nq2"


@pytest.mark.unit
@respx.mock
async def test_responses_leading_system_without_instructions_stays_system() -> None:
    """无 instructions 时首位 system 保留原 role（DashScope 允许「一条且首位」）。"""
    route = respx.post(f"{BASE}/responses").respond(200, json=_responses_ok())
    adapter = _adapter()
    try:
        await adapter.invoke(
            LLMRequest(
                model="resp-model",
                messages=[
                    Message(role="system", content="be terse"),
                    Message(role="user", content="q"),
                ],
            )
        )
    finally:
        await adapter.close()
    items = json.loads(route.calls.last.request.content)["input"]
    assert items[0] == {"role": "system", "content": "be terse"}


# ─────────────────────────────────────────────────────────────
# Stream decoding preserves the provider framing in sanitized fixtures.
# ─────────────────────────────────────────────────────────────
_SAMPLES = Path(__file__).parent / "fixtures/responses"


def _sse(route_bytes: bytes):
    return httpx2.Response(200, content=route_bytes, headers={"content-type": "text/event-stream"})


async def _collect_stream(adapter: OpenAICompatibleAdapter, req: LLMRequest) -> list:
    chunks = []
    async for chunk in adapter.stream(req):
        chunks.append(chunk)
    return chunks


@pytest.mark.unit
@respx.mock
async def test_responses_stream_sample_dashscope_bytes() -> None:
    """Decode DashScope framing with IDs, events, comments and compact data lines."""
    raw = (_SAMPLES / "dashscope-stream.txt").read_bytes()
    respx.post(f"{BASE}/responses").side_effect = lambda request: _sse(raw)
    adapter = _adapter()
    try:
        chunks = await _collect_stream(
            adapter,
            LLMRequest(model="resp-model", messages=[Message(role="user", content="count")], stream=True),
        )
    finally:
        await adapter.close()

    assert "".join(c.delta for c in chunks if c.delta) == "12345"
    final = chunks[-1]
    assert final.finish_reason == "stop"
    assert final.usage is not None
    assert final.usage.input_tokens == 55
    assert final.usage.output_tokens == 5


@pytest.mark.unit
@respx.mock
async def test_responses_stream_sample_deepseek_bytes() -> None:
    """Decode spaced DeepSeek events without ID lines or a DONE sentinel."""
    raw = (_SAMPLES / "deepseek-stream.txt").read_bytes()
    route = respx.post(f"{BASE}/responses")
    route.side_effect = lambda request: _sse(raw)
    adapter = _adapter()
    try:
        chunks = await _collect_stream(
            adapter,
            LLMRequest(model="resp-model", messages=[Message(role="user", content="count")], stream=True),
        )
    finally:
        await adapter.close()

    # Assert stream on the request (review R3-P2-10): the mock always returns SSE, so only this catches an omitted stream flag.
    assert json.loads(route.calls.last.request.content)["stream"] is True
    # Exact assertions (review R3-P2-6): loose assertions miss dropped, reordered, or duplicated frames.
    assert "".join(c.delta for c in chunks if c.delta) == "1\n2\n3"
    assert chunks[-1].finish_reason == "stop"
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.input_tokens == 14
    assert chunks[-1].usage.output_tokens == 5


@pytest.mark.unit
@respx.mock
async def test_responses_stream_sample_toolcall_bytes() -> None:
    """Preserve reasoning deltas, partial arguments and final tool-call identity."""
    raw = (_SAMPLES / "dashscope-stream-toolcall.txt").read_bytes()
    respx.post(f"{BASE}/responses").side_effect = lambda request: _sse(raw)
    adapter = _adapter()
    try:
        chunks = await _collect_stream(
            adapter,
            LLMRequest(model="resp-model", messages=[Message(role="user", content="weather")], tools=[_TOOL], stream=True),
        )
    finally:
        await adapter.close()

    thinking = "".join(c.thinking_delta for c in chunks if c.thinking_delta)
    # Compare with an independent mini-parser (review R3-P2-8): len>50 cannot detect dropped frames.
    expected_thinking = "".join(
        json.loads(line[5:].strip())["delta"]
        for line in raw.decode().splitlines()
        if line.startswith("data:") and '"response.reasoning_text.delta"' in line and json.loads(line[5:].strip()).get("delta")
    )
    assert thinking == expected_thinking and len(thinking) == 396
    partials = [c.tool_call_partial for c in chunks if c.tool_call_partial]
    assert partials and all(p["tool_name"] == "get_weather" for p in partials)
    assert "".join(p["partial_args_json"] for p in partials) == '{"city": "Paris"}'
    assert all(p["tool_call_id"] == "call_fixture_weather" for p in partials)
    tool_deltas = [c.tool_call_delta for c in chunks if c.tool_call_delta]
    assert len(tool_deltas) == 1
    assert tool_deltas[0].id == "call_fixture_weather"
    assert tool_deltas[0].arguments == {"city": "Paris"}
    assert chunks[-1].finish_reason == "tool_use"


class _FakeAttempt:
    def __init__(self) -> None:
        self.completed_usage = None
        self.failed: list[str] = []

    async def unknown(self, *, reason_code: str) -> None:
        self.failed.append(reason_code)

    async def confirmed_not_applied(self, *, evidence_code: str, evidence_digest: str) -> bool:
        return True

    async def complete(self, *, usage, response_digest: str) -> bool:
        self.completed_usage = usage
        return True


class _FakeHook:
    def __init__(self) -> None:
        self.specs = []
        self.attempts = []

    async def begin(self, spec):
        attempt = _FakeAttempt()
        self.specs.append(spec)
        self.attempts.append(attempt)
        return attempt


@pytest.mark.unit
@respx.mock
async def test_responses_stream_durable_bypasses_manifest_and_settles_exact_usage() -> None:
    """§9.3：durable 模式下 responses 流不撞 stream_options manifest 闸
    （usage 契约是协议原生的），completed 帧 usage 精确入账，账本 key 按面区分。"""
    raw = (_SAMPLES / "dashscope-stream.txt").read_bytes()
    respx.post(f"{BASE}/responses").side_effect = lambda request: _sse(raw)
    hook = _FakeHook()
    adapter = _adapter()
    try:
        with physical_invocation_scope(hook):
            chunks = await _collect_stream(
                adapter,
                LLMRequest(model="resp-model", messages=[Message(role="user", content="count")], stream=True),
            )
    finally:
        await adapter.close()

    assert chunks[-1].finish_reason == "stop"
    assert hook.specs and hook.specs[0].adapter_contract_key == "openai_compatible/openai-responses-v1/authoritative-stream-usage-v1"
    settled = hook.attempts[0].completed_usage
    assert settled is not None and settled.input_tokens == 55 and settled.output_tokens == 5
    assert not hook.attempts[0].failed


def _sse_frames(*objs: dict) -> bytes:
    return b"".join(b"data: " + json.dumps(obj).encode() + b"\n\n" for obj in objs)


@pytest.mark.unit
@respx.mock
async def test_responses_stream_failed_terminal_raises_typed() -> None:
    """⚠ 零真样本终态（合成帧）：response.failed ⇒ typed raise，upstream 字段保留。"""
    respx.post(f"{BASE}/responses").side_effect = lambda request: _sse(
        _sse_frames(
            {"type": "response.created", "response": {}},
            {"type": "response.failed", "response": {"error": {"code": "server_error", "message": "boom"}}},
        )
    )
    adapter = _adapter()
    try:
        with pytest.raises(OpenAICompatError) as exc:
            await _collect_stream(adapter, LLMRequest(model="resp-model", messages=[Message(role="user", content="x")], stream=True))
    finally:
        await adapter.close()
    # ★kind criterion (review R3-P1-1): without failed-branch detection, the missing-terminal-frame fallback raises
    # RESPONSE_INVALID. The exception class is the same; only asserting kind proves the branch is exercised.
    assert exc.value.kind is not ProviderFailureKind.RESPONSE_INVALID


@pytest.mark.unit
@respx.mock
async def test_responses_stream_error_event_and_bare_error_body_raise() -> None:
    respx.post(f"{BASE}/responses").side_effect = lambda request: _sse(_sse_frames({"type": "error", "code": "bad_request", "message": "nope"}))
    adapter = _adapter()
    try:
        with pytest.raises(OpenAICompatError) as exc1:
            await _collect_stream(adapter, LLMRequest(model="resp-model", messages=[Message(role="user", content="x")], stream=True))
        # Bare error body (DashScope gateway style, without type).
        respx.post(f"{BASE}/responses").side_effect = lambda request: _sse(
            _sse_frames({"request_id": "r1", "code": "InvalidParameter", "message": "Agent invalid parameter"})
        )
        with pytest.raises(OpenAICompatError) as exc2:
            await _collect_stream(adapter, LLMRequest(model="resp-model", messages=[Message(role="user", content="x")], stream=True))
    finally:
        await adapter.close()
    # ★kind criterion (review R3-P1-1): neither case may reach the missing-terminal-frame RESPONSE_INVALID fallback.
    assert exc1.value.kind is not ProviderFailureKind.RESPONSE_INVALID
    assert exc2.value.kind is not ProviderFailureKind.RESPONSE_INVALID


@pytest.mark.unit
@respx.mock
async def test_responses_stream_incomplete_maps_to_length() -> None:
    """⚠ 零真样本终态（合成帧）：incomplete/max_output_tokens ⇒ finish length。"""
    respx.post(f"{BASE}/responses").side_effect = lambda request: _sse(
        _sse_frames(
            {"type": "response.output_text.delta", "delta": "partial answ"},
            {
                "type": "response.incomplete",
                "response": {
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "usage": {"input_tokens": 9, "output_tokens": 100},
                },
            },
        )
    )
    adapter = _adapter()
    try:
        chunks = await _collect_stream(adapter, LLMRequest(model="resp-model", messages=[Message(role="user", content="x")], stream=True))
    finally:
        await adapter.close()
    assert chunks[-1].finish_reason == "length"
    assert chunks[-1].usage is not None and chunks[-1].usage.output_tokens == 100


@pytest.mark.unit
@respx.mock
async def test_responses_stream_without_terminal_is_response_invalid() -> None:
    """流断在中途（无 completed 帧）⇒ RESPONSE_INVALID retryable（框架终帧检查共用）。"""
    respx.post(f"{BASE}/responses").side_effect = lambda request: _sse(_sse_frames({"type": "response.output_text.delta", "delta": "hel"}))
    adapter = _adapter()
    try:
        with pytest.raises(OpenAICompatError) as exc:
            await _collect_stream(adapter, LLMRequest(model="resp-model", messages=[Message(role="user", content="x")], stream=True))
    finally:
        await adapter.close()
    assert exc.value.kind is ProviderFailureKind.RESPONSE_INVALID
    assert exc.value.retryable is True


# ─────────────────────────────────────────────────────────────
# §8.10-8: normalize wire names in rejected_param
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_rejected_param_normalizes_max_output_tokens_only_for_responses_family() -> None:
    """归一必须 family 门控（review R1-P2-1/R2 双路逮到）：chat 面的 override 可以把
    max_output_tokens 写进 body，无条件归一会删掉合法 max_tokens 并学错约束。"""
    body = json.dumps({"error": {"message": "unsupported", "param": "max_output_tokens", "type": "invalid_request_error"}})
    assert rejected_param(body, family="openai_responses") == "max_tokens"
    # ★Chat surface regression check: do not normalize with an absent family or the openai family.
    assert rejected_param(body) is None
    assert rejected_param(body, family="openai") is None
    # Semantic names match directly on both surfaces.
    body2 = json.dumps({"error": {"param": "max_tokens"}})
    assert rejected_param(body2) == "max_tokens"


# ─────────────────────────────────────────────────────────────
# §8.10-9: transport routing + not_testable probes (paired cases)
# ─────────────────────────────────────────────────────────────
def _provider(models: list) -> dict:
    return {
        "type": "openai_compatible",
        "base_url": BASE,
        "api_key": "sk-test",
        "models": models,
    }


@pytest.mark.unit
def test_transport_runtime_chat_forks_to_responses() -> None:
    plan = build_provider_transport_plan(
        _provider([{"id": "m1", "api_surface": "responses"}]),
        purpose="runtime_chat",
        model_id="m1",
        capability="chat",
    )
    assert plan.request_url == f"{BASE}/responses"
    assert plan.adapter_contract_version == "openai-responses-v1"


@pytest.mark.unit
def test_probe_plan_for_responses_model_targets_responses_shape() -> None:
    """批 2：探针真探 /responses（替换批 0 的 not_testable 钉住测试）。

    与真实调用同模式：URL/契约/body 形状全按 Responses 面；embed 与面无关不翻。"""
    registry = CompatibleProbeRegistry()
    plan = registry.plan(
        _provider([{"id": "m1", "api_surface": "responses", "capabilities": ["chat", "embed"]}]),
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="Main",
        model_id="m1",
        capability="chat",
    )
    assert not isinstance(plan, NotTestable)
    assert plan.transport.request_url == f"{BASE}/responses"
    assert plan.transport.adapter_contract_version == "openai-responses-v1"
    assert plan.probe_contract_version == "openai-responses-chat-v1"
    assert plan.body_shape["input"] == [{"role": "user", "content": "hello"}]
    assert plan.body_shape["store"] is False
    assert plan.body_shape["stream"] is True  # Match runtime calls, which stream after batch 1.
    assert plan.body_shape["max_output_tokens"] > 1  # Reasoning models consume the token budget; this follows the GPT5-tier rationale.
    assert "messages" not in plan.body_shape and "max_tokens" not in plan.body_shape
    # embed is unaffected by the API surface.
    plan3 = registry.plan(
        _provider([{"id": "m1", "api_surface": "responses"}]),
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="Main",
        model_id="m1",
        capability="embed",
    )
    assert not isinstance(plan3, NotTestable)
    assert plan3.transport.request_url == f"{BASE}/embeddings"


@pytest.mark.unit
def test_probe_plan_responses_tool_flat_and_outbound_mapped() -> None:
    """tool 探针：扁平 tools + outbound 声明走线上名映射 + overrides protected 换面。"""
    registry = CompatibleProbeRegistry()
    plan = registry.plan(
        _provider(
            [
                {
                    "id": "m1",
                    "api_surface": "responses",
                    "outbound_params": {"max_tokens": {"mode": "fixed", "value": 256}},
                    "request_body_overrides": {"tool": {"input": "CLOBBER", "reasoning": {"effort": "low"}}},
                },
            ]
        ),
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="Main",
        model_id="m1",
        capability="tool",
    )
    assert not isinstance(plan, NotTestable)
    body = plan.body_shape
    assert body["tools"][0]["name"] == "bowhead_connection_probe"
    assert "function" not in body["tools"][0]  # Flat shape, rather than Chat Completions nesting.
    assert body["max_output_tokens"] == 256  # Map fixed max_tokens to the wire name.
    assert "max_tokens" not in body
    assert body["input"] == [
        {"role": "user", "content": "You must call the bowhead_connection_probe tool now. Do not answer with text."}
    ]  # Protected: overrides must not clobber input.
    assert body["reasoning"] == {"effort": "low"}  # Merge unprotected keys as usual.


@pytest.mark.unit
async def test_probe_execute_responses_stream_bytes_verified(monkeypatch) -> None:
    """Classify a sanitized SSE completion using its decoded response shape."""
    import bowhead_router.probe as probe_module

    registry = CompatibleProbeRegistry()
    raw = (_SAMPLES / "dashscope-stream.txt").read_bytes()

    plan = registry.plan(
        _provider([{"id": "qwen-plus", "api_surface": "responses"}]),
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="Main",
        model_id="qwen-plus",
        capability="chat",
    )
    assert not isinstance(plan, NotTestable)

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url).endswith("/responses")
        return httpx2.Response(200, content=raw, headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(probe_module, "build_guarded_async_client", _probe_mock_factory(handler))
    result = await registry.execute(plan, timeout=10)
    assert result.terminal_state == "verified"
    assert result.code == "ok"
    assert result.actual_model_id == "qwen-plus"


@pytest.mark.unit
async def test_probe_execute_responses_toolcall_stream_bytes_verified(monkeypatch) -> None:
    """Recognize a function-call item in a sanitized tool stream."""
    import bowhead_router.probe as probe_module

    registry = CompatibleProbeRegistry()
    raw = (_SAMPLES / "dashscope-stream-toolcall.txt").read_bytes()
    plan = registry.plan(
        _provider([{"id": "qwen3.6-plus", "api_surface": "responses"}]),
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="Main",
        model_id="qwen3.6-plus",
        capability="tool",
    )
    assert not isinstance(plan, NotTestable)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=raw, headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(probe_module, "build_guarded_async_client", _probe_mock_factory(handler))
    result = await registry.execute(plan, timeout=10)
    assert result.terminal_state == "verified"
    assert result.code == "ok"


@pytest.mark.unit
async def test_probe_execute_responses_nonstream_json_and_rename_tolerated(monkeypatch) -> None:
    """Accept a JSON response and record model renaming without failing the probe."""
    import bowhead_router.probe as probe_module

    registry = CompatibleProbeRegistry()
    raw = (_SAMPLES / "deepseek-nonstream.json").read_bytes()  # model=deepseek-v4-flash
    plan = registry.plan(
        _provider([{"id": "deepseek-chat", "api_surface": "responses"}]),
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="Main",
        model_id="deepseek-chat",
        capability="chat",
    )
    assert not isinstance(plan, NotTestable)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=raw, headers={"content-type": "application/json"})

    monkeypatch.setattr(probe_module, "build_guarded_async_client", _probe_mock_factory(handler))
    result = await registry.execute(plan, timeout=10)
    assert result.terminal_state == "verified"
    assert result.actual_model_id == "deepseek-v4-flash"


# ─────────────────────────────────────────────────────────────
# §8.10-10: signature gate
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_model_request_shape_includes_api_surface_only_when_declared() -> None:
    declared = model_request_shape(
        _provider([{"id": "m1", "api_surface": "responses"}]),
        model_id="m1",
        capability="chat",
        adapter_type="openai_compatible",
    )
    assert declared["api_surface"] == "responses"
    undeclared = model_request_shape(
        _provider([{"id": "m1"}]),
        model_id="m1",
        capability="chat",
        adapter_type="openai_compatible",
    )
    assert "api_surface" not in undeclared
    assert undeclared == {}  # Existing deployments' shape stays byte-for-byte identical.


@pytest.mark.unit
def test_model_api_surface_single_implementation_defaults() -> None:
    provider = _provider([{"id": "m1", "api_surface": "responses"}, {"id": "m2"}])
    assert model_api_surface(provider, "m1") == "responses"
    assert model_api_surface(provider, "m2") == "chat_completions"
    assert model_api_surface(provider, "unknown") == "chat_completions"


# ─────────────────────────────────────────────────────────────
# §8.10-11: empty output ⇒ RESPONSE_INVALID (paired cases)
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
@respx.mock
async def test_responses_empty_output_is_response_invalid() -> None:
    """与 chat 面 choices 空同极性；静默接受空响应会喂进「payload 空时谎报」那族问题。"""
    respx.post(f"{BASE}/responses").respond(200, json=_responses_ok(output=[]))
    adapter = _adapter()
    try:
        with pytest.raises(OpenAICompatError) as exc:
            await adapter.invoke(LLMRequest(model="resp-model", messages=[Message(role="user", content="x")]))
    finally:
        await adapter.close()
    assert exc.value.kind is ProviderFailureKind.RESPONSE_INVALID


# ─────────────────────────────────────────────────────────────
# §8.10-12: preserve queries in base_url (required for api-version=preview)
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_base_url_query_survives_into_responses_request_url() -> None:
    plan = build_provider_transport_plan(
        {
            "type": "openai_compatible",
            "base_url": "https://api.example.com/openai/v1?api-version=preview",
            "api_key": "sk-test",
            "models": [{"id": "gpt-51", "api_surface": "responses"}],
        },
        purpose="runtime_chat",
        model_id="gpt-51",
        capability="chat",
    )
    assert plan.request_url == "https://api.example.com/openai/v1/responses?api-version=preview"


# ─────────────────────────────────────────────────────────────
# Verification criteria for implementation review fixes (R1/R2/R3/R4)
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
@respx.mock
async def test_responses_overrides_translate_chat_semantic_keys_and_store_protected() -> None:
    """R1-P1-1：存量 chat override 的 max_tokens 翻面后走线上名，不双键；
    R4：store 不许被 override 翻回 true。chat 正对照：max_tokens 原样。"""
    route = respx.post(f"{BASE}/responses").respond(200, json=_responses_ok())
    chat_route = respx.post(f"{BASE}/chat/completions").respond(
        200,
        json={"model": "chat-model", "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]},
    )
    adapter = OpenAICompatibleAdapter(
        base_url=BASE,
        api_key="sk-test",
        provider_name="test_provider",
        provider_options={
            "models": [
                {"id": "resp-model", "api_surface": "responses", "request_body_overrides": {"chat": {"max_tokens": 123, "store": True}}},
                {"id": "chat-model", "request_body_overrides": {"chat": {"max_tokens": 123}}},
            ],
        },
    )
    try:
        await adapter.invoke(LLMRequest(model="resp-model", messages=[Message(role="user", content="x")]))
        await adapter.invoke(LLMRequest(model="chat-model", messages=[Message(role="user", content="x")]))
    finally:
        await adapter.close()

    body = json.loads(route.calls.last.request.content)
    assert body["max_output_tokens"] == 123  # Translate override keys to wire names.
    assert "max_tokens" not in body  # Do not emit both keys.
    assert body["store"] is False  # Protected: always send false, with no bypass.
    chat_body = json.loads(chat_route.calls.last.request.content)
    assert chat_body["max_tokens"] == 123  # Chat surface mapping is the identity; behavior is unchanged.


@pytest.mark.unit
@respx.mock
async def test_responses_param_rejection_retry_erases_wire_name(monkeypatch) -> None:
    """R3-P1-3：400 结构化指认 max_output_tokens ⇒ 自愈重试的第二发不带该键（接线端到端）。"""
    from bowhead_router.outbound import reset_learned_for_test

    reset_learned_for_test()
    calls: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        calls.append(body)
        if "max_output_tokens" in body:
            return httpx2.Response(
                400, json={"error": {"message": "unsupported parameter", "param": "max_output_tokens", "type": "invalid_request_error"}}
            )
        return httpx2.Response(200, json=_responses_ok())

    respx.post(f"{BASE}/responses").side_effect = handler
    adapter = _adapter()
    try:
        resp = await adapter.invoke(LLMRequest(model="resp-model", messages=[Message(role="user", content="x")]))
    finally:
        await adapter.close()
        reset_learned_for_test()
    assert resp.content == "pong"
    assert len(calls) == 2
    assert "max_output_tokens" in calls[0]
    assert "max_output_tokens" not in calls[1]  # Erase the wire parameter name for the learned constraint.


@pytest.mark.unit
@respx.mock
async def test_responses_stream_ignores_frames_after_terminal() -> None:
    """R1-P2-4：completed 之后的尾随 error 帧不许把已送达的流打成失败。"""
    respx.post(f"{BASE}/responses").side_effect = lambda request: _sse(
        _sse_frames(
            {"type": "response.output_text.delta", "delta": "done"},
            {"type": "response.completed", "response": {"usage": {"input_tokens": 5, "output_tokens": 1}}},
            {"type": "error", "code": "bad_request", "message": "late"},
        )
    )
    adapter = _adapter()
    try:
        chunks = await _collect_stream(adapter, LLMRequest(model="resp-model", messages=[Message(role="user", content="x")], stream=True))
    finally:
        await adapter.close()
    assert "".join(c.delta for c in chunks if c.delta) == "done"
    assert chunks[-1].finish_reason == "stop"


@pytest.mark.unit
@respx.mock
async def test_responses_stream_done_only_terminator_is_response_invalid() -> None:
    """R1-P2-6：只发 [DONE] 不发 completed 的兼容网关 ⇒ RESPONSE_INVALID（可 fallback），
    不许以 finish=None 伪装成功。"""
    body = _sse_frames({"type": "response.output_text.delta", "delta": "hel"}) + b"data: [DONE]\n\n"
    respx.post(f"{BASE}/responses").side_effect = lambda request: _sse(body)
    adapter = _adapter()
    try:
        with pytest.raises(OpenAICompatError) as exc:
            await _collect_stream(adapter, LLMRequest(model="resp-model", messages=[Message(role="user", content="x")], stream=True))
    finally:
        await adapter.close()
    assert exc.value.kind is ProviderFailureKind.RESPONSE_INVALID


@pytest.mark.unit
@respx.mock
async def test_responses_garbage_output_is_response_invalid_but_incomplete_empty_allowed() -> None:
    """R1-P2-5：output 非空但全垃圾的 completed ⇒ invalid（谎报族进料口）；
    成对正对照：incomplete 的空产出是诚实的，放行且 finish=length。"""
    respx.post(f"{BASE}/responses").respond(200, json=_responses_ok(output=["junk", 42]))
    adapter = _adapter()
    try:
        with pytest.raises(OpenAICompatError) as exc:
            await adapter.invoke(LLMRequest(model="resp-model", messages=[Message(role="user", content="x")]))
        respx.post(f"{BASE}/responses").respond(
            200,
            json=_responses_ok(
                output=[{"type": "reasoning", "summary": []}], status="incomplete", incomplete_details={"reason": "max_output_tokens"}
            ),
        )
        resp = await adapter.invoke(LLMRequest(model="resp-model", messages=[Message(role="user", content="x")]))
    finally:
        await adapter.close()
    assert exc.value.kind is ProviderFailureKind.RESPONSE_INVALID
    assert resp.content == "" and resp.finish_reason == "length"


@pytest.mark.unit
@respx.mock
async def test_responses_incomplete_wins_over_tool_use() -> None:
    """R1-P2-2：参数被截断的 function_call + incomplete ⇒ finish=length（chat 面同语义），
    不许报 tool_use 让下游拿垃圾参数执行工具。"""
    respx.post(f"{BASE}/responses").respond(
        200,
        json=_responses_ok(
            status="incomplete",
            incomplete_details={"reason": "max_output_tokens"},
            output=[{"type": "function_call", "call_id": "call_t", "name": "get_weather", "arguments": '{"city": "Par'}],
        ),
    )
    adapter = _adapter()
    try:
        resp = await adapter.invoke(LLMRequest(model="resp-model", messages=[Message(role="user", content="x")], tools=[_TOOL]))
    finally:
        await adapter.close()
    assert resp.finish_reason == "length"
    # Non-streaming fallback uses {"_raw": …}, as in chat invoke (_args_parse_error belongs to streaming).
    assert resp.tool_calls and "_raw" in resp.tool_calls[0].arguments


@pytest.mark.unit
@respx.mock
async def test_responses_stream_done_frame_empty_args_uses_buffer() -> None:
    """R1-P2-3：done 帧显式给空串 arguments 时用累积 buffer，不许以 {} 执行工具。"""
    respx.post(f"{BASE}/responses").side_effect = lambda request: _sse(
        _sse_frames(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"type": "function_call", "id": "fc1", "call_id": "call_b", "name": "get_weather", "arguments": ""},
            },
            {"type": "response.function_call_arguments.delta", "item_id": "fc1", "delta": '{"a"'},
            {"type": "response.function_call_arguments.delta", "item_id": "fc1", "delta": ": 1}"},
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {"type": "function_call", "id": "fc1", "call_id": "call_b", "name": "get_weather", "arguments": ""},
            },
            {"type": "response.completed", "response": {"usage": {"input_tokens": 3, "output_tokens": 2}}},
        )
    )
    adapter = _adapter()
    try:
        chunks = await _collect_stream(
            adapter, LLMRequest(model="resp-model", messages=[Message(role="user", content="x")], tools=[_TOOL], stream=True)
        )
    finally:
        await adapter.close()
    tool_deltas = [c.tool_call_delta for c in chunks if c.tool_call_delta]
    assert tool_deltas and tool_deltas[0].arguments == {"a": 1}


@pytest.mark.unit
async def test_probe_execute_empty_completion_and_missing_tool_call_fail(monkeypatch) -> None:
    """R3-P1-2 成对负样本：分类守卫不许空转（_has_* 改 return True 必须让这里红）。"""
    import bowhead_router.probe as probe_module

    registry = CompatibleProbeRegistry()

    empty_payload = {
        "object": "response",
        "status": "completed",
        "model": "m1",
        "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "   "}]}],
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=empty_payload, headers={"content-type": "application/json"})

    monkeypatch.setattr(probe_module, "build_guarded_async_client", _probe_mock_factory(handler))
    plan = registry.plan(
        _provider([{"id": "m1", "api_surface": "responses"}]),
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="Main",
        model_id="m1",
        capability="chat",
    )
    result = await registry.execute(plan, timeout=10)
    assert result.terminal_state == "failed"
    assert result.code == "empty_completion"

    plan_tool = registry.plan(
        _provider([{"id": "m1", "api_surface": "responses"}]),
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="Main",
        model_id="m1",
        capability="tool",
    )
    empty_payload = {
        "object": "response",
        "status": "completed",
        "model": "m1",
        "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "I will not call tools"}]}],
    }
    result_tool = await registry.execute(plan_tool, timeout=10)
    assert result_tool.terminal_state == "failed"
    assert result_tool.code == "tool_call_missing"


@pytest.mark.unit
async def test_probe_execute_event_prefixed_sse_and_chat_choices_precedence(monkeypatch) -> None:
    """Decode event-prefixed SSE and prefer chat choices over unrelated output fields."""
    import bowhead_router.probe as probe_module

    registry = CompatibleProbeRegistry()
    raw = (_SAMPLES / "deepseek-stream.txt").read_bytes()
    assert raw.lstrip().startswith(b"event:")  # Ensure the fixture itself distinguishes the decoding path.

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=raw, headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(probe_module, "build_guarded_async_client", _probe_mock_factory(handler))
    plan = registry.plan(
        _provider([{"id": "deepseek-chat", "api_surface": "responses"}]),
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="Main",
        model_id="deepseek-chat",
        capability="chat",
    )
    result = await registry.execute(plan, timeout=10)
    assert result.terminal_state == "verified"

    # A chat payload may contain an unrelated top-level output; choices must determine the decoding path.
    chat_payload = {"model": "m2", "choices": [{"message": {"content": "OK"}}], "output": ["junk"]}

    def chat_handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=chat_payload, headers={"content-type": "application/json"})

    monkeypatch.setattr(probe_module, "build_guarded_async_client", _probe_mock_factory(chat_handler))
    plan2 = registry.plan(
        _provider([{"id": "m2"}]),
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="Main",
        model_id="m2",
        capability="chat",
    )
    result2 = await registry.execute(plan2, timeout=10)
    assert result2.terminal_state == "verified"


@pytest.mark.unit
def test_probe_plan_responses_vision_uses_input_image_string() -> None:
    """R3-P2-12：vision 探针 body 形状（input_image 字符串形；零真样本，形状先钉住）。"""
    registry = CompatibleProbeRegistry()
    plan = registry.plan(
        _provider([{"id": "m1", "api_surface": "responses"}]),
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="Main",
        model_id="m1",
        capability="vision",
    )
    assert not isinstance(plan, NotTestable)
    blocks = plan.body_shape["input"][0]["content"]
    image_block = next(b for b in blocks if b["type"] == "input_image")
    assert isinstance(image_block["image_url"], str)  # String shape, rather than the Chat Completions object shape.


@pytest.mark.unit
@respx.mock
async def test_responses_durable_invoke_settles_exact_usage() -> None:
    """R3-P1-4：durable invoke 结账的 responses 键名分支——flag 翻开那天生产恰恰跑这条。"""
    respx.post(f"{BASE}/responses").respond(200, json=_responses_ok())
    hook = _FakeHook()
    adapter = _adapter()
    try:
        with physical_invocation_scope(hook):
            resp = await adapter.invoke(LLMRequest(model="resp-model", messages=[Message(role="user", content="x")]))
    finally:
        await adapter.close()
    assert resp.content == "pong"
    assert hook.specs and hook.specs[0].adapter_contract_key == "openai_compatible/openai-responses-v1"
    settled = hook.attempts[0].completed_usage
    assert settled is not None and settled.input_tokens == 10 and settled.output_tokens == 2 and settled.cached_tokens == 4
    assert not hook.attempts[0].failed


@pytest.mark.unit
@pytest.mark.parametrize("surface", ["chat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
@respx.mock
async def test_empty_output_diagnostics_retain_budget_reasoning_and_terminal_reason(surface: str, stream: bool) -> None:
    responses = surface == "responses"
    usage = (
        {"input_tokens": 10, "output_tokens": 4000, "output_tokens_details": {"reasoning_tokens": 4000}}
        if responses
        else {
            "prompt_tokens": 10,
            "completion_tokens": 4000,
            "completion_tokens_details": {"reasoning_tokens": 4000},
        }
    )
    terminal = {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [{"type": "reasoning", "summary": [{"type": "summary_text", "text": "Synthetic reasoning only."}]}],
        "usage": usage,
    }
    path = "responses" if responses else "chat/completions"
    if stream:
        events = (
            [{"type": "response.incomplete", "response": terminal}]
            if responses
            else [
                {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": usage},
            ]
        )
        wire = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        respx.post(f"{BASE}/{path}").respond(200, content=wire, headers={"content-type": "text/event-stream"})
    else:
        payload = terminal if responses else {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}], "usage": usage}
        respx.post(f"{BASE}/{path}").respond(200, json=payload)
    adapter = _adapter()
    request = LLMRequest(
        model="resp-model" if responses else "chat-model", messages=[Message(role="user", content="Write a method")], max_output_tokens=4000
    )
    try:
        if stream:
            chunks = [chunk async for chunk in adapter.stream(request)]
            diagnostics = chunks[-1].diagnostics
        else:
            diagnostics = (await adapter.invoke(request)).diagnostics
    finally:
        await adapter.close()
    assert diagnostics is not None
    fields = diagnostics.trace_fields()
    assert fields["reasoning_tokens"] == fields["output_token_limit"] == 4000
    assert fields["upstream_finish_reason"] == ("incomplete" if responses else "stop")
    assert fields["incomplete_reason"] == ("max_output_tokens" if responses else None)


@pytest.mark.unit
@pytest.mark.parametrize("value", [None, -1, True, "private-value", {"private": "value"}])
def test_optional_response_diagnostics_keep_unknown_observations_content_free(value) -> None:
    from bowhead_router.types import LLMResponseDiagnostics

    fields = LLMResponseDiagnostics(transport_mode="invoke", reasoning_tokens=value, output_token_limit=value, incomplete_reason=value).trace_fields()
    assert fields["reasoning_tokens"] is None and fields["output_token_limit"] is None
    assert fields["incomplete_reason"] == (None if value is None else "unknown")
    assert "private" not in json.dumps(fields)
