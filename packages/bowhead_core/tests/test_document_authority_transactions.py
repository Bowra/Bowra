"""Document metadata writes preserve real split-domain authority fences."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.chat_turn_source import ChatTurnUserSource
from bowhead_core.document_chat_continuation import DocumentTurnOwner
from bowhead_core.documents.continuation import DocumentRuntimeSnapshot, register_document_turn_in_tx
from bowhead_core.documents.delivery import DocumentDeliveryStore
from bowhead_core.documents.inputs import DocumentInputStore
from bowhead_core.documents.request import DocumentProposal, DocumentRequestError, DocumentScope, InputRoleProposal, UserQuote
from bowhead_core.documents.retention import DocumentArtifactInUse
from bowhead_core.documents.store import DocumentStore
from bowhead_core.documents.turn_runner import fail_document_turn
from bowhead_core.language_context import LanguageContext
from bowhead_core.session_store import SessionStore, _insert_chat_turn_user_source_in_tx
from bowhead_core.sqlite_domain_runtime import connect_sync
from bowhead_core.sqlite_domains import domain_database_path
from test_document_capability import runtime
from test_sqlite_domain_lifecycle import _clone
from test_sqlite_domain_stores import domain_directory as domain_directory

from bowhead_core import _db, sqlite_domain_runtime

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
SCOPE = DocumentScope(team_id="team_1", workspace_id="ws_1", user_id="u_1")


@pytest.fixture
async def case(domain_directory: Path, tmp_path: Path) -> SimpleNamespace:
    path = _clone(domain_directory, tmp_path / "instance")
    sessions = SessionStore(path)
    text = "Fill the exact supplied template and show a private draft."
    await sessions.record_turn(turn_id="t_doc", session_id="s_1", user_text=text)
    artifacts = ArtifactStore(path, LocalBackend(tmp_path / "blobs"))
    original = b"Synthetic opaque document input"
    artifact = await artifacts.put_bytes(content=original, filename="source.bin", mime="application/octet-stream", created_by=SCOPE.user_id)
    await sessions.record_turn_attachments(turn_id="t_doc", artifact_ids=[artifact.id])
    source = ChatTurnUserSource(
        turn_id="t_doc",
        session_id="s_1",
        user_id=SCOPE.user_id,
        workspace_id=SCOPE.workspace_id,
        user_text=text,
        attachment_ids=(artifact.id,),
        attachment_refs=({"fileId": artifact.id},),
        parts=None,
    )
    async with open_shared_ctx(sessions.db_path) as db:
        await _insert_chat_turn_user_source_in_tx(db, source=source, created_at="2026-11-07")
        await db.commit()
    quote = UserQuote(start=0, text=text)
    proposal = DocumentProposal(
        evidence=quote,
        inputs=(InputRoleProposal(artifact_id=artifact.id, role="template", evidence=quote, current_message_position=1),),
        requested_outputs=("source",),
    )
    ctx = SimpleNamespace(path=path)
    admitted = await runtime(ctx, scope=SCOPE)
    store = DocumentStore(path)
    receipt = await store.submit_from_turn(scope=SCOPE, turn_id="t_doc", command_id="authority-document", proposal=proposal, runtime=admitted)
    worker = DocumentTurnOwner(**SCOPE.model_dump(), session_id="s_1", turn_id="t_doc", generation=1, owner_id="authority-document-worker")
    assert await sessions.claim_chat_spawn_dispatch_for_worker(turn_id=worker.turn_id, owner_id=worker.owner_id, lease_seconds=600) == "claimed"
    snapshot = DocumentRuntimeSnapshot(
        schema_version=1,
        image_id=admitted.image_id,
        model_config_version=admitted.model_config_version,
        planner_profile="pptx-grounded-plan-v4",
        language=LanguageContext().to_dict(),
    )
    async with open_shared_ctx(sessions.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await register_document_turn_in_tx(db, worker=worker, job_id=receipt.job_id, runtime=snapshot)
        await db.commit()
    return SimpleNamespace(
        path=path,
        execution=sessions.db_path,
        artifacts=artifacts,
        original=original,
        artifact=artifact,
        job_id=receipt.job_id,
        worker=worker,
        store=store,
        delivery=DocumentDeliveryStore(artifacts),
    )


def observe_busy(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    """Observe the real lease refusal, without manufacturing a SQLite error."""
    blocked = asyncio.Event()
    loop = asyncio.get_running_loop()
    original = sqlite_domain_runtime._busy

    def observed():
        error = original()
        loop.call_soon_threadsafe(blocked.set)
        return error

    monkeypatch.setattr(sqlite_domain_runtime, "_busy", observed)
    return blocked


async def counts(case: SimpleNamespace) -> tuple[int, int, int]:
    async with open_shared_ctx(case.execution) as db:
        values = []
        for table in ("document_input_sets", "document_input_copies", "document_artifact_refs"):
            async with db.execute(
                f"SELECT count(*) FROM {table} WHERE {'owner_id' if table == 'document_artifact_refs' else 'job_id'}=?", (case.job_id,)
            ) as cursor:
                values.append((await cursor.fetchone())[0])
        return values[0], values[1], values[2]


def authority_reader(case):
    reader = connect_sync(domain_database_path(case.path, "content"))
    reader.execute("BEGIN").close()
    reader.execute("SELECT artifact_id FROM document_input_copies LIMIT 1").close()
    assert reader._leases.get("execution") == 1
    return reader


async def test_input_freeze_waits_for_authority_reader_without_repeating_blob_write(case, monkeypatch):
    blocked = observe_busy(monkeypatch)
    writes = []
    put = case.artifacts.backend.put_immutable_upload

    async def recorded_put(path, *, content, mime):
        writes.append(path)
        return await put(path, content=content, mime=mime)

    monkeypatch.setattr(case.artifacts.backend, "put_immutable_upload", recorded_put)
    reader = authority_reader(case)
    task = None
    try:
        task = asyncio.create_task(DocumentInputStore(case.artifacts).freeze(scope=SCOPE, job_id=case.job_id))
        await asyncio.wait_for(blocked.wait(), 5)
        assert writes == []
        reader.rollback()
        frozen = await asyncio.wait_for(task, 5)
    finally:
        reader.close()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert await counts(case) == (1, 1, 1)
    assert len(writes) == 1
    inputs = DocumentInputStore(case.artifacts)
    assert await inputs.freeze(scope=SCOPE, job_id=case.job_id) == frozen
    assert await inputs.read(scope=SCOPE, job_id=case.job_id, artifact_id=frozen[0].artifact_id) == case.original
    assert len(writes) == 1
    with pytest.raises(DocumentArtifactInUse):
        await case.artifacts.soft_delete(frozen[0].artifact_id, user_id=SCOPE.user_id)
    assert await case.artifacts.purge_now([frozen[0].artifact_id], user_id=SCOPE.user_id) == 0


async def test_cancelled_freeze_wait_does_not_reserve_or_put_inputs(case, monkeypatch):
    blocked = observe_busy(monkeypatch)
    reader = authority_reader(case)
    task = asyncio.create_task(DocumentInputStore(case.artifacts).freeze(scope=SCOPE, job_id=case.job_id))
    try:
        await asyncio.wait_for(blocked.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    finally:
        reader.close()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert await counts(case) == (0, 0, 0)
    # No held writer/authority lease survives cancellation.
    assert await case.artifacts.soft_delete(case.artifact.id, user_id=SCOPE.user_id)


@pytest.mark.parametrize("change", ["revocation", "purge"])
async def test_new_authority_state_is_checked_after_reservation_rollback(case, monkeypatch, change):
    blocked = observe_busy(monkeypatch)
    reader = authority_reader(case)
    changed = False

    async def change_before_reauthorization(_delay):
        nonlocal changed
        assert blocked.is_set() and not changed
        # The real failed SQL transaction and connection have ended. Commit the
        # competing change before the next transaction, not inside its snapshot.
        reader.rollback()
        assert await counts(case) == (0, 0, 0)
        if change == "revocation":
            with closing(connect_sync(case.path)) as authority:
                authority.execute(
                    "UPDATE workspace_members SET status='removed' WHERE workspace_id=? AND user_id=?", (SCOPE.workspace_id, SCOPE.user_id)
                ).close()
                authority.commit()
        else:
            with closing(connect_sync(case.artifacts.db_path)) as authority:
                authority.execute("UPDATE artifacts SET deleted_at='2026-11-07' WHERE id=?", (case.artifact.id,)).close()
                authority.execute(
                    "INSERT INTO artifact_purge_claims(artifact_id,path,created_by,claimed_at) VALUES(?,?,?,?)",
                    (case.artifact.id, case.artifact.path, SCOPE.user_id, "2026-11-07"),
                ).close()
                authority.commit()
        changed = True

    monkeypatch.setattr(_db, "anyio", SimpleNamespace(sleep=change_before_reauthorization))
    try:
        with pytest.raises(DocumentRequestError, match="document_not_found" if change == "revocation" else "document_input_unavailable"):
            await asyncio.wait_for(DocumentInputStore(case.artifacts).freeze(scope=SCOPE, job_id=case.job_id), 5)
    finally:
        reader.close()
    assert changed
    assert await counts(case) == (0, 0, 0)


@pytest.mark.parametrize("change", ["revocation", "cancel"])
async def test_ready_retry_cannot_freeze_inputs_after_access_or_job_loss(case, monkeypatch, change):
    blocked = observe_busy(monkeypatch)
    put = case.artifacts.backend.put_immutable_upload
    reader = None
    writes = []
    changed = False

    async def pause_ready_after_put(path, *, content, mime):
        nonlocal reader
        writes.append(path)
        result = await put(path, content=content, mime=mime)
        reader = connect_sync(case.execution)
        reader.execute("BEGIN").close()
        reader.execute("SELECT id FROM artifacts WHERE path=?", (path,)).close()
        assert reader._leases.get("content") == 1
        return result

    async def release_and_change_state(_delay):
        nonlocal changed
        assert blocked.is_set() and reader is not None and not changed
        reader.rollback()
        if change == "revocation":
            async with open_shared_ctx(case.path) as authority:
                await authority.execute(
                    "UPDATE workspace_members SET status='removed' WHERE workspace_id=? AND user_id=?", (SCOPE.workspace_id, SCOPE.user_id)
                )
                await authority.commit()
        else:
            await case.store.cancel(scope=SCOPE, job_id=case.job_id, command_id="cancel-during-ready", expected_proposal_revision=1)
        changed = True

    monkeypatch.setattr(case.artifacts.backend, "put_immutable_upload", pause_ready_after_put)
    monkeypatch.setattr(_db, "anyio", SimpleNamespace(sleep=release_and_change_state))
    inputs = DocumentInputStore(case.artifacts)
    try:
        with pytest.raises((DocumentRequestError, sqlite3.IntegrityError)):
            await asyncio.wait_for(inputs.freeze(scope=SCOPE, job_id=case.job_id), 5)
    finally:
        if reader is not None:
            reader.close()
    assert changed and len(writes) == 1
    async with (
        open_shared_ctx(case.execution) as db,
        db.execute(
            "SELECT s.frozen_at,c.verified_at,c.artifact_id FROM document_input_sets s JOIN document_input_copies c ON c.job_id=s.job_id WHERE s.job_id=?",
            (case.job_id,),
        ) as cursor,
    ):
        frozen_at, verified_at, artifact_id = await cursor.fetchone()
    assert frozen_at is None and verified_at is not None
    with pytest.raises(DocumentRequestError):
        await inputs.read(scope=SCOPE, job_id=case.job_id, artifact_id=artifact_id)
    assert len(writes) == 1


async def test_failure_state_is_durable_once_after_authority_reader_finishes(case, monkeypatch):
    blocked = observe_busy(monkeypatch)
    reader = authority_reader(case)
    task = asyncio.create_task(fail_document_turn(case.delivery, case.worker, case.job_id, "document_execution_failed"))
    try:
        await asyncio.wait_for(blocked.wait(), 5)
        reader.rollback()
        result = await asyncio.wait_for(task, 5)
    finally:
        reader.close()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert result.kind == "failed" and result.error_code == "document_execution_failed"
    async with open_shared_ctx(case.execution) as db, db.execute("SELECT * FROM document_executions WHERE job_id=?", (case.job_id,)) as cursor:
        before = await cursor.fetchall()
    assert len(before) == 1
    await fail_document_turn(case.delivery, case.worker, case.job_id, "document_runtime_unavailable")
    async with open_shared_ctx(case.execution) as db, db.execute("SELECT * FROM document_executions WHERE job_id=?", (case.job_id,)) as cursor:
        assert await cursor.fetchall() == before
    saved = await case.delivery.get(scope=SCOPE, job_id=case.job_id)
    assert saved["state"] == "failed" and saved["error_code"] == "document_execution_failed"
