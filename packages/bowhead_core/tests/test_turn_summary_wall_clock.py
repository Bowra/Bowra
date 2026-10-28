"""Turn summary 口径：耗时 = 本执行代 turn_started→done 墙钟，计数覆盖所有代理。

116 实测（t_f1db44428228）：turn 跨度 121s，界面显示「1 分 5 秒」（= Lead runner 循环
自己的计时，漏掉起跑准备与收尾成文）；summary 恒写 `llm_count=1`、`tools_count` 只数
Lead 顶层调用；私有智能体轮 summary 为 null（只在 Lead 正常返回后才写）。
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_core.agents.lead_orchestrator import _emit_event
from bowhead_core.events import OrchestratorEvent, TurnEventBus
from bowhead_core.turn_runner import TurnRunner, turn_summary_counts


class _CaptureBus:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._done_emitted = False
        self._final_answer_emitted = True
        self._pi_turn_state: dict[str, Any] = {
            "intent": None,
            "final_answer": "",
            "tokens_input": 0,
            "tokens_output": 0,
            "cost_usd": 0.0,
            "error": None,
            "error_kind": None,
        }

    async def emit(self, event: OrchestratorEvent) -> bool:
        self.events.append({"event_type": event.event_type, "payload": event.payload})
        return True


@pytest.mark.asyncio
async def test_bus_counts_tool_results_from_lead_and_peers() -> None:
    bus = TurnEventBus()
    for parent in (None, "compute_1", "compute_1"):
        await bus.emit(
            OrchestratorEvent(
                event_type="tool_result",
                turn_id="t_1",
                payload={"tool_name": "bi_query", "ok": True},
                parent_agent_id=parent,
            )
        )
    await bus.emit(OrchestratorEvent(event_type="step_done", turn_id="t_1", payload={"step_id": "s1"}))

    assert bus._tool_result_count == 3
    # Lead top-level invocations lower bound only; absent worker-sink LLM count falls back to old 1.
    assert turn_summary_counts(bus, [object()]) == (3, 1)
    bus._turn_llm_call_count = 5  # type: ignore[attr-defined]
    assert turn_summary_counts(bus, None) == (3, 5)


@pytest.mark.asyncio
async def test_done_duration_is_generation_wall_clock_and_summary_matches() -> None:
    store = MagicMock()
    store.mark_completed = AsyncMock(return_value=True)
    store.persist_turn_summary = AsyncMock()
    orch = MagicMock()
    orch.session_store = store
    runner = TurnRunner(orch)

    bus = _CaptureBus()
    # turn_started 121s ago; Lead-runner loop measured only 65.29s.
    bus._turn_wall_t0_perf = time.perf_counter() - 121.0  # type: ignore[attr-defined]
    bus._tool_result_count = 6  # type: ignore[attr-defined]
    bus._turn_llm_call_count = 4  # type: ignore[attr-defined]
    runner._summary_bus = bus  # type: ignore[attr-defined]
    bus._pi_persist_turn_state = runner._persist_turn_state  # type: ignore[attr-defined]

    await _emit_event(
        bus,  # type: ignore[arg-type]
        "t_1",
        "done",
        {"status": "completed", "duration_ms": 65290, "tokens_input": 1000, "tokens_output": 200},
    )

    done = next(ev for ev in bus.events if ev["event_type"] == "done")
    live_ms = done["payload"]["duration_ms"]
    assert 121000 <= live_ms < 125000

    # Persist summary as soon as terminal state commits (independent of later Lead cleanup), same value as live done.
    store.persist_turn_summary.assert_awaited_once()
    summary = store.persist_turn_summary.call_args.kwargs
    assert summary["turn_id"] == "t_1"
    assert summary["total_ms"] == live_ms
    assert summary["tools_count"] == 6
    assert summary["llm_count"] == 4
    assert summary["tokens_input"] == 1000


@pytest.mark.asyncio
async def test_done_keeps_emitter_duration_without_turn_start_anchor() -> None:
    """非 TurnRunner 路径（没有 turn_started 起点）不改写、也不写 summary。"""
    store = MagicMock()
    store.mark_completed = AsyncMock(return_value=True)
    store.persist_turn_summary = AsyncMock()
    orch = MagicMock()
    orch.session_store = store
    runner = TurnRunner(orch)
    bus = _CaptureBus()
    runner._summary_bus = bus  # type: ignore[attr-defined]
    bus._pi_persist_turn_state = runner._persist_turn_state  # type: ignore[attr-defined]

    await _emit_event(bus, "t_2", "done", {"status": "completed", "duration_ms": 4200})  # type: ignore[arg-type]

    done = next(ev for ev in bus.events if ev["event_type"] == "done")
    assert done["payload"]["duration_ms"] == 4200
    store.persist_turn_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_lost_terminal_cas_writes_no_summary() -> None:
    store = MagicMock()
    store.mark_completed = AsyncMock(return_value=False)
    store.persist_turn_summary = AsyncMock()
    orch = MagicMock()
    orch.session_store = store
    runner = TurnRunner(orch)
    bus = _CaptureBus()
    bus._turn_wall_t0_perf = time.perf_counter() - 3.0  # type: ignore[attr-defined]
    runner._summary_bus = bus  # type: ignore[attr-defined]
    bus._pi_persist_turn_state = runner._persist_turn_state  # type: ignore[attr-defined]

    await _emit_event(bus, "t_3", "done", {"status": "completed"})  # type: ignore[arg-type]

    assert not any(ev["event_type"] == "done" for ev in bus.events)
    store.persist_turn_summary.assert_not_awaited()
