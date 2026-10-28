"""WS4 — bowhead_router.verify.verify_provider unit tests.

Two layers:
  * classification/protocol: monkeypatch build_guarded_async_client → MockTransport,
    so we can assert the exact request the probe sends (method/path/headers/body)
    and how each status code maps to a VerifyResult, with no network.
  * SSRF wiring: call through the REAL guarded client with a private/http target
    and assert it is blocked before any connection (proves the guard is engaged).
"""

from __future__ import annotations

import base64
import json

import bowhead_router.verify as vmod
import httpx2

from bowhead_router import (
    discover_provider_model_candidates,
    verify_provider,
    verify_provider_capabilities,
)


def _mock_factory(handler):
    """Return a build_guarded_async_client replacement that yields a MockTransport
    client but still honors the base_url/timeout/headers kwargs verify passes."""

    def _factory(*, resolver=None, **kwargs):
        return httpx2.AsyncClient(transport=httpx2.MockTransport(handler), **kwargs)

    return _factory


# ─── openai_compatible: GET /models ────────────────────────────────


async def test_openai_ok_with_catalog(monkeypatch):
    seen: dict = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        return httpx2.Response(200, json={"data": [{"id": "m1"}, {"id": "m2"}]})

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    r = await verify_provider(provider_type="openai_compatible", base_url="https://api.x.com/v1", api_key="sk-abc")
    assert r.ok and r.code == "ok" and r.model_count == 2
    assert seen["method"] == "GET"
    assert seen["path"].endswith("/models")
    assert seen["auth"] == "Bearer sk-abc"


async def test_openai_top_level_list_ok(monkeypatch):
    def handler(request):
        return httpx2.Response(200, json=[{"id": "m1"}])

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    r = await verify_provider(provider_type="openai_compatible", base_url="https://api.x.com/v1", api_key="sk-abc")
    assert r.ok and r.code == "ok" and r.model_count == 1


async def test_openai_empty_catalog(monkeypatch):
    def handler(request):
        return httpx2.Response(200, json={"data": []})

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    r = await verify_provider(provider_type="openai_compatible", base_url="https://api.x.com/v1", api_key="sk-abc")
    assert not r.ok and r.code == "empty_catalog"


async def test_openai_unauthorized(monkeypatch):
    def handler(request):
        return httpx2.Response(401, json={"error": "bad key"})

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    r = await verify_provider(provider_type="openai_compatible", base_url="https://api.x.com/v1", api_key="sk-bad")
    assert not r.ok and r.code == "unauthorized" and r.http_status == 401


async def test_openai_server_error(monkeypatch):
    def handler(request):
        return httpx2.Response(500, text="boom")

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    r = await verify_provider(provider_type="openai_compatible", base_url="https://api.x.com/v1", api_key="sk-abc")
    assert not r.ok and r.code == "error" and r.http_status == 500


async def test_v2_discovery_deduplicates_caps_and_bounds_metadata(
    monkeypatch,
):
    calls = {"count": 0}
    seen_headers: dict[str, str | None] = {}
    items = [
        {
            "id": f"model-{index}",
            "owned_by": "x" * 500,
            "nested": {"must": "drop"},
        }
        for index in range(1002)
    ]
    items.insert(1, {"id": "model-0", "owned_by": "duplicate"})

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["count"] += 1
        seen_headers["tenant"] = request.headers.get("x-tenant-id")
        return httpx2.Response(200, json={"data": items})

    monkeypatch.setattr(
        vmod,
        "build_guarded_async_client",
        _mock_factory(handler),
    )
    result = await discover_provider_model_candidates(
        base_url="https://api.x.com/v1",
        api_key="secret",
        default_headers={"X-Tenant-ID": "tenant-cn"},
    )

    assert calls["count"] == 1
    assert seen_headers == {"tenant": "tenant-cn"}
    assert result["ok"] is True
    assert result["returned_count"] == 1000
    assert result["truncated"] is True
    assert len(result["candidates"][0]["upstream_metadata"]["owned_by"]) == 200
    assert "nested" not in result["candidates"][0]["upstream_metadata"]


