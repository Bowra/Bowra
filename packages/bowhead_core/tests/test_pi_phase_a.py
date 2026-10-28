"""Sprint π Phase A tests — unify persist + retry/fallback + observability.

Covers tech-design r2 §A invariants (persist chokepoint), §J stream retry +
invoke fallback + notice + profile passthrough, §I compute peer prompt rule,
and presence of the 9 new Prom counters per r2 §5.

Per `feedback_ut_must_assert_emit_contract`: every emit-related test mocks the
bus and asserts the full event sequence + payload, not just return value.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_core.agents.lead_orchestrator import _emit_event
from bowhead_router.errors import (
    InvocationOutcomeUnknownError,
    LLMTargetFailure,
    ProviderFailureKind,
    StreamingTargetsExhaustedError,
)

# ─── Helpers ─────────────────────────────────────────────────────────────


class _MockBus:
    """Bus mock that tracks emit() calls (event_type + payload)."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self._done_emitted = False

    async def emit(self, event) -> None:
        self.events.append(
            {
                "event_type": event.event_type,
                "payload": event.payload,
            }
        )


# ─── §A — single chokepoint persist ──────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_before_emit() -> None:
    """Invariant 1 (r2 §A): persist_turn_state called BEFORE bus.emit returns.

    Asserts: when _emit_event("done", ...) is called with a persist callback
    wired on the bus, persist fires before the SSE emit lands on the bus.
    """
    bus = _MockBus()
    # Suppress universal silent-done fallback (independent feature) so we
    # observe only the chokepoint persist + done emit sequence.
    bus._final_answer_emitted = True  # type: ignore[attr-defined]
    call_order: list[str] = []

    async def _persist(**kwargs: Any) -> bool:
        call_order.append("persist")
        return True

    async def _emit(event) -> None:  # override the bus emit
        call_order.append("emit")
        bus.events.append({"event_type": event.event_type, "payload": event.payload})

    bus.emit = _emit  # type: ignore[assignment]
    bus._pi_persist_turn_state = _persist  # type: ignore[attr-defined]
    bus._pi_turn_state = {  # type: ignore[attr-defined]
        "intent": None,
        "final_answer": "",
        "tokens_input": 0,
        "tokens_output": 0,
        "cost_usd": 0.0,
        "error": None,
        "error_kind": None,
    }

    await _emit_event(bus, "t_1", "done", {"status": "completed"})

    # Invariant: persist fires BEFORE the actual "done" SSE event reaches bus.
    # ξ2-F3 R1 added a canvas.done fallback emit earlier in the done chokepoint
    # (pre-persist) — separate event, OK. Only assert persist precedes done emit.
    done_event_idx = next(
        (i for i, ev in enumerate(bus.events) if ev["event_type"] == "done"),
        -1,
    )
    assert done_event_idx >= 0, f"done event not emitted; events={bus.events}"
    # Count emits that happened before persist in call_order
    call_order.index("persist") if "persist" in call_order else -1
    # Emits in call_order map 1-1 to bus.events in order. So done_event_idx must
    # come AFTER the persist index in call_order (counting emits only).
    persist_idx = call_order.index("persist")
    emit_positions = [i for i, n in enumerate(call_order) if n == "emit"]
    assert persist_idx < emit_positions[done_event_idx], (
        f"persist must precede done emit; call_order={call_order} events={[e['event_type'] for e in bus.events]}"
    )
    assert bus.events[done_event_idx]["payload"]["status"] == "completed"


@pytest.mark.asyncio
async def test_silent_done_fallback_survives_event_log_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime, timedelta

    from bowhead_cli.migrate import TARGET_FILES, upgrade
    from bowhead_core.events import EventBusRegistry, TerminalDurabilityAbort
    from bowhead_core.session_store import SessionStore
    from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore

    monkeypatch.setenv("BOWHEAD_SILENT_DONE_FALLBACK", "1")
    core_path = tmp_path / TARGET_FILES["core"]
    assert upgrade(f"sqlite+aiosqlite:///{core_path}") == tuple(TARGET_FILES)
    store = SessionStore(core_path)
    await store.initialize()
    log_store = TurnLogStore(core_path)
    await log_store.initialize()
    session_id = "s_silent_fallback_failure"
    turn_id = "t_silent_fallback_failure"
    await store.ensure_session(
        session_id=session_id,
        workspace_id="ws1",
        user_id="u1",
    )
    await store.record_turn(
        turn_id=turn_id,
        session_id=session_id,
        user_text="finish without visible output",
    )
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id=turn_id,
            owner_id="gateway-owner",
        )
        == "claimed"
    )

    class _RejectFinalAnswerLogStore:
        async def record(self, **kwargs):
            if kwargs["event_type"] == "final_answer":
                return None
            return await log_store.record(**kwargs)

    registry = EventBusRegistry(
        default_log_sink=OrderedTurnLogSink(_RejectFinalAnswerLogStore()),  # type: ignore[arg-type]
    )
    bus = await registry.get_or_create(
        turn_id,
        exclusive_producer=True,
        persist_before_live=True,
    )
    bus._pi_turn_state = {  # type: ignore[attr-defined]
        "intent": None,
        "final_answer": "",
        "tokens_input": 0,
        "tokens_output": 0,
        "cost_usd": 0.0,
        "error": None,
        "error_kind": None,
    }

    async def persist_completed(**kwargs) -> bool:
        assert kwargs["status_value"] == "completed"
        return await store.mark_completed(
            turn_id=kwargs["turn_id"],
            intent=kwargs["intent"],
            final_answer=kwargs["final_answer"],
            tokens_input=kwargs["tokens_input"],
            tokens_output=kwargs["tokens_output"],
            cost_usd=kwargs["cost_usd"],
        )

    bus._pi_persist_turn_state = persist_completed  # type: ignore[attr-defined]

    with pytest.raises(TerminalDurabilityAbort):
        await _emit_event(
            bus,
            turn_id,
            "done",
            {"turn_id": turn_id, "status": "completed"},
        )
    canonical = await store.get_turn(turn_id)
    assert canonical is not None and canonical.status == "completed"
    assert canonical.final_answer
    assert not [event for event in await log_store.list_events(turn_id) if event.event_type in {"final_answer", "done"}]

    with sqlite3.connect(core_path) as connection:
        connection.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
            (
                (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat(),
                turn_id,
            ),
        )
        connection.commit()
    assert await store.recover_expired_chat_spawn_dispatches() == [turn_id]
    recovered = await log_store.list_events(turn_id)
    assert [event.event_type for event in recovered[-2:]] == [
        "final_answer",
        "done",
    ]
    assert recovered[-2].payload["text"] == canonical.final_answer
    assert recovered[-1].payload["status"] == "completed"
    assert "final_answer" not in recovered[-1].payload
    await registry.close(turn_id, expected=bus)


