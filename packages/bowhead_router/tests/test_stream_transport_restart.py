"""Transport failures retry through the Router loop, before or after output.

An attempt interrupted in transport is settled not applied and is not charged.
A caller that can discard yielded output (``allow_restart=True``) receives one
restart chunk before the next attempt; other callers keep the interrupted error.
"""

from __future__ import annotations

import json

import httpx2
import pytest
import respx
from bowhead_router.errors import ProviderCallError, ProviderFailureKind, StreamingInterruptedError, StreamingTargetsExhaustedError
from bowhead_router.invocation import PhysicalInvocationSpec, physical_observation_scope
from bowhead_router.registry import ProviderConfig, ProviderRegistry
from bowhead_router.retry import retry_observer_scope
from bowhead_router.types import LLMChunk, LLMRequest, Message, TokenUsage
from bowhead_router.usage import LLMUsageRecord, usage_context

from bowhead_router import AgentBinding, Router

_OPENAI = "https://api.openai.com/v1"
_ANTHROPIC = "https://api.anthropic.example.com"
_URLS = {"openai": f"{_OPENAI}/chat/completions", "anthropic": f"{_ANTHROPIC}/v1/messages"}


class _Attempt:
    def __init__(self) -> None:
        self.outcomes: list[str] = []

    async def confirmed_not_applied(self, *, evidence_code: str, evidence_digest: str) -> bool:
        self.outcomes.append(f"not_applied:{evidence_code}")
        return True

    async def complete(self, *, usage: TokenUsage, response_digest: str) -> bool:
        self.outcomes.append(f"complete:{usage.input_tokens}:{usage.output_tokens}")
        return True

    async def unknown(self, *, reason_code: str) -> None:
        self.outcomes.append(f"unknown:{reason_code}")


class _Observer:
    """Stands in for the chat runtime's physical usage observer."""

    def __init__(self) -> None:
        self.attempts: list[_Attempt] = []

    async def begin(self, spec: PhysicalInvocationSpec, *, request: LLMRequest | None, body: object, family: str | None) -> _Attempt:
        attempt = _Attempt()
        self.attempts.append(attempt)
        return attempt

    def outcomes(self) -> list[list[str]]:
        return [attempt.outcomes for attempt in self.attempts]


class _Gate:
    def __init__(self, *, admitted: bool = True) -> None:
        self.admitted = admitted
        self.notices: list[object] = []

    async def admit_retry(self, notice) -> bool:
        self.notices.append(notice)
        return self.admitted

    def attempt_fits(self, *, delay_s: float, attempt_timeout_s: float | None) -> bool:
        return self.admitted


class _FailAfter(httpx2.AsyncByteStream):
    """Yield the given SSE frames, then fail in transport."""

    def __init__(self, frames: list[bytes], failure: Exception) -> None:
        self.frames = frames
        self.failure = failure

    async def __aiter__(self):
        for frame in self.frames:
            yield frame
        raise self.failure


def _sse(family: str, text: str) -> list[bytes]:
    if family == "openai":
        return [b"data: " + json.dumps({"choices": [{"delta": {"content": text}, "finish_reason": None}]}).encode() + b"\n\n"]
    # The provider reports input usage in message_start, before any text.
    return [
        b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":11,"output_tokens":0}}}\n\n',
        b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
        b"event: content_block_delta\ndata: "
        + json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}).encode()
        + b"\n\n",
    ]


def _completed(family: str, text: str) -> httpx2.Response:
    frames = _sse(family, text)
    if family == "openai":
        frames += [
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":1}}\n\n',
            b"data: [DONE]\n\n",
        ]
    else:
        frames += [
            b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
            b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}\n\n',
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=b"".join(frames))


def _interrupted(family: str, text: str, failure: Exception | None = None) -> httpx2.Response:
    return httpx2.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=_FailAfter(_sse(family, text), failure or httpx2.ReadError("connection reset")),
    )


def _router(*targets: tuple[str, str], max_retries: int = 1) -> tuple[Router, ProviderRegistry]:
    registry = ProviderRegistry()
    registry.load(
        [
            ProviderConfig(
                name=name,
                type="openai_compatible" if family == "openai" else "anthropic_compatible",
                base_url=_OPENAI if family == "openai" else _ANTHROPIC,
                api_key="test-key",
            )
            for name, family in targets
        ]
    )
    router = Router(registry)
    names = [name for name, _ in targets]
    router.bind(AgentBinding(agent_type="lead", primary=(names[0], "model"), fallback=[(name, "model") for name in names[1:]]))
    router.max_retries = max_retries
    return router, registry


async def _collect(router: Router, *, allow_restart: bool, gate: _Gate, observer: _Observer, usage: list[LLMUsageRecord]) -> list[LLMChunk]:
    chunks: list[LLMChunk] = []
    with physical_observation_scope(observer), retry_observer_scope(gate), usage_context(turn_id="t_test", turn_usage_sink=usage.append):
        async for chunk in router.stream(
            "lead",
            LLMRequest(model="model", messages=[Message(role="user", content="hi")]),
            allow_restart=allow_restart,
        ):
            chunks.append(chunk)
    return chunks


