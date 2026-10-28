"""Physical request budgets honor model capacity, input and explicit policies."""

from __future__ import annotations

import math

import pytest
from bowhead_router.model_capabilities import model_token_capacity
from bowhead_router.output_budget import OutputBudgetError, estimate_body_input, output_reservation
from bowhead_router.parameter_preview import compile_request_body
from bowhead_router.types import LLMRequest, Message, ToolSchema

pytestmark = pytest.mark.unit


def provider(*, capacity=None, window=1_000_000, rule=None, kind="openai_compatible", surface=None):
    model = {"id": "deployment", "context_window": window, "capabilities": ["chat", "tool"]}
    if capacity is not None:
        model["max_output_tokens"] = capacity
    if rule:
        model["outbound_params"] = {"max_tokens": rule}
    if surface:
        model["api_surface"] = surface
    return {"type": kind, "base_url": "https://example.com/v1", "models": [model]}


@pytest.mark.parametrize("capacity,expected", [(None, 32_000), (16_384, 16_384), (128_000, 128_000), (393_216, 256_000)])
def test_automatic_uses_actual_capacity_and_default_ceiling(capacity, expected):
    request = LLMRequest(model="deployment", messages=[Message(role="user", content="Write a report.")])
    assert compile_request_body(provider(capacity=capacity), request)["max_tokens"] == expected
    assert request.max_output_tokens is None
    assert request.output_budget["capacity_source"] == ("fallback" if capacity is None else "model")


def test_aliases_require_declarations_and_provider_default_yields_to_model():
    definition = {"max_output_tokens": 48_000, "models": [{"id": "deployment", "max_output_tokens": 64_000}]}
    assert model_token_capacity(definition, "deployment").value == 64_000
    assert model_token_capacity(definition, "other").value == 48_000
    assert model_token_capacity({}, "deepseek-flash").value == 393_216
    assert model_token_capacity({}, "company-deepseek-flash").source == "fallback"


@pytest.mark.parametrize(
    "kind,surface,field",
    [
        ("openai_compatible", None, "max_tokens"),
        ("openai_compatible", "responses", "max_output_tokens"),
        ("azure_openai", None, "max_tokens"),
        ("anthropic_compatible", None, "max_tokens"),
    ],
)
def test_each_protocol_sends_one_output_field(kind, surface, field):
    body = compile_request_body(provider(capacity=64_000, kind=kind, surface=surface), LLMRequest(model="deployment", messages=[]))
    assert {key: body[key] for key in ("max_tokens", "max_output_tokens", "max_completion_tokens") if key in body} == {field: 64_000}


@pytest.mark.parametrize(
    "rule,expected",
    [({"mode": "follow"}, 256_000), ({"mode": "cap", "value": 9000}, 9000), ({"mode": "fixed", "value": 300_000}, 300_000), ({"mode": "omit"}, None)],
)
def test_explicit_policy_and_legal_omission_survive_automatic_defaults(rule, expected):
    request = LLMRequest(model="deployment", messages=[])
    body = compile_request_body(provider(capacity=393_216, rule=rule), request)
    assert body.get("max_tokens") == expected
    assert request.output_budget["wire_allowance"] == expected


def test_small_deliberate_call_stays_small_and_unknown_capacity_is_not_a_hard_maximum():
    for amount in (16, 4000, 48_000):
        assert compile_request_body(provider(), LLMRequest(model="deployment", messages=[], max_output_tokens=amount))["max_tokens"] == amount


def test_rendered_system_and_tool_schema_reduce_output_without_deleting_input():
    request = LLMRequest(
        model="deployment",
        messages=[Message(role="user", content="Evidence. " * 200)],
        system="Policy. " * 100,
        tools=[ToolSchema(name="write", description="Definition " * 200, input_schema={"type": "object"})],
    )
    body = compile_request_body(provider(capacity=32_000, window=6000), request)
    estimate, _ = estimate_body_input(body)
    assert body["max_tokens"] == 6000 - math.ceil(estimate * 1.15) - 1024
    assert body["messages"][0]["content"] == request.system
    assert request.output_budget["context_reduced"] is True


