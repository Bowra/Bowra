"""Regressions for the 116 physical connection exhaustion incident."""

import asyncio
import logging
import threading
from contextlib import AsyncExitStack, closing
from pathlib import Path

import anyio
import pytest
from bowhead_core.sqlite_domain_runtime import connect, connect_sync

from bowhead_core import _db, sqlite_capacity, sqlite_domain_runtime


async def test_capacity_report_identifies_suspended_borrowers_without_values(tmp_path, monkeypatch, caplog):
    await _db.close_pooled_readers()
    baseline = sqlite_capacity._used
    monkeypatch.setattr(sqlite_capacity, "LIMIT", baseline + 1)
    monkeypatch.setattr(sqlite_capacity, "RESERVE", 0)
    monkeypatch.setattr(sqlite_capacity, "_last_holder_report", 0)
    ready, release = asyncio.Event(), asyncio.Event()
    private_value = "synthetic-private-task-and-path"

    async def nested_database_borrow():
        async with _db.open_shared_ctx(str(tmp_path / f"{private_value}.db")):
            ready.set()
            await release.wait()

    borrower = asyncio.create_task(nested_database_borrow(), name=private_value)
    try:
        await ready.wait()
        with caplog.at_level(logging.WARNING, logger="bowhead_core.sqlite_capacity"), pytest.raises(sqlite_capacity.SQLiteCapacityError):
            await connect(tmp_path / "refused.db")
        records = [record.getMessage() for record in caplog.records if "sqlite.capacity_holder" in record.getMessage()]
        assert len(records) == 1
        assert "nested_database_borrow" in records[0] and "phase=borrowed" in records[0]
        assert private_value not in records[0]
    finally:
        release.set()
        await borrower
        await _db.close_pooled_readers()
    assert sqlite_capacity._used == baseline
    assert len(sqlite_capacity._holders) == baseline


@pytest.mark.parametrize("cancel_mode", ["task", "deadline"])
async def test_cancel_during_native_open_closes_on_worker(tmp_path, monkeypatch, cancel_mode):
    opened = threading.Event()
    resume = threading.Event()
    closed = threading.Event()
    owner = None

    class Paused(sqlite_domain_runtime.DomainConnection):
        def __init__(self, *args, **kwargs):
            nonlocal owner
            super().__init__(*args, **kwargs)
            owner = threading.get_ident()
            opened.set()
            assert resume.wait(5)

        def _close_native(self):
            assert threading.get_ident() == owner
            super()._close_native()
            closed.set()

    baseline = sqlite_capacity._used
    scope_ready = asyncio.Event()
    scope = None
    conn = connect(tmp_path / "cancel.db", factory=Paused)

    async def borrow():
        nonlocal scope
        with anyio.CancelScope() as scope:
            scope_ready.set()
            async with conn:
                pytest.fail("A cancelled open reached its borrower")

    task = asyncio.create_task(borrow())
    try:
        await scope_ready.wait()
        assert await asyncio.to_thread(opened.wait, 5)
        if cancel_mode == "task":
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
        else:
            scope.deadline = anyio.current_time()
        await asyncio.sleep(0.01)
    finally:
        resume.set()
    if cancel_mode == "task":
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    await asyncio.to_thread(conn._thread.join, 5)
    assert closed.is_set() and not conn._thread.is_alive()
    assert sqlite_capacity._used == baseline


async def test_saturation_preserves_nested_rollback_and_terminal_capacity(tmp_path, monkeypatch):
    await _db.close_pooled_readers()
    base = sqlite_capacity._used
    monkeypatch.setattr(sqlite_capacity, "LIMIT", base + 3)
    monkeypatch.setattr(sqlite_capacity, "RESERVE", 1)
    path = tmp_path / "capacity.db"
    async with _db.open_shared_ctx(str(path)) as db:
        await db.execute("CREATE TABLE outcomes (id INTEGER)")
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(_db.open_shared_ctx(str(path)))
        stack.enter_context(closing(connect_sync(path)))
        refused = connect(path)
        with pytest.raises(sqlite_capacity.SQLiteCapacityError):
            async with refused:
                pytest.fail("Ordinary opens consumed the completion reserve")
        assert refused._thread.ident is None
        async with sqlite_capacity.completion_capacity():
            with pytest.raises(sqlite_capacity.SQLiteCapacityError):
                async with _db.open_shared_ctx(str(path)) as terminal:
                    await terminal.execute("BEGIN IMMEDIATE")
                    await terminal.execute("INSERT INTO outcomes VALUES (1)")
                    connect_sync(path)
            async with _db.open_shared_ctx(str(path)) as terminal:
                await terminal.execute("INSERT INTO outcomes VALUES (2)")
                await terminal.commit()
            assert sqlite_capacity._used == base + 2
    with closing(connect_sync(path)) as check:
        assert check.execute("SELECT * FROM outcomes").fetchall() == [(2,)]
    await _db.close_pooled_readers()
    assert sqlite_capacity._used == base


