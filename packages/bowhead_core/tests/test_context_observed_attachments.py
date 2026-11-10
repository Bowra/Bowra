"""Typed directory observations retain disclosure authority without freezing unused bytes."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.attachments_injector import prepare_attachments
from bowhead_core.chat_turn_source import ChatTurnUserSource
from bowhead_core.context_dependencies import (
    ContextSourceValidator,
    PreparedContextSourceValidator,
    capture_execution_dependencies,
    validate_context_dependencies,
)
from bowhead_core.ordinary_lead_checkpoint import OrdinaryLeadRunState
from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable
from bowhead_core.session_store import SessionStore, _insert_chat_turn_user_source_in_tx
from test_agent_collaboration import collab as collab

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize(
    "use,mutation",
    [
        (use, mutation)
        for use in ("legacy", "directory", "content")
        for mutation in ("none", "metadata", "bytes", "revoke", "space")
        if not (use == "legacy" and mutation == "space")
    ],
)
async def test_observed_attachment_proof_checks_exact_content_and_current_directory_authority(collab, use, mutation):
    worker = collab.worker
    store = ArtifactStore(str(collab.path), LocalBackend(collab.path.parent / "observed-files"))
    await store.initialize()
    art = await store.put_bytes(
        content=b"Actual retained evidence.",
        filename="original.txt",
        mime="text/plain",
        created_by=worker.user_id,
        workspace_id=worker.workspace_id,
        session_id=worker.session_id,
        created_by_turn=worker.turn_id,
        bind_chat_upload=True,
    )
    async with open_shared_ctx(collab.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        user_text = (await (await db.execute("SELECT user_text FROM turns WHERE id=?", (worker.turn_id,))).fetchone())[0]
        await _insert_chat_turn_user_source_in_tx(
            db,
            created_at="2026-11-10T00:00:00Z",
            source=ChatTurnUserSource(
                turn_id=worker.turn_id,
                session_id=worker.session_id,
                user_id=worker.user_id,
                workspace_id=worker.workspace_id,
                user_text=user_text,
                attachment_ids=(),
                attachment_refs=(),
                parts=None,
            ),
        )
        await db.commit()
    ctx = SimpleNamespace(
        turn_id=worker.turn_id,
        session_id=worker.session_id,
        user_id=worker.user_id,
        workspace_id=worker.workspace_id,
        session_store=SessionStore(collab.path),
        artifact_store=store,
        knowledge_source_uses={},
        ordinary_lead_state=OrdinaryLeadRunState(observed_inputs=use != "legacy"),
        _psi_pinned_snapshot=[{"artifact_id": art.id, "filename": art.filename, "mime": art.mime, "size": art.size}],
        _psi_parsed_results=await prepare_attachments(store=store, artifact_ids=[art.id]) if use == "content" else [],
    )
    proofs = await capture_execution_dependencies(ctx)
    assert [message.name for message in proofs] == ["context_attachment_dependencies_v1" if use == "legacy" else "context_attachment_dependencies_v2"]
    prepared = PreparedContextSourceValidator(ContextSourceValidator(ctx))
    await prepared.collect(proofs)
    if mutation == "metadata":
        async with open_shared_ctx(collab.path) as db:
            await db.execute("UPDATE artifacts SET filename='renamed.txt' WHERE id=?", (art.id,))
            await db.commit()
    elif mutation == "bytes":
        Path(art.path).write_bytes(b"x" * art.size)
    elif mutation == "revoke":
        await store.soft_delete(art.id, user_id=worker.user_id)
    elif mutation == "space":
        async with open_shared_ctx(collab.path) as db:
            await db.execute("UPDATE artifacts SET workspace_id='ws_other' WHERE id=?", (art.id,))
            await db.commit()
    rejected = mutation in {"revoke", "space"} or mutation == "metadata" and use != "directory" or mutation == "bytes" and use == "content"
    if rejected:
        with pytest.raises(OrdinaryContinuationUnavailable):
            await validate_context_dependencies(ctx, proofs)
        if mutation != "bytes":
            with pytest.raises(OrdinaryContinuationUnavailable):
                await prepared(proofs)
    else:
        await validate_context_dependencies(ctx, proofs)
        await prepared(proofs)


async def test_new_directory_proof_cannot_reinterpret_inherited_legacy_metadata(collab):
    worker = collab.worker
    store = ArtifactStore(str(collab.path), LocalBackend(collab.path.parent / "inherited-directory"))
    await store.initialize()
    art = await store.put_bytes(
        content=b"untouched",
        filename="prior.txt",
        mime="text/plain",
        created_by=worker.user_id,
        workspace_id=worker.workspace_id,
        session_id=worker.session_id,
        created_by_turn=worker.turn_id,
        bind_chat_upload=True,
    )
    source = ChatTurnUserSource(
        turn_id=worker.turn_id,
        session_id=worker.session_id,
        user_id=worker.user_id,
        workspace_id=worker.workspace_id,
        user_text="Inspect",
        attachment_ids=(),
        attachment_refs=(),
        parts=None,
    )
    from bowhead_core.context_attachments import capture_attachment_dependencies, validate_attachment_dependencies

    ctx = SimpleNamespace(
        turn_id=worker.turn_id,
        session_id=worker.session_id,
        user_id=worker.user_id,
        workspace_id=worker.workspace_id,
        session_store=SessionStore(collab.path),
        artifact_store=store,
        ordinary_lead_state=OrdinaryLeadRunState(),
        _psi_pinned_snapshot=[{"artifact_id": art.id, "filename": art.filename, "mime": art.mime, "size": art.size}],
        _psi_parsed_results=[],
    )
    async with open_shared_ctx(collab.path) as db:
        await db.execute("BEGIN")
        legacy = await capture_attachment_dependencies(ctx, db, source)
        ctx.ordinary_lead_state.observed_inputs = True
        current = await capture_attachment_dependencies(ctx, db, source)
        await db.execute("UPDATE artifacts SET filename='changed.txt' WHERE id=?", (art.id,))
        await validate_attachment_dependencies(ctx, db, source, current)
        with pytest.raises(ValueError, match="context_attachment_source_changed"):
            await validate_attachment_dependencies(ctx, db, source, legacy)
        await db.rollback()


async def test_native_read_promotes_selected_metadata_to_byte_dependency(collab):
    from bowhead_core.attachments_injector import PreparedAttachment
    from bowhead_core.context_attachments import capture_attachment_dependencies, validate_attachment_dependencies
    from bowhead_core.ordinary_lead_material import artifact_evidence

    worker = collab.worker
    store = ArtifactStore(str(collab.path), LocalBackend(collab.path.parent / "promoted-input"))
    await store.initialize()
    art = await store.put_bytes(
        content=b"Exact native input.",
        filename="selected.txt",
        mime="text/plain",
        created_by=worker.user_id,
        workspace_id=worker.workspace_id,
        session_id=worker.session_id,
        created_by_turn=worker.turn_id,
        bind_chat_upload=True,
    )
    async with open_shared_ctx(collab.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        user_text = (await (await db.execute("SELECT user_text FROM turns WHERE id=?", (worker.turn_id,))).fetchone())[0]
        source = ChatTurnUserSource(
            turn_id=worker.turn_id,
            session_id=worker.session_id,
            user_id=worker.user_id,
            workspace_id=worker.workspace_id,
            user_text=user_text,
            attachment_ids=(art.id,),
            attachment_refs=({"file_id": art.id, "filename": art.filename, "mime": art.mime, "size": art.size},),
            parts=None,
        )
        await _insert_chat_turn_user_source_in_tx(db, created_at="2026-11-10T00:00:00Z", source=source)
        await db.commit()
    ctx = SimpleNamespace(
        turn_id=worker.turn_id,
        session_id=worker.session_id,
        user_id=worker.user_id,
        workspace_id=worker.workspace_id,
        session_store=SessionStore(collab.path),
        artifact_store=store,
        ordinary_lead_state=OrdinaryLeadRunState(observed_inputs=True),
        _psi_pinned_snapshot=[],
        _psi_parsed_results=[
            PreparedAttachment(
                artifact_id=art.id,
                filename=art.filename,
                mime=art.mime,
                size=art.size,
                extracted_text="",
                truncated=False,
                error=None,
                summary_only=True,
            )
        ],
    )
    async with open_shared_ctx(collab.path) as db:
        await db.execute("BEGIN")
        original = await capture_attachment_dependencies(ctx, db, source)
        assert original.files[0].use == "content" and original.files[0].content_hash is None
    receipt = await artifact_evidence(ctx, art.id, output=False, contract="ordinary-lead-loop.v15")
    ctx.ordinary_lead_state.tool_materials[("native-helper", "native-read")] = {"kind": "sandbox", "artifacts": [receipt]}
    async with open_shared_ctx(collab.path) as db:
        await db.execute("BEGIN")
        retained = await capture_attachment_dependencies(ctx, db, source)
        assert retained.files[0].content_hash == "sha256:" + receipt["sha256"]
        await validate_attachment_dependencies(ctx, db, source, retained)
        Path(art.path).write_bytes(b"x" * art.size)
        with pytest.raises(OrdinaryContinuationUnavailable) as failure:
            await validate_attachment_dependencies(ctx, db, source, retained)
        assert failure.value.condition == "retained_input"
