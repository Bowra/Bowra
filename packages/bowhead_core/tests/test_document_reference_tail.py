"""Native reservation expiry and consumed-owner recovery use real journals."""

import pytest
import test_document_agent_caller as callers
from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_turn_source import ReferenceExecutionUnavailableError
from bowhead_core.documents.agent_caller import _document_reference_lifecycle_in_tx

pytestmark = pytest.mark.integration


class RecoveryProbeFinished(Exception):
    pass


@pytest.mark.parametrize("phase", ["reserved", "reserved_spawned", "consumed", "foreign_owner", "missing_card"])
async def test_expired_native_resume_reconciles_without_a_replacement_call(tmp_path, monkeypatch, phase):
    original_claim = callers.claim_resume
    observations = []

    async def expire_and_recover(ctx, native, outcome, *, consumed):
        turn_id = native.context.turn_id
        original_calls = len(native.calls)
        async with open_shared_ctx(ctx.path) as db:
            await db.execute("BEGIN")
            assert await _document_reference_lifecycle_in_tx(db, turn_id=turn_id) == ("consumed" if consumed else "reserved")
            await db.rollback()
        with pytest.raises(ReferenceExecutionUnavailableError):
            await ctx.sessions.require_legacy_chat_execution(turn_id=turn_id)
        async with open_shared_ctx(ctx.path) as db:
            await db.execute("UPDATE chat_turn_spawn_dispatch SET lease_expires_at='2000-01-01' WHERE turn_id=?", (turn_id,))
            if phase == "foreign_owner":
                await db.execute("UPDATE chat_turn_spawn_dispatch SET owner_id='foreign-worker' WHERE turn_id=?", (turn_id,))
            await db.commit()
        if phase == "missing_card":
            from unittest.mock import AsyncMock

            monkeypatch.setattr("bowhead_core.document_clarification_lifecycle.document_card_id_in_tx", AsyncMock(return_value=None))
        recovered = await ctx.sessions.recover_expired_chat_spawn_dispatches()
        turn = await ctx.sessions.get_turn(turn_id)
        async with open_shared_ctx(ctx.path) as db:
            association = await (
                await db.execute(
                    "SELECT reserved_generation,consumed_generation FROM clarification_document_continuations WHERE turn_id=?", (turn_id,)
                )
            ).fetchone()
            primary = await (await db.execute("SELECT status FROM turn_agent_execution WHERE turn_id=?", (turn_id,))).fetchone()
            dispatch = await (await db.execute("SELECT state,owner_id FROM chat_turn_spawn_dispatch WHERE turn_id=?", (turn_id,))).fetchone()
        assert len(native.calls) == original_calls and primary[0] == "completed"
        if phase in {"foreign_owner", "missing_card"}:
            assert turn_id not in recovered and turn.status == "running"
            assert association[1] is not None and dispatch[0] == "spawned"
        elif consumed:
            assert turn_id in recovered and turn.status == "failed" and turn.error_kind == "dispatch_owner_lost"
            assert association[1] is not None and dispatch[0] == "failed"
        else:
            assert turn_id in recovered and turn.status == "clarification_pending"
            assert tuple(association) == (None, None) and dispatch[0] == "terminal"
            assert await ctx.sessions.is_turn_resume_ready(turn_id)
        assert await ctx.sessions.heartbeat_chat_spawn_dispatch(turn_id=turn_id, owner_id=native.context.execution_owner_id) == "lost"
        observations.append(turn_id)
        raise RecoveryProbeFinished

    async def claim_with_expiry(ctx, native, outcome, *args, **kwargs):
        if phase == "reserved":
            claim = ctx.sessions.claim_chat_spawn_dispatch_for_worker

            async def before_claim(**fields):
                if fields["owner_id"] == "resumed-document-worker":
                    await expire_and_recover(ctx, native, outcome, consumed=False)
                return await claim(**fields)

            monkeypatch.setattr(ctx.sessions, "claim_chat_spawn_dispatch_for_worker", before_claim)
        if phase == "reserved_spawned":
            from bowhead_core.turn_log_store import TurnLogStore

            record = TurnLogStore.record

            async def after_resume_record(log, **fields):
                result = await record(log, **fields)
                if fields.get("event_type") == "turn.resumed":
                    await expire_and_recover(ctx, native, outcome, consumed=False)
                return result

            monkeypatch.setattr(TurnLogStore, "record", after_resume_record)
        await original_claim(ctx, native, outcome, *args, **kwargs)
        await expire_and_recover(ctx, native, outcome, consumed=True)

    monkeypatch.setattr(callers, "claim_resume", claim_with_expiry)
    with pytest.raises(RecoveryProbeFinished):
        await callers.test_exact_mcp_obligation_precedes_native_job_and_original_question_resume(
            tmp_path, monkeypatch, invoke_required=True, final_reject=False, pause_fault=None, shared_tail=None
        )
    assert len(observations) == 1
