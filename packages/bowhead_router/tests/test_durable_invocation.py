from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import bowhead_router.outbound as _outbound
import httpx2
import pytest
import respx
from bowhead_router.adapters import _chi_sniff
from bowhead_router.adapters.anthropic_compat import AnthropicCompatibleAdapter
from bowhead_router.errors import ProviderCallError, ProviderFailureKind
from bowhead_router.invocation import (
    PhysicalInvocationSpec,
    complete_physical_invocation,
    physical_observation_scope,
)
from bowhead_router.types import LLMChunk, TokenUsage

from bowhead_router import (
    AgentBinding,
    InvocationOutcomeUnknownError,
    LLMRequest,
    LLMResponse,
    Message,
    OpenAICompatibleAdapter,
    Router,
    ToolSchema,
    physical_invocation_scope,
)

_BASE = "https://api.openai.com/v1"
_ANTHROPIC_BASE = "https://api.anthropic.example.com"


@dataclass
class _Attempt:
    outcomes: list[str]

    async def confirmed_not_applied(
        self,
        *,
        evidence_code: str,
        evidence_digest: str,
    ) -> bool:
        assert len(evidence_digest) == 64
        self.outcomes.append(f"not_applied:{evidence_code}")
        return True

    async def complete(self, *, usage: TokenUsage, response_digest: str) -> bool:
        self.outcomes.append(f"complete:{usage.input_tokens}:{usage.output_tokens}:{response_digest}")
        return True

    async def unknown(self, *, reason_code: str) -> None:
        self.outcomes.append(f"unknown:{reason_code}")


@dataclass
class _Hook:
    specs: list[PhysicalInvocationSpec] = field(default_factory=list)
    attempts: list[_Attempt] = field(default_factory=list)

    async def begin(self, spec: PhysicalInvocationSpec) -> _Attempt:
        self.specs.append(spec)
        attempt = _Attempt([])
        self.attempts.append(attempt)
        return attempt


class _Observer(_Hook):
    async def begin(self, spec: PhysicalInvocationSpec, *, request: LLMRequest | None, body: object, family: str | None) -> _Attempt:
        return await super().begin(spec)


class _DecliningAttempt(_Attempt):
    async def confirmed_not_applied(self, *, evidence_code: str, evidence_digest: str) -> bool:
        self.outcomes.append(f"declined:{evidence_code}")
        return False


class _DecliningObserver(_Observer):
    async def begin(self, spec: PhysicalInvocationSpec, *, request: LLMRequest | None, body: object, family: str | None) -> _Attempt:
        self.specs.append(spec)
        attempt = _DecliningAttempt([])
        self.attempts.append(attempt)
        return attempt


@dataclass
class _FailingUnknownAttempt(_Attempt):
    async def unknown(self, *, reason_code: str) -> None:
        self.outcomes.append(f"unknown_failed:{reason_code}")
        raise RuntimeError("durable transaction unavailable")


@dataclass
class _FailingUnknownHook(_Hook):
    async def begin(self, spec: PhysicalInvocationSpec) -> _Attempt:
        self.specs.append(spec)
        attempt = _FailingUnknownAttempt([])
        self.attempts.append(attempt)
        return attempt


@dataclass
class _BlockingCompleteAttempt(_Attempt):
    started: asyncio.Event
    never: asyncio.Event

    async def complete(self, *, usage: TokenUsage, response_digest: str) -> bool:
        del usage, response_digest
        self.started.set()
        await self.never.wait()
        raise AssertionError("unreachable")


@dataclass
class _BlockingCompleteHook(_Hook):
    started: asyncio.Event = field(default_factory=asyncio.Event)
    never: asyncio.Event = field(default_factory=asyncio.Event)

    async def begin(self, spec: PhysicalInvocationSpec) -> _Attempt:
        self.specs.append(spec)
        attempt = _BlockingCompleteAttempt([], self.started, self.never)
        self.attempts.append(attempt)
        return attempt


@dataclass
class _FailsThenBlockingUnknownAttempt(_Attempt):
    unknown_started: asyncio.Event
    unknown_release: asyncio.Event

    async def complete(self, *, usage: TokenUsage, response_digest: str) -> bool:
        del usage, response_digest
        raise RuntimeError("settlement transaction failed")

    async def unknown(self, *, reason_code: str) -> None:
        self.unknown_started.set()
        await self.unknown_release.wait()
        self.outcomes.append(f"unknown:{reason_code}")


@dataclass
class _SecondBeginFailsHook(_Hook):
    failure: BaseException = field(default_factory=lambda: RuntimeError("second begin failed"))
    second_begin_started: asyncio.Event | None = None

    async def begin(self, spec: PhysicalInvocationSpec) -> _Attempt:
        self.specs.append(spec)
        if len(self.specs) == 2:
            if self.second_begin_started is not None:
                self.second_begin_started.set()
                await asyncio.Event().wait()
            raise self.failure
        attempt = _Attempt([])
        self.attempts.append(attempt)
        return attempt


def _tool() -> ToolSchema:
    return ToolSchema(
        name="echo",
        description="echo",
        input_schema={"type": "object", "properties": {}},
    )


def _ok() -> dict[str, object]:
    return {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }


def _anthropic_ok() -> dict[str, object]:
    return {
        "content": [{"type": "text", "text": "ok"}],
        "model": "model",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 4, "output_tokens": 3},
    }


def _register_durable_chi_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _chi_sniff,
        "_DURABLE_REJECTION_MANIFEST_V1",
        frozenset(
            {
                (
                    "openai_compatible",
                    "openai-compatible-v1",
                    "api.openai.com",
                    "parallel_tool_calls",
                    "unknown_parameter",
                )
            }
        ),
    )


@pytest.mark.unit
@pytest.mark.parametrize("family", ["openai", "anthropic"])
@pytest.mark.parametrize("mode", ["invoke", "stream"])
@pytest.mark.parametrize("status_code", [401, 403])
@respx.mock
async def test_observed_auth_refusal_keeps_provider_error_and_does_not_retry(
    family: str,
    mode: str,
    status_code: int,
) -> None:
    base_url = _BASE if family == "openai" else _ANTHROPIC_BASE
    path = "/chat/completions" if family == "openai" else "/v1/messages"
    response = (
        {"error": {"code": "invalid_api_key", "message": "Invalid API key"}}
        if family == "openai"
        else {"type": "error", "error": {"type": "authentication_error", "message": "Invalid API key"}}
    )
    route = respx.post(f"{base_url}{path}").respond(status_code, json=response)
    observer = _Observer()
    adapter = (
        OpenAICompatibleAdapter(base_url=base_url, api_key="bad-test-key", provider_name="provider")
        if family == "openai"
        else AnthropicCompatibleAdapter(base_url=base_url, api_key="bad-test-key", provider_name="provider")
    )
    request = LLMRequest(model="model", messages=[Message(role="user", content="hi")])
    try:
        with physical_observation_scope(observer), pytest.raises(ProviderCallError) as raised:
            if mode == "invoke":
                await adapter.invoke(request)
            else:
                async for _ in adapter.stream(request):
                    pass
    finally:
        await adapter.close()

    assert raised.value.status_code == status_code
    assert raised.value.kind is (ProviderFailureKind.AUTH if status_code == 401 else ProviderFailureKind.PERMISSION)
    assert raised.value.upstream_message == "Invalid API key"
    assert len(route.calls) == 1
    assert observer.attempts[0].outcomes == ["not_applied:provider_http_auth_rejected/v1"]


