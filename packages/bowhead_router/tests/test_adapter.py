"""MockAdapter + Router 基础测试。"""

from __future__ import annotations

import pytest

from bowhead_router import (
    AgentBinding,
    LLMAdapter,
    LLMRequest,
    Message,
    MockAdapter,
    ProviderConfig,
    ProviderRegistry,
    Router,
)


@pytest.mark.unit
async def test_mock_adapter_echo() -> None:
    adapter = MockAdapter()
    req = LLMRequest(
        model="test",
        messages=[Message(role="user", content="hello")],
    )
    resp = await adapter.invoke(req)

    assert "hello" in resp.content
    assert resp.provider == "mock"
    assert resp.usage.input_tokens > 0
    assert resp.usage.output_tokens > 0


@pytest.mark.unit
async def test_mock_adapter_preset_response() -> None:
    adapter = MockAdapter(preset_response="canned answer")
    req = LLMRequest(model="m", messages=[Message(role="user", content="any")])
    resp = await adapter.invoke(req)

    assert resp.content == "canned answer"


@pytest.mark.unit
async def test_mock_adapter_stream() -> None:
    adapter = MockAdapter(preset_response="one two three")
    req = LLMRequest(model="m", messages=[Message(role="user", content="x")])

    chunks = []
    async for chunk in adapter.stream(req):
        chunks.append(chunk)

    text = "".join(c.delta for c in chunks)
    assert "one" in text and "two" in text and "three" in text
    assert chunks[-1].finish_reason == "stop"


@pytest.mark.unit
async def test_adapter_protocol_satisfies() -> None:
    """MockAdapter 必须实现 LLMAdapter Protocol。"""
    adapter = MockAdapter()
    assert isinstance(adapter, LLMAdapter)


@pytest.mark.unit
async def test_router_invoke_primary() -> None:
    registry = ProviderRegistry()
    registry.load([ProviderConfig(name="p1", type="mock")])

    router = Router(registry)
    router.bind(
        AgentBinding(
            agent_type="lead",
            primary=("p1", "m1"),
        )
    )

    req = LLMRequest(model="", messages=[Message(role="user", content="hi")])
    resp = await router.invoke("lead", req)

    assert resp.provider == "p1"
    assert resp.model == "m1"


@pytest.mark.unit
async def test_router_fallback_when_primary_missing() -> None:
    """Primary 不在 registry 时自动走 fallback。"""
    registry = ProviderRegistry()
    registry.load([ProviderConfig(name="p2", type="mock")])  # Only p2 is registered.

    router = Router(registry)
    router.bind(
        AgentBinding(
            agent_type="analyst",
            primary=("p1", "m1"),  # Not registered.
            fallback=[("p2", "m2")],
        )
    )

    req = LLMRequest(model="", messages=[Message(role="user", content="hi")])
    resp = await router.invoke("analyst", req)

    assert resp.provider == "p2"
    assert resp.model == "m2"


@pytest.mark.unit
async def test_router_profile_override() -> None:
    """Analyst 的 profile 可覆盖默认 binding（TD §4.3.5）。"""
    registry = ProviderRegistry()
    registry.load(
        [
            ProviderConfig(name="cheap", type="mock"),
            ProviderConfig(name="smart", type="mock"),
        ]
    )

    router = Router(registry)
    router.bind(AgentBinding(agent_type="analyst", primary=("cheap", "m1")))
    router.bind(AgentBinding(agent_type="analyst", profile="compute", primary=("smart", "m2")))

    req = LLMRequest(model="", messages=[Message(role="user", content="x")])

    resp_default = await router.invoke("analyst", req)
    resp_compute = await router.invoke("analyst", req, profile="compute")

    assert resp_default.provider == "cheap"
    assert resp_compute.provider == "smart"
