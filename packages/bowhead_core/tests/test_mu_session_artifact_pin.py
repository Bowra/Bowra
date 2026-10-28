"""Sprint µ — N4 fix: artifact_store.put_bytes pins to session_attachments.

Per discovery.md: prod t_059e6921 had 0 artifacts in Data tab because
bi.fetch_rows persisted artifacts but never linked them to session.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import aiosqlite
import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend


@pytest.fixture
async def store() -> ArtifactStore:
    """Init store + sessions + session_attachments table."""
    tmp = Path(tempfile.mkdtemp())
    db_path = tmp / "test.db"
    backend = LocalBackend(root_dir=tmp / "files")
    s = ArtifactStore(backend=backend, db_path=db_path)
    await s.initialize()
    # Need session_attachments table since put_bytes references it
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS session_attachments (
                session_id      TEXT NOT NULL,
                artifact_id     TEXT NOT NULL,
                pinned_at       TEXT NOT NULL,
                detached_at     TEXT,
                PRIMARY KEY (session_id, artifact_id)
            )
        """)
        await db.execute(
            "INSERT INTO sessions(id, created_at) VALUES ('s_test_x', '2026-05-10')",
        )
        await db.commit()
    return s


@pytest.mark.asyncio
async def test_put_bytes_with_session_id_pins(store: ArtifactStore) -> None:
    """When session_id given, put_bytes inserts into session_attachments."""
    art = await store.put_bytes(
        created_by=None,
        content=b"a,b\n1,2\n",
        filename="test.csv",
        mime="text/csv",
        source="bi_fetch_rows",
        session_id="s_test_x",
    )
    assert art.id.startswith("art_")
    # Verify session_attachments row created
    async with aiosqlite.connect(store.db_path) as db:
        cur = await db.execute(
            "SELECT artifact_id, detached_at FROM session_attachments WHERE session_id=? AND artifact_id=?",
            ("s_test_x", art.id),
        )
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == art.id
        assert row[1] is None  # active (detached_at NULL)


@pytest.mark.asyncio
async def test_put_bytes_without_session_id_no_pin(store: ArtifactStore) -> None:
    """When session_id None/empty, no session_attachments row created."""
    art = await store.put_bytes(
        created_by=None,
        content=b"x",
        filename="x.txt",
        mime="text/plain",
        source="upload",
    )
    async with aiosqlite.connect(store.db_path) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM session_attachments WHERE artifact_id=?",
            (art.id,),
        )
        row = await cur.fetchone()
        assert row[0] == 0


@pytest.mark.asyncio
async def test_put_bytes_session_id_idempotent_on_duplicate(store: ArtifactStore) -> None:
    """If somehow same artifact_id pinned twice, ON CONFLICT updates not fail."""
    art = await store.put_bytes(
        created_by=None,
        content=b"a",
        filename="a.txt",
        mime="text/plain",
        source="upload",
        session_id="s_test_x",
    )
    # Insert manually a 2nd time using same artifact_id (force collision)
    async with aiosqlite.connect(store.db_path) as db:
        # Should not raise
        await db.execute(
            "INSERT INTO session_attachments(session_id, artifact_id, pinned_at) "
            "VALUES (?, ?, '2026-01-01') "
            "ON CONFLICT(session_id, artifact_id) DO UPDATE SET detached_at=NULL",
            ("s_test_x", art.id),
        )
        await db.commit()