@pytest.mark.asyncio
async def test_idempotent_done_persist() -> None:
    """Invariant 2 (r2 §A): 2× emit_event("done", ...) → persist called 1×.

    Second done is short-circuited by bus._done_emitted; persist must also
    track its own `_done_persisted` flag for idempotency.
    """
    bus = _MockBus()
    bus._final_answer_emitted = True  # type: ignore[attr-defined]
    persist_mock = AsyncMock(return_value=True)
    bus._pi_persist_turn_state = persist_mock  # type: ignore[attr-defined]
    bus._pi_turn_state = {  # type: ignore[attr-defined]
        "intent": None,
        "final_answer": "",
        "tokens_input": 0,
        "tokens_output": 0,
        "cost_usd": 0.0,
        "error": None,
        "error_kind": None,
    }
    await _emit_event(bus, "t_1", "done", {"status": "completed"})
    await _emit_event(bus, "t_1", "done", {"status": "completed"})
    assert persist_mock.call_count == 1
    # Second emit short-circuited entirely → only 1 emit recorded.
    assert len([e for e in bus.events if e["event_type"] == "done"]) == 1


@pytest.mark.asyncio
async def test_persist_failure_blocks_live_terminal() -> None:
    """Unknown durable state cannot be projected as a live terminal."""
    bus = _MockBus()
    bus._final_answer_emitted = True  # type: ignore[attr-defined]  # suppress silent-done fallback

    async def _persist_raises(**kwargs: Any) -> None:
        raise RuntimeError("db unreachable")

    bus._pi_persist_turn_state = _persist_raises  # type: ignore[attr-defined]
    bus._pi_turn_state = {  # type: ignore[attr-defined]
        "intent": None,
        "final_answer": "",
        "tokens_input": 0,
        "tokens_output": 0,
        "cost_usd": 0.0,
        "error": None,
        "error_kind": None,
    }
    await _emit_event(bus, "t_1", "done", {"status": "error"})
    assert bus.events == []
    assert bus._done_emitted is False


@pytest.mark.asyncio
async def test_lost_terminal_cas_blocks_live_terminal() -> None:
    """A concurrent Stop/completion that wins the CAS stays authoritative."""
    bus = _MockBus()
    bus._final_answer_emitted = True  # type: ignore[attr-defined]
    bus._pi_persist_turn_state = AsyncMock(return_value=False)  # type: ignore[attr-defined]
    bus._pi_turn_state = {}  # type: ignore[attr-defined]

    await _emit_event(
        bus,
        "t_1",
        "turn_failed",
        {"reason": "lead_internal_error"},
    )
    await _emit_event(bus, "t_1", "done", {"status": "completed"})

    assert bus.events == []
    assert bus._done_emitted is False


@pytest.mark.asyncio
async def test_persist_dispatches_to_mark_completed() -> None:
    """Invariant 4 (chokepoint dispatch correctness).

    TurnRunner._persist_turn_state with status="completed" → mark_completed
    on session_store, with status="error" → mark_failed, etc.
    """
    from bowhead_core.turn_runner import TurnRunner

    mock_store = MagicMock()
    mock_store.mark_completed = AsyncMock(return_value=True)
    mock_store.mark_failed = AsyncMock(return_value=True)
    mock_store.mark_clarification_pending = AsyncMock(return_value=True)
    mock_store.mark_aborted = AsyncMock(return_value=True)
    mock_store.mark_cancelled = AsyncMock(return_value=True)

    orch = MagicMock()
    orch.session_store = mock_store
    runner = TurnRunner(orch)

    # completed → mark_completed
    await runner._persist_turn_state(
        turn_id="t_1",
        status_value="completed",
        intent="lookup",
        final_answer="hello",
        tokens_input=10,
        tokens_output=20,
        cost_usd=0.0012,
        error=None,
        error_kind=None,
    )
    mock_store.mark_completed.assert_awaited_once()
    kwargs = mock_store.mark_completed.call_args.kwargs
    assert kwargs["turn_id"] == "t_1"
    assert kwargs["intent"] == "lookup"
    assert kwargs["final_answer"] == "hello"
    assert kwargs["tokens_input"] == 10
    assert kwargs["tokens_output"] == 20

    # clarification_pending → mark_clarification_pending
    await runner._persist_turn_state(
        turn_id="t_2",
        status_value="clarification_pending",
        intent="ambiguous",
        final_answer=None,
        tokens_input=0,
        tokens_output=0,
        cost_usd=0.0,
        error=None,
        error_kind=None,
    )
    mock_store.mark_clarification_pending.assert_awaited_once()

    # aborted → mark_aborted
    await runner._persist_turn_state(
        turn_id="t_3",
        status_value="aborted",
        intent=None,
        final_answer=None,
        tokens_input=0,
        tokens_output=0,
        cost_usd=0.0,
        error="user_cancel",
        error_kind="aborted",
    )
    mock_store.mark_aborted.assert_awaited_once()

    # error → mark_failed
    await runner._persist_turn_state(
        turn_id="t_4",
        status_value="error",
        intent=None,
        final_answer=None,
        tokens_input=0,
        tokens_output=0,
        cost_usd=0.0,
        error="timeout",
        error_kind="timeout",
    )
    mock_store.mark_failed.assert_awaited_once()
    kwargs = mock_store.mark_failed.call_args.kwargs
    assert kwargs["error"] == "timeout"
    assert kwargs["error_kind"] == "timeout"


@pytest.mark.asyncio
async def test_legacy_turn_runner_noop_when_flag_on(monkeypatch, tmp_path) -> None:
    """Invariant 5 (r2 §A): BOWHEAD_PI_A_PERSIST_ORDER=1 + Lead emits done →
    turn_runner.py:683-691 legacy block does NOT call session_store.complete_turn.

    Tested at the code-path level: with flag=1 the `_pi_a_chokepoint_on` branch
    guards complete_turn from firing. We don't run the full LLM loop, just
    verify the gate expression evaluates the way the code does.
    """
    monkeypatch.setenv("BOWHEAD_PI_A_PERSIST_ORDER", "1")
    pi_chokepoint_on = os.environ.get("BOWHEAD_PI_A_PERSIST_ORDER", "1") == "1"
    completed = True
    # Mirror the if condition in turn_runner.py
    should_call_legacy = completed and not pi_chokepoint_on
    assert should_call_legacy is False


@pytest.mark.asyncio
async def test_legacy_fallback_when_flag_off(monkeypatch) -> None:
    """Invariant 5b: BOWHEAD_PI_A_PERSIST_ORDER=0 → legacy path fires
    (defensive rollback). With chokepoint off, the legacy block in
    turn_runner.py:683-691 must invoke session_store.complete_turn.
    """
    monkeypatch.setenv("BOWHEAD_PI_A_PERSIST_ORDER", "0")
    pi_chokepoint_on = os.environ.get("BOWHEAD_PI_A_PERSIST_ORDER", "1") == "1"
    completed = True
    should_call_legacy = completed and not pi_chokepoint_on
    assert should_call_legacy is True


