"""Content-free cleanup of the exact retained shared document pause."""

from pathlib import Path

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.documents.continuation import invalidate_document_pause_in_tx
from bowhead_core.documents.delivery import _now
from bowhead_core.documents.executor import WorkerResult
from bowhead_core.documents.request import DocumentRequestError
from test_document_attempt_consumption import associate, consume, ready
from test_document_clarification_lifecycle import pause, prepare, rows
from test_document_planning_continuation import checkpoint
from test_document_store import SCOPE
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db

pytestmark = pytest.mark.integration


async def invalidate(db, card, case):
    return await invalidate_document_pause_in_tx(db, card_id=card.request_id, turn_id=case.worker.turn_id, now=_now())


@pytest.mark.parametrize("change", ["none", "revoked", "model_changed"])
async def test_invalidation_preserves_usage_inputs_and_original_identity(ctx, monkeypatch, change):
    case = await checkpoint(ctx)
    _, card = await pause(ctx, case)
    tables = (
        "document_jobs",
        "document_turn_dispatches",
        "document_planning_attempts",
        "document_input_copies",
        "document_model_calls",
        "document_usage_receipts",
        "document_physical_receipt_links",
        "llm_usage_events",
        "credit_ledger",
        "clarification_requests",
        "clarification_document_continuations",
    )
    before = {table: await rows(ctx, f"SELECT * FROM {table}") for table in tables}
    files = {path: Path(path).read_bytes() for (path,) in await rows(ctx, "SELECT path FROM document_input_copies")}
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        if change == "revoked":
            await db.execute("UPDATE workspace_members SET role='viewer'")
        elif change == "model_changed":
            await db.execute("UPDATE teams SET llm_config_version=llm_config_version+1")
        assert await invalidate(db, card, case)
        assert not await invalidate(db, card, case)
        assert db.in_transaction
        await db.commit()
    view = await case.delivery.get(scope=SCOPE, job_id=case.job_id)
    assert (view["state"], view["error_code"]) == ("cancelled", "clarification_resume_unavailable")
    assert {table: await rows(ctx, f"SELECT * FROM {table}") for table in tables} == before
    assert {path: Path(path).read_bytes() for path in files} == files


async def test_invalidation_rolls_back_with_its_callers_turn_update(ctx):
    case = await checkpoint(ctx)
    _, card = await pause(ctx, case)
    async with open_shared_ctx(ctx.path) as db:
        with pytest.raises(DocumentRequestError, match="document_execution_fenced"):
            await invalidate(db, card, case)
        await db.execute("BEGIN IMMEDIATE")
        assert await invalidate(db, card, case)
        await db.execute("UPDATE turns SET status='failed' WHERE id=?", (case.worker.turn_id,))
        await db.rollback()
    assert await rows(ctx, "SELECT state,error_code FROM document_executions") == [("question", None)]
    assert await rows(ctx, "SELECT status FROM turns WHERE id=?", (case.worker.turn_id,)) == [("clarification_pending",)]


@pytest.mark.parametrize("boundary", ["live", "expired", "cancelled", "failed", "aborted", "foreign_owner", "foreign_generation"])
async def test_original_producer_lease_and_terminal_turn_boundary(ctx, boundary):
    case = await checkpoint(ctx)
    _, card = await prepare(ctx, case)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        if boundary == "expired":
            await db.execute("UPDATE chat_turn_spawn_dispatch SET lease_expires_at='2020-01-01'")
        elif boundary in {"cancelled", "failed", "aborted"}:
            await db.execute("UPDATE turns SET status=? WHERE id=?", (boundary, case.worker.turn_id))
        elif boundary in {"foreign_owner", "foreign_generation"}:
            await db.execute("UPDATE turns SET status='cancelled' WHERE id=?", (case.worker.turn_id,))
            if boundary == "foreign_owner":
                await db.execute("UPDATE chat_turn_spawn_dispatch SET owner_id='another-worker'")
            else:
                await db.execute("UPDATE chat_turn_spawn_dispatch SET lease_execution_generation=2")
        assert await invalidate(db, card, case) is (boundary in {"expired", "cancelled", "failed", "aborted"})
        await db.commit()


@pytest.mark.parametrize("wrong", ["card", "turn"])
async def test_other_pointer_cannot_cancel_original_question(ctx, wrong):
    case = await checkpoint(ctx)
    _, card = await pause(ctx, case)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        assert not await invalidate_document_pause_in_tx(
            db,
            card_id="another-card" if wrong == "card" else card.request_id,
            turn_id="another-turn" if wrong == "turn" else case.worker.turn_id,
            now=_now(),
        )
        await db.commit()
    assert (await case.delivery.get(scope=SCOPE, job_id=case.job_id))["state"] == "question"


@pytest.mark.parametrize("boundary", ["reserved", "consumed", "review", "published"])
async def test_resumed_attempt_and_candidate_are_never_overwritten(ctx, boundary):
    case = await ready(ctx)
    if boundary != "reserved":
        async with open_shared_ctx(ctx.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await consume(db, case)
            await associate(db, case)
            await db.commit()
    if boundary in {"review", "published"}:
        owner = case.resumed_worker.owner_id
        await case.delivery.transition(scope=SCOPE, job_id=case.job_id, owner=owner, state="rendering")
        await case.delivery.save_draft(
            scope=SCOPE,
            job_id=case.job_id,
            owner=owner,
            result=WorkerResult(
                {"kind": "draft", "manifest": {"semantic_status": "unapproved"}},
                {"candidate.pptx": b"external worker candidate", "candidate.pdf": b"%PDF-external worker fixture"},
                case.snapshot.image_id,
            ),
        )
        if boundary == "published":
            draft = await case.delivery.get(scope=SCOPE, job_id=case.job_id)
            await case.delivery.confirm(scope=SCOPE, job_id=case.job_id, command_id="confirm", draft_digest=draft["draft_digest"])
    before = await rows(ctx, "SELECT * FROM document_executions")
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        assert not await invalidate(db, case.card, case)
        await db.commit()
    assert await rows(ctx, "SELECT * FROM document_executions") == before
