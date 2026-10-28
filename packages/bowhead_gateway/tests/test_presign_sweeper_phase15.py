"""Phase 1.5 UT — orphan sweeper.

Tech-design §5.4:
- only matches status='pending_upload' AND expires_at < now (not 'ready')
- delete-storage-FIRST (P1-S-10: orphan storage is worse than orphan DB row)
- cascade kb_files / session_attachments / turn_attachments
- if backend.delete raises non-404 → retain DB row for next sweep
- background loop cancellable + idempotent
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest
from bowhead_core.artifact_store import LocalBackend
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_gateway.presign_sweeper import (
    _DEFAULT_INTERVAL_S,
    _sweep_once,
    env_interval_s,
)

from bowhead_core import ArtifactStore


class _BackendStub:
    """Wraps LocalBackend but lets tests inject delete() failures."""

    def __init__(self, inner: LocalBackend) -> None:
        self._inner = inner
        self.name = inner.name
        self.delete_raises: dict[str, Exception] = {}  # path → exc
        self.deleted_paths: list[str] = []

    async def put(self, **kw):  # type: ignore[no-untyped-def]
        return await self._inner.put(**kw)

    async def read(self, path):  # type: ignore[no-untyped-def]
        return await self._inner.read(path)

    async def download_url(self, **kw):  # type: ignore[no-untyped-def]
        return await self._inner.download_url(**kw)

    async def delete(self, path: str) -> None:
        if path in self.delete_raises:
            raise self.delete_raises[path]
        self.deleted_paths.append(path)
        await self._inner.delete(path)


@pytest.fixture
async def artifact_store(tmp_path: Path) -> ArtifactStore:
    backend = _BackendStub(LocalBackend(root_dir=tmp_path / "blobs"))
    s = ArtifactStore(db_path=str(tmp_path / "sw.db"), backend=backend)
    await s.initialize()
    # Sweeper expects session_attachments + turn_attachments to exist on
    # the same DB as artifacts. ArtifactStore.initialize() doesn't create
    # them (session_store does in lifespan). Bootstrap minimal versions
    # so the cascade DELETEs don't blow up with "no such table".
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS session_attachments (
                session_id TEXT NOT NULL,
                artifact_id TEXT NOT NULL,
                pinned_at TEXT NOT NULL,
                detached_at TEXT,
                PRIMARY KEY (session_id, artifact_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS turn_attachments (
                turn_id TEXT NOT NULL,
                artifact_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                PRIMARY KEY (turn_id, artifact_id)
            )
        """)
        await db.commit()
    return s


@pytest.fixture
async def kb_files_store(initialize_migrated_kb_files_database, tmp_path: Path) -> KbFilesStore:
    initialize_migrated_kb_files_database(tmp_path / "sw_kb.db")
    s = KbFilesStore(tmp_path / "sw_kb.db")
    await s.initialize()
    return s


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _past_iso(minutes: int = 30) -> str:
    return (datetime.now(tz=UTC) - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _future_iso(minutes: int = 30) -> str:
    return (datetime.now(tz=UTC) + timedelta(minutes=minutes)).isoformat(timespec="seconds")


async def _insert_pending(
    store: ArtifactStore,
    *,
    aid: str,
    path: str,
    expires_at: str,
    created_by: str = "u_t",
) -> None:
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO artifacts(id, filename, mime, size, source, path, "
            "canvas_id, created_by, created_at, status, expires_at) "
            "VALUES (?, ?, ?, ?, 'upload', ?, NULL, ?, ?, 'pending_upload', ?)",
            (aid, "f.bin", "application/octet-stream", 100, path, created_by, _now_iso(), expires_at),
        )
        await db.commit()


async def _insert_ready(store: ArtifactStore, *, aid: str, path: str) -> None:
    """A 'ready' row that the sweeper MUST NOT touch."""
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO artifacts(id, filename, mime, size, source, path, "
            "canvas_id, created_by, created_at, status, expires_at) "
            "VALUES (?, ?, ?, ?, 'upload', ?, NULL, 'u_t', ?, 'ready', NULL)",
            (aid, "ok.bin", "application/octet-stream", 200, path, _now_iso()),
        )
        await db.commit()


# ── happy paths ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_cleans_expired_pending_rows(
    artifact_store: ArtifactStore,
    kb_files_store: KbFilesStore,
    tmp_path: Path,
) -> None:
    aid = "art_expired00001"
    path = str(tmp_path / "blobs" / f"{aid}_f.bin")
    # Write a placeholder file so LocalBackend.delete succeeds
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(b"orphan")
    await _insert_pending(artifact_store, aid=aid, path=path, expires_at=_past_iso())

    metrics = await _sweep_once(
        artifact_store=artifact_store,
        kb_files_store=kb_files_store,
        now_iso=_now_iso(),
    )
    assert metrics["scanned"] == 1
    assert metrics["cleaned"] == 1

    # Row gone; file gone
    async with aiosqlite.connect(artifact_store.db_path) as db:
        cur = await db.execute("SELECT COUNT(*) FROM artifacts WHERE id=?", (aid,))
        count = (await cur.fetchone())[0]
    assert count == 0
    assert not Path(path).exists()


