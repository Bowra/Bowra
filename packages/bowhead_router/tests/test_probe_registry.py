from __future__ import annotations

import base64
import json

import bowhead_router.probe as probe_module
import httpx2
import pytest
from bowhead_router.transport import (
    build_provider_transport_plan,
    materialize_transport_headers,
    public_transport_preview,
    render_custom_http_body,
)

from bowhead_router import (
    CompatibleProbeRegistry,
    NotTestable,
    ProbeRequestPlan,
)


def _provider(
    *,
    provider_type: str = "openai_compatible",
) -> dict:
    return {
        "type": provider_type,
        "base_url": "https://api.example.com/v1",
        "api_key": "secret",
        "timeout_sec": 60,
        "extra": {},
    }


def _mock_factory(handler):
    def factory(**kwargs):
        return httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler),
            **kwargs,
        )

    return factory


@pytest.mark.unit
def test_transport_normalizes_overrides_and_hides_sensitive_headers() -> None:
    provider = _provider()
    provider["base_url"] = "https://api.example.com/v1?api-version=2026-07-30&sig=query-secret"
    provider["default_headers"] = {
        "authorization": "Bearer tenant-override",
        "Cookie": "session=private",
        "X-Tenant-ID": "tenant-cn",
    }
    plan = build_provider_transport_plan(
        provider,
        purpose="runtime_chat",
    )

    headers = materialize_transport_headers(plan)
    preview = public_transport_preview(plan)
    serialized_preview = json.dumps(preview, sort_keys=True)

    assert headers["authorization"] == "Bearer tenant-override"
    assert headers["cookie"] == "session=private"
    assert headers["x-tenant-id"] == "tenant-cn"
    assert len([header for header in plan.safe_header_semantics if header[0] == "authorization"]) == 1
    assert "tenant-override" not in serialized_preview
    assert "session=private" not in serialized_preview
    assert "query-secret" not in serialized_preview
    assert ["api-version", "2026-07-30"] in preview["non_secret_query"]
    assert "tenant-cn" in serialized_preview


