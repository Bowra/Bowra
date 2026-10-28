from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from bowhead_core.terminal_finalizer import FinalizeRequest, TerminalFinalizer
from bowhead_core.terminal_protocol import ActorScope, PhaseScope
from bowhead_core.terminal_protocol_store import TerminalProtocolStore, TerminalReadContext
from test_terminal_finalizer import (
    _AUTH_DIGEST,
    _NOW,
    _completed_plan,
    _create_db,
    _start_execution,
)


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return _create_db(tmp_path)


@pytest.mark.asyncio
async def test_terminal_protocol_s3_admission_to_receipt_checkpoint_e2e(db_path: str) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(store)
    proposal = await store.accept_event(
        execution,
        event_type="analysis_progress",
        actor_scope=ActorScope.ROOT,
        phase_scope=PhaseScope.IN_GENERATION,
        payload={"step": "validated"},
    )
    barrier_calls: list[int] = []

    async def barrier(context) -> None:
        barrier_calls.append(context.generation_row_version)

    build_snapshots = []

    def build_candidate(snapshot):
        build_snapshots.append(snapshot)
        return replace(
            _completed_plan(execution.scope),
            closing_accepted_through=snapshot.flush.closing_accepted_through,
            persisted_event_highwater=snapshot.flush.persisted_event_highwater,
        )

    receipt = await TerminalFinalizer(store).finalize(
        FinalizeRequest(
            execution=execution,
            finalization_owner_instance_id="gateway_1:finalizer",
            closing_reason="completed",
            selected_variant_key="primary_none",
            authz_snapshot_digest=_AUTH_DIGEST,
            writer_admission_epoch=3,
            candidate_builder=build_candidate,
            producer_barrier=barrier,
        )
    )
    assert barrier_calls == [3]
    assert len(build_snapshots) == 1
    assert build_snapshots[0].flush.persisted_event_highwater == 1
    assert [ticket.event_id for ticket in build_snapshots[0].flush.tickets] == [proposal.event_id]
    read_context = TerminalReadContext(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        viewer="u_1",
        auth_fence_vector_digest=_AUTH_DIGEST,
    )
    checkpoint = await store.read_checkpoint(read_context)
    assert checkpoint.current_receipt_summary is not None
    assert checkpoint.current_receipt_summary.receipt_id == receipt.receipt_id
    events = await store.list_protocol_events(read_context)
    assert [event.event_id for event in events] == [proposal.event_id, "evt_answer_1", "evt_done_1"]
    assert [event.event_seq for event in events] == [1, 2, 3]
    assert checkpoint.persistent_event_highwater == 3
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_receipts").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_session_mutations").fetchone() == (1,)
