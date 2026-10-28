"""Sprint ρ Phase E — SSE schema: seq + parent_agent_id.

Tests per tech-design-r2 §2 / r1 §2.5 (UT specification).

Coverage:
1. bus auto-populates monotonic seq per turn (1..N)
2. independent bus instances each start at seq=1 (per-turn isolation)
3. to_sse() with seq=-1 warns + bumps counter + returns seq=0 (warn-only migration)
4. bus-emitted event SSE payload contains "seq": <int>
5. peer-scope emit carries parent_agent_id; SSE payload includes it
6. Lead-scope emit (parent_agent_id=None) — SSE payload omits the field

Per `feedback_ut_must_assert_emit_contract`: FakeBus records full event list
+ asserts payload shape (not just return value).
"""

from __future__ import annotations

import json
import logging

import pytest
from bowhead_core.events import (
    OrchestratorEvent,
    TurnEventBus,
    make_event,
)


# ---------------------------------------------------------------------------
# FakeBus — subclass TurnEventBus so the real seq auto-pop path runs, but
# also records the emitted event list for assertion (per feedback_ut_*).
# ---------------------------------------------------------------------------
class FakeBus(TurnEventBus):
    def __init__(self) -> None:
        super().__init__(log_sink=None)
        self.events: list[OrchestratorEvent] = []

    async def emit(self, event: OrchestratorEvent) -> None:  # type: ignore[override]
        await super().emit(event)
        # Capture AFTER super.emit so seq is already assigned.
        self.events.append(event)


# ---------------------------------------------------------------------------
# Test 1 — seq monotonic per turn
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_event_seq_monotonic_per_turn() -> None:
    bus = FakeBus()
    for i in range(10):
        await bus.emit(
            OrchestratorEvent(
                event_type="tool_call",  # type: ignore[arg-type]
                turn_id="t1",
                payload={"i": i},
            )
        )
    seqs = [e.seq for e in bus.events]
    assert seqs == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10], f"expected strictly increasing 1..10, got {seqs}"
    # All events carry the same turn_id; bus is per-turn.
    assert {e.turn_id for e in bus.events} == {"t1"}


# ---------------------------------------------------------------------------
# Test 2 — per-turn isolation: two buses each start at seq=1
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_event_seq_per_turn_isolated() -> None:
    bus_a = FakeBus()
    bus_b = FakeBus()
    for _ in range(3):
        await bus_a.emit(
            OrchestratorEvent(
                event_type="tool_call",  # type: ignore[arg-type]
                turn_id="t_a",
                payload={},
            )
        )
    for _ in range(2):
        await bus_b.emit(
            OrchestratorEvent(
                event_type="tool_call",  # type: ignore[arg-type]
                turn_id="t_b",
                payload={},
            )
        )
    assert [e.seq for e in bus_a.events] == [1, 2, 3]
    assert [e.seq for e in bus_b.events] == [1, 2]


# ---------------------------------------------------------------------------
# Test 3 - direct to_sse() with seq=-1: warn + seq=0 in payload
# ---------------------------------------------------------------------------
def test_event_to_sse_warns_on_unset_seq(caplog: pytest.LogCaptureFixture) -> None:
    # Construct event WITHOUT going through bus.emit — seq stays at default -1.
    ev = OrchestratorEvent(
        event_type="tool_call",  # type: ignore[arg-type]
        turn_id="t_orphan",
        payload={"k": "v"},
    )
    assert ev.seq == -1, "fresh event should default to seq=-1"

    with caplog.at_level(logging.WARNING, logger="bowhead_core.events"):
        sse = ev.to_sse()

    # Payload must include seq=0 fallback so frontend never sees missing key.
    data = json.loads(sse["data"])
    assert data["seq"] == 0, f"unset seq must surface as 0 fallback, got {data}"
    # Warning must have fired.
    assert any("seq=-1" in rec.message for rec in caplog.records), f"expected seq=-1 warning, got logs: {[r.message for r in caplog.records]}"


# ---------------------------------------------------------------------------
# Test 4 — bus-emitted event SSE payload contains seq
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_event_to_sse_payload_includes_seq() -> None:
    bus = FakeBus()
    await bus.emit(
        OrchestratorEvent(
            event_type="tool_call",  # type: ignore[arg-type]
            turn_id="t1",
            payload={"tool_name": "demo"},
        )
    )
    ev = bus.events[0]
    assert ev.seq == 1
    sse = ev.to_sse()
    data = json.loads(sse["data"])
    assert data["seq"] == 1, f"SSE payload missing seq: {data}"
    # Existing payload field still preserved.
    assert data["tool_name"] == "demo"
    # parent_agent_id NOT injected for Lead-scope emits.
    assert "parent_agent_id" not in data


