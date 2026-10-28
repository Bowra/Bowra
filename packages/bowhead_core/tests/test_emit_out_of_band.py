"""dingsiyan #11 — ``EventBusRegistry.emit_out_of_band`` 三分支契约.

pause 后（done(clarification_pending) 已 emit）补发事件的唯一入口。锁死两个
真机死法（prod t_1ccf9e0aec11）：
- <5s：closed bus 还挂在 registry → 老实现 emit 早退，事件连库都不落；
- >5s：bus 已 pop → 老实现建 seq 从 0 计数的新 bus → payload seq 撞号，
  FE 重放去重误杀（clarification.resolved 收卡逆操作在生产必死）。
"""

from __future__ import annotations

import pytest
from bowhead_core.events import EventBusRegistry, OrchestratorEvent
from bowhead_core.turn_log_store import OrderedTurnLogSink

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _Sink:
    def __init__(self) -> None:
        self.rows: list[OrchestratorEvent] = []

    async def __call__(self, event: OrchestratorEvent) -> None:
        self.rows.append(event)


class _AsyncSink:
    """Models prod's fire-and-forget log_sink: `record()` is scheduled but the
    row isn't visible until the task runs (main.py `asyncio.create_task`). A
    `max_seq()` read before the task runs sees the STALE floor — the exact race
    behind review P1-B that the synchronous `_Sink` above cannot reproduce."""

    def __init__(self) -> None:
        self.rows: list[OrchestratorEvent] = []
        self._pending: list[OrchestratorEvent] = []

    async def __call__(self, event: OrchestratorEvent) -> None:
        # scheduled, not yet committed — mirrors create_task(record(...))
        self._pending.append(event)

    def flush(self) -> None:
        self.rows.extend(self._pending)
        self._pending.clear()

    def max_seq(self) -> int:
        return max((int(r.payload.get("seq", 0)) for r in self.rows), default=0)


def _ev(event_type: str, turn_id: str) -> OrchestratorEvent:
    return OrchestratorEvent(event_type=event_type, turn_id=turn_id, payload={})


async def test_open_bus_emits_live_with_continued_seq() -> None:
    """token-kind 澄清在 turn 进行中被 resolve：open bus 直发，seq 接续。"""
    sink = _Sink()
    registry = EventBusRegistry(default_log_sink=sink)
    bus = await registry.get_or_create("t_oob_open")
    await bus.emit(_ev("turn_started", "t_oob_open"))

    mode = await registry.emit_out_of_band(
        "t_oob_open",
        _ev("clarification.resolved", "t_oob_open"),
        fallback_initial_seq=1,
    )

    assert mode == "live"
    ring = bus.snapshot()
    assert [e.event_type for e in ring] == ["turn_started", "clarification.resolved"]
    assert ring[-1].payload["seq"] == 2  # Continue the bus counter without restarting it


async def test_registry_boundaries_adapt_fatal_bus_to_ordinary_error() -> None:
    """Business/decision boundaries may handle a failed auxiliary write.

    Direct worker ``bus.emit`` remains BaseException fail-stop; registry-only
    acknowledgements and out-of-band decisions become ordinary exceptions so
    required callers still fail while best-effort callers can omit the frame.
    """
    sink = _Sink()
    registry = EventBusRegistry(default_log_sink=sink)
    bus = await registry.get_or_create("t_oob_fatal")
    bus.latch_fatal_error(RuntimeError("durable sink failed"))

    with pytest.raises(
        RuntimeError,
        match="auxiliary_event_durability_unavailable",
    ) as auxiliary_failure:
        await registry.emit_if_local_producer(
            "t_oob_fatal",
            _ev("emoji_ack", "t_oob_fatal"),
        )
    assert auxiliary_failure.value.__cause__ is not None

    required = _ev("clarification.resolved", "t_oob_fatal")
    required.persist_before_delivery = True
    with pytest.raises(
        RuntimeError,
        match="out_of_band_event_durability_unavailable",
    ) as required_failure:
        await registry.emit_out_of_band(
            "t_oob_fatal",
            required,
        )
    assert required_failure.value.__cause__ is not None
    assert sink.rows == []


async def test_durable_worker_persists_ack_before_live_delivery() -> None:
    class _RecordingStore:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def record(self, **kwargs):
            self.calls.append(kwargs["event_type"])
            return object()

    store = _RecordingStore()
    ordered_sink = OrderedTurnLogSink(store)  # type: ignore[arg-type]
    registry = EventBusRegistry(default_log_sink=ordered_sink)
    bus = await registry.get_or_create(
        "t_optional_ack",
        persist_before_live=True,
    )

    assert await registry.emit_if_local_producer(
        "t_optional_ack",
        _ev("emoji_ack", "t_optional_ack"),
    )
    assert await bus.emit(_ev("narrative.delta", "t_optional_ack"))
    await ordered_sink.drain_turn("t_optional_ack")

    assert store.calls == ["emoji_ack", "narrative.delta"]
    assert [event.event_type for event in bus.snapshot()] == [
        "emoji_ack",
        "narrative.delta",
    ]


