"""OpenAICompatibleAdapter 单元测试（respx mock HTTP）+ live smoke test."""

from __future__ import annotations

import json
import os

import httpx2
import pytest
import respx
from bowhead_router.adapters.openai_compat import OpenAICompatError
from bowhead_router.errors import ProviderFailureKind, sanitize_upstream_text

from bowhead_router import (
    ImageRef,
    LLMChunk,
    LLMRequest,
    Message,
    OpenAICompatibleAdapter,
    ToolCall,
    ToolSchema,
)

BASE = "https://api.example.com/v1"


@pytest.fixture
async def adapter():
    a = OpenAICompatibleAdapter(base_url=BASE, api_key="sk-test", provider_name="test_provider")
    try:
        yield a
    finally:
        await a.close()


# ─────────────────────────────────────────────────────────────
# Normal chat completion
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
@respx.mock
async def test_invoke_basic_completion(adapter: OpenAICompatibleAdapter) -> None:
    respx.post(f"{BASE}/chat/completions").respond(
        200,
        json={
            "id": "cmpl_xx",
            "model": "qwen-plus",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "你好！"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        },
    )

    resp = await adapter.invoke(
        LLMRequest(
            model="qwen-plus",
            messages=[Message(role="user", content="你好")],
        )
    )
    assert resp.content == "你好！"
    assert resp.tool_calls == []
    assert resp.finish_reason == "stop"
    assert resp.usage.input_tokens == 12
    assert resp.usage.output_tokens == 3
    assert resp.provider == "test_provider"


@pytest.mark.unit
@respx.mock
async def test_azure_invoke_uses_deployment_api_version_and_gpt5_tokens() -> None:
    endpoint = "https://api.example.com"
    route = respx.post(
        f"{endpoint}/openai/deployments/gpt-54/chat/completions",
        params={"api-version": "2024-10-21"},
    ).respond(
        200,
        json={
            "model": "gpt-54",
            "choices": [
                {
                    "message": {"content": "OK"},
                    "finish_reason": "stop",
                }
            ],
        },
    )
    adapter = OpenAICompatibleAdapter(
        base_url=endpoint,
        api_key="azure-secret",
        provider_name="uptimize",
        adapter_type="azure_openai",
        provider_options={
            "endpoint": endpoint,
            "api_version": "2024-10-21",
            "auth": {"type": "bearer"},
            "models": [{"id": "gpt-54", "capabilities": ["chat"]}],
        },
    )
    try:
        response = await adapter.invoke(
            LLMRequest(
                model="gpt-54",
                messages=[Message(role="user", content="hello")],
                max_output_tokens=12,
            )
        )
    finally:
        await adapter.close()

    assert response.content == "OK"
    sent = json.loads(route.calls[0].request.content)
    assert sent["max_completion_tokens"] == 12
    assert "max_tokens" not in sent
    assert route.calls[0].request.headers["authorization"] == "Bearer azure-secret"


