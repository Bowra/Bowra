"""Final native adapter bodies retain methods and include effective output caps.

HTTP is mocked. This measures neither real tokenizer usage nor provider latency.
"""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
import respx
from bowhead_router.adapters.anthropic_compat import AnthropicCompatibleAdapter
from bowhead_router.adapters.openai_compat import OpenAICompatibleAdapter
from bowhead_router.input_admission import LLMInputAdmissionError, admit_request_input
from bowhead_router.router import AgentBinding, Router
from bowhead_router.types import ImageRef
from test_skill_read_model_input import Adapter, Registry, request

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def adapter_for(family, *, output=None):
    model = {"id": "large"}
    if family == "responses":
        model["api_surface"] = "responses"
    if output is not None:
        model["outbound_params"] = {"max_tokens": {"mode": "fixed", "value": output}}
    cls = AnthropicCompatibleAdapter if family == "anthropic" else OpenAICompatibleAdapter
    return cls(base_url="https://api.example.com/v1", api_key="test-only", provider_name="primary", provider_options={"models": [model]})


@pytest.mark.parametrize("family", ["openai", "responses", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
async def test_final_body_preserves_required_bytes_and_has_no_policy_object(family, stream):
    required, req = request("METHOD_中文_JSON")
    adapter = adapter_for(family)
    try:
        await admit_request_input(req, provider="primary", model="large", context_window=100000, adapter_supported=True)
        body = adapter._build_body(req, stream=stream)
        text = json.dumps(body, ensure_ascii=False)
        assert "input_admission" not in text
        slots = body["input" if family == "responses" else "messages"]
        user = next(message for message in slots if message.get("role") == "user")
        content = user["content"]
        user_text = content if isinstance(content, str) else "".join(part.get("text", "") for part in content)
        assert user_text.startswith(required.content)
        assert user_text.count(required.content) == 1
        assert "TASK" in text and "FINALIZE IN CHINESE" in text
        assert body["max_output_tokens" if family == "responses" else "max_tokens"] == 32000
    finally:
        await adapter.close()


@pytest.mark.parametrize("family", ["openai", "responses", "anthropic"])
async def test_reference_image_survives_native_wire_and_missing_image_is_rejected(family):
    required, req = request("METHOD_WITH_IMAGE")
    req.messages[1].images = [ImageRef(base64="UE5H", mime="image/png")]
    adapter = adapter_for(family)
    try:
        await admit_request_input(req, provider="primary", model="large", context_window=100000, adapter_supported=True)
        body = adapter._build_body(req, stream=False)
        assert "UE5H" in json.dumps(body)
        required.validate_outbound(req, body, family="openai_responses" if family == "responses" else family)
        missing = deepcopy(body)
        wire = json.dumps(missing).replace("UE5H", "")
        with pytest.raises(LLMInputAdmissionError, match="image_changed"):
            required.validate_outbound(req, json.loads(wire), family="openai_responses" if family == "responses" else family)
    finally:
        await adapter.close()


@pytest.mark.parametrize("family", ["openai", "responses", "anthropic"])
@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_final_output_override_is_checked_before_http_and_not_fallback(family, mode, monkeypatch):
    _, req = request("X" * 1000)
    adapter = adapter_for(family, output=99000)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url__regex=r"https://api\.example\.com/.*").respond(200, json={})
        secondary = Adapter(["must not execute"])
        router = Router(Registry(primary=adapter, secondary=secondary))
        router.bind(AgentBinding(agent_type="analyst", primary=("primary", "large"), fallback=[("secondary", "large")]))
        router.set_context_windows({("primary", "large"): 100000, ("secondary", "large"): 1000000})
        try:
            with pytest.raises(LLMInputAdmissionError, match="context_budget_exceeded"):
                if mode == "invoke":
                    await router.invoke("analyst", req)
                else:
                    async for _ in router.stream("analyst", req):
                        pass
            assert route.call_count == 0 and secondary.calls == []
        finally:
            await adapter.close()


@pytest.mark.parametrize("change", ["missing", "moved", "duplicated", "model", "output", "output_bool", "large_tools"])
async def test_final_body_integrity_and_unknown_reserves_fail_closed(change):
    required, req = request()
    adapter = adapter_for("openai")
    try:
        await admit_request_input(req, provider="primary", model="large", context_window=100000, adapter_supported=True)
        body = adapter._build_body(req, stream=False)
        user = next(m for m in body["messages"] if m["role"] == "user")
        if change == "missing":
            user["content"] = "TASK"
        elif change == "moved":
            user["content"] = "TASK\n" + user["content"]
        elif change == "duplicated":
            body["messages"].append({"role": "user", "content": required.content})
        elif change == "model":
            body["model"] = "other"
        elif change == "output":
            body.pop("max_tokens")
        elif change == "output_bool":
            body["max_tokens"] = True
        else:
            body["tools"] = [{"description": "X" * 100000}]
        with pytest.raises(LLMInputAdmissionError):
            required.validate_outbound(req, body, family="openai")
    finally:
        await adapter.close()


@pytest.mark.parametrize("family", ["openai", "anthropic"])
@pytest.mark.parametrize("deny_second", [False, True])
async def test_adapter_self_heal_rebuilds_are_guarded_before_the_next_http_request(family, deny_second, monkeypatch):
    import httpx2
    from bowhead_core.skill_read_model_input import RequiredSkillModelInput
    from bowhead_router.outbound import reset_learned_for_test

    reset_learned_for_test()
    required, req = request()
    adapter = adapter_for(family)
    router = Router(Registry(primary=adapter))
    router.bind(AgentBinding(agent_type="analyst", primary=("primary", "large")))
    router.set_context_windows({("primary", "large"): 100000})
    original = RequiredSkillModelInput.validate_outbound
    bodies = []

    def check(self, request, body, *, family):
        snapshot = json.loads(json.dumps(body))
        # Construction and physical dispatch both validate the same body.
        # Track distinct self-heal bodies; HTTP call counts remain independent.
        if not bodies or bodies[-1] != snapshot:
            bodies.append(snapshot)
        original(self, request, body, family=family)
        if deny_second and len(bodies) == 2:
            raise LLMInputAdmissionError("reference_model_input_changed")

    monkeypatch.setattr(RequiredSkillModelInput, "validate_outbound", check)
    error = {"error": {"message": "unsupported temperature for this model", "param": "temperature", "type": "invalid_request_error"}}
    success = (
        {"choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]}
        if family == "openai"
        else {"id": "reply", "content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}}
    )
    try:
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(url__regex=r"https://api\.example\.com/.*").mock(
                side_effect=[httpx2.Response(400, json=error), httpx2.Response(200, json=success)]
            )
            if deny_second:
                with pytest.raises(LLMInputAdmissionError, match="input_changed"):
                    await router.invoke("analyst", req)
            else:
                assert (await router.invoke("analyst", req)).content == "OK"
            assert len(bodies) == 2 and route.call_count == (1 if deny_second else 2)
            assert "temperature" in bodies[0] and "temperature" not in bodies[1]
            assert all(required.content in json.dumps(body, ensure_ascii=False).replace("\\n", "\n") for body in bodies)
    finally:
        await adapter.close()
        reset_learned_for_test()


@pytest.mark.parametrize("stream", [False, True])
async def test_custom_http_requires_its_own_verified_wire_contract(stream, monkeypatch):
    _, req = request()
    adapter = adapter_for("openai")
    adapter.adapter_type = "custom_http"

    def forbidden(*args, **kwargs):
        raise AssertionError("unverified template must not receive method input")

    monkeypatch.setattr("bowhead_router.adapters.openai_compat.render_custom_http_body", forbidden)
    try:
        await admit_request_input(req, provider="primary", model="large", context_window=100000, adapter_supported=True)
        with pytest.raises(LLMInputAdmissionError, match="adapter_unavailable"):
            adapter._build_body(req, stream=stream)
    finally:
        await adapter.close()