async def test_v2_discovery_enforces_response_byte_limit(monkeypatch):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b'{"data":[' + b"x" * 100 + b"]}")

    monkeypatch.setattr(
        vmod,
        "build_guarded_async_client",
        _mock_factory(handler),
    )
    result = await discover_provider_model_candidates(
        base_url="https://api.x.com/v1",
        api_key="secret",
        response_max_bytes=32,
    )

    assert result["ok"] is False
    assert result["code"] == "discovery_response_too_large"


async def test_transport_timeout_is_unreachable(monkeypatch):
    def handler(request):
        raise httpx2.ConnectTimeout("slow", request=request)

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    r = await verify_provider(provider_type="openai_compatible", base_url="https://api.x.com/v1", api_key="sk-abc")
    assert not r.ok and r.code == "unreachable"


# ─── anthropic_compatible: POST /v1/messages max_tokens:1 ──────────


async def test_anthropic_ok(monkeypatch):
    seen: dict = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["x_api_key"] = request.headers.get("x-api-key")
        seen["version"] = request.headers.get("anthropic-version")
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    r = await verify_provider(
        provider_type="anthropic_compatible",
        base_url="https://api.y.com",
        api_key="sk-abc",
        model="claude-x",
    )
    assert r.ok and r.code == "ok"
    assert seen["method"] == "POST"
    assert seen["path"].endswith("/v1/messages")
    assert seen["x_api_key"] == "sk-abc"
    assert seen["version"] == "2023-06-01"
    assert seen["body"]["max_tokens"] == 1
    assert seen["body"]["model"] == "claude-x"


async def test_anthropic_requires_model_no_network(monkeypatch):
    called = {"n": 0}

    def handler(request):  # must NOT be reached
        called["n"] += 1
        return httpx2.Response(200)

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    r = await verify_provider(provider_type="anthropic_compatible", base_url="https://api.y.com", api_key="sk-abc")
    assert not r.ok and r.code == "model_required"
    assert called["n"] == 0


async def test_anthropic_unauthorized(monkeypatch):
    def handler(request):
        return httpx2.Response(401)

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    r = await verify_provider(
        provider_type="anthropic_compatible",
        base_url="https://api.y.com",
        api_key="sk-bad",
        model="claude-x",
    )
    assert not r.ok and r.code == "unauthorized"


async def test_anthropic_404_is_model_not_found(monkeypatch):
    def handler(request):
        return httpx2.Response(404, json={"error": {"message": "model not found"}})

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    r = await verify_provider(
        provider_type="anthropic_compatible",
        base_url="https://api.y.com",
        api_key="sk-abc",
        model="nope",
    )
    assert not r.ok and r.code == "model_not_found"


async def test_anthropic_honors_messages_path(monkeypatch):
    seen: dict = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["path"] = request.url.path
        return httpx2.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    r = await verify_provider(
        provider_type="anthropic_compatible",
        base_url="https://api.y.com",
        api_key="sk-abc",
        model="claude-x",
        messages_path="/messages",
    )
    assert r.ok
    assert seen["path"].endswith("/messages")
    assert not seen["path"].endswith("/v1/messages")


async def test_anthropic_429_still_ok(monkeypatch):
    def handler(request):
        return httpx2.Response(429)

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    r = await verify_provider(
        provider_type="anthropic_compatible",
        base_url="https://api.y.com",
        api_key="sk-abc",
        model="claude-x",
    )
    assert r.ok and r.code == "ok"


async def test_unsupported_type():
    r = await verify_provider(provider_type="cohere", base_url="https://api.z.com", api_key="sk-abc")
    assert not r.ok and r.code == "error"


# ─── staged declared-capability probes ─────────────────────────────


async def test_openai_capability_probes_validate_tool_and_embedding(monkeypatch):
    seen: list[tuple[str, str, dict]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content) if request.content else {}
        seen.append((request.method, request.url.path, body))
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": [{"id": "m1"}]})
        if request.url.path.endswith("/embeddings"):
            return httpx2.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]})
        if body.get("stream") is True:
            return httpx2.Response(
                200,
                text='data: {"choices":[{"delta":{"content":"OK"}}]}\n\ndata: [DONE]\n\n',
                headers={"content-type": "text/event-stream"},
            )
        return httpx2.Response(200, json={"choices": [{"message": {"tool_calls": [{"id": "call_1"}]}}]})

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    result = await verify_provider_capabilities(
        provider_type="openai_compatible",
        base_url="https://api.x.com/v1",
        api_key="sk-abc",
        models=[{"id": "m1", "capabilities": ["tool", "embed"]}],
    )

    assert result["ok"] is True
    assert [(step["capability"], step["ok"]) for step in result["steps"]] == [("tool", True), ("embed", True)]
    assert not any(path.endswith("/models") for _, path, _ in seen)
    tool_request = next(body for _, path, body in seen if path.endswith("/chat/completions"))
    assert tool_request["tool_choice"] == "auto"
    assert tool_request["max_tokens"] == 128
    assert "bowhead_connection_probe" in tool_request["messages"][0]["content"]


