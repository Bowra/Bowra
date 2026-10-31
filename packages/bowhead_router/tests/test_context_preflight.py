"""Context preflight uses the final transport shape without model dispatch."""

from __future__ import annotations

from copy import deepcopy

import pytest
from bowhead_router.input_admission import LLMInputAdmissionError
from bowhead_router.output_budget import ContextCapacityError
from bowhead_router.registry import ProviderConfig, ProviderRegistry
from bowhead_router.router import AgentBinding, Router
from bowhead_router.types import ImageRef, LLMRequest, LLMResponse, Message, ToolSchema

pytestmark = pytest.mark.unit


def router_for(window: int, family: str, output: str) -> tuple[Router, ProviderRegistry]:
    registry = ProviderRegistry()
    model = {"id": "model", "context_window": window, "capabilities": ["chat", "tool", "vision"], "context_policy": {"enabled": False}}
    if family == "responses":
        model["api_surface"] = "responses"
    if output != "auto":
        model["outbound_params"] = {"max_tokens": {"mode": output, **({"value": 4096} if output == "fixed" else {})}}
    registry.load(
        [
            ProviderConfig(
                name="primary",
                type="anthropic_compatible" if family == "anthropic" else "openai_compatible",
                base_url="https://example.com/v1",
                models=[model],
            )
        ]
    )
    router = Router(registry)
    router.configuration_version = 42
    router.bind(AgentBinding(agent_type="lead", primary=("primary", "model"), fallback=[("primary", "small")]))
    router.set_context_windows({("primary", "model"): window, ("primary", "small"): 4000})
    return router, registry


@pytest.mark.asyncio
@pytest.mark.parametrize("window", [32_000, 128_000, 200_000, 1_000_000])
@pytest.mark.parametrize("family,output", [("chat", "auto"), ("responses", "fixed"), ("chat", "omit"), ("anthropic", "auto")])
async def test_preflight_matches_physical_rendering_and_does_not_change_input(window, family, output, monkeypatch):
    router, registry = router_for(window, family, output)
    request = LLMRequest(
        model="",
        system="Keep evidence and constraints. " * 100,
        messages=[Message(role="user", content="Inspect this evidence. " * 100, images=[ImageRef(base64="aGVsbG8=", mime="image/png")])],
        tools=[ToolSchema(name="read_source", description="Return authorized source data. " * 100, input_schema={"type": "object"})],
    )
    before = deepcopy(request)
    observed = []
    adapter = registry.get("primary")

    async def invoke(prepared):
        adapter._build_body(prepared, stream=False)
        observed.append(prepared.context_budget)
        return LLMResponse(content="Complete")

    monkeypatch.setattr(adapter, "invoke", invoke)
    try:
        result = await router.request_context_budget("lead", request, stream=False)
        assert observed == []
        assert request == before
        assert request.context_budget is None
        assert result.configuration_version == 42
        assert result.budget.context_window == window
        assert result.budget.estimate_coverage == "media_estimated"
        assert not result.policy.enabled
        await router.invoke("lead", request)
        assert observed == [result.budget]
        assert request == before
    finally:
        await registry.close_all()


@pytest.mark.asyncio
async def test_large_primary_is_not_limited_by_smaller_fallback():
    router, registry = router_for(1_000_000, "chat", "auto")
    request = LLMRequest(model="", messages=[Message(role="user", content="Evidence " * 20_000)])
    try:
        result = await router.request_context_budget("lead", request)
        assert result.budget.fits
        with pytest.raises(ContextCapacityError) as caught:
            await router.request_context_budget("lead", request, target=("primary", "small"))
        assert caught.value.provider == "primary"
        assert caught.value.model == "small"
        assert caught.value.configuration_version == 42
        assert caught.value.budget.context_window == 4000
        with pytest.raises(LLMInputAdmissionError, match="target_not_configured"):
            await router.request_context_budget("lead", request, target=("unrelated", "model"))
    finally:
        await registry.close_all()


@pytest.mark.asyncio
async def test_source_authority_failure_is_not_a_capacity_error():
    router, registry = router_for(128_000, "chat", "auto")

    class Revoked:
        async def validate(self, *args, **kwargs):
            raise LLMInputAdmissionError("source_access_revoked")

    request = LLMRequest(model="", messages=[], input_admission=Revoked())
    try:
        with pytest.raises(LLMInputAdmissionError, match="source_access_revoked") as caught:
            await router.request_context_budget("lead", request)
        assert not isinstance(caught.value, ContextCapacityError)
    finally:
        await registry.close_all()