@pytest.mark.unit
@respx.mock
async def test_auth_refusal_with_unconfirmed_settlement_stays_unknown() -> None:
    route = respx.post(f"{_BASE}/chat/completions").respond(
        401,
        json={"error": {"code": "invalid_api_key", "message": "Invalid API key"}},
    )
    observer = _DecliningObserver()
    adapter = OpenAICompatibleAdapter(base_url=_BASE, api_key="bad-test-key", provider_name="provider")
    try:
        with physical_observation_scope(observer), pytest.raises(InvocationOutcomeUnknownError):
            async for _ in adapter.stream(LLMRequest(model="model", messages=[Message(role="user", content="hi")])):
                pass
    finally:
        await adapter.close()
    assert len(route.calls) == 1
    assert observer.attempts[0].outcomes == ["declined:provider_http_auth_rejected/v1"]


@pytest.mark.unit
@pytest.mark.parametrize("family", ["openai", "anthropic"])
@pytest.mark.parametrize("mode", ["invoke", "stream"])
@respx.mock
async def test_observed_http_429_keeps_rate_limit_failure(
    family: str,
    mode: str,
) -> None:
    base_url = _BASE if family == "openai" else _ANTHROPIC_BASE
    path = "/chat/completions" if family == "openai" else "/v1/messages"
    route = respx.post(f"{base_url}{path}").respond(
        429,
        json={"error": {"type": "rate_limit_error", "message": "Rate limit exceeded"}},
    )
    hook = _Hook()
    observer = _Observer()
    adapter = (
        OpenAICompatibleAdapter(base_url=base_url, api_key="test-key", provider_name="provider")
        if family == "openai"
        else AnthropicCompatibleAdapter(base_url=base_url, api_key="test-key", provider_name="provider")
    )
    try:
        with physical_invocation_scope(hook), physical_observation_scope(observer), pytest.raises(ProviderCallError) as raised:
            request = LLMRequest(model="model", messages=[Message(role="user", content="hi")])
            if mode == "invoke":
                await adapter.invoke(request)
            else:
                async for _ in adapter.stream(request):
                    pass
    finally:
        await adapter.close()

    assert raised.value.status_code == 429
    assert raised.value.kind is ProviderFailureKind.RATE_LIMITED
    assert len(route.calls) == 1
    assert hook.attempts[0].outcomes == ["not_applied:provider_http_rate_limited/v1"]
    assert observer.attempts[0].outcomes == ["not_applied:provider_http_rate_limited/v1"]


@pytest.mark.unit
@respx.mock
async def test_http_429_with_unconfirmed_settlement_stays_unknown() -> None:
    route = respx.post(f"{_BASE}/chat/completions").respond(429, json={"error": {"code": "rate_limit_exceeded"}})
    observer = _DecliningObserver()
    adapter = OpenAICompatibleAdapter(base_url=_BASE, api_key="test-key", provider_name="provider")
    try:
        with physical_observation_scope(observer), pytest.raises(InvocationOutcomeUnknownError):
            async for _ in adapter.stream(LLMRequest(model="model", messages=[Message(role="user", content="hi")])):
                pass
    finally:
        await adapter.close()
    assert len(route.calls) == 1
    assert observer.attempts[0].outcomes == ["declined:provider_http_rate_limited/v1"]


@pytest.mark.unit
def test_durable_chi_production_manifest_stays_empty_without_capture() -> None:
    assert frozenset() == _chi_sniff._DURABLE_REJECTION_MANIFEST_V1


@pytest.mark.unit
@respx.mock
async def test_anthropic_send_uses_same_physical_completion_contract() -> None:
    respx.post(f"{_ANTHROPIC_BASE}/v1/messages").respond(
        200,
        json=_anthropic_ok(),
    )
    hook = _Hook()
    adapter = AnthropicCompatibleAdapter(
        base_url=_ANTHROPIC_BASE,
        api_key="sk-test",
        provider_name="provider",
    )
    try:
        with physical_invocation_scope(hook):
            response = await adapter.invoke(
                LLMRequest(
                    model="model",
                    messages=[Message(role="user", content="hi")],
                )
            )
    finally:
        await adapter.close()

    assert response.content == "ok"
    assert hook.specs[0].adapter_contract_key.startswith("anthropic_compatible/")
    assert hook.attempts[0].outcomes[0].startswith("complete:4:3:")


@pytest.mark.unit
@respx.mock
async def test_chi_retry_owns_two_distinct_physical_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EMIT", "1")
    _register_durable_chi_fixture(monkeypatch)
    _chi_sniff._reset_for_test()
    _outbound.reset_learned_for_test()
    route = respx.post(f"{_BASE}/chat/completions").mock(
        side_effect=[
            httpx2.Response(
                400,
                json={
                    "error": {
                        "code": "unknown_parameter",
                        "message": "Unknown parameter: parallel_tool_calls",
                        "param": "parallel_tool_calls",
                    }
                },
            ),
            httpx2.Response(200, json=_ok()),
        ]
    )
    hook = _Hook()
    adapter = OpenAICompatibleAdapter(
        base_url=_BASE,
        api_key="sk-test",
        provider_name="provider",
    )
    try:
        with physical_invocation_scope(hook):
            response = await adapter.invoke(
                LLMRequest(
                    model="model",
                    messages=[Message(role="user", content="hi")],
                    tools=[_tool()],
                )
            )
    finally:
        await adapter.close()
        _chi_sniff._reset_for_test()
        _outbound.reset_learned_for_test()
    _outbound.reset_learned_for_test()

    assert response.content == "ok"
    assert len(route.calls) == 2
    assert len(hook.specs) == 2
    assert hook.specs[0].request_digest != hook.specs[1].request_digest
    assert hook.attempts[0].outcomes == [
        "not_applied:outbound_param_rejected/parallel_tool_calls/v1",
    ]
    assert hook.attempts[1].outcomes[0].startswith("complete:3:2:")


@pytest.mark.unit
@respx.mock
async def test_durable_chi_unregistered_transport_identity_never_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EMIT", "1")
    _chi_sniff._reset_for_test()
    _outbound.reset_learned_for_test()
    base_url = "https://compatible.example.com/v1"
    route = respx.post(f"{base_url}/chat/completions").respond(
        400,
        json={
            "error": {
                "code": "unknown_parameter",
                "message": "Unknown parameter: parallel_tool_calls",
                "param": "parallel_tool_calls",
            }
        },
    )
    hook = _Hook()
    adapter = OpenAICompatibleAdapter(
        base_url=base_url,
        api_key="sk-test",
        provider_name="provider",
    )
    try:
        with physical_invocation_scope(hook), pytest.raises(InvocationOutcomeUnknownError):
            await adapter.invoke(
                LLMRequest(
                    model="model",
                    messages=[Message(role="user", content="hi")],
                    tools=[_tool()],
                )
            )
    finally:
        await adapter.close()
        _chi_sniff._reset_for_test()
        _outbound.reset_learned_for_test()
    _outbound.reset_learned_for_test()

    assert len(route.calls) == 1
    assert hook.attempts[0].outcomes == ["unknown:provider_response_unclassified"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        "request completed but billing export unavailable: parallel_tool_calls",
        "rate limit while processing parallel_tool_calls",
        "upstream request completed; parallel_tool_calls is invalid in billing metadata",
    ],
)
@respx.mock
async def test_durable_chi_rejects_ambiguous_400_without_second_send(
    monkeypatch: pytest.MonkeyPatch,
    body: str,
) -> None:
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EMIT", "1")
    _chi_sniff._reset_for_test()
    _outbound.reset_learned_for_test()
    route = respx.post(f"{_BASE}/chat/completions").respond(400, text=body)
    hook = _Hook()
    adapter = OpenAICompatibleAdapter(
        base_url=_BASE,
        api_key="sk-test",
        provider_name="provider",
    )
    try:
        with physical_invocation_scope(hook), pytest.raises(InvocationOutcomeUnknownError):
            await adapter.invoke(
                LLMRequest(
                    model="model",
                    messages=[Message(role="user", content="hi")],
                    tools=[_tool()],
                )
            )
    finally:
        await adapter.close()
        _chi_sniff._reset_for_test()
        _outbound.reset_learned_for_test()
    _outbound.reset_learned_for_test()

    assert len(route.calls) == 1
    assert len(hook.attempts) == 1
    assert hook.attempts[0].outcomes == ["unknown:provider_response_unclassified"]