async def test_closed_registered_bus_persists_via_transient() -> None:
    """<5s 窗口：done 已置 _closed 但 worker 延迟 close 还没 pop。

    老实现：get_or_create 返回死 bus → emit 早退 → 既不投递也不落库
    （log_sink 在 emit 内部 _closed 检查之后）。新实现必须仍然落库且 seq 播种。
    """
    sink = _Sink()
    registry = EventBusRegistry(default_log_sink=sink)
    bus = await registry.get_or_create("t_oob_closed")
    await bus.emit(_ev("turn_started", "t_oob_closed"))
    await bus.emit(_ev("done", "t_oob_closed"))  # terminal → _closed=True
    assert bus._closed

    mode = await registry.emit_out_of_band(
        "t_oob_closed",
        _ev("clarification.resolved", "t_oob_closed"),
        fallback_initial_seq=2,
    )

    assert mode == "persisted"
    resolved = [e for e in sink.rows if e.event_type == "clarification.resolved"]
    assert len(resolved) == 1
    assert resolved[0].payload["seq"] == 3  # Seed after max to avoid duplicate sequence numbers
    # Do not fill a dead bus's ring (closed buses have no subscribers by definition; delivery requires replay).
    assert "clarification.resolved" not in [e.event_type for e in bus.snapshot()]
    # Do not replace the registry entry (no leaks or interference with resume generation switching).
    assert (await registry.get_or_create("t_oob_closed")) is bus


async def test_absent_bus_persists_without_registering() -> None:
    """>5s 常态：bus 已 pop。老实现新 bus seq 从 1 计数（prod 实证 payload seq=1
    与 turn_started 撞号）。新实现 transient bus 播种落库且不注册进 registry。"""
    sink = _Sink()
    registry = EventBusRegistry(default_log_sink=sink)

    mode = await registry.emit_out_of_band(
        "t_oob_absent",
        _ev("clarification.resolved", "t_oob_absent"),
        fallback_initial_seq=16,
    )

    assert mode == "persisted"
    assert sink.rows[-1].payload["seq"] == 17
    assert "t_oob_absent" not in registry._buses  # No registration = no leak


async def test_open_but_unseeded_bus_gets_defensive_seed() -> None:
    """订阅端（/chat/stream 对 absent turn）裸 get_or_create 建出的 open-未播种
    bus：out-of-band emit 前防御 seed，emit 出的 seq 不撞已落库行。"""
    sink = _Sink()
    registry = EventBusRegistry(default_log_sink=sink)
    bus = await registry.get_or_create("t_oob_seed")  # counter=0

    mode = await registry.emit_out_of_band(
        "t_oob_seed",
        _ev("clarification.resolved", "t_oob_seed"),
        fallback_initial_seq=16,
    )

    assert mode == "live"
    assert bus.snapshot()[-1].payload["seq"] == 17


async def test_seq_floor_survives_stale_maxseq_resolve_then_resume() -> None:
    """review P1-B — resolve→resume 顺序流下 fire-and-forget 落库延迟不再撞号。

    复现 prod t_1ccf9e0aec11 的真实机制（不是并发，是「顺序 + 异步落库」）：
    1. resolve 发 clarification.resolved（seed=M=15），payload seq=16，log_sink 仅
       **调度** record，DB 尚未落库（max_seq 仍读到 15）。
    2. resume 用 turn_seq_seed = max(DB max_seq=15, registry.seq_floor)。老实现只读 DB
       → 15 → turn.resumed 又拿 seq=16 → **撞号**。新实现高水位=16 → seed=16 →
       turn.resumed seq=17，不撞。
    """
    sink = _AsyncSink()
    registry = EventBusRegistry(default_log_sink=sink)
    # Baseline: prior events of the paused turn are committed through seq 15.
    _base = _ev("done", "t_race")
    _base.payload["seq"] = 15
    sink.rows.append(_base)
    assert sink.max_seq() == 15

    # 1) Supplemental resolve emit (bus absent → transient); DB still at 15 (unflushed).
    await registry.emit_out_of_band(
        "t_race",
        _ev("clarification.resolved", "t_race"),
        fallback_initial_seq=15,
    )
    resolved_seq = sink._pending[-1].payload["seq"]
    assert resolved_seq == 16
    stale_db_max = sink.max_seq()  # Persistence task has not run → stale 15
    assert stale_db_max == 15

    # 2) resume seed = max(stale DB, high-water mark) (gateway turn_seq_seed algorithm).
    # emit_out_of_band already synchronously raised the high-water mark to 16.
    assert registry.seq_floor("t_race") == 16
    resume_seed = max(stale_db_max, registry.seq_floor("t_race"))
    assert resume_seed == 16  # Old implementation used only stale_db_max=15 → collision

    # Resume creates a new bus; caller folds floor into initial_seq → turn.resumed seq exceeds resolved.
    bus = await registry.get_or_create("t_race", initial_seq=resume_seed)
    await bus.emit(_ev("turn.resumed", "t_race"))
    resumed_seq = bus.snapshot()[-1].payload["seq"]
    assert resumed_seq == 17
    assert resumed_seq != resolved_seq  # Core assertion: no collision


