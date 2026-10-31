"""Rendered capacity failures and saved policy preserve continuation choices."""

from __future__ import annotations

import pytest
from bowhead_router.context_policy import ContextPolicyError, fallback_context_policy, model_context_policy, parse_context_policy
from bowhead_router.output_budget import ContextCapacityError, rendered_request_budget
from bowhead_router.parameter_preview import compile_request_body
from bowhead_router.types import LLMRequest, Message, ToolSchema

pytestmark = pytest.mark.unit


def definition(window: int, *, family: str = "openai_compatible", omitted: bool = False) -> dict:
    model = {"id": "private-model", "context_window": window, "capabilities": ["chat", "tool"]}
    if omitted:
        model["outbound_params"] = {"max_tokens": {"mode": "omit"}}
    if family == "responses":
        model["api_surface"] = "responses"
        family = "openai_compatible"
    return {"type": family, "base_url": "https://example.com/v1", "models": [model]}


@pytest.mark.parametrize("family", ["openai_compatible", "responses", "anthropic_compatible"])
def test_preflight_and_final_budget_count_rendered_system_tools_and_input_once(family):
    provider = definition(128_000, family=family)
    request = LLMRequest(
        model="private-model",
        messages=[Message(role="user", content="Evaluate the evidence. " * 100)],
        system="Preserve source references. " * 100,
        tools=[ToolSchema(name="read", description="Retrieve evidence. " * 100, input_schema={"type": "object"})],
    )
    body = compile_request_body(provider, request)
    assert request.context_budget == rendered_request_budget(request, body, provider, required_output=1024)
    assert request.context_budget.fits
    assert request.context_budget.required_output == 1024
    assert request.output_budget["wire_allowance"] > request.context_budget.required_output


@pytest.mark.parametrize("family", ["openai_compatible", "responses"])
def test_omitted_output_still_rejects_oversized_input_before_send(family):
    provider = definition(4000, family=family, omitted=True)
    request = LLMRequest(model="private-model", messages=[Message(role="user", content="Evidence " * 10_000)])
    with pytest.raises(ContextCapacityError) as caught:
        compile_request_body(provider, request)
    assert caught.value.model == "private-model"
    assert caught.value.budget == request.context_budget
    assert not caught.value.budget.fits
    assert "Evidence" not in str(caught.value)


def test_explicit_output_reservation_cannot_be_shrunk_to_fit():
    request = LLMRequest(model="private-model", messages=[], max_output_tokens=8000)
    with pytest.raises(ContextCapacityError) as caught:
        compile_request_body(definition(8000), request)
    assert caught.value.budget.required_output == 8000
    assert request.max_output_tokens == 8000


def test_model_policy_omission_reset_and_fallback_preserve_off():
    admitted = parse_context_policy({"enabled": False, "trigger_input_tokens": 20_000, "keep_recent_tokens": 4000})
    destination = model_context_policy({"models": [{"id": "small", "context_policy": {"trigger_input_tokens": 12_000}}]}, "small")
    assert parse_context_policy(None).enabled
    assert parse_context_policy({}).enabled
    assert not fallback_context_policy(admitted, destination).enabled
    assert fallback_context_policy(admitted, destination).trigger_input_tokens == 12_000
    assert not fallback_context_policy(destination, admitted).enabled
    assert admitted.keep_recent_tokens == 4000


@pytest.mark.parametrize("value", [True, 1.5, "1000", 0, -1, 9_007_199_254_740_992])
def test_numeric_policy_rejects_coercion_and_unsafe_values(value):
    with pytest.raises(ContextPolicyError, match="trigger_input_tokens"):
        parse_context_policy({"trigger_input_tokens": value})


@pytest.mark.parametrize("value", [{"enabled": None}, {"enabled": 0}, {"unknown": True}, {"trigger_input_tokens": 1000, "keep_recent_tokens": 1000}])
def test_malformed_policy_never_resets_to_enabled(value):
    with pytest.raises(ContextPolicyError):
        parse_context_policy(value)


@pytest.mark.parametrize("window", [32_000, 128_000, 200_000, 1_000_000])
@pytest.mark.parametrize("system_size", [100, 4000])
def test_automatic_policy_reserves_summary_growth_and_recent_work(window, system_size):
    from dataclasses import replace

    from bowhead_router.context_policy import ContextPolicy, resolve_context_policy, summary_output_target

    provider = definition(window)
    ordinary = LLMRequest(model="private-model", system="Rules " * system_size, messages=[Message(role="user", content="Task " * 1000)])
    fixed = replace(ordinary, messages=[])
    summary = replace(
        ordinary,
        messages=[*ordinary.messages, Message(role="user", content="Produce a continuity summary. " * 200)],
        max_output_tokens=summary_output_target(window),
    )
    for request in (ordinary, fixed, summary):
        compile_request_body(provider, request)
    resolved = resolve_context_policy(
        ContextPolicy(),
        ordinary=ordinary.context_budget,
        summary=summary.context_budget,
        fixed_input_tokens=fixed.context_budget.input_estimate,
        max_growth_tokens=262144,
    )
    assert resolved.trigger_input_tokens + resolved.growth_tokens <= ordinary.context_budget.input_limit
    overhead = summary.context_budget.input_estimate - ordinary.context_budget.input_estimate
    assert resolved.trigger_input_tokens + resolved.growth_tokens + overhead <= summary.context_budget.input_limit
    assert (
        resolved.keep_recent_tokens + resolved.summary_output_tokens + resolved.growth_tokens + fixed.context_budget.input_estimate
        <= resolved.trigger_input_tokens
    )
    assert resolved.keep_recent_tokens > 0
    # The historical global 40k allowance must not constrain large contexts.
    if window == 1_000_000:
        assert resolved.trigger_input_tokens > 700_000
        assert resolved.keep_recent_tokens > 100_000


def test_saved_policy_is_not_rewritten_when_current_shape_bounds_it():
    from bowhead_router.context_policy import ContextPolicy, resolve_context_policy
    from bowhead_router.output_budget import RenderedRequestBudget

    saved = ContextPolicy(enabled=False, trigger_input_tokens=100_000, keep_recent_tokens=50_000)
    ordinary = RenderedRequestBudget(1000, "text_estimated", 32_000, "model", 1024)
    summary = RenderedRequestBudget(1500, "text_estimated", 32_000, "model", 1024)
    resolved = resolve_context_policy(saved, ordinary=ordinary, summary=summary, fixed_input_tokens=500, max_growth_tokens=8000)
    assert not saved.enabled and saved.keep_recent_tokens == 50_000
    assert resolved.trigger_input_tokens < 32_000
    assert resolved.keep_recent_tokens < resolved.trigger_input_tokens
    assert resolved.limiting_reasons == ("trigger_limited_by_request_capacity", "recent_limited_by_request_capacity")
