"""Test bounded retries for provider-reported outbound parameter constraints.

The mock 400 response uses a standard OpenAI-compatible error envelope with
an unsupported temperature parameter. No real provider request is made."""

from __future__ import annotations

from typing import Any

import httpx2
import pytest
from bowhead_router.adapters.anthropic_compat import AnthropicCompatibleAdapter
from bowhead_router.adapters.openai_compat import OpenAICompatibleAdapter
from bowhead_router.outbound import learned_constraints, reset_learned_for_test

from bowhead_router import LLMRequest, Message

AZURE_GATEWAY_400 = {
    "error": {
        "message": (
            "litellm.BadRequestError: AzureException BadRequestError - Unsupported value: 'temperature' does not support 0.7 with this model. Only the default (1) value is supported."
        ),
        "type": "invalid_request_error",
        "param": "temperature",
        "code": "unsupported_value",
    }
}
OPENAI_OK = {
    "id": "1",
    "model": "openai_gpt56_luna",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
}
ANTHROPIC_OK = {
    "id": "1",
    "model": "claude-x",
    "content": [{"type": "text", "text": "OK"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    reset_learned_for_test()
    yield
    reset_learned_for_test()


class _Recorder:
    """记下每一次出站 body —— 断言「第几次发了什么」，而不是只看最终返回。"""

    def __init__(self, responses: list[httpx2.Response]) -> None:
        self.responses = responses
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        import json

        self.bodies.append(json.loads(request.content))
        return self.responses[min(len(self.bodies) - 1, len(self.responses) - 1)]

    def build(self, **kwargs: Any) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self), **kwargs)


def _openai(recorder: _Recorder, **options: Any) -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(
        base_url="https://api.marketplace.example.com/v1",
        api_key="sk-test",
        provider_name="acme-genai",
        ssrf_guard=True,
        client_builder=recorder.build,
        provider_options=options,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_first_call_succeeds_even_though_the_gateway_rejects_temperature() -> None:
    """★ 终验：调用方**看到成功**，而不是「失败一次再让人去改配置」。"""
    recorder = _Recorder(
        [
            httpx2.Response(400, json=AZURE_GATEWAY_400),
            httpx2.Response(200, json=OPENAI_OK),
        ]
    )
    adapter = _openai(recorder, models=[{"id": "openai_gpt56_luna", "capabilities": ["chat"]}])
    try:
        response = await adapter.invoke(
            LLMRequest(model="openai_gpt56_luna", messages=[Message(role="user", content="hi")]),
        )
    finally:
        await adapter.close()

    assert response.content == "OK"
    assert len(recorder.bodies) == 2, "只重试一次"
    assert recorder.bodies[0]["temperature"] == 0.7, "第一次带着没人选过的 dataclass 默认值"
    assert "temperature" not in recorder.bodies[1], "第二次把被指认的参数丢掉"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_constraint_is_remembered_so_the_next_call_costs_nothing() -> None:
    """学到之后不再白跑一次：第二个请求首发就不带 temperature。"""
    recorder = _Recorder([httpx2.Response(400, json=AZURE_GATEWAY_400), httpx2.Response(200, json=OPENAI_OK)])
    adapter = _openai(recorder, models=[{"id": "openai_gpt56_luna", "capabilities": ["chat"]}])
    req = LLMRequest(model="openai_gpt56_luna", messages=[Message(role="user", content="hi")])
    try:
        await adapter.invoke(req)
        await adapter.invoke(req)
    finally:
        await adapter.close()

    assert len(recorder.bodies) == 3, "首轮 2 次（含重试），次轮 1 次"
    assert "temperature" not in recorder.bodies[2]
    assert learned_constraints(
        adapter_type="openai_compatible", base_url="https://api.marketplace.example.com/v1", model="openai_gpt56_luna"
    ) == frozenset({"temperature"})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_explicit_declaration_is_never_overridden_by_the_machine() -> None:
    """★ 人的声明 > 机器的观测：管理员已声明 fixed 时不得自愈掉它（否则配置说了不算）。"""
    recorder = _Recorder([httpx2.Response(400, json=AZURE_GATEWAY_400)])
    adapter = _openai(
        recorder,
        models=[
            {
                "id": "openai_gpt56_luna",
                "capabilities": ["chat"],
                "outbound_params": {"temperature": {"mode": "fixed", "value": 1}},
            }
        ],
    )
    try:
        with pytest.raises(Exception):  # noqa: B017,PT011 — The criterion is that no second request is sent.
            await adapter.invoke(
                LLMRequest(model="openai_gpt56_luna", messages=[Message(role="user", content="hi")]),
            )
    finally:
        await adapter.close()

    assert len(recorder.bodies) == 1, "已声明的参数不得触发自愈重试"
    assert recorder.bodies[0]["temperature"] == 1, "发的是管理员声明的值"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_rejection_naming_an_untouchable_field_is_not_retried() -> None:
    """负对照：provider 点名 `messages` 时绝不照做 —— 那是语义不是形状。"""
    recorder = _Recorder(
        [
            httpx2.Response(
                400,
                json={"error": {"message": "invalid messages", "param": "messages", "type": "invalid_request_error"}},
            )
        ]
    )
    adapter = _openai(recorder, models=[{"id": "m", "capabilities": ["chat"]}])
    try:
        with pytest.raises(Exception):  # noqa: B017,PT011
            await adapter.invoke(LLMRequest(model="m", messages=[Message(role="user", content="hi")]))
    finally:
        await adapter.close()

    assert len(recorder.bodies) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_self_heal_works_without_anyone_setting_an_env_var(monkeypatch) -> None:
    """★ 默认开启：客户不需要知道有这么个开关。

    这条能力是为「陌生网关的第一句话」造的 —— 要求管理员先去开一个 env，
    等于把它锁在那个他不可能知道的前提后面。这条钉住「不配置也生效」。
    """
    recorder = _Recorder([httpx2.Response(400, json=AZURE_GATEWAY_400), httpx2.Response(200, json=OPENAI_OK)])
    adapter = _openai(recorder, models=[{"id": "m", "capabilities": ["chat"]}])
    try:
        response = await adapter.invoke(LLMRequest(model="m", messages=[Message(role="user", content="hi")]))
    finally:
        await adapter.close()

    assert response.content == "OK"
    assert len(recorder.bodies) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anthropic_face_self_heals_too() -> None:
    """anthropic 面此前**一条自愈路径都没有** —— 网关拒一个参数就整条链路不可用。"""
    recorder = _Recorder(
        [
            httpx2.Response(
                400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "unsupported temperature for this model"}}
            ),
            httpx2.Response(200, json=ANTHROPIC_OK),
        ]
    )
    adapter = AnthropicCompatibleAdapter(
        base_url="https://gateway.example.com",
        api_key="sk-test",
        provider_name="volces_doubao",
        ssrf_guard=True,
        client_builder=recorder.build,
        provider_options={"models": [{"id": "claude-x", "capabilities": ["chat"]}]},
    )
    try:
        response = await adapter.invoke(
            LLMRequest(model="claude-x", messages=[Message(role="user", content="hi")]),
        )
    finally:
        await adapter.close()

    assert response.content == "OK"
    assert len(recorder.bodies) == 2
    assert recorder.bodies[0]["temperature"] == 0.7
    assert "temperature" not in recorder.bodies[1]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anthropic_never_drops_its_required_max_tokens() -> None:
    """负对照：Anthropic 的 max_tokens 必填，删掉请求根本不合法 —— 不许自愈掉它。"""
    recorder = _Recorder(
        [
            httpx2.Response(400, json={"error": {"message": "max_tokens too large", "param": "max_tokens", "type": "invalid_request_error"}}),
        ]
    )
    adapter = AnthropicCompatibleAdapter(
        base_url="https://gateway.example.com",
        api_key="sk-test",
        provider_name="p",
        ssrf_guard=True,
        client_builder=recorder.build,
        provider_options={"models": [{"id": "claude-x", "capabilities": ["chat"]}]},
    )
    try:
        with pytest.raises(Exception):  # noqa: B017,PT011
            await adapter.invoke(LLMRequest(model="claude-x", messages=[Message(role="user", content="hi")]))
    finally:
        await adapter.close()

    assert len(recorder.bodies) == 1
