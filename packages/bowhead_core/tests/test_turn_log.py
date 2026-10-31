"""TurnLogStore + log_sink 接线 测试."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from bowhead_core.events import (
    EventBusRegistry,
    OrchestratorEvent,
    TerminalDurabilityAbort,
    TurnEventBus,
)

from bowhead_core import OrderedTurnLogSink, TurnLogStore

if TYPE_CHECKING:
    from pathlib import Path


async def test_store_records_and_lists(tmp_path: Path) -> None:
    store = TurnLogStore(db_path=str(tmp_path / "log.db"))
    await store.initialize()

    await store.record(turn_id="t1", event_type="turn_started", payload={"x": 1})
    await store.record(turn_id="t1", event_type="headline", payload={"text": "样例"})
    await store.record(turn_id="t2", event_type="turn_started", payload={})

    t1 = await store.list_events("t1")
    assert [r.seq for r in t1] == [1, 2]
    assert [r.event_type for r in t1] == ["turn_started", "headline"]
    assert t1[1].payload == {"text": "样例"}
    assert await store.max_seq("t1") == 2
    assert await store.max_seq("missing") == 0

    ids = await store.list_turn_ids()
    assert set(ids) == {"t1", "t2"}


async def test_bus_sink_persists(tmp_path: Path) -> None:
    store = TurnLogStore(db_path=str(tmp_path / "log.db"))
    await store.initialize()

    async def sink(e: OrchestratorEvent) -> None:
        await store.record(
            turn_id=e.turn_id,
            event_type=e.event_type,
            payload=e.payload,
            occurred_at=e.occurred_at,
        )

    reg = EventBusRegistry(default_log_sink=sink)
    bus = await reg.get_or_create("tt")
    await bus.emit(OrchestratorEvent(event_type="turn_started", turn_id="tt", payload={"a": 1}))
    await bus.emit(OrchestratorEvent(event_type="done", turn_id="tt", payload={"status": "completed"}))

    # drain
    evs = []
    async for e in bus.subscribe():
        evs.append(e)
    assert len(evs) == 2

    # Persisted.
    rows = await store.list_events("tt")
    assert [r.event_type for r in rows] == ["turn_started", "done"]


async def test_ordered_sink_keeps_earlier_background_write_before_durable_event() -> None:
    """A durable pointer cannot overtake a previously accepted normal event."""

    class DelayedStore:
        def __init__(self) -> None:
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()
            self.order: list[str] = []

        async def record(
            self,
            *,
            turn_id: str,
            event_type: str,
            payload: dict,
            occurred_at: str,
        ) -> object:
            del turn_id, payload, occurred_at
            if event_type == "normal":
                self.first_started.set()
                await self.release_first.wait()
            self.order.append(event_type)
            return object()

    store = DelayedStore()
    sink = OrderedTurnLogSink(store)  # type: ignore[arg-type]
    normal = OrchestratorEvent(event_type="normal", turn_id="tt", payload={})
    durable = OrchestratorEvent(
        event_type="subagent.draft_ready",
        turn_id="tt",
        payload={},
        persist_before_delivery=True,
    )

    assert await sink(normal) is True
    await store.first_started.wait()
    turn_drain = asyncio.create_task(sink.drain_turn("tt"))
    await asyncio.sleep(0)
    assert not turn_drain.done()
    durable_call = asyncio.create_task(sink(durable))
    await asyncio.sleep(0)
    assert not durable_call.done()

    store.release_first.set()
    await turn_drain
    assert await durable_call is True
    assert store.order == ["normal", "subagent.draft_ready"]
    await sink.drain()


async def test_ordered_sink_rejects_tail_after_preceding_write_failure() -> None:
    """Concurrent emitters cannot persist or publish past a failed prefix."""

    class GatedFailingStore:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls: list[str] = []

        async def record(self, **kwargs):
            self.calls.append(str(kwargs["event_type"]))
            self.started.set()
            await self.release.wait()
            return

    store = GatedFailingStore()
    sink = OrderedTurnLogSink(store)  # type: ignore[arg-type]
    bus = TurnEventBus(log_sink=sink, persist_before_live=True)
    first = asyncio.create_task(
        bus.emit(OrchestratorEvent("partial_answer", "t_failed_prefix", {})),
    )
    await store.started.wait()
    second = asyncio.create_task(
        bus.emit(OrchestratorEvent("step_started", "t_failed_prefix", {})),
    )
    await asyncio.sleep(0)
    store.release.set()

    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, TerminalDurabilityAbort) for result in results)
    assert store.calls == ["partial_answer"]
    assert bus.snapshot() == []


@pytest.mark.parametrize("persist_before_live", [True, False])
async def test_cancelled_emitter_cannot_cancel_shared_ordered_tail(
    persist_before_live: bool,
) -> None:
    class GatedStore:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls: list[str] = []

        async def record(self, **kwargs):
            event_type = str(kwargs["event_type"])
            self.calls.append(event_type)
            if event_type == "partial_answer":
                self.started.set()
                await self.release.wait()
            return object()

    store = GatedStore()
    sink = OrderedTurnLogSink(store)  # type: ignore[arg-type]
    bus = TurnEventBus(
        log_sink=sink,
        persist_before_live=persist_before_live,
    )
    peer = asyncio.create_task(
        bus.emit(
            OrchestratorEvent(
                "partial_answer",
                "t_cancelled_emitter",
                {"text": "peer"},
                persist_before_delivery=not persist_before_live,
            ),
        ),
    )
    await store.started.wait()
    root_done = asyncio.create_task(
        bus.emit(
            OrchestratorEvent(
                "done",
                "t_cancelled_emitter",
                {"status": "completed"},
                persist_before_delivery=not persist_before_live,
            ),
        ),
    )
    await asyncio.sleep(0)

    peer.cancel()
    await asyncio.sleep(0)
    assert not peer.done()
    peer.cancel()
    await asyncio.sleep(0)
    assert not peer.done()
    store.release.set()
    with pytest.raises(asyncio.CancelledError):
        await peer
    assert await root_done is True
    await sink.drain_turn("t_cancelled_emitter")

    assert store.calls == ["partial_answer", "done"]
    assert bus._fatal_error is None
    assert bus._closed is True
    assert [event.event_type for event in bus.snapshot()] == [
        "partial_answer",
        "done",
    ]


async def test_cancelled_durable_publish_surfaces_persistence_failure_first() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def failing_sink(_event: OrchestratorEvent) -> bool:
        started.set()
        await release.wait()
        raise RuntimeError("storage unavailable")

    bus = TurnEventBus(log_sink=failing_sink, persist_before_live=True)
    emitter = asyncio.create_task(
        bus.emit(
            OrchestratorEvent(
                "partial_answer",
                "t_cancelled_failed_publish",
                {"text": "must not become live"},
            )
        )
    )
    await started.wait()
    emitter.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(TerminalDurabilityAbort):
        await emitter
    assert bus._fatal_error is not None
    assert bus.snapshot() == []


async def test_cancelled_critical_sink_is_a_durability_abort() -> None:
    async def cancelled_sink(_event: OrchestratorEvent) -> bool:
        raise asyncio.CancelledError("sink stopped")

    bus = TurnEventBus(log_sink=cancelled_sink, persist_before_live=True)

    with pytest.raises(TerminalDurabilityAbort):
        await bus.emit(
            OrchestratorEvent(
                "partial_answer",
                "t_cancelled_sink",
                {"text": "must not become live"},
            )
        )
    assert bus._fatal_error is not None
    assert bus.snapshot() == []


async def test_terminal_persistence_fence_rejects_late_frame() -> None:
    terminal_started = asyncio.Event()
    release_terminal = asyncio.Event()
    persisted: list[str] = []

    async def gated_sink(event: OrchestratorEvent) -> bool:
        if event.event_type == "done":
            terminal_started.set()
            await release_terminal.wait()
        persisted.append(event.event_type)
        return True

    bus = TurnEventBus(log_sink=gated_sink, persist_before_live=True)
    terminal = asyncio.create_task(
        bus.emit(OrchestratorEvent("done", "t_terminal_fence", {})),
    )
    await terminal_started.wait()

    assert not await bus.emit(
        OrchestratorEvent("narrative.delta", "t_terminal_fence", {}),
    )
    release_terminal.set()
    assert await terminal
    assert persisted == ["done"]
    assert [event.event_type for event in bus.snapshot()] == ["done"]


async def test_concurrent_terminal_cannot_bypass_rejected_hook() -> None:
    hook_started = asyncio.Event()
    release_hook = asyncio.Event()
    persisted: list[str] = []

    async def sink(event: OrchestratorEvent) -> bool:
        persisted.append(event.event_type)
        return True

    async def reject_terminal() -> bool:
        hook_started.set()
        await release_hook.wait()
        return False

    bus = TurnEventBus(log_sink=sink, persist_before_live=True)
    bus._before_terminal_emit = reject_terminal  # type: ignore[attr-defined]
    first = asyncio.create_task(
        bus.emit(OrchestratorEvent("done", "t_hook_fence", {})),
    )
    await hook_started.wait()

    assert not await bus.emit(
        OrchestratorEvent("done", "t_hook_fence", {}),
    )
    release_hook.set()
    assert not await first
    assert persisted == []
    assert bus.snapshot() == []


async def test_canvas_opener_cannot_finish_after_concurrent_terminal() -> None:
    class GatedStore:
        def __init__(self) -> None:
            self.streaming_started = asyncio.Event()
            self.release_streaming = asyncio.Event()
            self.persisted: list[str] = []

        async def record(self, **kwargs):
            event_type = str(kwargs["event_type"])
            if event_type == "canvas.turn_streaming":
                self.streaming_started.set()
                await self.release_streaming.wait()
            self.persisted.append(event_type)
            return object()

    store = GatedStore()
    sink = OrderedTurnLogSink(store)  # type: ignore[arg-type]
    bus = TurnEventBus(log_sink=sink, persist_before_live=True)
    opener = asyncio.create_task(
        bus.emit(
            OrchestratorEvent(
                "canvas.section_added",
                "t_canvas_terminal_race",
                {},
            )
        ),
    )
    await store.streaming_started.wait()
    terminal = asyncio.create_task(
        bus.emit(
            OrchestratorEvent("done", "t_canvas_terminal_race", {}),
        ),
    )
    await asyncio.sleep(0)
    store.release_streaming.set()

    assert not await opener
    assert await terminal
    await sink.drain_turn("t_canvas_terminal_race")
    assert store.persisted == ["canvas.turn_streaming", "done"]
    assert [event.event_type for event in bus.snapshot()] == [
        "canvas.turn_streaming",
        "done",
    ]


async def test_cancelled_drain_waiter_cannot_cancel_shared_tail() -> None:
    class GatedStore:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls: list[str] = []

        async def record(self, **kwargs):
            self.started.set()
            await self.release.wait()
            self.calls.append(str(kwargs["event_type"]))
            return object()

    store = GatedStore()
    sink = OrderedTurnLogSink(store)  # type: ignore[arg-type]
    assert await sink(
        OrchestratorEvent(
            "partial_answer",
            "t_cancelled_drain",
            {"text": "durable after waiter cancellation"},
        ),
    )
    await store.started.wait()
    waiter = asyncio.create_task(sink.drain_turn("t_cancelled_drain"))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    store.release.set()
    await sink.drain()
    assert store.calls == ["partial_answer"]


async def test_durable_event_id_is_insert_once_and_suppresses_duplicate_live_delivery(
    tmp_path: Path,
) -> None:
    store = TurnLogStore(db_path=str(tmp_path / "log.db"))
    await store.initialize()
    sink = OrderedTurnLogSink(store)
    event_id = "evt_clarification_resolution_req_1"

    first = OrchestratorEvent(
        event_type="clarification.resolved",
        turn_id="tt",
        payload={"request_id": "req_1", "chosen_value": "A"},
        persist_before_delivery=True,
        durable_event_id=event_id,
    )
    duplicate = OrchestratorEvent(
        event_type="clarification.resolved",
        turn_id="tt",
        payload={"request_id": "req_1", "chosen_value": "A"},
        persist_before_delivery=True,
        durable_event_id=event_id,
    )

    assert await sink(first) is True
    assert first.suppress_live_delivery is False
    assert await sink(duplicate) is True
    assert duplicate.suppress_live_delivery is True
    rows = await store.list_events("tt")
    assert [(row.id, row.event_type) for row in rows] == [(event_id, "clarification.resolved")]


@pytest.mark.parametrize("conflict", [False, True])
async def test_owned_transaction_event_delivery_keeps_canonical_cursor_and_rejects_conflicts(tmp_path: Path, conflict: bool) -> None:
    store = TurnLogStore(db_path=str(tmp_path / "log.db"))
    await store.initialize()
    payload = {"phase": "committed", "seq": 40, "evt_seq": 40}
    await store.record(turn_id="tt", event_type="agent.context", payload=payload, event_id="evt_committed")
    bus = TurnEventBus(log_sink=OrderedTurnLogSink(store), persist_before_live=True)
    event = OrchestratorEvent(
        event_type="agent.context",
        turn_id="tt",
        payload={"phase": "skipped" if conflict else "committed"},
        durable_event_id="evt_committed",
        persist_before_delivery=True,
        deliver_existing_durable=True,
    )
    if conflict:
        with pytest.raises(TerminalDurabilityAbort):
            await bus.emit(event)
        assert bus.snapshot() == []
        return
    assert await bus.emit(event)
    assert event.seq == 40 and event.payload == payload
    assert await bus.emit(OrchestratorEvent("done", "tt", {"status": "completed"}))
    assert [item.seq for item in bus.snapshot()] == [40, 41]
    assert not await bus.emit(event)
    assert len(bus.snapshot()) == 2
    assert len(await store.list_events("tt")) == 2


async def test_terminal_diagnostics_survive_persist_and_replay_unchanged(
    tmp_path: Path,
) -> None:
    """The terminal pair must keep one correlation object through turn_events."""

    store = TurnLogStore(db_path=str(tmp_path / "log.db"))
    await store.initialize()

    async def sink(e: OrchestratorEvent) -> None:
        await store.record(
            turn_id=e.turn_id,
            event_type=e.event_type,
            payload=e.payload,
            occurred_at=e.occurred_at,
        )

    diagnostics = {
        "schema_version": 1,
        "disclosure": "authenticated",
        "diagnostic_code": "chat_turn_failed",
        "occurrence_id": "0123456789abcdef0123456789abcdef",
        "turn_id": "t_failed",
        "safe_detail": "phase=chat; failure_class=timeout",
        "retryable": True,
    }
    reg = EventBusRegistry(default_log_sink=sink)
    bus = await reg.get_or_create("t_failed")
    await bus.emit(
        OrchestratorEvent(
            event_type="turn_failed",
            turn_id="t_failed",
            payload={
                "reason": "wallclock_exceeded",
                "diagnostics": diagnostics,
            },
        )
    )
    await bus.emit(
        OrchestratorEvent(
            event_type="done",
            turn_id="t_failed",
            payload={
                "status": "error",
                "reason_code": "wallclock_exceeded",
                "diagnostics": diagnostics,
            },
        )
    )

    rows = await store.list_events("t_failed")
    assert [row.event_type for row in rows] == ["turn_failed", "done"]
    assert rows[0].payload["diagnostics"] == diagnostics
    assert rows[1].payload["diagnostics"] == diagnostics


async def test_persisted_payload_carries_bus_seq_for_replay_dedup(tmp_path: Path) -> None:
    """fix/sse-reconnect-resilience — 持久化(→replay)的 payload 必须带 bus seq.

    断线重连时 gateway 的 /chat/stream 重连 replay 与 /turns/{id}/events 都直接
    dump 存储的 payload (不经 to_sse). 若 payload 不带 seq, FE 的 seenSeq 去重在重连
    全量 replay 时失效 → append 型 reducer (chips/traceSteps/narrative) 翻倍 (这正是
    CTO review 抓到的 prod no-op). emit 在写 sink 前把 bus seq setdefault 进 payload;
    这里断言它确实落到了存储里 + 与 bus 单调序一致 + 不破坏业务字段.
    """
    store = TurnLogStore(db_path=str(tmp_path / "log.db"))
    await store.initialize()

    async def sink(e: OrchestratorEvent) -> None:
        await store.record(
            turn_id=e.turn_id,
            event_type=e.event_type,
            payload=e.payload,
            occurred_at=e.occurred_at,
        )

    reg = EventBusRegistry(default_log_sink=sink)
    bus = await reg.get_or_create("tt")
    # Callers omit seq (actual emit sites); bus fills and stores in payload.
    await bus.emit(OrchestratorEvent(event_type="turn_started", turn_id="tt", payload={"a": 1}))
    await bus.emit(OrchestratorEvent(event_type="tool_call", turn_id="tt", payload={"call_id": "c1"}))
    await bus.emit(OrchestratorEvent(event_type="done", turn_id="tt", payload={"status": "completed"}))

    rows = await store.list_events("tt")
    # Every persisted/replay payload has bus seq, monotonic 1..N for frontend dedup.
    assert [r.payload.get("seq") for r in rows] == [1, 2, 3]
    # Business fields intact.
    assert rows[0].payload["a"] == 1
    assert rows[1].payload["call_id"] == "c1"

    # setdefault preserves existing seq (e.g. canvas.section_added section-order seq).
    # Separate turn: done closed tt bus (_closed=True), later emits dropped.
    bus2 = await reg.get_or_create("cc")
    await bus2.emit(
        OrchestratorEvent(
            event_type="canvas.section_added",
            turn_id="cc",
            payload={"seq": 99, "section_id": "s1"},
        )
    )
    rows_cc = await store.list_events("cc")
    assert rows_cc[-1].payload["seq"] == 99


async def test_bus_seed_continues_after_persisted_seq_for_resume(tmp_path: Path) -> None:
    """Resume bus seq must continue after replayed persisted events.

    /chat/stream first replays persisted rows, then attaches to the live bus.
    If a resume-created bus starts from 1 again, the frontend per-turn seq
    dedupe treats live events as replay duplicates and drops them.
    """
    store = TurnLogStore(db_path=str(tmp_path / "log.db"))
    await store.initialize()

    async def sink(e: OrchestratorEvent) -> None:
        await store.record(
            turn_id=e.turn_id,
            event_type=e.event_type,
            payload=e.payload,
            occurred_at=e.occurred_at,
        )

    reg = EventBusRegistry(default_log_sink=sink)
    bus1 = await reg.get_or_create("tt")
    await bus1.emit(
        OrchestratorEvent(
            event_type="narrative.delta",
            turn_id="tt",
            payload={"delta": "old"},
        ),
    )
    await bus1.emit(
        OrchestratorEvent(
            event_type="done",
            turn_id="tt",
            payload={"status": "clarification_pending"},
        ),
    )
    assert await store.max_seq("tt") == 2

    await reg.close("tt")
    bus2 = await reg.get_or_create("tt", initial_seq=await store.max_seq("tt"))
    await bus2.emit(
        OrchestratorEvent(
            event_type="narrative.delta",
            turn_id="tt",
            payload={"delta": "new"},
        ),
    )

    rows = await store.list_events("tt")
    assert [r.seq for r in rows] == [1, 2, 3]
    assert [r.payload["seq"] for r in rows] == [1, 2, 3]
    assert rows[-1].payload["delta"] == "new"


async def test_registry_close_wakes_subscriber_before_propagating_repeated_cancellation() -> None:
    """A popped generation cannot strand its last condition waiter."""

    reg = EventBusRegistry()
    bus = await reg.get_or_create("t_close_cancel")

    async def drain() -> list[OrchestratorEvent]:
        return [event async for event in bus.subscribe()]

    subscriber = asyncio.create_task(drain())
    await asyncio.sleep(0)

    async with bus._cond:
        close_task = asyncio.create_task(
            reg.close("t_close_cancel", expected=bus),
        )
        for _ in range(20):
            projected, _floor = await reg.projection_checkpoint(
                "t_close_cancel",
            )
            if projected is None:
                break
            await asyncio.sleep(0)
        else:
            pytest.fail("close did not remove the exact bus generation")

        close_task.cancel()
        await asyncio.sleep(0)
        close_task.cancel()
        await asyncio.sleep(0)
        assert not close_task.done()
        assert not subscriber.done()

    with pytest.raises(asyncio.CancelledError):
        await close_task
    assert await asyncio.wait_for(subscriber, timeout=1) == []


async def test_sink_failure_does_not_break_bus(tmp_path: Path) -> None:
    """sink 报错时 bus.emit 仍正常投递，subscribe 不丢事件."""

    async def bad_sink(e: OrchestratorEvent) -> None:
        raise RuntimeError("sink broken")

    reg = EventBusRegistry(default_log_sink=bad_sink)
    bus = await reg.get_or_create("t")
    await bus.emit(OrchestratorEvent(event_type="turn_started", turn_id="t", payload={}))
    await bus.emit(OrchestratorEvent(event_type="done", turn_id="t", payload={}))
    got = []
    async for e in bus.subscribe():
        got.append(e.event_type)
    assert got == ["turn_started", "done"]


async def test_record_row_seq_ignores_payload_section_seq(tmp_path: Path) -> None:
    """chat-solidify O6 回滚回归锁(2026-07-04) — 行 seq 走 MAX+1, 绝不读 payload["seq"]。

    canvas.section_added 等事件 payload 里的 "seq" 是 section-order 下标(0/1/2…),
    不是 bus seq。若拿它当行 seq → 与真事件的行撞 UNIQUE(turn_id,seq) → record
    吞异常 → 段落静默丢库 → F5 掉段。这里模拟一条真事件(行 seq 1)后来一条 payload
    seq=0 的 canvas 段: 行 seq 必须是 2(MAX+1), 不能是 0; 且两条都在。
    """
    store = TurnLogStore(db_path=str(tmp_path / "t.db"))
    await store.initialize()
    r1 = await store.record(turn_id="tc", event_type="turn_started", payload={"seq": 1})
    # Canvas payload seq=0 (section order): row-seq misuse may avoid collision here but breaks order;
    # another payload seq=1 section collides with r1 row seq=1.
    r_sec0 = await store.record(turn_id="tc", event_type="canvas.section_added", payload={"seq": 0, "section_id": "s0"})
    r_sec1 = await store.record(turn_id="tc", event_type="canvas.section_added", payload={"seq": 1, "section_id": "s1"})
    assert r1 is not None and r1.seq == 1
    assert r_sec0 is not None and r_sec0.seq == 2  # MAX+1, not payload 0.
    assert r_sec1 is not None and r_sec1.seq == 3  # MAX+1, not payload 1 (collides r1).
    rows = await store.list_events("tc")
    assert [r.seq for r in rows] == [1, 2, 3]  # All three, none silently lost.
    # Preserve section-order payload seq (frontend canvas-store upserts by it).
    assert rows[1].payload["seq"] == 0
    assert rows[2].payload["seq"] == 1