def test_impossible_input_and_explicit_excess_fail_locally():
    with pytest.raises(OutputBudgetError, match="insufficient"):
        compile_request_body(provider(window=1200), LLMRequest(model="deployment", messages=[]))
    with pytest.raises(OutputBudgetError, match="exceeds model capacity"):
        compile_request_body(provider(capacity=8000), LLMRequest(model="deployment", messages=[], max_output_tokens=9000))
    with pytest.raises(OutputBudgetError, match="does not fit"):
        compile_request_body(provider(window=8000), LLMRequest(model="deployment", messages=[], max_output_tokens=8000))


def test_input_preparation_reserves_progress_instead_of_the_desired_256k():
    definition = provider(capacity=393_216)
    assert output_reservation(definition, "deployment", None, capability="tool", family="openai") == 1024
    assert output_reservation(definition, "deployment", None, capability="tool", family="openai", thinking_budget=4096) == 5120


def test_runtime_follow_keeps_a_legacy_fixed_allowance_explicit():
    definition = provider(capacity=393_216, rule={"mode": "fixed", "value": 300_000})
    definition["models"][0]["runtime_parameters"] = {"defaults": {"outbound_params": {"max_tokens": {"mode": "follow"}}}}
    request = LLMRequest(model="deployment", messages=[])
    assert compile_request_body(definition, request)["max_tokens"] == 300_000
    assert request.output_budget["automatic"] is False
    assert output_reservation(definition, "deployment", None, capability="chat", family="openai") == 300_000


@pytest.mark.parametrize("kind,surface", [("openai_compatible", None), ("openai_compatible", "responses"), ("anthropic_compatible", None)])
def test_recovery_notice_reports_the_final_wire_allowance_without_private_checkpoint_hashes(kind, surface):
    import json

    from bowhead_router.runtime_notes import OUTPUT_RECOVERY_STATE_NAME

    marker = Message(
        role="user",
        name=OUTPUT_RECOVERY_STATE_NAME,
        content=json.dumps({"attempts": 1, "operations": ["a" * 64], "results": [], "instruction": "Produce one smaller complete action."}),
    )
    request = LLMRequest(model="deployment", messages=[Message(role="user", content="Task"), marker])
    body = compile_request_body(provider(capacity=64_000, window=10_000, kind=kind, surface=surface), request)
    allowance = request.output_budget["wire_allowance"]
    assert allowance < 10_000
    assert f"at most {allowance} output tokens" in json.dumps(body)
    assert "a" * 64 not in json.dumps(body)
    assert marker.name == OUTPUT_RECOVERY_STATE_NAME


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_fallback_recompiles_for_its_own_capacity_and_surface(stream):
    from bowhead_router.errors import ProviderCallError, ProviderFailureKind, RetryPolicy
    from bowhead_router.router import AgentBinding, Router
    from bowhead_router.types import LLMChunk, LLMResponse

    seen = []

    class Adapter:
        def __init__(self, definition, fail):
            self.definition, self.fail = definition, fail

        def _provider_definition(self):
            return self.definition

        async def invoke(self, request):
            body = compile_request_body(self.definition, request)
            seen.append(body)
            if self.fail:
                raise ProviderCallError(kind=ProviderFailureKind.UPSTREAM, retryable=False)
            return LLMResponse(content="done")

        async def stream(self, request):
            response = await self.invoke(request)
            yield LLMChunk(delta=response.content, finish_reason="stop")

    class Registry:
        adapters = {"primary": Adapter(provider(capacity=393_216), True), "fallback": Adapter(provider(capacity=16_384, surface="responses"), False)}

        def has(self, name):
            return name in self.adapters

        def get(self, name):
            return self.adapters[name]

    router = Router(Registry())
    router.bind(AgentBinding(agent_type="analyst", primary=("primary", "deployment"), fallback=[("fallback", "deployment")]))
    request = LLMRequest(model="", messages=[Message(role="user", content="Report")])
    if stream:
        chunks = [chunk async for chunk in router.stream("analyst", request, retry_policy=RetryPolicy(max_attempts_per_target=1))]
        assert "".join(chunk.delta for chunk in chunks) == "done"
        assert chunks[-1].diagnostics.output_token_limit == 16_384
    else:
        response = await router.invoke("analyst", request, retry_policy=RetryPolicy(max_attempts_per_target=1))
        assert response.diagnostics.output_token_limit == 16_384
    assert seen[0]["max_tokens"] == 256_000
    assert seen[1]["max_output_tokens"] == 16_384