@pytest.mark.unit
def test_plan_is_single_request_and_rerank_is_zero_request() -> None:
    registry = CompatibleProbeRegistry()
    chat = registry.plan(
        _provider(),
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="Main",
        model_id="gpt-4.1",
        capability="chat",
    )
    rerank = registry.plan(
        _provider(),
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="Main",
        model_id="rerank-v1",
        capability="rerank",
    )

    assert isinstance(chat, ProbeRequestPlan)
    assert chat.provider_request_count == 1
    assert isinstance(rerank, NotTestable)
    assert rerank.provider_request_count == 0
    assert rerank.probe_contract_version is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_compatible_chat_probe_only_sends_hello(monkeypatch) -> None:
    registry = CompatibleProbeRegistry()
    plan = registry.plan(
        _provider(),
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="acme-genai",
        model_id="any-model",
        capability="chat",
    )

    assert isinstance(plan, ProbeRequestPlan)
    # chat-v2 streams like runtime calls; it still sends only hello, without model-level parameters.
    assert plan.body_shape == {
        "model": "any-model",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert json.loads(request.content) == plan.body_shape
        return httpx2.Response(
            200,
            json={"model": "any-model", "choices": [{"message": {"content": "hello"}}]},
        )

    monkeypatch.setattr(
        probe_module,
        "build_guarded_async_client",
        _mock_factory(handler),
    )
    result = await registry.execute(plan, timeout=10)

    assert result.terminal_state == "verified"
    assert result.code == "ok"


@pytest.mark.unit
def test_vision_plan_uses_vendor_accepted_png_size_and_new_contract() -> None:
    plan = CompatibleProbeRegistry().plan(
        _provider(),
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="Main",
        model_id="vision-1",
        capability="vision",
    )

    assert isinstance(plan, ProbeRequestPlan)
    assert plan.probe_contract_version == "vision-v3"
    data_url = plan.body_shape["messages"][0]["content"][1]["image_url"]["url"]
    mime, encoded = data_url.split(",", 1)
    image = base64.b64decode(encoded)
    assert mime == "data:image/png;base64"
    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert int.from_bytes(image[16:20], "big") == 16
    assert int.from_bytes(image[20:24], "big") == 16


@pytest.mark.unit
def test_anthropic_plan_matches_runtime_budget_and_thinking_contract() -> None:
    provider = _provider(provider_type="anthropic_compatible")
    registry = CompatibleProbeRegistry()
    chat = registry.plan(
        provider,
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="DeepSeek",
        model_id="deepseek-v4-pro",
        capability="chat",
    )
    tool = registry.plan(
        provider,
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="DeepSeek",
        model_id="deepseek-v4-pro",
        capability="tool",
    )

    assert isinstance(chat, ProbeRequestPlan)
    assert chat.probe_contract_version == "anthropic-chat-v2"
    assert chat.body_shape["max_tokens"] == 8
    assert chat.body_shape["thinking"] == {"type": "disabled"}
    assert isinstance(tool, ProbeRequestPlan)
    assert tool.probe_contract_version == "anthropic-tool-call-v2"
    assert tool.body_shape["max_tokens"] == 128
    assert tool.body_shape["tool_choice"] == {"type": "auto"}
    assert tool.body_shape["thinking"] == {"type": "disabled"}


@pytest.mark.unit
def test_anthropic_transport_honors_v2_top_level_endpoint_settings() -> None:
    provider = _provider(provider_type="anthropic_compatible")
    provider["base_url"] = "https://api.example.com/anthropic"
    provider["messages_path"] = "/messages"
    provider["anthropic_version"] = "2026-07-31"

    plan = build_provider_transport_plan(
        provider,
        purpose="probe_chat",
        model_id="deepseek-v4-pro",
        capability="chat",
    )
    runtime_provider = {key: value for key, value in provider.items() if key not in {"messages_path", "anthropic_version"}}
    runtime_provider["extra"] = {
        "messages_path": "/messages",
        "anthropic_version": "2026-07-31",
    }
    runtime_plan = build_provider_transport_plan(
        runtime_provider,
        purpose="probe_chat",
        model_id="deepseek-v4-pro",
        capability="chat",
    )

    assert plan.request_url == "https://api.example.com/anthropic/messages"
    assert materialize_transport_headers(plan)["anthropic-version"] == "2026-07-31"
    assert plan == runtime_plan


@pytest.mark.unit
def test_azure_transport_uses_root_endpoint_deployment_and_api_version() -> None:
    provider = {
        "type": "azure_openai",
        "endpoint": "https://api.example.com/",
        "api_version": "2024-10-21",
        "api_key": "secret",
        "auth": {"type": "bearer"},
        "models": [
            {
                "id": "gpt-54",
                "deployment": "gpt-54 prod",
                "capabilities": ["chat", "tool"],
            }
        ],
    }

    plan = build_provider_transport_plan(
        provider,
        purpose="runtime_chat",
        model_id="gpt-54",
        capability="chat",
    )

    assert plan.request_url == ("https://api.example.com/openai/deployments/gpt-54%20prod/chat/completions?api-version=2024-10-21")
    assert materialize_transport_headers(plan)["authorization"] == "Bearer secret"


@pytest.mark.unit
def test_custom_http_renders_only_whole_value_placeholders() -> None:
    provider = {
        "type": "custom_http",
        "base_url": "https://gateway.example.com/v2",
        "api_key": "secret",
        "auth": {
            "type": "secret_header",
            "header_name": "X-API-Key",
        },
        "transports": {
            "chat": {
                "contract": "openai_chat_completions_sse",
                "request": {
                    "method": "POST",
                    "url_template": "{base_url}/deployments/{deployment}/chat",
                    "query": {"version": "1"},
                    "body_template": {
                        "deployment_name": {"$bowhead": "deployment"},
                        "messages": {"$bowhead": "messages"},
                        "tools": {
                            "$bowhead": "tools",
                            "omit_if_empty": True,
                        },
                        "stream": {"$bowhead": "stream"},
                    },
                },
            }
        },
        "models": [
            {
                "id": "company-gpt",
                "deployment": "prod/chat",
                "capabilities": ["chat"],
                "capability_transports": {"chat": "chat"},
            }
        ],
    }
    plan = build_provider_transport_plan(
        provider,
        purpose="runtime_chat",
        model_id="company-gpt",
        capability="chat",
    )
    body = render_custom_http_body(
        provider,
        model_id="company-gpt",
        capability="chat",
        variables={
            "deployment": "prod/chat",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [],
            "stream": True,
        },
    )

    assert plan.request_url == ("https://gateway.example.com/v2/deployments/prod%2Fchat/chat?version=1")
    assert materialize_transport_headers(plan)["x-api-key"] == "secret"
    assert body == {
        "deployment_name": "prod/chat",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
    }


@pytest.mark.unit
def test_custom_http_rejects_proxy_headers_and_executable_templates() -> None:
    provider = {
        "type": "custom_http",
        "base_url": "https://gateway.example.com",
        "auth": {"type": "none"},
        "transports": {
            "chat": {
                "contract": "openai_chat_completions",
                "request": {
                    "url_template": "{base_url}/chat",
                    "headers": {"X-Forwarded-Host": "metadata.internal"},
                    "body_template": {
                        "messages": {"$bowhead": "messages"},
                        "model": "${env.SECRET}",
                    },
                },
            }
        },
        "models": [
            {
                "id": "m1",
                "capabilities": ["chat"],
                "capability_transports": {"chat": "chat"},
            }
        ],
    }
    with pytest.raises(ValueError, match="forbidden"):
        build_provider_transport_plan(
            provider,
            purpose="runtime_chat",
            model_id="m1",
            capability="chat",
        )
    provider["transports"]["chat"]["request"]["headers"] = {}
    with pytest.raises(ValueError, match="interpolation"):
        render_custom_http_body(
            provider,
            model_id="m1",
            capability="chat",
            variables={},
        )


@pytest.mark.unit
def test_custom_http_model_overrides_cannot_replace_runtime_budget_or_template() -> None:
    provider = {
        "type": "custom_http",
        "base_url": "https://gateway.example.com",
        "auth": {"type": "none"},
        "transports": {
            "chat": {
                "contract": "openai_chat_completions",
                "request": {
                    "url_template": "{base_url}/chat",
                    "body_template": {
                        "messages": {"$bowhead": "messages"},
                        "max_tokens": {"$bowhead": "max_output_tokens"},
                        "temperature": 0,
                        "vendor": {},
                    },
                },
            }
        },
        "models": [
            {
                "id": "m1",
                "capabilities": ["chat"],
                "capability_transports": {"chat": "chat"},
                "request_body_overrides": {
                    "chat": {"max_tokens": 999999},
                },
            }
        ],
    }

    with pytest.raises(ValueError, match="runtime fields"):
        render_custom_http_body(
            provider,
            model_id="m1",
            capability="chat",
            variables={
                "messages": [{"role": "user", "content": "hello"}],
                "max_output_tokens": 32,
            },
        )

    provider["models"][0]["request_body_overrides"] = {
        "chat": {"vendor": {"nested": {"$bowhead": "model"}}},
    }
    with pytest.raises(ValueError, match=r"cannot add \$bowhead"):
        render_custom_http_body(
            provider,
            model_id="m1",
            capability="chat",
            variables={
                "messages": [{"role": "user", "content": "hello"}],
                "max_output_tokens": 32,
            },
        )

    provider["models"][0]["request_body_overrides"] = {
        "chat": {"temperature": 0.2},
    }
    body = render_custom_http_body(
        provider,
        model_id="m1",
        capability="chat",
        variables={
            "messages": [{"role": "user", "content": "hello"}],
            "max_output_tokens": 32,
        },
    )
    assert body["temperature"] == 0.2


@pytest.mark.unit
def test_custom_http_signature_ignores_declaration_metadata() -> None:
    transport = {
        "chat": {
            "contract": "openai_chat_completions_sse",
            "request": {
                "url_template": "https://api.example.com/chat",
                "body_template": {
                    "stream": {"$bowhead": "stream"},
                    "messages": {"$bowhead": "messages"},
                },
            },
        },
    }
    base_provider = {
        "type": "custom_http",
        "api_key": "secret",
        "auth": {"type": "bearer"},
        "transports": transport,
    }
    verified_draft = {
        **base_provider,
        "models": [
            {
                "id": "gpt-51",
                "capabilities": ["chat", "tool"],
                "declaration_origin": "manual",
                "capability_transports": {
                    "chat": "chat",
                    "tool": "chat",
                },
            },
        ],
    }
    saved_provider = {
        **base_provider,
        "models": [
            {
                "id": "gpt-51",
                "capabilities": ["chat", "tool"],
                "source": "manual",
                "capability_transports": {
                    "chat": "chat",
                    "tool": "chat",
                },
            },
        ],
    }
    registry = CompatibleProbeRegistry()

    verified_plan = registry.plan(
        verified_draft,
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="UPTIMIZE",
        model_id="gpt-51",
        capability="chat",
    )
    saved_plan = registry.plan(
        saved_provider,
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="UPTIMIZE",
        model_id="gpt-51",
        capability="chat",
    )

    assert verified_plan.transport.request_options == (saved_plan.transport.request_options)

    changed_provider = json.loads(json.dumps(saved_provider))
    changed_provider["transports"]["chat"]["request"]["body_template"]["temperature"] = 0.7
    changed_plan = registry.plan(
        changed_provider,
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="UPTIMIZE",
        model_id="gpt-51",
        capability="chat",
    )
    assert changed_plan.transport.request_options != (saved_plan.transport.request_options)


@pytest.mark.unit
def test_azure_chat_probe_omits_model_level_parameters() -> None:
    provider = {
        "type": "azure_openai",
        "endpoint": "https://api.example.com",
        "api_version": "2024-10-21",
        "api_key": "secret",
        "models": [{"id": "gpt-51", "capabilities": ["chat"]}],
    }
    plan = CompatibleProbeRegistry().plan(
        provider,
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="UPTIMIZE",
        model_id="gpt-51",
        capability="chat",
    )
    assert isinstance(plan, ProbeRequestPlan)
    assert "max_completion_tokens" not in plan.body_shape
    assert "max_tokens" not in plan.body_shape
    assert plan.probe_contract_version == "azure-gpt5-chat-v3"
    # chat-v2 streams like runtime calls; previously omitting stream overstated what "verified" guaranteed.
    assert plan.body_shape["stream"] is True


@pytest.mark.unit
def test_azure_gpt5_vision_probe_has_reasoning_output_budget() -> None:
    provider = {
        "type": "azure_openai",
        "endpoint": "https://api.example.com",
        "api_version": "2025-11-13",
        "api_key": "secret",
        "models": [{"id": "gpt-51", "capabilities": ["vision"]}],
    }
    plan = CompatibleProbeRegistry().plan(
        provider,
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="UPTIMIZE",
        model_id="gpt-51",
        capability="vision",
    )

    assert isinstance(plan, ProbeRequestPlan)
    assert plan.body_shape["max_completion_tokens"] == 128
    assert plan.probe_contract_version == "azure-gpt5-vision-v2"


@pytest.mark.unit
def test_azure_non_gpt5_chat_probe_omits_model_level_parameters() -> None:
    provider = {
        "type": "azure_openai",
        "endpoint": "https://api.example.com",
        "api_version": "2024-10-21",
        "api_key": "secret",
        "models": [{"id": "gpt-4o", "capabilities": ["chat"]}],
    }
    plan = CompatibleProbeRegistry().plan(
        provider,
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="Azure",
        model_id="gpt-4o",
        capability="chat",
    )

    assert isinstance(plan, ProbeRequestPlan)
    assert "max_tokens" not in plan.body_shape
    assert "max_completion_tokens" not in plan.body_shape
    assert plan.probe_contract_version == "chat-v2"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("model_id", "expected_budget", "expected_contract"),
    [
        ("gpt-51", 128, "custom-http-gpt5-chat-v3"),
        ("gpt-4o", 1, "chat-v2"),
    ],
)
def test_custom_http_probe_renders_model_appropriate_output_budget(
    model_id: str,
    expected_budget: int,
    expected_contract: str,
) -> None:
    provider = {
        "type": "custom_http",
        "base_url": "https://api.example.com",
        "api_key": "secret",
        "auth": {"type": "bearer"},
        "transports": {
            "chat": {
                "contract": "openai_chat_completions_sse",
                "request": {
                    "method": "POST",
                    "url_template": "{base_url}/deployments/{deployment}/chat",
                    "body_template": {
                        "model": {"$bowhead": "model"},
                        "messages": {"$bowhead": "messages"},
                        "stream": {"$bowhead": "stream"},
                        "max_completion_tokens": {
                            "$bowhead": "max_output_tokens",
                        },
                    },
                },
            },
        },
        "models": [
            {
                "id": model_id,
                "capabilities": ["chat"],
                "capability_transports": {"chat": "chat"},
            },
        ],
    }

    plan = CompatibleProbeRegistry().plan(
        provider,
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="Custom",
        model_id=model_id,
        capability="chat",
    )

    assert isinstance(plan, ProbeRequestPlan)
    assert plan.body_shape["max_completion_tokens"] == expected_budget
    # chat-v2 streams like runtime calls.
    assert plan.body_shape["stream"] is True
    assert plan.probe_contract_version == expected_contract


