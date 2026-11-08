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
from bowhead_router.output_budget import ContextCapacityError
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
            with pytest.raises(ContextCapacityError):
                if mode == "invoke":
                    await router.invoke("analyst", req)
                else:
                    async for _ in router.stream("analyst", req):
                        pass
            assert route.call_count == 0 and secondary.calls == []
        finally:
            await adapter.close()


@pytest.mark.parametrize("change", ["missing", "moved", "duplicated", "model"])
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


@pytest.mark.parametrize("family", ["openai", "responses", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
async def test_complete_large_method_uses_shared_context_accounting(family, stream):
    text = "Synthetic method " + "\u4e2d\u6587\u62a5\u544a\u6392\u7248\u3002" * 600
    required, req = request(text)
    adapter = adapter_for(family)
    try:
        await admit_request_input(req, provider="primary", model="large", context_window=100000, adapter_supported=True)
        body = adapter._build_body(req, stream=stream)
        assert req.context_budget.fits
        assert req.context_budget.estimate_coverage == "text_estimated"
        assert text in required.content
        required.validate_outbound(req, body, family="openai_responses" if family == "responses" else family)
        # Only the resolved model window changes. The same complete source then
        # fails through the shared capacity exception rather than an input quota.
        await admit_request_input(req, provider="primary", model="large", context_window=3000, adapter_supported=True)
        with pytest.raises(ContextCapacityError):
            adapter._build_body(req, stream=stream)
        assert req.messages[0].content == required.content
    finally:
        await adapter.close()


@pytest.mark.parametrize("family", ["openai", "responses", "anthropic"])
@pytest.mark.parametrize("pressure", ["system", "tools", "output"])
async def test_method_shares_capacity_with_actual_system_tools_and_output(family, pressure):
    from bowhead_router.types import ToolSchema

    _, req = request("METHOD")
    adapter = adapter_for(family, output=99000 if pressure == "output" else None)
    if pressure == "system":
        req.system = "Large system instructions. " * 30000
    elif pressure == "tools":
        req.tools = [ToolSchema("tool", "Synthetic tool", {"type": "object", "description": "Large schema instructions. " * 30000})]
    try:
        await admit_request_input(req, provider="primary", model="large", context_window=100000, adapter_supported=True)
        with pytest.raises(ContextCapacityError):
            adapter._build_body(req, stream=False)
    finally:
        await adapter.close()


@pytest.mark.parametrize("family", ["openai", "responses", "anthropic"])
async def test_receiver_selection_uses_model_output_policy_without_a_tiny_caller_cap(family):
    from bowhead_core.chat_reference_scope import ReferenceRequestScope
    from bowhead_core.chat_references import AgentResourceRef
    from bowhead_core.reference_receiver_candidates import ReferenceReceiverCandidate, ReferenceReceiverCandidates, select_reference_receiver

    candidates = ReferenceReceiverCandidates(
        ReferenceRequestScope(team_id="team_test", user_id="u_test", workspace_id="ws_test"),
        "sha256:" + "a" * 64,
        1,
        tuple(
            ReferenceReceiverCandidate(
                AgentResourceRef(kind="agent", source_kind="system", id="system:" + name, version="arv_" + "a" * 32),
                name,
                name,
                "Synthetic candidate",
                (),
            )
            for name in ("compute", "analyst")
        ),
    )
    adapter = adapter_for(family)
    router = Router(Registry(primary=adapter))
    router.bind(AgentBinding(agent_type="lead", primary=("primary", "large")))
    sent = []

    def respond(request):
        body = json.loads(request.content)
        sent.append(body)
        allowance = body.get("max_output_tokens", body.get("max_tokens"))
        # A scripted reasoning provider cannot finish within 64 total output
        # tokens. Exercise the real caller, Router and native request compiler.
        complete = allowance > 64
        content = '{"candidate_index":1}' if complete else ""
        if family == "responses":
            response = {
                "status": "completed" if complete else "incomplete",
                "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": content}]}],
                "incomplete_details": None if complete else {"reason": "max_output_tokens"},
            }
        elif family == "anthropic":
            response = {"content": [{"type": "text", "text": content}], "stop_reason": "end_turn" if complete else "max_tokens"}
        else:
            response = {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop" if complete else "length"}]}
        import httpx2

        return httpx2.Response(200, json=response)

    try:
        with respx.mock() as mock:
            mock.post(url__regex=r"https://api\.example\.com/.*").mock(side_effect=respond)
            selected = await select_reference_receiver(
                candidates,
                task_text="Write a synthetic HTML report",
                router=router,
                trace_id="routing:test",
                team_id="team_test",
                user_id="u_test",
                workspace_id="ws_test",
            )
        assert selected == candidates.candidates[1].resource
        assert len(sent) == 1
        assert not sent[0].get("tools")
    finally:
        await adapter.close()


@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_method_fallback_measures_the_selected_target_before_sending(mode):
    from bowhead_router.errors import RetryPolicy

    required, req = request("Complete method data. " * 150)
    primary = adapter_for("openai")
    secondary = OpenAICompatibleAdapter(base_url="https://secondary.example.com/v1", api_key="test-only", provider_name="secondary")
    router = Router(Registry(primary=primary, secondary=secondary))
    router.bind(AgentBinding(agent_type="analyst", primary=("primary", "large"), fallback=[("secondary", "large")]))
    router.set_context_windows({("primary", "large"): 100000, ("secondary", "large"): 2000})
    try:
        with respx.mock(assert_all_called=False) as mock:
            first = mock.post(url__regex=r"https://api\.example\.com/.*").respond(503, json={"error": {"message": "Synthetic outage"}})
            fallback = mock.post(url__regex=r"https://secondary\.example\.com/.*").respond(200, json={})
            with pytest.raises(ContextCapacityError):
                if mode == "invoke":
                    await router.invoke("analyst", req, retry_policy=RetryPolicy(max_attempts_per_target=1))
                else:
                    async for _ in router.stream("analyst", req, retry_policy=RetryPolicy(max_attempts_per_target=1)):
                        pass
            assert first.call_count == 1 and fallback.call_count == 0
            assert req.messages[0].content == required.content
    finally:
        await primary.close()
        await secondary.close()


@pytest.mark.parametrize("family", ["openai", "responses"])
async def test_method_preserves_legal_configured_output_omission(family):
    required, req = request("METHOD_DATA")
    adapter = adapter_for(family)
    adapter.provider_options["models"][0]["outbound_params"] = {"max_tokens": {"mode": "omit"}}
    try:
        await admit_request_input(req, provider="primary", model="large", context_window=100000, adapter_supported=True)
        body = adapter._build_body(req, stream=False)
        assert not {"max_tokens", "max_output_tokens", "max_completion_tokens"}.intersection(body)
        assert req.context_budget.fits
        required.validate_outbound(req, body, family="openai_responses" if family == "responses" else family)
    finally:
        await adapter.close()
