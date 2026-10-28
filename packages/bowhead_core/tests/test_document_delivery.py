"""Real migrated persistence and blobs; worker rendering is an external boundary."""

import asyncio

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.documents.delivery import DocumentDeliveryStore
from bowhead_core.documents.executor import WorkerResult
from bowhead_core.documents.inputs import DocumentInputStore
from bowhead_core.documents.request import DocumentRequestError
from test_document_store import SCOPE
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db

pytestmark = pytest.mark.integration


async def _review(ctx, *, record=None):
    receipt = await ctx.submit()
    await DocumentInputStore(ctx.artifacts).freeze(scope=SCOPE, job_id=receipt.job_id)
    delivery = DocumentDeliveryStore(ctx.artifacts)
    owner = await delivery.claim(scope=SCOPE, job_id=receipt.job_id)
    await delivery.transition(scope=SCOPE, job_id=receipt.job_id, owner=owner, state="planning", image_id="sha256:test")
    await delivery.transition(scope=SCOPE, job_id=receipt.job_id, owner=owner, state="rendering")
    result = WorkerResult(
        record
        if record is not None
        else {"kind": "draft", "manifest": {"semantic_status": "unapproved"}, "preview": {"native_powerpoint_certified": False}},
        {"candidate.pptx": b"external worker candidate", "candidate.pdf": b"%PDF-external worker fixture", "page-1.png": b"external preview fixture"},
        "sha256:test",
    )
    await delivery.save_draft(scope=SCOPE, job_id=receipt.job_id, owner=owner, result=result)
    return delivery, receipt.job_id, await delivery.get(scope=SCOPE, job_id=receipt.job_id), result


async def test_draft_is_private_until_exact_confirmation_then_downloads_same_bytes(ctx):
    delivery, job_id, draft, result = await _review(ctx)
    assert draft["state"] == "review"
    ids = draft["draft"]["files"]
    for file in ids.values():
        assert await ctx.artifacts.get(file["artifact_id"]) is None
    with pytest.raises(DocumentRequestError, match="document_not_found"):
        await delivery.read_file(scope=SCOPE, job_id=job_id, name="candidate.pptx", preview=False)
    assert (await delivery.read_file(scope=SCOPE, job_id=job_id, name="page-1.png", preview=True))[0] == result.files["page-1.png"]
    published = await delivery.confirm(scope=SCOPE, job_id=job_id, command_id="confirm", draft_digest=draft["draft_digest"])
    assert published["state"] == "published"
    for name in ("candidate.pptx", "candidate.pdf"):
        assert (await delivery.read_file(scope=SCOPE, job_id=job_id, name=name, preview=False))[0] == result.files[name]
        assert (await ctx.artifacts.get(ids[name]["artifact_id"])).kind == "deliverable"
    assert await ctx.artifacts.get(ids["page-1.png"]["artifact_id"]) is None
    assert await delivery.confirm(scope=SCOPE, job_id=job_id, command_id="confirm", draft_digest=draft["draft_digest"]) == published


async def test_stale_confirmation_cannot_publish(ctx):
    delivery, job_id, _, _ = await _review(ctx)
    with pytest.raises(DocumentRequestError, match="document_revision_conflict"):
        await delivery.confirm(scope=SCOPE, job_id=job_id, command_id="confirm", draft_digest="0" * 64)
    assert (await delivery.get(scope=SCOPE, job_id=job_id))["state"] == "review"


async def test_cancel_fences_confirmation_and_private_preview(ctx):
    delivery, job_id, draft, _ = await _review(ctx)
    await ctx.store.cancel(scope=SCOPE, job_id=job_id, command_id="cancel", expected_proposal_revision=1)
    assert (await delivery.get(scope=SCOPE, job_id=job_id))["state"] == "cancelled"
    with pytest.raises(DocumentRequestError):
        await delivery.confirm(scope=SCOPE, job_id=job_id, command_id="confirm", draft_digest=draft["draft_digest"])
    with pytest.raises(DocumentRequestError):
        await delivery.read_file(scope=SCOPE, job_id=job_id, name="page-1.png", preview=True)


