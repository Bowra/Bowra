"""Current authorization, fair queue scans and canonical question successors."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.documents.delivery import DocumentDeliveryStore
from bowhead_core.documents.inputs import DocumentInputStore
from bowhead_core.documents.request import DocumentRequestError, UserQuote
from bowhead_gateway.document_service import DocumentService
from test_document_capability import runtime
from test_document_store import SCOPE, _save_source
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db

pytestmark = pytest.mark.integration


async def turn(ctx, index, *, scope=SCOPE, text=None, session_id=None, attach=True):
    session_id, turn_id = session_id or f"s_recovery_{index}", f"t_recovery_{index}"
    await ctx.sessions.ensure_session(session_id=session_id, workspace_id=scope.workspace_id, user_id=scope.user_id)
    await ctx.sessions.record_turn(turn_id=turn_id, session_id=session_id, user_text=text or ctx.source.user_text)
    if attach:
        await ctx.sessions.record_turn_attachments(turn_id=turn_id, artifact_ids=[ctx.artifact.id])
        await ctx.sessions.pin_session_attachments(session_id=session_id, artifact_ids=[ctx.artifact.id])
    await _save_source(
        ctx.path,
        replace(
            ctx.source,
            session_id=session_id,
            turn_id=turn_id,
            workspace_id=scope.workspace_id,
            user_text=text or ctx.source.user_text,
            attachment_ids=(ctx.artifact.id,) if attach else (),
            attachment_refs=({"fileId": ctx.artifact.id},) if attach else (),
        ),
    )
    return turn_id


async def question(ctx):
    parent = await ctx.submit()
    await DocumentInputStore(ctx.artifacts).freeze(scope=SCOPE, job_id=parent.job_id)
    delivery = DocumentDeliveryStore(ctx.artifacts)
    owner = await delivery.claim(scope=SCOPE, job_id=parent.job_id)
    await delivery.transition(
        scope=SCOPE,
        job_id=parent.job_id,
        owner=owner,
        state="planning",
        context_json=json.dumps({"payload": json.dumps({"instruction": ctx.source.user_text})}),
    )
    await delivery.transition(scope=SCOPE, job_id=parent.job_id, owner=owner, state="rendering")
    await delivery.transition(scope=SCOPE, job_id=parent.job_id, owner=owner, state="question", question="Which comparison period?")
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("UPDATE turns SET status='completed' WHERE id='t_doc'")
        await db.commit()
    await ctx.sessions.finalize_turn(session_id="s_doc", current_turn_id="t_doc", next_turn_id=None)
    return parent.job_id


async def test_revoked_queue_heads_do_not_starve_valid_job(ctx, monkeypatch):
    healthy = None
    for index in range(11):
        scope = SCOPE if index < 10 else SCOPE.model_copy(update={"workspace_id": "ws_other"})
        turn_id = await turn(ctx, index, scope=scope)
        receipt = await ctx.store.submit_from_turn(
            scope=scope,
            turn_id=turn_id,
            command_id=f"recovery-{index}",
            proposal=ctx.proposal,
            runtime=await runtime(ctx, turn_id=turn_id, scope=scope),
        )
        if index == 10:
            healthy = receipt.job_id
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("UPDATE workspace_members SET status='removed' WHERE workspace_id='ws_doc' AND user_id='u_doc'")
        await db.commit()
    service = DocumentService(SimpleNamespace(artifact_store=ctx.artifacts))
    # The external executor is intentionally unavailable: reaching the healthy
    # job must still claim and terminalize it, instead of leaving it behind page one.
    monkeypatch.delenv("BOWHEAD_SKILL_WORKDIR_ROOT", raising=False)
    cycles = 0

    async def stop(seconds):
        nonlocal cycles
        cycles += 1
        if cycles == 2:
            raise asyncio.CancelledError()

    monkeypatch.setattr("bowhead_gateway.document_service.anyio.sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await service.run()
    saved = await service.delivery.get(scope=SCOPE.model_copy(update={"workspace_id": "ws_other"}), job_id=healthy)
    assert saved["state"] == "failed" and saved["error_code"] == "document_runtime_unavailable"


async def test_answer_inherits_request_inputs_and_has_one_exact_successor(ctx):
    parent = await question(ctx)
    turn_id = await turn(ctx, "answer", text="Use the same quarter last year.", session_id="s_doc", attach=False)
    listed = await ctx.store.questions_from_turn(scope=SCOPE, turn_id=turn_id)
    assert listed == [{"job_id": parent, "question": "Which comparison period?"}]
    arguments = {
        "scope": SCOPE,
        "turn_id": turn_id,
        "command_id": "answer",
        "parent_job_id": parent,
        "evidence": UserQuote(text="Use the same quarter last year."),
        "runtime": await runtime(ctx, turn_id=turn_id),
    }
    one, two = await asyncio.gather(ctx.store.answer_from_turn(**arguments), ctx.store.answer_from_turn(**arguments))
    assert one == two
    delivery = DocumentDeliveryStore(ctx.artifacts)
    assert (await delivery.get(scope=SCOPE, job_id=parent))["state"] == "cancelled"
    successor = await delivery.get(scope=SCOPE, job_id=one.job_id)
    assert ctx.source.user_text in successor["request_text"]
    assert "Which comparison period?" in successor["request_text"]
    assert "Use the same quarter last year." in successor["request_text"]
    inputs = DocumentInputStore(ctx.artifacts)
    frozen = await inputs.freeze(scope=SCOPE, job_id=one.job_id)
    assert len(frozen) == 1 and frozen[0].source_artifact_id == ctx.artifact.id
    assert await inputs.read(scope=SCOPE, job_id=one.job_id, artifact_id=frozen[0].artifact_id) == b"synthetic metadata-only input"
    assert await ctx.store.questions_from_turn(scope=SCOPE, turn_id=turn_id) == []
    with pytest.raises(DocumentRequestError, match="document_command_conflict|command_conflict"):
        await ctx.store.answer_from_turn(**{**arguments, "evidence": UserQuote(text="same quarter")})


@pytest.mark.parametrize("change", ["cancel", "other_session", "revoked", "invented_quote", "other_owner"])
async def test_question_answer_rechecks_current_identity_and_context(ctx, change):
    parent = await question(ctx)
    turn_id = await turn(ctx, "answer", text="Last year.", session_id="s_elsewhere" if change == "other_session" else "s_doc", attach=False)
    admission = await runtime(ctx, turn_id=turn_id)
    if change == "cancel":
        await ctx.store.cancel(scope=SCOPE, job_id=parent, command_id="cancel", expected_proposal_revision=1)
    if change == "revoked":
        async with open_shared_ctx(ctx.path) as db:
            await db.execute("UPDATE workspace_members SET role='viewer' WHERE workspace_id=? AND user_id=?", (SCOPE.workspace_id, SCOPE.user_id))
            await db.commit()
    with pytest.raises(DocumentRequestError):
        await ctx.store.answer_from_turn(
            scope=SCOPE.model_copy(update={"user_id": "u_other"}) if change == "other_owner" else SCOPE,
            turn_id=turn_id,
            command_id="answer",
            parent_job_id=parent,
            runtime=admission,
            evidence=UserQuote(text="Invented answer" if change == "invented_quote" else "Last year."),
        )
    async with open_shared_ctx(ctx.path) as db, db.execute("SELECT count(*) FROM document_continuations") as cursor:
        assert (await cursor.fetchone())[0] == 0


async def test_answer_rejects_replaced_source_bytes(ctx):
    from pathlib import Path

    parent = await question(ctx)
    turn_id = await turn(ctx, "answer", text="Last year.", session_id="s_doc", attach=False)
    answer = await ctx.store.answer_from_turn(
        scope=SCOPE,
        turn_id=turn_id,
        command_id="answer",
        parent_job_id=parent,
        evidence=UserQuote(text="Last year."),
        runtime=await runtime(ctx, turn_id=turn_id),
    )
    Path(ctx.artifact.path).write_bytes(b"replaced original file")
    with pytest.raises(DocumentRequestError, match="document_input_changed"):
        await DocumentInputStore(ctx.artifacts).freeze(scope=SCOPE, job_id=answer.job_id)


async def test_question_successor_failure_preserves_answerable_parent(ctx):
    import aiosqlite

    parent = await question(ctx)
    turn_id = await turn(ctx, "answer", text="Last year.", session_id="s_doc", attach=False)
    admission = await runtime(ctx, turn_id=turn_id)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("CREATE TRIGGER fail_successor BEFORE INSERT ON document_dispatches BEGIN SELECT RAISE(ABORT,'test failure'); END")
        await db.commit()
    with pytest.raises(aiosqlite.IntegrityError, match="test failure"):
        await ctx.store.answer_from_turn(
            scope=SCOPE, turn_id=turn_id, command_id="answer", parent_job_id=parent, evidence=UserQuote(text="Last year."), runtime=admission
        )
    assert await ctx.store.questions_from_turn(scope=SCOPE, turn_id=turn_id) == [{"job_id": parent, "question": "Which comparison period?"}]
    async with open_shared_ctx(ctx.path) as db:
        for table, expected in (("document_continuations", 0), ("document_jobs", 1), ("turn_capability_execution", 0)):
            async with db.execute(f"SELECT count(*) FROM {table}") as cursor:
                assert (await cursor.fetchone())[0] == expected
