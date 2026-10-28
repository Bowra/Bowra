"""Native review opening uses real domain permissions, leases and durable events."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.document_chat_continuation import DocumentTurnOwner
from bowhead_core.documents.canvas_open import DocumentDraftOpen, build_document_draft_open_in_tx, validate_document_draft_open_in_tx
from bowhead_core.documents.continuation import DocumentPlanningReview
from bowhead_core.documents.executor import WorkerResult
from bowhead_core.documents.request import DocumentRequestError
from bowhead_core.events import TurnEventBus
from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore
from test_document_delivery import _review
from test_document_planning_continuation import planning
from test_document_store import SCOPE
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db

pytestmark = pytest.mark.integration


async def ready(ctx):
    case = await planning(ctx)
    await case.delivery.transition(scope=SCOPE, job_id=case.job_id, owner=case.worker.owner_id, state="rendering")
    await case.delivery.save_draft(
        scope=SCOPE,
        job_id=case.job_id,
        owner=case.worker.owner_id,
        result=WorkerResult(
            {"kind": "draft", "manifest": {"semantic_status": "unapproved"}, "preview": {"native_powerpoint_certified": False}},
            {"candidate.pptx": b"original candidate", "candidate.pdf": b"%PDF-original", "page-1.png": b"preview"},
            case.snapshot.image_id,
        ),
    )
    value = await case.delivery.get(scope=SCOPE, job_id=case.job_id)
    return case, DocumentPlanningReview(case.job_id, value["draft_digest"])


async def test_native_open_persists_exact_pointer_before_live_without_publication(ctx):
    case, review = await ready(ctx)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN")
        event = await build_document_draft_open_in_tx(db, worker=case.worker, review=review)
    assert event.persist_before_delivery
    store = TurnLogStore(str(ctx.path))
    bus = TurnEventBus(log_sink=OrderedTurnLogSink(store))
    assert await bus.emit(event)
    rows = await store.list_events(case.worker.turn_id)
    row = rows[-1]
    assert row.event_type == "document.draft_open"
    live = json.loads(event.to_sse()["data"])
    assert live == row.payload
    assert DocumentDraftOpen.model_validate({key: value for key, value in row.payload.items() if key not in {"seq", "evt_seq"}}).job_id == case.job_id
    assert (await case.delivery.get(scope=SCOPE, job_id=case.job_id))["state"] == "review"
    assert "draft" not in row.payload and "title" not in row.payload


@pytest.mark.parametrize("failure", ["lease", "generation", "scope", "digest", "source", "legacy", "permission", "model"])
async def test_native_open_rejects_wrong_or_unavailable_original_authority(ctx, failure, monkeypatch):
    if failure == "legacy":
        delivery, job_id, value, _ = await _review(ctx)
        worker = DocumentTurnOwner(**SCOPE.model_dump(), session_id="s_doc", turn_id="t_doc", generation=1, owner_id="legacy-worker")
        assert await ctx.sessions.claim_chat_spawn_dispatch_for_worker(turn_id="t_doc", owner_id=worker.owner_id, lease_seconds=600) == "claimed"
        case = SimpleNamespace(worker=worker, job_id=job_id, delivery=delivery)
        review = DocumentPlanningReview(job_id, value["draft_digest"])
    else:
        case, review = await ready(ctx)
    worker = case.worker
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        if failure == "lease":
            await db.execute("UPDATE chat_turn_spawn_dispatch SET cancel_requested_at='2026-10-04' WHERE turn_id='t_doc'")
        elif failure == "generation":
            worker = worker.model_copy(update={"generation": 2})
        elif failure == "scope":
            worker = worker.model_copy(update={"session_id": "s_other"})
        elif failure == "digest":
            review = DocumentPlanningReview(case.job_id, "f" * 64)
        elif failure == "source":
            monkeypatch.setattr("bowhead_core.documents.canvas_open._read_chat_turn_user_source_in_tx", AsyncMock(return_value=None))
        elif failure == "permission":
            await db.execute("DELETE FROM workspace_members WHERE user_id='u_doc' AND workspace_id='ws_doc'")
        elif failure == "model":
            review = {"kind": "review", "job_id": case.job_id, "draft_digest": review.draft_digest}
        with pytest.raises(DocumentRequestError):
            await build_document_draft_open_in_tx(db, worker=worker, review=review)


async def test_replay_read_does_not_require_or_reactivate_a_completed_worker(ctx):
    case, review = await ready(ctx)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        event = await build_document_draft_open_in_tx(db, worker=case.worker, review=review)
        await db.execute("UPDATE turns SET status='completed' WHERE id='t_doc'")
        pointer = DocumentDraftOpen.model_validate(event.payload)
        await validate_document_draft_open_in_tx(db, pointer=pointer)
        with pytest.raises(DocumentRequestError):
            await build_document_draft_open_in_tx(db, worker=case.worker, review=review)
        await db.execute("DELETE FROM workspace_members WHERE user_id='u_doc' AND workspace_id='ws_doc'")
        with pytest.raises(DocumentRequestError):
            await validate_document_draft_open_in_tx(db, pointer=pointer)