async def test_two_confirmations_publish_one_file_pair(ctx):
    delivery, job_id, draft, _ = await _review(ctx)
    outcomes = await asyncio.gather(
        *(delivery.confirm(scope=SCOPE, job_id=job_id, command_id=command, draft_digest=draft["draft_digest"]) for command in ("one", "two"))
    )
    assert outcomes[0] == outcomes[1]
    async with open_shared_ctx(ctx.path) as db, db.execute("SELECT count(*) FROM document_confirmations WHERE job_id=?", (job_id,)) as cursor:
        assert (await cursor.fetchone())[0] == 1


async def test_current_owner_and_space_membership_are_required(ctx):
    delivery, job_id, draft, _ = await _review(ctx)
    with pytest.raises(DocumentRequestError, match="document_not_found"):
        await delivery.get(scope=SCOPE.model_copy(update={"user_id": "u_other"}), job_id=job_id)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("DELETE FROM workspace_members WHERE workspace_id=? AND user_id=?", (SCOPE.workspace_id, SCOPE.user_id))
        await db.commit()
    with pytest.raises(DocumentRequestError, match="document_not_found"):
        await delivery.confirm(scope=SCOPE, job_id=job_id, command_id="confirm", draft_digest=draft["draft_digest"])


async def test_hash_mismatch_blocks_confirmation(ctx):
    delivery, job_id, draft, _ = await _review(ctx)
    async with (
        open_shared_ctx(ctx.path) as db,
        db.execute("SELECT path FROM document_draft_files WHERE job_id=? AND name='candidate.pptx'", (job_id,)) as cursor,
    ):
        path = (await cursor.fetchone())[0]
    # Simulate storage corruption, not a supported application mutation.
    from pathlib import Path

    Path(path).write_bytes(b"changed after preview")
    with pytest.raises(DocumentRequestError, match="document_draft_hash_mismatch"):
        await delivery.confirm(scope=SCOPE, job_id=job_id, command_id="confirm", draft_digest=draft["draft_digest"])


async def test_viewer_transition_keeps_history_but_disables_publication(ctx):
    delivery, job_id, draft, _ = await _review(ctx)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("UPDATE workspace_members SET role='viewer' WHERE workspace_id=? AND user_id=?", (SCOPE.workspace_id, SCOPE.user_id))
        await db.commit()
    current = await delivery.get(scope=SCOPE, job_id=job_id)
    assert current["state"] == "review" and current["can_confirm"] is False
    with pytest.raises(DocumentRequestError, match="document_not_found"):
        await delivery.confirm(scope=SCOPE, job_id=job_id, command_id="confirm", draft_digest=draft["draft_digest"])
    await ctx.store.cancel(scope=SCOPE, job_id=job_id, command_id="cancel", expected_proposal_revision=1)
    assert (await delivery.get(scope=SCOPE, job_id=job_id))["state"] == "cancelled"


async def test_cancelled_private_outputs_are_purged_after_grace_with_identity_retained(ctx):
    from pathlib import Path

    delivery, job_id, _, _ = await _review(ctx)
    await ctx.store.cancel(scope=SCOPE, job_id=job_id, command_id="cancel", expected_proposal_revision=1)
    await delivery.cleanup()
    async with open_shared_ctx(ctx.path) as db:
        async with db.execute("SELECT path FROM document_draft_files WHERE job_id=?", (job_id,)) as cursor:
            paths = [Path(row[0]) for row in await cursor.fetchall()]
        assert all(path.exists() for path in paths)
        await db.execute("UPDATE document_executions SET updated_at='2000-01-01T00:00:00Z' WHERE job_id=?", (job_id,))
        await db.commit()
    await delivery.cleanup()
    assert all(not path.exists() for path in paths)
    async with open_shared_ctx(ctx.path) as db, db.execute("SELECT count(*) FROM document_draft_files WHERE job_id=?", (job_id,)) as cursor:
        assert (await cursor.fetchone())[0] == 3
