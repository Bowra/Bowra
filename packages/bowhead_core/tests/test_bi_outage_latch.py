"""Latch asynchronous BI timeouts within one execution context.

Use BIAsyncTaskTimeout rather than matching variable exception prose. Repeated
sequential queries must not each consume a full polling window during an outage.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry
from bowhead_router.types import ToolCall

from bowhead_bi_bridge import BIAsyncTaskTimeout, BIConnectionError


def _ctx(datarc: MagicMock) -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        # ⚠ Each _ctx() must represent a **new turn**: bi_query has a process-level turn-scoped cache
        # (same turn/query reuses results without datarc). Several cases here share one
        # default question; a fixed turn_id would let later cases read earlier successful results,
        # causing false failures such as "DID NOT RAISE" / await_count=0. Each case is already
        # semantically an independent turn.
        turn_id=f"t-outage-{uuid.uuid4().hex[:8]}",
        datarc=datarc,
        session_id="s1",
    )


def _ok_download() -> MagicMock:
    d = MagicMock()
    d.rows = [{"city": "上海", "gmv": 100}]
    d.columns = ["city", "gmv"]
    d.total_rows = 1
    d.csv_filename = "rows.csv"
    d.task_uid = "task_1"
    d.download_ms = 10
    return d


async def _call(ctx: ToolExecutionContext, text: str = "2026年营业额 按城市"):
    return await build_default_registry().get("bi_query").handler({"text": text}, ctx)


@pytest.mark.asyncio
async def test_async_task_timeout_sets_the_latch() -> None:
    datarc = MagicMock()
    datarc.download_data = AsyncMock(side_effect=BIAsyncTaskTimeout("poll timeout after 60.0s"))
    ctx = _ctx(datarc)

    with pytest.raises(BIAsyncTaskTimeout):
        await _call(ctx)

    assert ctx.bi_outage.latched_reason, "第一次异步任务超时后必须上闸"


@pytest.mark.asyncio
async def test_second_call_fails_fast_without_touching_datarc() -> None:
    """闸的**唯一价值**就在这条：第二次不再付一个轮询窗口。"""
    datarc = MagicMock()
    datarc.download_data = AsyncMock(side_effect=BIAsyncTaskTimeout("poll timeout after 60.0s"))
    ctx = _ctx(datarc)

    with pytest.raises(BIAsyncTaskTimeout):
        await _call(ctx)
    with pytest.raises(BIAsyncTaskTimeout):
        await _call(ctx, "换个说法再查一次")

    assert datarc.download_data.await_count == 1, "上闸后不该再打 datarc（否则又是一个 60 秒）"


@pytest.mark.asyncio
async def test_fast_fail_message_tells_the_model_not_to_retry() -> None:
    """跨 agent 那一半靠这段文案兜——Lead 与 peer 各有自己的 ctx，闸不共享。

    ⚠ 2026-09-03 改判据：原来钉的是「不要换措辞重试」这句**具体措辞**，而 116 取证
    证明换措辞恰恰是唯一有效的解法（去掉「明细」133 秒 → 4 秒）。这条要保护的从来
    不是那句话，是「别继续一条条烧 60 秒窗口」这个行为。判据改成后者。
    """
    datarc = MagicMock()
    datarc.download_data = AsyncMock(side_effect=BIAsyncTaskTimeout("poll timeout after 60.0s"))
    ctx = _ctx(datarc)

    with pytest.raises(BIAsyncTaskTimeout):
        await _call(ctx)
    with pytest.raises(BIAsyncTaskTimeout) as caught:
        await _call(ctx)

    msg = str(caught.value)
    assert "未返回" in msg, "必须说清发生了什么（观测事实）"
    assert "别再发" in msg and "60 秒" in msg, "必须让模型知道再发就是再烧一个窗口"
    assert "不要换措辞" not in msg, "换措辞恰恰是解法（116 取证），不许再劝阻"
    # ★★ Pin the no-attribution decision: one timeout is ambiguous (upstream outage or simply
    # a heavy query); asserting a cause misleads users in the latter case. Observations alone are true in both.
    assert "服务不可用" not in msg and "服务暂时不可用" not in msg, "文案不许把「超时」推断成「服务不可用」——那是会误报的归因"


@pytest.mark.asyncio
async def test_latched_error_is_still_a_biconnectionerror() -> None:
    """既有 `except BIConnectionError` 的调用点必须照旧接得住，失败语义不变。"""
    datarc = MagicMock()
    datarc.download_data = AsyncMock(side_effect=BIAsyncTaskTimeout("poll timeout after 60.0s"))
    ctx = _ctx(datarc)

    with pytest.raises(BIAsyncTaskTimeout):
        await _call(ctx)
    with pytest.raises(BIConnectionError):
        await _call(ctx)


# ─── Positive controls: two cases that must not latch ──────────────────────────────────


@pytest.mark.asyncio
async def test_success_does_not_latch() -> None:
    """正对照：查询正常时闸必须保持关着，第二次照旧真打 datarc。"""
    datarc = MagicMock()
    datarc.download_data = AsyncMock(return_value=_ok_download())
    ctx = _ctx(datarc)

    await _call(ctx)
    assert ctx.bi_outage.latched_reason == "", "成功的查询不该上闸"

    await _call(ctx, "再查一次")
    assert datarc.download_data.await_count == 2, "没上闸时第二次必须照旧真查"


@pytest.mark.asyncio
async def test_other_connection_errors_do_not_latch() -> None:
    """正对照：OBS 下载失败 / 网络抖动是**单条**查询的偶发，不代表服务整体不可用。

    ⚠ 这条钉的是判据的**窄度**：若改成"任何 BIConnectionError 都上闸"，
    一次偶发抖动会把整轮后续查询全部掐掉。
    """
    datarc = MagicMock()
    datarc.download_data = AsyncMock(side_effect=BIConnectionError("download_data: OBS 503"))
    ctx = _ctx(datarc)

    with pytest.raises(BIConnectionError):
        await _call(ctx)

    assert ctx.bi_outage.latched_reason == "", "非异步任务超时不该上闸"

    datarc.download_data = AsyncMock(return_value=_ok_download())
    # ⚠ Do not use placeholder questions such as "重试": bi_query first sanity-checks meta/help text,
    # returning immediately without datarc if classified as meta, falsely failing await_count assertions below.
    await _call(ctx, "2026年营业额 按门店")
    assert datarc.download_data.await_count == 1, "没上闸 ⇒ 后续查询照旧真打 datarc"


# Exercise real peer dispatch, which shallow-copies the execution context.
# A mutable marker must survive that boundary; direct handler tests alone would
# not catch a scalar marker being written only to the copied context.


def _peer_ctx(datarc: MagicMock) -> ToolExecutionContext:
    ctx = ToolExecutionContext(
        workspace_id="1",
        # Real execute() checks connector context; without it, dispatch short-circuits before datarc.
        active_datarc_project="1",
        member_uid="m",
        user_id="u",
        turn_id="t-outage-latch",
        datarc=datarc,
        session_id="s1",
    )
    ctx.role = "lead"  # turn_runner sets _peer_ctx this way; non-None triggers role-swap.
    return ctx


async def _dispatch_as_peer(ctx: ToolExecutionContext, n: int) -> None:
    runner = SubagentRunner(router=None, tool_registry=build_default_registry())  # type: ignore[arg-type]
    bus = TurnEventBus(None)
    for i in range(n):
        await runner._execute_tool(
            ToolCall(id=f"call_{i}", name="bi_query", arguments={"text": f"查询 {i}"}),
            ctx,
            bus,
            "t",
            "sub_x",
            running_profile="marketing-analyst",  # != "lead" ⇒ role-swap ⇒ copy.copy(ctx)
        )


@pytest.mark.asyncio
async def test_latch_survives_peer_role_swap_shallow_copy() -> None:
    """peer 连发两条：第二条必须被闸挡住，不能再打 datarc。

    突变：把 `bi_outage` 从可变载体改回 ctx 上的标量 → 这条**必红**（第一版就是那样）。
    """
    datarc = MagicMock()
    datarc.download_data = AsyncMock(side_effect=BIAsyncTaskTimeout("poll timeout after 60.0s"))
    ctx = _peer_ctx(datarc)

    await _dispatch_as_peer(ctx, 2)

    assert datarc.download_data.await_count == 1, "peer 路径闸失效——第二条又付了一个 60 秒窗口（浅拷贝丢标记）"
    assert ctx.bi_outage.latched_reason, "标记必须落在共享 ctx 上，不是浅拷贝上"


@pytest.mark.asyncio
async def test_peer_path_positive_control_success_still_queries_twice() -> None:
    """正对照：同一条 peer 派发路径上，查询正常时两次都必须真打 datarc。

    没有它，上一条可能是被别的东西（连接器闸/角色闸）挡住的，而不是被我的闸挡住的。
    """
    datarc = MagicMock()
    datarc.download_data = AsyncMock(return_value=_ok_download())
    ctx = _peer_ctx(datarc)

    await _dispatch_as_peer(ctx, 2)

    assert datarc.download_data.await_count == 2, "正常路径被误挡——闸太宽或前置闸拦下了"


# ── Row-level fetch timeouts must not latch (2026-09-03, 116 evidence) ──────────────────
#
# Observed on 116: same data/dimensions/metrics, "…各种类大类营业额" took 4.0 seconds / 38 rows,
# "…营业额按种类大类**明细**" took 133.1 seconds / **1,000,000 rows**.
# Production comparison at 21:44: five tasks in one batch, three finished in 0–1 seconds, two in 224/304 seconds;
# the slow pair had only one additional `keyword_detail` token in params and **both eventually succeeded**
# (status=2 / result zips present / empty errors), after we had given up.
#
# ⇒ BI was available; the query needed minutes. The old latch also blocked the model's rewritten
#   aggregate query that could finish in four seconds, shutting down the sole solution.


async def _timeout_call(ctx: ToolExecutionContext, text: str):
    with pytest.raises(BIAsyncTaskTimeout) as ei:
        await _call(ctx, text)
    return str(ei.value)


@pytest.mark.asyncio
async def test_row_level_timeout_does_not_latch() -> None:
    """★★ 带「明细」的超时**不上闸**——否则改写后的聚合查询也发不出去。"""
    datarc = MagicMock()
    datarc.download_data = AsyncMock(side_effect=BIAsyncTaskTimeout("poll timeout after 60.0s"))
    ctx = _ctx(datarc)

    msg = await _timeout_call(ctx, "列出2025年每个品牌的营业额明细表")

    assert not ctx.bi_outage.latched_reason, "行级取数超时不该上闸"
    assert "明细" in msg and "聚合" in msg, msg


@pytest.mark.asyncio
async def test_row_level_timeout_still_lets_the_rewritten_query_through() -> None:
    """★★ 这条才是重点：改写成聚合问法之后，那一条必须真的发得出去。"""
    datarc = MagicMock()
    datarc.download_data = AsyncMock(side_effect=BIAsyncTaskTimeout("poll timeout after 60.0s"))
    ctx = _ctx(datarc)
    await _timeout_call(ctx, "2025年各品牌营业额明细")

    # Rewritten query succeeds (proving the latch did not block it).
    datarc.download_data = AsyncMock(return_value=_ok_download())
    out = await _call(ctx, "2025年各品牌营业额")
    assert out.get("ok") is not False, out
    assert datarc.download_data.await_count == 1, "改写后的查询根本没被发出去"


@pytest.mark.asyncio
async def test_plain_query_timeout_still_latches() -> None:
    """负对照：不带行级词的超时仍然上闸（真 outage 的场景没被放宽）。"""
    datarc = MagicMock()
    datarc.download_data = AsyncMock(side_effect=BIAsyncTaskTimeout("poll timeout after 60.0s"))
    ctx = _ctx(datarc)

    await _timeout_call(ctx, "2026年各城市营业额")

    assert ctx.bi_outage.latched_reason, "非行级查询超时仍应上闸"


@pytest.mark.asyncio
async def test_no_message_tells_the_model_not_to_rephrase() -> None:
    """★ 旧文案写着「请不要换措辞重试」——而换措辞恰恰是唯一有效的解法。

    它与旧行为是自洽的（闸拦住一切，换措辞确实没用），两者一起错。
    这条钉住那句话不许回来。
    """
    datarc = MagicMock()
    datarc.download_data = AsyncMock(side_effect=BIAsyncTaskTimeout("poll timeout after 60.0s"))

    plain = await _timeout_call(_ctx(datarc), "2026年各城市营业额")
    rowlevel = await _timeout_call(_ctx(datarc), "2026年营业额明细")
    latched_ctx = _ctx(datarc)
    await _timeout_call(latched_ctx, "2026年各城市营业额")
    latched = await _timeout_call(latched_ctx, "2026年各省营业额")

    for msg in (plain, rowlevel, latched):
        assert "不要换措辞" not in msg, msg
