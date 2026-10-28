"""Sprint chat-polish-r3 Phase 4 (B8) — canvas.reordered emit ungating.

Pre-r3 the canvas.reordered emit was inside `if emit_lifecycle:` (canvas_
composer.py:1092). The eager-compose path (called from subagent_return
with emit_lifecycle=False to avoid double canvas.opened/done) would
reorder the DB silently. Lead's end-of-turn re-compose then hit the
dedup → new_payload_ids empty → never reached the reorder branch →
canvas.reordered never emitted. FE stayed with arrival-order rendering;
F5 fetched the sorted DB state so it always looked "complete" (B8 "canvas
needs F5").

Fix: drop the lifecycle gate on canvas.reordered emit. The reorder is a
content event, not lifecycle. canvas.opened / outline_ready / done stay
gated.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from bowhead_core.canvas_composer import compose_canvas_from_payload
from bowhead_core.types import IntentType, TurnRequest


def _turn_id() -> str:
    """Unique per-test turn_id — `_PSI_COMPOSED_KEYS` in canvas_composer
    is a module-global dedup map keyed by (turn_id, peer_id, kind#ord);
    sharing turn_id across tests would let test #2 hit dedup keys laid
    down by test #1 and skip the entire compose call."""
    return f"t_b8_{uuid.uuid4().hex[:8]}"


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, ev: Any) -> None:
        self.events.append((ev.event_type, ev.payload))


class _Sec:
    def __init__(self, sid: str, seq: int, kind: str, payload: dict) -> None:
        self.id = sid
        self.seq = seq
        self.kind = kind
        self.payload = payload

    def to_report_section(self) -> dict:
        return {"id": self.id, "kind": self.kind, **self.payload}


class _Canvas:
    def __init__(self) -> None:
        self.id = "cv_b8"
        self.session_id = "s_b8"
        self.title = None


class _Store:
    """Stub canvas_store with append + list + reorder.

    Pre-seed `_pre` to simulate existing chart/code_block sections (eager
    streaming added these before subagent_return composes the payload).
    """

    def __init__(self, pre: list[_Sec] | None = None) -> None:
        self._sections: list[_Sec] = list(pre or [])
        self._seq = len(self._sections)
        self.reorder_called_with: list[str] | None = None

    async def ensure_canvas(self, session_id: str, workspace_id: str, user_id: str):
        return _Canvas()

    async def append_section(self, *, canvas_id: str, kind: str, payload: dict, by_turn_id: str, owner: str | None = None) -> _Sec:
        self._seq += 1
        sid = f"sec_payload_{self._seq:03d}"
        sec = _Sec(sid, self._seq, kind, payload)
        self._sections.append(sec)
        return sec

    async def list_sections(self, canvas_id: str) -> list[_Sec]:
        return list(self._sections)

    async def reorder_sections(self, *, canvas_id: str, ordered_ids: list[str], by_turn_id: str) -> None:
        self.reorder_called_with = list(ordered_ids)
        by_id = {s.id: s for s in self._sections}
        self._sections = [by_id[i] for i in ordered_ids if i in by_id]


def _payload() -> dict:
    return {
        "title": "test",
        "eyebrow": "test",
        "exec_summary": "summary text here",
        "kpis": [{"label": "x", "value": "1"}],
        "insights": [
            {"title": "i1", "thesis": "t", "narrative": "n" * 50},
        ],
        "recommendations": [
            {"priority": "P0", "headline": "do x", "rationale": "r" * 30},
        ],
    }


@pytest.mark.asyncio
async def test_reorder_emit_fires_even_when_emit_lifecycle_false() -> None:
    """B8 root-cause test: with pre-existing streaming sections + eager
    compose (emit_lifecycle=False), canvas.reordered MUST be emitted so
    FE re-sorts to match the DB sorted order.
    """
    # Pre-seed 2 chart sections that were added via add_chart
    # before subagent_return ran (live-emit-order anti-pattern).
    pre = [
        _Sec("sec_chart_a", 1, "chart", {}),
        _Sec("sec_chart_b", 2, "chart", {}),
    ]
    store = _Store(pre=pre)
    bus = _Bus()
    request = TurnRequest(
        turn_id=_turn_id(),
        session_id="s_b8",
        workspace_id="w",
        user_id="u",
        member_uid="m",
        text="x",
    )
    await compose_canvas_from_payload(
        canvas_store=store,
        request=request,
        bus=bus,
        payload=_payload(),
        intent=IntentType.REPORT,
        emit_lifecycle=False,  # the gate that pre-r3 silenced the emit
    )
    # Confirm the DB reorder ran with payload sections in front of charts.
    assert store.reorder_called_with is not None
    chart_idx = store.reorder_called_with.index("sec_chart_a")
    payload_idxs = [i for i, sid in enumerate(store.reorder_called_with) if sid.startswith("sec_payload_")]
    assert all(i < chart_idx for i in payload_idxs), "payload sections must come before chart"
    # The fix: canvas.reordered MUST emit even with emit_lifecycle=False.
    reordered = [p for ev, p in bus.events if ev == "canvas.reordered"]
    assert len(reordered) == 1, "canvas.reordered must emit when DB reorder occurred"
    assert reordered[0]["order"] == store.reorder_called_with
    assert reordered[0]["reason"] == "payload_path_promote_to_top"


