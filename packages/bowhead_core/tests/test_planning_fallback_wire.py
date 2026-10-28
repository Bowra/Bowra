"""planning fall-through 诚实化 —— 「我们试了但失败了」必须让用户知道。

背景（S8 dogfood）：用户**显式**点了计划模式，`_maybe_propose_plan` 三条 None 路径
（出图空 / reify 失败 / 花名册外 agent）却只写服务端日志就静默 fall through 走普通
Lead —— 而 Lead 常顺着用户的话写一段 Markdown「执行计划」散文：没有卡、没有批准、
不会被执行，用户以为排上了。

「这个问题不值得出计划」保持静默是对的（Auto 档每轮都在发生）；两者必须分开，
判据 = `sctx.planning_mode == "planning"`。

这里钉的是**接线**那一节（`feedback_verify_at_the_gate_not_around_it`：只测 helper
等于在闸旁边测，闸本身通没通电要在 done 咽喉点上验）：

    三条 None 路径 → bus._planning_fallback
      → _emit_event 的 done 分支 → done payload 带 planning_fallback
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.agents import lead_orchestrator as lo
from bowhead_core.agents.lead_orchestrator import (
    _emit_event,
    _note_planning_fallback,
    _planning_fallback_fields,
)


def _sctx(mode: str | None) -> SimpleNamespace:
    return SimpleNamespace(planning_mode=mode, turn_id="t1")


# ── Criterion: speak only in explicit planning mode ─────────────────────────


def test_explicit_planning_mode_records_reason() -> None:
    bus = SimpleNamespace()
    _note_planning_fallback(_sctx("planning"), bus, "authoring_empty")
    assert _planning_fallback_fields(bus) == {"planning_fallback": "authoring_empty"}


@pytest.mark.parametrize("mode", [None, "off", "auto"])
def test_non_explicit_modes_stay_silent(mode: str | None) -> None:
    """Auto/off 档拆不出计划是**正常**的，不该每轮都弹一条告知。"""
    bus = SimpleNamespace()
    _note_planning_fallback(_sctx(mode), bus, "authoring_empty")
    assert _planning_fallback_fields(bus) == {}


def test_unknown_reason_never_reaches_the_wire() -> None:
    """只放行白名单里的 reason 码——前端按码查 i18n，放野码过去 = 前端没文案。"""
    bus = SimpleNamespace(_planning_fallback="something_new")
    assert _planning_fallback_fields(bus) == {}


def test_untouched_bus_keeps_wire_shape() -> None:
    assert _planning_fallback_fields(SimpleNamespace()) == {}


@pytest.mark.parametrize("reason", ["authoring_empty", "reify_failed", "unknown_agents"])
def test_all_three_fallthrough_reasons_are_wireable(reason: str) -> None:
    bus = SimpleNamespace()
    _note_planning_fallback(_sctx("planning"), bus, reason)
    assert _planning_fallback_fields(bus) == {"planning_fallback": reason}


# ── ★ The gate itself: is it actually in the done payload? ─────────────────────


class _CapturingBus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, ev: Any) -> None:
        self.events.append(ev)


def _done_payload(bus: _CapturingBus) -> dict[str, Any]:
    for ev in bus.events:
        if getattr(ev, "event_type", "") == "done":
            return dict(ev.payload or {})
    raise AssertionError(f"no done event emitted; got {[getattr(e, 'event_type', '?') for e in bus.events]}")


@pytest.fixture()
def _stub_done_chokepoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """短路 done 分支里两个要真依赖的协作者，只留 payload 组装这一节。"""

    async def _rk(_bus: Any, _turn_id: str, provisional: str) -> str:
        return provisional

    async def _persist(_bus: Any, _turn_id: str, _payload: dict) -> bool:
        return True

    monkeypatch.setattr(lo, "_resolve_terminal_response_kind", _rk)
    monkeypatch.setattr(lo, "_persist_done_state", _persist)


@pytest.mark.usefixtures("_stub_done_chokepoint")
async def test_done_payload_carries_planning_fallback() -> None:
    """出计划失败的那一轮，终态自己带着诚实信号到前端。"""
    bus = _CapturingBus()
    _note_planning_fallback(_sctx("planning"), bus, "reify_failed")

    await _emit_event(bus, "t1", "done", {"turn_id": "t1", "status": "completed"})

    assert _done_payload(bus)["planning_fallback"] == "reify_failed"


@pytest.mark.usefixtures("_stub_done_chokepoint")
async def test_done_payload_unchanged_when_plan_was_never_attempted() -> None:
    """没触发就一个键都不多——老客户端与普通轮的 wire 形状逐字节不变。"""
    bus = _CapturingBus()
    await _emit_event(bus, "t1", "done", {"turn_id": "t1", "status": "completed"})
    assert "planning_fallback" not in _done_payload(bus)