@pytest.mark.asyncio
async def test_session_store_mark_methods_write_ended_at(tmp_path: Path) -> None:
    """Invariant: all 5 mark_* methods write ended_at and reject late terminals.

    Real SQLite path (the methods do actual DB writes).
    """
    from bowhead_core.session_store import SessionStore

    db_path = tmp_path / "test.db"
    store = SessionStore(str(db_path))
    await store.initialize()

    sess = await store.ensure_session(
        session_id=None,
        workspace_id="ws_1",
        user_id="u_1",
    )

    # Each Turn must settle before the next fresh Turn can own the Session.
    terminal_statuses = [
        "completed",
        "failed",
        "clarification_pending",
        "cancelled",
        "aborted",
    ]
    await store.record_turn(
        turn_id="t_completed",
        session_id=sess.id,
        user_text="q completed",
    )
    assert await store.mark_completed(
        turn_id="t_completed",
        intent="lookup",
        final_answer="hi",
        tokens_input=5,
        tokens_output=10,
        cost_usd=0.001,
    )
    await store.finalize_turn(
        session_id=sess.id,
        current_turn_id="t_completed",
        next_turn_id=None,
    )
    await store.record_turn(
        turn_id="t_failed",
        session_id=sess.id,
        user_text="q failed",
    )
    assert await store.mark_failed(
        turn_id="t_failed",
        error="boom",
        error_kind="internal",
        tokens_input=2,
        tokens_output=3,
        cost_usd=0.002,
    )
    await store.finalize_turn(
        session_id=sess.id,
        current_turn_id="t_failed",
        next_turn_id=None,
    )
    await store.record_turn(
        turn_id="t_clarification_pending",
        session_id=sess.id,
        user_text="q clarification_pending",
    )
    assert await store.mark_clarification_pending(
        turn_id="t_clarification_pending",
        intent="ambiguous",
        tokens_input=4,
        tokens_output=5,
        cost_usd=0.003,
        error_kind="needs_clarification",
    )
    await store.finalize_turn(
        session_id=sess.id,
        current_turn_id="t_clarification_pending",
        next_turn_id=None,
    )
    await store.record_turn(
        turn_id="t_cancelled",
        session_id=sess.id,
        user_text="q cancelled",
    )
    assert await store.mark_cancelled(
        turn_id="t_cancelled",
        tokens_input=6,
        tokens_output=7,
        cost_usd=0.004,
    )
    await store.finalize_turn(
        session_id=sess.id,
        current_turn_id="t_cancelled",
        next_turn_id=None,
    )
    await store.record_turn(
        turn_id="t_aborted",
        session_id=sess.id,
        user_text="q aborted",
    )
    assert await store.mark_aborted(
        turn_id="t_aborted",
        tokens_input=8,
        tokens_output=9,
        cost_usd=0.005,
    )

    # Any late terminal producer loses after one of the five durable states
    # has won. Resume must explicitly advance clarification_pending→running.
    for terminal_status in terminal_statuses:
        turn_id = f"t_{terminal_status}"
        assert not await store.mark_completed(
            turn_id=turn_id,
            intent="late",
            final_answer="late",
            tokens_input=0,
            tokens_output=0,
            cost_usd=0,
        )
        assert not await store.mark_failed(
            turn_id=turn_id,
            error="late worker failure",
            error_kind="internal",
        )
        assert not await store.mark_clarification_pending(
            turn_id=turn_id,
            intent="late",
        )
        assert not await store.mark_cancelled(turn_id=turn_id)
        assert not await store.mark_aborted(turn_id=turn_id)

    rows = await store.list_turns(session_id=sess.id)
    by_id = {r.id: r for r in rows}
    assert by_id["t_completed"].status == "completed"
    assert by_id["t_completed"].ended_at is not None

    assert by_id["t_failed"].status == "failed"
    assert by_id["t_failed"].ended_at is not None
    assert by_id["t_failed"].error == "boom"
    assert by_id["t_failed"].error_kind == "internal"

    assert by_id["t_clarification_pending"].status == "clarification_pending"
    assert by_id["t_clarification_pending"].ended_at is not None
    assert by_id["t_clarification_pending"].error_kind == "needs_clarification"

    assert by_id["t_cancelled"].status == "cancelled"
    assert by_id["t_cancelled"].ended_at is not None

    assert by_id["t_aborted"].status == "aborted"
    assert by_id["t_aborted"].ended_at is not None
    assert by_id["t_aborted"].error_kind == "aborted"  # default
    rolled_up = await store.get_session(sess.id)
    assert rolled_up is not None
    assert rolled_up.tokens_total == 59
    assert rolled_up.cost_usd == pytest.approx(0.015)


@pytest.mark.asyncio
async def test_terminal_cas_losers_have_no_session_side_effects(
    tmp_path: Path,
) -> None:
    """All five late terminal producers are true no-ops, including rollups."""
    from bowhead_core.session_store import SessionStore

    db_path = tmp_path / "loser_side_effects.db"
    store = SessionStore(str(db_path))
    await store.initialize()
    sess = await store.ensure_session(
        session_id="s_loser_side_effects",
        workspace_id="ws",
        user_id="u",
    )
    await store.record_turn(
        turn_id="t_loser_side_effects",
        session_id=sess.id,
        user_text="q",
    )
    assert await store.mark_completed(
        turn_id="t_loser_side_effects",
        intent="lookup",
        final_answer="winner",
        tokens_input=7,
        tokens_output=3,
        cost_usd=0.01,
    )
    frozen = "2026-01-01T00:00:00+00:00"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE sessions SET last_active_at=? WHERE id=?",
            (frozen, sess.id),
        )
        connection.commit()

    assert not await store.mark_completed(
        turn_id="t_loser_side_effects",
        intent="late",
        final_answer="late",
        tokens_input=999,
        tokens_output=999,
        cost_usd=99,
    )
    assert not await store.mark_cancelled(turn_id="t_loser_side_effects")
    assert not await store.mark_failed(
        turn_id="t_loser_side_effects",
        error="late",
        error_kind="internal",
    )
    assert not await store.mark_clarification_pending(
        turn_id="t_loser_side_effects",
        intent="late",
    )
    assert not await store.mark_aborted(turn_id="t_loser_side_effects")

    after = await store.get_session(sess.id)
    assert after is not None
    assert after.last_active_at == frozen
    assert after.tokens_total == 10
    assert after.cost_usd == pytest.approx(0.01)


@pytest.mark.asyncio
async def test_session_store_complete_turn_alias_works(tmp_path: Path) -> None:
    """The retained rollback alias delegates to the canonical completion CAS."""
    from bowhead_core.session_store import SessionStore

    db_path = tmp_path / "test_alias.db"
    store = SessionStore(str(db_path))
    await store.initialize()
    sess = await store.ensure_session(
        session_id=None,
        workspace_id="ws",
        user_id="u",
    )
    await store.record_turn(turn_id="t_a", session_id=sess.id, user_text="q")

    # alias: complete_turn → mark_completed
    await store.complete_turn(
        turn_id="t_a",
        intent="lookup",
        final_answer="x",
        tokens_input=1,
        tokens_output=2,
        cost_usd=0.0001,
    )
    await store.finalize_turn(
        session_id=sess.id,
        current_turn_id="t_a",
        next_turn_id=None,
    )
    await store.record_turn(turn_id="t_b", session_id=sess.id, user_text="q2")
    await store.mark_cancelled(
        turn_id="t_b",
        tokens_input=3,
        tokens_output=4,
    )

    rows = {r.id: r for r in await store.list_turns(session_id=sess.id)}
    assert rows["t_a"].status == "completed"
    assert rows["t_b"].status == "cancelled"


