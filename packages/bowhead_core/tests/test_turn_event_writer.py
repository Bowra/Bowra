from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.migrations.baseline_schema import initial_schema
from bowhead_core.terminal_event_writer import TurnEventWriter
from bowhead_core.terminal_generation_bus import (
    GenerationBusFaultPoint,
    GenerationEventBus,
    GenerationEventBusRegistry,
    GenerationSubscription,
    ProducerAdmissionClosedError,
)
from bowhead_core.terminal_protocol import (
    ActorScope,
    AdmissionContext,
    EventPublishMode,
    EventTicket,
    ExecutionGenerationContext,
    GenerationClaimContext,
    PhaseScope,
    RootDurability,
    TerminalAdmissionRequest,
    TerminalProtocolMalformedError,
    TerminalScope,
    canonical_digest_v1,
    canonical_json_v1,
)
from bowhead_core.terminal_protocol_store import TerminalProtocolStore, TerminalReadContext
from bowhead_core.terminal_runtime_factory import (
    terminal_auth_resolve_context_values,
)

_NOW = datetime(2026, 8, 3, 8, 9, 10, tzinfo=UTC)


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    path = tmp_path / "bowhead.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()
    upgrade(f"sqlite+aiosqlite:///{path}", target="core")
    return str(path)


def _scope(*, generation: int = 1) -> TerminalScope:
    return TerminalScope(team_id="team_1", session_id="s_1", turn_id="t_1", generation=generation)


def _ticket(*, ordinal: int, event_seq: int, generation: int = 1) -> EventTicket:
    payload = {"ordinal": ordinal}
    return EventTicket(
        scope=_scope(generation=generation),
        proposal_id=f"tep_{generation}_{ordinal}",
        event_id=f"evt_{generation}_{ordinal}",
        accept_ordinal=ordinal,
        event_seq=event_seq,
        legacy_seq=event_seq,
        event_type="step_progress",
        actor_scope=ActorScope.STEP,
        phase_scope=PhaseScope.IN_GENERATION,
        payload_json=canonical_json_v1(payload),
        payload_digest=canonical_digest_v1(payload),
        root_durability=RootDurability.COMMITTED,
        occurred_at=_NOW.isoformat(),
    )


async def _take(subscription: GenerationSubscription, count: int) -> list[EventTicket]:
    iterator = subscription.__aiter__()
    return [await anext(iterator) for _ in range(count)]


