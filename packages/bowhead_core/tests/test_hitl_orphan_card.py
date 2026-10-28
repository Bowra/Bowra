"""2026-09-04 HITL 审计 §5 — 不可答的孤儿卡。

三处在 ClarificationStore 落库失败后仍然 emit `clarification_required`，用的是
`_gen_clarification_id` 那个从不入库的本地串（`clar_{turn}_{ms}`）。用户点了恒 404，
而这一轮已经 done，卡片就是唯一出口 —— 死路一条。peer bail 路早就修过同一个坑
（注释：「we cannot emit an unresolvable card」），另外三处没跟上。

本文件钉住修好之后的不变量：**发出去的卡，背后一定有行**。
- CH2 路：哨兵证明不了有行 → 补写；补不上 → 不发卡、按普通完成收尾。
（heuristic 预闸 2026-09-08 整条拆除，它那两条守卫随之删掉；CH2 这条仍然有效。）
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.agents.clarification_tool import make_clarification_sentinel
from bowhead_core.agents.lead_orchestrator import LeadRuntime, _lead_loop
from bowhead_core.clarification_store import ClarificationStore
from bowhead_core.subagent import SubagentResult
from bowhead_core.types import ToolInvocation, TurnStatus
from test_lead_hitl_fix_ch1_ch2 import (  # noqa: F401 — bring in the autouse fixture too
    _ensure_lead_profile,
    _FakeBus,
    _FakeSubagentRunner,
    _make_ctx,
    _make_sctx,
)


@pytest.fixture
def _stream_env(monkeypatch):
    """与 test_lead_hitl_fix_ch1_ch2 同款（那边有为什么不能关流式的长注释）。

    这里重新定义而不是 import：import 进来的 fixture 名会与同名参数撞成 F811。
    """
    monkeypatch.setenv("BOWHEAD_STREAMING_LEAD", "1")
    monkeypatch.setenv("BOWHEAD_LEAD_POST_PEER_BREAK", "0")


class _ExplodingStore:
    """落库恒失败的 ClarificationStore —— 磁盘满 / schema 漂移 / 连接断的现场形状。"""

    def __init__(self) -> None:
        self.put_calls = 0

    async def put(self, **_kw: Any) -> Any:
        self.put_calls += 1
        raise RuntimeError("clarification store unavailable")

    async def set_resume_context(self, *_a: Any, **_kw: Any) -> bool:
        raise RuntimeError("clarification store unavailable")


def _inv(tool_name: str, result_dict: dict[str, Any]) -> ToolInvocation:
    return ToolInvocation(tool_name=tool_name, arguments={}, result=result_dict)


def _sub_result(invocations: list[ToolInvocation], final_answer: str) -> SubagentResult:
    return SubagentResult(
        profile="lead",
        subagent_id="sub_x",
        ok=True,
        final_answer=final_answer,
        tokens_input=10,
        tokens_output=5,
        tool_invocations=invocations,
    )


def _runtime(bus: Any, ctx: Any, runner: Any) -> LeadRuntime:
    return LeadRuntime(
        router=SimpleNamespace(),
        tool_registry=SimpleNamespace(to_schemas=list, has=lambda *_: False),
        parent_bus=bus,
        parent_ctx=ctx,
        subagent_runner=runner,
    )


# ═══════════════════ CH2 (clarification_ask tool path) ═══════════════════


@pytest.mark.asyncio
async def test_ch2_put_failure_drops_card_and_falls_through(monkeypatch, _stream_env):
    """落库失败 → 不发卡，turn 不以 clarification_pending 收尾。

    降级形态是散文：Lead 在调工具前写的那句引导语（「客单价有几种算法，您指哪一个?」）
    本身就是问题，用户可以直接在输入框回答。发一张点不动的卡则什么都做不了。
    """
    bus = _FakeBus()
    store = _ExplodingStore()
    ctx = _make_ctx(clarification_store=store)

    sentinel = make_clarification_sentinel(
        candidates=["GMV / 订单数", "净销售额 / 订单数"],
        fallback_text="客单价用哪个公式?",
        aborted_turn_id="t_orphan_ch2",
        store_persisted=False,  # The tool did not persist it either.
    )
    runner = _FakeSubagentRunner(result=_sub_result([_inv("clarification_ask", sentinel)], "客单价有几种算法，您指哪一个?"))
    sctx = _make_sctx(turn_id="t_orphan_ch2")

    result = await _lead_loop(sctx, "客单价同比", _runtime(bus, ctx, runner))

    assert bus.find_all("clarification_required") == []
    assert result.status is not TurnStatus.CLARIFICATION_PENDING
    # Exactly one backfill was attempted, rather than silently abandoning it.
    assert store.put_calls == 1
    # The done event must not falsely report clarification pending.
    for ev in bus.find_all("done"):
        assert ev.payload.get("status") != "clarification_pending"


@pytest.mark.asyncio
async def test_ch2_put_failure_seeds_prose_when_lead_wrote_nothing(monkeypatch, _stream_env):
    """降级路不能变成空转：模型没写引导语时，把问题本身写成散文兜上。

    子 agent 撞哨兵就地 break，所以 final_answer 是「调工具之前」写了什么。工具描述
    要求先写一句引导语，但那是提示词约束，不是保证 —— 空的时候只发一个 done，
    用户这一轮什么都拿不到（和发一张点不动的卡一样是死路）。
    """
    bus = _FakeBus()
    ctx = _make_ctx(clarification_store=_ExplodingStore())

    sentinel = make_clarification_sentinel(
        candidates=["GMV / 订单数", "净销售额 / 订单数"],
        fallback_text="客单价用哪个公式?",
        aborted_turn_id="t_orphan_prose",
        store_persisted=False,
        questions=[
            {
                "question": "客单价用哪个公式?",
                "options": ["GMV / 订单数", "净销售额 / 订单数"],
                "type": "single_select",
            }
        ],
    )
    runner = _FakeSubagentRunner(result=_sub_result([_inv("clarification_ask", sentinel)], ""))  # The model wrote no text.
    sctx = _make_sctx(turn_id="t_orphan_prose")

    await _lead_loop(sctx, "客单价同比", _runtime(bus, ctx, runner))

    assert bus.find_all("clarification_required") == []
    texts = [str(ev.payload.get("text") or "") for ev in bus.find_all("final_answer")]
    body = "\n".join(texts)
    assert "客单价用哪个公式?" in body, f"问题必须落到正文里，实际: {body!r}"
    assert "GMV / 订单数" in body, f"选项也要给，否则用户不知道有哪些口径: {body!r}"


@pytest.mark.asyncio
async def test_ch2_backfills_row_when_tool_put_failed(monkeypatch, _stream_env):
    """工具落库失败但 CH2 补写成功 → 照常发卡，且 id 指向补写出来的真行。"""
    bus = _FakeBus()

    with tempfile.TemporaryDirectory() as td:
        store = ClarificationStore(db_path=Path(td) / "c.db")
        await store.initialize()
        ctx = _make_ctx(clarification_store=store)

        sentinel = make_clarification_sentinel(
            candidates=["华东", "华南"],
            fallback_text="哪个区域?",
            aborted_turn_id="t_orphan_backfill",
            clarification_id="clar_t_orphan_backfill_123",  # Local string left by the tool's failed write
            store_persisted=False,
        )
        runner = _FakeSubagentRunner(result=_sub_result([_inv("clarification_ask", sentinel)], "问一下区域"))
        sctx = _make_sctx(turn_id="t_orphan_backfill")

        result = await _lead_loop(sctx, "看看销量", _runtime(bus, ctx, runner))

        clar = bus.find_all("clarification_required")
        assert len(clar) == 1
        pending = await store.get_pending_for_turn("t_orphan_backfill")
        assert len(pending) == 1
        # Key invariant: the emitted id must be retrievable from the store.
        assert clar[0].payload["clarification_id"] == pending[0].request_id
        assert clar[0].payload["clarification_id"] != "clar_t_orphan_backfill_123"
        assert result.status is TurnStatus.CLARIFICATION_PENDING


@pytest.mark.asyncio
async def test_ch2_persisted_sentinel_still_skips_double_write(monkeypatch, _stream_env):
    """工具已写成（store_persisted=True）→ 不重复写，沿用它的 id（B3 防双写不变）。"""
    bus = _FakeBus()
    store = _ExplodingStore()  # Explodes on backfill, proving none occurs.
    ctx = _make_ctx(clarification_store=store)

    sentinel = make_clarification_sentinel(
        candidates=["营收", "销量"],
        fallback_text="哪个指标?",
        aborted_turn_id="t_no_double_write",
        clarification_id="req_real_row",
        store_persisted=True,
    )
    runner = _FakeSubagentRunner(result=_sub_result([_inv("clarification_ask", sentinel)], "问一下指标"))
    sctx = _make_sctx(turn_id="t_no_double_write")

    result = await _lead_loop(sctx, "客单价同比", _runtime(bus, ctx, runner))

    clar = bus.find_all("clarification_required")
    assert len(clar) == 1
    assert clar[0].payload["clarification_id"] == "req_real_row"
    assert store.put_calls == 0
    assert result.status is TurnStatus.CLARIFICATION_PENDING
