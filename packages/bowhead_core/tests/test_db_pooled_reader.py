"""Pooled read-only SQLite connections (`bowhead_core._db.open_pooled_read_ctx`)."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import aiosqlite
import pytest
from bowhead_core._db import close_pooled_readers, open_pooled_read_ctx

from bowhead_core import _db

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@pytest.fixture(autouse=True)
async def _isolated_pools() -> AsyncIterator[None]:
    await close_pooled_readers()
    yield
    await close_pooled_readers()


def _make_db(path: Path, value: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    conn.execute("INSERT OR REPLACE INTO kv (k, v) VALUES ('a', ?)", (value,))
    conn.commit()
    conn.close()


async def _read(path: Path) -> tuple[aiosqlite.Connection, str]:
    async with open_pooled_read_ctx(path) as db, db.execute("SELECT v FROM kv WHERE k='a'") as cur:
        row = await cur.fetchone()
    assert row is not None
    return db, str(row[0])


@pytest.mark.asyncio
async def test_reader_is_reused_and_sees_later_commits(tmp_path: Path) -> None:
    db_path = tmp_path / "shared.db"
    _make_db(db_path, "first")

    first_conn, first_value = await _read(db_path)
    _make_db(db_path, "second")
    second_conn, second_value = await _read(db_path)

    assert first_conn is second_conn
    assert (first_value, second_value) == ("first", "second")
    assert first_conn._thread.daemon is True


@pytest.mark.asyncio
async def test_writes_are_rejected_and_failed_reader_is_not_reused(tmp_path: Path) -> None:
    db_path = tmp_path / "shared.db"
    _make_db(db_path, "kept")

    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        async with open_pooled_read_ctx(db_path) as db:
            failed_conn = db
            await db.execute("UPDATE kv SET v='changed'")

    next_conn, value = await _read(db_path)
    assert next_conn is not failed_conn
    assert value == "kept"


@pytest.mark.asyncio
async def test_row_factory_is_reset_before_reuse(tmp_path: Path) -> None:
    db_path = tmp_path / "shared.db"
    _make_db(db_path, "x")

    async with open_pooled_read_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT v FROM kv") as cur:
            assert (await cur.fetchone())["v"] == "x"

    async with open_pooled_read_ctx(db_path) as again:
        assert again is db
        async with again.execute("SELECT v FROM kv") as cur:
            assert await cur.fetchone() == ("x",)


@pytest.mark.asyncio
async def test_reader_left_in_transaction_is_closed_not_reused(tmp_path: Path) -> None:
    db_path = tmp_path / "shared.db"
    _make_db(db_path, "x")

    async with open_pooled_read_ctx(db_path) as db:
        await db.execute("BEGIN")
        leaked = db

    other, _value = await _read(db_path)
    assert other is not leaked


@pytest.mark.asyncio
async def test_replaced_database_file_is_never_read_through_old_handle(tmp_path: Path) -> None:
    db_path = tmp_path / "shared.db"
    _make_db(db_path, "old file")
    old_conn, _value = await _read(db_path)

    for suffix in ("", "-wal", "-shm"):
        (tmp_path / f"shared.db{suffix}").unlink(missing_ok=True)
    _make_db(db_path, "new file")

    new_conn, value = await _read(db_path)
    assert new_conn is not old_conn
    assert value == "new file"


@pytest.mark.asyncio
async def test_idle_readers_are_capped_per_file(tmp_path: Path) -> None:
    db_path = tmp_path / "shared.db"
    _make_db(db_path, "x")
    borrowed = [open_pooled_read_ctx(db_path) for _ in range(_db._READ_POOL_MAX_IDLE + 2)]
    conns = [await ctx.__aenter__() for ctx in borrowed]
    for ctx in borrowed:
        await ctx.__aexit__(None, None, None)

    assert len({id(conn) for conn in conns}) == _db._READ_POOL_MAX_IDLE + 2
    assert len(_db._read_pools[str(db_path)].idle) == _db._READ_POOL_MAX_IDLE

    await close_pooled_readers()
    assert _db._read_pools == {}


@pytest.mark.asyncio
async def test_per_request_public_entry_reads_reuse_one_reader_and_see_new_writes(tmp_path: Path) -> None:
    from bowhead_core.public_entries import PublicEntryStore

    db_path = tmp_path / "public-entries.db"
    store = PublicEntryStore(db_path)
    await store.initialize()
    assert await store.list_all() == []

    pending, _token = await store.create_pending(
        team_id="team_a",
        base_url="https://a.example.com/bowhead",
        actor_id="usr_owner",
    )

    assert [entry.id for entry in await store.list_all()] == [pending.id]
    assert await store.list_active() == []
    assert len(_db._read_pools[str(db_path)].idle) == 1
