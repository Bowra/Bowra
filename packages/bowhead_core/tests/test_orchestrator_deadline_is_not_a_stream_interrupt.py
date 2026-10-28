"""我们自己的单次流式时限到期，不能报成「模型回答传输中断」。

116 dogfood 2026-09-24：近 90 分钟里 `subagent.llm_failed` **10/10** 都是
`provider=orchestrator_deadline kind=timeout`（`asyncio.wait_for(_drain(),
timeout=llm_timeout)`，默认 300s），零个真 provider 故障。而 `_reason_for`
**第一行就按异常类型短路**，根本不看 `failure`：

    if isinstance(exc, StreamingInterruptedError):
        return "llm_stream_interrupted", "interrupted"

于是用户拿到「模型回答传输中断，请重新发起本轮」——原样重发必然再撞同一个时限。
对照组：peer 级超时的文案一直是对的（「这个分析智能体执行超时，请缩小分析范围后重试」）。
"""

from __future__ import annotations

import pytest
from bowhead_core.llm_failure import (
    LLM_REASON_CODES,
    ORCHESTRATOR_DEADLINE_PROVIDER,
    project_llm_failure,
)
from bowhead_router.errors import (
    LLMTargetFailure,
    ProviderFailureKind,
    StreamingInterruptedError,
    StreamingTargetsExhaustedError,
)


def _failure(
    *,
    provider: str,
    kind: ProviderFailureKind,
    visible: bool = True,
) -> LLMTargetFailure:
    return LLMTargetFailure(
        provider=provider,
        model="m",
        provider_family="custom",
        mode="stream",
        target_index=0,
        attempt=1,
        kind=kind,
        retryable=True,
        first_visible_chunk_emitted=visible,
    )


@pytest.mark.unit
def test_our_own_deadline_is_reported_as_a_deadline_not_an_interrupt() -> None:
    """已吐过可见内容 ⇒ raise 点包成 `StreamingInterruptedError`，这正是被误判的那一路。"""
    exc = StreamingInterruptedError([_failure(provider=ORCHESTRATOR_DEADLINE_PROVIDER, kind=ProviderFailureKind.TIMEOUT)])

    proj = project_llm_failure(exc)

    assert proj is not None
    assert proj.reason_code == "llm_stream_deadline_exceeded"


@pytest.mark.unit
def test_the_deadline_is_also_caught_when_nothing_was_streamed_yet() -> None:
    """一个字都没吐时 raise 点包的是 `StreamingTargetsExhaustedError`——同一个 deadline，
    此前会落到 `kind is TIMEOUT` 那支、报成「模型服务响应超时，请稍后重试」。
    也是假话：稍后永远不会好，时限是我们自己的。"""
    exc = StreamingTargetsExhaustedError([_failure(provider=ORCHESTRATOR_DEADLINE_PROVIDER, kind=ProviderFailureKind.TIMEOUT, visible=False)])

    proj = project_llm_failure(exc)

    assert proj is not None
    assert proj.reason_code == "llm_stream_deadline_exceeded"


@pytest.mark.unit
def test_a_real_stream_interrupt_still_says_interrupted() -> None:
    """★ 负对照：provider 真把流掐断了，`llm_stream_interrupted` 仍然是对的答案。
    判据必须认那个哨兵，不能把整条 `StreamingInterruptedError` 一并改掉。"""
    exc = StreamingInterruptedError([_failure(provider="deepseek_anthropic", kind=ProviderFailureKind.NETWORK)])

    proj = project_llm_failure(exc)

    assert proj is not None
    assert proj.reason_code == "llm_stream_interrupted"


@pytest.mark.unit
def test_a_real_provider_timeout_still_says_provider_timeout() -> None:
    """★ 第二个负对照：provider 自己超时也是 `kind=TIMEOUT`，而那条「稍后重试」是对的。
    只靠 `kind` 区分不出来——这就是判据必须是 provider 哨兵的理由。"""
    exc = StreamingTargetsExhaustedError([_failure(provider="deepseek_anthropic", kind=ProviderFailureKind.TIMEOUT, visible=False)])

    proj = project_llm_failure(exc)

    assert proj is not None
    assert proj.reason_code == "llm_provider_timeout"


@pytest.mark.unit
def test_the_new_code_is_whitelisted_or_it_never_reaches_the_user() -> None:
    """`published_diagnostics` 对不在白名单里的 reason_code 直接丢成泛化失败——
    加了码不进白名单等于没加（上一个新码正是这么漏的）。"""
    assert "llm_stream_deadline_exceeded" in LLM_REASON_CODES


@pytest.mark.unit
def test_the_raise_site_uses_the_shared_sentinel() -> None:
    """哨兵是个魔法字符串，两处各写各的就会静默失联：raise 点写 'orchestrator-deadline'
    而判据认 'orchestrator_deadline'，测试全绿、线上恒走老分支。"""
    import inspect

    from bowhead_core import subagent

    src = inspect.getsource(subagent)
    assert "provider=ORCHESTRATOR_DEADLINE_PROVIDER," in src
    assert 'provider="orchestrator_deadline"' not in src
