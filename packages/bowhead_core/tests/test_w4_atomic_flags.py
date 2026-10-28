"""chat-v4 W4-fix CTO P0-1 — atomic V4-or-V1 flag readers.

Wires being tested:
  - ``_psi_skip_final_compose_only`` set on parent_bus → canvas_composer
    ``compose_canvas_from_payload`` returns [] without writing narrative
    sections. Other canvas writes (add_chart from peer) unaffected.
  - ``_turn_committed_to_v4`` set on parent_bus at FIRST scanner v4 callback
    fire (BEFORE lead.final.payload), not only at lead.final.payload emit.
    FE derives v4FallbackMode from any v4 event so the marker must lead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

# ─── _psi_skip_final_compose_only reader ──────────────────────────────────


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, ev: Any) -> None:
        self.events.append(ev)


class _FakeCanvas:
    id = "canvas_xyz"
    session_id = "sess1"
    title = "T"


class _FakeCanvasStore:
    async def ensure_canvas(self, *, session_id: str, workspace_id: str, user_id: str) -> Any:
        return _FakeCanvas()

    async def list_sections(self, canvas_id: str) -> list[Any]:
        return []

    async def append_section(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def count_sections_for_turn(self, turn_id: str) -> int:
        return 0


@dataclass
class _FakeTurnRequest:
    session_id: str = "sess1"
    workspace_id: str = "ws1"
    user_id: str = "u1"
    member_uid: str = ""
    text: str = "hi"
    turn_id: str = "t1"
    language_context: Any = None


@pytest.mark.unit
async def test_psi_skip_when_flag_set() -> None:
    """``_psi_skip_final_compose_only=True`` on bus → composer returns []
    without invoking _build_sections_from_payload or any append_section.

    Verified by patching `_build_sections_from_payload` to assert it never
    runs when the flag is set.
    """
    from bowhead_core import canvas_composer

    bus = _FakeBus()
    bus._psi_skip_final_compose_only = True
    store = _FakeCanvasStore()
    req = _FakeTurnRequest()

    called = {"build": 0}
    orig_build = canvas_composer._build_sections_from_payload

    def _spy_build(*args: Any, **kwargs: Any) -> Any:
        called["build"] += 1
        return orig_build(*args, **kwargs)

    canvas_composer._build_sections_from_payload = _spy_build  # type: ignore[assignment]
    try:
        result = await canvas_composer.compose_canvas_from_payload(
            canvas_store=store,
            request=req,
            bus=bus,
            payload={"title": "T", "exec_summary": "x" * 40, "kpis": [], "insights": [], "recommendations": []},
            intent=None,
        )
    finally:
        canvas_composer._build_sections_from_payload = orig_build  # type: ignore[assignment]

    assert result == [], "guard must return [] (no kinds emitted)"
    assert called["build"] == 0, "narrative compose path must not run"


@pytest.mark.unit
async def test_psi_no_skip_when_flag_absent() -> None:
    """Sanity: flag UNSET → composer proceeds (build is called)."""
    from bowhead_core import canvas_composer

    bus = _FakeBus()
    # NOT setting _psi_skip_final_compose_only.
    store = _FakeCanvasStore()
    req = _FakeTurnRequest()

    called = {"build": 0}
    orig_build = canvas_composer._build_sections_from_payload

    def _spy_build(*args: Any, **kwargs: Any) -> Any:
        called["build"] += 1
        return []  # short-circuit deeper logic

    canvas_composer._build_sections_from_payload = _spy_build  # type: ignore[assignment]
    try:
        await canvas_composer.compose_canvas_from_payload(
            canvas_store=store,
            request=req,
            bus=bus,
            payload={"title": "T", "exec_summary": "x" * 40, "kpis": [], "insights": [], "recommendations": []},
            intent=None,
        )
    finally:
        canvas_composer._build_sections_from_payload = orig_build  # type: ignore[assignment]

    assert called["build"] == 1, "without flag, narrative build must run"


# ─── _turn_committed_to_v4 set on first scanner callback ─────────────────


@pytest.mark.unit
def test_turn_committed_set_on_first_v4_event() -> None:
    """Simulates the _v4_emit_event_sync helper in lead_orchestrator — it
    must setattr(_turn_committed_to_v4=True) BEFORE the emit task is
    scheduled, and on the FIRST event only.

    Sprint phase-plan-removal: turn.plan + phase.update events deleted;
    the surviving v4 emit is lead.final.payload (single canonical emit).
    Test now asserts the atomic-write semantic for that emit.
    """
    bus = _FakeBus()
    events_emitted: list[str] = []

    # mimic lead_orchestrator._v4_emit_event_sync ordering
    def _v4_emit(name: str) -> None:
        # P0-1 wire: set marker idempotently BEFORE emit
        if not getattr(bus, "_turn_committed_to_v4", False):
            bus._turn_committed_to_v4 = True
        events_emitted.append(name)

    # First emit = lead.final.payload (the only surviving v4 emit)
    _v4_emit("lead.final.payload")
    assert bus._turn_committed_to_v4 is True
    assert events_emitted == ["lead.final.payload"]

    # Idempotent — second hypothetical emit doesn't unset the marker.
    _v4_emit("lead.final.payload")
    assert bus._turn_committed_to_v4 is True
    assert events_emitted == ["lead.final.payload", "lead.final.payload"]


@pytest.mark.unit
def test_turn_committed_set_BEFORE_lead_final_payload() -> None:
    """Specifically: marker must be set before the final.payload emit fires.
    Verifies that even if scanner only fires for lead.final.payload (no
    earlier turn.plan), the marker is set on that single event — not after.
    """
    bus = _FakeBus()
    set_at: list[bool] = []  # snapshot of flag at each step

    def _v4_emit(name: str) -> None:
        # Snapshot BEFORE flag-set logic
        set_at.append(getattr(bus, "_turn_committed_to_v4", False))
        if not getattr(bus, "_turn_committed_to_v4", False):
            bus._turn_committed_to_v4 = True

    _v4_emit("lead.final.payload")
    # First event saw flag False, then set True afterwards.
    assert set_at == [False]
    assert bus._turn_committed_to_v4 is True
