"""credential-readiness D1 — RefusalAdapter 契约（tech-design §3 三条安全性事实 + A5b）.

守卫必须位于 usage seam 之下：record_usage 在 adapter 成功返回后才调用，
拒绝 = 零用量行——这是「mock 假用量污染 llm_usage_events」的根治判据（A5b）。
"""

from __future__ import annotations

import pytest
from bowhead_router.registry import ProviderConfig, ProviderRegistry

from bowhead_router import (
    AgentBinding,
    InvocationTargetsExhaustedError,
    LLMRequest,
    Message,
    RefusalAdapter,
    Router,
    TeamModelUnconfiguredError,
    set_refusal_sink,
    set_usage_sink,
)


def _refusal_router() -> Router:
    reg = ProviderRegistry()
    reg.load([ProviderConfig(name="llm_refusal", type="refusal")])
    r = Router(reg)
    r.bind(AgentBinding(agent_type="lead", primary=("llm_refusal", "mock-model")))
    return r


def _req() -> LLMRequest:
    return LLMRequest(model="", messages=[Message(role="user", content="hi")])


async def test_registry_builds_refusal_adapter() -> None:
    reg = ProviderRegistry()
    reg.load([ProviderConfig(name="llm_refusal", type="refusal")])
    assert isinstance(reg.get("llm_refusal"), RefusalAdapter)


async def test_invoke_raises_typed_nonretryable_error() -> None:
    # Since b89ee28c4, Router wraps exhausted targets in InvocationTargetsExhaustedError
    # (from last_error); __cause__ preserves the typed refusal reason. Verify this chain.
    with pytest.raises(InvocationTargetsExhaustedError) as ei:
        await _refusal_router().invoke("lead", _req())
    cause = ei.value.__cause__
    assert isinstance(cause, TeamModelUnconfiguredError)
    # retryable=False → _invoke_with_retry propagates immediately without retrying (the criterion in router.py).
    assert cause.retryable is False


async def test_refusal_records_zero_usage_and_fires_sink() -> None:
    """A5b：拒绝路径零用量行；refusal sink 恰好一次（归因面）。"""
    usage_rows: list[object] = []
    refusals: list[object] = []
    prev_usage = set_usage_sink(usage_rows.append)
    prev_refusal = set_refusal_sink(refusals.append)
    try:
        with pytest.raises(InvocationTargetsExhaustedError):
            await _refusal_router().invoke("lead", _req())
    finally:
        set_usage_sink(prev_usage)
        set_refusal_sink(prev_refusal)
    assert usage_rows == []  # record_usage runs only after the adapter returns; errors produce no usage records.
    assert len(refusals) == 1


async def test_stream_raises_too() -> None:
    adapter = RefusalAdapter()
    with pytest.raises(TeamModelUnconfiguredError):
        async for _ in adapter.stream(_req()):  # pragma: no cover — No chunks should be yielded.
            raise AssertionError("stream must not yield")


async def test_refusal_sink_error_is_swallowed() -> None:
    """镜像 record_usage 契约：sink 抛异常不得改变对调用方抛出的错误类型。"""

    def _boom(_ctx: object) -> None:
        raise RuntimeError("sink down")

    prev = set_refusal_sink(_boom)
    try:
        with pytest.raises(InvocationTargetsExhaustedError) as ei:
            await _refusal_router().invoke("lead", _req())
        assert isinstance(ei.value.__cause__, TeamModelUnconfiguredError)
    finally:
        set_refusal_sink(prev)