@pytest.mark.asyncio
async def test_sweep_ignores_ready_rows(
    artifact_store: ArtifactStore,
    kb_files_store: KbFilesStore,
    tmp_path: Path,
) -> None:
    """status='ready' must NOT be touched even if expires_at were set."""
    aid = "art_ready000001"
    path = str(tmp_path / "blobs" / f"{aid}_ok.bin")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(b"keep me")
    await _insert_ready(artifact_store, aid=aid, path=path)
    metrics = await _sweep_once(
        artifact_store=artifact_store,
        kb_files_store=kb_files_store,
        now_iso=_now_iso(),
    )
    assert metrics["scanned"] == 0
    assert metrics["cleaned"] == 0
    # Row + file intact
    assert Path(path).exists()


@pytest.mark.asyncio
async def test_sweep_ignores_pending_rows_not_yet_expired(
    artifact_store: ArtifactStore,
    kb_files_store: KbFilesStore,
    tmp_path: Path,
) -> None:
    aid = "art_future00001"
    path = str(tmp_path / "blobs" / f"{aid}_f.bin")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(b"too new")
    await _insert_pending(artifact_store, aid=aid, path=path, expires_at=_future_iso())
    metrics = await _sweep_once(
        artifact_store=artifact_store,
        kb_files_store=kb_files_store,
        now_iso=_now_iso(),
    )
    assert metrics["scanned"] == 0


@pytest.mark.asyncio
async def test_sweep_cascades_kb_files_and_session_attachments(
    artifact_store: ArtifactStore,
    kb_files_store: KbFilesStore,
    tmp_path: Path,
) -> None:
    aid = "art_cascade0001"
    path = str(tmp_path / "blobs" / f"{aid}_f.bin")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(b"orphan")
    await _insert_pending(artifact_store, aid=aid, path=path, expires_at=_past_iso())

    # Seed children
    await kb_files_store.create(
        artifact_id=aid,
        filename="f.bin",
        mime_type="application/octet-stream",
        size_bytes=100,
        scope="personal",
        scope_id="u_t",
        workspace_id="",
        team_id="",
        uploaded_by="u_t",
        initial_parse_status="awaiting_upload",
    )
    async with aiosqlite.connect(artifact_store.db_path) as db:
        await db.execute(
            "INSERT INTO session_attachments(session_id, artifact_id, pinned_at, detached_at) VALUES (?, ?, ?, NULL)",
            ("sess_x", aid, _now_iso()),
        )
        await db.execute(
            "INSERT INTO turn_attachments(turn_id, artifact_id, seq) VALUES (?, ?, 0)",
            ("turn_x", aid),
        )
        await db.commit()

    await _sweep_once(
        artifact_store=artifact_store,
        kb_files_store=kb_files_store,
        now_iso=_now_iso(),
    )

    # All children gone
    async with aiosqlite.connect(kb_files_store._db_path) as db:
        cur = await db.execute("SELECT COUNT(*) FROM kb_files WHERE artifact_id=?", (aid,))
        kb_count = (await cur.fetchone())[0]
    assert kb_count == 0

    async with aiosqlite.connect(artifact_store.db_path) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM session_attachments WHERE artifact_id=?",
            (aid,),
        )
        sess_count = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT COUNT(*) FROM turn_attachments WHERE artifact_id=?",
            (aid,),
        )
        turn_count = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM artifacts WHERE id=?", (aid,))
        art_count = (await cur.fetchone())[0]
    assert sess_count == 0
    assert turn_count == 0
    assert art_count == 0


# ── failure mode: backend.delete raises non-404 ───────────────────