@pytest.mark.unit
@respx.mock
async def test_azure_stream_uses_same_deployment_contract() -> None:
    endpoint = "https://api.example.com"
    route = respx.post(
        f"{endpoint}/openai/deployments/gpt-51/chat/completions",
        params={"api-version": "2024-10-21"},
    ).respond(
        200,
        content=(b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'),
        headers={"content-type": "text/event-stream"},
    )
    adapter = OpenAICompatibleAdapter(
        base_url=endpoint,
        api_key="azure-secret",
        adapter_type="azure_openai",
        provider_options={
            "endpoint": endpoint,
            "api_version": "2024-10-21",
            "models": [{"id": "gpt-51", "capabilities": ["chat"]}],
        },
    )
    try:
        chunks = [
            chunk
            async for chunk in adapter.stream(
                LLMRequest(
                    model="gpt-51",
                    messages=[Message(role="user", content="hello")],
                    max_output_tokens=9,
                )
            )
        ]
    finally:
        await adapter.close()

    sent = json.loads(route.calls[0].request.content)
    assert sent["stream"] is True
    assert sent["max_completion_tokens"] == 9
    assert chunks[0].delta == "OK"


@pytest.mark.unit
async def test_openai_tool_description_is_bounded_for_wire_contract() -> None:
    adapter = OpenAICompatibleAdapter(
        base_url=BASE,
        api_key="sk-test",
    )
    try:
        body = adapter._build_body(
            LLMRequest(
                model="gpt-51",
                messages=[Message(role="user", content="hello")],
                tools=[
                    ToolSchema(
                        name="clarification_ask",
                        description="界" * 3776,
                        input_schema={
                            "type": "object",
                            "properties": {},
                        },
                    ),
                ],
            ),
            stream=True,
        )
    finally:
        await adapter.close()

    function = body["tools"][0]["function"]
    assert function["name"] == "clarification_ask"
    assert function["description"] == "界" * 1024
    assert function["parameters"] == {
        "type": "object",
        "properties": {},
    }


@pytest.mark.unit
async def test_openai_tool_required_alternatives_are_normalized_for_wire_contract() -> None:
    adapter = OpenAICompatibleAdapter(
        base_url=BASE,
        api_key="sk-test",
        adapter_type="azure_openai",
        provider_options={
            "endpoint": BASE,
            "api_version": "2024-10-21",
            "models": [{"id": "gpt-51", "capabilities": ["chat", "tool"]}],
        },
    )
    try:
        body = adapter._build_body(
            LLMRequest(
                model="gpt-51",
                messages=[Message(role="user", content="hello")],
                tools=[
                    ToolSchema(
                        name="clarification_ask",
                        description="Ask one clarifying question.",
                        input_schema={
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "questions": {"type": "array"},
                                "candidates": {"type": "array"},
                                "fallback_text": {"type": "string"},
                            },
                            "anyOf": [
                                {"required": ["questions"]},
                                {"required": ["candidates"]},
                                {"required": ["fallback_text"]},
                            ],
                        },
                    ),
                ],
            ),
            stream=False,
        )
    finally:
        await adapter.close()

    parameters = body["tools"][0]["function"]["parameters"]
    assert "anyOf" not in parameters
    assert parameters["additionalProperties"] is False
    assert parameters["properties"] == {
        "questions": {"type": "array"},
        "candidates": {"type": "array"},
        "fallback_text": {"type": "string"},
    }


@pytest.mark.unit
async def test_openai_tool_non_required_any_of_is_preserved() -> None:
    adapter = OpenAICompatibleAdapter(
        base_url=BASE,
        api_key="sk-test",
    )
    try:
        body = adapter._build_body(
            LLMRequest(
                model="gpt-4o",
                messages=[Message(role="user", content="hello")],
                tools=[
                    ToolSchema(
                        name="accept_scalar",
                        description="Accept a string or number.",
                        input_schema={
                            "type": "object",
                            "properties": {
                                "value": {
                                    "anyOf": [
                                        {"type": "string"},
                                        {"type": "number"},
                                    ],
                                },
                            },
                        },
                    ),
                ],
            ),
            stream=False,
        )
    finally:
        await adapter.close()

    parameters = body["tools"][0]["function"]["parameters"]
    assert parameters["properties"]["value"]["anyOf"] == [
        {"type": "string"},
        {"type": "number"},
    ]


@pytest.mark.unit
@respx.mock
async def test_custom_http_invoke_uses_declared_body_contract() -> None:
    base_url = "https://gateway.example.com"
    route = respx.post(f"{base_url}/company/chat").respond(
        200,
        json={
            "model": "company-gpt",
            "choices": [
                {
                    "message": {"content": "OK"},
                    "finish_reason": "stop",
                }
            ],
        },
    )
    adapter = OpenAICompatibleAdapter(
        base_url=base_url,
        api_key="custom-secret",
        provider_name="internal",
        adapter_type="custom_http",
        provider_options={
            "auth": {
                "type": "secret_header",
                "header_name": "X-Internal-Key",
            },
            "transports": {
                "chat": {
                    "contract": "openai_chat_completions",
                    "request": {
                        "url_template": "{base_url}/company/chat",
                        "body_template": {
                            "model": {"$bowhead": "model"},
                            "history": {"$bowhead": "messages"},
                            "limit": {"$bowhead": "max_output_tokens"},
                            "stream": {"$bowhead": "stream"},
                        },
                    },
                }
            },
            "models": [
                {
                    "id": "company-gpt",
                    "capabilities": ["chat"],
                    "capability_transports": {"chat": "chat"},
                }
            ],
        },
    )
    try:
        response = await adapter.invoke(
            LLMRequest(
                model="company-gpt",
                messages=[Message(role="user", content="hello")],
                max_output_tokens=7,
            )
        )
    finally:
        await adapter.close()

    assert response.content == "OK"
    sent = json.loads(route.calls[0].request.content)
    assert sent == {
        "model": "company-gpt",
        "history": [{"role": "user", "content": "hello"}],
        "limit": 7,
        "stream": False,
    }
    assert route.calls[0].request.headers["x-internal-key"] == "custom-secret"


