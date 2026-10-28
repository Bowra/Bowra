"""``open_shared_ctx`` connection reuse.

Each case pins one rule a returned connection must satisfy; if it cannot, the connection
is closed rather than handing the previous borrower's state to the next one.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import aiosqlite
import pytest
from bowhead_core._db import close_pooled_readers, open_shared_ctx

from bowhead_core import _db

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@pytest.fixture(autouse=True)
async def _empty_pool() -> AsyncIterator[None]:
    await close_pooled_readers()
    yield
    await close_pooled_readers()


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS parent (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE IF NOT EXISTS child (id TEXT PRIMARY KEY, parent_id TEXT NOT NULL REFERENCES parent(id))")
    conn.executemany("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", [("a", "1"), ("b", "2"), ("c", "3")])
    conn.commit()
    conn.close()


async def _value(path: Path, key: str = "a") -> tuple[aiosqlite.Connection, str]:
    async with open_shared_ctx(str(path)) as db, db.execute("SELECT v FROM kv WHERE k=?", (key,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    return db, str(row[0])


def _write_elsewhere(path: Path, value: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("UPDATE kv SET v=? WHERE k='a'", (value,))
    conn.commit()
    conn.close()


async def test_connection_is_reused_and_sees_later_commits(tmp_path: Path) -> None:
    db_path = tmp_path / "bowhead.db"
    _make_db(db_path)
    first, v1 = await _value(db_path)
    _write_elsewhere(db_path, "changed")
    second, v2 = await _value(db_path)
    assert first is second
    assert (v1, v2) == ("1", "changed")
    assert first._thread.daemon is True


async def test_writes_through_the_pool_commit_and_are_visible(tmp_path: Path) -> None:
    db_path = tmp_path / "bowhead.db"
    _make_db(db_path)
    async with open_shared_ctx(str(db_path)) as db:
        await db.execute("UPDATE kv SET v='w' WHERE k='a'")
        await db.commit()
    with sqlite3.connect(db_path) as check:
        assert check.execute("SELECT v FROM kv WHERE k='a'").fetchone() == ("w",)


async def test_unclosed_cursor_does_not_leak_an_old_snapshot(tmp_path: Path) -> None:
    """上一个借用者没读完也没关的游标持有 WAL 读快照：不关掉它，下一个借用者读到的是旧数据。"""
    db_path = tmp_path / "bowhead.db"
    _make_db(db_path)
    async with open_shared_ctx(str(db_path)) as db:
        cur = await db.execute("SELECT k FROM kv ORDER BY k")
        assert await cur.fetchone() == ("a",)  # Two rows remain unread; the statement is still active.
        leaked = db
    _write_elsewhere(db_path, "fresh")
    again, value = await _value(db_path)
    assert again is leaked
    assert value == "fresh"
    async with open_shared_ctx(str(db_path)) as db:  # An unreleased old snapshot makes write-lock upgrades fail with SQLITE_BUSY_SNAPSHOT.
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("UPDATE kv SET v='after' WHERE k='b'")
        await db.commit()


async def test_block_that_raised_closes_its_connection(tmp_path: Path) -> None:
    db_path = tmp_path / "bowhead.db"
    _make_db(db_path)
    with pytest.raises(RuntimeError):
        async with open_shared_ctx(str(db_path)) as db:
            failed = db
            async with db.execute("SELECT v FROM kv") as cur:
                await cur.fetchall()
            raise RuntimeError("boom")  # No uncommitted transaction or open cursor: only the error remains as a reason.
    other, value = await _value(db_path)
    assert other is not failed
    assert value == "1"


async def test_connection_left_in_transaction_is_closed_and_rolled_back(tmp_path: Path) -> None:
    db_path = tmp_path / "bowhead.db"
    _make_db(db_path)
    async with open_shared_ctx(str(db_path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("UPDATE kv SET v='uncommitted' WHERE k='a'")
        leaked = db
    other, value = await _value(db_path)
    assert other is not leaked
    assert value == "1"


async def test_pragmas_changed_by_a_borrower_are_reset(tmp_path: Path) -> None:
    db_path = tmp_path / "bowhead.db"
    _make_db(db_path)
    async with open_shared_ctx(str(db_path)) as db:
        await db.execute("PRAGMA query_only=ON")
        await db.execute("PRAGMA foreign_keys=OFF")
        await db.execute("PRAGMA synchronous=FULL")
        await db.execute("PRAGMA busy_timeout=1")
        db.row_factory = aiosqlite.Row
        changed = db
    async with open_shared_ctx(str(db_path)) as db:
        assert db is changed
        pragmas = {}
        for name in ("query_only", "foreign_keys", "synchronous", "busy_timeout", "secure_delete", "legacy_alter_table"):
            async with db.execute(f"PRAGMA {name}") as cur:
                pragmas[name] = (await cur.fetchone())[0]
        assert pragmas == {"query_only": 0, "foreign_keys": 1, "synchronous": 1, "busy_timeout": 5000, "secure_delete": 1, "legacy_alter_table": 0}
        async with db.execute("SELECT v FROM kv WHERE k='a'") as cur:
            assert type(await cur.fetchone()) is tuple  # row_factory is back to its default.
        with pytest.raises(sqlite3.IntegrityError):  # Foreign keys are actually enforced again.
            await db.execute("INSERT INTO child (id, parent_id) VALUES ('c1', 'missing')")
        await db.rollback()


async def test_connection_with_temp_objects_is_not_reused(tmp_path: Path) -> None:
    db_path = tmp_path / "bowhead.db"
    _make_db(db_path)
    async with open_shared_ctx(str(db_path)) as db:
        await db.execute("CREATE TEMP TABLE stage (x TEXT)")
        with_temp = db
    other, _ = await _value(db_path)
    assert other is not with_temp


async def test_replaced_database_file_is_never_used_through_old_handle(tmp_path: Path) -> None:
    db_path = tmp_path / "bowhead.db"
    _make_db(db_path)
    old, _ = await _value(db_path)
    for suffix in ("", "-wal", "-shm"):
        (tmp_path / f"bowhead.db{suffix}").unlink(missing_ok=True)
    _make_db(db_path)
    _write_elsewhere(db_path, "new file")
    new, value = await _value(db_path)
    assert new is not old
    assert value == "new file"


async def test_concurrent_borrowers_get_distinct_connections_and_idle_is_capped(tmp_path: Path) -> None:
    db_path = tmp_path / "bowhead.db"
    _make_db(db_path)
    borrowed = [open_shared_ctx(str(db_path)) for _ in range(_db._SHARED_POOL_MAX_IDLE + 2)]
    conns = [await ctx.__aenter__() for ctx in borrowed]
    for ctx in borrowed:
        await ctx.__aexit__(None, None, None)
    assert len({id(conn) for conn in conns}) == _db._SHARED_POOL_MAX_IDLE + 2
    assert len(_db._shared_pools[str(db_path)].idle) == _db._SHARED_POOL_MAX_IDLE
    await close_pooled_readers()
    assert _db._shared_pools == {}


async def test_reused_connection_skips_the_schema_parse(tmp_path: Path) -> None:
    """复用的意义：同一个库第二次借用不再开新连接（116 上每次新开要解析整库 schema ≈12ms CPU）。"""
    db_path = tmp_path / "bowhead.db"
    _make_db(db_path)
    real_connect = aiosqlite.connect
    opened = {"n": 0}

    def counting(*args, **kwargs):
        opened["n"] += 1
        return real_connect(*args, **kwargs)

    await _value(db_path)
    _db.aiosqlite.connect = counting  # type: ignore[assignment]
    try:
        for key in ("a", "b", "c", "a", "b"):
            await _value(db_path, key)
    finally:
        _db.aiosqlite.connect = real_connect  # type: ignore[assignment]
    assert opened["n"] == 0


async def test_file_replaced_while_borrowed_is_not_returned_to_the_pool(tmp_path: Path) -> None:
    db_path = tmp_path / "bowhead.db"
    _make_db(db_path)
    async with open_shared_ctx(str(db_path)) as db:
        borrowed = db
        for suffix in ("", "-wal", "-shm"):
            (tmp_path / f"bowhead.db{suffix}").unlink(missing_ok=True)
        _make_db(db_path)
    assert not _db._shared_pools.get(str(db_path)) or borrowed not in _db._shared_pools[str(db_path)].idle


async def test_callbacks_are_cleared_before_the_scrub_runs_its_own_statements(tmp_path: Path) -> None:
    """借用者挂的 trace / authorizer 不能看到或拦下归还时的清理语句，也不能留给下一个借用者。"""
    db_path = tmp_path / "bowhead.db"
    _make_db(db_path)
    traced: list[str] = []
    async with open_shared_ctx(str(db_path)) as db:
        await db.set_trace_callback(traced.append)
        await db.set_authorizer(lambda *_args: sqlite3.SQLITE_DENY)
        first = db
    assert traced == []
    again, value = await _value(db_path)
    assert again is first  # The authorizer did not block cleanup; the connection is reused normally.
    assert value == "1"
    assert traced == []
