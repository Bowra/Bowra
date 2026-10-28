"""report-streaming P1 —— `canvas.turn_streaming` 由 bus 补发，exactly-once。

**为什么这组测试站在 bus 上而不是组件上**：D2 等待带 / D3 横幅原来的 20 条组件测试
全都直接把 `turnId` 当 prop 传进去，于是全绿；而 prod 上 `turnId` 恒 `null`，因为它只由
`canvas.turn_streaming` 写入，而该事件在现役主路（单写手 `report_writer_loop` +
`canvas_tools`）从不发 —— 116 实测 141 个有画布产物的 turn 里只有 3 个发过。
两批功能 100% 空转、测试零红。见 [[feedback_test_fixture_supplies_what_caller_wont]]。

所以这里测的是**闸本身**：「本 turn 第一条画布产内容事件之前，总线上有没有那一帧」。
"""

from __future__ import annotations

import pytest
from bowhead_core.events import (
    OrchestratorEvent,
    TerminalDurabilityAbort,
    TurnEventBus,
)

TURN = "t_backfill_0001"


def _ev(event_type: str, **payload: object) -> OrchestratorEvent:
    return OrchestratorEvent(event_type=event_type, turn_id=TURN, payload=dict(payload))  # type: ignore[arg-type]


class _RecordingBus(TurnEventBus):
    """真 bus + 记录 log_sink 实际收到的序列。

    用 `log_sink` 而不是订阅者来记录，是因为它同时证明**补发的那一帧会落库**——
    F5 重放走的是持久化的行，不是 live ring（live == persist == replay 不能破）。
    """

    def __init__(self) -> None:
        self.persisted: list[OrchestratorEvent] = []

        async def _sink(e: OrchestratorEvent) -> None:
            self.persisted.append(e)

        super().__init__(log_sink=_sink)

    @property
    def types(self) -> list[str]:
        return [e.event_type for e in self.persisted]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "opener",
    ["canvas.opened", "canvas.section_streaming", "canvas.section_added", "canvas.section_chunk"],
)
async def test_first_canvas_content_event_backfills_turn_streaming(opener: str) -> None:
    """四个「产内容」事件里的任意一个，都要在它之前补出整 turn 锁。"""
    bus = _RecordingBus()
    await bus.emit(_ev(opener))

    assert bus.types == ["canvas.turn_streaming", opener], f"{opener} 之前必须先有 canvas.turn_streaming（FE 的 currentTurnId 只由它写入）"
    first = bus.persisted[0]
    assert first.turn_id == TURN
    assert first.payload["turn_id"] == TURN, "payload 必须带 turn_id —— FE reducer 读的就是它"
    assert first.seq < bus.persisted[1].seq, "补发帧的 seq 必须严格早于触发它的那条"


@pytest.mark.anyio
async def test_backfill_is_exactly_once_across_a_whole_turn() -> None:
    """一轮真实报告 turn 的形状（图表 → 报告头 → 正文）只应有一帧锁。"""
    bus = _RecordingBus()
    for e in (
        _ev("canvas.section_added", section={"kind": "code_block"}),
        _ev("canvas.section_streaming", section_id="s1", kind="image"),
        _ev("canvas.section_added", section={"kind": "chart"}),
        _ev("canvas.section_added", section={"kind": "kpi_row"}),
        _ev("canvas.section_chunk", section_id="s2", delta="半句话"),
        _ev("canvas.section_added", section={"kind": "narrative"}),
    ):
        await bus.emit(e)

    assert bus.types.count("canvas.turn_streaming") == 1
    assert bus.types[0] == "canvas.turn_streaming"


@pytest.mark.anyio
async def test_explicit_emit_is_honoured_then_deduped() -> None:
    """显式发过就不再补；重复显式发被压掉。

    压重复不是洁癖：`onTurnStreaming` 会重置 `streamingSectionIds` /
    `cancelledSectionIds`，turn 中途再来一发会清掉正在流式的段落标记，而批 A 的
    草稿槽此刻很可能正在流。
    """
    bus = _RecordingBus()
    await bus.emit(_ev("canvas.turn_streaming", turn_id=TURN))
    await bus.emit(_ev("canvas.section_added", section={"kind": "chart"}))
    await bus.emit(_ev("canvas.turn_streaming", turn_id=TURN))  # Reemit midway.
    await bus.emit(_ev("canvas.section_chunk", section_id="s1", delta="x"))

    assert bus.types == [
        "canvas.turn_streaming",
        "canvas.section_added",
        "canvas.section_chunk",
    ], "中途那次重发必须被压掉，且不得触发第二次补发"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "event_type",
    [
        # Pure chat: no canvas output means no canvas streaming state (D2 critical invariant ①).
        "turn_started",
        "partial_answer",
        "tool_result",
        "final_answer",
        # Editing: REST path; user awaits no analysis turn.
        "canvas.section_patched",
        "canvas.reordered",
    ],
)
async def test_non_content_events_do_not_open_the_lock(event_type: str) -> None:
    bus = _RecordingBus()
    await bus.emit(_ev(event_type))
    assert bus.types == [event_type], f"{event_type} 不该凭空给画布上锁"


@pytest.mark.anyio
async def test_backfill_reaches_live_subscribers_too() -> None:
    """live 路径与持久化路径必须看到同一序列（chat 不变量）。"""
    bus = _RecordingBus()
    await bus.emit(_ev("canvas.section_added", section={"kind": "chart"}))
    await bus.close()

    live = [e.event_type async for e in bus.subscribe()]
    assert live[:2] == ["canvas.turn_streaming", "canvas.section_added"]
    assert live == bus.types, "live 与 persist 的序列必须一致"


@pytest.mark.anyio
async def test_closed_bus_does_not_backfill() -> None:
    """bus 已关就整条不发——补发不能反过来把终态之后的帧塞回去。"""
    bus = _RecordingBus()
    await bus.close()
    delivered = await bus.emit(_ev("canvas.section_added", section={"kind": "chart"}))
    assert delivered is False
    assert bus.types == []


@pytest.mark.anyio
async def test_rejected_synthetic_opener_fences_outer_canvas_event() -> None:
    """A failed synthetic opener cannot be overtaken by its canvas payload."""
    attempted: list[str] = []

    async def reject_first(event: OrchestratorEvent) -> bool:
        attempted.append(event.event_type)
        return False

    bus = TurnEventBus(
        log_sink=reject_first,
        persist_before_live=True,
    )

    with pytest.raises(TerminalDurabilityAbort, match="legacy durable event write has failed"):
        await bus.emit(_ev("canvas.section_added", section={"kind": "chart"}))

    assert attempted == ["canvas.turn_streaming"]
    assert bus.snapshot() == []
    with pytest.raises(TerminalDurabilityAbort, match="legacy durable event write has failed"):
        await bus.emit(_ev("canvas.section_chunk", section_id="s1", delta="x"))
