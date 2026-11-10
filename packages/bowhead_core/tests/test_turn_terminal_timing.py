"""Root ``done`` duration and steering acknowledgement time on the event bus.

Chat dogfood 2026-11-08 F7: a failed Turn's ``done`` had no ``duration_ms``
because its producer bypassed Lead's done chokepoint. The bus now stamps the
current generation's wall time onto every root ``done`` that lacks one.
F10: steering acknowledgements carry their event time, so the steering byline
can show it live and on replay.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from bowhead_core.events import OrchestratorEvent, TurnEventBus, make_steer_ack_event

pytestmark = pytest.mark.unit


class _RecordingSink:
    """Durable sink stand-in that records each persisted payload."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, event: OrchestratorEvent) -> bool:
        self.rows.append((event.event_type, dict(event.payload)))
        return True


def _durable_bus(*, initial_seq: int = 0, started_seconds_ago: float | None = None) -> tuple[TurnEventBus, _RecordingSink]:
    sink = _RecordingSink()
    bus = TurnEventBus(log_sink=sink, persist_before_live=True, initial_seq=initial_seq)
    if started_seconds_ago is not None:
        # TurnRunner anchors each generation when it emits turn_started.
        bus._turn_wall_t0_perf = time.perf_counter() - started_seconds_ago  # type: ignore[attr-defined]
    return bus, sink


def _done(status: str, **fields: Any) -> OrchestratorEvent:
    return OrchestratorEvent(event_type="done", turn_id="t_timing", payload={"turn_id": "t_timing", "status": status, **fields})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "fields"),
    [
        ("failed", {"error": "internal_error", "error_kind": "lead_internal_error", "reason_code": "lead_internal_error"}),
        ("cancelled", {"error_kind": "user_cancel"}),
        ("clarification_pending", {"error_kind": "needs_clarification"}),
    ],
    ids=["worker_failure", "cancelled", "clarification"],
)
async def test_root_done_without_duration_carries_generation_wall_time(status: str, fields: dict[str, Any]) -> None:
    bus, sink = _durable_bus(started_seconds_ago=55.0)

    assert await bus.emit(_done(status, **fields))

    live = bus.snapshot()[-1].payload
    assert 55_000 <= live["duration_ms"] < 60_000
    assert sink.rows[-1] == ("done", live)
    # The turn summary reads the same value as the terminal frame.
    assert bus._turn_wall_duration_ms == live["duration_ms"]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_existing_done_duration_is_never_overwritten() -> None:
    bus, sink = _durable_bus(started_seconds_ago=30.0)

    assert await bus.emit(_done("completed", duration_ms=6646))

    assert bus.snapshot()[-1].payload["duration_ms"] == 6646
    assert sink.rows[-1][1]["duration_ms"] == 6646


@pytest.mark.asyncio
async def test_resumed_generation_is_measured_from_its_own_start() -> None:
    paused, _ = _durable_bus(started_seconds_ago=120.0)
    assert await paused.emit(_done("clarification_pending"))
    resumed, sink = _durable_bus(initial_seq=31, started_seconds_ago=2.0)

    assert await resumed.emit(_done("failed", reason_code="lead_internal_error"))

    assert 120_000 <= paused.snapshot()[-1].payload["duration_ms"] < 125_000
    assert 2_000 <= sink.rows[-1][1]["duration_ms"] < 10_000


@pytest.mark.asyncio
async def test_done_without_generation_start_does_not_invent_a_duration() -> None:
    bus, sink = _durable_bus()

    assert await bus.emit(_done("failed", reason_code="lead_internal_error"))

    assert "duration_ms" not in bus.snapshot()[-1].payload
    assert "duration_ms" not in sink.rows[-1][1]


@pytest.mark.asyncio
async def test_already_persisted_terminal_frame_is_left_unchanged() -> None:
    bus, sink = _durable_bus(started_seconds_ago=5.0)

    assert await bus.emit(_done("failed", event_id="evt_v1_done", reason_code="lead_internal_error"))

    assert "duration_ms" not in sink.rows[-1][1]


def test_steer_ack_carries_its_event_time_in_iso_utc() -> None:
    before = datetime.now(UTC)
    event = make_steer_ack_event("t_steer", target_msg_id="u_1", text="Use green bars.")
    after = datetime.now(UTC)

    payload = event.payload
    assert event.event_type == "emoji_ack"
    assert payload == {"target_msg_id": "u_1", "emoji": "🤝", "text": "Use green bars.", "ts": payload["ts"]}
    ts = datetime.fromisoformat(payload["ts"])
    assert ts.utcoffset() == timedelta(0)
    assert before <= ts <= after
    # The payload time is the event's own time, without the uniqueness suffix.
    assert event.occurred_at.split("#", 1)[0] == payload["ts"]


@pytest.mark.asyncio
async def test_steer_ack_time_is_persisted_with_the_live_frame() -> None:
    bus, sink = _durable_bus()

    assert await bus.emit(make_steer_ack_event("t_steer", target_msg_id="u_1", text="Use green bars."))

    live = bus.snapshot()[-1].payload
    assert live["ts"]
    assert sink.rows[-1] == ("emoji_ack", live)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "clarification_pending"])
async def test_root_done_carries_its_server_end_time_live_and_persisted(status: str) -> None:
    """Dogfood N3: the reply byline shows the server end time, never the browser clock."""
    bus, sink = _durable_bus(started_seconds_ago=3.0)
    before = datetime.now(UTC)

    assert await bus.emit(_done(status))

    after = datetime.now(UTC)
    event = bus.snapshot()[-1]
    ended_at = datetime.fromisoformat(event.payload["ended_at"])
    assert ended_at.utcoffset() == timedelta(0)
    assert before <= ended_at <= after
    # The payload time is the event's own time, without the uniqueness suffix.
    assert event.payload["ended_at"] == event.occurred_at.split("#", 1)[0]
    assert sink.rows[-1] == ("done", event.payload)


@pytest.mark.asyncio
async def test_existing_end_time_peer_done_and_persisted_terminal_frame_are_left_unchanged() -> None:
    bus, sink = _durable_bus()
    assert await bus.emit(_done("completed", ended_at="2026-11-08T02:49:21.000000+00:00"))
    assert sink.rows[-1][1]["ended_at"] == "2026-11-08T02:49:21.000000+00:00"

    peer_bus, peer_sink = _durable_bus()
    peer = OrchestratorEvent(event_type="done", turn_id="t_timing", payload={"status": "completed"}, parent_agent_id="sa_1")
    assert await peer_bus.emit(peer)
    assert "ended_at" not in peer_sink.rows[-1][1]

    compat_bus, compat_sink = _durable_bus()
    assert await compat_bus.emit(_done("failed", event_id="evt_v1_done", reason_code="lead_internal_error"))
    assert "ended_at" not in compat_sink.rows[-1][1]