@pytest.mark.unit
@respx.mock
async def test_chi_stream_second_begin_failure_does_not_poison_settled_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EMIT", "1")
    _register_durable_chi_fixture(monkeypatch)
    _chi_sniff._reset_for_test()
    _outbound.reset_learned_for_test()
    route = respx.post(f"{_BASE}/chat/completions").respond(
        400,
        json={
            "error": {
                "code": "unknown_parameter",
                "message": "Unknown parameter: parallel_tool_calls",
                "param": "parallel_tool_calls",
            }
        },
    )
    hook = _SecondBeginFailsHook()
    adapter = OpenAICompatibleAdapter(
        base_url=_BASE,
        api_key="sk-test",
        provider_name="provider",
    )
    try:
        with physical_invocation_scope(hook), pytest.raises(RuntimeError, match="second begin failed"):
            async for _chunk in adapter.stream(
                LLMRequest(
                    model="model",
                    messages=[Message(role="user", content="hi")],
                    tools=[_tool()],
                )
            ):
                pass
    finally:
        await adapter.close()
        _chi_sniff._reset_for_test()
        _outbound.reset_learned_for_test()
    _outbound.reset_learned_for_test()

    assert len(route.calls) == 1
    assert hook.attempts[0].outcomes == [
        "not_applied:outbound_param_rejected/parallel_tool_calls/v1",
    ]


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["invoke", "stream"])
@respx.mock
async def test_legacy_plain_text_chi_rejection_still_retries_without_flag(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EMIT", "1")
    _chi_sniff._reset_for_test()
    _outbound.reset_learned_for_test()
    stream_ok = httpx2.Response(
        200,
        content=(b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'),
        headers={"content-type": "text/event-stream"},
    )
    route = respx.post(f"{_BASE}/chat/completions").mock(
        side_effect=[
            httpx2.Response(
                400,
                text="Unknown parameter: parallel_tool_calls",
            ),
            httpx2.Response(200, json=_ok()) if mode == "invoke" else stream_ok,
        ]
    )
    adapter = OpenAICompatibleAdapter(
        base_url=_BASE,
        api_key="sk-test",
        provider_name="provider",
    )
    try:
        request = LLMRequest(
            model="model",
            messages=[Message(role="user", content="hi")],
            tools=[_tool()],
        )
        if mode == "invoke":
            await adapter.invoke(request)
        else:
            async for _chunk in adapter.stream(request):
                pass
    finally:
        await adapter.close()
        _chi_sniff._reset_for_test()
        _outbound.reset_learned_for_test()
    _outbound.reset_learned_for_test()

    assert len(route.calls) == 2
    first = json.loads(route.calls[0].request.content)
    second = json.loads(route.calls[1].request.content)
    assert first["parallel_tool_calls"] is True
    assert "parallel_tool_calls" not in second


@pytest.mark.unit
@respx.mock
async def test_chi_stream_second_begin_cancellation_does_not_poison_settled_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EMIT", "1")
    _register_durable_chi_fixture(monkeypatch)
    _chi_sniff._reset_for_test()
    _outbound.reset_learned_for_test()
    route = respx.post(f"{_BASE}/chat/completions").respond(
        400,
        json={
            "error": {
                "code": "unknown_parameter",
                "message": "Unknown parameter: parallel_tool_calls",
                "param": "parallel_tool_calls",
            }
        },
    )
    second_begin_started = asyncio.Event()
    hook = _SecondBeginFailsHook(second_begin_started=second_begin_started)
    adapter = OpenAICompatibleAdapter(
        base_url=_BASE,
        api_key="sk-test",
        provider_name="provider",
    )

    async def consume() -> None:
        async for _chunk in adapter.stream(
            LLMRequest(
                model="model",
                messages=[Message(role="user", content="hi")],
                tools=[_tool()],
            )
        ):
            pass

    try:
        with physical_invocation_scope(hook):
            task = asyncio.create_task(consume())
            await second_begin_started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        await adapter.close()
        _chi_sniff._reset_for_test()
        _outbound.reset_learned_for_test()
    _outbound.reset_learned_for_test()

    assert len(route.calls) == 1
    assert hook.attempts[0].outcomes == [
        "not_applied:outbound_param_rejected/parallel_tool_calls/v1",
    ]


@pytest.mark.unit
@respx.mock
async def test_missing_authoritative_usage_becomes_unknown() -> None:
    respx.post(f"{_BASE}/chat/completions").respond(
        200,
        json={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        },
    )
    hook = _Hook()
    adapter = OpenAICompatibleAdapter(
        base_url=_BASE,
        api_key="sk-test",
        provider_name="provider",
    )
    try:
        with physical_invocation_scope(hook), pytest.raises(InvocationOutcomeUnknownError):
            await adapter.invoke(
                LLMRequest(
                    model="model",
                    messages=[Message(role="user", content="hi")],
                )
            )
    finally:
        await adapter.close()
    assert len(hook.attempts) == 1
    assert hook.attempts[0].outcomes == ["unknown:authoritative_usage_missing"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad_count",
    [None, True, "3", 3.0],
    ids=["null", "bool", "string", "float"],
)
@respx.mock
async def test_openai_invoke_rejects_non_exact_authoritative_usage(
    bad_count: object,
) -> None:
    respx.post(f"{_BASE}/chat/completions").respond(
        200,
        json={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": bad_count, "completion_tokens": 2},
        },
    )
    hook = _Hook()
    adapter = OpenAICompatibleAdapter(
        base_url=_BASE,
        api_key="sk-test",
        provider_name="provider",
    )
    try:
        with physical_invocation_scope(hook), pytest.raises(InvocationOutcomeUnknownError):
            await adapter.invoke(
                LLMRequest(
                    model="model",
                    messages=[Message(role="user", content="hi")],
                )
            )
    finally:
        await adapter.close()

    assert hook.attempts[0].outcomes == ["unknown:authoritative_usage_missing"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad_count",
    [None, True, "3", 3.0],
    ids=["null", "bool", "string", "float"],
)
@respx.mock
async def test_openai_stream_rejects_non_exact_authoritative_usage(
    bad_count: object,
) -> None:
    frame = json.dumps(
        {
            "choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": bad_count, "completion_tokens": 2},
        },
        separators=(",", ":"),
    )
    respx.post(f"{_BASE}/chat/completions").respond(
        200,
        content=f"data: {frame}\n\ndata: [DONE]\n\n".encode(),
        headers={"content-type": "text/event-stream"},
    )
    hook = _Hook()
    adapter = OpenAICompatibleAdapter(
        base_url=_BASE,
        api_key="sk-test",
        provider_name="provider",
    )
    try:
        with physical_invocation_scope(hook), pytest.raises(InvocationOutcomeUnknownError):
            async for _chunk in adapter.stream(
                LLMRequest(
                    model="model",
                    messages=[Message(role="user", content="hi")],
                )
            ):
                pass
    finally:
        await adapter.close()

    assert hook.attempts[0].outcomes == ["unknown:authoritative_usage_missing"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad_count",
    [None, True, "4", 4.0],
    ids=["null", "bool", "string", "float"],
)
@respx.mock
async def test_anthropic_invoke_rejects_non_exact_authoritative_usage(
    bad_count: object,
) -> None:
    respx.post(f"{_ANTHROPIC_BASE}/v1/messages").respond(
        200,
        json={
            "content": [{"type": "text", "text": "ok"}],
            "model": "model",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": bad_count, "output_tokens": 3},
        },
    )
    hook = _Hook()
    adapter = AnthropicCompatibleAdapter(
        base_url=_ANTHROPIC_BASE,
        api_key="sk-test",
        provider_name="provider",
    )
    try:
        with physical_invocation_scope(hook), pytest.raises(InvocationOutcomeUnknownError):
            await adapter.invoke(
                LLMRequest(
                    model="model",
                    messages=[Message(role="user", content="hi")],
                )
            )
    finally:
        await adapter.close()

    assert hook.attempts[0].outcomes == ["unknown:authoritative_usage_missing"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad_count",
    [None, True, "4", 4.0],
    ids=["null", "bool", "string", "float"],
)
@respx.mock
async def test_anthropic_stream_rejects_non_exact_authoritative_usage(
    bad_count: object,
) -> None:
    start = json.dumps(
        {
            "type": "message_start",
            "message": {"usage": {"input_tokens": bad_count}},
        },
        separators=(",", ":"),
    )
    respx.post(f"{_ANTHROPIC_BASE}/v1/messages").respond(
        200,
        content=(
            f"event: message_start\ndata: {start}\n\n".encode()
            + b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}\n\n'
            + b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        ),
        headers={"content-type": "text/event-stream"},
    )
    hook = _Hook()
    adapter = AnthropicCompatibleAdapter(
        base_url=_ANTHROPIC_BASE,
        api_key="sk-test",
        provider_name="provider",
    )
    try:
        with physical_invocation_scope(hook), pytest.raises(InvocationOutcomeUnknownError):
            async for _chunk in adapter.stream(
                LLMRequest(
                    model="model",
                    messages=[Message(role="user", content="hi")],
                )
            ):
                pass
    finally:
        await adapter.close()

    assert hook.attempts[0].outcomes == ["unknown:authoritative_usage_missing"]