async def test_openai_tool_probe_accepts_runtime_auto_when_named_choice_is_rejected(monkeypatch):
    seen_choices: list[object] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": [{"id": "deepseek-v4-pro"}]})
        body = json.loads(request.content)
        seen_choices.append(body.get("tool_choice"))
        if body.get("stream") is True:
            return httpx2.Response(
                200,
                text='data: {"choices":[{"delta":{"content":"OK"}}]}\n\ndata: [DONE]\n\n',
                headers={"content-type": "text/event-stream"},
            )
        if isinstance(body.get("tool_choice"), dict):
            return httpx2.Response(
                400,
                json={
                    "error": {
                        "message": "Thinking mode does not support this tool_choice",
                        "type": "invalid_request_error",
                    }
                },
            )
        return httpx2.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call_probe",
                                    "type": "function",
                                    "function": {
                                        "name": "bowhead_connection_probe",
                                        "arguments": "{}",
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    result = await verify_provider_capabilities(
        provider_type="openai_compatible",
        base_url="https://api.deepseek.com",
        api_key="sk-abc",
        models=[{"id": "deepseek-v4-pro", "capabilities": ["tool"]}],
    )

    assert result["ok"] is True
    assert seen_choices == ["auto", "auto"]


async def test_capability_failure_does_not_skip_later_models(monkeypatch):
    calls = {"chat": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": [{"id": "m1"}, {"id": "m2"}]})
        body = json.loads(request.content)
        calls["chat"] += 1
        if body["model"] == "m1":
            return httpx2.Response(200, json={"choices": [{"message": {}}]})
        if body.get("stream") is True:
            return httpx2.Response(
                200,
                text='data: {"choices":[{"delta":{"content":"OK"}}]}\n\ndata: [DONE]\n\n',
                headers={"content-type": "text/event-stream"},
            )
        return httpx2.Response(200, json={"choices": [{"message": {"tool_calls": [{"id": "call_2"}]}}]})

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    result = await verify_provider_capabilities(
        provider_type="openai_compatible",
        base_url="https://api.x.com/v1",
        api_key="sk-abc",
        models=[
            {"id": "m1", "capabilities": ["tool"]},
            {"id": "m2", "capabilities": ["tool"]},
        ],
    )

    assert result["ok"] is False
    assert [step["code"] for step in result["steps"]] == ["tool_call_missing", "ok"]
    assert calls["chat"] == 3


async def test_chat_probe_reports_degraded_when_only_streaming_fails(monkeypatch):
    seen_stream: list[bool] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        streaming = body.get("stream") is True
        seen_stream.append(streaming)
        if streaming:
            return httpx2.Response(
                500,
                json={
                    "error": {
                        "id": "upstream-123",
                        "type": "UPTIMIZE_NLP_API_ERROR",
                        "message": "Internal server error Authorization: Bearer raw-secret",
                    }
                },
            )
        return httpx2.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "OK"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    result = await verify_provider_capabilities(
        provider_type="openai_compatible",
        base_url="https://api.x.com/v1",
        api_key="sk-abc",
        models=[{"id": "m1", "capabilities": ["chat"]}],
    )

    assert result["ok"] is False
    assert result["code"] == "capability_degraded"
    assert seen_stream == [True, False]
    assert result["steps"] == [
        {
            "step": "capability",
            "ok": False,
            "code": "stream_degraded",
            "status": "degraded",
            "model": "m1",
            "capability": "chat",
            "phase": "stream",
            "http_status": 500,
            "message": "非流式调用可用，但流式调用失败",
        }
    ]
    assert "raw-secret" not in str(result)
    assert "Internal server error" not in str(result)


async def test_chat_probe_rejects_stream_4xx_even_when_invoke_would_succeed(
    monkeypatch,
):
    seen_stream: list[bool] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        streaming = body.get("stream") is True
        seen_stream.append(streaming)
        if streaming:
            return httpx2.Response(
                400,
                json={
                    "error": {
                        "type": "unsupported_parameter",
                        "message": "stream is not supported",
                    }
                },
            )
        return httpx2.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "OK"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    result = await verify_provider_capabilities(
        provider_type="openai_compatible",
        base_url="https://api.x.com/v1",
        api_key="sk-abc",
        models=[{"id": "m1", "capabilities": ["chat"]}],
    )

    assert result["ok"] is False
    assert result["code"] == "capability_failed"
    assert seen_stream == [True]
    assert result["steps"][0] == {
        "step": "capability",
        "ok": False,
        "code": "bad_request",
        "status": "failed",
        "model": "m1",
        "capability": "chat",
        "phase": "stream",
        "http_status": 400,
        "message": "模型服务拒绝了探针请求",
    }


async def test_capability_probe_returns_finite_failure_for_invalid_base_url():
    result = await verify_provider_capabilities(
        provider_type="openai_compatible",
        base_url=":::",
        api_key="sk-abc",
        models=[{"id": "m1", "capabilities": ["chat"]}],
    )

    assert result["ok"] is False
    assert result["code"] == "capability_failed"
    assert result["steps"][0] == {
        "step": "capability",
        "ok": False,
        "code": "bad_request",
        "status": "failed",
        "model": "m1",
        "capability": "chat",
        "phase": "prepare",
        "http_status": None,
        "message": "模型服务地址无效",
    }


async def test_chat_probe_does_not_pass_empty_terminal_stream(monkeypatch):
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        if body.get("stream") is True:
            return httpx2.Response(
                200,
                text="data: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx2.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "OK"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    result = await verify_provider_capabilities(
        provider_type="openai_compatible",
        base_url="https://api.x.com/v1",
        api_key="sk-abc",
        models=[{"id": "m1", "capabilities": ["chat"]}],
    )

    assert result["ok"] is False
    assert result["code"] == "capability_degraded"
    assert result["steps"][0]["status"] == "degraded"
    assert result["steps"][0]["http_status"] is None


async def test_tool_probe_runs_streaming_continuation_after_selection(monkeypatch):
    bodies: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if body.get("stream") is True:
            assert body["messages"][-1]["role"] == "tool"
            assert body["messages"][-1]["tool_call_id"] == "call_probe"
            return httpx2.Response(
                200,
                text=('data: {"choices":[{"delta":{"content":"OK"}}]}\n\ndata: [DONE]\n\n'),
                headers={"content-type": "text/event-stream"},
            )
        return httpx2.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_probe",
                                    "type": "function",
                                    "function": {
                                        "name": "bowhead_connection_probe",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    result = await verify_provider_capabilities(
        provider_type="openai_compatible",
        base_url="https://api.x.com/v1",
        api_key="sk-abc",
        models=[{"id": "m1", "capabilities": ["tool"]}],
    )

    assert result["ok"] is True
    assert result["steps"][0]["status"] == "passed"
    assert result["steps"][0]["phase"] == "tool_continuation"
    assert [body["stream"] for body in bodies] == [False, True]


async def test_vision_probe_uses_a_vendor_accepted_image_size(monkeypatch):
    seen: dict = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        data_url = body["messages"][0]["content"][1]["image_url"]["url"]
        mime, encoded = data_url.split(",", 1)
        image = base64.b64decode(encoded)
        seen["mime"] = mime
        seen["width"] = int.from_bytes(image[16:20], "big")
        seen["height"] = int.from_bytes(image[20:24], "big")
        return httpx2.Response(
            200,
            json={"choices": [{"message": {"content": "OK"}}]},
        )

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    result = await verify_provider_capabilities(
        provider_type="openai_compatible",
        base_url="https://api.x.com/v1",
        api_key="sk-abc",
        models=[{"id": "vision-1", "capabilities": ["vision"]}],
    )

    assert result["ok"] is False
    assert result["code"] == "capability_degraded"
    assert result["steps"][0]["status"] == "degraded"
    assert result["steps"][0]["code"] == "stream_degraded"
    assert seen == {
        "mime": "data:image/png;base64",
        "width": 16,
        "height": 16,
    }


async def test_rerank_probe_supports_openai_compat(monkeypatch):
    seen: dict = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            json={"results": [{"index": 0, "relevance_score": 0.99}]},
        )

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    result = await verify_provider_capabilities(
        provider_type="openai_compatible",
        base_url="https://api.x.com/v1",
        api_key="sk-abc",
        models=[
            {
                "id": "rerank-1",
                "capabilities": ["rerank"],
                "capability_protocol": "openai_compat",
            },
        ],
    )

    assert result["ok"] is True
    assert seen["path"].endswith("/rerank")
    assert seen["body"]["query"] == "Bowhead capability probe"
    assert seen["body"]["top_n"] == 1


async def test_rerank_probe_supports_dashscope_native(monkeypatch):
    seen: dict = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            json={
                "output": {
                    "results": [{"index": 0, "relevance_score": 0.99}],
                },
            },
        )

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    result = await verify_provider_capabilities(
        provider_type="openai_compatible",
        base_url="https://dashscope.aliyuncs.com/api/v1",
        api_key="sk-abc",
        models=[
            {
                "id": "gte-rerank-v2",
                "capabilities": ["rerank"],
                "capability_protocol": "dashscope_native",
            },
        ],
    )

    assert result["ok"] is True
    assert seen["path"].endswith(
        "/api/v1/services/rerank/text-rerank/text-rerank",
    )
    assert seen["body"]["input"]["documents"]
    assert seen["body"]["parameters"]["return_documents"] is False


async def test_image_generation_probe_supports_dashscope_async(monkeypatch):
    seen: dict = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.setdefault("paths", []).append(request.url.path)
        if request.method == "POST":
            seen["async"] = request.headers.get("x-dashscope-async")
            seen["body"] = json.loads(request.content)
            return httpx2.Response(200, json={"output": {"task_id": "task_1"}})
        return httpx2.Response(
            200,
            json={
                "output": {
                    "task_status": "SUCCEEDED",
                    "results": [{"url": "https://assets.example.com/probe.png"}],
                },
            },
        )

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    result = await verify_provider_capabilities(
        provider_type="openai_compatible",
        base_url="https://dashscope.aliyuncs.com/api/v1",
        api_key="sk-abc",
        models=[
            {
                "id": "qwen-image-plus",
                "capabilities": ["image_gen"],
                "capability_protocol": "dashscope_async",
            },
        ],
    )

    assert result["ok"] is True
    assert seen["paths"][0].endswith(
        "/api/v1/services/aigc/text2image/image-synthesis",
    )
    assert seen["paths"][1].endswith("/api/v1/tasks/task_1")
    assert seen["async"] == "enable"
    assert seen["body"]["parameters"]["size"] == "1328*1328"


async def test_image_generation_probe_rejects_failed_dashscope_task(monkeypatch):
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST":
            return httpx2.Response(200, json={"output": {"task_id": "task_failed"}})
        return httpx2.Response(
            200,
            json={"output": {"task_status": "FAILED", "code": "InvalidParameter"}},
        )

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    result = await verify_provider_capabilities(
        provider_type="openai_compatible",
        base_url="https://dashscope.aliyuncs.com/api/v1",
        api_key="sk-abc",
        models=[
            {
                "id": "qwen-image-plus",
                "capabilities": ["image_gen"],
                "capability_protocol": "dashscope_async",
            },
        ],
    )

    assert result["ok"] is False
    assert result["steps"][0]["code"] == "image_generation_failed"


async def test_image_generation_probe_supports_ark_sync(monkeypatch):
    seen: dict = {}
    encoded = base64.b64encode(b"\x89PNG\r\n\x1a\nprobe").decode()

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            json={"data": [{"b64_json": encoded}]},
        )

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    result = await verify_provider_capabilities(
        provider_type="openai_compatible",
        base_url="https://ark.example.com/api/v3",
        api_key="sk-abc",
        models=[
            {
                "id": "seedream-1",
                "capabilities": ["image_gen"],
                "capability_protocol": "ark_sync",
            },
        ],
    )

    assert result["ok"] is True
    assert seen["path"].endswith("/api/v3/images/generations")
    assert seen["body"]["response_format"] == "b64_json"


