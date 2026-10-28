"""canvas-comprehensive Batch 1 (WP-1 / TD-1) — evt_seq stamp on every event.

`evt_seq` = a per-turn monotonic event-sequence key that ALL events carry, used by
the FE canvas-sse dedup gate (attachCanvasSSE) to drop replayed frames on reconnect
(kills the section_chunk double-accumulate, F2).

The subtle invariant: `seq` in the payload is UNRELIABLE as a canvas dedup key —
canvas.section_added/patched/chunk pre-set payload["seq"] to a section-ORDER value,
which `emit()`'s setdefault preserves. `evt_seq` is stamped by direct assignment from
the bus monotonic counter → always the true event order, never a section order.
"""

from __future__ import annotations

import pytest
from bowhead_core.events import OrchestratorEvent, TurnEventBus


class _FakeBus(TurnEventBus):
    """Subclass so the real emit() seq/evt_seq stamping runs, then capture."""

    def __init__(self) -> None:
        super().__init__(log_sink=None)
        self.events: list[OrchestratorEvent] = []

    async def emit(self, event: OrchestratorEvent) -> None:  # type: ignore[override]
        await super().emit(event)
        self.events.append(event)


@pytest.mark.asyncio
async def test_evt_seq_monotonic_on_every_event() -> None:
    bus = _FakeBus()
    for _ in range(5):
        await bus.emit(
            OrchestratorEvent(
                event_type="tool_call",  # type: ignore[arg-type]
                turn_id="t1",
                payload={},
            )
        )
    evt_seqs = [e.payload["evt_seq"] for e in bus.events]
    assert evt_seqs == [1, 2, 3, 4, 5]
    # evt_seq == the bus monotonic seq for plain events.
    assert [e.seq for e in bus.events] == evt_seqs


@pytest.mark.asyncio
async def test_canvas_section_added_carries_both_section_seq_and_evt_seq() -> None:
    """The regression that motivated TD-1: canvas.section_added's payload `seq` is a
    section-ORDER value (pre-set by the caller); it must be PRESERVED, while `evt_seq`
    must be the monotonic event order — the two diverge and only evt_seq is a valid
    dedup key."""
    bus = _FakeBus()
    # a turn event first → bumps the monotonic counter to 1
    await bus.emit(
        OrchestratorEvent(
            event_type="turn_started",  # type: ignore[arg-type]
            turn_id="t1",
            payload={},
        )
    )
    # canvas.section_added with a pre-set section-order seq=99 (as the real writers do)
    await bus.emit(
        OrchestratorEvent(
            event_type="canvas.section_added",  # type: ignore[arg-type]
            turn_id="t1",
            payload={"canvas_id": "cv1", "section": {"id": "s1"}, "seq": 99},
        )
    )
    # report-streaming P1: bus inserts canvas.turn_streaming before canvas.section_added,
    # so select by type, not index (this case checks the two seq roles, independent of position).
    assert [e.event_type for e in bus.events] == [
        "turn_started",
        "canvas.turn_streaming",
        "canvas.section_added",
    ]
    added = next(e for e in bus.events if e.event_type == "canvas.section_added").payload
    # section-order seq preserved (setdefault didn't override it) …
    assert added["seq"] == 99
    # … but evt_seq is the monotonic event order (3rd event this turn).
    assert added["evt_seq"] == 3


@pytest.mark.asyncio
async def test_evt_seq_present_across_canvas_event_kinds() -> None:
    bus = _FakeBus()
    for etype, payload in [
        ("canvas.opened", {"canvas_id": "cv1"}),
        ("canvas.section_chunk", {"section_id": "s1", "delta": "x"}),
        ("canvas.section_patched", {"section_id": "s1", "patch": {}}),
        ("canvas.done", {"canvas_id": "cv1", "turn_id": "t1"}),
    ]:
        await bus.emit(
            OrchestratorEvent(
                event_type=etype,  # type: ignore[arg-type]
                turn_id="t1",
                payload=payload,
            )
        )
    # report-streaming P1: the first canvas.opened triggers insertion of canvas.turn_streaming,
    # yielding five bus frames. This case requires every Canvas event to carry integer, monotonic evt_seq,
    # so assert the sequence itself rather than hardcoding its length.
    assert [e.event_type for e in bus.events][0] == "canvas.turn_streaming"
    evt_seqs = [e.payload["evt_seq"] for e in bus.events]
    assert evt_seqs == list(range(1, len(bus.events) + 1))
    assert all(isinstance(s, int) for s in evt_seqs)