@pytest.mark.unit
@respx.mock
async def test_openai_durable_stream_rejects_malformed_frame_before_terminal() -> None:
    respx.post(f"{_BASE}/chat/completions").respond(
        200,
        content=(
            b"data: not-json\n\n"
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":2,"completion_tokens":1}}\n\n'
            b"data: [DONE]\n\n"
        ),
        headers={"content-type": "text/event-stream"},
    )
    hook = _Hook()
    adapter = OpenAICompatibleAdapter(
        base_url=_BASE,
        api_key="sk-test",
        provider_name="provider",
    )
    try:
        with physical_invocation_scope(hook), pytest.raises(InvocationOutcomeUnknownError):
            async for _chunk in adapter.stream(LLMRequest(model="model", messages=[Message(role="user", content="hi")])):
                pass
    finally:
        await adapter.close()

    assert hook.attempts[0].outcomes == ["unknown:provider_response_unclassified"]


@pytest.mark.unit
@respx.mock
async def test_anthropic_durable_stream_rejects_malformed_frame_before_terminal() -> None:
    respx.post(f"{_ANTHROPIC_BASE}/v1/messages").respond(
        200,
        content=(
            b"event: message_start\ndata: not-json\n\n"
            b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"input_tokens":4,"output_tokens":3}}\n\n'
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        ),
        headers={"content-type": "text/event-stream"},
    )
    hook = _Hook()
    adapter = AnthropicCompatibleAdapter(
        base_url=_ANTHROPIC_BASE,
        api_key="sk-test",
        provider_name="provider",
    )
    try:
        with physical_invocation_scope(hook), pytest.raises(InvocationOutcomeUnknownError):
            async for _chunk in adapter.stream(LLMRequest(model="model", messages=[Message(role="user", content="hi")])):
                pass
    finally:
        await adapter.close()

    assert hook.attempts[0].outcomes == ["unknown:provider_response_unclassified"]


@pytest.mark.unit
@respx.mock
async def test_transport_failure_stays_unknown_when_durable_unknown_write_fails() -> None:
    # A failure after the request was sent; a no-output timeout is settled instead.
    route = respx.post(f"{_BASE}/chat/completions").mock(
        side_effect=httpx2.RemoteProtocolError("connection dropped"),
    )
    hook = _FailingUnknownHook()
    adapter = OpenAICompatibleAdapter(
        base_url=_BASE,
        api_key="sk-test",
        provider_name="provider",
    )
    try:
        with (
            physical_invocation_scope(hook),
            pytest.raises(
                InvocationOutcomeUnknownError,
            ) as raised,
        ):
            await adapter.invoke(
                LLMRequest(
                    model="model",
                    messages=[Message(role="user", content="hi")],
                )
            )
    finally:
        await adapter.close()

    assert raised.value.reason_code == "transport_network_error"
    assert len(route.calls) == 1
    assert hook.attempts[0].outcomes == ["unknown_failed:transport_network_error"]


@pytest.mark.unit
@respx.mock
async def test_unclassified_invoke_transport_failure_never_retries_or_falls_back() -> None:
    route = respx.post(f"{_BASE}/chat/completions").mock(
        side_effect=RuntimeError("custom transport failed after dispatch"),
    )
    hook = _Hook()
    primary = OpenAICompatibleAdapter(
        base_url=_BASE,
        api_key="sk-test",
        provider_name="primary",
    )
    fallback = _UnknownAdapter()
    router = Router(_Registry({"primary": primary, "fallback": fallback}))  # type: ignore[arg-type]
    router.bind(
        AgentBinding(
            agent_type="lead",
            primary=("primary", "model"),
            fallback=[("fallback", "other")],
        )
    )
    try:
        with (
            physical_invocation_scope(hook),
            pytest.raises(
                InvocationOutcomeUnknownError,
            ) as raised,
        ):
            await router.invoke(
                "lead",
                LLMRequest(model="ignored", messages=[]),
            )
    finally:
        await primary.close()

    assert raised.value.reason_code == "transport_unclassified_error"
    assert len(route.calls) == 1
    assert fallback.calls == 0
    assert hook.attempts[0].outcomes == ["unknown:transport_unclassified_error"]


