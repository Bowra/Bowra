from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core._db import open_shared_ctx
from bowhead_core.events import TerminalDurabilityAbort
from bowhead_core.migrations.baseline_schema import initial_schema
from bowhead_core.session_store import SessionStore
from bowhead_gateway.routes import chat as chat_routes
from bowhead_gateway.routes.chat import _persist_turn_row_for_stream
from bowhead_gateway.turn_service import (
    admit_legacy_chat_execution as _real_admit_legacy_chat_execution,
)
from bowhead_gateway.turn_service import spawn_turn as _real_spawn_turn

from bowhead_core import OrchestratorEvent, TurnRequest, get_event_registry


@pytest.fixture(autouse=True)
def _instance_encryption_root(monkeypatch):
    """Deployments always set BOWHEAD_FERNET_KEY; since 9c6fe1654 every chat worker turn builds the
    physical-usage receipt cipher from it. Without .env in the checkout nothing else provides it."""
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")


async def _accept_test_event(_event: OrchestratorEvent) -> bool:
    return True


async def _steer_buffer_is_empty(store: SessionStore, session_id: str) -> bool:
    async with (
        open_shared_ctx(store.db_path) as db,
        db.execute(
            "SELECT steer_buffer_json FROM sessions WHERE id=?",
            (session_id,),
        ) as cursor,
    ):
        row = await cursor.fetchone()
    return row == ("[]",)


@pytest.fixture(autouse=True)
def _durable_test_event_sink():
    registry = get_event_registry()
    previous_sink = registry._default_log_sink
    registry.set_log_sink(_accept_test_event)
    try:
        yield
    finally:
        registry.set_log_sink(previous_sink)


async def _admit_legacy_test_worker(
    orchestrator,
    req: TurnRequest,
    *,
    dispatch_owner_id: str | None = None,
    claim_dispatch: bool = True,
):
    registry = get_event_registry()
    orchestrator.event_registry = registry
    admitted_bus = await _real_admit_legacy_chat_execution(orchestrator, req)
    owner_id = dispatch_owner_id or f"test-dispatch:{req.turn_id}"
    orchestrator.dispatch_owner_id = owner_id
    if claim_dispatch:
        assert (
            await orchestrator.session_store.claim_chat_spawn_dispatch_for_worker(
                turn_id=req.turn_id,
                owner_id=owner_id,
            )
            == "claimed"
        )
    else:
        assert (
            await orchestrator.session_store.get_chat_spawn_dispatch_projection_state(
                turn_id=req.turn_id,
            )
            == "active"
        )
    return orchestrator, admitted_bus