@pytest.mark.asyncio
async def test_sweep_retains_row_on_backend_delete_failure(
    artifact_store: ArtifactStore,
    kb_files_store: KbFilesStore,
    tmp_path: Path,
) -> None:
    """P1-S-10: if backend.delete raises non-404, KEEP the DB row for retry."""
    aid = "art_failed0001"
    path = str(tmp_path / "blobs" / f"{aid}_f.bin")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(b"")
    await _insert_pending(artifact_store, aid=aid, path=path, expires_at=_past_iso())

    artifact_store.backend.delete_raises[path] = RuntimeError("S3 access denied")
    metrics = await _sweep_once(
        artifact_store=artifact_store,
        kb_files_store=kb_files_store,
        now_iso=_now_iso(),
    )
    assert metrics["delete_failed"] == 1
    assert metrics["cleaned"] == 0

    # DB row still present
    async with aiosqlite.connect(artifact_store.db_path) as db:
        cur = await db.execute("SELECT COUNT(*) FROM artifacts WHERE id=?", (aid,))
        count = (await cur.fetchone())[0]
    assert count == 1, "row must be retained so next sweep can retry"


@pytest.mark.asyncio
async def test_sweep_passes_through_when_backend_swallows_404(
    artifact_store: ArtifactStore,
    kb_files_store: KbFilesStore,
    tmp_path: Path,
) -> None:
    """LocalBackend.delete is a no-op for missing files (treats absent as success).
    S3Backend.delete now swallows ClientError 404 too (Phase 1.5 P0-2 fix in
    artifact_store.py). So when backend.delete returns cleanly, sweeper cleans.
    """
    aid = "art_404000001"
    path = str(tmp_path / "blobs" / f"{aid}_f.bin")
    # NO placeholder file → LocalBackend.delete silently no-ops (file doesn't exist)
    await _insert_pending(artifact_store, aid=aid, path=path, expires_at=_past_iso())

    metrics = await _sweep_once(
        artifact_store=artifact_store,
        kb_files_store=kb_files_store,
        now_iso=_now_iso(),
    )
    assert metrics["delete_failed"] == 0
    assert metrics["cleaned"] == 1


@pytest.mark.asyncio
async def test_sweep_loses_cas_race_to_confirm(
    artifact_store: ArtifactStore,
    kb_files_store: KbFilesStore,
    tmp_path: Path,
) -> None:
    """Phase 1.5 review P0-1: sweeper CAS-claims pending_upload → expiring.
    If a concurrent confirm CAS'd to 'confirming' first, sweeper sees rowcount=0
    and bails — does NOT cascade-delete kb_files / artifact row.
    """
    aid = "art_race00001"
    path = str(tmp_path / "blobs" / f"{aid}_f.bin")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(b"in flight")
    await _insert_pending(artifact_store, aid=aid, path=path, expires_at=_past_iso())

    # Simulate confirm racing in: flip status to 'confirming' BEFORE sweeper runs
    async with aiosqlite.connect(artifact_store.db_path) as db:
        await db.execute(
            "UPDATE artifacts SET status='confirming' WHERE id=?",
            (aid,),
        )
        await db.commit()

    metrics = await _sweep_once(
        artifact_store=artifact_store,
        kb_files_store=kb_files_store,
        now_iso=_now_iso(),
    )
    # Row was 'confirming' when sweeper looked → CAS-claim couldn't match
    # → bail before backend.delete + cascade
    assert metrics["cleaned"] == 0
    # Row + file still intact (NOT deleted by sweeper)
    async with aiosqlite.connect(artifact_store.db_path) as db:
        cur = await db.execute(
            "SELECT status FROM artifacts WHERE id=?",
            (aid,),
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "confirming"
    assert Path(path).exists()


# ── helpers ───────────────────────────────────────────────────────


def test_env_interval_default_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("BOWHEAD_PRESIGN_SWEEPER_INTERVAL_S", raising=False)
    assert env_interval_s() == _DEFAULT_INTERVAL_S


def test_env_interval_overridable(monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_PRESIGN_SWEEPER_INTERVAL_S", "120")
    assert env_interval_s() == 120


def test_env_interval_floors_at_30s(monkeypatch) -> None:
    """Floor at 30s prevents misconfigured runaway tick loops."""
    monkeypatch.setenv("BOWHEAD_PRESIGN_SWEEPER_INTERVAL_S", "5")
    assert env_interval_s() == 30


def test_env_interval_ignores_garbage(monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_PRESIGN_SWEEPER_INTERVAL_S", "not-a-number")
    assert env_interval_s() == _DEFAULT_INTERVAL_S