def _text(chunks: list[LLMChunk]) -> list[str]:
    return ["<restart>" if chunk.stream_restart else chunk.delta for chunk in chunks if chunk.stream_restart or chunk.delta]


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr("bowhead_router.router.asyncio.sleep", no_sleep)


@pytest.mark.unit
@pytest.mark.parametrize("family", ["openai", "anthropic"])
@pytest.mark.parametrize("failure", [httpx2.ReadError("reset"), httpx2.RemoteProtocolError("peer closed"), httpx2.ReadTimeout("stalled")])
@respx.mock
async def test_failure_after_output_restarts_and_retries_without_charging(family: str, failure: Exception) -> None:
    route = respx.post(_URLS[family]).mock(side_effect=[_interrupted(family, "Partial", failure), _completed(family, "Done")])
    router, registry = _router(("primary", family))
    gate, observer, usage = _Gate(), _Observer(), []
    try:
        chunks = await _collect(router, allow_restart=True, gate=gate, observer=observer, usage=usage)
    finally:
        await registry.close_all()

    assert _text(chunks) == ["Partial", "<restart>", "Done"]
    restart = next(chunk for chunk in chunks if chunk.stream_restart)
    assert restart == LLMChunk(stream_restart=True)
    assert len(route.calls) == 2
    timed_out = isinstance(failure, httpx2.TimeoutException)
    assert [(n.retry, n.kind) for n in gate.notices] == [(1, ProviderFailureKind.TIMEOUT if timed_out else ProviderFailureKind.NETWORK)]
    assert observer.outcomes()[0] == ["not_applied:provider_transport_interrupted/v1"]
    assert observer.outcomes()[1][0].startswith("complete:")
    # Only the completed attempt is charged; the interrupted one's message_start input usage is not.
    assert [(record.tokens_input, record.tokens_output) for record in usage] == [(11, 1) if family == "anthropic" else (3, 1)]


@pytest.mark.unit
@pytest.mark.parametrize("failure", [httpx2.ReadError("reset"), httpx2.RemoteProtocolError("peer closed")])
@respx.mock
async def test_network_error_before_output_is_settled_not_applied_and_retried(failure: Exception) -> None:
    route = respx.post(_URLS["openai"]).mock(side_effect=[failure, _completed("openai", "Done")])
    router, registry = _router(("primary", "openai"))
    gate, observer, usage = _Gate(), _Observer(), []
    try:
        chunks = await _collect(router, allow_restart=True, gate=gate, observer=observer, usage=usage)
    finally:
        await registry.close_all()

    # Nothing reached the caller before the failure, so no restart is needed.
    assert _text(chunks) == ["Done"]
    assert len(route.calls) == 2
    assert [n.kind for n in gate.notices] == [ProviderFailureKind.NETWORK]
    assert observer.outcomes()[0] == ["not_applied:provider_transport_interrupted/v1"]


@pytest.mark.unit
@respx.mock
async def test_without_restart_a_failure_after_output_stays_interrupted() -> None:
    route = respx.post(_URLS["anthropic"]).mock(side_effect=[_interrupted("anthropic", "Partial"), _completed("anthropic", "Done")])
    router, registry = _router(("primary", "anthropic"))
    gate, observer, usage = _Gate(), _Observer(), []
    chunks: list[LLMChunk] = []
    try:
        with (
            pytest.raises(StreamingInterruptedError) as raised,
            physical_observation_scope(observer),
            retry_observer_scope(gate),
            usage_context(turn_usage_sink=usage.append),
        ):
            async for chunk in router.stream("lead", LLMRequest(model="model", messages=[Message(role="user", content="hi")])):
                chunks.append(chunk)
    finally:
        await registry.close_all()

    assert _text(chunks) == ["Partial"]
    assert len(route.calls) == 1
    assert gate.notices == []
    assert [f.first_visible_chunk_emitted for f in raised.value.failures] == [True]
    assert observer.outcomes() == [["not_applied:provider_transport_interrupted/v1"]]
    assert usage == []


@pytest.mark.unit
@respx.mock
async def test_refused_retry_after_output_ends_without_another_attempt() -> None:
    primary = respx.post(_URLS["openai"]).mock(side_effect=[_interrupted("openai", "Partial"), _completed("openai", "Done")])
    fallback = respx.post(_URLS["anthropic"]).mock(return_value=_completed("anthropic", "Other"))
    router, registry = _router(("primary", "openai"), ("fallback", "anthropic"))
    gate, observer = _Gate(admitted=False), _Observer()
    chunks: list[LLMChunk] = []
    try:
        with pytest.raises(StreamingInterruptedError), physical_observation_scope(observer), retry_observer_scope(gate):
            async for chunk in router.stream(
                "lead",
                LLMRequest(model="model", messages=[Message(role="user", content="hi")]),
                allow_restart=True,
            ):
                chunks.append(chunk)
    finally:
        await registry.close_all()

    assert _text(chunks) == ["Partial"]
    assert len(gate.notices) == 1
    assert len(primary.calls) == 1
    assert len(fallback.calls) == 0


