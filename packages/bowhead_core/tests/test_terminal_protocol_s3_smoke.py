from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from bowhead_core.terminal_finalizer import FinalizeRequest, TerminalFinalizer
from bowhead_core.terminal_protocol import CommitConfirmationDisposition
from bowhead_core.terminal_protocol_store import (
    TerminalProtocolFaultPoint,
    TerminalProtocolStore,
    TerminalReadContext,
)
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
async def test_terminal_protocol_s3_reopen_and_ack_loss_smoke(db_path: str) -> None:
    faulting = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=lambda point: (
            (_ for _ in ()).throw(RuntimeError("simulated process ack loss"))
            if point is TerminalProtocolFaultPoint.TERMINAL_AFTER_COMMIT_BEFORE_ACK
            else None
        ),
    )
    execution = await _start_execution(faulting)
    plan = _completed_plan(execution.scope)
    receipt = await TerminalFinalizer(faulting).finalize(
        FinalizeRequest(
            execution=execution,
            finalization_owner_instance_id="gateway_1:finalizer",
            closing_reason="completed",
            selected_variant_key="primary_none",
            authz_snapshot_digest=_AUTH_DIGEST,
            writer_admission_epoch=3,
            candidate_builder=lambda _context: plan,
        )
    )
    del faulting

    reopened = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    await reopened.initialize()
    confirmation = await reopened.confirm_commit(
        candidate_id=plan.candidate_id,
        candidate_projection_digest=plan.candidate_projection_digest,
    )
    assert confirmation.disposition is CommitConfirmationDisposition.COMMITTED
    assert confirmation.receipt == receipt
    checkpoint = await reopened.read_checkpoint(
        TerminalReadContext(
            team_id="team_1",
            session_id="s_1",
            turn_id="t_1",
            viewer="u_1",
            auth_fence_vector_digest=_AUTH_DIGEST,
        )
    )
    assert checkpoint.current_generation_state.value == "closed"
    assert checkpoint.current_receipt_summary is not None
    assert checkpoint.current_receipt_summary.receipt_id == receipt.receipt_id
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