@pytest.mark.asyncio
async def test_persist_turn_row_for_stream_creates_turn_before_sse(tmp_path) -> None:
    path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{path}", target="core")
    store = SessionStore(path)
    await store.initialize()
    req = TurnRequest(
        session_id="s_stream",
        workspace_id="ws_sales",
        user_id="u_alice",
        member_uid="m_alice",
        text="hello",
        turn_id="t_stream",
    )

    await _persist_turn_row_for_stream(SimpleNamespace(session_store=store), req)

    turn = await store.get_turn("t_stream")
    assert turn is not None
    assert turn.session_id == "s_stream"
    assert turn.user_text == "hello"
    source = await store.get_chat_turn_user_source(turn_id="t_stream", user_id="u_alice", workspace_id="ws_sales")
    assert source is not None and source.user_text == "hello"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entitlement_health", "team_router", "credit_balance", "expected_error_kind"),
    [
        ("over_grace", object(), None, "steer_entitlement_unavailable"),
        ("active", None, None, "steer_team_model_runtime_unavailable"),
        # Parent debited balance to ≤0: reject child resumption, or steering during execution permits endless chained turns.
        ("active", object(), -50, "user_credit_exhausted"),
    ],
)
async def test_steer_followup_halt_recovers_durable_child(
    tmp_path,
    monkeypatch,
    entitlement_health,
    team_router,
    credit_balance,
    expected_error_kind,
) -> None:
    db_path = tmp_path / "bowhead.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    store = SessionStore(db_path)
    await store.initialize()
    await store.ensure_session(session_id="s1", workspace_id="ws_sales", user_id="u_alice")
    await store.record_turn(turn_id="t_parent", session_id="s1", user_text="parent")
    await store.append_steer_buffer(session_id="s1", text="follow up")

    class _Orchestrator:
        session_store = store

        async def run_turn(self, req):
            assert await store.mark_completed(
                turn_id=req.turn_id,
                intent=None,
                final_answer="",
                tokens_input=0,
                tokens_output=0,
                cost_usd=0.0,
            )

    class _Entitlements:
        async def get(self, team_id):
            return SimpleNamespace(health=entitlement_health)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(chat_routes.asyncio, "sleep", _no_sleep)
    req = TurnRequest(
        session_id="s1",
        workspace_id="ws_sales",
        user_id="u_alice",
        member_uid="m_alice",
        text="parent",
        turn_id="t_parent",
        team_id="team_a",
        team_router=team_router,
    )

    class _CreditStore:
        def __init__(self, balance: int) -> None:
            self.balance = balance
            self.events: list[dict] = []

        async def get_user(self, user_id):
            return SimpleNamespace(id=user_id, credit_balance=self.balance, credit_quota=1_000)

        async def get_team(self, team_id):
            return SimpleNamespace(id=team_id, team_credit_balance=100_000, team_credit_quota=100_000)

        async def record_credit_exhaustion_event(self, **kwargs):
            self.events.append(kwargs)
            return {"id": "cex_test", **kwargs}

    credit_store = _CreditStore(credit_balance) if credit_balance is not None else None
    registry = get_event_registry()
    orchestrator, admitted_bus = await _admit_legacy_test_worker(_Orchestrator(), req)
    await chat_routes._run_turn_and_cleanup(
        orchestrator,
        req,
        user_store=credit_store,
        team_id="team_a",
        entitlements=_Entitlements(),
        admitted_legacy_bus=admitted_bus,
        cleanup_grace_s=0,
    )

    if credit_store is not None:
        assert [event["code"] for event in credit_store.events] == ["user_credit_exhausted"]
    assert await store.get_active_turn("s1") is None
    assert await _steer_buffer_is_empty(store, "s1")
    async with (
        open_shared_ctx(store.db_path) as db,
        db.execute(
            "SELECT t.status,t.error_kind,u.user_text,d.state "
            "FROM turns t JOIN chat_turn_user_sources u ON u.turn_id=t.id "
            "JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id "
            "WHERE t.session_id=? AND t.id!='t_parent'",
            ("s1",),
        ) as cur,
    ):
        row = await cur.fetchone()
    assert row == (
        "failed",
        expected_error_kind,
        "follow up",
        "failed",
    )
    # FE banners use persisted turn_failed.reason_code: credit codes unchanged, others remain generic failures.
    async with (
        open_shared_ctx(store.db_path) as db,
        db.execute(
            "SELECT e.payload_json FROM turn_events e JOIN turns t ON t.id=e.turn_id "
            "WHERE t.session_id=? AND t.id!='t_parent' AND e.event_type='turn_failed'",
            ("s1",),
        ) as cur,
    ):
        failed_payloads = [json.loads(r[0]) for r in await cur.fetchall()]
    expected_reason_code = expected_error_kind if expected_error_kind == "user_credit_exhausted" else "lead_internal_error"
    assert [p["reason_code"] for p in failed_payloads] == [expected_reason_code]
    parent_bus, _floor = await registry.projection_checkpoint(req.turn_id)
    assert parent_bus is None