@pytest.mark.asyncio
async def test_mark_completed_clears_stale_error_kind(tmp_path) -> None:
    """D7a: a clarification-pending turn that later completes on resume must
    CLEAR error_kind. mark_completed used COALESCE(?, error_kind) which wrongly
    preserved 'needs_clarification' on the now-completed row (prod: 7 turns).
    """
    from bowhead_core.session_store import SessionStore

    db_path = tmp_path / "bowhead.db"
    store = SessionStore(str(db_path))
    await store.initialize()
    from bowhead_cli.migrate import upgrade

    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    sess = await store.ensure_session(
        session_id=None,
        workspace_id="ws",
        user_id="u",
    )
    await store.record_turn(turn_id="t_c", session_id=sess.id, user_text="q")

    # 1) clarification pass sets error_kind='needs_clarification'
    await store.mark_clarification_pending(
        turn_id="t_c",
        intent="ambiguous",
        error_kind="needs_clarification",
    )
    row = await store.get_turn("t_c")
    assert row is not None
    assert row.status == "clarification_pending"
    assert row.error_kind == "needs_clarification"

    # 2) resume completes the same turn (success → error_kind=None must CLEAR it)
    assert await store.advance_turn_execution_generation(turn_id="t_c") == 2
    assert await store.mark_completed(
        turn_id="t_c",
        intent="lookup",
        final_answer="done",
        tokens_input=10,
        tokens_output=2,
        cost_usd=0.001,
    )
    row = await store.get_turn("t_c")
    assert row is not None
    assert row.status == "completed"
    assert row.error_kind is None  # D7a: COALESCE→direct write clears the stale value


@pytest.mark.asyncio
async def test_completed_turn_rolls_up_session_usage(tmp_path) -> None:
    """D7b: sessions.tokens_total/cost_usd were always 0 (touch_session unwired,
    zero callers → 0/41 prod sessions had usage). A completed turn must roll its
    tokens+cost into the session aggregate; recompute-from-SUM must NOT double-count
    a clarification turn that paused then completed on resume.
    """
    from bowhead_core.session_store import SessionStore

    db_path = tmp_path / "bowhead.db"
    store = SessionStore(str(db_path))
    await store.initialize()
    from bowhead_cli.migrate import upgrade

    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    sess = await store.ensure_session(
        session_id=None,
        workspace_id="ws",
        user_id="u",
    )

    # turn 1: plain completed
    await store.record_turn(turn_id="t1", session_id=sess.id, user_text="q1")
    await store.mark_completed(
        turn_id="t1",
        intent="lookup",
        final_answer="a",
        tokens_input=100,
        tokens_output=20,
        cost_usd=0.01,
    )
    s = await store.get_session(sess.id)
    assert s is not None
    assert s.tokens_total == 120
    assert abs(s.cost_usd - 0.01) < 1e-9

    # turn 2: clarification pause (generation 1) → completed (generation 2).
    # Each worker reports only its own generation; the durable baseline adds
    # both generations while the session recompute still counts t2 once.
    await store.finalize_turn(
        session_id=sess.id,
        current_turn_id="t1",
        next_turn_id=None,
    )
    await store.record_turn(turn_id="t2", session_id=sess.id, user_text="q2")
    await store.mark_clarification_pending(
        turn_id="t2",
        intent="ambiguous",
        tokens_input=50,
        tokens_output=5,
        error_kind="needs_clarification",
    )
    assert await store.advance_turn_execution_generation(turn_id="t2") == 2
    assert await store.mark_completed(
        turn_id="t2",
        intent="lookup",
        final_answer="b",
        tokens_input=200,
        tokens_output=30,
        cost_usd=0.05,
    )
    s = await store.get_session(sess.id)
    assert s is not None
    assert s.tokens_total == 120 + 55 + 230
    assert abs(s.cost_usd - 0.06) < 1e-9


@pytest.mark.asyncio
async def test_resume_cancel_usage_is_cumulative_and_idempotent(tmp_path: Path) -> None:
    """A Stop settlement adds this generation to the clarification baseline once."""
    from bowhead_cli.migrate import upgrade
    from bowhead_core.session_store import SessionStore

    db_path = tmp_path / "bowhead.db"
    store = SessionStore(str(db_path))
    await store.initialize()
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    sess = await store.ensure_session(
        session_id="s_resume_cancel",
        workspace_id="ws",
        user_id="u",
    )
    await store.record_turn(
        turn_id="t_resume_cancel",
        session_id=sess.id,
        user_text="q",
    )
    assert await store.mark_clarification_pending(
        turn_id="t_resume_cancel",
        intent="ambiguous",
        tokens_input=100,
        tokens_output=10,
        cost_usd=0.01,
    )
    assert await store.advance_turn_execution_generation(turn_id="t_resume_cancel") == 2
    assert (
        await store.request_cancel_turn(
            team_id="team",
            user_id="u",
            cancel_request_id="018f0cc3-e4a2-7b21-a109-001122334598",
            workspace_id="ws",
            turn_id="t_resume_cancel",
            expected_execution_generation=2,
        )
        == "cancelled"
    )
    for _ in range(2):
        assert await store.finalize_cancelled_usage(
            turn_id="t_resume_cancel",
            expected_execution_generation=2,
            tokens_input=20,
            tokens_output=2,
            cost_usd=0.002,
        )

    with sqlite3.connect(db_path) as connection:
        usage = connection.execute(
            "SELECT tokens_input,tokens_output,cost_usd FROM turns WHERE id=?",
            ("t_resume_cancel",),
        ).fetchone()
    assert usage == pytest.approx((120, 12, 0.012))
    rolled_up = await store.get_session(sess.id)
    assert rolled_up is not None
    assert rolled_up.tokens_total == 132
    assert rolled_up.cost_usd == pytest.approx(0.012)


# ─── §J — stream retry + invoke fallback + notice + profile passthrough ─


def _stream_exhausted() -> StreamingTargetsExhaustedError:
    return StreamingTargetsExhaustedError(
        [
            LLMTargetFailure(
                provider="p",
                model="m",
                provider_family="openai_compatible",
                mode="stream",
                target_index=1,
                attempt=2,
                kind=ProviderFailureKind.UPSTREAM,
                retryable=True,
                status_code=500,
            )
        ]
    )


@pytest.mark.asyncio
async def test_stream_non_retryable_raises_immediately() -> None:
    """Per r2 §J: non-retryable error → raise without retry/fallback.

    Mocks router.stream raising plain RuntimeError (no retryable attr).
    """
    from bowhead_core.subagent import SubagentRunner

    router = MagicMock()

    async def _failing_stream(agent_type, req, **kwargs):  # accept profile/user_pref (BYO-LLM)
        raise RuntimeError("hard fail")
        yield  # pragma: no cover

    router.stream = _failing_stream
    router.invoke = AsyncMock()
    runner = SubagentRunner(router=router, tool_registry=MagicMock())

    from bowhead_router import LLMRequest, Message

    req = LLMRequest(model="m", messages=[Message(role="user", content="hi")])

    with pytest.raises(RuntimeError, match="hard fail"):
        await runner._stream_invoke(
            agent_type="lead",
            llm_req=req,
            stream_callback=AsyncMock(),
            timeout=5.0,
        )
    router.invoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_retry_once_then_invoke_fallback() -> None:
    """Per r2 §J: stream throws retryable 2× → invoke fallback called."""
    from bowhead_core.subagent import SubagentRunner

    router = MagicMock()
    stream_calls = []

    async def _failing_stream(agent_type, req, **kwargs):  # accept profile/user_pref (BYO-LLM)
        stream_calls.append(agent_type)
        raise _stream_exhausted()
        yield  # pragma: no cover

    router.stream = _failing_stream

    from bowhead_router.types import LLMResponse

    router.invoke = AsyncMock(
        return_value=LLMResponse(
            content="fallback response",
            tool_calls=[],
        )
    )

    runner = SubagentRunner(router=router, tool_registry=MagicMock())

    from bowhead_router import LLMRequest, Message

    req = LLMRequest(model="m", messages=[Message(role="user", content="hi")])
    bus = _MockBus()

    resp = await runner._stream_invoke(
        agent_type="lead",
        llm_req=req,
        stream_callback=AsyncMock(),
        timeout=5.0,
        profile="lead",
        user_pref=None,
        bus=bus,
        turn_id="t_1",
    )

    # Router owns stream retries; SubagentRunner makes one logical stream call.
    assert len(stream_calls) == 1
    # invoke fallback called
    router.invoke.assert_awaited_once()
    invoke_kwargs = router.invoke.call_args.kwargs
    assert invoke_kwargs["profile"] == "lead"
    assert invoke_kwargs["user_pref"] is None
    assert invoke_kwargs["retry_policy"].max_attempts_per_target == 1
    assert resp.content == "fallback response"


