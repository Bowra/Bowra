"""Real admission, migrated SQLite and local create-only I/O, including faults.

Opaque synthetic fixture bytes do NOT prove valid PPT or template generation.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import test_document_store as admission_fixtures
from bowhead_core.documents.inputs import RECOVERY_SECONDS, DocumentInputStore, cleanup_document_inputs
from bowhead_core.documents.request import DocumentRequestError, InputRoleProposal
from bowhead_core.documents.retention import DocumentArtifactInUse
from bowhead_core.documents.store import DocumentStore
from bowhead_core.results_artifact_policy import is_results_internal_artifact, results_internal_artifact_sql
from bowhead_core.session_lifecycle import SessionLifecycleCoordinator
from bowhead_core.terminal_protocol_store import TerminalProtocolStore
from test_document_store import SCOPE, _sql

ctx = admission_fixtures.ctx
migrated_db = admission_fixtures.migrated_db

pytestmark = pytest.mark.integration


async def _freeze(ctx):
    receipt = await ctx.submit()
    inputs = DocumentInputStore(ctx.artifacts)
    frozen = await inputs.freeze(scope=SCOPE, job_id=receipt.job_id)
    return receipt.job_id, inputs, frozen[0]


async def _cancel(ctx, job_id):
    await ctx.store.cancel(scope=SCOPE, job_id=job_id, command_id="cancel", expected_proposal_revision=1)


async def test_real_bytes_are_frozen_once_not_regenerated_or_public(ctx, monkeypatch):
    original = await ctx.artifacts.backend.read(ctx.artifact.path)
    job_id, inputs, frozen = await _freeze(ctx)
    assert frozen.sha256 == hashlib.sha256(original).hexdigest()
    assert frozen.size_bytes == len(original) and frozen.source_artifact_id == ctx.artifact.id
    assert await inputs.read(scope=SCOPE, job_id=job_id, artifact_id=frozen.artifact_id) == original
    assert await inputs.freeze(scope=SCOPE, job_id=job_id) == (frozen,)
    assert await ctx.artifacts.get(frozen.artifact_id, include_deleted=True) is None
    assert await ctx.artifacts.get_many([frozen.artifact_id, ctx.artifact.id]) == [ctx.artifact]
    items, total, size = await ctx.artifacts.list_for_user(user_id=SCOPE.user_id, limit=1)
    assert [item.id for item in items] == [ctx.artifact.id] and total == 1 and size == len(original)
    assert [item.id for item in await ctx.artifacts.list_usage_artifacts_for_user(SCOPE.user_id)] == [ctx.artifact.id]
    assert [item.id for item in await ctx.artifacts.list_for_session(session_id="s_doc", user_id=SCOPE.user_id)] == [ctx.artifact.id]
    assert (await _sql(ctx, "SELECT count(*) FROM document_artifact_refs WHERE released_at IS NULL"))[0][0] == 1
    assert (await DocumentStore(ctx.path).get(scope=SCOPE, job_id=job_id)).phase == "resolving"
    # Changing the original after freeze never changes/recreates the copy.
    await ctx.artifacts.backend.delete(ctx.artifact.path)
    await ctx.artifacts.backend.put(aid=ctx.artifact.id, content=b"changed original", filename=ctx.artifact.filename, mime=ctx.artifact.mime)
    assert await inputs.read(scope=SCOPE, job_id=job_id, artifact_id=frozen.artifact_id) == original


async def test_concurrent_freeze_reuses_whole_set_and_survives_parent_turn_completion(ctx):
    receipt = await ctx.submit()
    await _sql(ctx, "UPDATE turns SET status='completed' WHERE id='t_doc'")
    results = await asyncio.gather(*(DocumentInputStore(ctx.artifacts).freeze(scope=SCOPE, job_id=receipt.job_id) for _ in range(3)))
    assert results == [results[0]] * 3
    for table in ("document_input_sets", "document_input_copies", "document_artifact_refs"):
        assert (await _sql(ctx, f"SELECT count(*) FROM {table}"))[0][0] == 1


@pytest.mark.parametrize("lost_ack", [False, True])
async def test_candidate_reservation_survives_failed_or_unknown_put(ctx, monkeypatch, lost_ack):
    receipt = await ctx.submit()
    put = ctx.artifacts.backend.put_immutable_upload

    async def fault(path, content, mime):
        # Durability and retention precede the external write.
        assert (await _sql(ctx, "SELECT count(*) FROM document_artifact_refs WHERE released_at IS NULL"))[0][0] == 1
        if lost_ack:
            await put(path, content=content, mime=mime)
        raise OSError("controlled PUT response failure")

    monkeypatch.setattr(ctx.artifacts.backend, "put_immutable_upload", fault)
    with pytest.raises(OSError, match="controlled"):
        await DocumentInputStore(ctx.artifacts).freeze(scope=SCOPE, job_id=receipt.job_id)
    identity = await _sql(ctx, "SELECT artifact_id,path,sha256 FROM document_input_copies")
    assert await _sql(ctx, "SELECT frozen_at FROM document_input_sets") == [(None,)]
    monkeypatch.setattr(ctx.artifacts.backend, "put_immutable_upload", put)
    await DocumentInputStore(ctx.artifacts).freeze(scope=SCOPE, job_id=receipt.job_id)
    assert await _sql(ctx, "SELECT artifact_id,path,sha256 FROM document_input_copies") == identity


async def test_reservation_transaction_failure_leaves_no_metadata_or_blob(ctx, monkeypatch):
    receipt = await ctx.submit()
    put = AsyncMock(side_effect=AssertionError("must not write before commit"))
    monkeypatch.setattr(ctx.artifacts.backend, "put_immutable_upload", put)
    await _sql(ctx, "CREATE TRIGGER fault_ref BEFORE INSERT ON document_artifact_refs BEGIN SELECT RAISE(ABORT,'controlled'); END")
    with pytest.raises(aiosqlite.IntegrityError, match="controlled"):
        await DocumentInputStore(ctx.artifacts).freeze(scope=SCOPE, job_id=receipt.job_id)
    for table in ("document_input_sets", "document_input_copies", "document_artifact_refs"):
        assert (await _sql(ctx, f"SELECT count(*) FROM {table}"))[0][0] == 0
    assert (await _sql(ctx, "SELECT count(*) FROM artifacts"))[0][0] == 1
    put.assert_not_awaited()


async def test_put_ack_does_not_prove_candidate_hash(ctx, monkeypatch):
    receipt = await ctx.submit()
    put = ctx.artifacts.backend.put_immutable_upload

    async def corrupted(path, content, mime):
        return await put(path, content=b"corrupted storage", mime=mime)

    monkeypatch.setattr(ctx.artifacts.backend, "put_immutable_upload", corrupted)
    with pytest.raises(DocumentRequestError, match="frozen_hash_mismatch"):
        await DocumentInputStore(ctx.artifacts).freeze(scope=SCOPE, job_id=receipt.job_id)
    assert await _sql(ctx, "SELECT frozen_at FROM document_input_sets") == [(None,)]
    assert await _sql(ctx, "SELECT verified_at FROM document_input_copies") == [(None,)]
    with pytest.raises(DocumentRequestError, match="frozen_hash_mismatch"):
        await DocumentInputStore(ctx.artifacts).freeze(scope=SCOPE, job_id=receipt.job_id)


async def test_retry_never_replaces_reserved_sha_with_changed_original(ctx, monkeypatch):
    receipt = await ctx.submit()
    put = ctx.artifacts.backend.put_immutable_upload
    monkeypatch.setattr(ctx.artifacts.backend, "put_immutable_upload", AsyncMock(side_effect=OSError("controlled")))
    with pytest.raises(OSError):
        await DocumentInputStore(ctx.artifacts).freeze(scope=SCOPE, job_id=receipt.job_id)
    identity = await _sql(ctx, "SELECT artifact_id,path,sha256 FROM document_input_copies")
    await ctx.artifacts.backend.delete(ctx.artifact.path)
    await ctx.artifacts.backend.put(aid=ctx.artifact.id, content=b"new bytes", filename=ctx.artifact.filename, mime=ctx.artifact.mime)
    monkeypatch.setattr(ctx.artifacts.backend, "put_immutable_upload", put)
    with pytest.raises(DocumentRequestError, match="source_hash_mismatch"):
        await DocumentInputStore(ctx.artifacts).freeze(scope=SCOPE, job_id=receipt.job_id)
    assert await _sql(ctx, "SELECT artifact_id,path,sha256 FROM document_input_copies") == identity


@pytest.mark.parametrize("change", ["owner", "membership", "source", "cancel", "session"])
async def test_read_reauthorizes_and_checks_lifecycle(ctx, change):
    job_id, inputs, frozen = await _freeze(ctx)
    if change == "owner":
        await _sql(ctx, "UPDATE artifacts SET created_by='u_other' WHERE id=?", (ctx.artifact.id,))
    elif change == "membership":
        await _sql(ctx, "DELETE FROM workspace_members WHERE user_id='u_doc' AND workspace_id='ws_doc'")
    elif change == "source":
        await ctx.artifacts.soft_delete(ctx.artifact.id, user_id=SCOPE.user_id)
    elif change == "cancel":
        await _cancel(ctx, job_id)
    else:
        await SessionLifecycleCoordinator(ctx.sessions, TerminalProtocolStore(ctx.path)).soft_delete(
            session_id="s_doc", actor_audit_ref="user:u_doc", request_id="delete-session"
        )
    with pytest.raises(DocumentRequestError):
        await inputs.read(scope=SCOPE, job_id=job_id, artifact_id=frozen.artifact_id)
    with pytest.raises(DocumentRequestError):
        await inputs.freeze(scope=SCOPE, job_id=job_id)


async def test_cancel_during_put_is_fenced_and_late_put_is_reaped_again(ctx, monkeypatch):
    receipt = await ctx.submit()
    started, release = asyncio.Event(), asyncio.Event()
    put = ctx.artifacts.backend.put_immutable_upload

    async def paused(path, content, mime):
        started.set()
        await release.wait()
        return await put(path, content=content, mime=mime)

    monkeypatch.setattr(ctx.artifacts.backend, "put_immutable_upload", paused)
    task = asyncio.create_task(DocumentInputStore(ctx.artifacts).freeze(scope=SCOPE, job_id=receipt.job_id))
    try:
        await asyncio.wait_for(started.wait(), 5)
        await _cancel(ctx, receipt.job_id)
        now = time.time() + RECOVERY_SECONDS + 2
        assert await cleanup_document_inputs(ctx.artifacts, now=now) == 1
        path = (await _sql(ctx, "SELECT path FROM document_input_copies"))[0][0]
        assert not Path(path).exists()
    finally:
        release.set()
    with pytest.raises(DocumentRequestError):
        await asyncio.wait_for(task, 5)
    assert Path(path).exists()  # Delayed external PUT after first cleanup.
    assert await _sql(ctx, "SELECT frozen_at FROM document_input_sets") == [(None,)]
    assert await cleanup_document_inputs(ctx.artifacts, now=now + RECOVERY_SECONDS + 2) == 0
    assert not Path(path).exists()
    assert (await _sql(ctx, "SELECT released_at FROM document_artifact_refs"))[0][0] is not None


async def test_active_references_block_ttl_manual_delete_purge_and_pin(ctx):
    job_id, _, frozen = await _freeze(ctx)
    assert frozen.artifact_id not in await ctx.artifacts.stale_run_data_ids(days=-1, limit=1)
    assert await ctx.artifacts.expire_run_data_ids([frozen.artifact_id]) == 0
    with pytest.raises(DocumentArtifactInUse, match="artifact_in_use"):
        await ctx.artifacts.soft_delete(frozen.artifact_id, user_id=SCOPE.user_id)
    assert await ctx.artifacts.purge_now([frozen.artifact_id], user_id=SCOPE.user_id) == 0
    assert (await ctx.artifacts.hard_delete_old_soft_deleted(days=-1))["rows_removed"] == 0
    for sql, params in [
        ("UPDATE artifacts SET deleted_at='2000-01-01' WHERE id=?", (frozen.artifact_id,)),
        ("DELETE FROM artifacts WHERE id=?", (frozen.artifact_id,)),
        ("UPDATE artifacts SET path='forged' WHERE id=?", (frozen.artifact_id,)),
        ("UPDATE document_artifact_refs SET released_at='2000-01-01' WHERE artifact_id=?", (frozen.artifact_id,)),
        (
            "INSERT INTO artifact_purge_claims(artifact_id,path,created_by,claimed_at) SELECT id,path,created_by,'2000-01-01' FROM artifacts WHERE id=?",
            (frozen.artifact_id,),
        ),
    ]:
        with pytest.raises(aiosqlite.IntegrityError):
            await _sql(ctx, sql, params)
    with pytest.raises(aiosqlite.IntegrityError, match="not a user reference"):
        await ctx.sessions.pin_session_attachments(session_id="s_doc", artifact_ids=[frozen.artifact_id])
    with pytest.raises(aiosqlite.IntegrityError, match="not a user reference"):
        await ctx.sessions.record_turn_attachments(turn_id="t_doc", artifact_ids=[frozen.artifact_id])
    assert await cleanup_document_inputs(ctx.artifacts, now=time.time() + 10 * RECOVERY_SECONDS) == 0
    assert await DocumentInputStore(ctx.artifacts).read(scope=SCOPE, job_id=job_id, artifact_id=frozen.artifact_id)


async def test_cleanup_waits_after_cancel_and_runs_flag_off_after_delete_failure(ctx, monkeypatch):
    job_id, _, frozen = await _freeze(ctx)
    await _cancel(ctx, job_id)
    assert await cleanup_document_inputs(ctx.artifacts, now=time.time() + RECOVERY_SECONDS - 30) == 0
    delete = ctx.artifacts.backend.delete
    monkeypatch.setattr(ctx.artifacts.backend, "delete", AsyncMock(side_effect=OSError("controlled")))
    now = time.time() + RECOVERY_SECONDS + 2
    assert await cleanup_document_inputs(ctx.artifacts, now=now) == 0
    assert not await ctx.artifacts.restore(frozen.artifact_id, user_id=SCOPE.user_id)
    assert await ctx.artifacts.force_restore([frozen.artifact_id]) == 0
    assert (await _sql(ctx, "SELECT completed_at FROM artifact_purge_claims")) == [(None,)]
    monkeypatch.setattr(ctx.artifacts.backend, "delete", delete)
    assert (await ctx.artifacts.hard_delete_old_soft_deleted())["rows_removed"] == 1
    assert (await _sql(ctx, "SELECT count(*) FROM document_input_copies"))[0][0] == 1


async def test_results_sql_filters_internal_rows_before_limit(ctx):
    _, _, frozen = await _freeze(ctx)
    clause, params = results_internal_artifact_sql("a")
    assert await _sql(ctx, f"SELECT a.id FROM artifacts a WHERE NOT {clause} ORDER BY a.id LIMIT 1", params) == [(ctx.artifact.id,)]
    assert is_results_internal_artifact(SimpleNamespace(id=frozen.artifact_id, producer_kind="template_document_internal_v1"))


async def test_read_verifies_frozen_blob_hash(ctx):
    job_id, inputs, frozen = await _freeze(ctx)
    path = (await _sql(ctx, "SELECT path FROM document_input_copies"))[0][0]
    await ctx.artifacts.backend.delete(path)
    await ctx.artifacts.backend.put_immutable_upload(path, content=b"corrupt", mime="application/octet-stream")
    with pytest.raises(DocumentRequestError, match="frozen_hash_mismatch"):
        await inputs.read(scope=SCOPE, job_id=job_id, artifact_id=frozen.artifact_id)


async def test_partial_set_is_not_readable_until_every_copy_is_verified(ctx, monkeypatch):
    extra = await ctx.artifacts.put_bytes(content=b"synthetic reference", filename="reference.txt", mime="text/plain", created_by=SCOPE.user_id)
    await ctx.sessions.pin_session_attachments(session_id="s_doc", artifact_ids=[extra.id])
    proposal = ctx.proposal.model_copy(
        update={"inputs": (*ctx.proposal.inputs, InputRoleProposal(artifact_id=extra.id, role="reference", evidence=ctx.proposal.evidence))}
    )
    receipt = await ctx.submit(proposal=proposal)
    inputs = DocumentInputStore(ctx.artifacts)
    put = ctx.artifacts.backend.put_immutable_upload
    calls = 0

    async def second_fails(path, *, content, mime):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("second PUT failed")
        return await put(path, content=content, mime=mime)

    monkeypatch.setattr(ctx.artifacts.backend, "put_immutable_upload", second_fails)
    with pytest.raises(OSError, match="second PUT"):
        await inputs.freeze(scope=SCOPE, job_id=receipt.job_id)
    copies = await _sql(ctx, "SELECT artifact_id,verified_at FROM document_input_copies ORDER BY input_index")
    assert len(copies) == 2 and copies[0][1] is not None and copies[1][1] is None
    assert await _sql(ctx, "SELECT frozen_at FROM document_input_sets") == [(None,)]
    with pytest.raises(DocumentRequestError, match="input_unavailable"):
        await inputs.read(scope=SCOPE, job_id=receipt.job_id, artifact_id=copies[0][0])
    monkeypatch.setattr(ctx.artifacts.backend, "put_immutable_upload", put)
    frozen = await DocumentInputStore(ctx.artifacts).freeze(scope=SCOPE, job_id=receipt.job_id)
    assert [item.artifact_id for item in frozen] == [copy[0] for copy in copies]
    assert await inputs.read(scope=SCOPE, job_id=receipt.job_id, artifact_id=frozen[1].artifact_id) == b"synthetic reference"


@pytest.mark.parametrize("boundary", ["file", "total"])
async def test_byte_limits_apply_before_reservation(ctx, monkeypatch, boundary):
    from bowhead_core.artifact_store import ArtifactReadLimitError
    from bowhead_core.documents import inputs

    receipt = await ctx.submit()
    monkeypatch.setattr(inputs, "MAX_INPUT_BYTES" if boundary == "file" else "MAX_TOTAL_BYTES", 8)
    with pytest.raises(ArtifactReadLimitError if boundary == "file" else DocumentRequestError):
        await DocumentInputStore(ctx.artifacts).freeze(scope=SCOPE, job_id=receipt.job_id)
    assert await _sql(ctx, "SELECT count(*) FROM document_input_sets") == [(0,)]


async def test_gateway_file_routes_do_not_expose_private_copies_and_sweeper_cleans_flag_off(ctx, monkeypatch):
    from datetime import UTC, datetime

    from bowhead_gateway.presign_sweeper import _sweep_once
    from bowhead_gateway.routes import files
    from fastapi import FastAPI
    from httpx2 import ASGITransport, AsyncClient

    job_id, _, frozen = await _freeze(ctx)
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")
    app = FastAPI()
    app.include_router(files.router, prefix="/api/v1/files")
    app.state.artifact_store = ctx.artifacts
    app.state.settings = SimpleNamespace(dev_default_user=SCOPE.user_id, auth_mode="dev")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture.test") as client:
        for path in (frozen.artifact_id, f"{frozen.artifact_id}/preview", f"{frozen.artifact_id}/usages"):
            response = await client.get(f"/api/v1/files/{path}", headers={"X-Bowhead-User-Id": SCOPE.user_id})
            assert response.status_code == 404
        # Positive control: same router/auth/storage still serves the original.
        response = await client.get(f"/api/v1/files/{ctx.artifact.id}", headers={"X-Bowhead-User-Id": SCOPE.user_id})
        assert response.status_code == 200 and response.content == b"synthetic metadata-only input"
    await _cancel(ctx, job_id)
    await _sweep_once(
        artifact_store=ctx.artifacts, kb_files_store=None, now_iso=datetime.fromtimestamp(time.time() + RECOVERY_SECONDS + 2, UTC).isoformat()
    )
    assert await _sql(ctx, "SELECT count(*) FROM artifacts WHERE id=?", (frozen.artifact_id,)) == [(0,)]