@pytest.mark.unit
@pytest.mark.asyncio
async def test_azure_chat_probe_accepts_a_non_streaming_response(
    monkeypatch,
) -> None:
    provider = {
        "type": "azure_openai",
        "endpoint": "https://api.example.com",
        "api_version": "2024-10-21",
        "api_key": "secret",
        "models": [{"id": "gpt-51", "capabilities": ["chat"]}],
    }
    registry = CompatibleProbeRegistry()
    plan = registry.plan(
        provider,
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="UPTIMIZE",
        model_id="gpt-51",
        capability="chat",
    )
    assert isinstance(plan, ProbeRequestPlan)

    def handler(request: httpx2.Request) -> httpx2.Response:
        # Send a streaming chat-v2 request, but still accept plain JSON responses from the provider, as originally intended.
        assert json.loads(request.content) == {
            "model": "gpt-51",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        }
        return httpx2.Response(
            200,
            json={"model": "gpt-51", "choices": [{"message": {"content": "OK"}}]},
        )

    monkeypatch.setattr(
        probe_module,
        "build_guarded_async_client",
        _mock_factory(handler),
    )
    result = await registry.execute(plan, timeout=10)

    assert result.terminal_state == "verified"
    assert result.code == "ok"
    assert result.actual_model_id == "gpt-51"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_requires_real_tool_call(monkeypatch) -> None:
    registry = CompatibleProbeRegistry()
    plan = registry.plan(
        _provider(),
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="Main",
        model_id="gpt-4.1",
        capability="tool",
    )
    assert isinstance(plan, ProbeRequestPlan)
    requests: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "model": "gpt-4.1",
                "choices": [{"message": {"content": "I can use tools, but did not call one."}}],
            },
        )

    monkeypatch.setattr(
        probe_module,
        "build_guarded_async_client",
        _mock_factory(handler),
    )
    result = await registry.execute(plan, timeout=10)

    assert len(requests) == 1
    assert requests[0]["tool_choice"] == "auto"
    assert result.terminal_state == "failed"
    assert result.code == "tool_call_missing"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transient_error_does_not_create_factual_outcome(
    monkeypatch,
) -> None:
    registry = CompatibleProbeRegistry()
    plan = registry.plan(
        _provider(),
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="Main",
        model_id="gpt-4.1",
        capability="chat",
    )
    assert isinstance(plan, ProbeRequestPlan)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429)

    monkeypatch.setattr(
        probe_module,
        "build_guarded_async_client",
        _mock_factory(handler),
    )
    result = await registry.execute(plan, timeout=10)

    assert result.classification == "attempt_error"
    assert result.outcome is None
    assert result.terminal_state == "errored"
    assert result.code == "rate_limited"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_message", "expected_code"),
    [
        (
            "The API version 2024-10-21 is not supported",
            "api_version_invalid",
        ),
        (
            "Unsupported parameter: max_tokens",
            "parameter_incompatible",
        ),
    ],
)
async def test_azure_probe_classifies_actionable_bad_requests(
    monkeypatch,
    provider_message: str,
    expected_code: str,
) -> None:
    provider = {
        "type": "azure_openai",
        "endpoint": "https://api.example.com",
        "api_version": "2024-10-21",
        "api_key": "secret",
        "models": [{"id": "gpt-51", "capabilities": ["chat"]}],
    }
    registry = CompatibleProbeRegistry()
    plan = registry.plan(
        provider,
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="UPTIMIZE",
        model_id="gpt-51",
        capability="chat",
    )
    assert isinstance(plan, ProbeRequestPlan)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            400,
            json={"error": {"message": provider_message}},
        )

    monkeypatch.setattr(
        probe_module,
        "build_guarded_async_client",
        _mock_factory(handler),
    )
    result = await registry.execute(plan, timeout=10)

    assert result.terminal_state == "failed"
    assert result.code == expected_code
    assert result.safe_http_status == 400