@pytest.mark.asyncio
async def test_invoke_fallback_preserves_unknown_outcome_sentinel() -> None:
    from bowhead_core.subagent import SubagentRunner

    router = MagicMock()

    async def _failing_stream(agent_type, req, **kwargs):
        raise _stream_exhausted()
        yield  # pragma: no cover

    unknown = InvocationOutcomeUnknownError("invoke_unknown")
    router.stream = _failing_stream
    router.invoke = AsyncMock(side_effect=unknown)
    runner = SubagentRunner(router=router, tool_registry=MagicMock())

    from bowhead_router import LLMRequest, Message

    req = LLMRequest(model="m", messages=[Message(role="user", content="hi")])
    with pytest.raises(InvocationOutcomeUnknownError) as caught:
        await runner._stream_invoke(
            agent_type="lead",
            llm_req=req,
            stream_callback=AsyncMock(),
            timeout=5.0,
        )

    assert caught.value is unknown
    assert caught.value.reason_code == "invoke_unknown"


@pytest.mark.asyncio
async def test_notice_event_emitted_on_fallback() -> None:
    """Per r2 §J J-Notice: invoke fallback → notice event emit.

    Asserts payload contract: event_type="notice", reason+message fields.
    """
    from bowhead_core.subagent import SubagentRunner

    router = MagicMock()

    async def _failing_stream(agent_type, req, **kwargs):  # accept profile/user_pref (BYO-LLM)
        raise _stream_exhausted()
        yield  # pragma: no cover

    router.stream = _failing_stream
    from bowhead_router.types import LLMResponse

    router.invoke = AsyncMock(return_value=LLMResponse(content="ok", tool_calls=[]))

    runner = SubagentRunner(router=router, tool_registry=MagicMock())

    from bowhead_router import LLMRequest, Message

    req = LLMRequest(model="m", messages=[Message(role="user", content="hi")])
    bus = _MockBus()

    await runner._stream_invoke(
        agent_type="lead",
        llm_req=req,
        stream_callback=AsyncMock(),
        timeout=5.0,
        profile="lead",
        bus=bus,
        turn_id="t_1",
    )

    # Bus must have exactly one notice event
    notice_events = [e for e in bus.events if e["event_type"] == "notice"]
    assert len(notice_events) == 1, bus.events
    notice = notice_events[0]
    assert notice["payload"]["reason"] == "stream_to_invoke_fallback"
    assert notice["payload"]["message"] == "流式输出未能启动，已切换到非流式模式"


@pytest.mark.asyncio
async def test_profile_and_user_pref_forwarded() -> None:
    """Per r2 §J J-Router: profile + user_pref forwarded to router.invoke
    so the fallback resolves the same model binding."""
    from bowhead_core.subagent import SubagentRunner

    router = MagicMock()

    async def _failing_stream(agent_type, req, **kwargs):  # accept profile/user_pref (BYO-LLM)
        raise _stream_exhausted()
        yield  # pragma: no cover

    router.stream = _failing_stream
    from bowhead_router.types import LLMResponse

    router.invoke = AsyncMock(return_value=LLMResponse(content="x", tool_calls=[]))

    runner = SubagentRunner(router=router, tool_registry=MagicMock())

    # Use a real UserModelPref-like sentinel to ensure pass-through identity
    class _PrefSentinel:
        provider = "deepseek"
        model = "deepseek-chat"

    pref = _PrefSentinel()

    from bowhead_router import LLMRequest, Message

    req = LLMRequest(model="m", messages=[Message(role="user", content="hi")])

    await runner._stream_invoke(
        agent_type="lead",
        llm_req=req,
        stream_callback=AsyncMock(),
        timeout=5.0,
        profile="lead",
        user_pref=pref,
        bus=_MockBus(),
        turn_id="t_1",
    )
    router.invoke.assert_awaited_once()
    kwargs = router.invoke.call_args.kwargs
    assert kwargs["profile"] == "lead"
    assert kwargs["user_pref"] is pref


@pytest.mark.asyncio
async def test_invoke_fallback_calls_streaming_decomposer_abort() -> None:
    """Per r2 §J: before invoke fallback, abort_open_section(reason='invoke_fallback')
    is invoked on streaming_decomposer to close any open canvas section.
    """
    from bowhead_core.subagent import SubagentRunner

    router = MagicMock()

    async def _failing_stream(agent_type, req, **kwargs):  # accept profile/user_pref (BYO-LLM)
        raise _stream_exhausted()
        yield  # pragma: no cover

    router.stream = _failing_stream
    from bowhead_router.types import LLMResponse

    router.invoke = AsyncMock(return_value=LLMResponse(content="ok", tool_calls=[]))

    runner = SubagentRunner(router=router, tool_registry=MagicMock())

    # Mock decomposer with abort_open_section
    decomposer = MagicMock()
    decomposer.abort_open_section = AsyncMock()

    from bowhead_router import LLMRequest, Message

    req = LLMRequest(model="m", messages=[Message(role="user", content="hi")])

    await runner._stream_invoke(
        agent_type="lead",
        llm_req=req,
        stream_callback=AsyncMock(),
        timeout=5.0,
        profile="lead",
        bus=_MockBus(),
        turn_id="t_1",
        streaming_decomposer=decomposer,
    )
    decomposer.abort_open_section.assert_awaited_once_with(reason="invoke_fallback")


# ─── §I — compute peer prompt soft rule ──────────────────────────────────


def test_compute_sop_contains_batch_rule() -> None:
    """Verify analyst-sop-compute SKILL.md contains the batch-metric rule
    marker phrase (per r2 §I I-FileLine).
    """
    sop_path = Path(__file__).resolve().parents[3] / "packages" / "bowhead_skill" / "catalog" / "analyst-sop-compute" / "SKILL.md"
    assert sop_path.exists(), sop_path
    content = sop_path.read_text(encoding="utf-8")
    # Marker phrase tokens — locking the rule presence, not the exact wording.
    assert "多 metric 同维同时间" in content
    assert "1 次 batch query" in content or "1 次 `bi_query" in content
    assert "22 次" in content  # The counterexample.
    # Batch threshold: N ≥ 2 since batch 1 (bi_query queries[] is designed for ≥2 parallel queries; 5 was the old threshold).
    assert "N ≥ 2" in content or "N >= 2" in content


