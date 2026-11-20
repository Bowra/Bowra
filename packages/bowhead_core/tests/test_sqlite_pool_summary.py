"""Pooled SQLite connection opens, reuse and closes are counted per pool and database file."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from bowhead_core._db import close_pooled_readers, open_pooled_read_ctx, open_shared_ctx

from bowhead_core import _db, sqlite_capacity, sqlite_pool_summary


class _Recorder:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def info(self, event: str, **fields: object) -> None:
        self.events.append({"event": event, **fields})

    def lines(self) -> dict[tuple[object, object], dict[str, object]]:
        return {(entry["pool"], entry["database"]): entry for entry in self.events if entry["event"] == "sqlite.pool_summary"}


@pytest.fixture
async def recorded(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Recorder]:
    await close_pooled_readers()
    sqlite_pool_summary._reset_for_test()
    recorder = _Recorder()
    monkeypatch.setattr(sqlite_pool_summary, "log", recorder)
    yield recorder
    await close_pooled_readers()
    sqlite_pool_summary._reset_for_test()


def _make_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    return path


async def _read(path: Path) -> None:
    async with open_pooled_read_ctx(path) as db, db.execute("SELECT count(*) FROM kv") as cursor:
        await cursor.fetchone()


async def _hold_reads(path: Path, count: int) -> None:
    """Borrow ``count`` readers of ``path`` at once, then return them in order."""
    async with AsyncExitStack() as stack:
        for _ in range(count):
            await stack.enter_async_context(open_pooled_read_ctx(path))


@pytest.mark.asyncio
async def test_opens_and_reuse_are_counted_per_pool_and_file(tmp_path: Path, recorded: _Recorder) -> None:
    path = _make_db(tmp_path / "alpha.db")
    await _read(path)
    await _read(path)
    async with open_shared_ctx(path) as db, db.execute("SELECT 1") as cursor:
        await cursor.fetchone()
    assert recorded.lines() == {}  # nothing is written while the window is open
    sqlite_pool_summary.flush_summary()
    lines = recorded.lines()
    read = lines[("read", "alpha.db")]
    assert (read["opened"], read["reused"], read["peak_borrowed"], read["max_idle"]) == (1, 1, 1, _db._READ_POOL_MAX_IDLE)
    assert read["closed_per_file_cap"] == read["closed_combined_cap"] == read["closed_other"] == 0
    shared = lines[("shared", "alpha.db")]
    assert (shared["opened"], shared["reused"], shared["max_idle"]) == (1, 0, _db._SHARED_POOL_MAX_IDLE)
    assert read["combined_idle_cap"] == sqlite_capacity.combined_idle_cap() == min(32, max(1, sqlite_capacity.LIMIT // 4))


@pytest.mark.asyncio
async def test_a_return_beyond_the_files_idle_limit_is_a_per_file_cap_close(
    tmp_path: Path, recorded: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_db, "_READ_POOL_MAX_IDLE", 1)
    path = _make_db(tmp_path / "beta.db")
    await _hold_reads(path, 3)
    sqlite_pool_summary.flush_summary()
    line = recorded.lines()[("read", "beta.db")]
    assert (line["opened"], line["peak_borrowed"]) == (3, 3)
    assert (line["closed_per_file_cap"], line["closed_combined_cap"]) == (2, 0)


@pytest.mark.asyncio
async def test_a_return_beyond_the_combined_idle_cap_is_a_combined_cap_close(
    tmp_path: Path, recorded: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A combined cap of 2 binds before the per-file idle limit of 8. Set the cap itself rather than LIMIT, so
    # connections other tests in this worker still hold cannot exhaust a lowered LIMIT.
    monkeypatch.setattr(sqlite_capacity, "combined_idle_cap", lambda: 2)
    path = _make_db(tmp_path / "beta.db")
    await _hold_reads(path, 3)
    sqlite_pool_summary.flush_summary()
    line = recorded.lines()[("read", "beta.db")]
    assert (line["closed_per_file_cap"], line["closed_combined_cap"], line["combined_idle_cap"]) == (0, 1, 2)


@pytest.mark.asyncio
async def test_a_failed_block_is_counted_as_another_close_and_the_next_borrow_opens(tmp_path: Path, recorded: _Recorder) -> None:
    path = _make_db(tmp_path / "gamma.db")
    with pytest.raises(RuntimeError):
        async with open_pooled_read_ctx(path):
            raise RuntimeError("boom")
    await _read(path)
    sqlite_pool_summary.flush_summary()
    line = recorded.lines()[("read", "gamma.db")]
    assert (line["opened"], line["reused"], line["closed_other"]) == (2, 0, 1)
    assert (line["other_failed_block"], line["other_not_reusable"], line["other_file_replaced"], line["other_pools_reset"]) == (1, 0, 0, 0)


@pytest.mark.asyncio
async def test_another_close_names_the_release_check_that_failed(tmp_path: Path, recorded: _Recorder) -> None:
    path = _make_db(tmp_path / "delta.db")
    async with open_shared_ctx(path) as db:
        await db.execute("CREATE TEMP TABLE stage (x TEXT)")  # temp objects make a connection unreusable
    async with open_shared_ctx(path) as db:
        await db.execute("BEGIN")
        await db.execute("CREATE TABLE kept (x TEXT)")  # a schema change counts as no row change ...
        await db.execute("INSERT INTO kept VALUES ('row')")  # ... a row change keeps the old rule: close
    async with open_shared_ctx(path) as db:
        await db.execute("BEGIN")  # a read transaction left open is ended in place and kept ...
    async with open_shared_ctx(path):  # ... so this borrow reuses it
        pass
    sqlite_pool_summary.flush_summary()
    line = recorded.lines()[("shared", "delta.db")]
    assert (line["opened"], line["reused"], line["closed_other"], line["other_not_reusable"]) == (3, 1, 2, 2)


def _in_use(path: Path) -> int:
    return sqlite_pool_summary._borrowed.get(("read", path.name), 0)


@pytest.mark.asyncio
async def test_peak_borrowed_is_the_most_connections_in_use_at_once(tmp_path: Path, recorded: _Recorder) -> None:
    path = _make_db(tmp_path / "beta.db")
    await _hold_reads(path, 2)
    await _read(path)
    assert _in_use(path) == 0
    sqlite_pool_summary.flush_summary()
    line = recorded.lines()[("read", "beta.db")]
    assert (line["opened"], line["reused"], line["peak_borrowed"]) == (2, 1, 2)


@pytest.mark.asyncio
async def test_a_borrow_cancelled_inside_its_block_is_returned(tmp_path: Path, recorded: _Recorder) -> None:
    path = _make_db(tmp_path / "alpha.db")
    inside = asyncio.Event()

    async def borrower() -> None:
        async with open_pooled_read_ctx(path):
            inside.set()
            await asyncio.sleep(60)

    task = asyncio.create_task(borrower())
    await inside.wait()
    assert _in_use(path) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _in_use(path) == 0
    sqlite_pool_summary.flush_summary()
    line = recorded.lines()[("read", "alpha.db")]
    assert (line["opened"], line["closed_other"]) == (1, 1)


@pytest.mark.asyncio
async def test_a_failed_release_still_counts_the_return(tmp_path: Path, recorded: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_release(*_args: object) -> object:
        raise RuntimeError("release broken")

    monkeypatch.setattr(_db, "_release", broken_release)
    path = _make_db(tmp_path / "alpha.db")
    with pytest.raises(RuntimeError, match="release broken"):
        await _read(path)
    assert _in_use(path) == 0
    sqlite_pool_summary.flush_summary()
    assert recorded.lines()[("read", "alpha.db")]["closed_other"] == 1


@pytest.mark.asyncio
async def test_a_connection_whose_setup_failed_is_never_counted(tmp_path: Path, recorded: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    original = _db.execute_and_close

    async def failing_setup(conn: object, sql: str) -> None:
        if "query_only" in sql:
            raise sqlite3.OperationalError("setup failed")
        await original(conn, sql)

    monkeypatch.setattr(_db, "execute_and_close", failing_setup)
    path = _make_db(tmp_path / "alpha.db")
    with pytest.raises(sqlite3.OperationalError, match="setup failed"):
        await _read(path)
    assert _in_use(path) == 0
    sqlite_pool_summary.flush_summary()
    assert recorded.lines() == {}


def test_a_window_is_written_by_the_first_event_after_it_ends_and_covers_only_its_span(recorded: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1_000.0]
    monkeypatch.setattr(sqlite_pool_summary.time, "monotonic", lambda: clock[0])
    sqlite_pool_summary.note_borrow("read", "/x/beta.db", opened=True, max_idle=4)
    sqlite_pool_summary.note_return("read", "/x/beta.db", closed_reason=None)
    clock[0] += 3 * 3600  # a quiet spell writes nothing
    assert recorded.lines() == {}
    sqlite_pool_summary.note_borrow("read", "/x/beta.db", opened=False, max_idle=4)
    [first] = recorded.lines().values()
    assert (first["opened"], first["reused"], first["window_seconds"]) == (1, 0, sqlite_pool_summary.SUMMARY_WINDOW_S)
    clock[0] += 10
    sqlite_pool_summary.flush_summary()
    second = recorded.events[-1]
    assert (second["opened"], second["reused"], second["window_seconds"]) == (0, 1, 10.0)
    # The connection borrowed in the first window and still out counts toward the next window's peak.
    assert second["peak_borrowed"] == 1


def test_nothing_is_written_when_nothing_happened(recorded: _Recorder) -> None:
    sqlite_pool_summary.flush_summary()
    sqlite_pool_summary.flush_summary()
    assert recorded.events == []


@pytest.mark.asyncio
async def test_a_failing_counter_never_breaks_a_borrow(tmp_path: Path, recorded: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    class _Broken:
        def get(self, *_args: object) -> object:
            raise RuntimeError("counter broken")

    monkeypatch.setattr(sqlite_pool_summary, "_counts", _Broken())
    path = _make_db(tmp_path / "beta.db")
    await _read(path)
    await _read(path)


def _iter_reasons() -> Iterator[str]:
    yield from sqlite_pool_summary._CLOSE_REASONS


def test_every_close_reason_of_the_pool_is_a_summary_field() -> None:
    fields = set(sqlite_pool_summary._Counts.__dataclass_fields__)
    assert set(_iter_reasons()) <= fields
