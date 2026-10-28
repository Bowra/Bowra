"""Durable legacy-clarification invalidation delivery tests."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from bowhead_core.clarification_store import (
    ClarificationCandidate,
    ClarificationStore,
)
from bowhead_core.events import get_event_registry
from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore
from bowhead_gateway.clarification_invalidation import (
    publish_pending_clarification_invalidations,
    require_durable_event_sink,
    require_shared_core_database,
)


def _install_invalidation_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE clarification_invalidation_outbox (
              request_id TEXT PRIMARY KEY,
              turn_id TEXT NOT NULL,
              event_id TEXT NOT NULL UNIQUE,
              target_status TEXT NOT NULL,
              reason_code TEXT NOT NULL,
              actor_kind TEXT NOT NULL,
              state TEXT NOT NULL DEFAULT 'pending',
              lease_owner TEXT,
              lease_expires_at TEXT,
              attempt_count INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              delivered_at TEXT
            );
            CREATE INDEX idx_clarification_invalidation_outbox_pending
            ON clarification_invalidation_outbox(state, lease_expires_at, created_at);
            CREATE INDEX idx_clarification_invalidation_outbox_turn_claim
            ON clarification_invalidation_outbox(turn_id, state, lease_expires_at);
            """
        )


async def _stores(tmp_path: Path):
    db_path = tmp_path / "core.db"
    clarification_store = ClarificationStore(db_path)
    await clarification_store.initialize()
    _install_invalidation_schema(db_path)
    turn_log_store = TurnLogStore(str(db_path))
    await turn_log_store.initialize()
    return db_path, clarification_store, turn_log_store


async def _pending(clarification_store: ClarificationStore, turn_id: str):
    return await clarification_store.put(
        turn_id=turn_id,
        user_id="u1",
        workspace_id="w1",
        token="",
        rationale="口径不明",
        candidates=[ClarificationCandidate(value="a", label="A")],
    )


def _app(clarification_store, turn_log_store, owner: str):
    return SimpleNamespace(
        state=SimpleNamespace(
            clarification_store=clarification_store,
            turn_log_store=turn_log_store,
            clarification_invalidation_owner=owner,
        ),
    )


@pytest.mark.asyncio
async def test_two_publishers_keep_one_turn_group_ordered(tmp_path: Path) -> None:
    _db_path, clarification_store, turn_log_store = await _stores(tmp_path)
    turn_id = f"t_group_{uuid4().hex}"
    requests = [
        await _pending(clarification_store, turn_id),
        await _pending(clarification_store, turn_id),
    ]
    await clarification_store.invalidate_pending_for_turn(
        turn_id,
        target_status="dismissed",
        reason="scheduled_input_superseded",
        actor="system",
    )

    registry = get_event_registry()
    previous_sink = registry._default_log_sink
    sink = OrderedTurnLogSink(turn_log_store)
    registry.set_log_sink(sink)
    try:
        delivered = await asyncio.gather(
            publish_pending_clarification_invalidations(
                _app(clarification_store, turn_log_store, "gateway_a"),
            ),
            publish_pending_clarification_invalidations(
                _app(clarification_store, turn_log_store, "gateway_b"),
            ),
        )
        await sink.drain()
    finally:
        registry.set_log_sink(previous_sink)

    assert sum(delivered) == 2
    events = await turn_log_store.list_events(turn_id)
    assert [event.event_type for event in events] == [
        "clarification.dismissed",
        "clarification.dismissed",
    ]
    assert [event.seq for event in events] == [1, 2]
    assert [event.payload["evt_seq"] for event in events] == [1, 2]
    assert {event.payload["request_id"] for event in events} == {request.request_id for request in requests}
    assert await clarification_store.count_pending_invalidations() == 0