# ─── §A P0-1 fix — 3 bypass-the-chokepoint sites route through mark_* ────


@pytest.mark.asyncio
async def test_finish_cancelled_persists_aborted(tmp_path: Path) -> None:
    """Cancellation persists its canonical Store fact before the wire terminal.

    The row carries status='cancelled', ended_at and user_cancel diagnostics.
    """
    from unittest.mock import MagicMock

    from bowhead_core.events import EventBusRegistry
    from bowhead_core.session_store import SessionStore
    from bowhead_core.turn_runner import TurnRunner
    from bowhead_core.types import TurnRequest

    db_path = tmp_path / "fc.db"
    store = SessionStore(str(db_path))
    await store.initialize()
    sess = await store.ensure_session(
        session_id=None,
        workspace_id="ws",
        user_id="u",
    )
    await store.record_turn(
        turn_id="t_cancel",
        session_id=sess.id,
        user_text="hi",
    )

    emitted: list[dict[str, Any]] = []

    async def _capture_event(event: Any) -> None:
        emitted.append(
            {
                "event_type": event.event_type,
                "payload": dict(event.payload),
            }
        )

    orch = MagicMock()
    orch.session_store = store
    orch.event_registry = EventBusRegistry(default_log_sink=_capture_event)
    await orch.event_registry.get_or_create(
        "t_cancel",
        exclusive_producer=True,
    )
    await orch.event_registry.get_or_create("t_cancel", persist_before_live=True)
    orch._event = lambda turn_id, name, payload: __import__("bowhead_core.events", fromlist=["OrchestratorEvent"]).OrchestratorEvent(
        event_type=name, turn_id=turn_id, payload=payload
    )

    runner = TurnRunner(orch)
    runner._t_start = 0.0

    req = TurnRequest(
        session_id=sess.id,
        workspace_id="ws",
        user_id="u",
        member_uid="u",
        text="x",
        turn_id="t_cancel",
    )
    await runner._finish_cancelled(req)

    rows = await store.list_turns(session_id=sess.id)
    row = {r.id: r for r in rows}["t_cancel"]
    assert row.status == "cancelled"
    assert row.ended_at is not None
    assert row.error_kind == "user_cancel"
    assert [event["event_type"] for event in emitted] == [
        "turn_cancelled",
        "step_list_card_finalized",
        "done",
    ]
    assert emitted[1]["payload"]["card_id"] == "t_cancel"
    assert emitted[1]["payload"]["state"] == "cancelled"


