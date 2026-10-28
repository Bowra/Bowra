"""credit-billing gap 7 第一刀 —— Router 计量 seam 的语义 pin。

背景: token 用量此前靠每个调用点手工上报, turn 外的 LLM 调用(dreaming 记忆候选 /
workflow distill / 会话标题)完全不进账本。`Router.invoke` / `Router.stream` 是所有 LLM
调用的必经点, 计量收敛到这里, 新增调用点就不可能漏。

本文件钉死三条不可破的约束:
1. **未注册 sink = 零行为变化**(这一刀本身不改任何现有行为)
2. **sink 抛异常绝不能波及 LLM 调用**(宁可丢一条用量记录, 不能把用户的 turn 弄挂)
3. 归属上下文按 contextvar 传递且可嵌套覆盖(后台调用能标出自己的 reason)
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from bowhead_router.registry import ProviderConfig, ProviderRegistry
from bowhead_router.router import AgentBinding, Router
from bowhead_router.types import LLMChunk, LLMRequest, LLMResponse, Message, TokenUsage
from bowhead_router.usage import (
    UNATTRIBUTED,
    LLMUsageRecord,
    current_usage_context,
    detached_usage_context,
    set_usage_sink,
    usage_context,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator


class _UsageAdapter:
    """回固定 usage 的最小 adapter（不继承 Protocol，结构化满足即可）。"""

    def __init__(self, *, tokens_in: int = 120, tokens_out: int = 30, fail: bool = False) -> None:
        self._in, self._out, self._fail = tokens_in, tokens_out, fail

    async def invoke(self, req: LLMRequest) -> LLMResponse:
        if self._fail:
            raise RuntimeError("provider down")
        return LLMResponse(
            content="ok",
            usage=TokenUsage(input_tokens=self._in, output_tokens=self._out, cached_tokens=7),
        )

    async def stream(self, req: LLMRequest) -> AsyncIterator[LLMChunk]:
        yield LLMChunk(delta="par")
        yield LLMChunk(delta="tial")
        # Most providers attach usage to the final chunk, alongside finish_reason.
        yield LLMChunk(
            finish_reason="stop",
            usage=TokenUsage(input_tokens=self._in, output_tokens=self._out),
        )


class _CumulativeUsageAdapter:
    """每个 chunk 都回**累计** usage 的 adapter —— 部分 provider 就是这个形状。

    逐 chunk 记账会把用量放大成 O(chunk 数)，所以 Router 必须只记最后一次。
    """

    async def invoke(self, req: LLMRequest) -> LLMResponse:  # pragma: no cover - This test uses only stream.
        raise NotImplementedError

    async def stream(self, req: LLMRequest) -> AsyncIterator[LLMChunk]:
        for i in (1, 2, 3):
            yield LLMChunk(delta="x", usage=TokenUsage(input_tokens=100, output_tokens=10 * i))
        yield LLMChunk(finish_reason="stop", usage=TokenUsage(input_tokens=100, output_tokens=30))


def _router(adapter: object, *, agent: str = "fast", owner_team_id: str | None = None) -> Router:
    """真 Router + 真 registry, 只把内建 mock adapter 换成回固定 usage 的替身。

    走 registry.load 而非直接塞 dict, 保证 config/adapter 两张表一致(reload/close_all 依赖它)。
    """
    registry = ProviderRegistry()
    registry.load([ProviderConfig(name="p1", type="mock")])
    registry._adapters["p1"] = adapter  # type: ignore[assignment]  # noqa: SLF001 — Inject a stand-in adapter.
    router = Router(registry, owner_team_id=owner_team_id)
    router.bind(AgentBinding(agent_type=agent, primary=("p1", "m1")))
    return router


@pytest.fixture
def sink() -> Iterator[list[LLMUsageRecord]]:
    """装一个收集型 sink，退出时还原上一个（避免污染其他测试）。"""
    seen: list[LLMUsageRecord] = []
    previous = set_usage_sink(seen.append)
    try:
        yield seen
    finally:
        set_usage_sink(previous)


def _req() -> LLMRequest:
    return LLMRequest(model="", messages=[Message(role="user", content="hi")])


@pytest.mark.asyncio
async def test_invoke_records_usage_with_provider_and_model(sink: list[LLMUsageRecord]) -> None:
    r = _router(_UsageAdapter())
    resp = await r.invoke("fast", _req())

    assert resp.content == "ok"
    assert len(sink) == 1
    rec = sink[0]
    assert (rec.agent_type, rec.provider, rec.model) == ("fast", "p1", "m1")
    assert (rec.tokens_input, rec.tokens_output, rec.tokens_cached) == (120, 30, 7)
    assert rec.tokens_total == 150
    assert rec.streamed is False


@pytest.mark.asyncio
async def test_no_sink_registered_is_a_noop() -> None:
    """★ 这一刀不改任何现有行为: 没人注册 sink 时调用路径与改前等价。"""
    previous = set_usage_sink(None)
    try:
        r = _router(_UsageAdapter())
        resp = await r.invoke("fast", _req())
        assert resp.content == "ok"  # Return normally without raising or altering the response.
    finally:
        set_usage_sink(previous)


@pytest.mark.asyncio
async def test_turn_usage_sink_receives_usage_without_global_recorder() -> None:
    """Stop accounting is per-Turn and cannot depend on the telemetry recorder."""
    partial: list[LLMUsageRecord] = []
    previous = set_usage_sink(None)
    try:
        r = _router(_UsageAdapter(tokens_in=17, tokens_out=3))
        with usage_context(
            turn_id="t_stop_partial",
            reason="turn",
            turn_usage_sink=partial.append,
        ):
            await r.invoke("fast", _req())
    finally:
        set_usage_sink(previous)

    assert [(record.tokens_input, record.tokens_output) for record in partial] == [(17, 3)]


@pytest.mark.asyncio
async def test_detached_usage_context_keeps_attribution_but_clears_turn_sink(
    sink: list[LLMUsageRecord],
) -> None:
    partial: list[LLMUsageRecord] = []
    r = _router(_UsageAdapter(tokens_in=17, tokens_out=3))

    with (
        usage_context(
            team_id="team_1",
            user_id="user_1",
            turn_id="turn_1",
            reason="turn",
            turn_usage_sink=partial.append,
        ),
        detached_usage_context(reason="session_title"),
    ):
        await r.invoke("fast", _req())

    assert partial == []
    assert len(sink) == 1
    assert sink[0].context.reason == "session_title"
    assert sink[0].context.team_id == "team_1"
    assert sink[0].context.turn_id == "turn_1"


@pytest.mark.asyncio
async def test_sink_exception_never_breaks_the_llm_call() -> None:
    """★ 记账失败必须被吞: 宁可丢一条用量记录, 不能让记账把 turn 弄挂。"""

    def _boom(_rec: LLMUsageRecord) -> None:
        raise RuntimeError("ledger on fire")

    previous = set_usage_sink(_boom)
    try:
        r = _router(_UsageAdapter())
        resp = await r.invoke("fast", _req())
        assert resp.content == "ok"
    finally:
        set_usage_sink(previous)


@pytest.mark.asyncio
async def test_zero_token_usage_is_not_recorded(sink: list[LLMUsageRecord]) -> None:
    """mock adapter / 不回 usage 的 provider 不该刷出一堆空行。"""
    r = _router(_UsageAdapter(tokens_in=0, tokens_out=0))
    await r.invoke("fast", _req())
    assert sink == []


@pytest.mark.asyncio
async def test_failed_invoke_records_nothing(sink: list[LLMUsageRecord]) -> None:
    """provider 全挂 → 抛给调用方, 且不记用量(没成功调用就没有用量)。"""
    r = _router(_UsageAdapter(fail=True))
    with pytest.raises(RuntimeError):
        await r.invoke("fast", _req())
    assert sink == []


@pytest.mark.asyncio
async def test_stream_records_usage_from_final_chunk(sink: list[LLMUsageRecord]) -> None:
    r = _router(_UsageAdapter())
    chunks = [c async for c in r.stream("fast", _req())]

    assert len(chunks) == 3
    assert len(sink) == 1, "只有带 usage 的末 chunk 记一次, 不该逐 chunk 重复记"
    assert sink[0].streamed is True
    assert sink[0].tokens_total == 150


@pytest.mark.asyncio
async def test_usage_context_attributes_the_call(sink: list[LLMUsageRecord]) -> None:
    r = _router(_UsageAdapter())
    with usage_context(team_id="t_1", user_id="u_1", workspace_id="ws_1", turn_id="t_abc", reason="turn"):
        await r.invoke("fast", _req())

    ctx = sink[0].context
    assert (ctx.team_id, ctx.user_id, ctx.workspace_id, ctx.turn_id, ctx.reason) == (
        "t_1",
        "u_1",
        "ws_1",
        "t_abc",
        "turn",
    )


@pytest.mark.asyncio
async def test_unset_context_records_as_unattributed(sink: list[LLMUsageRecord]) -> None:
    """没设上下文仍要记 —— 「有调用但归属不明」本身就是要暴露的信号。

    注：这是**进程级装配 router**（无 owner_team_id）的形态；团队 router 见下面
    gap23 那组用例——它即便没有 usage_context 也能归属。
    """
    r = _router(_UsageAdapter())
    await r.invoke("fast", _req())
    # 2026-08-10: reason is no longer a bare `unattributed`; it includes a call-site fingerprint.
    # The 116 database had 286 unattributed rows (9%), growing daily, all without workspace/turn_id.
    # A single generic bucket prevented tracing callers afterward, leaving only guesses among 14 candidates.
    # Preserve the prefix for existing aggregation; assert both the prefix and a nonempty fingerprint.
    reason = sink[0].context.reason
    assert reason.startswith(UNATTRIBUTED + ":"), reason
    fingerprint = reason.split(":", 1)[1]
    assert fingerprint and fingerprint != "unknown", f"指纹应指向真实调用点，实得 {fingerprint!r}"
    assert "." in fingerprint, f"指纹应为 `模块.函数`，实得 {fingerprint!r}"
    assert sink[0].context.team_id is None


# ── credit gap23 step 1: attribution follows the router, not the call site ──────────────────


@pytest.mark.asyncio
async def test_team_router_attributes_without_any_usage_context(sink: list[LLMUsageRecord]) -> None:
    """★ 本刀的核心判据：**没有任何 `usage_context`，用量依然归属到团队**。

    这正是定时任务 / canvas 改写 / KB 摄取等 20+ 入口过去落成无主行的场景——
    修法不是让它们各自申报，而是让 router 自带出生证明。
    """
    r = _router(_UsageAdapter(), owner_team_id="team_owner")
    await r.invoke("fast", _req())

    ctx = sink[0].context
    assert ctx.team_id == "team_owner"  # No more ownerless records.
    # reason still has the unreported prefix, but includes a call-site fingerprint since 2026-08-10.
    # This fulfills the promised refinement: identify the caller during metering, without call-site declarations.
    assert ctx.reason.startswith(UNATTRIBUTED), ctx.reason


@pytest.mark.asyncio
async def test_stream_path_also_attributes_from_router(sink: list[LLMUsageRecord]) -> None:
    """stream 与 invoke 是两个独立记账点，必须都带上归属（否则 canvas 改写那条漏）。"""
    r = _router(_UsageAdapter(), owner_team_id="team_owner")
    async for _ in r.stream("fast", _req()):
        pass

    assert sink[0].streamed is True
    assert sink[0].context.team_id == "team_owner"


@pytest.mark.asyncio
async def test_usage_context_wins_over_router_owner(sink: list[LLMUsageRecord]) -> None:
    """★ 既有行为一字不变：显式申报的 team 优先，router 只做兜底。

    turn 路径（chat.py 的 `usage_context`）因此完全不受影响。
    """
    r = _router(_UsageAdapter(), owner_team_id="team_from_router")
    with usage_context(team_id="team_from_context", reason="turn"):
        await r.invoke("fast", _req())

    ctx = sink[0].context
    assert ctx.team_id == "team_from_context"
    assert ctx.reason == "turn"


@pytest.mark.asyncio
async def test_router_owner_fills_team_but_keeps_other_context_fields(sink: list[LLMUsageRecord]) -> None:
    """只补 team_id 这一个字段，不碰调用方申报的其余轴（reason/turn_id…）。"""
    r = _router(_UsageAdapter(), owner_team_id="team_owner")
    with usage_context(reason="dreaming", turn_id="t_abc"):
        await r.invoke("fast", _req())

    ctx = sink[0].context
    assert (ctx.team_id, ctx.reason, ctx.turn_id) == ("team_owner", "dreaming", "t_abc")


def test_assembly_router_carries_no_owner() -> None:
    """进程级装配 router 必须保持无归属——它在 prod 是 RefusalAdapter，被调即缺陷。"""
    assert _router(_UsageAdapter()).owner_team_id is None


@pytest.mark.asyncio
async def test_nested_context_only_overrides_given_fields(sink: list[LLMUsageRecord]) -> None:
    """后台子流程只改 reason, team/user/turn 继承外层 —— 不必层层重复穿参。"""
    r = _router(_UsageAdapter())
    with usage_context(team_id="t_1", user_id="u_1", turn_id="t_abc", reason="turn"), usage_context(reason="session_title"):
        await r.invoke("fast", _req())

    ctx = sink[0].context
    assert ctx.reason == "session_title"
    assert (ctx.team_id, ctx.user_id, ctx.turn_id) == ("t_1", "u_1", "t_abc")


@pytest.mark.asyncio
async def test_context_restored_after_block() -> None:
    with usage_context(team_id="t_1", reason="turn"):
        assert current_usage_context().reason == "turn"
    assert current_usage_context().reason == UNATTRIBUTED
    assert current_usage_context().team_id is None


@pytest.mark.asyncio
async def test_context_propagates_into_spawned_task(sink: list[LLMUsageRecord]) -> None:
    """★ fire-and-forget 的后台调用(dreaming 就是这个形状)必须继承派生时的上下文。"""
    r = _router(_UsageAdapter())

    async def _bg() -> None:
        await r.invoke("fast", _req())

    with usage_context(team_id="t_1", user_id="u_1", reason="dreaming"):
        task = asyncio.create_task(_bg())
    await task  # await is outside the context block; attribution must still come from when create_task was called.

    assert sink[0].context.reason == "dreaming"
    assert sink[0].context.team_id == "t_1"


@pytest.mark.asyncio
async def test_stream_with_cumulative_usage_records_once_not_per_chunk(sink: list[LLMUsageRecord]) -> None:
    """★ 回归: 每 chunk 都回累计 usage 的 provider 不能被记成 4 条 / 放大 4 倍。"""
    r = _router(_CumulativeUsageAdapter())
    chunks = [c async for c in r.stream("fast", _req())]

    assert len(chunks) == 4
    assert len(sink) == 1, f"应只记一次, 实际 {len(sink)} 次（逐 chunk 记账会放大用量）"
    assert sink[0].tokens_total == 130, "应取最后一次累计值(100+30), 不是各 chunk 之和"


@pytest.mark.asyncio
async def test_stream_records_usage_even_if_consumer_breaks_early(sink: list[LLMUsageRecord]) -> None:
    """调用方中途 break（async gen 被 aclose）时, 已见到的用量仍要记 —— 不能整段漏账。"""
    r = _router(_CumulativeUsageAdapter())
    gen = r.stream("fast", _req())
    seen = 0
    async for _chunk in gen:
        seen += 1
        if seen == 2:
            break
    await gen.aclose()

    assert len(sink) == 1
    assert sink[0].tokens_total == 120, "取 break 前最后见到的累计值(100+20)"