async def test_image_generation_probe_rejects_invalid_ark_image(monkeypatch):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"data": [{"b64_json": "not-base64"}]})

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    result = await verify_provider_capabilities(
        provider_type="openai_compatible",
        base_url="https://ark.example.com/api/v3",
        api_key="sk-abc",
        models=[
            {
                "id": "seedream-1",
                "capabilities": ["image_gen"],
                "capability_protocol": "ark_sync",
            },
        ],
    )

    assert result["ok"] is False
    assert result["steps"][0]["code"] == "image_output_missing"


async def test_web_search_probe_supports_dashscope_compat(monkeypatch):
    seen: dict = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "OpenAI: https://www.openai.com/",
                        },
                    },
                ],
            },
        )

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    result = await verify_provider_capabilities(
        provider_type="openai_compatible",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="sk-abc",
        models=[
            {
                "id": "qwen-plus",
                "capabilities": ["web_search"],
                "capability_protocol": "dashscope_compat",
            },
        ],
    )

    assert result["ok"] is True
    assert seen["path"].endswith("/compatible-mode/v1/chat/completions")
    assert seen["body"]["enable_search"] is True
    assert seen["body"]["search_options"]["return_citations"] is True


async def test_web_search_probe_uses_search_specific_timeout(monkeypatch):
    seen: dict = {}

    async def fake_probe_web_search(**kwargs):
        seen.update(kwargs)
        return vmod.VerifyResult(True, "ok", http_status=200)

    monkeypatch.setattr(vmod, "_probe_web_search", fake_probe_web_search)
    result = await vmod._probe_special_capability(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="sk-abc",
        model="qwen-plus",
        capability="web_search",
        capability_protocol="dashscope_compat",
        timeout_sec=10,
    )

    assert result.ok is True
    assert seen["timeout_sec"] == 45