@pytest.mark.unit
@respx.mock
async def test_chi_second_unclassified_transport_failure_never_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EMIT", "1")
    _register_durable_chi_fixture(monkeypatch)
    _chi_sniff._reset_for_test()
    _outbound.reset_learned_for_test()
    route = respx.post(f"{_BASE}/chat/completions").mock(
        side_effect=[
            httpx2.Response(
                400,
                json={
                    "error": {
                        "code": "unknown_parameter",
                        "message": "Unknown parameter: parallel_tool_calls",
                        "param": "parallel_tool_calls",
                    }
                },
            ),
            RuntimeError("custom transport failed after second dispatch"),
        ]
    )
    hook = _Hook()
    primary = OpenAICompatibleAdapter(
        base_url=_BASE,
        api_key="sk-test",
        provider_name="primary",
    )
    fallback = _UnknownAdapter()
    router = Router(_Registry({"primary": primary, "fallback": fallback}))  # type: ignore[arg-type]
    router.bind(
        AgentBinding(
            agent_type="lead",
            primary=("primary", "model"),
            fallback=[("fallback", "other")],
        )
    )
    try:
        with (
            physical_invocation_scope(hook),
            pytest.raises(
                InvocationOutcomeUnknownError,
            ) as raised,
        ):
            await router.invoke(
                "lead",
                LLMRequest(
                    model="ignored",
                    messages=[],
                    tools=[_tool()],
                ),
            )
    finally:
        await primary.close()
        _chi_sniff._reset_for_test()
        _outbound.reset_learned_for_test()
    _outbound.reset_learned_for_test()

    assert raised.value.reason_code == "transport_unclassified_error"
    assert len(route.calls) == 2
    assert fallback.calls == 0
    assert hook.attempts[0].outcomes == [
        "not_applied:outbound_param_rejected/parallel_tool_calls/v1",
    ]
    assert hook.attempts[1].outcomes == [
        "unknown:transport_unclassified_error",
    ]


@pytest.mark.unit
@respx.mock
async def test_anthropic_unclassified_transport_failure_never_retries_or_falls_back() -> None:
    route = respx.post(f"{_ANTHROPIC_BASE}/v1/messages").mock(
        side_effect=RuntimeError("custom anthropic transport failed"),
    )
    hook = _Hook()
    primary = AnthropicCompatibleAdapter(
        base_url=_ANTHROPIC_BASE,
        api_key="sk-test",
        provider_name="primary",
    )
    fallback = _UnknownAdapter()
    router = Router(_Registry({"primary": primary, "fallback": fallback}))  # type: ignore[arg-type]
    router.bind(
        AgentBinding(
            agent_type="lead",
            primary=("primary", "model"),
            fallback=[("fallback", "other")],
        )
    )
    try:
        with (
            physical_invocation_scope(hook),
            pytest.raises(
                InvocationOutcomeUnknownError,
            ) as raised,
        ):
            await router.invoke(
                "lead",
                LLMRequest(model="ignored", messages=[]),
            )
    finally:
        await primary.close()

    assert raised.value.reason_code == "transport_unclassified_error"
    assert len(route.calls) == 1
    assert fallback.calls == 0
    assert hook.attempts[0].outcomes == [
        "unknown:transport_unclassified_error",
    ]


@pytest.mark.unit
@respx.mock
async def test_cancelled_transport_finishes_unknown_handoff_before_propagating() -> None:
    entered = asyncio.Event()
    never = asyncio.Event()

    async def block_transport(_request: httpx2.Request) -> httpx2.Response:
        entered.set()
        await never.wait()
        raise AssertionError("unreachable")

    respx.post(f"{_BASE}/chat/completions").mock(
        side_effect=block_transport,
    )
    hook = _Hook()
    adapter = OpenAICompatibleAdapter(
        base_url=_BASE,
        api_key="sk-test",
        provider_name="provider",
    )
    try:
        with physical_invocation_scope(hook):
            task = asyncio.create_task(
                adapter.invoke(
                    LLMRequest(
                        model="model",
                        messages=[Message(role="user", content="hi")],
                    )
                )
            )
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        await adapter.close()

    assert len(hook.attempts) == 1
    assert hook.attempts[0].outcomes == ["unknown:transport_cancelled"]


@pytest.mark.unit
@respx.mock
async def test_cancelled_completion_poison_handoff_precedes_propagation() -> None:
    respx.post(f"{_BASE}/chat/completions").respond(200, json=_ok())
    hook = _BlockingCompleteHook()
    adapter = OpenAICompatibleAdapter(
        base_url=_BASE,
        api_key="sk-test",
        provider_name="provider",
    )
    try:
        with physical_invocation_scope(hook):
            task = asyncio.create_task(
                adapter.invoke(
                    LLMRequest(
                        model="model",
                        messages=[Message(role="user", content="hi")],
                    )
                )
            )
            await hook.started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        await adapter.close()

    assert hook.attempts[0].outcomes == [
        "unknown:durable_settlement_cancelled",
    ]


@pytest.mark.unit
async def test_cancel_during_failed_settlement_still_completes_unknown_handoff() -> None:
    unknown_started = asyncio.Event()
    unknown_release = asyncio.Event()
    attempt = _FailsThenBlockingUnknownAttempt(
        [],
        unknown_started,
        unknown_release,
    )
    task = asyncio.create_task(
        complete_physical_invocation(
            attempt,
            usage=TokenUsage(input_tokens=3, output_tokens=2),
            response={"id": "safe-id"},
        )
    )

    await unknown_started.wait()
    task.cancel()
    unknown_release.set()
    with pytest.raises(InvocationOutcomeUnknownError) as failed:
        await task

    assert failed.value.reason_code == "durable_settlement_failed"
    assert attempt.outcomes == ["unknown:durable_settlement_failed"]


@pytest.mark.unit
@respx.mock
async def test_chi_second_transport_timeout_stops_after_two_physical_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EMIT", "1")
    _register_durable_chi_fixture(monkeypatch)
    _chi_sniff._reset_for_test()
    _outbound.reset_learned_for_test()
    route = respx.post(f"{_BASE}/chat/completions").mock(
        side_effect=[
            httpx2.Response(
                400,
                json={
                    "error": {
                        "code": "unknown_parameter",
                        "message": "Unknown parameter: parallel_tool_calls",
                        "param": "parallel_tool_calls",
                    }
                },
            ),
            httpx2.ReadTimeout("timeout"),
        ]
    )
    hook = _Hook()
    adapter = OpenAICompatibleAdapter(
        base_url=_BASE,
        api_key="sk-test",
        provider_name="provider",
    )
    try:
        with physical_invocation_scope(hook), pytest.raises(ProviderCallError) as raised:
            await adapter.invoke(
                LLMRequest(
                    model="model",
                    messages=[Message(role="user", content="hi")],
                    tools=[_tool()],
                )
            )
    finally:
        await adapter.close()
        _chi_sniff._reset_for_test()
        _outbound.reset_learned_for_test()
    _outbound.reset_learned_for_test()
    # The adapter stops after two sends; the no-output timeout is left to Router retry.
    assert raised.value.kind is ProviderFailureKind.TIMEOUT
    assert len(route.calls) == 2
    assert len(hook.attempts) == 2
    assert hook.attempts[0].outcomes[0].startswith("not_applied:")
    assert hook.attempts[1].outcomes == ["not_applied:provider_no_response_timeout/v1"]