async def test_durable_resolved_receipt_seeds_resume_on_another_gateway() -> None:
    """The resolve response waits for DB visibility, not a process-local floor."""

    sink = _Sink()
    gateway_a = EventBusRegistry(default_log_sink=sink)
    resolved = _ev("clarification.resolved", "t_cross_gateway")
    resolved.persist_before_delivery = True

    await gateway_a.emit_out_of_band(
        "t_cross_gateway",
        resolved,
        fallback_initial_seq=15,
    )

    # Gateway B has no access to A's memory floor; the committed row is now
    # sufficient to seed its fresh resume generation strictly after resolved.
    db_max = max(int(row.payload["seq"]) for row in sink.rows)
    assert db_max == 16
    gateway_b = EventBusRegistry(default_log_sink=sink)
    resume_bus = await gateway_b.get_or_create(
        "t_cross_gateway",
        initial_seq=db_max,
    )
    await resume_bus.emit(_ev("turn.resumed", "t_cross_gateway"))
    assert resume_bus.snapshot()[-1].payload["seq"] == 17


async def test_durable_out_of_band_rejection_raises() -> None:
    async def _reject(_event: OrchestratorEvent) -> bool:
        return False

    registry = EventBusRegistry(default_log_sink=_reject)
    event = _ev("clarification.resolved", "t_rejected_receipt")
    event.persist_before_delivery = True

    with pytest.raises(RuntimeError, match="durable out-of-band event rejected"):
        await registry.emit_out_of_band(
            "t_rejected_receipt",
            event,
            fallback_initial_seq=3,
        )


async def test_close_snapshots_seq_floor_for_cross_generation_resume() -> None:
    """worker emit 不经 registry；close 时快照 bus 计数器进高水位，让 resume 的
    get_or_create 跨代际接续（即便 DB 落库全部延迟）。"""
    sink = _AsyncSink()
    registry = EventBusRegistry(default_log_sink=sink)
    bus = await registry.get_or_create("t_gen")
    for _ in range(20):
        await bus.emit(_ev("narrative.delta", "t_gen"))
    await bus.emit(_ev("done", "t_gen"))  # counter=21, closed
    assert registry.seq_floor("t_gen") == 0  # Ordinary emit does not update floor
    await registry.close("t_gen")  # close snapshot → floor=21
    assert registry.seq_floor("t_gen") == 21

    # Resume: even if DB max_seq remains 0 (all events in flight), caller folds floor into initial_seq.
    resume_seed = max(0, registry.seq_floor("t_gen"))  # gateway turn_seq_seed algorithm
    bus2 = await registry.get_or_create("t_gen", initial_seq=resume_seed)
    await bus2.emit(_ev("turn.resumed", "t_gen"))
    assert bus2.snapshot()[-1].payload["seq"] == 22


async def test_closed_bus_counter_beats_stale_maxseq() -> None:
    """dogfood #11 收口 —— <5s 窗口：worker bus 已 closed 但延迟 close 未 pop，其
    `_seq_counter`=真实尾 seq（27），而 DB max_seq 落库还停在 25。emit_out_of_band 必须
    从 closed bus counter 播种，让 resolved 落在 paused 尾之后（28），不撞 paused 通道。
    """
    sink = _Sink()
    registry = EventBusRegistry(default_log_sink=sink)
    bus = await registry.get_or_create("t_stale")
    for _ in range(27):
        await bus.emit(_ev("narrative.delta", "t_stale"))
    bus._closed = True  # done set closed, but bus remains registered (deferred close has not run)
    assert bus._seq_counter == 27

    # Fallback passes stale DB max_seq=25 (tail 26/27 still unpersisted in the fire-and-forget queue).
    mode = await registry.emit_out_of_band(
        "t_stale",
        _ev("clarification.resolved", "t_stale"),
        fallback_initial_seq=25,
    )
    assert mode == "persisted"
    resolved = [e for e in sink.rows if e.event_type == "clarification.resolved"]
    assert resolved[0].payload["seq"] == 28  # 27+1, after paused tail (old implementation used 26)


async def test_downgrade_when_open_bus_closed_after_lock() -> None:
    """review BE-P2-2 — open bus 在锁外被并发 close，emit 返回 False → 降级 transient
    落库，事件不丢。"""
    sink = _Sink()
    registry = EventBusRegistry(default_log_sink=sink)
    bus = await registry.get_or_create("t_toctou")
    await bus.emit(_ev("turn_started", "t_toctou"))
    # Simulate close after lock release but before emit (explicit close outside terminal handling).
    bus._closed = True

    mode = await registry.emit_out_of_band(
        "t_toctou",
        _ev("clarification.resolved", "t_toctou"),
        fallback_initial_seq=1,
    )
    assert mode == "persisted"  # Successful fallback
    resolved = [e for e in sink.rows if e.event_type == "clarification.resolved"]
    assert len(resolved) == 1  # Old implementation (no fallback) lost it: closed bus emit returned early