async def test_web_search_probe_supports_dashscope_native(monkeypatch):
    seen: dict = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            json={
                "output": {
                    "text": "OpenAI: https://www.openai.com/",
                    "search_info": {
                        "search_results": [
                            {
                                "title": "OpenAI",
                                "url": "https://www.openai.com/",
                            },
                        ],
                    },
                },
            },
        )

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    result = await verify_provider_capabilities(
        provider_type="openai_compatible",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="sk-abc",
        models=[
            {
                "id": "qwen-plus",
                "capabilities": ["web_search"],
                "capability_protocol": "dashscope_native",
            },
        ],
    )

    assert result["ok"] is True
    assert seen["url"].endswith(
        "/api/v1/services/aigc/text-generation/generation",
    )
    assert seen["body"]["parameters"]["search_options"]["enable_source"] is True


async def test_web_search_probe_rejects_plain_chat_output(monkeypatch):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "I cannot browse the web.",
                        },
                    },
                ],
            },
        )

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    result = await verify_provider_capabilities(
        provider_type="openai_compatible",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="sk-abc",
        models=[
            {
                "id": "qwen-plus",
                "capabilities": ["web_search"],
                "capability_protocol": "dashscope_compat",
            },
        ],
    )

    assert result["ok"] is False
    assert result["steps"][0]["code"] == "web_search_output_missing"


async def test_unknown_capability_is_explicitly_unsupported(monkeypatch):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"data": [{"id": "m1"}]})

    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(handler))
    result = await verify_provider_capabilities(
        provider_type="openai_compatible",
        base_url="https://api.x.com/v1",
        api_key="sk-abc",
        models=[{"id": "m1", "capabilities": ["audio"]}],
    )

    assert result["ok"] is False
    assert result["steps"][-1]["code"] == "probe_not_supported"


# ─── SSRF guard is really engaged (no monkeypatch) ─────────────────


async def test_private_ip_is_not_blocked(monkeypatch):
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(lambda request: httpx2.Response(200, json={"data": []})))
    r = await verify_provider(provider_type="openai_compatible", base_url="https://10.0.0.1/v1", api_key="sk-abc")
    assert r.code != "blocked"


async def test_http_is_not_blocked(monkeypatch):
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    monkeypatch.setattr(vmod, "build_guarded_async_client", _mock_factory(lambda request: httpx2.Response(200, json={"data": []})))
    r = await verify_provider(provider_type="openai_compatible", base_url="http://example.com/v1", api_key="sk-abc")
    assert r.code != "blocked"
