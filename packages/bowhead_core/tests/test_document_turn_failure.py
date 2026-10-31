"""Native pre-execution failures use real admission and migrated SQLite guards."""

from types import SimpleNamespace

import aiosqlite
import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.clarification_store import ClarificationCandidate
from bowhead_core.document_chat_continuation import DocumentTurnOwner
from bowhead_core.documents.turn_runner import fail_document_turn
from bowhead_core.turn_log_store import TurnLogStore
from test_document_store import SCOPE, Context
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db
from test_document_turn_runner import claim_resume, start
from test_document_turn_runner import native as native

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def worker_for(native: SimpleNamespace) -> DocumentTurnOwner:
    return DocumentTurnOwner(
        **SCOPE.model_dump(),
        session_id="s_doc",
        turn_id="t_doc",
        generation=native.context.execution_generation,
        owner_id=native.context.execution_owner_id,
    )


async def executions(ctx: Context) -> list:
    async with open_shared_ctx(ctx.path) as db, db.execute("SELECT * FROM document_executions ORDER BY job_id") as cursor:
        return await cursor.fetchall()


@pytest.mark.parametrize("boundary", ["normal", "revoked", "flag_off"])
async def test_pre_execution_failure_is_durable_and_idempotent(
    ctx: Context, native: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    receipt = await native.service.submit(native.context, ctx.proposal)
    if boundary == "flag_off":
        monkeypatch.setenv("FEATURE_DOCUMENT_CHAT_CONTINUATION", "0")
    elif boundary == "revoked":
        async with open_shared_ctx(ctx.path) as db:
            await db.execute("DELETE FROM workspace_members WHERE workspace_id=? AND user_id=?", (SCOPE.workspace_id, SCOPE.user_id))
            await db.commit()
    result = await fail_document_turn(native.service.delivery, worker_for(native), receipt.job_id, "document_execution_failed")
    assert result.kind == "failed" and result.error_code == "document_execution_failed"
    first = await executions(ctx)
    assert len(first) == 1
    async with open_shared_ctx(ctx.path) as db:
        async with db.execute("SELECT state,error_code,settled_credits FROM document_executions WHERE job_id=?", (receipt.job_id,)) as cursor:
            assert await cursor.fetchone() == ("failed", "document_execution_failed", 0)
        for table in (
            "document_planning_attempts",
            "document_input_copies",
            "document_model_calls",
            "document_draft_files",
            "document_confirmations",
        ):
            async with db.execute(f"SELECT count(*) FROM {table} WHERE job_id=?", (receipt.job_id,)) as cursor:
                assert (await cursor.fetchone())[0] == 0
    await fail_document_turn(native.service.delivery, worker_for(native), receipt.job_id, "document_runtime_unavailable")
    assert await executions(ctx) == first
    assert not native.calls and not native.worker.runs
    if boundary == "normal":
        saved = await native.service.delivery.get(scope=SCOPE, job_id=receipt.job_id)
        assert saved["state"] == "failed" and saved["error_code"] == "document_execution_failed"


@pytest.mark.parametrize("field", ["team_id", "user_id", "workspace_id", "session_id", "turn_id", "owner_id", "generation"])
async def test_early_failure_cannot_use_another_worker_identity(ctx: Context, native: SimpleNamespace, field: str) -> None:
    receipt = await native.service.submit(native.context, ctx.proposal)
    wrong = worker_for(native).model_copy(update={field: 2 if field == "generation" else "another-identity"})
    await fail_document_turn(native.service.delivery, wrong, receipt.job_id, "document_execution_failed")
    assert await executions(ctx) == []
    assert not native.calls and not native.worker.runs


@pytest.mark.parametrize("cause", ["user_cancel", "session_deleted"])
async def test_cancelled_admission_cannot_be_reopened_by_late_failure(ctx: Context, native: SimpleNamespace, cause: str) -> None:
    receipt = await native.service.submit(native.context, ctx.proposal)
    if cause == "user_cancel":
        await native.service.jobs.cancel(
            scope=SCOPE,
            job_id=receipt.job_id,
            command_id="cancel-before-execution",
            expected_proposal_revision=receipt.proposal_revision,
        )
    else:
        async with open_shared_ctx(ctx.path) as db:
            await db.execute("UPDATE sessions SET deleted_at='2026-10-04T00:00:00Z' WHERE id='s_doc'")
            await db.commit()
    await fail_document_turn(native.service.delivery, worker_for(native), receipt.job_id, "document_execution_failed")
    assert await executions(ctx) == []
    async with open_shared_ctx(ctx.path) as db, db.execute("SELECT status FROM document_jobs WHERE id=?", (receipt.job_id,)) as cursor:
        assert (await cursor.fetchone())[0] == "cancelled"
    if cause == "user_cancel":
        assert (await native.service.delivery.get(scope=SCOPE, job_id=receipt.job_id))["state"] == "cancelled"
    assert not native.calls and not native.worker.runs


async def test_early_failure_terminal_write_rolls_back_its_preparing_insert(ctx: Context, native: SimpleNamespace) -> None:
    receipt = await native.service.submit(native.context, ctx.proposal)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute(
            "CREATE TRIGGER reject_document_failure BEFORE UPDATE OF state ON document_executions "
            "WHEN NEW.state='failed' BEGIN SELECT RAISE(ABORT,'controlled terminal write failure'); END"
        )
        await db.commit()
    with pytest.raises(aiosqlite.IntegrityError, match="controlled terminal write failure"):
        await fail_document_turn(native.service.delivery, worker_for(native), receipt.job_id, "document_execution_failed")
    assert await executions(ctx) == []
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("DROP TRIGGER reject_document_failure")
        await db.commit()
    await fail_document_turn(native.service.delivery, worker_for(native), receipt.job_id, "document_execution_failed")
    assert (await native.service.delivery.get(scope=SCOPE, job_id=receipt.job_id))["state"] == "failed"
    assert not native.calls and not native.worker.runs


@pytest.mark.parametrize("state", ["question", "resumed"])
async def test_late_creation_worker_failure_preserves_a_pause_or_resumed_owner(ctx: Context, native: SimpleNamespace, state: str) -> None:
    original = worker_for(native)
    outcome = await start(ctx, native)
    if state == "resumed":
        await claim_resume(ctx, native, outcome)
    before = await executions(ctx)
    async with open_shared_ctx(ctx.path) as db, db.execute("SELECT * FROM credit_ledger ORDER BY id") as cursor:
        charges = await cursor.fetchall()
    await fail_document_turn(native.service.delivery, original, outcome.job_id, "document_execution_failed")
    assert await executions(ctx) == before
    async with open_shared_ctx(ctx.path) as db, db.execute("SELECT * FROM credit_ledger ORDER BY id") as cursor:
        assert await cursor.fetchall() == charges
    assert len(native.calls) == 1 and native.worker.runs == ["inventory", "apply_verify"]


@pytest.mark.parametrize("history", ["untyped", "legacy"])
async def test_unsupported_ordinary_history_records_a_finite_failure(ctx: Context, native: SimpleNamespace, history: str) -> None:
    context = native.context
    store = context.clarification_store
    question, selected = "Which total should be used?", "Sum: 6000"
    card = await store.put(
        turn_id="t_doc",
        user_id=SCOPE.user_id,
        workspace_id=SCOPE.workspace_id,
        session_id="s_doc",
        token="",
        rationale=question,
        candidates=[ClarificationCandidate(value=selected, label=selected)],
        kind="ambiguous_turn",
        resume_context={"user_text": ctx.source.user_text},
        bounded_question={"generation": 1, "owner_id": context.execution_owner_id, "questions": [{"question": question}]}
        if history == "untyped"
        else None,
    )
    # Retain the actual incomplete answer representation; never repair history
    # or loosen the document consumer to make this failure branch pass.
    assert (await store.resolve_once(card.request_id, chosen_value=selected))[1]
    assert await ctx.sessions.mark_clarification_pending(turn_id="t_doc", intent="ambiguous", error_kind="needs_clarification")
    await TurnLogStore(str(ctx.path)).record(turn_id="t_doc", event_type="done", payload={"turn_id": "t_doc", "status": "clarification_pending"})
    assert await ctx.sessions.mark_chat_spawn_dispatch_terminal(turn_id="t_doc", owner_id=context.execution_owner_id)
    context.execution_generation = await ctx.sessions.advance_turn_execution_generation(turn_id="t_doc", clarification_id=card.request_id)
    assert context.execution_generation == 2
    context.execution_owner_id = "document-after-single-choice"
    assert (
        await ctx.sessions.claim_chat_spawn_dispatch_for_worker(turn_id="t_doc", owner_id=context.execution_owner_id, lease_seconds=600) == "claimed"
    )
    await TurnLogStore(str(ctx.path)).record(turn_id="t_doc", event_type="turn.resumed", payload={"turn_id": "t_doc", "execution_generation": 2})
    result = (await native.registry.execute("template_document_submit", native.arguments, context)).result
    assert result["state"] == "failed", result
    outcome = context.document_turn_state.planning_result
    saved = await native.service.delivery.get(scope=SCOPE, job_id=outcome.job_id)
    assert saved["state"] == "failed" and saved["error_code"] == outcome.error_code == "document_history_unavailable"
    assert not native.calls and not native.worker.runs
    async with open_shared_ctx(ctx.path) as db:
        async with db.execute("SELECT answers_json,chosen_value FROM clarification_requests WHERE request_id=?", (card.request_id,)) as cursor:
            assert await cursor.fetchone() == (None, selected)
        async with db.execute("SELECT count(*) FROM document_planning_attempts WHERE job_id=?", (outcome.job_id,)) as cursor:
            assert (await cursor.fetchone())[0] == 0


@pytest.mark.parametrize(
    ("error_code", "reason", "message"),
    [
        ("document_authoring_script_failed", "document_authoring_script_failed", "editing script failed"),
        ("pptx_slide_identity_invalid", "document_candidate_invalid", "file-structure check"),
        ("pptx_shape_identity_invalid", "document_candidate_invalid", "file-structure check"),
        ("pptx_structure_unsupported", "document_candidate_invalid", "file-structure check"),
        ("document_execution_failed", "document_planning_failed", "could not finish"),
    ],
)
async def test_known_document_failure_survives_original_terminal_persistence(ctx, native, error_code, reason, message):
    import json
    from dataclasses import replace

    from bowhead_core.document_chat_runtime import finish_document_planning
    from bowhead_core.events import TurnEventBus
    from bowhead_core.turn_runner import TurnRunner

    native.context.language_context = replace(native.context.language_context, output_language="en-US")
    receipt = await native.service.submit(native.context, ctx.proposal)
    outcome = await fail_document_turn(native.service.delivery, worker_for(native), receipt.job_id, error_code)
    logs = TurnLogStore(str(ctx.path))
    live = []

    async def sink(event):
        live.append((event.event_type, json.loads(event.to_sse()["data"])))
        return await logs.record(turn_id=event.turn_id, event_type=event.event_type, payload=event.payload)

    bus = TurnEventBus(log_sink=sink, persist_before_live=True)
    runner = TurnRunner(SimpleNamespace(session_store=ctx.sessions))
    bus._pi_persist_turn_state = runner._persist_turn_state
    bus._pi_turn_state = {}
    result = await finish_document_planning(native.context, outcome, bus=bus)
    turn = await ctx.sessions.get_turn("t_doc")
    assert turn.status == "failed" and turn.error_kind == reason
    assert message in result.final_answer == turn.final_answer
    assert error_code not in result.final_answer
    events = await logs.list_events("t_doc")
    done = next(event for event in events if event.event_type == "done")
    assert done.payload["reason_code"] == reason
    assert done.payload["diagnostics"]["retryable"] is False
    assert next(payload for event_type, payload in live if event_type == "done")["diagnostics"] == done.payload["diagnostics"]
    assert not any(event.event_type == "document.draft_open" for event in events)
    saved = await native.service.delivery.get(scope=SCOPE, job_id=receipt.job_id)
    assert saved["state"] == "failed" and saved["error_code"] == error_code
    assert not native.calls and not native.worker.runs