def _insert_runtime_fixture(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute("INSERT INTO teams(id,name,status,created_at) VALUES ('team_1','Team','active','t')")
        connection.execute(
            """
            INSERT INTO workspaces(id,team_id,name,status,created_at,updated_at,revision)
            VALUES ('ws_1','team_1','Space','active','t','t',1)
            """,
        )
        connection.execute(
            """
            INSERT INTO sessions(id,workspace_id,user_id,title,started_at,last_active_at,status,cost_usd,tokens_total,intents)
            VALUES ('s_1','ws_1','u_1','Session','t','t','active',0,0,'[]')
            """,
        )
        connection.execute(
            """
            INSERT INTO terminal_protocol_writer_lease(
              lease_name,owner_instance_id,epoch,admission_state,admission_epoch,
              rollout_config_generation,lease_expires_at,heartbeat_at
            ) VALUES ('v1_writer','gateway_1',7,'open',3,'rollout_1',?,?)
            """,
            (
                (_NOW + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                _NOW.isoformat(),
            ),
        )
        connection.commit()


async def _start_execution(db_path: str) -> tuple[TerminalProtocolStore, ExecutionGenerationContext]:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    vector = await store.ensure_auth_scope_epochs(
        terminal_auth_resolve_context_values(
            team_id="team_1",
            session_id="s_1",
            user_id="u_1",
            resource_ref="t_1",
        )
    )
    request = TerminalAdmissionRequest(
        context=AdmissionContext(
            team_id="team_1",
            session_id="s_1",
            request_id="req_1",
            surface="web",
            cohort="bucket=1;config=rollout_1",
            actor="u_1",
            writer_instance_id="gateway_1",
            writer_lease_epoch=7,
            writer_admission_epoch=3,
            rollout_config_generation="rollout_1",
            auth_fence_vector_digest=vector.auth_fence_vector_digest,
        ),
        turn_id="t_1",
        user_text="hello",
        execution_intent={"text": "hello"},
        presentation_locale="zh-CN",
        message_catalog_version=1,
    )
    admitted = await store.admit_or_steer_v1(request)
    assert admitted.scope == _scope()
    claimed = await store.claim_generation(
        GenerationClaimContext(
            scope=_scope(),
            expected_generation_row_version=1,
            expected_session_claim_epoch=0,
            writer_lease_epoch=7,
            owner_instance_id="gateway_1",
        )
    )
    assert claimed.context is not None
    return store, await store.mark_execution_started(claimed.context)


@pytest.mark.asyncio
async def test_ac_b1b_038_two_subscribers_each_receive_full_committed_batch() -> None:
    bus = GenerationEventBus(_scope(), claim_epoch=1, bus_epoch=1)
    left = await bus.subscribe_after(0, paused=False, subscriber_id="left")
    right = await bus.subscribe_after(0, paused=False, subscriber_id="right")
    tickets = (_ticket(ordinal=1, event_seq=1), _ticket(ordinal=2, event_seq=2))

    left_task = asyncio.create_task(_take(left, 2))
    right_task = asyncio.create_task(_take(right, 2))
    await bus.publish_batch(tickets, claim_epoch=1)
    left_events, right_events = await asyncio.gather(left_task, right_task)
    assert left_events == list(tickets)
    assert right_events == list(tickets)
    await left.aclose()
    await right.aclose()


@pytest.mark.asyncio
async def test_generation_bus_fault_never_exposes_half_appended_batch() -> None:
    def fail(point: GenerationBusFaultPoint) -> None:
        if point is GenerationBusFaultPoint.FANOUT_AFTER_PREFIX:
            raise RuntimeError("injected prefix fault")

    bus = GenerationEventBus(_scope(), claim_epoch=1, bus_epoch=1, _fault_injector=fail)
    with pytest.raises(RuntimeError, match="prefix fault"):
        await bus.publish_batch((_ticket(ordinal=1, event_seq=1), _ticket(ordinal=2, event_seq=2)), claim_epoch=1)
    assert await bus.snapshot_after(0) == ()


@pytest.mark.asyncio
async def test_ac_b1b_038_after_full_fault_keeps_full_batch_for_each_subscriber() -> None:
    def fail(point: GenerationBusFaultPoint) -> None:
        if point is GenerationBusFaultPoint.FANOUT_AFTER_FULL:
            raise RuntimeError("injected after-full fault")

    bus = GenerationEventBus(
        _scope(),
        claim_epoch=1,
        bus_epoch=1,
        _fault_injector=fail,
    )
    left = await bus.subscribe_after(0, paused=False, subscriber_id="left")
    right = await bus.subscribe_after(0, paused=False, subscriber_id="right")
    tickets = (_ticket(ordinal=1, event_seq=1), _ticket(ordinal=2, event_seq=2))
    with pytest.raises(RuntimeError, match="after-full fault"):
        await bus.publish_batch(tickets, claim_epoch=1)
    left_events, right_events = await asyncio.gather(_take(left, 2), _take(right, 2))
    assert left_events == list(tickets)
    assert right_events == list(tickets)
    await left.aclose()
    await right.aclose()


@pytest.mark.asyncio
async def test_generation_registry_isolates_generations_and_fences_stale_owner() -> None:
    registry = GenerationEventBusRegistry()
    old = await registry.get_or_create(_scope(generation=1), claim_epoch=1)
    subscription = await old.subscribe_after(0, paused=True)
    current = await registry.get_or_create(_scope(generation=2), claim_epoch=2)
    assert old is not current
    assert old.bus_epoch != current.bus_epoch
    with pytest.raises(TerminalProtocolMalformedError, match="stale owner"):
        await old.close(claim_epoch=2)

    await registry.retire(_scope(generation=1), claim_epoch=1)
    assert registry.size == 2
    await subscription.aclose()
    assert await registry.collect_retired() == 1
    assert registry.size == 1


@pytest.mark.asyncio
async def test_generation_producer_admission_closes_without_cancelling_registered_task() -> None:
    bus = GenerationEventBus(_scope(), claim_epoch=1, bus_epoch=1)
    lease = await bus.register_producer(claim_epoch=1)
    tasks = await bus.close_producer_admission(claim_epoch=1)
    assert asyncio.current_task() in tasks
    with pytest.raises(ProducerAdmissionClosedError):
        await bus.register_producer(claim_epoch=1)
    await lease.release()


@pytest.mark.asyncio
async def test_turn_event_writer_resolves_ticket_before_live_publish(db_path: str) -> None:
    _insert_runtime_fixture(db_path)
    store, context = await _start_execution(db_path)
    bus = GenerationEventBus(_scope(), claim_epoch=1, bus_epoch=1)
    writer = TurnEventWriter(store, bus, context, batch_delay_seconds=60)
    handle = await writer.accept_event(
        event_type="step_started",
        actor_scope=ActorScope.STEP,
        phase_scope=PhaseScope.IN_GENERATION,
        payload={"step_id": "step_1"},
    )
    tickets = await writer.flush_through(1)
    assert await handle.persisted() == tickets[0]
    assert await handle.persisted_with_prefix() == (tickets[0], tickets)
    assert await bus.snapshot_after(0) == tickets
    await writer.close()


@pytest.mark.asyncio
async def test_live_fanout_failure_disconnects_but_does_not_poison_durable_writer(db_path: str) -> None:
    _insert_runtime_fixture(db_path)
    store, context = await _start_execution(db_path)

    def fail(point: GenerationBusFaultPoint) -> None:
        if point is GenerationBusFaultPoint.FANOUT_BEFORE_APPEND:
            raise RuntimeError("injected fanout failure")

    bus = GenerationEventBus(
        _scope(),
        claim_epoch=1,
        bus_epoch=1,
        _fault_injector=fail,
    )
    subscription = await bus.subscribe_after(0, paused=False)
    writer = TurnEventWriter(store, bus, context, batch_delay_seconds=60)
    first = await writer.accept_event(
        event_type="step_started",
        actor_scope=ActorScope.STEP,
        phase_scope=PhaseScope.IN_GENERATION,
        payload={"step_id": "step_1"},
    )
    first_tickets = await writer.flush_through(1)
    assert await first.persisted() == first_tickets[0]
    assert isinstance(writer.fanout_error, RuntimeError)
    assert bus.fanout_failed
    with pytest.raises(StopAsyncIteration):
        await anext(subscription.__aiter__())

    second = await writer.accept_event(
        event_type="step_done",
        actor_scope=ActorScope.STEP,
        phase_scope=PhaseScope.IN_GENERATION,
        payload={"step_id": "step_1"},
    )
    await writer.flush_through(2)
    assert (await second.persisted()).event_seq == 2
    assert [
        event.event_id
        for event in await store.list_protocol_events(
            TerminalReadContext(
                team_id="team_1",
                session_id="s_1",
                turn_id="t_1",
                viewer="u_1",
                auth_fence_vector_digest=(
                    await store.read_auth_fence_vector(
                        terminal_auth_resolve_context_values(
                            team_id="team_1",
                            session_id="s_1",
                            user_id="u_1",
                            resource_ref="t_1",
                        )
                    )
                ).auth_fence_vector_digest,
            )
        )
    ] == [first.event_id, second.event_id]
    await writer.close()


@pytest.mark.asyncio
async def test_before_live_mode_triggers_immediate_background_flush(db_path: str) -> None:
    _insert_runtime_fixture(db_path)
    store, context = await _start_execution(db_path)
    bus = GenerationEventBus(_scope(), claim_epoch=1, bus_epoch=1)
    writer = TurnEventWriter(store, bus, context, batch_delay_seconds=60)
    handle = await writer.accept_event(
        event_type="final_answer",
        actor_scope=ActorScope.ROOT,
        phase_scope=PhaseScope.IN_GENERATION,
        payload={"text": "answer"},
        publish_mode=EventPublishMode.BEFORE_LIVE,
    )
    ticket = await asyncio.wait_for(handle.persisted(), timeout=1)
    assert await bus.snapshot_after(0) == (ticket,)
    await writer.close()
