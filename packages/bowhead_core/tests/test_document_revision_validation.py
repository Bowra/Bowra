"""Real read-only validation of an exact draft inside Chat admission."""

from dataclasses import replace

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.document_revision_target import DocumentRevisionTarget
from bowhead_core.documents.request import DocumentRequestError
from bowhead_core.documents.revision import validate_revision_target_in_tx
from test_document_delivery import _review
from test_document_store import SCOPE
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db

pytestmark = pytest.mark.integration
INSTRUCTION = "Change the title to Q3; preserve all other content."


def revision_source(ctx, job_id, digest, *, index="one"):
    return replace(
        ctx.source,
        turn_id=f"t_revision_{index}",
        user_text=INSTRUCTION,
        attachment_ids=(),
        attachment_refs=(),
        document_revision_target=DocumentRevisionTarget(
            kind="document_revision", version=1, session_id=ctx.source.session_id, job_id=job_id, draft_digest=digest
        ),
    )


@pytest.mark.parametrize(
    "change", ["digest", "owner", "session", "space", "viewer", "revoked", "deleted_session", "published", "attachments", "missing_bytes"]
)
async def test_revision_validation_denies_stale_or_unauthorized_target_without_writes(ctx, change):
    delivery, parent, draft, _ = await _review(ctx)
    source = revision_source(ctx, parent, draft["draft_digest"])
    if change == "digest":
        source = replace(source, document_revision_target=source.document_revision_target.model_copy(update={"draft_digest": "0" * 64}))
    elif change == "owner":
        source = replace(source, user_id="u_other")
    elif change == "session":
        await ctx.sessions.ensure_session(session_id="s_other", workspace_id=SCOPE.workspace_id, user_id=SCOPE.user_id)
        source = replace(
            source, session_id="s_other", document_revision_target=source.document_revision_target.model_copy(update={"session_id": "s_other"})
        )
    elif change == "space":
        source = replace(source, workspace_id="ws_other")
    elif change == "attachments":
        source = replace(source, attachment_ids=(ctx.artifact.id,), attachment_refs=({"fileId": ctx.artifact.id},))
    elif change == "published":
        await delivery.confirm(scope=SCOPE, job_id=parent, command_id="confirm-first", draft_digest=draft["draft_digest"])
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        if change == "viewer":
            await db.execute("UPDATE workspace_members SET role='viewer' WHERE workspace_id=? AND user_id=?", (SCOPE.workspace_id, SCOPE.user_id))
        elif change == "revoked":
            await db.execute("UPDATE team_members SET status='removed' WHERE user_id=?", (SCOPE.user_id,))
        elif change == "deleted_session":
            await db.execute("UPDATE sessions SET deleted_at='2026-10-03' WHERE id=?", (source.session_id,))
        elif change == "missing_bytes":
            # Read-only preflight rejects inconsistent retained metadata without blob I/O.
            await db.execute("UPDATE artifacts SET status='missing' WHERE id=?", (draft["draft"]["files"]["candidate.pptx"]["artifact_id"],))
        with pytest.raises(DocumentRequestError):
            await validate_revision_target_in_tx(db, source=source)
        async with db.execute("SELECT count(*) FROM document_jobs") as cursor:
            assert (await cursor.fetchone())[0] == 1
        await db.rollback()


async def test_validator_keeps_caller_transaction_and_does_not_cancel_parent(ctx):
    delivery, parent, draft, _ = await _review(ctx)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("UPDATE sessions SET title='Uncommitted revision' WHERE id='s_doc'")
        await validate_revision_target_in_tx(db, source=revision_source(ctx, parent, draft["draft_digest"]))
        assert db.in_transaction
        await db.rollback()
    assert (await delivery.get(scope=SCOPE, job_id=parent))["state"] == "review"
    async with open_shared_ctx(ctx.path) as db, db.execute("SELECT title FROM sessions WHERE id='s_doc'") as cursor:
        assert (await cursor.fetchone())[0] != "Uncommitted revision"


async def test_revision_admission_does_not_change_ordinary_source(ctx):
    _, parent, draft, _ = await _review(ctx)
    async with open_shared_ctx(ctx.path) as db:
        await validate_revision_target_in_tx(db, source=ctx.source)
        await validate_revision_target_in_tx(db, source=revision_source(ctx, parent, draft["draft_digest"]))
