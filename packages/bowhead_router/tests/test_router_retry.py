"""Router retry behavior — 0.13.2 (2026-05-12).

Why: 8686 dogfood showed 18/31 turns died on transient HTTP 429 from DeepSeek
primary. Without retry, single 429 burst falls over immediately to fallback,
which (during the same burst) may also 429. Short retry (1 attempt, 2s
backoff) catches transient bursts while account-quota-exhausted still falls
over after exhausted.
"""

from __future__ import annotations

import asyncio

import pytest
from bowhead_router.errors import (
    InvocationTargetsExhaustedError,
    ProviderCallError,
    ProviderFailureKind,
    RetryPolicy,
    StreamingInterruptedError,
    StreamingTargetsExhaustedError,
)
from bowhead_router.router import AgentBinding, Router, _invoke_with_retry
from bowhead_router.types import LLMChunk, LLMRequest, LLMResponse, TokenUsage


class _FakeRetryableError(Exception):
    """Mimics adapter-side 429-class errors."""

    def __init__(self, message: str = "http 429"):
        super().__init__(message)
        self.retryable = True


class _FakeFatalError(Exception):
    """Mimics 4xx schema-validation class errors (NOT retryable)."""

    def __init__(self, message: str = "http 400"):
        super().__init__(message)
        self.retryable = False


class _StubAdapter:
    """Records each invoke call and replays a script of outcomes."""

    def __init__(self, script: list):
        self._script = list(script)
        self.invoke_count = 0

    async def invoke(self, req: LLMRequest) -> LLMResponse:
        self.invoke_count += 1
        outcome = self._script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.mark.asyncio
async def test_retry_retries_once_on_retryable_error_then_succeeds(monkeypatch):
    """Retryable 429 on attempt 1 → retry after 2s → succeeds. 2 total invokes."""
    monkeypatch.setattr("asyncio.sleep", lambda *_a, **_k: _noop_sleep())
    adapter = _StubAdapter(
        [
            _FakeRetryableError("429 first"),
            LLMResponse(content="ok", model="m"),
        ]
    )
    req = LLMRequest(model="m", messages=[])
    resp = await _invoke_with_retry(
        adapter,
        req,
        provider_name="p",
        model="m",
    )
    assert resp.content == "ok"
    assert adapter.invoke_count == 2


@pytest.mark.asyncio
async def test_retry_does_not_retry_non_retryable(monkeypatch):
    """Non-retryable 400 schema error → immediate raise, no retry."""
    monkeypatch.setattr("asyncio.sleep", lambda *_a, **_k: _noop_sleep())
    adapter = _StubAdapter([_FakeFatalError("400 schema")])
    req = LLMRequest(model="m", messages=[])
    with pytest.raises(_FakeFatalError):
        await _invoke_with_retry(
            adapter,
            req,
            provider_name="p",
            model="m",
        )
    assert adapter.invoke_count == 1  # no retry


@pytest.mark.asyncio
async def test_retry_exhausts_after_max_attempts_still_raises(monkeypatch):
    """All attempts hit retryable 429 → final raise. max_attempts invokes."""
    monkeypatch.setattr("asyncio.sleep", lambda *_a, **_k: _noop_sleep())
    adapter = _StubAdapter(
        [
            _FakeRetryableError("429-1"),
            _FakeRetryableError("429-2"),
        ]
    )
    req = LLMRequest(model="m", messages=[])
    with pytest.raises(_FakeRetryableError):
        await _invoke_with_retry(
            adapter,
            req,
            provider_name="p",
            model="m",
        )
    assert adapter.invoke_count == 2  # exhausted


@pytest.mark.asyncio
async def test_retry_no_attribute_treated_as_non_retryable(monkeypatch):
    """Exception without `.retryable` attr → not retried (defensive default)."""
    monkeypatch.setattr("asyncio.sleep", lambda *_a, **_k: _noop_sleep())

    class _PlainErr(Exception):
        pass

    adapter = _StubAdapter([_PlainErr("unknown")])
    req = LLMRequest(model="m", messages=[])
    with pytest.raises(_PlainErr):
        await _invoke_with_retry(
            adapter,
            req,
            provider_name="p",
            model="m",
        )
    assert adapter.invoke_count == 1


