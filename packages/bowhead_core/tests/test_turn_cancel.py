"""M2.1 · Turn cancel 单元测试.

设计文档: docs/M157-cancel-turn-design.md

覆盖:
- Orchestrator.signal_cancel 幂等 / 返值正确
- TurnRunner._check_cancel 抛 TurnCancelledError 当 event set
- TurnRunner.run boundary check 触发 cancel → 走 _finish_cancelled
- POST /api/v1/turns/:tid/cancel 返 cancelling / noop
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from bowhead_core.orchestrator import Orchestrator, TurnCancelledError
from bowhead_core.terminal_effect_contract import resolve_effect_execution_contract
from bowhead_core.terminal_protocol import TerminalScope
from bowhead_core.turn_runner import TurnRunner
from bowhead_core.types import TurnRequest

# ─── Orchestrator.signal_cancel ─────────────────────────────────


def _make_orch_minimal():
    """构造最小 Orchestrator 桩, 仅 _cancel_events 字段; 不要求其它依赖."""
    orch = SimpleNamespace()
    orch._cancel_events = {}
    orch.signal_cancel = Orchestrator.signal_cancel.__get__(orch)
    orch.is_cancelled = Orchestrator.is_cancelled.__get__(orch)
    return orch


def test_signal_cancel_returns_false_when_no_running_turn():
    orch = _make_orch_minimal()
    assert orch.signal_cancel("t_nope") is False
    assert orch.is_cancelled("t_nope") is False


def test_signal_cancel_returns_true_when_event_registered():
    orch = _make_orch_minimal()
    ev = asyncio.Event()
    orch._cancel_events["t_abc"] = ev
    assert orch.signal_cancel("t_abc") is True
    assert ev.is_set()
    assert orch.is_cancelled("t_abc") is True


def test_signal_cancel_idempotent():
    orch = _make_orch_minimal()
    ev = asyncio.Event()
    orch._cancel_events["t_abc"] = ev
    assert orch.signal_cancel("t_abc") is True
    # Second call also True (event.set idempotent).
    assert orch.signal_cancel("t_abc") is True


def test_terminal_cancel_signal_is_generation_bound():
    orch = SimpleNamespace()
    orch._terminal_cancel_events = {}
    orch.signal_cancel_generation = Orchestrator.signal_cancel_generation.__get__(orch)
    generation_2 = asyncio.Event()
    orch._terminal_cancel_events[("t_same", 2)] = generation_2

    assert orch.signal_cancel_generation("t_same", 1) is False
    assert generation_2.is_set() is False
    assert orch.signal_cancel_generation("t_same", 2) is True
    assert generation_2.is_set() is True
    assert orch.signal_cancel_generation("t_same", 2) is True


@pytest.mark.asyncio
async def test_terminal_cancel_registration_cannot_leak_when_setup_fails(
    monkeypatch,
) -> None:
    import bowhead_core.delta_lifecycle as delta_lifecycle

    def fail_setup(**_kwargs):
        raise RuntimeError("lifecycle setup failed")

    monkeypatch.setattr(
        delta_lifecycle,
        "build_delta_lifecycle",
        fail_setup,
    )
    orch = SimpleNamespace(
        session_store=None,
        clarification_store=None,
        fact_store=object(),
        router=None,
        _cancel_events={},
        _terminal_cancel_events={},
        _executing_plans={},
    )
    request = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="member_1",
        text="hello",
        turn_id="t_1",
        terminal_runtime=SimpleNamespace(
            scope=TerminalScope(
                team_id="team_1",
                session_id="s_1",
                turn_id="t_1",
                generation=1,
            ),
            effect_contract=resolve_effect_execution_contract({}),
        ),
    )

    with pytest.raises(RuntimeError, match="lifecycle setup failed"):
        await TurnRunner(orch).run(request)

    assert orch._terminal_cancel_events == {}
    assert orch._cancel_events == {}


# ─── TurnRunner._check_cancel ─────────────────────────────────


def test_check_cancel_raises_when_event_set():
    orch = _make_orch_minimal()
    runner = TurnRunner(orch)
    ev = asyncio.Event()
    runner._cancel_event = ev
    # Unset → no raise.
    runner._check_cancel()
    # Set → TurnCancelledError.
    ev.set()
    with pytest.raises(TurnCancelledError):
        runner._check_cancel()


def test_check_cancel_safe_when_no_event_attr():
    """run() 之外的 path 调 _check_cancel 不应炸 (例如 helper / test)."""
    orch = _make_orch_minimal()
    runner = TurnRunner(orch)
    # Absent self._cancel_event also no raise.
    runner._check_cancel()


class _DurableTestBus:
    _persist_before_live = True


class _DurableTestRegistry:
    def __init__(self) -> None:
        self.bus = _DurableTestBus()

    async def projection_checkpoint(self, _turn_id: str):
        return self.bus, 0


def _run_boundary_orchestrator(*, session_store=None):
    async def require_legacy_chat_execution(**_kwargs: object) -> None:
        # This cancellation harness models a legacy text-only storage boundary.
        pass

    if session_store is not None:
        session_store.require_legacy_chat_execution = require_legacy_chat_execution
    orch = SimpleNamespace(
        event_registry=_DurableTestRegistry(),
        session_store=session_store,
        clarification_store=None,
        fact_store=None,
        router=None,
        _cancel_events={},
        _terminal_cancel_events={},
        _executing_plans={},
    )
    orch.signal_cancel = Orchestrator.signal_cancel.__get__(orch)
    orch.signal_cancel_generation = Orchestrator.signal_cancel_generation.__get__(
        orch,
    )
    return orch


@pytest.mark.asyncio
async def test_run_signal_interrupts_active_legacy_worker() -> None:
    async def get_turn(_turn_id: str):
        return SimpleNamespace(status="cancelled", execution_generation=1)

    orch = _run_boundary_orchestrator(
        session_store=SimpleNamespace(get_turn=get_turn),
    )
    runner = TurnRunner(orch)
    started = asyncio.Event()
    finish_started = asyncio.Event()

    async def blocking_inner(_request, _cancel_event):
        started.set()
        await asyncio.Event().wait()

    async def finish_cancelled(_request):
        finish_started.set()
        await asyncio.sleep(0)
        return "cancelled"

    runner._run_inner = blocking_inner  # type: ignore[method-assign]
    runner._finish_cancelled = finish_cancelled  # type: ignore[method-assign]
    request = TurnRequest(
        session_id="s_watch_legacy",
        workspace_id="ws",
        user_id="u",
        member_uid="m",
        text="wait",
        turn_id="t_watch_legacy",
    )
    task = asyncio.create_task(runner.run(request))
    await asyncio.wait_for(started.wait(), timeout=2)

    assert orch.signal_cancel(request.turn_id)
    assert await asyncio.wait_for(task, timeout=0.5) == "cancelled"
    assert finish_started.is_set()
    assert orch._cancel_events == {}


@pytest.mark.asyncio
async def test_run_terminal_cancel_watcher_is_generation_exact() -> None:
    orch = _run_boundary_orchestrator()
    runner = TurnRunner(orch)
    started = asyncio.Event()

    async def blocking_inner(_request, _cancel_event):
        started.set()
        await asyncio.Event().wait()

    async def finish_cancelled(_request):
        return "terminal-cancelled"

    runner._run_inner = blocking_inner  # type: ignore[method-assign]
    runner._finish_cancelled = finish_cancelled  # type: ignore[method-assign]
    request = TurnRequest(
        session_id="s_watch_terminal",
        workspace_id="ws",
        user_id="u",
        member_uid="m",
        text="wait",
        turn_id="t_watch_terminal",
        execution_generation=2,
        terminal_runtime=SimpleNamespace(
            scope=TerminalScope(
                team_id="team",
                session_id="s_watch_terminal",
                turn_id="t_watch_terminal",
                generation=2,
            ),
            effect_contract=resolve_effect_execution_contract({}),
        ),
    )
    task = asyncio.create_task(runner.run(request))
    await asyncio.wait_for(started.wait(), timeout=2)

    assert not orch.signal_cancel_generation(request.turn_id, 1)
    await asyncio.sleep(0)
    assert not task.done()
    assert orch.signal_cancel_generation(request.turn_id, 2)
    assert await asyncio.wait_for(task, timeout=0.5) == "terminal-cancelled"
    assert orch._terminal_cancel_events == {}


@pytest.mark.asyncio
async def test_run_observes_remote_store_cancel_after_inner_returns() -> None:
    canonical_status = "running"

    async def get_turn(_turn_id: str):
        return SimpleNamespace(
            status=canonical_status,
            execution_generation=1,
        )

    orch = _run_boundary_orchestrator(
        session_store=SimpleNamespace(get_turn=get_turn),
    )
    runner = TurnRunner(orch)

    async def cas_losing_inner(_request, _cancel_event):
        nonlocal canonical_status
        canonical_status = "cancelled"
        return "completed-cas-loser"

    async def finish_cancelled(_request):
        return "remote-cancelled"

    runner._run_inner = cas_losing_inner  # type: ignore[method-assign]
    runner._finish_cancelled = finish_cancelled  # type: ignore[method-assign]
    result = await runner.run(
        TurnRequest(
            session_id="s_remote_cancel",
            workspace_id="ws",
            user_id="u",
            member_uid="m",
            text="race",
            turn_id="t_remote_cancel",
        )
    )

    assert result == "remote-cancelled"


# ─── Feishu07.06 #7: Orchestrator.get_executing_plan (Stop cascades plan cancellation) ──
def test_get_executing_plan_roundtrip():
    orch = SimpleNamespace()
    orch._executing_plans = {}
    orch.get_executing_plan = Orchestrator.get_executing_plan.__get__(orch)
    # Unregistered →None.
    assert orch.get_executing_plan("t_x") is None
    # Registered →plan_id.
    orch._executing_plans["t_exec"] = "plan_42"
    assert orch.get_executing_plan("t_exec") == "plan_42"
    # Unregistered again →None (turn-end cleanup).
    orch._executing_plans.pop("t_exec", None)
    assert orch.get_executing_plan("t_exec") is None