@pytest.mark.unit
@respx.mock
async def test_stream_consumer_close_marks_physical_attempt_unknown() -> None:
    respx.post(f"{_BASE}/chat/completions").respond(
        200,
        content=(
            b'data: {"choices":[{"delta":{"content":"first"},"finish_reason":null}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"second"},"finish_reason":"stop"}],'
            b'"usage":{"prompt_tokens":2,"completion_tokens":2}}\n\n'
            b"data: [DONE]\n\n"
        ),
        headers={"content-type": "text/event-stream"},
    )
    hook = _Hook()
    adapter = OpenAICompatibleAdapter(
        base_url=_BASE,
        api_key="sk-test",
        provider_name="provider",
    )
    try:
        with physical_invocation_scope(hook):
            stream = adapter.stream(
                LLMRequest(
                    model="model",
                    messages=[Message(role="user", content="hi")],
                )
            )
            assert (await anext(stream)).delta == "first"
            await stream.aclose()
    finally:
        await adapter.close()
    assert hook.attempts[0].outcomes == ["unknown:stream_consumer_closed"]


@pytest.mark.unit
@respx.mock
async def test_router_consumer_close_reaches_physical_stream_cleanup() -> None:
    respx.post(f"{_BASE}/chat/completions").respond(
        200,
        content=(
            b'data: {"choices":[{"delta":{"content":"first"},"finish_reason":null}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"second"},"finish_reason":"stop"}],'
            b'"usage":{"prompt_tokens":2,"completion_tokens":2}}\n\n'
            b"data: [DONE]\n\n"
        ),
        headers={"content-type": "text/event-stream"},
    )
    hook = _Hook()
    adapter = OpenAICompatibleAdapter(
        base_url=_BASE,
        api_key="sk-test",
        provider_name="provider",
    )
    router = Router(_Registry({"provider": adapter}))  # type: ignore[arg-type]
    router.bind(
        AgentBinding(
            agent_type="lead",
            primary=("provider", "model"),
        )
    )
    try:
        with physical_invocation_scope(hook):
            stream = router.stream(
                "lead",
                LLMRequest(
                    model="ignored",
                    messages=[Message(role="user", content="hi")],
                ),
            )
            assert (await anext(stream)).delta == "first"
            await stream.aclose()
    finally:
        await adapter.close()

    assert hook.attempts[0].outcomes == ["unknown:stream_consumer_closed"]


@pytest.mark.unit
@pytest.mark.parametrize("base_url", [_BASE, "https://configured.example.com/v1"])
@respx.mock
async def test_stream_incomplete_authoritative_usage_becomes_unknown(base_url: str) -> None:
    route = respx.post(f"{base_url}/chat/completions").respond(
        200,
        content=(
            b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":2}}\n\n'
            b"data: [DONE]\n\n"
        ),
        headers={"content-type": "text/event-stream"},
    )
    hook = _Hook()
    adapter = OpenAICompatibleAdapter(
        base_url=base_url,
        api_key="sk-test",
        provider_name="provider",
    )
    try:
        with (
            physical_invocation_scope(hook),
            pytest.raises(
                InvocationOutcomeUnknownError,
            ),
        ):
            async for _chunk in adapter.stream(
                LLMRequest(
                    model="model",
                    messages=[Message(role="user", content="hi")],
                )
            ):
                pass
    finally:
        await adapter.close()

    assert len(route.calls) == 1
    assert hook.attempts[0].outcomes == ["unknown:authoritative_usage_missing"]


@pytest.mark.unit
@respx.mock
async def test_legacy_openai_stream_request_and_usage_projection_stay_unchanged() -> None:
    route = respx.post(f"{_BASE}/chat/completions").respond(
        200,
        content=(
            b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}],'
            b'"usage":{"prompt_tokens":2,"completion_tokens":1}}\n\n'
            b"data: [DONE]\n\n"
        ),
        headers={"content-type": "text/event-stream"},
    )
    adapter = OpenAICompatibleAdapter(
        base_url=_BASE,
        api_key="sk-test",
        provider_name="provider",
    )
    try:
        chunks = [
            chunk
            async for chunk in adapter.stream(
                LLMRequest(
                    model="model",
                    messages=[Message(role="user", content="hi")],
                )
            )
        ]
    finally:
        await adapter.close()

    sent = __import__("json").loads(route.calls[0].request.content)
    assert "stream_options" not in sent
    assert chunks[-1].usage is None


@pytest.mark.unit
@respx.mock
async def test_durable_openai_stream_requests_usage_and_binds_output() -> None:
    route = respx.post(f"{_BASE}/chat/completions").mock(
        side_effect=[
            httpx2.Response(
                200,
                content=(
                    b'data: {"id":"r1","choices":[{"delta":{"content":"alpha"},"finish_reason":"stop"}],'
                    b'"usage":{"prompt_tokens":2,"completion_tokens":1}}\n\n'
                    b"data: [DONE]\n\n"
                ),
                headers={"content-type": "text/event-stream"},
            ),
            httpx2.Response(
                200,
                content=(
                    b'data: {"id":"r2","choices":[{"delta":{"content":"beta"},"finish_reason":"stop"}],'
                    b'"usage":{"prompt_tokens":2,"completion_tokens":1}}\n\n'
                    b"data: [DONE]\n\n"
                ),
                headers={"content-type": "text/event-stream"},
            ),
        ],
    )
    hook = _Hook()
    adapter = OpenAICompatibleAdapter(
        base_url=_BASE,
        api_key="sk-test",
        provider_name="provider",
    )
    try:
        with physical_invocation_scope(hook):
            for _ in range(2):
                async for _chunk in adapter.stream(
                    LLMRequest(
                        model="model",
                        messages=[Message(role="user", content="hi")],
                    )
                ):
                    pass
    finally:
        await adapter.close()

    for call in route.calls:
        sent = __import__("json").loads(call.request.content)
        assert sent["stream_options"] == {"include_usage": True}
    assert len(hook.attempts) == 2
    assert hook.attempts[0].outcomes[0].startswith("complete:2:1:")
    assert hook.attempts[1].outcomes[0].startswith("complete:2:1:")
    assert hook.attempts[0].outcomes != hook.attempts[1].outcomes


@pytest.mark.unit
@pytest.mark.parametrize(
    "base_url, adapter_type",
    [
        ("https://compatible.example.com/v1", "openai_compatible"),
        ("http://127.0.0.1:8181/v1", "openai_compatible"),
        ("https://cloud.example.com", "azure_openai"),
        ("https://custom.example.com", "custom_http"),
    ],
)
@respx.mock
async def test_durable_configured_endpoint_streams_tools_and_settles_usage(base_url: str, adapter_type: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    endpoint = (
        f"{base_url}/openai/deployments/model/chat/completions?api-version=2024-10-21"
        if adapter_type == "azure_openai"
        else f"{base_url}/chat/completions"
    )
    route = respx.post(endpoint).respond(
        200,
        content=(
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"echo","arguments":"{}"}}]},"finish_reason":"tool_calls"}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":1}}\n\n'
            b"data: [DONE]\n\n"
        ),
        headers={"content-type": "text/event-stream"},
    )
    hook = _Hook()
    options = {"endpoint": base_url, "api_version": "2024-10-21"} if adapter_type == "azure_openai" else {}
    if adapter_type == "custom_http":
        options = {
            "transports": {
                "chat": {
                    "contract": "openai_chat_completions_sse",
                    "request": {
                        "url_template": "{base_url}/chat/completions",
                        "body_template": {"model": {"$bowhead": "model"}, "messages": {"$bowhead": "messages"}, "stream": {"$bowhead": "stream"}},
                    },
                }
            },
            "models": [{"id": "model", "capabilities": ["chat"], "capability_transports": {"chat": "chat"}}],
        }
    adapter = OpenAICompatibleAdapter(
        base_url=base_url,
        api_key="sk-test",
        provider_name="provider",
        adapter_type=adapter_type,
        provider_options=options,
    )
    try:
        with physical_invocation_scope(hook):
            chunks = [chunk async for chunk in adapter.stream(LLMRequest(model="model", messages=[Message(role="user", content="hi")]))]
    finally:
        await adapter.close()

    assert len(route.calls) == 1
    assert json.loads(route.calls[0].request.content)["stream_options"] == {"include_usage": True}
    assert [chunk.tool_call_delta.name for chunk in chunks if chunk.tool_call_delta] == ["echo"]
    assert hook.specs[0].mode == "stream"
    assert hook.attempts[0].outcomes[0].startswith("complete:2:1:")