async def _noop_sleep():
    """Async no-op for monkeypatched asyncio.sleep."""
    return


class _Registry:
    def __init__(self, adapters: dict[str, object]) -> None:
        self.adapters = adapters

    def has(self, name: str) -> bool:
        return name in self.adapters

    def get(self, name: str) -> object:
        return self.adapters[name]


class _StreamAdapter:
    provider_name = "provider"

    def __init__(self, scripts: list[list[LLMChunk | Exception]]) -> None:
        self.scripts = scripts
        self.stream_count = 0
        self.invoke_count = 0

    async def stream(self, req: LLMRequest):
        script = self.scripts[self.stream_count]
        self.stream_count += 1
        for item in script:
            if isinstance(item, Exception):
                raise item
            yield item

    async def invoke(self, req: LLMRequest) -> LLMResponse:
        self.invoke_count += 1
        return LLMResponse(content="fallback")


def _router_for(adapter: object) -> Router:
    router = Router(_Registry({"provider": adapter}))  # type: ignore[arg-type]
    router.bind(AgentBinding(agent_type="lead", primary=("provider", "model")))
    return router


@pytest.mark.asyncio
async def test_stream_exhaustion_before_visible_chunk_is_typed_and_fallback_safe(
    monkeypatch,
) -> None:
    monkeypatch.setattr("asyncio.sleep", lambda *_a, **_k: _noop_sleep())
    adapter = _StreamAdapter(
        [
            [
                LLMChunk(usage=TokenUsage(input_tokens=1)),
                ProviderCallError(
                    kind=ProviderFailureKind.UPSTREAM,
                    retryable=True,
                    status_code=500,
                ),
            ],
            [
                ProviderCallError(
                    kind=ProviderFailureKind.UPSTREAM,
                    retryable=True,
                    status_code=500,
                ),
            ],
        ]
    )

    router = _router_for(adapter)
    # Two scripted attempts: the configured retry setting allows one retry.
    router.max_retries = 1
    with pytest.raises(StreamingTargetsExhaustedError) as caught:
        async for _ in router.stream(
            "lead",
            LLMRequest(model="", messages=[]),
        ):
            pass

    assert caught.value.safe_to_fallback is True
    assert caught.value.first_visible_chunk_emitted is False
    assert len(caught.value.failures) == 2


@pytest.mark.asyncio
async def test_stream_failure_after_visible_chunk_never_falls_through() -> None:
    adapter = _StreamAdapter(
        [
            [
                LLMChunk(delta="already visible"),
                ProviderCallError(
                    kind=ProviderFailureKind.UPSTREAM,
                    retryable=True,
                    status_code=500,
                ),
            ]
        ]
    )

    chunks: list[LLMChunk] = []
    with pytest.raises(StreamingInterruptedError) as caught:
        async for chunk in _router_for(adapter).stream(
            "lead",
            LLMRequest(model="", messages=[]),
        ):
            chunks.append(chunk)

    assert [chunk.delta for chunk in chunks] == ["already visible"]
    assert caught.value.first_visible_chunk_emitted is True
    assert adapter.stream_count == 1


@pytest.mark.asyncio
async def test_invoke_retry_policy_can_limit_fallback_to_one_attempt() -> None:
    adapter = _StubAdapter([_FakeRetryableError("one attempt only")])
    router = _router_for(adapter)

    with pytest.raises(Exception) as caught:
        await router.invoke(
            "lead",
            LLMRequest(model="", messages=[]),
            retry_policy=RetryPolicy(max_attempts_per_target=1),
        )

    assert "one attempt only" not in str(caught.value)
    assert adapter.invoke_count == 1