async def test_burst_bounds_combined_idle_and_shutdown_closes_late_return(tmp_path: Path, monkeypatch):
    await _db.close_pooled_readers()
    baseline = sqlite_capacity._used
    monkeypatch.setattr(sqlite_capacity, "LIMIT", baseline + 10)
    monkeypatch.setattr(sqlite_capacity, "RESERVE", 1)
    paths = [str(tmp_path / f"domain-{i}.db") for i in range(4)]
    for path in paths:
        with closing(connect_sync(path)) as conn:
            conn.execute("CREATE TABLE x (id INTEGER)")
    async with AsyncExitStack() as stack:
        for path in paths:
            await stack.enter_async_context(_db.open_pooled_read_ctx(path))
            await stack.enter_async_context(_db.open_shared_ctx(path))
    assert sqlite_capacity._used - baseline <= max(1, sqlite_capacity.LIMIT // 8)
    async with _db.open_shared_ctx(paths[0]):
        await _db.close_pooled_readers()
    assert sqlite_capacity._used == baseline


async def test_partial_domain_attachment_failure_releases_physical_slot(tmp_path, monkeypatch):
    from bowhead_core import sqlite_domains

    database = tmp_path / "bowhead.db"
    with closing(connect_sync(database)):
        pass
    monkeypatch.setattr(sqlite_domains, "domain_layout_active", lambda _: True)
    baseline = sqlite_capacity._used
    with pytest.raises(FileNotFoundError, match="Required SQLite domain file"):
        async with connect(database):
            pytest.fail("A missing domain attachment must reject the open")
    assert sqlite_capacity._used == baseline


async def test_ten_nested_reads_after_a_pool_burst_under_116_limit(tmp_path, monkeypatch):
    import resource

    await _db.close_pooled_readers()
    baseline = sqlite_capacity._used
    monkeypatch.setattr(resource, "getrlimit", lambda _: (1024, 1024))
    limit = sqlite_capacity._connection_limit()
    monkeypatch.setattr(sqlite_capacity, "LIMIT", baseline + limit)
    monkeypatch.setattr(sqlite_capacity, "RESERVE", 4)
    path = tmp_path / "execution.db"
    async with AsyncExitStack() as warm:
        for _ in range(12):
            await warm.enter_async_context(_db.open_shared_ctx(str(path)))
    # All ten operations need a fresh direct snapshot while their outer connection is held.
    async with AsyncExitStack() as active:
        for _ in range(10):
            await active.enter_async_context(connect(path))
            await active.enter_async_context(connect(tmp_path / "source.db"))
    await _db.close_pooled_readers()
    assert sqlite_capacity._used == baseline


async def test_async_burst_waits_before_open_without_consuming_reserve(tmp_path, monkeypatch):
    await _db.close_pooled_readers()
    baseline = sqlite_capacity._used
    monkeypatch.setattr(sqlite_capacity, "LIMIT", baseline + 3)
    monkeypatch.setattr(sqlite_capacity, "RESERVE", 1)
    peak = baseline

    async def write(index):
        nonlocal peak
        async with connect(tmp_path / "burst.db") as db:
            peak = max(peak, sqlite_capacity._used)
            await db.execute("CREATE TABLE IF NOT EXISTS outcomes (id INTEGER)")
            await db.execute("INSERT INTO outcomes VALUES (?)", (index,))
            await db.commit()
            await asyncio.sleep(0.02)

    await asyncio.gather(*(write(index) for index in range(20)))
    assert peak <= baseline + 2
    assert sqlite_capacity._used == baseline
    with closing(connect_sync(tmp_path / "burst.db")) as check:
        assert check.execute("SELECT count(*) FROM outcomes").fetchone() == (20,)


async def test_cancel_capacity_wait_never_starts_worker_or_leaks_slot(tmp_path, monkeypatch):
    await _db.close_pooled_readers()
    baseline = sqlite_capacity._used
    monkeypatch.setattr(sqlite_capacity, "LIMIT", baseline)
    monkeypatch.setattr(sqlite_capacity, "RESERVE", 0)
    conn = connect(tmp_path / "cancel-wait.db")

    async def waiting():
        async with conn:
            pytest.fail("A saturated connection reached its borrower")

    task = asyncio.create_task(waiting())
    await asyncio.sleep(0.03)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert conn._thread.ident is None
    assert sqlite_capacity._used == baseline
    assert not (tmp_path / "cancel-wait.db").exists()