@pytest.mark.asyncio
async def test_steer_admission_failure_lost_cas_publishes_no_false_terminal(
    tmp_path,
    monkeypatch,
) -> None:
    """A concurrent child cancellation wins over the admission failure writer."""
    db_path = tmp_path / "bowhead.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    store = SessionStore(db_path)
    await store.initialize()
    await store.ensure_session(session_id="s1", workspace_id="ws_sales", user_id="u_alice")
    await store.record_turn(turn_id="t_parent", session_id="s1", user_text="parent")
    await store.append_steer_buffer(session_id="s1", text="follow up")

    original_mark_cancelled = store.mark_cancelled

    async def concurrent_cancel_wins(*, turn_id: str, **_kwargs) -> bool:
        assert await original_mark_cancelled(
            turn_id=turn_id,
            error_kind="user_cancel",
        )
        return False

    monkeypatch.setattr(store, "mark_failed", concurrent_cancel_wins)

    class _Orchestrator:
        session_store = store

        async def run_turn(self, req):
            assert await store.mark_completed(
                turn_id=req.turn_id,
                intent=None,
                final_answer="",
                tokens_input=0,
                tokens_output=0,
                cost_usd=0.0,
            )

    class _Entitlements:
        async def get(self, team_id):
            return SimpleNamespace(health="active")

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(chat_routes.asyncio, "sleep", _no_sleep)
    req = TurnRequest(
        session_id="s1",
        workspace_id="ws_sales",
        user_id="u_alice",
        member_uid="m_alice",
        text="parent",
        turn_id="t_parent",
        team_id="team_a",
        team_router=None,
    )

    orchestrator, admitted_bus = await _admit_legacy_test_worker(_Orchestrator(), req)
    await chat_routes._run_turn_and_cleanup(
        orchestrator,
        req,
        team_id="team_a",
        entitlements=_Entitlements(),
        admitted_legacy_bus=admitted_bus,
        cleanup_grace_s=0,
    )

    async with (
        open_shared_ctx(store.db_path) as db,
        db.execute(
            "SELECT t.id,t.status,d.state FROM turns t JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id WHERE t.session_id=? AND t.id!='t_parent'",
            ("s1",),
        ) as cur,
    ):
        child_id, child_status, dispatch_state = await cur.fetchone()
    assert child_status == "cancelled"
    assert dispatch_state != "failed"
    child_bus, _floor = await get_event_registry().projection_checkpoint(child_id)
    assert child_bus is None


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent_log_failure", [False, True])
@pytest.mark.parametrize("child_spawn_close_fault", [False, True])
async def test_live_steer_follow_up_precedes_parent_done_and_is_persisted(
    tmp_path,
    monkeypatch,
    persistent_log_failure,
    child_spawn_close_fault,
) -> None:
    db_path = tmp_path / "bowhead.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    store = SessionStore(db_path)
    await store.initialize()
    await store.ensure_session(
        session_id="s_live_follow_up",
        workspace_id="ws_sales",
        user_id="u_alice",
    )
    await store.record_turn(
        turn_id="t_live_parent",
        session_id="s_live_follow_up",
        user_text="parent",
    )
    await store.append_steer_buffer(
        session_id="s_live_follow_up",
        text="follow live",
    )
    registry = get_event_registry()
    bus = await registry.get_or_create("t_live_parent", producer=False)
    persisted: list[str] = []
    follow_up_attempts = 0

    async def persist_event(event: OrchestratorEvent) -> bool:
        nonlocal follow_up_attempts
        if event.event_type == "turn.follow_up":
            follow_up_attempts += 1
            if persistent_log_failure:
                return False
        persisted.append(event.event_type)
        return True

    bus._log_sink = persist_event

    class _Orchestrator:
        session_store = store

        async def run_turn(self, req):
            assert await store.mark_completed(
                turn_id=req.turn_id,
                intent=None,
                final_answer="",
                tokens_input=0,
                tokens_output=0,
                cost_usd=0.0,
            )
            await bus.emit(
                OrchestratorEvent(
                    event_type="done",
                    turn_id=req.turn_id,
                    payload={"turn_id": req.turn_id, "status": "completed"},
                )
            )
            return

    spawned: list[TurnRequest] = []
    spawn_error = RuntimeError("child spawn unavailable")

    async def capture_spawn(_orchestrator, req, **_kwargs):
        spawned.append(req)
        if child_spawn_close_fault:
            raise spawn_error

    async def no_sleep(_seconds):
        return None

    import bowhead_gateway.turn_service as turn_service

    monkeypatch.setattr(turn_service, "spawn_turn", capture_spawn)
    monkeypatch.setattr(chat_routes.asyncio, "sleep", no_sleep)
    req = TurnRequest(
        session_id="s_live_follow_up",
        workspace_id="ws_sales",
        user_id="u_alice",
        member_uid="m_alice",
        text="parent",
        turn_id="t_live_parent",
    )
    orchestrator, admitted_bus = await _admit_legacy_test_worker(_Orchestrator(), req)
    if persistent_log_failure:
        with pytest.raises(TerminalDurabilityAbort):
            await chat_routes._run_turn_and_cleanup(
                orchestrator,
                req,
                admitted_legacy_bus=admitted_bus,
                cleanup_grace_s=0,
            )
    else:
        await chat_routes._run_turn_and_cleanup(
            orchestrator,
            req,
            admitted_legacy_bus=admitted_bus,
            cleanup_grace_s=0,
        )

    if persistent_log_failure:
        # A rejected durable write fails closed before child execution. The
        # already-materialized relation remains recovery work.
        assert bus.snapshot() == []
        assert persisted == []
        assert follow_up_attempts == 1
        assert await store.get_chat_follow_up(parent_turn_id="t_live_parent") is not None
    else:
        assert [event.event_type for event in bus.snapshot()] == [
            "turn.follow_up",
            "done",
        ]
        assert persisted == ["turn.follow_up", "done"]
        assert follow_up_attempts == 1
    # This test invokes the worker directly. Production ``spawn_turn`` owns
    # the one lease monitor that drains and terminalizes this active row.
    assert (
        await store.get_chat_spawn_dispatch_projection_state(
            turn_id="t_live_parent",
        )
        == "active"
    )
    if persistent_log_failure:
        assert spawned == []
    else:
        assert len(spawned) == 1
        assert spawned[0].text == "follow live"
        assert (
            await store.get_chat_spawn_dispatch_projection_state(
                turn_id=spawned[0].turn_id,
            )
            == "active"
        )
    # This test deliberately bypasses production ``spawn_turn``; retire its
    # direct worker fixture so parameterized cases cannot share a producer.
    await registry.close(req.turn_id, expected=admitted_bus)


