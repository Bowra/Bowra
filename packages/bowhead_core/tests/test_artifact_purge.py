"""Real SQLite/LocalBackend lifecycle and fault tests; no document job fiction.

Storage pauses/failures are controlled boundaries. Guard-removal mutations must
make restore/pin races or lost-ack recovery fail, not merely change SQL text.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.artifact_purge import claim_artifact_purge
from bowhead_core.artifact_store import Artifact, ArtifactStore, LocalBackend, _open_db
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.export_assets import ExportAssetError
from bowhead_core.export_snapshot import ExportSnapshotRequest, ExportSnapshotStore
from bowhead_core.session_store import SessionStore

from bowhead_core import migrations

pytestmark = pytest.mark.integration


@dataclass
class Context:
    store: ArtifactStore
    sessions: SessionStore
    canvases: CanvasStore
    snapshots: ExportSnapshotStore
    canvas_id: str


@pytest.fixture
async def ctx(tmp_path: Path) -> Context:
    path = tmp_path / "core.db"
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = "core"
    command.upgrade(config, "head")
    store = ArtifactStore(db_path=path, backend=LocalBackend(tmp_path / "blobs"))
    sessions = SessionStore(path)
    canvases = CanvasStore(path)
    await sessions.ensure_session(session_id="s_purge", workspace_id="ws_purge", user_id="u_owner")
    canvas = await canvases.ensure_canvas(session_id="s_purge", workspace_id="ws_purge", user_id="u_owner")
    return Context(store, sessions, canvases, ExportSnapshotStore(path), canvas.id)


async def _file(ctx: Context, *, trash: bool = True) -> Artifact:
    art = await ctx.store.put_bytes(
        content=b"synthetic file bytes", filename="input.png", mime="image/png", created_by="u_owner", kind="tool_data", scope="user"
    )
    if trash:
        assert await ctx.store.soft_delete(art.id, user_id="u_owner")
    return art


async def _claim_row(ctx: Context, artifact_id: str) -> tuple | None:
    async with _open_db(ctx.store.db_path) as db, db.execute("SELECT * FROM artifact_purge_claims WHERE artifact_id=?", (artifact_id,)) as cur:
        return await cur.fetchone()


async def _snapshot_candidate(ctx: Context, art: Artifact):
    await ctx.canvases.append_section(canvas_id=ctx.canvas_id, kind="image", payload={"artifact_id": art.id})
    return await ctx.snapshots.build_candidate(
        canvas_id=ctx.canvas_id,
        request=ExportSnapshotRequest(
            requested_by="u_owner",
            team_id="team_purge",
            fmt="pdf",
            profile="full_report",
            parity_contract="full_content_v1",
            locale="zh-CN",
            timezone="Asia/Singapore",
            client_surface_receipt_sha256="sha256:fixture",
            theme={"kind": "default"},
            options={},
        ),
    )


async def _commit(ctx: Context, candidate):
    return await ctx.snapshots.commit_candidate(
        candidate,
        expected_projection_token=candidate.content_projection.projection_token,
        expected_surface_token=candidate.surface_projection.surface_token,
    )


async def test_manual_purge_is_scoped_idempotent_and_leaves_exact_tombstone(ctx: Context) -> None:
    art = await _file(ctx)
    active = await _file(ctx, trash=False)
    assert await ctx.store.purge_now([art.id], user_id="u_other") == 0
    assert await ctx.store.purge_now([art.id, art.id, active.id], user_id="u_owner") == 1
    receipt = await _claim_row(ctx, art.id)
    assert receipt and receipt[0:3] == (art.id, art.path, "u_owner") and receipt[4] is not None
    assert not Path(art.path).exists()
    assert await ctx.store.get(art.id, include_deleted=True) is None
    assert await ctx.store.get(active.id) is not None
    assert await ctx.store.purge_now([art.id], user_id="u_owner") == 0
    assert await _claim_row(ctx, art.id) == receipt


@pytest.mark.parametrize("entry", ["manual", "gc"])
async def test_committed_claim_blocks_restore_and_new_pins_before_blob_io_finishes(ctx, monkeypatch, entry) -> None:
    art = await _file(ctx)
    started, release = asyncio.Event(), asyncio.Event()
    delete = ctx.store.backend.delete

    async def paused(path):
        assert path == art.path and await _claim_row(ctx, art.id) is not None
        started.set()
        await release.wait()
        await delete(path)

    monkeypatch.setattr(ctx.store.backend, "delete", paused)
    task = asyncio.create_task(
        ctx.store.purge_now([art.id], user_id="u_owner") if entry == "manual" else ctx.store.hard_delete_old_soft_deleted(days=0)
    )
    try:
        await asyncio.wait_for(started.wait(), 5)
        assert not await ctx.store.restore(art.id, user_id="u_owner")
        assert await ctx.store.force_restore([art.id]) == 0
        with pytest.raises(aiosqlite.IntegrityError, match="purge in progress"):
            await ctx.sessions.pin_session_attachments(session_id="s_purge", artifact_ids=[art.id])
        async with _open_db(ctx.store.db_path) as db:
            with pytest.raises(aiosqlite.IntegrityError, match="purge in progress"):
                await db.execute("UPDATE artifacts SET deleted_at=NULL WHERE id=?", (art.id,))
        assert Path(art.path).read_bytes() == b"synthetic file bytes"
        assert (await ctx.store.get(art.id, include_deleted=True)).deleted_at is not None
    finally:
        release.set()
        await asyncio.wait_for(task, 5)
    assert not Path(art.path).exists()


@pytest.mark.parametrize("lost_ack", [False, True])
async def test_storage_failure_never_unlocks_and_periodic_gc_recovers_recent_manual_claim(ctx, monkeypatch, lost_ack) -> None:
    art = await _file(ctx)
    delete = ctx.store.backend.delete

    async def failed(path):
        if lost_ack:
            await delete(path)
        raise OSError("controlled storage failure")

    monkeypatch.setattr(ctx.store.backend, "delete", failed)
    assert await ctx.store.purge_now([art.id], user_id="u_owner") == 0
    receipt = await _claim_row(ctx, art.id)
    assert receipt and receipt[4] is None
    assert not await ctx.store.restore(art.id, user_id="u_owner")
    assert await ctx.store.get(art.id, include_deleted=True) is not None
    # A fresh Store represents process recovery. The item is not 30 days old,
    # but its durable unfinished claim is eligible independently of trash age.
    recovered = ArtifactStore(db_path=ctx.store.db_path, backend=LocalBackend(Path(art.path).parent))
    result = await recovered.hard_delete_old_soft_deleted(days=30)
    assert result["rows_removed"] == 1
    assert (await _claim_row(ctx, art.id))[:4] == receipt[:4]
    assert (await _claim_row(ctx, art.id))[4] is not None
    assert await ctx.store.get(art.id, include_deleted=True) is None
    assert not Path(art.path).exists()


async def test_cancelled_delete_leaves_recoverable_claim(ctx, monkeypatch) -> None:
    art = await _file(ctx)
    started = asyncio.Event()

    async def cancelled(path):
        started.set()
        await asyncio.Event().wait()

    delete = ctx.store.backend.delete
    monkeypatch.setattr(ctx.store.backend, "delete", cancelled)
    task = asyncio.create_task(ctx.store.purge_now([art.id], user_id="u_owner"))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await _claim_row(ctx, art.id))[4] is None
    assert not await ctx.store.restore(art.id, user_id="u_owner")
    monkeypatch.setattr(ctx.store.backend, "delete", delete)
    assert await ctx.store.purge_now([art.id], user_id="u_owner") == 1


async def test_metadata_commit_failure_after_blob_delete_is_recoverable(ctx) -> None:
    art = await _file(ctx)
    async with _open_db(ctx.store.db_path) as db:
        await db.execute("CREATE TRIGGER fault_purge_commit BEFORE DELETE ON artifacts BEGIN SELECT RAISE(ABORT,'fault'); END")
        await db.commit()
    with pytest.raises(aiosqlite.IntegrityError, match="fault"):
        await ctx.store.purge_now([art.id], user_id="u_owner")
    assert not Path(art.path).exists()
    assert (await _claim_row(ctx, art.id))[4] is None
    assert not await ctx.store.restore(art.id, user_id="u_owner")
    async with _open_db(ctx.store.db_path) as db:
        await db.execute("DROP TRIGGER fault_purge_commit")
        await db.commit()
    assert (await ctx.store.hard_delete_old_soft_deleted())["rows_removed"] == 1


async def test_concurrent_manual_and_gc_finalize_only_once(ctx, monkeypatch) -> None:
    art = await _file(ctx)
    entered, release = asyncio.Event(), asyncio.Event()
    delete = ctx.store.backend.delete
    calls = 0

    async def paused(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
        await release.wait()
        await delete(path)

    monkeypatch.setattr(ctx.store.backend, "delete", paused)
    manual = asyncio.create_task(ctx.store.purge_now([art.id], user_id="u_owner"))
    gc = asyncio.create_task(ctx.store.hard_delete_old_soft_deleted(days=0))
    try:
        await asyncio.wait_for(entered.wait(), 5)
    finally:
        release.set()
    manual_count, gc_count = await asyncio.wait_for(asyncio.gather(manual, gc), 5)
    assert manual_count + gc_count["rows_removed"] == 1
    assert (await _claim_row(ctx, art.id))[4] is not None


async def test_gc_rechecks_restore_after_candidate_selection(ctx, monkeypatch) -> None:
    first, second = sorted([await _file(ctx), await _file(ctx)], key=lambda art: art.id)
    delete = ctx.store.backend.delete
    deleted = []

    async def restore_other(path):
        deleted.append(path)
        if path == first.path:
            assert await ctx.store.restore(second.id, user_id="u_owner")
        await delete(path)

    monkeypatch.setattr(ctx.store.backend, "delete", restore_other)
    stats = await ctx.store.hard_delete_old_soft_deleted(days=0)
    assert stats["rows_removed"] == 1 and deleted == [first.path]
    assert await ctx.store.get(second.id) is not None
    assert Path(second.path).exists()


@pytest.mark.parametrize("entry", ["manual", "gc"])
async def test_live_snapshot_retains_blob_until_snapshot_released(ctx, monkeypatch, entry) -> None:
    art = await _file(ctx, trash=False)
    snapshot = await _commit(ctx, await _snapshot_candidate(ctx, art))
    assert await ctx.store.soft_delete(art.id, user_id="u_owner")
    delete = AsyncMock(wraps=ctx.store.backend.delete)
    monkeypatch.setattr(ctx.store.backend, "delete", delete)
    if entry == "manual":
        assert await ctx.store.purge_now([art.id], user_id="u_owner") == 0
    else:
        assert (await ctx.store.hard_delete_old_soft_deleted(days=0))["pins_skipped"] == 1
    delete.assert_not_called()
    assert await _claim_row(ctx, art.id) is None
    assert Path(art.path).exists()
    async with _open_db(ctx.store.db_path) as db:
        await db.execute("DELETE FROM canvas_export_snapshots WHERE id=?", (snapshot.id,))
        await db.commit()
    assert await ctx.store.purge_now([art.id], user_id="u_owner") == 1


async def test_old_snapshot_candidate_cannot_commit_after_purge_claim(ctx) -> None:
    art = await _file(ctx, trash=False)
    candidate = await _snapshot_candidate(ctx, art)
    assert await ctx.store.soft_delete(art.id, user_id="u_owner")
    assert await claim_artifact_purge(ctx.store.db_path, artifact_id=art.id, user_id="u_owner") is not None
    with pytest.raises(ExportAssetError, match="asset_missing_or_forbidden"):
        await _commit(ctx, candidate)
    assert await ctx.snapshots.get_snapshot(candidate.id) is None


async def test_gc_rechecks_snapshot_created_after_candidate_selection(ctx, monkeypatch) -> None:
    first, second = sorted([await _file(ctx), await _file(ctx)], key=lambda art: art.id)
    delete = ctx.store.backend.delete
    deleted = []

    async def pin_other(path):
        deleted.append(path)
        if path == first.path:
            assert await ctx.store.restore(second.id, user_id="u_owner")
            await _commit(ctx, await _snapshot_candidate(ctx, second))
            assert await ctx.store.soft_delete(second.id, user_id="u_owner")
        await delete(path)

    monkeypatch.setattr(ctx.store.backend, "delete", pin_other)
    stats = await ctx.store.hard_delete_old_soft_deleted(days=0)
    assert stats["rows_removed"] == 1 and deleted == [first.path]
    assert await ctx.store.get(second.id, include_deleted=True) is not None
    assert Path(second.path).exists()


@pytest.mark.parametrize("reference", ["session", "canvas"])
async def test_ttl_rechecks_new_reference_after_listing_candidates(ctx, reference) -> None:
    art = await _file(ctx, trash=False)
    async with _open_db(ctx.store.db_path) as db:
        await db.execute("UPDATE artifacts SET created_at='2000-01-01' WHERE id=?", (art.id,))
        if reference == "session":
            # Since 619e2f58e, only user-intent pins (inject='full') count as references; automatic pins do not retain artifacts;
            # it was not pinned when candidates were listed; the pin below adds a reference after that listing.
            await db.execute("UPDATE artifacts SET inject='full' WHERE id=?", (art.id,))
        await db.commit()
    candidates = await ctx.store.stale_run_data_ids()
    assert candidates == [art.id]
    if reference == "session":
        await ctx.sessions.pin_session_attachments(session_id="s_purge", artifact_ids=[art.id])
    else:
        async with _open_db(ctx.store.db_path) as db:
            await db.execute("UPDATE artifacts SET canvas_id=? WHERE id=?", (ctx.canvas_id, art.id))
            await db.commit()
    assert await ctx.store.expire_run_data_ids(candidates) == 0
    assert await ctx.store.get(art.id) is not None


async def test_purge_without_migration_fails_before_any_blob_delete(tmp_path, monkeypatch) -> None:
    store = ArtifactStore(db_path=tmp_path / "legacy.db", backend=LocalBackend(tmp_path / "blobs"))
    await store.initialize()
    art = await store.put_bytes(content=b"legacy", filename="f.txt", mime="text/plain", created_by="u_owner")
    assert await store.soft_delete(art.id, user_id="u_owner")
    delete = AsyncMock()
    monkeypatch.setattr(store.backend, "delete", delete)
    with pytest.raises(aiosqlite.OperationalError):
        await store.purge_now([art.id], user_id="u_owner")
    with pytest.raises(aiosqlite.OperationalError):
        await store.hard_delete_old_soft_deleted(days=0)
    delete.assert_not_called()
    assert Path(art.path).read_bytes() == b"legacy"
    assert await store.restore(art.id, user_id="u_owner")