@pytest.mark.asyncio
async def test_fresh_canvas_gets_one_canonicalizing_reorder(monkeypatch: pytest.MonkeyPatch) -> None:
    """A3 (report-edit-audit, seq 权威化): 空 canvas 也要落**展示规范序**。

    原契约是"append-only 路不发 reorder"。A3 起库里 seq 是唯一权威序（页面/导出/
    拖拽都认它），而 payload 路的 append 序实测是
    `eyebrow → h1 → meta → narrative → kpi_row → …`（KPI band 在正文之后落库）——
    前端 `preprocessSections` 一直把 KPI 提到 body 前面显示，所以那个 append 序
    **从来**不等于用户看到的序。现在补一次规范化 reorder 让库序 == 显示序（不是
    "spurious reorder"，恰恰是消除双口径）。"不发多余 reorder"的意图由下一个测试守住。
    """
    store = _Store(pre=[])  # empty canvas
    bus = _Bus()
    request = TurnRequest(
        turn_id=_turn_id(),
        session_id="s_b8",
        workspace_id="w",
        user_id="u",
        member_uid="m",
        text="x",
    )
    await compose_canvas_from_payload(
        canvas_store=store,
        request=request,
        bus=bus,
        payload=_payload(),
        intent=IntentType.REPORT,
        emit_lifecycle=False,
    )
    assert store.reorder_called_with is not None
    kinds = [s.kind for s in store._sections]
    # Headers lead; the KPI band precedes body text (same rank as frontend preprocessSections).
    assert kinds.index("h1") < kinds.index("kpi_row")
    assert kinds.index("kpi_row") < kinds.index("narrative")
    styles = [s.payload.get("style") for s in store._sections if s.kind == "narrative"]
    assert styles[0] == "executive"
    assert styles[1:] == ["insight"]
    reordered = [p for ev, p in bus.events if ev == "canvas.reordered"]
    assert len(reordered) == 1


@pytest.mark.asyncio
async def test_no_reorder_when_already_canonical() -> None:
    """A3: 已是规范序的 canvas 再 compose 不得发多余 reorder（no-op 守卫）。"""
    store = _Store(pre=[])
    bus = _Bus()
    request = TurnRequest(
        turn_id=_turn_id(),
        session_id="s_b8",
        workspace_id="w",
        user_id="u",
        member_uid="m",
        text="x",
    )
    await compose_canvas_from_payload(
        canvas_store=store,
        request=request,
        bus=bus,
        payload=_payload(),
        intent=IntentType.REPORT,
        emit_lifecycle=False,
    )
    # Second turn: database order is already canonical and no new payload sections exist, so emit no reorder.
    store.reorder_called_with = None
    bus.events.clear()
    await compose_canvas_from_payload(
        canvas_store=store,
        request=request,
        bus=bus,
        payload=_payload(),
        intent=IntentType.REPORT,
        emit_lifecycle=False,
    )
    assert store.reorder_called_with is None
    assert [p for ev, p in bus.events if ev == "canvas.reordered"] == []


@pytest.mark.asyncio
async def test_lifecycle_events_still_gated_by_emit_lifecycle_false() -> None:
    """Sanity: dropping the lifecycle gate on canvas.reordered must NOT
    accidentally release canvas.opened / outline_ready / done — those
    stay gated."""
    store = _Store(pre=[_Sec("sec_chart_a", 1, "chart", {})])
    bus = _Bus()
    request = TurnRequest(
        turn_id=_turn_id(),
        session_id="s_b8",
        workspace_id="w",
        user_id="u",
        member_uid="m",
        text="x",
    )
    await compose_canvas_from_payload(
        canvas_store=store,
        request=request,
        bus=bus,
        payload=_payload(),
        intent=IntentType.REPORT,
        emit_lifecycle=False,
    )
    event_types = [ev for ev, _ in bus.events]
    assert "canvas.opened" not in event_types
    assert "canvas.outline_ready" not in event_types
    assert "canvas.done" not in event_types
    # But content events DO emit
    assert "canvas.section_added" in event_types
    assert "canvas.reordered" in event_types


@pytest.mark.asyncio
async def test_lifecycle_emit_true_still_emits_reorder() -> None:
    """emit_lifecycle=True path also emits reorder (regression check)."""
    store = _Store(pre=[_Sec("sec_chart_a", 1, "chart", {})])
    bus = _Bus()
    request = TurnRequest(
        turn_id=_turn_id(),
        session_id="s_b8",
        workspace_id="w",
        user_id="u",
        member_uid="m",
        text="x",
    )
    await compose_canvas_from_payload(
        canvas_store=store,
        request=request,
        bus=bus,
        payload=_payload(),
        intent=IntentType.REPORT,
        emit_lifecycle=True,
    )
    reordered = [p for ev, p in bus.events if ev == "canvas.reordered"]
    assert len(reordered) == 1