@pytest.mark.unit
@respx.mock
async def test_custom_http_accepts_full_url_without_base_url() -> None:
    request_url = "https://gateway.example.com/company/chat"
    route = respx.post(request_url).respond(
        200,
        json={
            "model": "company-gpt",
            "choices": [
                {
                    "message": {"content": "OK"},
                    "finish_reason": "stop",
                }
            ],
        },
    )
    adapter = OpenAICompatibleAdapter(
        base_url="",
        api_key="",
        provider_name="internal",
        adapter_type="custom_http",
        provider_options={
            "auth": {"type": "none"},
            "transports": {
                "chat": {
                    "contract": "openai_chat_completions",
                    "request": {
                        "url_template": request_url,
                        "body_template": {
                            "messages": {"$bowhead": "messages"},
                        },
                    },
                }
            },
            "models": [
                {
                    "id": "company-gpt",
                    "capabilities": ["chat"],
                    "capability_transports": {"chat": "chat"},
                }
            ],
        },
    )
    try:
        response = await adapter.invoke(
            LLMRequest(
                model="company-gpt",
                messages=[Message(role="user", content="hello")],
            )
        )
    finally:
        await adapter.close()

    assert response.content == "OK"
    assert len(route.calls) == 1


@pytest.mark.unit
@respx.mock
async def test_custom_http_non_stream_contract_adapts_to_stream_interface() -> None:
    base_url = "https://gateway.example.com"
    route = respx.post(f"{base_url}/company/chat").respond(
        200,
        json={
            "model": "company-gpt",
            "choices": [
                {
                    "message": {"content": "one response"},
                    "finish_reason": "stop",
                }
            ],
        },
    )
    adapter = OpenAICompatibleAdapter(
        base_url=base_url,
        api_key="custom-secret",
        adapter_type="custom_http",
        provider_options={
            "transports": {
                "chat": {
                    "contract": "openai_chat_completions",
                    "request": {
                        "url_template": "{base_url}/company/chat",
                        "body_template": {
                            "messages": {"$bowhead": "messages"},
                        },
                    },
                }
            },
            "models": [
                {
                    "id": "company-gpt",
                    "capabilities": ["chat"],
                    "capability_transports": {"chat": "chat"},
                }
            ],
        },
    )
    try:
        chunks = [
            chunk
            async for chunk in adapter.stream(
                LLMRequest(
                    model="company-gpt",
                    messages=[Message(role="user", content="hello")],
                )
            )
        ]
    finally:
        await adapter.close()

    assert route.call_count == 1
    assert chunks[0].delta == "one response"
    assert chunks[-1].finish_reason == "stop"
    assert chunks[-1].diagnostics is not None
    assert chunks[-1].diagnostics.trace_fields() == {
        "mode": "invoke",
        "upstream_tool_call_count": 0,
        "upstream_finish_reason": "stop",
        "legacy_function_call": False,
        "incomplete_reason": None,
        "reasoning_tokens": None,
        "output_token_limit": None,
    }


@pytest.mark.unit
@respx.mock
async def test_invoke_with_system_and_history(
    adapter: OpenAICompatibleAdapter,
) -> None:
    route = respx.post(f"{BASE}/chat/completions").respond(
        200,
        json={
            "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 1},
        },
    )
    await adapter.invoke(
        LLMRequest(
            model="qwen-plus",
            system="你是 Bowhead Lead Agent",
            messages=[
                Message(role="user", content="hi"),
                Message(role="assistant", content="hello"),
                Message(role="user", content="continue"),
            ],
        )
    )
    # Verify that system is moved to messages[0].
    body = json.loads(route.calls[0].request.content)
    assert body["messages"][0]["role"] == "system"
    assert "Bowhead" in body["messages"][0]["content"]
    assert [m["role"] for m in body["messages"]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]


