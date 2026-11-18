"""Office files sent without an upload receipt are bound to their stored bytes.

Knowledge base files, results produced in the Session and uploads made before
receipts existed carry no receipt. Completing a Turn or pausing it for a
question must still record them; a receipt that no longer matches its file
still refuses.
"""

import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.attachment_read import read_attachment
from bowhead_core.attachments_injector import prepare_attachments
from bowhead_core.chat_reference_attachment_snapshot import reopen_source_attachment_snapshot, seal_source_attachment_snapshot
from bowhead_core.chat_turn_source import ChatTurnUserSource
from bowhead_core.context_dependencies import ContextSourceValidator, capture_execution_dependencies, validate_context_dependencies
from bowhead_core.context_execution import ExecutionContextView
from bowhead_core.documents.package_io import OFFICE_MIMES
from bowhead_core.documents.upload_confirmation import CHECK_PROFILE, UploadReceiptMismatch
from bowhead_core.ordinary_lead_checkpoint import OrdinaryLeadRunState
from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable
from bowhead_core.session_store import SessionStore, _insert_chat_turn_user_source_in_tx
from test_agent_collaboration import collab as collaboration_fixture
from test_context_execution import prepare
from test_context_history import completion
from test_context_maintenance import context_case as maintenance_fixture

collab = collaboration_fixture
context_case = maintenance_fixture
pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _office_file(store: ArtifactStore, worker, origin: str):
    content = f"Package bytes sent as {origin}.".encode()
    if origin == "unconfirmed_upload":
        # An upload made before receipts existed: ready and sendable, no receipt.
        art = await store.put_bytes(
            content=content,
            filename="brief.docx",
            mime=OFFICE_MIMES[".docx"],
            created_by=worker.user_id,
            workspace_id=worker.workspace_id,
            bind_chat_upload=True,
        )
        return art, {"fileId": art.id, "filename": art.filename, "mime": art.mime, "size": art.size}
    if origin == "knowledge_base":
        # Knowledge base upload, then "Use in chat".
        art = await store.put_bytes(
            content=content,
            filename="policy.docx",
            mime=OFFICE_MIMES[".docx"],
            source="kb_upload",
            kind="user_upload",
            producer_kind="kb_upload",
            created_by=worker.user_id,
            workspace_id=worker.workspace_id,
        )
        assert await store.bind_owned_kb_upload_for_chat(art.id, user_id=worker.user_id, workspace_id=worker.workspace_id) is not None
        return art, {"fileId": art.id, "filename": art.filename, "mime": art.mime, "size": art.size}
    # A deck a sandbox produced in this Session, sent back through Results "Reference in chat".
    art = await store.put_bytes(
        content=content,
        filename="deck.pptx",
        mime=OFFICE_MIMES[".pptx"],
        source="python_sandbox",
        created_by=worker.user_id,
        workspace_id=worker.workspace_id,
        session_id=worker.session_id,
        created_by_turn=worker.turn_id,
    )
    return art, {"fileId": art.id, "filename": art.filename, "mime": art.mime, "size": art.size, "referenceKind": "result"}


async def _record_receipt(collab, art, *, matching: bool) -> None:
    content = Path(art.path).read_bytes()
    async with open_shared_ctx(collab.path) as db:
        await db.execute(
            "INSERT INTO artifact_upload_confirmations(artifact_id, quarantine_path, candidate_path, filename, mime, declared_size, "
            "user_id, workspace_id, session_id, upload_expires_at, phase, candidate_sha256, actual_size, check_profile, "
            "package_info_json, cleanup_after) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 'ready', ?, ?, ?, '{}', ?)",
            (
                art.id,
                art.path + ".quarantine",
                art.path,
                # A mismatched receipt names the file as it was confirmed, before a rename.
                art.filename if matching else "confirmed-original.docx",
                art.mime,
                art.size,
                collab.worker.user_id,
                collab.worker.workspace_id,
                time.time(),
                hashlib.sha256(content).hexdigest(),
                art.size,
                CHECK_PROFILE,
                time.time() + 86400,
            ),
        )
        await db.commit()


async def _sent(collab, store: ArtifactStore, art, ref):
    worker = collab.worker
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
            attachment_refs=(ref,),
            parts=None,
        )
        await _insert_chat_turn_user_source_in_tx(db, created_at="2026-11-17T00:00:00Z", source=source)
        await db.commit()
    parsed = await prepare_attachments(store=store, artifact_ids=[art.id])
    assert parsed[0].content_deferred and parsed[0].content_hash is None
    ctx = SimpleNamespace(
        turn_id=worker.turn_id,
        session_id=worker.session_id,
        user_id=worker.user_id,
        workspace_id=worker.workspace_id,
        session_store=SessionStore(collab.path),
        artifact_store=store,
        knowledge_scope=None,
        knowledge_source_uses={},
        knowledge_source_ancestors=(),
        context_source_observations=None,
        reference_skill_required=False,
        ordinary_lead_state=OrdinaryLeadRunState(observed_inputs=True),
        _psi_pinned_snapshot=[],
        _psi_parsed_results=parsed,
    )
    return source, ctx


