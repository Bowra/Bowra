"""Explicit database-only authority retries release failed transactions first."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from bowhead_core import _db, sqlite_capacity, sqlite_domain_runtime

pytestmark = pytest.mark.integration


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Path]:
    await _db.close_pooled_readers()
    baseline = sqlite_capacity._used
    path = tmp_path / "authority-write.db"
    async with _db.open_shared_ctx(str(path)) as db:
        await db.execute("CREATE TABLE permission(allowed INTEGER NOT NULL)")
        await db.execute("INSERT INTO permission VALUES(1)")
        await db.execute("CREATE TABLE accepted(id TEXT PRIMARY KEY)")
        await db.commit()
    try:
        yield path
    finally:
        await _db.close_pooled_readers()
        assert sqlite_capacity._used == baseline


async def accepted_count(path: Path) -> int:
    async with _db.open_shared_ctx(str(path)) as db, db.execute("SELECT COUNT(*) FROM accepted") as cursor:
        return (await cursor.fetchone())[0]


@pytest.mark.parametrize("revoked", [False, True])
async def test_authority_retry_rolls_back_and_rechecks_committed_scope(database: Path, monkeypatch: pytest.MonkeyPatch, revoked: bool) -> None:
    attempts = 0

    async def operation(db):
        nonlocal attempts
        attempts += 1
        async with db.execute("SELECT allowed FROM permission") as cursor:
            if not (await cursor.fetchone())[0]:
                raise PermissionError("Current scope was revoked")
        await db.execute("INSERT INTO accepted VALUES('original-operation')")
        if attempts == 1:
            raise sqlite_domain_runtime._busy()
        return "accepted"

    async def release_and_change_scope(_delay):
        async with _db.open_shared_ctx(str(database)) as other:
            await other.execute("BEGIN IMMEDIATE")
            await other.execute("UPDATE permission SET allowed=?", (int(not revoked),))
            await other.commit()
        assert await accepted_count(database) == 0

    monkeypatch.setattr(_db, "anyio", SimpleNamespace(sleep=release_and_change_scope))
    if revoked:
        with pytest.raises(PermissionError, match="scope was revoked"):
            await _db.run_sqlite_authority_write(database, operation)
    else:
        assert await _db.run_sqlite_authority_write(database, operation) == "accepted"
    assert attempts == 2
    assert await accepted_count(database) == int(not revoked)


@pytest.mark.parametrize("kind", ["native_busy", "integrity"])
async def test_unmarked_sqlite_failures_are_not_retried(database: Path, kind: str) -> None:
    failure = sqlite3.OperationalError("controlled native busy") if kind == "native_busy" else sqlite3.IntegrityError("controlled constraint")
    if kind == "native_busy":
        failure.sqlite_errorcode = sqlite3.SQLITE_BUSY
    attempts = 0

    async def operation(db):
        nonlocal attempts
        attempts += 1
        await db.execute("INSERT INTO accepted VALUES('original-operation')")
        raise failure

    with pytest.raises(type(failure)) as caught:
        await _db.run_sqlite_authority_write(database, operation)
    assert caught.value is failure and attempts == 1
    assert await accepted_count(database) == 0


async def test_authority_timeout_preserves_the_original_error(database: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    monkeypatch.setattr(_db, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    failure = sqlite_domain_runtime._busy()

    async def operation(db):
        await db.execute("INSERT INTO accepted VALUES('original-operation')")
        clock[0] = 6.0
        raise failure

    with pytest.raises(sqlite3.OperationalError) as caught:
        await _db.run_sqlite_authority_write(database, operation)
    assert caught.value is failure
    assert await accepted_count(database) == 0


async def test_new_attempt_uses_only_the_remaining_timeout(database: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock, observed = [0.0], []
    monkeypatch.setattr(_db, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def operation(db):
        async with db.execute("PRAGMA busy_timeout") as cursor:
            observed.append((await cursor.fetchone())[0])
        if len(observed) == 1:
            clock[0] = 4.0
            raise sqlite_domain_runtime._busy()
        return "accepted"

    async def pause(delay):
        clock[0] += delay

    monkeypatch.setattr(_db, "anyio", SimpleNamespace(sleep=pause))
    assert await _db.run_sqlite_authority_write(database, operation) == "accepted"
    assert observed[0] == 5000 and 0 < observed[1] <= 1000
    async with _db.open_shared_ctx(str(database)) as db, db.execute("PRAGMA busy_timeout") as cursor:
        assert (await cursor.fetchone())[0] == 5000


@pytest.mark.parametrize("phase", ["commit", "rollback"])
async def test_transaction_settlement_failures_are_never_retried(database: Path, monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    failure = sqlite_domain_runtime._busy()
    attempts = 0
    original = _db.open_shared_ctx

    class Connection:
        def __init__(self, raw):
            self.raw = raw

        def execute(self, *args):
            return self.raw.execute(*args)

        async def commit(self):
            await self.raw.commit()
            if phase == "commit":
                raise failure

        async def rollback(self):
            await self.raw.rollback()
            raise failure

    @asynccontextmanager
    async def settlement_failure(path):
        async with original(path) as db:
            yield Connection(db)

    async def operation(db):
        nonlocal attempts
        attempts += 1
        await db.execute("INSERT INTO accepted VALUES('original-operation')")
        if phase == "rollback":
            raise sqlite_domain_runtime._busy()

    monkeypatch.setattr(_db, "open_shared_ctx", settlement_failure)
    with pytest.raises(sqlite3.OperationalError) as caught:
        await _db.run_sqlite_authority_write(database, operation)
    monkeypatch.setattr(_db, "open_shared_ctx", original)
    assert caught.value is failure and attempts == 1
    assert await accepted_count(database) == int(phase == "commit")


@pytest.mark.parametrize("when", ["operation", "retry_pause"])
async def test_cancellation_never_restarts_the_operation(database: Path, monkeypatch: pytest.MonkeyPatch, when: str) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    attempts = 0

    async def operation(db):
        nonlocal attempts
        attempts += 1
        await db.execute("INSERT INTO accepted VALUES('original-operation')")
        if when == "retry_pause":
            raise sqlite_domain_runtime._busy()
        entered.set()
        await release.wait()

    async def pause(_delay):
        assert await accepted_count(database) == 0
        entered.set()
        await release.wait()

    monkeypatch.setattr(_db, "anyio", SimpleNamespace(sleep=pause))
    pending = asyncio.create_task(_db.run_sqlite_authority_write(database, operation))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
    assert attempts == 1
    assert await accepted_count(database) == 0