# ---------------------------------------------------------------------------
# Test 5 — peer-scope emit carries parent_agent_id and SSE includes it
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_parent_agent_id_peer_emit() -> None:
    """Simulates a subagent.py peer-scope emit: parent_agent_id=subagent_id."""
    bus = FakeBus()
    subagent_id = "sub_abc123def0"
    await bus.emit(
        OrchestratorEvent(
            event_type="tool_call",  # type: ignore[arg-type]
            turn_id="t1",
            payload={"subagent_id": subagent_id, "tool_name": "bi_fetch_rows"},
            parent_agent_id=subagent_id,
        )
    )
    ev = bus.events[0]
    assert ev.parent_agent_id == subagent_id
    assert ev.seq == 1
    sse = ev.to_sse()
    data = json.loads(sse["data"])
    assert data["parent_agent_id"] == subagent_id, f"SSE payload must surface parent_agent_id, got {data}"
    assert data["seq"] == 1


# ---------------------------------------------------------------------------
# Test 6 — Lead-scope emit: parent_agent_id=None, omitted from SSE payload
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_parent_agent_id_lead_emit() -> None:
    """Lead-scope emits leave parent_agent_id=None (default) — SSE omits field."""
    bus = FakeBus()
    await bus.emit(
        OrchestratorEvent(
            event_type="peer_dispatched",  # type: ignore[arg-type]
            turn_id="t1",
            payload={"peer_name": "data-analyst"},
            # No parent_agent_id — Lead-scope.
        )
    )
    ev = bus.events[0]
    assert ev.parent_agent_id is None
    sse = ev.to_sse()
    data = json.loads(sse["data"])
    assert "parent_agent_id" not in data, f"Lead-scope SSE payload must NOT include parent_agent_id, got {data}"
    assert data["seq"] == 1
    assert data["peer_name"] == "data-analyst"


# ---------------------------------------------------------------------------
# Bonus — make_event helper sanity check (tech-design r2 §2 step 4).
# Not in the 6-test list but worth a smoke; doesn't add to the 6 count.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_make_event_helper_passes_parent_agent_id() -> None:
    bus = FakeBus()
    ev = make_event(
        "tool_call",  # type: ignore[arg-type]
        "t1",
        {"x": 1},
        parent_agent_id="sub_xyz",
    )
    assert ev.parent_agent_id == "sub_xyz"
    assert ev.seq == -1  # not yet bus-emitted
    await bus.emit(ev)
    assert ev.seq == 1
    data = json.loads(ev.to_sse()["data"])
    assert data["parent_agent_id"] == "sub_xyz"
    assert data["seq"] == 1


@pytest.mark.asyncio
async def test_durable_event_persists_before_live_delivery() -> None:
    order: list[str] = []
    bus: TurnEventBus

    async def sink(_event: OrchestratorEvent) -> bool:
        assert bus.snapshot() == []
        order.append("persisted")
        return True

    bus = TurnEventBus(log_sink=sink)
    event = make_event(
        "cca.subagent_creator_open",
        "t_durable",
        {"draft_id": "sad_1"},
        persist_before_delivery=True,
    )

    assert await bus.emit(event) is True
    order.append("delivered")
    assert order == ["persisted", "delivered"]
    assert bus.snapshot() == [event]


@pytest.mark.asyncio
async def test_durable_event_is_not_delivered_when_persistence_fails() -> None:
    async def sink(_event: OrchestratorEvent) -> bool:
        return False

    bus = TurnEventBus(log_sink=sink)
    event = make_event(
        "cca.subagent_creator_open",
        "t_durable_failed",
        {"draft_id": "sad_1"},
        persist_before_delivery=True,
    )

    assert await bus.emit(event) is False
    assert bus.snapshot() == []


@pytest.mark.asyncio
async def test_durable_event_fails_closed_without_a_log_sink() -> None:
    bus = TurnEventBus()
    event = make_event(
        "cca.subagent_creator_open",
        "t_durable_no_sink",
        {"draft_id": "sad_1"},
        persist_before_delivery=True,
    )

    assert await bus.emit(event) is False
    assert bus.snapshot() == []