@pytest.mark.asyncio
async def test_legacy_worker_rejects_live_event_when_persistence_rejects(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "bowhead.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    store = SessionStore(db_path)
    await store.initialize()
    await store.ensure_session(
        session_id="s_legacy_persist",
        workspace_id="ws_sales",
        user_id="u_alice",
    )
    await store.record_turn(
        turn_id="t_legacy_persist",
        session_id="s_legacy_persist",
        user_text="legacy",
    )
    registry = get_event_registry()
    bus = await registry.get_or_create(
        "t_legacy_persist",
        producer=False,
    )

    async def reject_event(_event: OrchestratorEvent) -> bool:
        return False

    bus._log_sink = reject_event

    class _Orchestrator:
        session_store = store

        async def run_turn(self, req):
            assert not await bus.emit(
                OrchestratorEvent(
                    event_type="partial_answer",
                    turn_id=req.turn_id,
                    payload={"text": "must stay durable-first"},
                )
            )
            return

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(chat_routes.asyncio, "sleep", no_sleep)
    req = TurnRequest(
        session_id="s_legacy_persist",
        workspace_id="ws_sales",
        user_id="u_alice",
        member_uid="m_alice",
        text="legacy",
        turn_id="t_legacy_persist",
    )
    orchestrator, admitted_bus = await _admit_legacy_test_worker(_Orchestrator(), req)
    with pytest.raises(TerminalDurabilityAbort):
        await chat_routes._run_turn_and_cleanup(
            orchestrator,
            req,
            admitted_legacy_bus=admitted_bus,
            cleanup_grace_s=0,
        )

    assert bus.snapshot() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("marker_outage", [False, True])
async def test_legacy_materialize_failure_creates_durable_recovery_marker(
    tmp_path,
    monkeypatch,
    marker_outage,
) -> None:
    db_path = tmp_path / "bowhead.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    store = SessionStore(db_path)
    await store.initialize()
    await store.ensure_session(
        session_id="s_legacy_drain_pending",
        workspace_id="ws_sales",
        user_id="u_alice",
    )
    await store.record_turn(
        turn_id="t_legacy_drain_pending",
        session_id="s_legacy_drain_pending",
        user_text="legacy parent",
    )
    await store.append_steer_buffer(
        session_id="s_legacy_drain_pending",
        text="accepted legacy steer",
    )
    dispatch_owner_id = "test-dispatch:t_legacy_drain_pending"
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id="t_legacy_drain_pending",
            owner_id=dispatch_owner_id,
        )
        == "claimed"
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turns SET status='completed',ended_at=? WHERE id=?",
            ("2026-08-03T00:00:00+00:00", "t_legacy_drain_pending"),
        )
        connection.commit()
    registry = get_event_registry()
    bus = await registry.get_or_create(
        "t_legacy_drain_pending",
        producer=False,
    )

    async def fail_materialize(**_kwargs):
        raise sqlite3.OperationalError("materialize unavailable")

    monkeypatch.setattr(store, "materialize_steer_follow_up", fail_materialize)
    marker_calls = 0
    if marker_outage:

        async def fail_marker(**_kwargs):
            nonlocal marker_calls
            marker_calls += 1
            raise sqlite3.OperationalError("marker unavailable")

        monkeypatch.setattr(
            store,
            "mark_chat_spawn_dispatch_drain_pending",
            fail_marker,
        )

    class _Orchestrator:
        session_store = store

        async def run_turn(self, req):
            await bus.emit(
                OrchestratorEvent(
                    event_type="done",
                    turn_id=req.turn_id,
                    payload={"turn_id": req.turn_id, "status": "completed"},
                )
            )
            return

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(chat_routes.asyncio, "sleep", no_sleep)
    req = TurnRequest(
        session_id="s_legacy_drain_pending",
        workspace_id="ws_sales",
        user_id="u_alice",
        member_uid="m_alice",
        text="legacy parent",
        turn_id="t_legacy_drain_pending",
    )
    orchestrator = _Orchestrator()
    orchestrator.event_registry = registry
    orchestrator.dispatch_owner_id = dispatch_owner_id
    admitted_bus = await _real_admit_legacy_chat_execution(orchestrator, req)
    await chat_routes._run_turn_and_cleanup(
        orchestrator,
        req,
        admitted_legacy_bus=admitted_bus,
        cleanup_grace_s=0,
    )

    assert bus.snapshot() == []
    assert (
        await store.get_chat_spawn_dispatch_projection_state(
            turn_id="t_legacy_drain_pending",
        )
        == "active"
    )
    with sqlite3.connect(db_path) as connection:
        marker_row = connection.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            ("t_legacy_drain_pending",),
        ).fetchone()
    if marker_outage:
        assert marker_calls == 2
        assert marker_row == ("spawned",)
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
                ("2000-01-01T00:00:00+00:00", "t_legacy_drain_pending"),
            )
            connection.commit()
    else:
        assert marker_row == ("drain_pending",)
    assert await store.recover_expired_chat_spawn_dispatches() == [
        "t_legacy_drain_pending",
    ]
    assert (
        await store.get_chat_follow_up(
            parent_turn_id="t_legacy_drain_pending",
        )
        is not None
    )
    assert await _steer_buffer_is_empty(store, "s_legacy_drain_pending")