# ─────────────────────────────────────────────────────────────
# Tool calling: the LLM returns tool_calls.
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
@respx.mock
async def test_invoke_returns_tool_call(adapter: OpenAICompatibleAdapter) -> None:
    respx.post(f"{BASE}/chat/completions").respond(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "type": "function",
                                "function": {
                                    "name": "bi_ask_question",
                                    "arguments": json.dumps(
                                        {
                                            "workspace_id": "1",
                                            "text": "今年 GMV",
                                        }
                                    ),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 30, "completion_tokens": 20},
        },
    )

    resp = await adapter.invoke(
        LLMRequest(
            model="qwen-plus",
            messages=[Message(role="user", content="今年 GMV")],
            tools=[
                ToolSchema(
                    name="bi_ask_question",
                    description="ask BI",
                    input_schema={"type": "object"},
                )
            ],
        )
    )
    assert resp.finish_reason == "tool_use"
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "bi_ask_question"
    assert resp.tool_calls[0].arguments == {
        "workspace_id": "1",
        "text": "今年 GMV",
    }


@pytest.mark.unit
@respx.mock
async def test_invoke_sends_tools_in_openai_format(
    adapter: OpenAICompatibleAdapter,
) -> None:
    route = respx.post(f"{BASE}/chat/completions").respond(
        200,
        json={
            "choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
        },
    )

    await adapter.invoke(
        LLMRequest(
            model="m",
            messages=[Message(role="user", content="q")],
            tools=[
                ToolSchema(
                    name="bi_ask_question",
                    description="NL query",
                    input_schema={
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                )
            ],
        )
    )

    body = json.loads(route.calls[0].request.content)
    assert "tools" in body
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["name"] == "bi_ask_question"
    assert body["tool_choice"] == "auto"


@pytest.mark.unit
@respx.mock
async def test_assistant_tool_calls_are_serialized_in_history(
    adapter: OpenAICompatibleAdapter,
) -> None:
    """多轮 tool-use：第 2 轮请求里必须带第 1 轮的 assistant.tool_calls + tool role."""
    from bowhead_router.types import ToolCall

    route = respx.post(f"{BASE}/chat/completions").respond(
        200,
        json={
            "choices": [{"message": {"content": "final"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 1},
        },
    )

    await adapter.invoke(
        LLMRequest(
            model="m",
            messages=[
                Message(role="user", content="q"),
                Message(role="assistant", content="", tool_calls=[ToolCall(id="c1", name="bi_ask_question", arguments={"text": "GMV"})]),
                Message(role="tool", content='{"rows":[]}', tool_call_id="c1", name="bi_ask_question"),
            ],
        )
    )

    body = json.loads(route.calls[0].request.content)
    messages = body["messages"]
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] is None
    assert messages[1]["tool_calls"][0]["id"] == "c1"
    assert messages[1]["tool_calls"][0]["function"]["name"] == "bi_ask_question"
    assert messages[2]["role"] == "tool"
    assert messages[2]["tool_call_id"] == "c1"


# ─────────────────────────────────────────────────────────────
# Error mapping
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
@respx.mock
async def test_401_auth_error(adapter: OpenAICompatibleAdapter) -> None:
    respx.post(f"{BASE}/chat/completions").respond(
        401,
        json={"error": {"message": "invalid api key"}},
    )
    with pytest.raises(OpenAICompatError) as exc:
        await adapter.invoke(
            LLMRequest(
                model="m",
                messages=[Message(role="user", content="x")],
            )
        )
    assert not exc.value.retryable
    assert exc.value.status_code == 401


@pytest.mark.unit
@respx.mock
async def test_429_rate_limit_retryable(adapter: OpenAICompatibleAdapter) -> None:
    respx.post(f"{BASE}/chat/completions").respond(
        429,
        json={"error": {"message": "rate limit"}},
    )
    with pytest.raises(OpenAICompatError) as exc:
        await adapter.invoke(
            LLMRequest(
                model="m",
                messages=[Message(role="user", content="x")],
            )
        )
    assert exc.value.retryable
    assert exc.value.status_code == 429


@pytest.mark.unit
@respx.mock
async def test_stream_429_rate_limit_retryable(adapter: OpenAICompatibleAdapter) -> None:
    respx.post(f"{BASE}/chat/completions").respond(
        429,
        json={"error": {"message": "rate limit"}},
    )

    with pytest.raises(OpenAICompatError) as exc:
        async for _ in adapter.stream(
            LLMRequest(
                model="m",
                messages=[Message(role="user", content="x")],
            )
        ):
            pass

    assert exc.value.kind is ProviderFailureKind.RATE_LIMITED
    assert exc.value.retryable
    assert exc.value.status_code == 429


@pytest.mark.unit
@respx.mock
async def test_5xx_retryable(adapter: OpenAICompatibleAdapter) -> None:
    respx.post(f"{BASE}/chat/completions").respond(
        500,
        json={
            "error": {
                "id": "error-123",
                "type": "UPTIMIZE NLP API Error",
                "message": "Internal server error Authorization: Bearer raw-secret",
            }
        },
        headers={"x-request-id": "request-456"},
    )
    with pytest.raises(OpenAICompatError) as exc:
        await adapter.invoke(
            LLMRequest(
                model="m",
                messages=[Message(role="user", content="x")],
            )
        )
    assert exc.value.retryable
    assert exc.value.kind is ProviderFailureKind.UPSTREAM
    assert exc.value.status_code == 500
    assert exc.value.upstream_error_id == "error-123"
    assert exc.value.upstream_error_type == "UPTIMIZE NLP API Error"
    assert exc.value.upstream_request_id == "request-456"
    assert "Internal server error" in (exc.value.upstream_message or "")
    assert "raw-secret" not in (exc.value.upstream_message or "")
    assert "Internal server error" not in str(exc.value)


@pytest.mark.unit
@respx.mock
async def test_invoke_2xx_invalid_json_is_typed_without_response_body(
    adapter: OpenAICompatibleAdapter,
) -> None:
    respx.post(f"{BASE}/chat/completions").respond(200, text="secret response body")

    with pytest.raises(OpenAICompatError) as exc:
        await adapter.invoke(
            LLMRequest(
                model="m",
                messages=[Message(role="user", content="x")],
            )
        )

    assert exc.value.kind is ProviderFailureKind.RESPONSE_INVALID
    assert exc.value.status_code == 200
    assert exc.value.upstream_message is None
    assert "secret response body" not in str(exc.value)


@pytest.mark.unit
@respx.mock
async def test_stream_without_terminal_frame_is_typed_response_invalid(
    adapter: OpenAICompatibleAdapter,
) -> None:
    respx.post(f"{BASE}/chat/completions").respond(
        200,
        text='data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
        headers={"content-type": "text/event-stream"},
    )

    chunks: list[LLMChunk] = []
    with pytest.raises(OpenAICompatError) as exc:
        async for chunk in adapter.stream(
            LLMRequest(
                model="m",
                messages=[Message(role="user", content="x")],
            )
        ):
            chunks.append(chunk)

    assert [chunk.delta for chunk in chunks] == ["partial"]
    assert exc.value.kind is ProviderFailureKind.RESPONSE_INVALID
    assert exc.value.retryable is True
    assert exc.value.status_code == 200


@pytest.mark.unit
@respx.mock
async def test_stream_in_band_error_preserves_upstream_fields(
    adapter: OpenAICompatibleAdapter,
) -> None:
    respx.post(f"{BASE}/chat/completions").respond(
        200,
        text=(
            'data: {"error":{"id":"error-123","type":"UPTIMIZE NLP API Error",'
            '"message":"Internal server error Authorization: Bearer raw-secret"}}\n\n'
        ),
        headers={
            "content-type": "text/event-stream",
            "x-request-id": "request-456",
        },
    )

    with pytest.raises(OpenAICompatError) as exc:
        async for _ in adapter.stream(
            LLMRequest(
                model="m",
                messages=[Message(role="user", content="x")],
            )
        ):
            pass

    assert exc.value.kind is ProviderFailureKind.UPSTREAM
    assert exc.value.retryable is True
    assert exc.value.status_code is None
    assert exc.value.upstream_error_id == "error-123"
    assert exc.value.upstream_error_type == "UPTIMIZE NLP API Error"
    assert exc.value.upstream_request_id == "request-456"
    assert "Internal server error" in (exc.value.upstream_message or "")
    assert "raw-secret" not in (exc.value.upstream_message or "")


@pytest.mark.unit
@respx.mock
async def test_stream_in_band_request_error_is_not_retryable(
    adapter: OpenAICompatibleAdapter,
) -> None:
    respx.post(f"{BASE}/chat/completions").respond(
        200,
        text=('data: {"error":{"type":"invalid_request_error","message":"stream is not supported for this model"}}\n\n'),
        headers={"content-type": "text/event-stream"},
    )

    with pytest.raises(OpenAICompatError) as exc:
        async for _ in adapter.stream(
            LLMRequest(
                model="m",
                messages=[Message(role="user", content="x")],
            )
        ):
            pass

    assert exc.value.kind is ProviderFailureKind.REQUEST_REJECTED
    assert exc.value.retryable is False
    assert exc.value.status_code is None


@pytest.mark.unit
def test_upstream_message_scrubber_handles_quoted_and_key_shaped_secrets() -> None:
    scrubbed = sanitize_upstream_text('{"api_key":"sk-proj-abcdefghijk","access_token":"opaque-value"} Bearer another-secret')

    assert scrubbed is not None
    assert "sk-proj-abcdefghijk" not in scrubbed
    assert "opaque-value" not in scrubbed
    assert "another-secret" not in scrubbed


@pytest.mark.unit
@respx.mock
async def test_timeout_retryable(adapter: OpenAICompatibleAdapter) -> None:
    respx.post(f"{BASE}/chat/completions").side_effect = httpx2.TimeoutException("t")
    with pytest.raises(OpenAICompatError) as exc:
        await adapter.invoke(
            LLMRequest(
                model="m",
                messages=[Message(role="user", content="x")],
            )
        )
    assert exc.value.retryable


# ─────────────────────────────────────────────────────────────
# Finish reason mapping
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
@respx.mock
async def test_finish_reason_mapping(adapter: OpenAICompatibleAdapter) -> None:
    respx.post(f"{BASE}/chat/completions").respond(
        200,
        json={
            "choices": [{"message": {"content": "x"}, "finish_reason": "content_filter"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )
    resp = await adapter.invoke(
        LLMRequest(
            model="m",
            messages=[Message(role="user", content="x")],
        )
    )
    assert resp.finish_reason == "content_filter"


# ─────────────────────────────────────────────────────────────
# Live smoke test: real Qwen API (requires ALIYUN_DASHSCOPE_API_KEY).
# ─────────────────────────────────────────────────────────────
@pytest.mark.live
async def test_live_qwen_roundtrip() -> None:
    """真实调用 qwen-plus。只在 `pytest -m live` 时跑。"""
    key = os.environ.get("ALIYUN_DASHSCOPE_API_KEY")
    if not key:
        pytest.skip("ALIYUN_DASHSCOPE_API_KEY not set")

    adapter = OpenAICompatibleAdapter(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key=key,
        provider_name="aliyun_qwen",
    )
    try:
        resp = await adapter.invoke(
            LLMRequest(
                model="qwen-plus",
                messages=[
                    Message(
                        role="user",
                        content="用一句话介绍你自己，不超过 15 字",
                    )
                ],
                temperature=0.3,
                max_output_tokens=50,
            )
        )
        assert resp.content
        assert resp.usage.input_tokens > 0
        assert resp.usage.output_tokens > 0
    finally:
        await adapter.close()


@pytest.mark.live
async def test_live_qwen_tool_calling() -> None:
    """验证 Qwen 真实场景下能触发 tool_calls."""
    key = os.environ.get("ALIYUN_DASHSCOPE_API_KEY")
    if not key:
        pytest.skip("ALIYUN_DASHSCOPE_API_KEY not set")

    adapter = OpenAICompatibleAdapter(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key=key,
        provider_name="aliyun_qwen",
    )
    try:
        resp = await adapter.invoke(
            LLMRequest(
                model="qwen-plus",
                system=("You are a BI assistant. When user asks for data, call the bi.ask_question tool."),
                messages=[
                    Message(
                        role="user",
                        content="查一下今年的 GMV",
                    )
                ],
                tools=[
                    ToolSchema(
                        name="bi_ask_question",
                        description="Ask the BI in natural language",
                        input_schema={
                            "type": "object",
                            "properties": {
                                "text": {"type": "string", "description": "business NL question"},
                            },
                            "required": ["text"],
                        },
                    )
                ],
                temperature=0.1,
            )
        )
        # Do not require tool_use (the model may answer directly), but require at least a valid response.
        assert resp.content or resp.tool_calls
        if resp.tool_calls:
            pass
        else:
            pass
    finally:
        await adapter.close()


@pytest.mark.unit
async def test_per_model_request_body_overrides_applied_by_capability() -> None:
    """config-copilot 批0: V2ModelDeclaration.request_body_overrides 按 capability 合入 body
    (典型 = DashScope reasoning 模型 chat 档 enable_thinking=false), 运行时字段受保护."""
    adapter = OpenAICompatibleAdapter(
        base_url=BASE,
        api_key="sk-test",
        provider_options={
            "models": [
                {
                    "id": "qwen3.6-plus",
                    "capabilities": ["chat", "tool"],
                    "request_body_overrides": {
                        "chat": {"enable_thinking": False, "messages": [{"role": "user", "content": "evil"}]},
                        "tool": {"enable_thinking": True},
                    },
                },
            ],
        },
    )
    try:
        request = LLMRequest(model="qwen3.6-plus", messages=[Message(role="user", content="hi")])
        body = adapter._build_body(request, stream=False)
        assert body["enable_thinking"] is False
        assert body["messages"][0]["content"] == "hi"  # Runtime fields cannot be overridden.

        tool_request = LLMRequest(
            model="qwen3.6-plus",
            messages=[Message(role="user", content="hi")],
            tools=[ToolSchema(name="t", description="d", input_schema={"type": "object", "properties": {}})],
        )
        assert adapter._build_body(tool_request, stream=False)["enable_thinking"] is True

        other = LLMRequest(model="other-model", messages=[Message(role="user", content="hi")])
        assert "enable_thinking" not in adapter._build_body(other, stream=False)  # Models without declarations are unaffected.
    finally:
        await adapter.close()


# ─────────────────────────────────────────────────────────────
# Merge adjacent user messages (same normalization as anthropic_compat).
#
# 🔍 `BOWHEAD_REFS_IN_MESSAGES=1` emits `[user(task), user(refs)]` on the first iteration and
# `[…, tool, user(refs), user(bail)]` on the bail iteration. OpenAI allows consecutive same-role messages, but that was an
# **unverified assumption**: the previous failure came from treating official Anthropic API behavior as a guarantee for compatible endpoints.
# ⇒ Make outbound messages consistently alternate at assembly time instead of relying on each provider.
# ─────────────────────────────────────────────────────────────


def _mk() -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(base_url=BASE, api_key="sk-test", provider_name="test_provider")


def test_adjacent_user_messages_merge_into_one():
    """★ 主判据：出线不得出现两条连续 user。

    突变：删掉合并分支 ⇒ 本条红（roles 变成 user,user）。
    """
    out = _mk()._convert_messages(
        [
            Message(role="user", content="任务"),
            Message(role="user", content="<available_refs>…</available_refs>"),
        ],
        None,
    )
    assert [m["role"] for m in out] == ["user"], [m["role"] for m in out]
    # Merging must preserve all content.
    assert "任务" in out[0]["content"]
    assert "<available_refs>…</available_refs>" in out[0]["content"]


def test_user_after_tool_result_merges_nothing_but_stays_alternating():
    """tool 在 OpenAI face 是**独立 role**（不像 Anthropic 折进 user），
    所以 `[…, tool, user, user]` 只该合并末尾那两条 user，tool 原样保留。"""
    out = _mk()._convert_messages(
        [
            Message(role="user", content="任务"),
            Message(role="assistant", content="", tool_calls=[ToolCall(id="c1", name="kb_search", arguments={})]),
            Message(role="tool", content="命中", tool_call_id="c1", name="kb_search"),
            Message(role="user", content="refs"),
            Message(role="user", content="⚠️ 强制收尾"),
        ],
        None,
    )
    roles = [m["role"] for m in out]
    assert roles == ["user", "assistant", "tool", "user"], roles
    assert out[-1]["content"] == "refs\n\n⚠️ 强制收尾"


def test_alternating_conversation_is_untouched_openai():
    """★正对照：本来就交替的对话一个字节都不许动。

    没有它，「严格交替」也可以靠把所有消息压成一条来满足。
    """
    msgs = [
        Message(role="user", content="Q1"),
        Message(role="assistant", content="A1"),
        Message(role="user", content="Q2"),
    ]
    out = _mk()._convert_messages(msgs, "SYS")
    assert out == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "Q2"},
    ]


def test_multimodal_user_is_merged_too():
    """★ 带 image block 的 user **也必须合并**。

    这条是 review 打回来的：我上一版把多模态排除在外，于是 Lead 轮带图
    （`_lead_message_images`）且 allowlist 非空（任何第 2 轮起）时，首圈出线仍是
    `[user(task,images), user(refs)]` —— 两条连续 user。默认翻开之后那是**默认路径**，
    而代码与文档三处写着「出线恒定严格交替、不依赖 provider 宽容度」，不修就是假话。
    突变：把合并条件加回 `isinstance(..., str)` 限制 ⇒ 本条红。
    """
    out = _mk()._convert_messages(
        [
            Message(role="user", content="看图", images=[ImageRef(mime="image/png", base64="AAA")]),
            Message(role="user", content="refs"),
        ],
        None,
    )
    assert [m["role"] for m in out] == ["user"], [m["role"] for m in out]
    blocks = out[0]["content"]
    assert isinstance(blocks, list)
    # Preserve the exact order: text(看图) → image → text(refs).
    assert [b["type"] for b in blocks] == ["text", "image_url", "text"], [b["type"] for b in blocks]
    assert blocks[0]["text"] == "看图"
    assert blocks[-1]["text"] == "refs"


def test_plain_text_pair_stays_a_string():
    """两边都是纯文本时保持字符串形态——别无谓把所有 user 都升格成 block list。"""
    out = _mk()._convert_messages(
        [Message(role="user", content="a"), Message(role="user", content="b")],
        None,
    )
    assert out[0]["content"] == "a\n\nb"


@pytest.mark.unit
@respx.mock
async def test_call_diagnostics_do_not_publish_arbitrary_finish_reason(adapter: OpenAICompatibleAdapter) -> None:
    secret = "sk-secret-arbitrary-provider-value"
    respx.post(f"{BASE}/chat/completions").respond(
        200,
        json={
            "choices": [{"message": {"content": secret}, "finish_reason": secret}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )
    response = await adapter.invoke(LLMRequest(model="m", messages=[Message(role="user", content=secret)]))
    assert response.content == secret
    assert response.diagnostics is not None
    fields = response.diagnostics.trace_fields()
    assert fields["upstream_finish_reason"] == "unknown"
    assert fields["upstream_tool_call_count"] == 0
    assert secret not in json.dumps(fields)


@pytest.mark.unit
async def test_openai_tool_choice_none_keeps_tools_but_forbids_calls() -> None:
    """The wrap-up call at a run limit keeps tool history valid but must not call tools."""
    adapter = OpenAICompatibleAdapter(base_url=BASE, api_key="sk-test")
    try:
        body = adapter._build_body(
            LLMRequest(
                model="gpt-51",
                messages=[Message(role="user", content="summarize")],
                tools=[ToolSchema(name="bi_ask_question", description="Query BI", input_schema={"type": "object"})],
                tool_choice="none",
            ),
            stream=False,
        )
    finally:
        await adapter.close()
    assert body["tools"][0]["function"]["name"] == "bi_ask_question"
    assert body["tool_choice"] == "none"
    assert "parallel_tool_calls" not in body
