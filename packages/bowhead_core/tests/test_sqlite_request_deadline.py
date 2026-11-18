"""A save a person triggers gets one deadline from entry that also bounds its writer waits.

Design: docs/sprints/request-write-deadline-2026-11-17/tech-design.md. Real ``BEGIN IMMEDIATE``
holders, with the 30-second budget shortened.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from bowhead_core.sqlite_diagnostics import DiagnosticCursor
from bowhead_core.sqlite_domain_runtime import connect as diagnostic_connect

from bowhead_core import _db, sqlite_domain_runtime, sqlite_request_deadline

pytestmark = pytest.mark.integration


@pytest.fixture
async def path(tmp_path: Path) -> AsyncIterator[str]:
    await _db.close_pooled_readers()
    file = tmp_path / "deadline.db"
    with sqlite3.connect(file) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    yield str(file)
    await _db.close_pooled_readers()


@pytest.fixture
def writer_held(path: str) -> Iterator[sqlite3.Connection]:
    """Another connection holds the writer lock until the test releases it."""
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    yield holder
    if holder.in_transaction:
        holder.execute("ROLLBACK")
    holder.close()


async def _insert(db, value: str) -> None:
    await db.execute("BEGIN IMMEDIATE")
    await db.execute("INSERT INTO t(v) VALUES(?)", (value,))
    await db.commit()


@_db.repeat_when_authority_busy()
async def pooled_save(path: str) -> None:
    async with _db.open_shared_ctx(path) as db:
        await _insert(db, "pooled")


@_db.repeat_when_authority_busy()
async def unpooled_save(path: str) -> None:
    async with diagnostic_connect(path) as db:
        await _insert(db, "unpooled")


@_db.repeat_when_authority_busy(kind="turn")
async def turn_write(path: str) -> None:
    async with _db.open_shared_ctx(path) as db:
        await _insert(db, "turn")


async def test_a_pooled_save_stops_waiting_for_the_writer_at_its_deadline(
    path: str, writer_held: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without the deadline the pooled connection would wait its one-minute busy timeout.
    monkeypatch.setattr(_db, "AUTHORITY_BUSY_WAIT_SECONDS", 1.0)
    started = time.monotonic()
    with pytest.raises(_db.SQLiteStoreBusy):
        await pooled_save(path)
    assert time.monotonic() - started < 3


async def test_an_unpooled_save_ends_with_its_deadline_after_lease_repeats(
    path: str, writer_held: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Lease conflicts use most of the budget; the last attempt must not add the default 5 s writer wait.
    monkeypatch.setattr(_db, "AUTHORITY_BUSY_WAIT_SECONDS", 1.0)
    lease_free_at = time.monotonic() + 0.8
    original = DiagnosticCursor.execute

    def execute(self, sql, parameters=(), /):
        if sql.lstrip().upper().startswith("BEGIN IMMEDIATE") and time.monotonic() < lease_free_at:
            raise sqlite_domain_runtime._busy()
        return original(self, sql, parameters)

    monkeypatch.setattr(DiagnosticCursor, "execute", execute)
    started = time.monotonic()
    with pytest.raises(_db.SQLiteStoreBusy):
        await unpooled_save(path)
    assert time.monotonic() - started < 2


async def test_a_turn_write_keeps_its_connections_writer_wait(path: str, writer_held: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_db, "AUTHORITY_BUSY_WAIT_SECONDS", 0.3)
    asyncio.get_running_loop().call_later(1.0, writer_held.execute, "ROLLBACK")
    started = time.monotonic()
    await turn_write(path)
    assert time.monotonic() - started >= 0.9
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT v FROM t").fetchall() == [("turn",)]


async def test_the_deadline_stays_with_the_request_write(path: str) -> None:
    observed: dict[str, object] = {}

    @_db.repeat_when_authority_busy()
    async def spawning_save(p: str) -> asyncio.Task:
        async with _db.open_shared_ctx(p) as db, db.execute("PRAGMA busy_timeout") as cursor:
            observed["inside"] = (await cursor.fetchone())[0]

        @_db.repeat_when_authority_busy()
        async def nested() -> None:
            observed["nested_left"] = sqlite_request_deadline.remaining_ms()

        observed["outer_left"] = sqlite_request_deadline.remaining_ms()
        await nested()

        async def child() -> None:
            await asyncio.sleep(0.05)
            observed["child_left"] = sqlite_request_deadline.remaining_ms()
            async with diagnostic_connect(p) as db:
                observed["child_timeout_ms"] = db._conn.bowhead_busy_timeout_ms

        return asyncio.create_task(child())

    await (await spawning_save(path))
    async with _db.open_shared_ctx(path) as db, db.execute("PRAGMA busy_timeout") as cursor:
        next_borrow = (await cursor.fetchone())[0]

    assert 0 < observed["inside"] <= _db.AUTHORITY_BUSY_WAIT_SECONDS * 1000
    # A nested request write keeps the outer deadline rather than starting its own.
    assert observed["nested_left"] <= observed["outer_left"]
    # A task spawned inside the write, running after it returned, keeps the defaults.
    assert observed["child_left"] is None
    assert observed["child_timeout_ms"] == 5000
    # The pool's release scrub restored one minute for the next borrower.
    assert next_borrow == _db.WRITER_BUSY_TIMEOUT_MS
    assert sqlite_request_deadline.remaining_ms() is None
