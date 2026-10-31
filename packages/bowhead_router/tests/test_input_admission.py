"""Admission is per router attempt, including retry/fallback; never provider I/O."""

from __future__ import annotations

import asyncio
import traceback
from types import SimpleNamespace

import pytest
from bowhead_router.errors import RetryPolicy
from bowhead_router.input_admission import LLMInputAdmissionError
from bowhead_router.invocation import begin_physical_invocation, physical_invocation_scope
from bowhead_router.router import AgentBinding, Router
from bowhead_router.types import LLMChunk, LLMRequest, LLMResponse

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class Adapter:
    supports_input_admission = True

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def invoke(self, request):
        self.calls.append(request)
        value = self.outcomes.pop(0)
        if isinstance(value, BaseException):
            raise value
        return LLMResponse(content=value)

    async def stream(self, request):
        response = await self.invoke(request)
        yield LLMChunk(delta=response.content, finish_reason="stop")


class Registry:
    def __init__(self, **adapters):
        self.adapters = adapters

    def has(self, name):
        return name in self.adapters

    def get(self, name):
        return self.adapters[name]


class Failure(Exception):
    def __init__(self, retryable):
        self.retryable = retryable


class Guard:
    def __init__(self, denied_call=None, failure=None):
        self.calls = []
        self.denied_call = denied_call
        self.failure = failure or LLMInputAdmissionError("denied")

    async def validate(self, request, **target):
        self.calls.append((request, target))
        if len(self.calls) == self.denied_call:
            raise self.failure


async def run(router, request, mode):
    policy = RetryPolicy(max_attempts_per_target=2, base_delay_s=0)
    if mode == "invoke":
        return (await router.invoke("analyst", request, retry_policy=policy)).content
    return "".join([chunk.delta async for chunk in router.stream("analyst", request, retry_policy=policy)])


def setup(guard, outcomes):
    primary = Adapter(outcomes)
    fallback = Adapter(["fallback"])
    router = Router(Registry(primary=primary, fallback=fallback))
    router.bind(AgentBinding(agent_type="analyst", primary=("primary", "large"), fallback=[("fallback", "small")], max_output_tokens=123))
    router.set_context_windows({("primary", "large"): 100000, ("fallback", "small"): 1000})
    return router, LLMRequest(model="unspecified", messages=[], max_output_tokens=None, input_admission=guard), primary, fallback


@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_every_retry_and_fallback_keeps_admission_and_actual_target(mode):
    guard = Guard()
    router, request, primary, fallback = setup(guard, [Failure(True), Failure(False)])
    assert await run(router, request, mode) == "fallback"
    assert [target for _, target in guard.calls] == [
        {"provider": "primary", "model": "large", "context_window": 100000},
        {"provider": "primary", "model": "large", "context_window": 100000},
        {"provider": "fallback", "model": "small", "context_window": 1000},
    ]
    assert all(req.input_admission is guard and req.max_output_tokens == 123 and "runtime_context" in req.system for req, _ in guard.calls)
    assert request.model == "unspecified" and request.system is None
    assert len(primary.calls) == 2 and len(fallback.calls) == 1


@pytest.mark.parametrize("mode", ["invoke", "stream"])
@pytest.mark.parametrize("denied_call", [1, 2, 3])
async def test_local_denial_never_calls_adapter_or_falls_through(mode, denied_call):
    guard = Guard(denied_call)
    router, request, primary, fallback = setup(guard, [Failure(True), Failure(False)])
    with pytest.raises(LLMInputAdmissionError, match="^denied$"):
        await run(router, request, mode)
    assert len(primary.calls) + len(fallback.calls) == denied_call - 1
    assert len(guard.calls) == denied_call


@pytest.mark.parametrize("mode", ["invoke", "stream"])
@pytest.mark.parametrize("failure", [ValueError("PRIVATE_INPUT"), asyncio.CancelledError()])
async def test_admission_errors_and_cancellation_do_not_become_provider_failures(mode, failure):
    guard = Guard(1, failure)
    router, request, primary, fallback = setup(guard, ["unused"])
    with pytest.raises(asyncio.CancelledError if isinstance(failure, asyncio.CancelledError) else LLMInputAdmissionError) as error:
        await run(router, request, mode)
    assert "PRIVATE_INPUT" not in "".join(traceback.format_exception(error.value))
    assert primary.calls == fallback.calls == []


@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_adapter_without_final_body_gate_is_not_reference_ready(mode):
    guard = Guard()
    router, request, primary, fallback = setup(guard, ["unused"])
    primary.supports_input_admission = False
    with pytest.raises(LLMInputAdmissionError, match="adapter_unavailable"):
        await run(router, request, mode)
    assert primary.calls == fallback.calls == [] and guard.calls == []


@pytest.mark.parametrize("with_hook", [False, True])
@pytest.mark.parametrize("failure", ["missing", "private", "cancelled"])
async def test_physical_admission_is_required_before_tickets_even_without_a_terminal_hook(with_hook, failure):
    tickets = []

    class Hook:
        async def begin(self, spec):
            tickets.append(spec)
            raise AssertionError("local rejection must precede the durable ticket")

    async def deny(*args, **kwargs):
        if failure == "cancelled":
            raise asyncio.CancelledError()
        raise ValueError("PRIVATE_SOURCE_DETAILS")

    guard = SimpleNamespace() if failure == "missing" else SimpleNamespace(validate_dispatch=deny)
    request = LLMRequest(model="model", messages=[], input_admission=guard)
    with (
        physical_invocation_scope(Hook() if with_hook else None),
        pytest.raises(asyncio.CancelledError if failure == "cancelled" else LLMInputAdmissionError) as error,
    ):
        await begin_physical_invocation(
            provider="primary",
            model="model",
            adapter_contract_key="openai/1",
            mode="invoke",
            request={"model": "model"},
            input_request=request,
            input_family="openai",
        )
    assert tickets == []
    assert "PRIVATE_SOURCE_DETAILS" not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_owned_capacity_recovery_targets_only_the_selected_fallback(mode):
    guard = Guard()
    router, request, primary, fallback = setup(guard, ["primary must not be restarted"])
    if mode == "invoke":
        result = (await router.invoke("analyst", request, target=("fallback", "small"))).content
    else:
        result = "".join([chunk.delta async for chunk in router.stream("analyst", request, target=("fallback", "small"))])
    assert result == "fallback"
    assert primary.calls == []
    assert len(fallback.calls) == 1
    assert guard.calls[0][1] == {"provider": "fallback", "model": "small", "context_window": 1000}


@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_owned_target_cannot_bypass_the_admitted_route(mode):
    guard = Guard()
    router, request, primary, fallback = setup(guard, ["primary"])
    with pytest.raises(LLMInputAdmissionError, match="target_not_configured"):
        if mode == "invoke":
            await router.invoke("analyst", request, target=("fallback", "unconfigured"))
        else:
            _ = [chunk async for chunk in router.stream("analyst", request, target=("fallback", "unconfigured"))]
    assert primary.calls == fallback.calls == guard.calls == []
