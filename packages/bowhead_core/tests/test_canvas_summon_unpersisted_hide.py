"""canvas-summon 止血 —— 「纯聊天轮闪一下 canvas」的终态裁决必须总是发得出来。

现场（116 prod 1.0.48 实测，见 docs/sprints/canvas-summon-2026-08-03/discovery.md）：

    canvas.section_streaming …            Lead 把终答拆成 sec_lead_* 段流给前端（前端 buffer-hide）
    canvas.done  section_count = 0        一条都没落库
    done { response_kind: "chat" }        **全程没有 canvas.sections_hidden**
    右栏出现 ← done 的 reveal 把 buffer 放出来了
    by-session 对账回包（0 段）→ 右栏消失     闪现 887ms

根因不是渲染抖动：前端把 keep/hide 的权威完全交给后端的 `canvas.sections_hidden`，
而 D6 只对**已落库**的段做判定（`if _d6_sections:`），纯聊天轮里那个列表恒为空 ⇒
裁决整段被跳过 ⇒ 前端等的信号在这条路径上结构性发不出来。

所以这里测的是「裁决的总覆盖」，不是某个 helper 的返回值：
走真的 `_emit_event(bus, tid, "done", …)`，断言事件真的上了 bus。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.agents.lead_orchestrator import _emit_event


class _RecordingBus:
    """够 `_emit_event` 用的最小 bus：记事件 + 带 D6 需要的那几个属性。"""

    def __init__(self, **d6_attrs: Any) -> None:
        self.events: list[Any] = []
        for k, v in d6_attrs.items():
            setattr(self, k, v)

    async def emit(self, event: Any) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.event_type for e in self.events]

    def payload_of(self, event_type: str) -> dict:
        for e in self.events:
            if e.event_type == event_type:
                return dict(e.payload or {})
        raise AssertionError(f"{event_type} 没发出来；实际发了 {self.types()}")


class _FakeCanvasStore:
    """`appended` = 本 turn 落库的段。纯聊天轮就是空的。"""

    def __init__(self, appended: list[Any] | None = None) -> None:
        self._appended = appended or []
        self.marked_hidden: list[str] = []

    async def list_sections_appended_by_turn(self, turn_id: str) -> list[Any]:
        return list(self._appended)

    async def mark_turn_hidden_for_chat_mode(self, turn_id: str, rationale: str) -> list[str]:
        ids = [getattr(s, "id", "") for s in self._appended]
        self.marked_hidden.extend(ids)
        return ids


def _section(sid: str, kind: str) -> Any:
    return SimpleNamespace(id=sid, kind=kind, payload={})


async def _emit_done(bus: _RecordingBus, turn_id: str = "t_flash") -> None:
    await _emit_event(bus, turn_id, "done", {"turn_id": turn_id, "status": "completed", "response_kind": "chat"})


@pytest.mark.asyncio
async def test_unpersisted_lead_sections_still_get_a_hide_verdict() -> None:
    """核心回归：段流过前端但一条没落库 → 仍必须发 canvas.sections_hidden 点名藏掉。

    修复前这里一个 canvas 事件都没有 → 前端 done 时 reveal → 闪现。
    """
    streamed = ["sec_lead_t_flash_001", "sec_lead_t_flash_002"]
    bus = _RecordingBus(
        _d6_canvas_store=_FakeCanvasStore(appended=[]),  # Unpersisted.
        _d6_turn_id="t_flash",
        _d6_streamed_lead_section_ids=list(streamed),
    )
    await _emit_done(bus)

    assert "canvas.sections_hidden" in bus.types(), bus.types()
    p = bus.payload_of("canvas.sections_hidden")
    assert p["hidden_section_ids"] == streamed
    assert p["turn_id"] == "t_flash"
    # FE onSectionsHidden removes sections by ID; decision must name them, never an empty list.
    assert p["hidden_section_ids"], "空名单等于没发（前端 handler 直接 return）"


@pytest.mark.asyncio
async def test_writer_and_reader_agree_on_the_bus_handoff() -> None:
    """跨接缝的一条：decomposer 流段 → `_stash_streamed_lead_section_ids` 写 bus →
    done-emit 的 D6 读同一个 bus。

    分开测两头都会绿，但只要那个 bus 属性名在写/读两侧漂了一个字，线上就退回闪现
    且**没有任何测试会红**。所以这里真跑一遍交接。
    """
    from bowhead_core.agents.lead_orchestrator import _stash_streamed_lead_section_ids
    from bowhead_core.canvas_composers.streaming import StreamingDecomposer

    bus = _RecordingBus(
        _d6_canvas_store=_FakeCanvasStore(appended=[]),
        _d6_turn_id="t_wire",
    )
    emitted: list[tuple[str, dict]] = []

    async def _emit(et: str, p: dict) -> None:
        emitted.append((et, p))

    sd = StreamingDecomposer(canvas_id="", turn_id="t_wire", emit=_emit, tool_invocations=[], question="")
    await sd.feed("# 上周各门店销售额\n\n| 门店 | 金额 |\n|---|---|\n| 东湖街 | 25,183,642 |\n")
    await sd.finalize()

    _stash_streamed_lead_section_ids(bus, sd)
    await _emit_done(bus, "t_wire")

    streamed = [p["section_id"] for et, p in emitted if et == "canvas.section_streaming"]
    assert streamed, "用例本身没流出段"
    assert bus.payload_of("canvas.sections_hidden")["hidden_section_ids"] == streamed


@pytest.mark.asyncio
async def test_no_streamed_sections_stays_silent() -> None:
    """对照组：Lead 一个段都没流过（真·纯问答）→ 无事可裁，不该凭空发事件。"""
    bus = _RecordingBus(
        _d6_canvas_store=_FakeCanvasStore(appended=[]),
        _d6_turn_id="t_quiet",
    )
    await _emit_done(bus, "t_quiet")
    assert "canvas.sections_hidden" not in bus.types(), bus.types()


@pytest.mark.asyncio
async def test_persisted_report_sections_are_not_touched() -> None:
    """报告轮（有落库段且够格）不能被这条新分支误伤：不发 hide。"""
    store = _FakeCanvasStore(appended=[_section("sec_1", "insight_card")])
    bus = _RecordingBus(
        _d6_canvas_store=store,
        _d6_turn_id="t_report",
        # Report turns stream sec_lead_* too; new branch must never hide just because one exists.
        _d6_streamed_lead_section_ids=["sec_lead_t_report_001"],
    )
    await _emit_done(bus, "t_report")
    assert "canvas.sections_hidden" not in bus.types(), bus.types()
    assert store.marked_hidden == []
    assert bus.payload_of("done")["response_kind"] == "canvas"


@pytest.mark.asyncio
async def test_persisted_but_chat_mode_still_hides_by_db_ids() -> None:
    """有落库段但不够格（只有表）→ 走原来的 DB 路径，用库内 id，不是流出去的 id。"""
    store = _FakeCanvasStore(appended=[_section("sec_db_1", "data_table")])
    bus = _RecordingBus(
        _d6_canvas_store=store,
        _d6_turn_id="t_table",
        _d6_streamed_lead_section_ids=["sec_lead_t_table_001"],
    )
    await _emit_done(bus, "t_table")
    p = bus.payload_of("canvas.sections_hidden")
    assert p["hidden_section_ids"] == ["sec_db_1"]
    assert store.marked_hidden == ["sec_db_1"]


@pytest.mark.asyncio
async def test_done_surface_uses_materialized_report_signal_not_spawn_classifier() -> None:
    """116 t_6f95d818d6ad：code_block 会被 D6 藏，done 也必须持久化为 chat。

    调用方传进来的 ``canvas`` 模拟旧 spawn(compute) 分类结果；终态 chokepoint
    必须在 persist/live 共用 payload 形成前按实际 sections 覆盖它。
    """
    store = _FakeCanvasStore(appended=[_section("sec_python", "code_block")])
    bus = _RecordingBus(
        _d6_canvas_store=store,
        _d6_turn_id="t_python",
    )

    await _emit_event(
        bus,
        "t_python",
        "done",
        {
            "turn_id": "t_python",
            "status": "completed",
            "response_kind": "canvas",
        },
    )

    assert bus.payload_of("done")["response_kind"] == "chat"
    assert bus.payload_of("canvas.sections_hidden")["hidden_section_ids"] == [
        "sec_python",
    ]


@pytest.mark.asyncio
async def test_deliverable_turn_is_not_hidden_even_without_persisted_sections() -> None:
    """回归守卫：出了交付物（deck/海报）但没落库段的轮次，**不能**被当成 chat-mode 藏掉。

    我第一版把新分支写成 `if not _d6_sections:` 放在最前面，`elif _d6_sections:` 恒真，
    把原有的 `elif _d6_has_deliverable:` 挤成了死分支——纯渲染轮会被误判成"没东西"
    进而把流出去的段全藏掉，用户什么都看不到。独立 review 只报了"elif 是同义反复"，
    真正的伤害是它下面那条分支被吃掉了。
    """
    bus = _RecordingBus(
        _d6_canvas_store=_FakeCanvasStore(appended=[]),  # Unpersisted section.
        _d6_turn_id="t_deck",
        _d6_artifact_store=SimpleNamespace(turn_has_deliverable=_always_true),
        _d6_streamed_lead_section_ids=["sec_lead_t_deck_001"],
    )
    await _emit_done(bus, "t_deck")
    assert "canvas.sections_hidden" not in bus.types(), bus.types()
    assert bus.payload_of("done")["response_kind"] == "canvas"


async def _always_true(turn_id: str) -> bool:
    return True
