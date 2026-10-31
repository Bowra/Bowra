"""A reserved native question crosses the actual worker gate without weakening it."""

import pytest
import test_document_agent_caller as callers
from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_turn_source import ReferenceExecutionUnavailableError
from bowhead_core.document_chat_continuation import DocumentResumeReservation
from bowhead_core.document_chat_runtime import worker_from_context
from bowhead_core.document_clarification_lifecycle import document_card_id_in_tx
from bowhead_core.turn_log_store import TurnLogStore

pytestmark = pytest.mark.integration


class EntryProbeFinished(Exception):
    pass


@pytest.mark.parametrize("fault", [None, "expired", "stopped", "deleted", "rollback"])
async def test_original_reference_native_resume_passes_only_its_exact_worker_gate(tmp_path, monkeypatch, fault):
    original_claim = callers.claim_resume
    entered = []

    async def claim_with_entry(ctx, native, outcome, *args, **kwargs):
        original_record = TurnLogStore.record

        async def record_then_enter(log, **fields):
            result = await original_record(log, **fields)
            if fields.get("event_type") == "turn.resumed":
                worker = worker_from_context(native.context)
                async with open_shared_ctx(ctx.path) as db:
                    card_id = await document_card_id_in_tx(db, turn_id=worker.turn_id)
                reservation = DocumentResumeReservation(card_id=card_id, checkpoint=outcome.checkpoint, generation=worker.generation)
                with pytest.raises(ReferenceExecutionUnavailableError):
                    await ctx.sessions.require_legacy_chat_execution(turn_id=worker.turn_id)
                await ctx.sessions.require_legacy_chat_execution(turn_id=worker.turn_id, document_worker=worker, document_resume=reservation)
                for field in ("team_id", "user_id", "workspace_id", "session_id", "turn_id", "owner_id"):
                    with pytest.raises(ReferenceExecutionUnavailableError):
                        await ctx.sessions.require_legacy_chat_execution(
                            turn_id=worker.turn_id,
                            document_worker=worker.model_copy(update={field: "another-identity"}),
                            document_resume=reservation,
                        )
                for candidate in (
                    reservation.model_copy(update={"card_id": "another-card"}),
                    reservation.model_copy(update={"generation": worker.generation + 1}),
                    reservation.model_copy(update={"checkpoint": reservation.checkpoint.model_copy(update={"checkpoint_digest": "f" * 64})}),
                ):
                    with pytest.raises(ReferenceExecutionUnavailableError):
                        await ctx.sessions.require_legacy_chat_execution(turn_id=worker.turn_id, document_worker=worker, document_resume=candidate)
                for missing_worker, missing_resume in ((None, reservation), (worker, None)):
                    with pytest.raises(ReferenceExecutionUnavailableError):
                        await ctx.sessions.require_legacy_chat_execution(
                            turn_id=worker.turn_id, document_worker=missing_worker, document_resume=missing_resume
                        )
                async with open_shared_ctx(ctx.path) as db:
                    await db.execute("UPDATE chat_turn_spawn_dispatch SET lease_kind='turn' WHERE turn_id=?", (worker.turn_id,))
                    await db.commit()
                try:
                    with pytest.raises(ReferenceExecutionUnavailableError):
                        await ctx.sessions.require_legacy_chat_execution(turn_id=worker.turn_id, document_worker=worker, document_resume=reservation)
                    assert await ctx.sessions.heartbeat_chat_spawn_dispatch(turn_id=worker.turn_id, owner_id=worker.owner_id) == "lost"
                finally:
                    async with open_shared_ctx(ctx.path) as db:
                        await db.execute("UPDATE chat_turn_spawn_dispatch SET lease_kind='resume' WHERE turn_id=?", (worker.turn_id,))
                        await db.commit()
                assert await ctx.sessions.heartbeat_chat_spawn_dispatch(turn_id=worker.turn_id, owner_id=worker.owner_id) == "renewed"
                assert await ctx.sessions.heartbeat_chat_spawn_dispatch(turn_id=worker.turn_id, owner_id=worker.owner_id, settling=True) == "renewed"
                assert await ctx.sessions.heartbeat_chat_spawn_dispatch(turn_id=worker.turn_id, owner_id="another-owner") == "lost"
                if fault == "rollback":
                    assert await ctx.sessions.rollback_turn_resume_generation(
                        turn_id=worker.turn_id, expected_execution_generation=worker.generation, owner_id=worker.owner_id
                    )
                    assert (await ctx.sessions.get_turn(worker.turn_id)).status == "clarification_pending"
                    assert await ctx.sessions.is_turn_resume_ready(worker.turn_id)
                    entered.append((worker, reservation))
                    raise EntryProbeFinished
                if fault is not None:
                    table, field = {
                        "expired": ("chat_turn_spawn_dispatch", "lease_expires_at"),
                        "stopped": ("chat_turn_spawn_dispatch", "cancel_requested_at"),
                        "deleted": ("sessions", "deleted_at"),
                    }[fault]
                    key = "turn_id" if table == "chat_turn_spawn_dispatch" else "id"
                    identity = worker.turn_id if key == "turn_id" else worker.session_id
                    async with open_shared_ctx(ctx.path) as db:
                        await db.execute(f"UPDATE {table} SET {field}='2000-01-01' WHERE {key}=?", (identity,))
                        await db.commit()
                    with pytest.raises(ReferenceExecutionUnavailableError):
                        await ctx.sessions.require_legacy_chat_execution(turn_id=worker.turn_id, document_worker=worker, document_resume=reservation)
                    entered.append((worker, reservation))
                    raise EntryProbeFinished
                entered.append((worker, reservation))
            return result

        with monkeypatch.context() as local:
            local.setattr(TurnLogStore, "record", record_then_enter)
            result = await original_claim(ctx, native, outcome, *args, **kwargs)
        worker, reservation = entered[-1]
        assert await ctx.sessions.heartbeat_chat_spawn_dispatch(turn_id=worker.turn_id, owner_id=worker.owner_id) == "renewed"
        assert await ctx.sessions.heartbeat_chat_spawn_dispatch(turn_id=worker.turn_id, owner_id=worker.owner_id, settling=True) == "renewed"
        with pytest.raises(ReferenceExecutionUnavailableError):
            await ctx.sessions.require_legacy_chat_execution(turn_id=worker.turn_id, document_worker=worker, document_resume=reservation)
        return result

    monkeypatch.setattr(callers, "claim_resume", claim_with_entry)
    try:
        await callers.test_exact_mcp_obligation_precedes_native_job_and_original_question_resume(
            tmp_path, monkeypatch, invoke_required=True, final_reject=False, pause_fault=None, shared_tail=None
        )
    except EntryProbeFinished:
        assert fault is not None
    else:
        assert fault is None
    assert len(entered) == 1