@pytest.mark.unit
@respx.mock
async def test_fallback_target_after_output_is_preceded_by_a_restart() -> None:
    primary = respx.post(_URLS["openai"]).mock(side_effect=[_interrupted("openai", "Partial")])
    fallback = respx.post(_URLS["anthropic"]).mock(return_value=_completed("anthropic", "From fallback"))
    router, registry = _router(("primary", "openai"), ("fallback", "anthropic"), max_retries=0)
    gate, observer, usage = _Gate(), _Observer(), []
    try:
        chunks = await _collect(router, allow_restart=True, gate=gate, observer=observer, usage=usage)
    finally:
        await registry.close_all()

    assert _text(chunks) == ["Partial", "<restart>", "From fallback"]
    assert len(primary.calls) == len(fallback.calls) == 1
    assert observer.outcomes()[0] == ["not_applied:provider_transport_interrupted/v1"]
    assert [(record.provider, record.tokens_input) for record in usage] == [("fallback", 11)]


@pytest.mark.unit
@respx.mock
async def test_exhaustion_reports_whether_the_last_attempt_left_output() -> None:
    # The withdrawn first attempt no longer counts as visible output.
    route = respx.post(_URLS["openai"]).mock(side_effect=[_interrupted("openai", "Partial"), httpx2.ConnectError("refused")])
    router, registry = _router(("primary", "openai"))
    gate, observer = _Gate(), _Observer()
    chunks: list[LLMChunk] = []
    try:
        with pytest.raises(StreamingTargetsExhaustedError) as raised, physical_observation_scope(observer), retry_observer_scope(gate):
            async for chunk in router.stream(
                "lead",
                LLMRequest(model="model", messages=[Message(role="user", content="hi")]),
                allow_restart=True,
            ):
                chunks.append(chunk)
    finally:
        await registry.close_all()

    assert not isinstance(raised.value, StreamingInterruptedError)
    assert _text(chunks) == ["Partial", "<restart>"]
    assert len(route.calls) == 2
    assert observer.outcomes() == [["not_applied:provider_transport_interrupted/v1"], ["not_applied:provider_no_response_timeout/v1"]]

    route.side_effect = [_interrupted("openai", "First"), _interrupted("openai", "Second")]
    router, registry = _router(("primary", "openai"))
    chunks.clear()
    try:
        with pytest.raises(StreamingInterruptedError), physical_observation_scope(observer), retry_observer_scope(gate):
            async for chunk in router.stream(
                "lead",
                LLMRequest(model="model", messages=[Message(role="user", content="hi")]),
                allow_restart=True,
            ):
                chunks.append(chunk)
    finally:
        await registry.close_all()
    assert _text(chunks) == ["First", "<restart>", "Second"]


class _UsageBeforeFailure:
    """An adapter that reports input usage, yields text, then fails."""

    def __init__(self, failure: Exception) -> None:
        self.failure = failure
        self.calls = 0

    async def stream(self, request: LLMRequest):
        self.calls += 1
        if self.calls == 1:
            yield LLMChunk(usage=TokenUsage(input_tokens=11))
            yield LLMChunk(delta="Partial")
            raise self.failure
        yield LLMChunk(delta="Done")
        yield LLMChunk(finish_reason="stop", usage=TokenUsage(input_tokens=11, output_tokens=2))


def _transport_error() -> ProviderCallError:
    try:
        raise httpx2.ReadError("reset")
    except httpx2.ReadError as cause:
        error = ProviderCallError(kind=ProviderFailureKind.NETWORK, retryable=True)
        error.__cause__ = cause
        return error


@pytest.mark.unit
@pytest.mark.parametrize("transport", [True, False])
async def test_only_a_transport_interrupted_attempt_is_not_charged(transport: bool) -> None:
    failure = _transport_error() if transport else ProviderCallError(kind=ProviderFailureKind.UPSTREAM, retryable=True)
    adapter = _UsageBeforeFailure(failure)
    registry = ProviderRegistry()
    registry.load([ProviderConfig(name="primary", type="mock")])
    registry._adapters["primary"] = adapter  # type: ignore[assignment]  # noqa: SLF001 — Inject a stand-in adapter.
    router = Router(registry)
    router.bind(AgentBinding(agent_type="lead", primary=("primary", "model")))
    router.max_retries = 1
    usage: list[LLMUsageRecord] = []
    with usage_context(turn_usage_sink=usage.append):
        chunks = [chunk async for chunk in router.stream("lead", LLMRequest(model="model", messages=[]), allow_restart=True)]

    assert _text(chunks) == ["Partial", "<restart>", "Done"]
    # A provider-reported failure keeps today's accounting of the usage it reported.
    assert [(record.tokens_input, record.tokens_output) for record in usage] == ([] if transport else [(11, 0)]) + [(11, 2)]
