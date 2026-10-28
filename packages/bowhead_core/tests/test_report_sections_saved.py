"""终态 ``done.report_sections_saved``：本轮落库、刷新后报告里能看到的节数。

前端在报告写作被停止 / 失败时显示「已写的 N 节已保存」。live 时前端会剪掉正在写的那一节，
服务端却可能已把它落库（写手草稿槽），反过来 Lead 流式预览的段只发过前端、从不落库——
所以这个数只能由服务端在终态时数库给出，并且 live、持久化与 F5 重放必须是同一个数。

口径以前端刷新后的渲染为准（by-session 回包 → ``selectReportSurfaceSections`` →
ReportView ``currentSections`` / ``preprocessSections``）：只数 ``turn_id`` 是本轮的段；
chat-mode 隐藏、已折叠进历史、SQL / 代码 / 裸表、被 insight_card 内嵌的图不算。

每个终态出口（Lead done 咽喉点、TurnRunner 停止、Orchestrator 失败）都走真实 emit 路径，
live 取 bus 的 fanout 环，持久化 / 重放取 TurnLogStore（``/turns/{id}/events`` 的来源）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from bowhead_core.agents.lead_orchestrator import _emit_event
from bowhead_core.canvas_composers.streaming import StreamingDecomposer
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.events import EventBusRegistry, OrchestratorEvent

from bowhead_core import TurnLogStore

if TYPE_CHECKING:
    from pathlib import Path

    from bowhead_core.events import TurnEventBus


@pytest.fixture(autouse=True)
def _fanout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_SSE_FANOUT", "1")


@pytest.fixture
async def canvas(tmp_path: Path) -> CanvasStore:
    store = CanvasStore(tmp_path / "canvas.db")
    await store.initialize()
    return store


@pytest.fixture
async def log_store(tmp_path: Path) -> TurnLogStore:
    store = TurnLogStore(db_path=str(tmp_path / "log.db"))
    await store.initialize()
    return store


def _registry(log_store: TurnLogStore) -> EventBusRegistry:
    async def sink(event: OrchestratorEvent) -> bool:
        await log_store.record(
            turn_id=event.turn_id,
            event_type=event.event_type,
            payload=event.payload,
            occurred_at=event.occurred_at,
        )
        return True

    return EventBusRegistry(default_log_sink=sink)


async def _bus(log_store: TurnLogStore, turn_id: str, registry: EventBusRegistry | None = None) -> TurnEventBus:
    return await (registry or _registry(log_store)).get_or_create(turn_id, persist_before_live=True)


async def _live_and_replayed_done(bus: TurnEventBus, log_store: TurnLogStore, turn_id: str) -> tuple[dict, dict]:
    live = [event.payload for event in bus.snapshot() if event.event_type == "done"]
    replayed = [row.payload for row in await log_store.list_events(turn_id) if row.event_type == "done"]
    assert len(live) == 1 and len(replayed) == 1
    return live[0], replayed[0]


async def _write_report(canvas: CanvasStore, turn_id: str, *, session_id: str = "s1") -> str:
    """本轮写一份报告：刷新后报告面显示 标题 + 正文 + 洞察卡 = 3 节。

    同轮还落了被洞察卡内嵌的图、SQL 代码块与裸表（报告面都不单独成节）。
    """
    c = await canvas.ensure_canvas(session_id=session_id, workspace_id="w", user_id="u")
    await canvas.append_section(canvas_id=c.id, kind="h1", payload={"text": "标题"}, by_turn_id=turn_id)
    await canvas.append_section(canvas_id=c.id, kind="narrative", payload={"markdown": "正文。"}, by_turn_id=turn_id)
    chart = await canvas.append_section(canvas_id=c.id, kind="chart", payload={"spec": {"title": "图"}}, by_turn_id=turn_id)
    await canvas.append_section(
        canvas_id=c.id,
        kind="insight_card",
        payload={"headline": "洞察", "evidence": [{"kind": "chart", "chart_section_id": chart.id}]},
        by_turn_id=turn_id,
    )
    await canvas.append_section(canvas_id=c.id, kind="code_block", payload={"code": "select 1"}, by_turn_id=turn_id)
    await canvas.append_section(canvas_id=c.id, kind="data_table", payload={"rows": []}, by_turn_id=turn_id)
    return c.id


# ── Counting rule matches refreshed report surface ──────────────


async def test_count_matches_report_surface_after_refresh(canvas: CanvasStore) -> None:
    canvas_id = await _write_report(canvas, "t2")
    # Prior-turn sections and charts not embedded this turn use own turn ownership and visibility.
    await canvas.append_section(canvas_id=canvas_id, kind="narrative", payload={"markdown": "上一轮。"}, by_turn_id="t1")
    await canvas.append_section(canvas_id=canvas_id, kind="chart", payload={"spec": {"title": "独立图"}}, by_turn_id="t2")

    assert await canvas.count_report_sections_saved_for_turn("t2") == 4
    assert await canvas.count_report_sections_saved_for_turn("t1") == 1
    assert await canvas.count_report_sections_saved_for_turn("t_none") == 0


async def test_chart_embedded_by_another_turns_insight_card_is_not_a_section(canvas: CanvasStore) -> None:
    c = await canvas.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    chart = await canvas.append_section(canvas_id=c.id, kind="chart", payload={"spec": {}}, by_turn_id="t2")
    await canvas.append_section(
        canvas_id=c.id,
        kind="insight_card",
        payload={"evidence": [{"chart_section_id": chart.id}]},
        by_turn_id="t1",
    )
    assert await canvas.count_report_sections_saved_for_turn("t2") == 0


async def test_superseded_and_chat_mode_hidden_sections_are_not_counted(canvas: CanvasStore) -> None:
    c = await canvas.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    kept = await canvas.append_section(canvas_id=c.id, kind="narrative", payload={"markdown": "留。"}, by_turn_id="t2")
    folded = await canvas.append_section(canvas_id=c.id, kind="narrative", payload={"markdown": "折。"}, by_turn_id="t2")
    await canvas.mark_sections_superseded(section_ids=[folded.id], superseded_by_turn="t3")
    assert await canvas.count_report_sections_saved_for_turn("t2") == 1

    await canvas.mark_turn_hidden_for_chat_mode("t2", "chat_only")
    assert await canvas.count_report_sections_saved_for_turn("t2") == 0
    assert kept.id  # Still stored, hidden after refresh.


# ── Lead done boundary: completion / abort / failure ────────────


@pytest.mark.parametrize("status", ["completed", "aborted", "failed"])
async def test_lead_done_carries_the_same_count_live_and_replayed(
    canvas: CanvasStore,
    log_store: TurnLogStore,
    status: str,
) -> None:
    await _write_report(canvas, "t2")
    bus = await _bus(log_store, "t2")
    bus._d6_canvas_store = canvas  # type: ignore[attr-defined]

    await _emit_event(bus, "t2", "done", {"turn_id": "t2", "status": status})

    live, replayed = await _live_and_replayed_done(bus, log_store, "t2")
    assert live["report_sections_saved"] == 3
    assert replayed["report_sections_saved"] == 3


async def test_stop_mid_partial_section_counts_only_what_refresh_shows(
    canvas: CanvasStore,
    log_store: TurnLogStore,
) -> None:
    """在正在写的那一节中途停止。

    写手的草稿槽已落库（刷新后显示草稿）→ 算；Lead 流式预览段只发给前端、live 里被标
    ``is_partial`` 剪掉 → 库里没有，不算。live 前端看到的节数（去掉 is_partial 的）与刷新后
    不同，所以这个数必须来自服务端：两条路拿到同一个值。
    """
    canvas_id = await _write_report(canvas, "t2")
    # Writer draft slot: stop midwrite, store holds deterministic draft.
    await canvas.append_section(canvas_id=canvas_id, kind="narrative", payload={"markdown": "草稿：第二章"}, by_turn_id="t2")

    bus = await _bus(log_store, "t2")
    bus._d6_canvas_store = canvas  # type: ignore[attr-defined]

    async def emit(name: str, payload: dict[str, Any]) -> None:
        await bus.emit(OrchestratorEvent(event_type=name, turn_id="t2", payload=payload))

    decomposer = StreamingDecomposer(canvas_id=canvas_id, turn_id="t2", emit=emit)
    await decomposer.feed("## 第二章\n正在写的半截")
    await decomposer.abort_open_section(reason="cancelled")
    partial = [e.payload for e in bus.snapshot() if e.event_type == "canvas.section_done" and e.payload.get("is_partial")]
    assert partial, "流式段应被标成 is_partial（前端据此剪掉）"

    await _emit_event(bus, "t2", "done", {"turn_id": "t2", "status": "aborted"})

    live, replayed = await _live_and_replayed_done(bus, log_store, "t2")
    assert live["report_sections_saved"] == replayed["report_sections_saved"] == 4


async def test_chat_only_verdict_at_done_counts_zero(canvas: CanvasStore, log_store: TurnLogStore) -> None:
    """终态裁决判成纯聊天（没有洞察 / 建议）时本轮段被隐藏，刷新后报告里没有它们 ⇒ 0。

    数库必须在裁决落定之后：先数再藏会报出刷新后根本看不到的节。
    """
    c = await canvas.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    await canvas.append_section(canvas_id=c.id, kind="h1", payload={"text": "标题"}, by_turn_id="t2")
    await canvas.append_section(canvas_id=c.id, kind="narrative", payload={"markdown": "一句话回答。"}, by_turn_id="t2")
    bus = await _bus(log_store, "t2")
    bus._d6_canvas_store = canvas  # type: ignore[attr-defined]

    await _emit_event(bus, "t2", "done", {"turn_id": "t2", "status": "cancelled"})

    live, replayed = await _live_and_replayed_done(bus, log_store, "t2")
    assert live["report_sections_saved"] == replayed["report_sections_saved"] == 0


async def test_lead_done_without_canvas_store_omits_the_field(log_store: TurnLogStore) -> None:
    bus = await _bus(log_store, "t2")
    await _emit_event(bus, "t2", "done", {"turn_id": "t2", "status": "completed"})
    live, replayed = await _live_and_replayed_done(bus, log_store, "t2")
    assert "report_sections_saved" not in live
    assert "report_sections_saved" not in replayed


async def test_count_failure_never_blocks_done(log_store: TurnLogStore) -> None:
    class _BrokenStore:
        async def count_report_sections_saved_for_turn(self, turn_id: str) -> int:
            raise RuntimeError("db locked")

    bus = await _bus(log_store, "t2")
    bus._d6_canvas_store = _BrokenStore()  # type: ignore[attr-defined]
    await _emit_event(bus, "t2", "done", {"turn_id": "t2", "status": "failed"})
    live, replayed = await _live_and_replayed_done(bus, log_store, "t2")
    assert live["status"] == "failed"
    assert "report_sections_saved" not in live and "report_sections_saved" not in replayed


# ── Other terminal exits: user Stop (TurnRunner) and Orchestrator failure ──


async def _session_turn(tmp_path: Path, turn_id: str) -> tuple[Any, str]:
    from bowhead_core.session_store import SessionStore

    store = SessionStore(str(tmp_path / "sessions.db"))
    await store.initialize()
    sess = await store.ensure_session(session_id=None, workspace_id="ws", user_id="u")
    await store.record_turn(turn_id=turn_id, session_id=sess.id, user_text="q")
    return store, sess.id


def _request(session_id: str, turn_id: str) -> Any:
    from bowhead_core.types import TurnRequest

    return TurnRequest(session_id=session_id, workspace_id="ws", user_id="u", member_uid="u", text="q", turn_id=turn_id)


async def test_user_stop_done_carries_the_same_count_live_and_replayed(
    tmp_path: Path,
    canvas: CanvasStore,
    log_store: TurnLogStore,
) -> None:
    from types import SimpleNamespace

    from bowhead_core.turn_runner import TurnRunner

    await _write_report(canvas, "t_stop")
    sessions, session_id = await _session_turn(tmp_path, "t_stop")
    registry = _registry(log_store)
    bus = await _bus(log_store, "t_stop", registry)
    orch = SimpleNamespace(
        session_store=sessions,
        canvas_store=canvas,
        event_registry=registry,
        _event=lambda turn_id, name, payload: OrchestratorEvent(event_type=name, turn_id=turn_id, payload=payload),
    )
    runner = TurnRunner(orch)
    runner._t_start = 0.0

    await runner._finish_cancelled(_request(session_id, "t_stop"))

    live, replayed = await _live_and_replayed_done(bus, log_store, "t_stop")
    assert live["status"] == "cancelled"
    assert live["report_sections_saved"] == replayed["report_sections_saved"] == 3