@pytest.mark.asyncio
async def test_cancelled_legacy_finalizer_retires_bus_for_recovery(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "bowhead.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    store = SessionStore(db_path)
    await store.initialize()
    await store.ensure_session(
        session_id="s_cancelled_legacy_barrier",
        workspace_id="ws_sales",
        user_id="u_alice",
    )
    await store.record_turn(
        turn_id="t_cancelled_legacy_barrier",
        session_id="s_cancelled_legacy_barrier",
        user_text="legacy parent",
    )
    await store.append_steer_buffer(
        session_id="s_cancelled_legacy_barrier",
        text="accepted follow-up",
    )
    dispatch_owner_id = "test-dispatch:t_cancelled_legacy_barrier"
    registry = get_event_registry()
    bus = await registry.get_or_create(
        "t_cancelled_legacy_barrier",
        producer=False,
    )

    async def persist_event(event: OrchestratorEvent) -> bool:
        return True

    bus._log_sink = persist_event

    class _Orchestrator:
        session_store = store

        async def run_turn(self, req):
            assert await store.mark_completed(
                turn_id=req.turn_id,
                intent="lookup",
                final_answer="",
                tokens_input=0,
                tokens_output=0,
                cost_usd=0.0,
            )
            await bus.emit(
                OrchestratorEvent(
                    event_type="done",
                    turn_id=req.turn_id,
                    payload={"turn_id": req.turn_id, "status": "completed"},
                )
            )
            return

    spawn_started = asyncio.Event()
    keep_spawn_blocked = asyncio.Event()

    async def blocked_spawn(_orchestrator, _req, **_kwargs):
        spawn_started.set()
        await keep_spawn_blocked.wait()

    import bowhead_gateway.turn_service as turn_service

    monkeypatch.setattr(turn_service, "spawn_turn", blocked_spawn)
    req = TurnRequest(
        session_id="s_cancelled_legacy_barrier",
        workspace_id="ws_sales",
        user_id="u_alice",
        member_uid="m_alice",
        text="legacy parent",
        turn_id="t_cancelled_legacy_barrier",
    )
    orchestrator = _Orchestrator()
    orchestrator.event_registry = registry
    orchestrator.dispatch_owner_id = dispatch_owner_id
    admitted_bus = await _real_admit_legacy_chat_execution(orchestrator, req)
    worker = await _real_spawn_turn(
        orchestrator,
        req,
        admitted_legacy_bus=admitted_bus,
        cleanup_grace_s=0,
    )
    await spawn_started.wait()
    assert (
        await store.get_chat_spawn_dispatch_projection_state(
            turn_id="t_cancelled_legacy_barrier",
        )
        == "active"
    )

    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    producer_bus = bus
    for _attempt in range(200):
        await asyncio.sleep(0.01)
        producer_bus, _floor = await registry.projection_checkpoint(
            "t_cancelled_legacy_barrier",
        )
        if producer_bus is None:
            break
    assert producer_bus is None
    assert [event.event_type for event in bus.snapshot()] == [
        "turn.follow_up",
        "done",
    ]
    assert (
        await store.get_chat_spawn_dispatch_projection_state(
            turn_id="t_cancelled_legacy_barrier",
        )
        == "active"
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
            ("2000-01-01T00:00:00+00:00", "t_cancelled_legacy_barrier"),
        )
        connection.commit()
    assert await store.recover_expired_chat_spawn_dispatches() == [
        "t_cancelled_legacy_barrier",
    ]
    assert (
        await store.get_chat_spawn_dispatch_projection_state(
            turn_id="t_cancelled_legacy_barrier",
        )
        == "settled"
    )


@pytest.mark.asyncio
async def test_cancelled_marker_outage_retires_worker_bus_without_barrier(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "bowhead.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    store = SessionStore(db_path)
    await store.initialize()
    await store.ensure_session(
        session_id="s_marker_cancel",
        workspace_id="ws_sales",
        user_id="u_alice",
    )
    await store.record_turn(
        turn_id="t_marker_cancel",
        session_id="s_marker_cancel",
        user_text="legacy parent",
    )
    await store.append_steer_buffer(
        session_id="s_marker_cancel",
        text="accepted follow-up",
    )
    dispatch_owner_id = "test-dispatch:t_marker_cancel"
    registry = get_event_registry()
    bus = await registry.get_or_create("t_marker_cancel", producer=False)

    async def fail_materialize(**_kwargs):
        raise sqlite3.OperationalError("materialize unavailable")

    second_marker_started = asyncio.Event()
    keep_second_marker_blocked = asyncio.Event()
    marker_calls = 0

    async def fail_then_block_marker(**_kwargs):
        nonlocal marker_calls
        marker_calls += 1
        if marker_calls == 1:
            raise sqlite3.OperationalError("marker unavailable")
        second_marker_started.set()
        await keep_second_marker_blocked.wait()
        return False

    monkeypatch.setattr(store, "materialize_steer_follow_up", fail_materialize)
    monkeypatch.setattr(
        store,
        "mark_chat_spawn_dispatch_drain_pending",
        fail_then_block_marker,
    )

    class _Orchestrator:
        session_store = store

        async def run_turn(self, req):
            assert await store.mark_completed(
                turn_id=req.turn_id,
                intent="lookup",
                final_answer="",
                tokens_input=0,
                tokens_output=0,
                cost_usd=0.0,
            )
            await bus.emit(
                OrchestratorEvent(
                    event_type="done",
                    turn_id=req.turn_id,
                    payload={"turn_id": req.turn_id, "status": "completed"},
                )
            )

    req = TurnRequest(
        session_id="s_marker_cancel",
        workspace_id="ws_sales",
        user_id="u_alice",
        member_uid="m_alice",
        text="legacy parent",
        turn_id="t_marker_cancel",
    )
    orchestrator = _Orchestrator()
    orchestrator.event_registry = registry
    orchestrator.dispatch_owner_id = dispatch_owner_id
    admitted_bus = await _real_admit_legacy_chat_execution(orchestrator, req)
    worker = await _real_spawn_turn(
        orchestrator,
        req,
        admitted_legacy_bus=admitted_bus,
        cleanup_grace_s=0,
    )
    await second_marker_started.wait()
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker

    producer_bus = bus
    for _attempt in range(200):
        await asyncio.sleep(0.01)
        producer_bus, _floor = await registry.projection_checkpoint(
            "t_marker_cancel",
        )
        if producer_bus is None:
            break
    assert marker_calls == 2
    assert producer_bus is None
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
            ("2000-01-01T00:00:00+00:00", "t_marker_cancel"),
        )
        connection.commit()
    assert await store.recover_expired_chat_spawn_dispatches() == [
        "t_marker_cancel",
    ]
    assert (
        await store.get_chat_follow_up(
            parent_turn_id="t_marker_cancel",
        )
        is not None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_steer_drain_failure_becomes_durable_recovery_work(
    tmp_path,
    monkeypatch,
    cancelled,
) -> None:
    db_path = tmp_path / "bowhead.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    store = SessionStore(db_path)
    await store.initialize()
    await store.ensure_session(
        session_id="s_drain_pending",
        workspace_id="ws_sales",
        user_id="u_alice",
    )
    await store.record_turn(
        turn_id="t_drain_parent",
        session_id="s_drain_pending",
        user_text="parent",
    )
    await store.append_steer_buffer(
        session_id="s_drain_pending",
        text="never lose me",
    )
    now = "2026-08-03T00:00:00+00:00"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO chat_turn_spawn_dispatch("
            "turn_id,session_id,source_submission_ids_json,dispatch_kind,state,"
            "lease_kind,lease_execution_generation,owner_id,lease_expires_at,"
            "created_at,updated_at"
            ") VALUES (?,?,?,'start','spawned','turn',1,?,?,?,?)",
            (
                "t_drain_parent",
                "s_drain_pending",
                "[]",
                "owner-a",
                "2099-01-01T00:00:00+00:00",
                now,
                now,
            ),
        )
        connection.commit()
    bus = await get_event_registry().get_or_create(
        "t_drain_parent",
        producer=False,
    )
    marker_calls = 0

    async def fail_materialize_once(**_kwargs):
        if cancelled:
            raise asyncio.CancelledError("durable_user_cancel")
        raise sqlite3.OperationalError("busy once")

    monkeypatch.setattr(store, "materialize_steer_follow_up", fail_materialize_once)
    if not cancelled:

        async def fail_marker_while_storage_is_unavailable(**_kwargs):
            nonlocal marker_calls
            marker_calls += 1
            raise sqlite3.OperationalError("marker storage unavailable")

        monkeypatch.setattr(
            store,
            "mark_chat_spawn_dispatch_drain_pending",
            fail_marker_while_storage_is_unavailable,
        )

    class _Orchestrator:
        session_store = store

        async def run_turn(self, req):
            assert await store.mark_completed(
                turn_id=req.turn_id,
                intent=None,
                final_answer="",
                tokens_input=0,
                tokens_output=0,
                cost_usd=0.0,
            )
            await bus.emit(
                OrchestratorEvent(
                    event_type="done",
                    turn_id=req.turn_id,
                    payload={"turn_id": req.turn_id, "status": "completed"},
                )
            )
            return

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(chat_routes.asyncio, "sleep", no_sleep)
    req = TurnRequest(
        session_id="s_drain_pending",
        workspace_id="ws_sales",
        user_id="u_alice",
        member_uid="m_alice",
        text="parent",
        turn_id="t_drain_parent",
    )
    orchestrator, admitted_bus = await _admit_legacy_test_worker(
        _Orchestrator(),
        req,
        dispatch_owner_id="owner-a",
        claim_dispatch=False,
    )
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await chat_routes._run_turn_and_cleanup(
                orchestrator,
                req,
                admitted_legacy_bus=admitted_bus,
                cleanup_grace_s=0,
            )
    else:
        await chat_routes._run_turn_and_cleanup(
            orchestrator,
            req,
            admitted_legacy_bus=admitted_bus,
            cleanup_grace_s=0,
        )

    # Accepted steer uncertainty must never deliver the parent's terminal
    # frame. If both direct marker attempts fail, the monitor's terminal handoff
    # atomically sees the non-empty buffer and moves it to drain_pending without
    # requiring a root terminal wire proof first.
    assert bus.snapshot() == []
    if not cancelled:
        assert marker_calls == 2
        assert await store.mark_chat_spawn_dispatch_terminal(
            turn_id="t_drain_parent",
            owner_id="owner-a",
        )

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            ("t_drain_parent",),
        ).fetchone() == ("drain_pending",)
        assert connection.execute(
            "SELECT steer_buffer_json FROM sessions WHERE id=?",
            ("s_drain_pending",),
        ).fetchone() != ("[]",)

    assert await store.recover_expired_chat_spawn_dispatches() == [
        "t_drain_parent",
    ]
    follow_up = await store.get_chat_follow_up(
        parent_turn_id="t_drain_parent",
    )
    assert follow_up is not None
    assert await _steer_buffer_is_empty(store, "s_drain_pending")
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM turn_events WHERE turn_id=? AND event_type='done'",
            ("t_drain_parent",),
        ).fetchone() == (1,)
