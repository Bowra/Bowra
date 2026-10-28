"""LLM 用量落库 recorder —— `bowhead_router.usage` seam 的 gateway 侧实现（credit-billing gap 7）。

## 形状为什么是「内存缓冲 + 后台批量 flush」

`record_usage` 由 `Router.invoke` 在**每次 LLM 调用之后**同步调用。若在那里直接写库，就把
SQLite 的单写者争用搬到了 chat 热路径上——这正是 discovery §9.1 里反对 per-call 扣费的同一个
理由（一个 turn 内部有几十次 LLM 调用）。所以 sink 只做一件事：往内存 deque 里 append（O(1)，
不 await、不碰锁）；真正的写由后台循环批量做。

## 丢数据的取舍

deque 有上界。溢出时**丢最旧的**并计数告警：用量记录是可观测数据，不是钱账（钱账仍在
`credit_ledger`，由 turn 收尾的原子扣费维护）。宁可丢一批统计，也不能让记账把内存吃穿或把
LLM 调用阻塞住。进程退出前会 flush 一次，正常关停不丢。

## 计量 ≠ 计费

本 recorder 只写 `llm_usage_events`，**不动任何余额**。它的产出用于回答「dreaming / distill /
title 这些后台调用到底烧了多少」，那是决定「该不该对它们计费」的前置数据。所以这一层没有
flag：不存在「想要旧的漏计行为」的场景。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import TYPE_CHECKING

from bowhead_router.usage import set_usage_sink

from bowhead_gateway.buffered_flush import BufferedFlushLoop

if TYPE_CHECKING:
    from bowhead_core.user_store import UserStore
    from bowhead_router.usage import LLMUsageRecord

log = logging.getLogger(__name__)

_DEFAULT_INTERVAL_S = 30.0
_DEFAULT_MAX_BUFFER = 20_000
_FLUSH_BATCH = 500

# ─── Context-engineering sprint batches 0-a / 0-e: prompt-cache observability ───
#
# Instrument here: record_usage is the billing chokepoint for **all** LLM calls (router.py:312),
# and this sink is its sole Gateway implementation, so new call sites cannot escape it.
# ⚠ Do not instrument bowhead_router: it intentionally does not depend on bowhead_core (metrics lives in core).
#
# Hit rate is cached / (in + cached): both providers' input_tokens count only **uncached input**
# (turn-latency sprint verified the 116 endpoint; Anthropic's official definition agrees).
# cached / in would produce a ratio >1.
_CACHE_HIT_BUCKETS = (0.0, 0.1, 0.3, 0.5, 0.67, 0.8, 0.9, 1.0)

#: 0-e: consecutive calls per provider with no reported cache tokens required to raise a signal.
_NEVER_REPORTED_STREAK = 50

#: Count only actual agent-loop calls; short one-off title/distill calls have no reusable prefix,
#: so including them dilutes the diagnostic into noise.
_CACHE_RELEVANT_AGENTS = frozenset({"lead", "analyst", "compute", "modeler", "narrative"})


class LLMUsageRecorder(BufferedFlushLoop[dict[str, object]]):
    """把 router 记的用量攒进内存，由后台循环批量落库。"""

    log_name = "llm_usage_recorder"

    def __init__(
        self,
        store: UserStore,
        *,
        interval_s: float = _DEFAULT_INTERVAL_S,
        max_buffer: int = _DEFAULT_MAX_BUFFER,
    ) -> None:
        super().__init__(interval_s=interval_s, max_buffer=max_buffer, flush_batch=_FLUSH_BATCH)
        self._store = store
        # 0-e: per-provider zero-hit streak. Synchronous calls in one process/event loop need no lock.
        self._zero_cache_streak: dict[str, int] = {}

    def sink(self, record: LLMUsageRecord) -> None:
        """`bowhead_router.usage` 的 sink。**必须廉价且不抛** —— 它跑在 LLM 调用返回路径上。"""
        if record.physical_recorded:
            with contextlib.suppress(Exception):
                self._observe_cache(record)
            return
        ctx = record.context
        self._enqueue(
            {
                "team_id": ctx.team_id,
                "user_id": ctx.user_id,
                "workspace_id": ctx.workspace_id,
                "turn_id": ctx.turn_id,
                "reason": ctx.reason,
                "agent_type": record.agent_type,
                "provider": record.provider,
                "model": record.model,
                "tokens_input": record.tokens_input,
                "tokens_output": record.tokens_output,
                "tokens_cached": record.tokens_cached,
                "streamed": record.streamed,
            }
        )
        # Batches 0-a/0-e: cache observability uses in-memory counters/histograms and suppresses all exceptions;
        # it runs on the LLM return path, so observation must never fail the call.
        with contextlib.suppress(Exception):
            self._observe_cache(record)

    def _observe_cache(self, record: LLMUsageRecord) -> None:
        """记 prompt 缓存命中率（0-a）与「从不报告命中」的判别线索（0-e）。

        ⚠ **0-e 的指标名是 `never_reported` 不是 `never_hit`**：一家上游完全可能
        *在缓存但不在 usage 里报* —— 两个 adapter 都只在
        `input_tokens_details.cached_tokens`（`openai_compat.py:1595`）/
        `cache_read_input_tokens`（`anthropic_compat.py:347`）**存在时**才填。
        所以这条指标是**线索不是判决**：真要下「该 provider 不缓存」的结论，
        还得人工两连发探针（同一 prompt 连发两次、看第二次 usage）。
        """
        from bowhead_core.metrics import inc, observe

        provider = record.provider or ""
        cached = record.tokens_cached or 0
        denom = (record.tokens_input or 0) + cached
        if denom <= 0:
            return
        observe(
            "bowhead_llm_cache_hit_ratio",
            cached / denom,
            labels={"provider": provider, "agent_type": record.agent_type},
            help="prompt 缓存命中率 cached/(in+cached)。前缀被打断时这个数会掉。",
            buckets=_CACHE_HIT_BUCKETS,
        )
        if record.agent_type not in _CACHE_RELEVANT_AGENTS:
            return
        if cached > 0:
            self._zero_cache_streak[provider] = 0
            return
        streak = self._zero_cache_streak.get(provider, 0) + 1
        self._zero_cache_streak[provider] = streak
        if streak % _NEVER_REPORTED_STREAK == 0:
            inc(
                "bowhead_llm_cache_never_reported_total",
                labels={"provider": provider},
                help=(
                    "该 provider 连续 N 次一个缓存 token 都没报（线索，非判决——可能是缓存了但不报）。持续非零 ⇒ 用两连发探针确认，再考虑显式 breakpoint（批 5）。"
                ),
            )
            log.warning(
                "llm_cache.never_reported provider=%s streak=%d — 连续零命中；若两连发探针确认真不缓存，走 batch 5",
                provider,
                streak,
            )

    async def _write_batch(self, batch: list[dict[str, object]]) -> int:
        return await self._store.record_llm_usage_events(batch)


def start_llm_usage_recorder(store: UserStore) -> LLMUsageRecorder:
    """建 recorder、注册成 router 的 usage sink、起后台 flush 任务。"""
    interval_s = _DEFAULT_INTERVAL_S
    raw = os.environ.get("BOWHEAD_LLM_USAGE_FLUSH_INTERVAL_S", "").strip()
    if raw:
        try:
            interval_s = float(raw)
        except ValueError:
            log.warning("llm_usage_recorder.bad_interval value=%r -> default %.1f", raw, interval_s)
    recorder = LLMUsageRecorder(store, interval_s=interval_s)
    set_usage_sink(recorder.sink)
    recorder._task = asyncio.create_task(recorder.run())  # noqa: SLF001 — Task handle owned within this module.
    return recorder


async def stop_llm_usage_recorder(recorder: LLMUsageRecorder | None) -> None:
    """卸掉 sink 并停后台任务（`run()` 在取消路径上会先 flush）。"""
    set_usage_sink(None)
    if recorder is None or recorder._task is None:  # noqa: SLF001
        return
    recorder._task.cancel()  # noqa: SLF001
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await recorder._task  # noqa: SLF001