@pytest.mark.asyncio
async def test_stream_idle_timeout_before_visible_chunk_is_fallback_safe() -> None:
    class _SlowAdapter:
        provider_name = "provider"

        async def stream(self, req: LLMRequest):
            await asyncio.sleep(0.05)
            yield LLMChunk(delta="late")

    with pytest.raises(StreamingTargetsExhaustedError) as caught:
        async for _ in _router_for(_SlowAdapter()).stream(
            "lead",
            LLMRequest(model="", messages=[]),
            retry_policy=RetryPolicy(max_attempts_per_target=1),
            idle_timeout_s=0.001,
        ):
            pass

    assert caught.value.failures[0].kind is ProviderFailureKind.TIMEOUT
    assert caught.value.safe_to_fallback is True


@pytest.mark.asyncio
async def test_unregistered_target_is_a_typed_configuration_failure() -> None:
    router = Router(_Registry({}))  # type: ignore[arg-type]
    router.bind(AgentBinding(agent_type="lead", primary=("missing", "model")))

    with pytest.raises(InvocationTargetsExhaustedError) as invoke_error:
        await router.invoke("lead", LLMRequest(model="", messages=[]))
    assert invoke_error.value.failures[0].kind is ProviderFailureKind.CONFIGURATION
    assert invoke_error.value.failures[0].attempt == 0

    with pytest.raises(StreamingTargetsExhaustedError) as stream_error:
        async for _ in router.stream("lead", LLMRequest(model="", messages=[])):
            pass
    assert stream_error.value.failures[0].kind is ProviderFailureKind.CONFIGURATION
    assert stream_error.value.safe_to_fallback is False


@pytest.mark.asyncio
async def test_unregistered_fallback_prevents_nonstream_replay(monkeypatch) -> None:
    monkeypatch.setattr("asyncio.sleep", lambda *_a, **_k: _noop_sleep())
    adapter = _StreamAdapter(
        [
            [
                ProviderCallError(
                    kind=ProviderFailureKind.UPSTREAM,
                    retryable=True,
                    status_code=500,
                )
            ]
        ]
    )
    router = Router(_Registry({"provider": adapter}))  # type: ignore[arg-type]
    router.bind(
        AgentBinding(
            agent_type="lead",
            primary=("provider", "model"),
            fallback=[("missing", "fallback-model")],
        )
    )

    with pytest.raises(StreamingTargetsExhaustedError) as caught:
        async for _ in router.stream(
            "lead",
            LLMRequest(model="", messages=[]),
            retry_policy=RetryPolicy(max_attempts_per_target=1),
        ):
            pass

    assert [failure.kind for failure in caught.value.failures] == [
        ProviderFailureKind.UPSTREAM,
        ProviderFailureKind.CONFIGURATION,
    ]
    assert caught.value.safe_to_fallback is False


@pytest.mark.asyncio
async def test_stream_interruption_retains_failures_from_prior_targets() -> None:
    primary = _StreamAdapter(
        [
            [
                ProviderCallError(
                    kind=ProviderFailureKind.REQUEST_REJECTED,
                    retryable=False,
                    status_code=400,
                )
            ]
        ]
    )
    fallback = _StreamAdapter(
        [
            [
                LLMChunk(delta="visible"),
                ProviderCallError(
                    kind=ProviderFailureKind.UPSTREAM,
                    retryable=True,
                    status_code=500,
                ),
            ]
        ]
    )
    router = Router(_Registry({"primary": primary, "fallback": fallback}))  # type: ignore[arg-type]
    router.bind(
        AgentBinding(
            agent_type="lead",
            primary=("primary", "model-a"),
            fallback=[("fallback", "model-b")],
        )
    )

    with pytest.raises(StreamingInterruptedError) as caught:
        async for _ in router.stream(
            "lead",
            LLMRequest(model="", messages=[]),
            retry_policy=RetryPolicy(max_attempts_per_target=1),
        ):
            pass

    assert [failure.status_code for failure in caught.value.failures] == [400, 500]
    assert caught.value.failures[-1].first_visible_chunk_emitted is True
