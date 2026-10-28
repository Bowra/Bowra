"""V3 PRD-B · regression test for legacy-DB migration ordering (P0 caught in smoke).

Reproduce: pre-V3 DB has memory_entries WITHOUT deleted_at column.
Initialize() must add the column via ALTER TABLE BEFORE running CREATE INDEX
on that column. Otherwise sqlite raises "no such column: deleted_at".
"""

from __future__ import annotations

import aiosqlite
import pytest
from bowhead_core.user_memory import ProjectMemoryStore, UserMemoryStore

_LEGACY_PRE_V3_SCHEMA = """
CREATE TABLE memory_entries (
    id              TEXT PRIMARY KEY,
    subject_type    TEXT NOT NULL,
    subject_id      TEXT NOT NULL,
    kind            TEXT NOT NULL,
    key             TEXT NOT NULL,
    value           TEXT NOT NULL,
    locked          INTEGER NOT NULL DEFAULT 0,
    lock_reason     TEXT,
    source          TEXT NOT NULL DEFAULT 'user_explicit',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(subject_type, subject_id, kind, key)
);

CREATE INDEX idx_memory_subject_kind
    ON memory_entries(subject_type, subject_id, kind);
"""


async def _seed_legacy_db(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_LEGACY_PRE_V3_SCHEMA)
        await db.execute(
            "INSERT INTO memory_entries(id, subject_type, subject_id, kind, key, "
            "value, locked, lock_reason, source, created_at, updated_at) "
            "VALUES ('mem_old', 'user', 'u1', 'preference', 'k', '\"v\"', 0, NULL, "
            "'user_explicit', '2026-04-01T00:00:00', '2026-04-01T00:00:00')",
        )
        await db.commit()


@pytest.mark.asyncio
async def test_legacy_db_migration_adds_deleted_at(tmp_path):
    """Pre-V3 DB without deleted_at column should migrate cleanly + preserve data."""
    db_path = tmp_path / "legacy.db"
    await _seed_legacy_db(str(db_path))

    store = UserMemoryStore(db_path)
    await store.initialize()  # must NOT raise

    entries = await store.list_for_user("u1")
    assert len(entries) == 1
    assert entries[0].key == "k"
    assert entries[0].deleted_at is None

    # Verify deleted_at column was added
    async with aiosqlite.connect(str(db_path)) as db:
        cur = await db.execute("PRAGMA table_info(memory_entries)")
        cols = {row[1] for row in await cur.fetchall()}
    assert "deleted_at" in cols

    # Verify deleted_at index was created
    async with aiosqlite.connect(str(db_path)) as db:
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_memory_deleted_at'",
        )
        row = await cur.fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_legacy_db_works_with_project_memory_store(tmp_path):
    """ProjectMemoryStore shares the table; same migration must apply."""
    db_path = tmp_path / "legacy.db"
    await _seed_legacy_db(str(db_path))

    pms = ProjectMemoryStore(db_path)
    await pms.initialize()

    # Should not raise; can write project entries side by side
    await pms.upsert(
        workspace_id="ws1",
        kind="pattern",
        key="x",
        value={"v": 1},
    )
    items = await pms.list_for_workspace("ws1")
    assert len(items) == 1


@pytest.mark.asyncio
async def test_initialize_idempotent(tmp_path):
    """Running initialize() twice on same DB must not error or duplicate indexes."""
    db_path = tmp_path / "idem.db"
    s = UserMemoryStore(db_path)
    await s.initialize()
    await s.initialize()  # second call should be no-op
    await s.upsert(user_id="u1", kind="preference", key="k", value="v")
    entries = await s.list_for_user("u1")
    assert len(entries) == 1