@pytest.mark.unit
@respx.mock
async def test_anthropic_stream_accepts_exact_usage_in_delta_and_binds_output() -> None:
    def response(text: str) -> httpx2.Response:
        return httpx2.Response(
            200,
            content=(
                b'event: message_start\ndata: {"type":"message_start","message":{"usage":{}}}\n\n'
                + f'event: content_block_delta\ndata: {{"type":"content_block_delta","index":0,"delta":{{"type":"text_delta","text":"{text}"}}}}\n\n'.encode()
                + b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"input_tokens":4,"output_tokens":3}}\n\n'
                + b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
            ),
            headers={"content-type": "text/event-stream"},
        )

    respx.post(f"{_ANTHROPIC_BASE}/v1/messages").mock(
        side_effect=[response("alpha"), response("beta")],
    )
    hook = _Hook()
    adapter = AnthropicCompatibleAdapter(
        base_url=_ANTHROPIC_BASE,
        api_key="sk-test",
        provider_name="provider",
    )
    try:
        with physical_invocation_scope(hook):
            for _ in range(2):
                async for _chunk in adapter.stream(
                    LLMRequest(
                        model="model",
                        messages=[Message(role="user", content="hi")],
                    )
                ):
                    pass
    finally:
        await adapter.close()

    assert hook.attempts[0].outcomes[0].startswith("complete:4:3:")
    assert hook.attempts[1].outcomes[0].startswith("complete:4:3:")
    assert hook.attempts[0].outcomes != hook.attempts[1].outcomes


class _Registry:
    def __init__(self, adapters: dict[str, object]) -> None:
        self.adapters = adapters

    def has(self, name: str) -> bool:
        return name in self.adapters

    def get(self, name: str) -> object:
        return self.adapters[name]


class _UnknownAdapter:
    def __init__(self) -> None:
        self.calls = 0

    async def invoke(self, _request: LLMRequest) -> LLMResponse:
        self.calls += 1
        raise InvocationOutcomeUnknownError()

    async def stream(self, _request: LLMRequest):
        self.calls += 1
        raise InvocationOutcomeUnknownError()
        yield  # pragma: no cover


class _TrackingStream:
    def __init__(self, *, block: bool) -> None:
        self.block = block
        self.close_calls = 0

    def __aiter__(self) -> _TrackingStream:
        return self

    async def __anext__(self) -> LLMChunk:
        if self.block:
            await asyncio.Event().wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_calls += 1


class _TrackingStreamAdapter:
    def __init__(self, *, block: bool) -> None:
        self.calls = 0
        self.iterator = _TrackingStream(block=block)

    def stream(self, _request: LLMRequest) -> _TrackingStream:
        self.calls += 1
        return self.iterator


@pytest.mark.unit
async def test_unknown_sentinel_bypasses_retry_and_fallback() -> None:
    primary = _UnknownAdapter()
    fallback = _UnknownAdapter()
    router = Router(_Registry({"primary": primary, "fallback": fallback}))  # type: ignore[arg-type]
    router.bind(
        AgentBinding(
            agent_type="lead",
            primary=("primary", "a"),
            fallback=[("fallback", "b")],
        )
    )

    with pytest.raises(InvocationOutcomeUnknownError):
        await router.invoke(
            "lead",
            LLMRequest(model="ignored", messages=[]),
        )
    assert primary.calls == 1
    assert fallback.calls == 0


@pytest.mark.unit
async def test_unknown_stream_sentinel_bypasses_retry_and_fallback() -> None:
    primary = _UnknownAdapter()
    fallback = _UnknownAdapter()
    router = Router(_Registry({"primary": primary, "fallback": fallback}))  # type: ignore[arg-type]
    router.bind(
        AgentBinding(
            agent_type="lead",
            primary=("primary", "a"),
            fallback=[("fallback", "b")],
        )
    )

    with pytest.raises(InvocationOutcomeUnknownError):
        async for _chunk in router.stream(
            "lead",
            LLMRequest(model="ignored", messages=[]),
        ):
            pass
    assert primary.calls == 1
    assert fallback.calls == 0


@pytest.mark.unit
async def test_durable_stream_idle_timeout_bypasses_retry_and_fallback() -> None:
    primary = _TrackingStreamAdapter(block=True)
    fallback = _TrackingStreamAdapter(block=False)
    router = Router(_Registry({"primary": primary, "fallback": fallback}))  # type: ignore[arg-type]
    router.bind(
        AgentBinding(
            agent_type="lead",
            primary=("primary", "a"),
            fallback=[("fallback", "b")],
        )
    )

    with (
        physical_invocation_scope(_Hook()),
        pytest.raises(
            InvocationOutcomeUnknownError,
        ) as raised,
    ):
        async for _chunk in router.stream(
            "lead",
            LLMRequest(model="ignored", messages=[]),
            idle_timeout_s=0.001,
        ):
            pass

    assert primary.calls == 1
    assert primary.iterator.close_calls == 1
    assert fallback.calls == 0
    assert raised.value.reason_code == "durable_stream_idle_timeout"


@pytest.mark.unit
@pytest.mark.parametrize("durable", [False, True])
async def test_router_only_closes_adapter_iterator_for_durable_stream(
    durable: bool,
) -> None:
    adapter = _TrackingStreamAdapter(block=False)
    router = Router(_Registry({"provider": adapter}))  # type: ignore[arg-type]
    router.bind(
        AgentBinding(
            agent_type="lead",
            primary=("provider", "model"),
        )
    )

    if durable:
        with physical_invocation_scope(_Hook()):
            async for _chunk in router.stream(
                "lead",
                LLMRequest(model="ignored", messages=[]),
            ):
                pass
    else:
        async for _chunk in router.stream(
            "lead",
            LLMRequest(model="ignored", messages=[]),
        ):
            pass

    assert adapter.calls == 1
    assert adapter.iterator.close_calls == int(durable)


class _StallAfterFirstLine(httpx2.AsyncByteStream):
    """A stream that yields one content frame, then times out."""

    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"Partial"},"finish_reason":null}]}\n\n'
        raise httpx2.ReadTimeout("stalled after output")