@pytest.mark.unit
@pytest.mark.asyncio
async def test_embedding_rejects_non_finite_values(monkeypatch) -> None:
    registry = CompatibleProbeRegistry()
    plan = registry.plan(
        _provider(),
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="Main",
        model_id="embed-v1",
        capability="embed",
    )
    assert isinstance(plan, ProbeRequestPlan)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            content=b'{"data":[{"embedding":[0.1,NaN]}]}',
            headers={"content-type": "application/json"},
        )

    monkeypatch.setattr(
        probe_module,
        "build_guarded_async_client",
        _mock_factory(handler),
    )
    result = await registry.execute(plan, timeout=10)

    assert result.terminal_state == "failed"
    assert result.code == "embedding_non_finite"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_actual_model_mismatch_is_recorded_not_failed(monkeypatch) -> None:
    """名字不等只记事实，不判失败（2026-08-25 撤掉 `model_identity_mismatch` 短路）。

    厂商改名是常态不是异常：116 的 Doubao 回带版本号 id（2026-08-14 两条 HTTP 200
    被误判失败）、DeepSeek 发 `deepseek-chat` 回 `deepseek-v4-flash`、UPTIMIZE canonical
    名三层改名。观察值必须原样活到 `actual_model_id`（overview 靠它注记
    「服务端返回：X」），能力判定只看响应内容。"""
    registry = CompatibleProbeRegistry()
    plan = registry.plan(
        _provider(),
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="Main",
        model_id="gpt-4.1",
        capability="chat",
    )
    assert isinstance(plan, ProbeRequestPlan)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "model": "proxy-default",
                "choices": [{"message": {"content": "ok"}}],
            },
        )

    monkeypatch.setattr(
        probe_module,
        "build_guarded_async_client",
        _mock_factory(handler),
    )
    result = await registry.execute(plan, timeout=10)

    assert result.terminal_state == "verified"
    assert result.code == "ok"
    assert result.actual_model_id == "proxy-default"


@pytest.mark.unit
def test_rejected_parameter_is_classified_as_parameter_incompatible() -> None:
    """A structured parameter rejection must direct the operator to model parameters."""
    rejection = (
        b'{"error":{"message":"litellm.BadRequestError: AzureException BadRequestError - '
        b"Unsupported value: 'temperature' does not support 0.7 with this model. "
        b'Only the default (1) value is supported.","type":"invalid_request_error",'
        b'"param":"temperature","code":"unsupported_value"}}'
    )

    assert probe_module._invalid_request_code("openai_compatible", rejection) == "parameter_incompatible"


@pytest.mark.unit
def test_unrelated_bad_requests_still_read_as_invalid_request() -> None:
    """负对照：没指名参数的请求格式错误仍是 `invalid_request`，不得一律升级。"""
    assert probe_module._invalid_request_code("openai_compatible", b'{"error":{"message":"messages must not be empty"}}') == "invalid_request"
