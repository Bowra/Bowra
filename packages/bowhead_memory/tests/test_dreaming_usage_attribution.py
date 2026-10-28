"""credit-billing gap 7 —— dreaming 记忆候选的用量归属 pin。

`DreamingLight.observe` 的 `router.invoke` 是 turn 完成后 fire-and-forget 发的，此前
**完全不进任何账本**（`candidates.py` 里那句「S2 记账」欠账注释指的就是它）：turns 表不记、
credit_ledger 不记，等于这部分 token 在系统里不存在。

计量收敛到 Router 之后它自动被记，但只有打上 `reason="dreaming"` 才能与用户请求驱动的
`reason="turn"` 分开看——而「系统自发的记忆蒸馏该不该由用户付费」这个决策，前提就是先能
把这两类用量分开。本文件钉死这个归属。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bowhead_memory.candidates import DreamingLight, DreamObservation, MemoryCandidateStore
from bowhead_router.registry import ProviderConfig, ProviderRegistry
from bowhead_router.router import AgentBinding, Router
from bowhead_router.types import LLMResponse, TokenUsage
from bowhead_router.usage import LLMUsageRecord, set_usage_sink

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_CANDIDATE_JSON = '{"candidates":[{"kind":"preference","key":"k","value":"v","reason":"r"}]}'


class _UsageMockAdapter:
    """回固定 usage 的 adapter —— 内建 MockAdapter 的 usage 是估算值，这里要确定的数字。"""

    async def invoke(self, req: object) -> LLMResponse:  # noqa: ARG002
        return LLMResponse(
            content=_CANDIDATE_JSON,
            usage=TokenUsage(input_tokens=800, output_tokens=60),
        )

    async def stream(self, req: object):  # noqa: ANN201, ARG002
        raise NotImplementedError


def _router() -> Router:
    reg = ProviderRegistry()
    reg.load([ProviderConfig(name="p", type="mock")])
    reg._adapters["p"] = _UsageMockAdapter()  # type: ignore[assignment]  # noqa: SLF001
    r = Router(reg)
    r.bind(AgentBinding(agent_type="echo", primary=("p", "mock")))  # DreamingLight uses echo (fast tier).
    return r


@pytest.fixture
def sink() -> Iterator[list[LLMUsageRecord]]:
    seen: list[LLMUsageRecord] = []
    previous = set_usage_sink(seen.append)
    try:
        yield seen
    finally:
        set_usage_sink(previous)


@pytest.mark.asyncio
async def test_dreaming_usage_is_recorded_and_labelled(tmp_path: Path, sink: list[LLMUsageRecord]) -> None:
    """★ 核心回归: dreaming 的 LLM 调用必须被记账, 且 reason="dreaming" 可与 turn 用量区分。"""
    store = MemoryCandidateStore(db_path=str(tmp_path / "c.db"))
    await store.initialize()
    dl = DreamingLight(router=_router(), candidate_store=store, min_interval_seconds=0)

    created = await dl.observe(
        DreamObservation(
            turn_id="t_1",
            user_id="u_1",
            user_text="以后都用环比",
            final_answer="好的",
            intent="lookup",
            tool_chain=["bi_ask_question"],
            workspace_id="ws_1",
        )
    )

    assert len(created) == 1, "前置: 蒸馏本身要正常出候选, 否则这条测试测不到记账"
    assert len(sink) == 1, "dreaming 的 LLM 调用必须留下用量记录（此前完全不进账本）"
    rec = sink[0]
    assert rec.tokens_total == 860
    ctx = rec.context
    assert ctx.reason == "dreaming", "必须标成 dreaming, 否则与用户请求驱动的用量混在一起"
    assert (ctx.turn_id, ctx.user_id, ctx.workspace_id) == ("t_1", "u_1", "ws_1")


@pytest.mark.asyncio
async def test_dreaming_attribution_does_not_leak(tmp_path: Path, sink: list[LLMUsageRecord]) -> None:
    """reason 只在这次蒸馏内生效, 不能污染之后的调用。"""
    from bowhead_router.usage import UNATTRIBUTED, current_usage_context

    store = MemoryCandidateStore(db_path=str(tmp_path / "c.db"))
    await store.initialize()
    dl = DreamingLight(router=_router(), candidate_store=store, min_interval_seconds=0)

    await dl.observe(
        DreamObservation(
            turn_id="t_1",
            user_id="u_1",
            user_text="x",
            final_answer="y",
            intent="lookup",
            tool_chain=[],
        )
    )

    assert current_usage_context().reason == UNATTRIBUTED