@pytest.mark.unit
@pytest.mark.parametrize("family", ["openai", "anthropic"])
@pytest.mark.parametrize("mode", ["invoke", "stream"])
@pytest.mark.parametrize("failure", [httpx2.ReadTimeout("no response"), httpx2.ConnectError("refused")])
@respx.mock
async def test_no_output_transport_failure_is_settled_not_applied(family: str, mode: str, failure: Exception) -> None:
    base_url = _BASE if family == "openai" else _ANTHROPIC_BASE
    path = "/chat/completions" if family == "openai" else "/v1/messages"
    route = respx.post(f"{base_url}{path}").mock(side_effect=failure)
    hook = _Hook()
    observer = _Observer()
    adapter = (
        OpenAICompatibleAdapter(base_url=base_url, api_key="test-key", provider_name="provider")
        if family == "openai"
        else AnthropicCompatibleAdapter(base_url=base_url, api_key="test-key", provider_name="provider")
    )
    try:
        with physical_invocation_scope(hook), physical_observation_scope(observer), pytest.raises(ProviderCallError) as raised:
            request = LLMRequest(model="model", messages=[Message(role="user", content="hi")])
            if mode == "invoke":
                await adapter.invoke(request)
            else:
                async for _ in adapter.stream(request):
                    pass
    finally:
        await adapter.close()

    timed_out = isinstance(failure, httpx2.TimeoutException)
    assert raised.value.kind is (ProviderFailureKind.TIMEOUT if timed_out else ProviderFailureKind.NETWORK)
    assert raised.value.retryable is True
    assert len(route.calls) == 1
    assert hook.attempts[0].outcomes == ["not_applied:provider_no_response_timeout/v1"]
    assert observer.attempts[0].outcomes == ["not_applied:provider_no_response_timeout/v1"]


@pytest.mark.unit
@respx.mock
async def test_timeout_after_streamed_output_stays_unknown() -> None:
    route = respx.post(f"{_BASE}/chat/completions").mock(
        return_value=httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=_StallAfterFirstLine())
    )
    hook = _Hook()
    adapter = OpenAICompatibleAdapter(base_url=_BASE, api_key="test-key", provider_name="provider")
    seen = []
    try:
        with physical_invocation_scope(hook), pytest.raises(InvocationOutcomeUnknownError):
            async for chunk in adapter.stream(LLMRequest(model="model", messages=[Message(role="user", content="hi")])):
                seen.append(chunk.delta)
    finally:
        await adapter.close()
    assert seen == ["Partial"]
    assert len(route.calls) == 1
    assert hook.attempts[0].outcomes == ["unknown:transport_timeout"]


@pytest.mark.unit
@pytest.mark.parametrize("admitted", [True, False])
@respx.mock
async def test_router_retries_no_output_timeout_only_when_admitted(monkeypatch, admitted: bool) -> None:
    from bowhead_router.errors import StreamingTargetsExhaustedError
    from bowhead_router.registry import ProviderConfig, ProviderRegistry
    from bowhead_router.retry import retry_observer_scope

    async def no_sleep(delay):
        return None

    monkeypatch.setattr("bowhead_router.router.asyncio.sleep", no_sleep)
    ok = (
        'data: {"choices":[{"delta":{"content":"Done"},"finish_reason":"stop"}]}\n\n'
        'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":1}}\n\n'
        "data: [DONE]\n\n"
    )
    route = respx.post(f"{_BASE}/chat/completions").mock(
        side_effect=[httpx2.ReadTimeout("no response"), httpx2.Response(200, headers={"content-type": "text/event-stream"}, text=ok)]
    )
    registry = ProviderRegistry()
    registry.load([ProviderConfig(name="provider", type="openai_compatible", base_url=_BASE, api_key="test-key")])
    router = Router(registry)
    router.bind(AgentBinding(agent_type="lead", primary=("provider", "model")))
    router.max_retries = 3
    notices = []

    class Gate:
        async def admit_retry(self, notice) -> bool:
            notices.append(notice)
            return admitted

    hook = _Hook()
    text = []
    try:
        with physical_invocation_scope(hook), retry_observer_scope(Gate()):
            if admitted:
                async for chunk in router.stream("lead", LLMRequest(model="model", messages=[Message(role="user", content="hi")])):
                    text.append(chunk.delta)
            else:
                with pytest.raises(StreamingTargetsExhaustedError) as raised:
                    async for _ in router.stream("lead", LLMRequest(model="model", messages=[Message(role="user", content="hi")])):
                        pass
                assert [f.kind for f in raised.value.failures] == [ProviderFailureKind.TIMEOUT]
    finally:
        await registry.close_all()
    assert [(n.retry, n.max_retries, n.kind, n.mode) for n in notices] == [(1, 3, ProviderFailureKind.TIMEOUT, "stream")]
    assert len(route.calls) == (2 if admitted else 1)
    assert hook.attempts[0].outcomes == ["not_applied:provider_no_response_timeout/v1"]
    if admitted:
        assert "".join(text) == "Done"


@pytest.mark.unit
@respx.mock
async def test_refused_retry_does_not_fall_through_to_a_fallback_provider() -> None:
    from bowhead_router.errors import StreamingTargetsExhaustedError
    from bowhead_router.registry import ProviderConfig, ProviderRegistry
    from bowhead_router.retry import retry_observer_scope

    primary_route = respx.post(f"{_BASE}/chat/completions").mock(side_effect=httpx2.ReadTimeout("no response"))
    fallback_route = respx.post(f"{_ANTHROPIC_BASE}/v1/messages").respond(500)
    registry = ProviderRegistry()
    registry.load(
        [
            ProviderConfig(name="primary", type="openai_compatible", base_url=_BASE, api_key="test-key"),
            ProviderConfig(name="fallback", type="anthropic_compatible", base_url=_ANTHROPIC_BASE, api_key="test-key"),
        ]
    )
    router = Router(registry)
    router.bind(AgentBinding(agent_type="lead", primary=("primary", "model"), fallback=[("fallback", "model")]))

    class OutOfTime:
        async def admit_retry(self, notice) -> bool:
            return False

        def attempt_fits(self, *, delay_s: float, attempt_timeout_s: float | None) -> bool:
            return False

    try:
        with retry_observer_scope(OutOfTime()), pytest.raises(StreamingTargetsExhaustedError):
            async for _ in router.stream("lead", LLMRequest(model="model", messages=[Message(role="user", content="hi")])):
                pass
    finally:
        await registry.close_all()
    assert len(primary_route.calls) == 1
    assert len(fallback_route.calls) == 0


@pytest.mark.unit
@respx.mock
async def test_provider_failure_observation_precedes_fence_and_cannot_enable_retry() -> None:
    from bowhead_router.errors import LLMTargetFailure

    hook = _Hook()
    observed: list[LLMTargetFailure] = []

    async def failed_observer(failure: LLMTargetFailure) -> None:
        assert hook.attempts[0].outcomes == []
        observed.append(failure)
        raise RuntimeError("diagnostic writer unavailable")

    route = respx.post(f"{_BASE}/chat/completions").respond(400, json={"error": {"message": "Invalid request"}})
    adapter = OpenAICompatibleAdapter(base_url=_BASE, api_key="sk-test", provider_name="provider")
    try:
        with physical_invocation_scope(hook, failure_observer=failed_observer), pytest.raises(InvocationOutcomeUnknownError):
            await adapter.invoke(LLMRequest(model="model", messages=[Message(role="user", content="hello")]))
    finally:
        await adapter.close()
    assert route.call_count == 1
    assert len(observed) == 1
    assert observed[0].status_code == 400
    assert hook.attempts[0].outcomes == ["unknown:provider_response_unclassified"]