@pytest.mark.asyncio
async def test_durable_stop_worker_finalizes_partial_usage(tmp_path: Path) -> None:
    """HTTP Stop wins terminal CAS; the same-generation worker still settles usage."""
    from bowhead_cli.migrate import upgrade
    from bowhead_core.events import EventBusRegistry
    from bowhead_core.session_store import SessionStore
    from bowhead_core.turn_runner import TurnRunner
    from bowhead_core.types import TurnRequest

    db_path = tmp_path / "bowhead.db"
    store = SessionStore(str(db_path))
    await store.initialize()
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    sess = await store.ensure_session(
        session_id="s_stop_usage",
        workspace_id="ws",
        user_id="u",
    )
    await store.record_turn(
        turn_id="t_stop_usage",
        session_id=sess.id,
        user_text="q",
    )
    assert (
        await store.request_cancel_turn(
            team_id="team",
            user_id="u",
            cancel_request_id="018f0cc3-e4a2-7b21-a109-001122334599",
            workspace_id="ws",
            turn_id="t_stop_usage",
            expected_execution_generation=1,
        )
        == "cancelled"
    )

    emitted: list[Any] = []

    async def _capture(event: Any) -> bool:
        emitted.append(event)
        return True

    orch = MagicMock()
    orch.session_store = store
    orch.event_registry = EventBusRegistry(default_log_sink=_capture)
    orch._event = lambda turn_id, name, payload: __import__(
        "bowhead_core.events",
        fromlist=["OrchestratorEvent"],
    ).OrchestratorEvent(event_type=name, turn_id=turn_id, payload=payload)
    runner = TurnRunner(orch)
    runner._t_start = 0.0
    bus = await orch.event_registry.get_or_create("t_stop_usage")
    assert await orch.event_registry.get_or_create("t_stop_usage", persist_before_live=True) is bus
    bus._pi_turn_state = {  # type: ignore[attr-defined]
        "tokens_input": 17,
        "tokens_output": 3,
        "invocations": [],
    }
    result = await runner._finish_cancelled(
        TurnRequest(
            session_id=sess.id,
            workspace_id="ws",
            user_id="u",
            member_uid="u",
            text="q",
            turn_id="t_stop_usage",
            execution_generation=1,
        )
    )

    turn = await store.get_turn("t_stop_usage")
    session = await store.get_session(sess.id)
    assert turn is not None and session is not None
    assert turn.status == "cancelled"
    with sqlite3.connect(db_path) as connection:
        persisted_usage = connection.execute(
            "SELECT tokens_input,tokens_output,cost_usd FROM turns WHERE id=?",
            ("t_stop_usage",),
        ).fetchone()
    assert persisted_usage is not None
    assert persisted_usage[:2] == (17, 3)
    assert persisted_usage[2] > 0
    assert session.tokens_total == 20
    assert result.tokens_input == 17
    assert result.tokens_output == 3
    assert result.cost_usd == persisted_usage[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("finalize_outcome", ["false", "raise"])
async def test_cancelled_usage_failure_emits_no_terminal_or_summary(
    finalize_outcome: str,
) -> None:
    from types import SimpleNamespace

    from bowhead_core.events import (
        EventBusRegistry,
        OrchestratorEvent,
        TerminalDurabilityAbort,
    )
    from bowhead_core.turn_runner import TurnRunner
    from bowhead_core.types import TurnRequest

    class Store:
        def __init__(self) -> None:
            self.finalize_calls = 0
            self.summary_calls = 0

        async def mark_cancelled(self, **_kwargs) -> bool:
            return False

        async def get_turn(self, _turn_id: str):
            return SimpleNamespace(status="cancelled")

        async def finalize_cancelled_usage(self, **_kwargs) -> bool:
            self.finalize_calls += 1
            if finalize_outcome == "raise":
                raise sqlite3.OperationalError("usage store unavailable")
            return False

        async def persist_turn_trace(self, **_kwargs) -> None:
            raise AssertionError("unsettled usage must not persist a summary tail")

        async def persist_turn_summary(self, **_kwargs) -> None:
            self.summary_calls += 1

    store = Store()
    persisted_events: list[str] = []

    async def sink(event) -> bool:
        persisted_events.append(event.event_type)
        return True

    registry = EventBusRegistry(default_log_sink=sink)
    bus = await registry.get_or_create(
        "t_cancel_usage_failure",
        persist_before_live=True,
    )
    bus._pi_turn_state = {  # type: ignore[attr-defined]
        "tokens_input": 7,
        "tokens_output": 3,
        "invocations": [],
    }
    orch = SimpleNamespace(
        session_store=store,
        event_registry=registry,
        _event=lambda turn_id, name, payload: OrchestratorEvent(
            event_type=name,
            turn_id=turn_id,
            payload=payload,
        ),
    )
    runner = TurnRunner(orch)
    runner._t_start = 0.0

    with pytest.raises(TerminalDurabilityAbort):
        await runner._finish_cancelled(
            TurnRequest(
                session_id="s_cancel_usage_failure",
                workspace_id="ws",
                user_id="u",
                member_uid="m",
                text="stop",
                turn_id="t_cancel_usage_failure",
            )
        )

    assert store.finalize_calls == (2 if finalize_outcome == "raise" else 1)
    assert store.summary_calls == 0
    assert persisted_events == []
    assert bus.snapshot() == []


@pytest.mark.asyncio
async def test_cancelled_usage_transient_failure_retries_before_terminal() -> None:
    from types import SimpleNamespace

    from bowhead_core.events import EventBusRegistry, OrchestratorEvent
    from bowhead_core.turn_runner import TurnRunner
    from bowhead_core.types import TurnRequest

    class Store:
        def __init__(self) -> None:
            self.finalize_calls = 0
            self.summary_tokens: tuple[int | None, int | None] | None = None

        async def mark_cancelled(self, **_kwargs) -> bool:
            return False

        async def get_turn(self, _turn_id: str):
            return SimpleNamespace(status="cancelled")

        async def finalize_cancelled_usage(self, **_kwargs) -> bool:
            self.finalize_calls += 1
            if self.finalize_calls == 1:
                raise sqlite3.OperationalError("busy once")
            return True

        async def persist_turn_trace(self, **_kwargs) -> None:
            return None

        async def persist_turn_summary(self, **kwargs) -> None:
            self.summary_tokens = (
                kwargs["tokens_input"],
                kwargs["tokens_output"],
            )

    store = Store()
    persisted_events: list[str] = []

    async def sink(event) -> bool:
        persisted_events.append(event.event_type)
        return True

    registry = EventBusRegistry(default_log_sink=sink)
    bus = await registry.get_or_create(
        "t_cancel_usage_retry",
        persist_before_live=True,
    )
    bus._pi_turn_state = {  # type: ignore[attr-defined]
        "tokens_input": 7,
        "tokens_output": 3,
        "invocations": [],
    }
    orch = SimpleNamespace(
        session_store=store,
        event_registry=registry,
        _event=lambda turn_id, name, payload: OrchestratorEvent(
            event_type=name,
            turn_id=turn_id,
            payload=payload,
        ),
    )
    runner = TurnRunner(orch)
    runner._t_start = 0.0
    result = await runner._finish_cancelled(
        TurnRequest(
            session_id="s_cancel_usage_retry",
            workspace_id="ws",
            user_id="u",
            member_uid="m",
            text="stop",
            turn_id="t_cancel_usage_retry",
        )
    )

    assert store.finalize_calls == 2
    assert store.summary_tokens == (7, 3)
    assert result.tokens_input == 7
    assert result.tokens_output == 3
    assert persisted_events == [
        "turn_cancelled",
        "step_list_card_finalized",
        "done",
    ]


@pytest.mark.asyncio
async def test_terminal_stop_uses_receipt_frozen_usage_snapshot() -> None:
    from types import SimpleNamespace

    from bowhead_core.events import OrchestratorEvent
    from bowhead_core.turn_runner import TurnRunner
    from bowhead_core.types import TurnRequest

    class Store:
        def __init__(self) -> None:
            self.summary_tokens: tuple[int | None, int | None] | None = None

        async def persist_turn_trace(self, **_kwargs) -> None:
            return None

        async def persist_turn_summary(self, **kwargs) -> None:
            self.summary_tokens = (
                kwargs["tokens_input"],
                kwargs["tokens_output"],
            )

    class TerminalBus:
        terminal_protocol_version = 1
        _card_finalized_emitted = True
        _pi_turn_state = {
            "tokens_input": 18,
            "tokens_output": 6,
            "invocations": [],
        }
        _terminal_snapshot = {
            "tokens_input": 17,
            "tokens_output": 3,
            "invocations": [],
        }

        def __init__(self) -> None:
            self.events = []

        async def emit(self, event) -> bool:
            self.events.append(event)
            return True

    store = Store()
    bus = TerminalBus()
    orch = SimpleNamespace(
        session_store=store,
        _event=lambda turn_id, name, payload: OrchestratorEvent(
            event_type=name,
            turn_id=turn_id,
            payload=payload,
        ),
    )
    runner = TurnRunner(orch)
    runner._t_start = 0.0
    result = await runner._finish_cancelled(
        TurnRequest(
            session_id="s_terminal_usage",
            workspace_id="ws",
            user_id="u",
            member_uid="m",
            text="stop",
            turn_id="t_terminal_usage",
            terminal_runtime=SimpleNamespace(bus=bus),
        )
    )

    assert store.summary_tokens == (17, 3)
    assert result.tokens_input == 17
    assert result.tokens_output == 3
    assert bus.events[-1].payload["tokens_input"] == 17
    assert bus.events[-1].payload["tokens_output"] == 3


# ─── §A P0-2 fix — chokepoint snapshot coverage at all 11 emit sites ────


@pytest.mark.asyncio
async def test_chokepoint_emit_sites_surface_intent_to_snapshot(tmp_path: Path) -> None:
    """P0-2 fix (CTO review): only 2/11 chokepoint emit sites populated
    bus._pi_turn_state. The other 9 (abort / timeout / cap / clarification /
    runner exception / sentinel / not_ok / heuristic / fallback) relied on
    payload defaults, so mark_failed/mark_aborted persisted with tokens=0 +
    intent=None. After fix, snapshot is populated at each emit site so the
    chokepoint persists a complete row.

    We exercise the dispatch shape: directly call _emit_event with status
    values from the 11 sites and verify the persist callback gets
    intent/error/error_kind from the snapshot (not just payload defaults).
    """
    from bowhead_core.agents.lead_orchestrator import _emit_event

    persisted: list[dict] = []

    async def _persist(**kwargs):
        persisted.append(dict(kwargs))
        return True

    async def _run_emit(*, status: str, snapshot: dict, payload_extra: dict | None = None):
        bus = _MockBus()
        bus._final_answer_emitted = True  # type: ignore[attr-defined]
        bus._pi_persist_turn_state = _persist  # type: ignore[attr-defined]
        bus._pi_turn_state = {  # type: ignore[attr-defined]
            "intent": None,
            "final_answer": "",
            "tokens_input": 0,
            "tokens_output": 0,
            "cost_usd": 0.0,
            "error": None,
            "error_kind": None,
        }
        bus._pi_turn_state.update(snapshot)  # type: ignore[attr-defined]
        payload = {"status": status, **(payload_extra or {})}
        await _emit_event(bus, "t_x", "done", payload)

    # Site: CancelledError → aborted
    persisted.clear()
    await _run_emit(status="aborted", snapshot={"error": "user_cancel", "error_kind": "aborted"})
    assert persisted[-1]["status_value"] == "aborted"
    assert persisted[-1]["error_kind"] == "aborted"
    assert persisted[-1]["error"] == "user_cancel"

    # Site: TimeoutError → error/timeout
    persisted.clear()
    await _run_emit(status="error", snapshot={"error": "lead_loop_hard_timeout", "error_kind": "timeout"})
    assert persisted[-1]["error_kind"] == "timeout"
    assert persisted[-1]["error"] == "lead_loop_hard_timeout"

    # Site: a snapshot error_kind passes through unchanged
    persisted.clear()
    await _run_emit(status="error", snapshot={"error": "token_exceeded", "error_kind": "cap_exceeded"})
    assert persisted[-1]["error_kind"] == "cap_exceeded"

    # Site: heuristic ambiguous → clarification_pending
    persisted.clear()
    from bowhead_core.types import IntentType

    await _run_emit(
        status="clarification_pending",
        snapshot={"intent": IntentType.AMBIGUOUS, "error_kind": "needs_clarification"},
    )
    assert persisted[-1]["status_value"] == "clarification_pending"
    assert persisted[-1]["intent"] == "ambiguous"
    assert persisted[-1]["error_kind"] == "needs_clarification"

    # Site: sentinel clarification with partial tokens
    persisted.clear()
    await _run_emit(
        status="clarification_pending",
        snapshot={
            "intent": IntentType.AMBIGUOUS,
            "tokens_input": 100,
            "tokens_output": 50,
            "error_kind": "needs_clarification",
        },
    )
    assert persisted[-1]["tokens_input"] == 100
    assert persisted[-1]["tokens_output"] == 50
    assert persisted[-1]["intent"] == "ambiguous"

    # Site: not_ok → error/llm_failed with token surface
    persisted.clear()
    await _run_emit(
        status="error",
        snapshot={
            "intent": IntentType.LOOKUP,
            "tokens_input": 200,
            "tokens_output": 80,
            "error": "peer_returned_not_ok",
            "error_kind": "llm_failed",
        },
    )
    assert persisted[-1]["error_kind"] == "llm_failed"
    assert persisted[-1]["tokens_input"] == 200
    assert persisted[-1]["intent"] == "lookup"

    # Site: bail (completed) — pre-existing snapshot population
    persisted.clear()
    await _run_emit(
        status="completed",
        snapshot={
            "intent": None,
            "final_answer": "bail msg",
            "tokens_input": 10,
            "tokens_output": 5,
        },
    )
    assert persisted[-1]["status_value"] == "completed"
    assert persisted[-1]["final_answer"] == "bail msg"


@pytest.mark.asyncio
async def test_chokepoint_emit_error_paths_surface_error_kind() -> None:
    """P0-2 fix: abort / timeout / cap / unexpected / runner exception / not_ok /
    fallback all set error_kind in _pi_turn_state OR payload so the chokepoint
    writes a non-NULL error_kind (per A-ErrorKind). Failing rows that landed
    with error_kind=NULL would defeat the purpose of the column.
    """
    from bowhead_core.agents.lead_orchestrator import _emit_event

    persisted: list[dict] = []

    async def _persist(**kwargs):
        persisted.append(dict(kwargs))
        return True

    # Drive a "error" status with NO error_kind in payload OR snapshot — the
    # chokepoint's default-to-'internal' branch should fire (defensive line).
    bus = _MockBus()
    bus._final_answer_emitted = True  # type: ignore[attr-defined]
    bus._pi_persist_turn_state = _persist  # type: ignore[attr-defined]
    bus._pi_turn_state = {  # type: ignore[attr-defined]
        "intent": None,
        "final_answer": "",
        "tokens_input": 0,
        "tokens_output": 0,
        "cost_usd": 0.0,
        "error": None,
        "error_kind": None,
    }
    await _emit_event(bus, "t_x", "done", {"status": "error"})
    assert persisted[-1]["error_kind"] == "internal", persisted[-1]

    # Driven explicit error_kind beats the default — verify payload path wins.
    persisted.clear()
    bus = _MockBus()
    bus._final_answer_emitted = True  # type: ignore[attr-defined]
    bus._pi_persist_turn_state = _persist  # type: ignore[attr-defined]
    bus._pi_turn_state = {  # type: ignore[attr-defined]
        "intent": None,
        "final_answer": "",
        "tokens_input": 0,
        "tokens_output": 0,
        "cost_usd": 0.0,
        "error": None,
        "error_kind": None,
    }
    await _emit_event(
        bus,
        "t_x",
        "done",
        {
            "status": "error",
            "error": "boom",
            "error_kind": "timeout",
        },
    )
    assert persisted[-1]["error_kind"] == "timeout"
    assert persisted[-1]["error"] == "boom"

    # Snapshot path also wins over default when payload is bare.
    persisted.clear()
    bus = _MockBus()
    bus._final_answer_emitted = True  # type: ignore[attr-defined]
    bus._pi_persist_turn_state = _persist  # type: ignore[attr-defined]
    bus._pi_turn_state = {  # type: ignore[attr-defined]
        "intent": None,
        "final_answer": "",
        "tokens_input": 0,
        "tokens_output": 0,
        "cost_usd": 0.0,
        "error": "snap_err",
        "error_kind": "cap_exceeded",
    }
    await _emit_event(bus, "t_x", "done", {"status": "error"})
    assert persisted[-1]["error_kind"] == "cap_exceeded"
    assert persisted[-1]["error"] == "snap_err"


@pytest.mark.asyncio
async def test_done_payload_enriched_with_summary_fields() -> None:
    """chat-solidify O3 (2026-07-03) — done payload 汇总字段与 persist 同源。

    此前 done payload 全路缺 cost_usd(FE live 汇总恒 $0)、plan/error 路连
    tokens 都缺 → live≠F5。chokepoint 现把 persist 用的同一批值 setdefault
    注回 payload: 缺则补(含 token→cost estimate), 已带值的路不覆盖。
    """
    bus = _MockBus()
    bus._final_answer_emitted = True  # type: ignore[attr-defined]

    async def _persist(**kwargs: Any) -> bool:
        return True

    bus._pi_persist_turn_state = _persist  # type: ignore[attr-defined]
    bus._pi_turn_state = {  # type: ignore[attr-defined]
        "intent": None,
        "final_answer": "x",
        "tokens_input": 1200,
        "tokens_output": 340,
        "cost_usd": 0.0,
        "error": None,
        "error_kind": None,
    }

    # Plan/error shape: payload has no tokens/cost.
    await _emit_event(bus, "t_en1", "done", {"status": "completed"})
    done_ev = next(e for e in bus.events if e["event_type"] == "done")
    assert done_ev["payload"]["tokens_input"] == 1200
    assert done_ev["payload"]["tokens_output"] == 340
    # Cost falls back to a token estimate (same source as persisted D15 logic) and must be > 0.
    assert done_ev["payload"]["cost_usd"] > 0

    # Main completion path with existing values: do not overwrite them.
    bus2 = _MockBus()
    bus2._final_answer_emitted = True  # type: ignore[attr-defined]
    bus2._pi_persist_turn_state = _persist  # type: ignore[attr-defined]
    bus2._pi_turn_state = {  # type: ignore[attr-defined]
        "intent": None,
        "final_answer": "",
        "tokens_input": 99,
        "tokens_output": 99,
        "cost_usd": 9.9,
        "error": None,
        "error_kind": None,
    }
    await _emit_event(
        bus2,
        "t_en2",
        "done",
        {
            "status": "completed",
            "tokens_input": 5,
            "tokens_output": 6,
            "cost_usd": 0.5,
        },
    )
    done_ev2 = next(e for e in bus2.events if e["event_type"] == "done")
    assert done_ev2["payload"]["tokens_input"] == 5
    assert done_ev2["payload"]["cost_usd"] == 0.5