@pytest.mark.asyncio
async def test_publish_failure_leaves_intent_for_another_gateway(tmp_path: Path) -> None:
    db_path, clarification_store, turn_log_store = await _stores(tmp_path)
    turn_id = f"t_recover_{uuid4().hex}"
    request = await _pending(clarification_store, turn_id)
    await clarification_store.invalidate_one(
        request.request_id,
        target_status="dismissed",
        reason="user_rephrase",
        actor="user",
    )

    registry = get_event_registry()
    previous_sink = registry._default_log_sink
    registry.set_log_sink(None)
    try:
        assert (
            await publish_pending_clarification_invalidations(
                _app(clarification_store, turn_log_store, "gateway_crashed"),
            )
            == 0
        )
        assert await clarification_store.count_pending_invalidations() == 1
        with sqlite3.connect(db_path) as connection:
            connection.execute("UPDATE clarification_invalidation_outbox SET lease_expires_at='1970-01-01T00:00:00+00:00'")

        sink = OrderedTurnLogSink(turn_log_store)
        registry.set_log_sink(sink)
        assert (
            await publish_pending_clarification_invalidations(
                _app(clarification_store, turn_log_store, "gateway_recovery"),
            )
            == 1
        )
        await sink.drain()
    finally:
        registry.set_log_sink(previous_sink)

    events = await turn_log_store.list_events(turn_id)
    assert len(events) == 1
    assert events[0].id.endswith(request.request_id)
    assert await clarification_store.count_pending_invalidations() == 0


@pytest.mark.asyncio
async def test_retry_after_event_persisted_before_outbox_settlement_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, clarification_store, turn_log_store = await _stores(tmp_path)
    turn_id = f"t_settle_recover_{uuid4().hex}"
    request = await _pending(clarification_store, turn_id)
    await clarification_store.invalidate_one(
        request.request_id,
        target_status="dismissed",
        reason="user_rephrase",
        actor="user",
    )

    registry = get_event_registry()
    previous_sink = registry._default_log_sink
    sink = OrderedTurnLogSink(turn_log_store)
    registry.set_log_sink(sink)
    original_settle = clarification_store.mark_invalidation_delivered

    async def _lose_settlement(*, request_id: str, owner: str) -> bool:
        return False

    try:
        monkeypatch.setattr(
            clarification_store,
            "mark_invalidation_delivered",
            _lose_settlement,
        )
        assert (
            await publish_pending_clarification_invalidations(
                _app(clarification_store, turn_log_store, "gateway_before_crash"),
            )
            == 0
        )
        assert len(await turn_log_store.list_events(turn_id)) == 1
        assert await clarification_store.count_pending_invalidations() == 1

        monkeypatch.setattr(
            clarification_store,
            "mark_invalidation_delivered",
            original_settle,
        )
        with sqlite3.connect(db_path) as connection:
            connection.execute("UPDATE clarification_invalidation_outbox SET lease_expires_at='1970-01-01T00:00:00+00:00'")
        assert (
            await publish_pending_clarification_invalidations(
                _app(clarification_store, turn_log_store, "gateway_after_crash"),
            )
            == 1
        )
        await sink.drain()
    finally:
        registry.set_log_sink(previous_sink)

    events = await turn_log_store.list_events(turn_id)
    assert [(event.id, event.event_type) for event in events] == [
        (
            f"evt_clarification_invalidation_{request.request_id}",
            "clarification.dismissed",
        ),
    ]
    assert await clarification_store.count_pending_invalidations() == 0


@pytest.mark.asyncio
async def test_readiness_requires_complete_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    store = ClarificationStore(db_path)
    await store.initialize()
    assert await store.invalidation_schema_ready() is False
    _install_invalidation_schema(db_path)
    assert await store.invalidation_schema_ready() is True


def test_shared_database_gate_normalizes_equivalent_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    absolute = tmp_path / "core.db"
    stores = (
        SimpleNamespace(db_path="core.db"),
        SimpleNamespace(_db_path=str(absolute)),
        SimpleNamespace(db_path=absolute),
        SimpleNamespace(_db_path=Path("core.db")),
    )
    assert require_shared_core_database(*stores) == absolute.resolve()

    with pytest.raises(RuntimeError, match="must share"):
        require_shared_core_database(
            *stores[:-1],
            SimpleNamespace(db_path=tmp_path / "other.db"),
        )


@pytest.mark.asyncio
async def test_readiness_requires_ordered_durable_sink(tmp_path: Path) -> None:
    _db_path, _clarification_store, turn_log_store = await _stores(tmp_path)
    registry = get_event_registry()
    previous_sink = registry._default_log_sink
    try:
        registry.set_log_sink(None)
        with pytest.raises(RuntimeError, match="ordered durable"):
            require_durable_event_sink()
        registry.set_log_sink(OrderedTurnLogSink(turn_log_store))
        require_durable_event_sink()
    finally:
        registry.set_log_sink(previous_sink)