def _publisher(collab, ctx) -> ExecutionContextView:
    return ExecutionContextView(
        db_path=collab.path,
        worker=collab.worker,
        execution_id=collab.lead,
        lane="ordinary",
        cipher=collab.store.cipher,
        validate_sources=ContextSourceValidator(ctx),
        activity=AsyncMock(),
        source_dependencies=lambda: capture_execution_dependencies(ctx),
    )


def _heads(collab) -> list[str]:
    import sqlite3

    with sqlite3.connect(collab.path) as db:
        return [
            row[0]
            for row in db.execute("SELECT published_turn_id FROM model_context_conversation_heads WHERE session_id=?", (collab.worker.session_id,))
        ]


@pytest.mark.parametrize("origin", ["unconfirmed_upload", "knowledge_base", "result_reference"])
async def test_completed_turn_publishes_an_office_file_without_a_receipt(collab, context_case, origin):
    store = ArtifactStore(str(collab.path), LocalBackend(collab.path.parent / f"office-{origin}"))
    await store.initialize()
    art, ref = await _office_file(store, collab.worker, origin)
    _, ctx = await _sent(collab, store, art, ref)
    owner = _publisher(collab, ctx)
    await prepare(owner, context_case)
    publication = await owner.conversation_publication(context_case.request.messages)
    proof = next(message for message in publication.dependencies if message.name == "context_attachment_dependencies_v2")
    assert json.loads(proof.content)["files"] == [
        {
            "artifact_id": art.id,
            "filename": art.filename,
            "mime": art.mime,
            "size": art.size,
            "content_hash": "sha256:" + hashlib.sha256(Path(art.path).read_bytes()).hexdigest(),
            "use": "content",
        }
    ]
    assert await SessionStore(collab.path).mark_completed(**completion(collab, publication))
    assert _heads(collab) == [collab.worker.turn_id]
    # The Lead's reader still needs a receipt; it refuses without failing the Turn.
    assert await read_attachment({"artifact_id": art.id}, ctx) == {"ok": False, "error": "attachment_read_unavailable"}
    # A later Turn verifies the same stored bytes.
    await validate_context_dependencies(ctx, publication.dependencies)
    Path(art.path).write_bytes(b"x" * art.size)
    with pytest.raises(OrdinaryContinuationUnavailable):
        await validate_context_dependencies(ctx, publication.dependencies)


@pytest.mark.parametrize("matching", [True, False])
async def test_completion_keeps_a_matching_receipt_and_refuses_a_mismatched_one(collab, context_case, matching):
    store = ArtifactStore(str(collab.path), LocalBackend(collab.path.parent / "office-receipt"))
    await store.initialize()
    art, ref = await _office_file(store, collab.worker, "unconfirmed_upload")
    if matching:
        await _record_receipt(collab, art, matching=True)
    _, ctx = await _sent(collab, store, art, ref)
    owner = _publisher(collab, ctx)
    await prepare(owner, context_case)
    if not matching:
        # A receipt that exists but names another file is never read as "no receipt".
        await _record_receipt(collab, art, matching=False)
        with pytest.raises(UploadReceiptMismatch):
            await owner.conversation_publication(context_case.request.messages)
        assert _heads(collab) == []
        return
    publication = await owner.conversation_publication(context_case.request.messages)
    proof = next(message for message in publication.dependencies if message.name == "context_attachment_dependencies_v2")
    assert json.loads(proof.content)["files"][0]["content_hash"] == "sha256:" + hashlib.sha256(Path(art.path).read_bytes()).hexdigest()
    assert await SessionStore(collab.path).mark_completed(**completion(collab, publication))
    assert _heads(collab) == [collab.worker.turn_id]


@pytest.mark.parametrize("receipt", ["none", "matching", "mismatched"])
async def test_question_pause_seals_an_office_file_by_receipt_or_stored_bytes(collab, receipt):
    store = ArtifactStore(str(collab.path), LocalBackend(collab.path.parent / "office-question"))
    await store.initialize()
    art, ref = await _office_file(store, collab.worker, "knowledge_base" if receipt == "none" else "unconfirmed_upload")
    if receipt != "none":
        await _record_receipt(collab, art, matching=receipt == "matching")
    source, ctx = await _sent(collab, store, art, ref)
    if receipt == "mismatched":
        # prepare_ordinary_question refuses the optional question on exactly this error.
        with pytest.raises(UploadReceiptMismatch):
            await seal_source_attachment_snapshot(source, ctx, "Attached: one Word file.")
        return
    snapshot = await seal_source_attachment_snapshot(source, ctx, "Attached: one Word file.")
    assert snapshot["schema_version"] == 2
    assert snapshot["files"][0]["content_deferred"] is True
    assert snapshot["files"][0]["content_hash"] == "sha256:" + hashlib.sha256(Path(art.path).read_bytes()).hexdigest()
    assert await reopen_source_attachment_snapshot(source, ctx, snapshot) == ("Attached: one Word file.", [])
    if receipt == "none":
        # Without a receipt the resume reads the stored bytes again.
        Path(art.path).write_bytes(b"x" * art.size)
        with pytest.raises(ValueError, match="reference_attachment_material_changed"):
            await reopen_source_attachment_snapshot(source, ctx, snapshot)
