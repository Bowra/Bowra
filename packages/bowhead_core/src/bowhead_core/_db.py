"""Sprint ω Phase 0 — shared SQLite connection helper.

Raw `aiosqlite.connect(...)` inherits sqlite3’s five-second default timeout
unless explicitly overridden. WAL is persistent on the database file, while
`busy_timeout` and other connection settings are local. These helpers set them
explicitly so Store connections have consistent behavior.

This module exposes:

- `bootstrap_shared_db(path)` — one-time startup hook. Sets WAL (persistent),
  creates the `_phase0_probe` table used by `phase0_self_test`. Idempotent.
- `open_shared_ctx(path)` — async context manager. All shared-db writers must
  route through this. Sets the full PRAGMA triple per-connection:
  `busy_timeout` (``WRITER_BUSY_TIMEOUT_MS``, one minute), `synchronous=NORMAL`, `foreign_keys=ON`,
  `secure_delete=ON`. Drop-in for `async with diagnostic_connect(path) as db:`.
- `phase0_self_test(path, store_names)` — boot-time probe that each store path
  can connect + write + delete. uvicorn exits non-zero on failure (per
  `tech-design-r2.md` §3 Phase 0 protocol).

`open_shared_conn(path)` (long-lived) is NOT exposed: spike audit confirmed
the 3 hot writers (turn_log_store / canvas_store / session_store) all use the
ephemeral context-manager pattern.

- `open_pooled_read_ctx(path)` — reuse an idle **read-only** connection for hot,
  single-statement reads that run on every request (auth identity, public entry,
  resource-grant checks). Opening a connection is not free on this schema: the
  first statement on every new connection parses all of ``sqlite_master``
  (116 production: 321 tables / 841 indexes / 397 triggers, ~650KB DDL), measured
  at 15-20ms CPU per connection in the app image (~45ms on the busy host) while
  the indexed read itself costs ~0.006ms. Pooled readers run with
  ``query_only=ON``; each read is its own autocommit statement, so it sees the
  latest committed data exactly like a fresh connection would.

- ``open_shared_ctx`` itself borrows from a per-file pool of read-write
  connections instead of opening one per block. On 116 a simple chat turn opened
  231 connections (~2.8s CPU of schema parsing), 78% of them through
  ``open_shared_ctx``; idle background loops alone opened 5-13/s. Returned
  connections are scrubbed before reuse (see ``_scrub_for_reuse``); anything
  that cannot be scrubbed is closed. ``close_pooled_readers`` closes every idle
  connection at app shutdown.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import sqlite3
import threading
import time
import weakref
from collections import OrderedDict
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import anyio

from bowhead_core import sqlite_capacity
from bowhead_core.sqlite_diagnostics import clear_diagnostics, diagnostic_snapshot
from bowhead_core.sqlite_domain_runtime import DomainConnection, reset_domain_connection
from bowhead_core.sqlite_domain_runtime import connect as diagnostic_connect

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

# Module-level bootstrap-done dict. Single-process gateway (Dockerfile.app
# uvicorn CMD lacks --workers — verified Phase 0 SRE seat r3.1) so this is
# safe. If multi-worker is ever added, replace with file-lock (`fcntl.flock`)
# OR check `PRAGMA journal_mode` before set.
_bootstrap_done: dict[str, bool] = {}
_logger = logging.getLogger(__name__)
_TRANSACTION_WARNING_SECONDS = 5.0


def _await_locations(task: asyncio.Task | None) -> list[str]:
    """Return bounded code locations without inspecting arguments or locals."""
    return sqlite_capacity.await_locations(task)


def _watch_shared_transaction(conn: aiosqlite.Connection) -> Callable[[], None]:
    """Attribute a long open transaction; this does not identify its lock mode.

    The check runs on the event loop while the connection's worker may be inside a statement, so it
    never calls SQLite. From SQLite 3.53.3 ``in_transaction`` takes the connection mutex, which the
    worker holds for the whole of a statement: reading it here stopped the event loop for as long as
    a ``BEGIN IMMEDIATE`` waited for the writer, and the transaction it waited for needs the event
    loop to commit. The worker records the state after every operation instead.
    """
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    started = time.monotonic()
    borrowed_at = _await_locations(task)

    def inspect_transaction() -> None:
        nonlocal timer
        try:
            active = conn._conn.bowhead_open_transaction  # noqa: SLF001 -- A Python attribute the worker maintains, not a SQLite call.
        except ValueError:
            return
        if not active:
            timer = loop.call_later(_TRANSACTION_WARNING_SECONDS, inspect_transaction)
            return
        _logger.warning(
            "sqlite.shared_transaction_open connection=%x borrowed_ms=%d borrowed_at=%s awaiting=%s diagnostics=%s",
            id(conn),
            round((time.monotonic() - started) * 1000),
            borrowed_at,
            _await_locations(task),
            diagnostic_snapshot(conn._conn),  # noqa: SLF001 -- Read-only Python diagnostic metadata, not SQLite calls.
        )

    timer = loop.call_later(_TRANSACTION_WARNING_SECONDS, inspect_transaction)
    return lambda: timer.cancel()


async def bootstrap_shared_db(path: str) -> None:
    """One-time per-process setup:
    - PRAGMA journal_mode=WAL (persistent on the file — idempotent across boots).
    - CREATE TABLE _phase0_probe (used by `phase0_self_test`).

    Best-effort idempotent within a process via `_bootstrap_done` dict.
    The check-set is not atomic across awaits — two concurrent callers may
    both run the body once. This is safe because both operations (WAL set on
    a WAL file, `CREATE TABLE IF NOT EXISTS`) are themselves idempotent at
    the SQLite layer.
    """
    if _bootstrap_done.get(path):
        return
    async with diagnostic_connect(path) as conn:
        # WAL is persistent on the file — `_ref_audit.init_ref_audit` also
        # sets it. Setting twice is no-op.
        await conn.execute("PRAGMA main.journal_mode=WAL")
        await conn.execute("PRAGMA main.secure_delete=ON")
        # _phase0_probe — pre-create so self-test INSERT doesn't raise
        # `no such table` (r3.1 P0 catch CTO+SRE+Eng triple).
        await conn.execute("CREATE TABLE IF NOT EXISTS _phase0_probe (k TEXT PRIMARY KEY, ts REAL NOT NULL)")
        # Batch 4: remove the latently broken `admin_audit_log_no_delete_full` trigger:
        # `WHEN NEW IS NULL` is invalid in a DELETE trigger (NEW does not exist); SQLite defers
        # the error until schema re-prepare (any ALTER/DROP/executescript-DDL), making future migrations
        # fail at boot (this sprint's glossary CHECK expansion hit it once). The trigger body is
        # a no-op `SELECT 1` and enforces nothing (audit deletion is protected by the app); removal changes no behavior.
        # Remove it here during bootstrap (before all store.initialize calls), once for existing databases.
        await conn.execute("DROP TRIGGER IF EXISTS admin_audit_log_no_delete_full")
        await conn.commit()
    _bootstrap_done[path] = True


@asynccontextmanager
async def open_shared_ctx(path: str) -> AsyncIterator[aiosqlite.Connection]:
    """Drop-in replacement for `async with diagnostic_connect(path) as db:`.

    Applies per-connection PRAGMAs:
    - `busy_timeout` = ``WRITER_BUSY_TIMEOUT_MS`` — wait up to one minute for the writer lock.
      Snapshot-upgrade and application authority conflicts can fail immediately.
    - `synchronous=NORMAL` — WAL-mode-friendly (FULL is per-write fsync).
    - `foreign_keys=ON` — artifact_store precedent; reserve for FK CASCADE.
    - `secure_delete=ON` — overwrite deleted SQLite cell content.

    The connection is borrowed from a per-file pool and goes back after the block
    only if the release check passes (see ``_scrub_for_reuse``); a block that
    raised or was cancelled always closes its connection. If any PRAGMA raises
    while a new connection is set up, it is closed before re-raising.
    """
    async with _borrow(str(path), _shared_pools, _SHARED_POOL_MAX_IDLE, _open_pooled_shared, _scrub_for_reuse) as conn:
        yield conn


# ── Repeating a write that found its lock taken ──────────────────────────
# Every repeat of a busy SQLite write goes through ``repeat_while_busy``. The entry points below
# only choose which errors repeat, the wait budget and what a spent wait raises:
#
# - ``wait_for_writer``: any SQLITE_BUSY (writer lock or authority lease), up to one minute;
#   for background writes such as usage settlement. Turn event writes use the same rule.
# - ``repeat_when_authority_busy``: a store method that owns its transaction and lost the
#   authority lease; 30 seconds, then ``SQLiteStoreBusy`` (503 ``store_busy``) for a request.
# - ``run_sqlite_authority_write``: a database-only callback on a borrowed connection that lost
#   the authority lease; the connection's own busy-timeout budget.


async def repeat_while_busy[T](
    attempt: Callable[[], Awaitable[T]],
    *,
    retry: Callable[[sqlite3.OperationalError], bool],
    seconds_left: Callable[[], float | None],
    first_delay: float,
    max_delay: float,
    spent: Callable[[], BaseException] | None = None,
    log: Callable[[str, float], None] | None = None,
) -> T:
    """Repeat ``attempt`` while it fails with a busy error that ``retry`` admits.

    ``attempt`` runs one complete write and has written nothing when it raises: one transaction
    that rolled back, or one autocommit statement. It opens its own connection, so waiting holds
    no lock, snapshot or lease. An admitted ``OperationalError`` repeats after a pause that doubles
    from ``first_delay`` up to ``max_delay`` while ``seconds_left()`` stays positive; ``None`` means
    no budget is known yet. Other errors and cancellation propagate at once. A spent budget
    re-raises the last error, or raises ``spent()`` from it. ``log(phase, waited_seconds)`` hears
    ``"started"`` on the first pause and then ``"ended"`` or ``"failed"``.
    """
    started = time.monotonic()
    delay = first_delay
    waiting = False
    while True:
        try:
            result = await attempt()
        except sqlite3.OperationalError as error:
            if retry(error):
                remaining = seconds_left()
                if remaining is not None and remaining > 0:
                    if not waiting:
                        waiting = True
                        if log is not None:
                            log("started", time.monotonic() - started)
                    await anyio.sleep(min(delay, remaining))
                    delay = min(delay * 2, max_delay)
                    remaining = seconds_left()
                    if remaining is not None and remaining > 0:
                        continue
                if waiting and log is not None:
                    log("failed", time.monotonic() - started)
                if spent is not None:
                    raise spent() from error
            elif waiting and log is not None:
                log("failed", time.monotonic() - started)
            raise
        if waiting and log is not None:
            log("ended", time.monotonic() - started)
        return result


#: How long a write waits for a database's writer lock before it fails. With the earlier 5 s, one long
#: transaction of another Turn (10 s and 12 s completion writes on 116, 2026-11-15) failed unrelated
#: writes that had not started. A waiting write holds no lock, so the wait costs only its own caller's
#: time; the maintainer set it to one minute. Read-write pool connections use it as their SQLite busy
#: timeout, so every write through ``open_shared_ctx`` waits this long without a retry of its own.
WRITER_WAIT_SECONDS = 60.0
WRITER_BUSY_TIMEOUT_MS = int(WRITER_WAIT_SECONDS * 1000)


def writer_busy(error: BaseException) -> bool:
    """Whether a write found the writer lock held (any SQLITE_BUSY code, the authority lease included)."""
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(error, sqlite3.OperationalError) and isinstance(code, int) and code & 0xFF == sqlite3.SQLITE_BUSY


async def wait_for_writer[T](label: str, attempt: Callable[[], Awaitable[T]]) -> T:
    """Repeat a write that found the writer lock held, for up to ``WRITER_WAIT_SECONDS``.

    ``attempt`` must write nothing when it fails busy: one transaction that begins with
    ``BEGIN IMMEDIATE`` and rolls back on error, or one autocommit statement. It opens its own
    connection each time, so no lock or snapshot is kept while waiting.
    """
    started = time.monotonic()

    def log(phase: str, waited: float) -> None:
        if phase == "started":
            _logger.warning("sqlite.waits_for_writer label=%s", label)
        elif phase == "ended":
            _logger.info("sqlite.writer_wait_ended label=%s waited_ms=%d", label, waited * 1000)
        else:
            _logger.warning("sqlite.writer_wait_failed label=%s waited_ms=%d", label, waited * 1000)

    return await repeat_while_busy(
        attempt,
        retry=writer_busy,
        seconds_left=lambda: WRITER_WAIT_SECONDS - (time.monotonic() - started),
        first_delay=0.05,
        max_delay=0.5,
        log=log,
    )


#: How long a request's write keeps repeating after it loses the cross-file authority lease. Most
#: conflicts clear within the first repeats; the bound covers a long transaction of another Turn
#: (completion writes held their lock 10-12 s on 116) without the person seeing a busy answer.
#: The maintainer set 30 s on 2026-11-17 (it was 5 s for one day).
AUTHORITY_BUSY_WAIT_SECONDS = 30.0


def is_authority_busy(error: BaseException) -> bool:
    """Whether ``error`` is the runtime's marked cross-file authority-lease conflict."""
    return isinstance(error, sqlite3.OperationalError) and getattr(error, "bowhead_authority_busy", False) is True


class SQLiteStoreBusy(RuntimeError):
    """A write kept losing its lock for its whole wait; nothing was written.

    The gateway answers it with 503 ``store_busy`` and ``Retry-After``: the same request can
    succeed once running work releases the lock.
    """

    code = "store_busy"


def repeat_when_authority_busy[**P, T](
    spent: Callable[[], BaseException] | None = SQLiteStoreBusy,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """Repeat a complete write method that lost the cross-file authority lease.

    This is the standard for a store method that opens its own connection and runs one write
    transaction on fenced tables. A fenced write asks for an exclusive lease that fails at once
    while a running Turn holds the shared side, so without a repeat the request fails under
    ordinary load. Admit a method only when all of these hold:

    - its transaction is database-only, and it has rolled back and closed its connection by the
      time the error leaves it;
    - every attempt rechecks its preconditions (existence, revision or ETag, uniqueness,
      permission) inside its own transaction, so a repeat cannot act on a stale decision;
    - no statement runs after its commit (a follow-up read belongs to the caller), so a repeat
      never meets its own committed write.

    Only the runtime's marked conflict repeats, for up to ``AUTHORITY_BUSY_WAIT_SECONDS``. When
    the wait is spent, ``spent()`` is raised from the last conflict and nothing was written. A
    writer-lock timeout (an unmarked SQLITE_BUSY after the connection's own busy timeout) is not
    repeated but raises ``spent()`` too, since it also wrote nothing. The
    default ``SQLiteStoreBusy`` needs no route code: the gateway answers it with 503
    ``store_busy``; ``None`` re-raises the last conflict for callers that already map it.
    A database-only callback that can take a borrowed connection uses
    ``run_sqlite_authority_write`` instead.
    """

    def decorate(method: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        label = method.__qualname__

        def log(phase: str, waited: float) -> None:
            if phase == "started":
                _logger.info("sqlite.authority_busy_wait_started label=%s", label)
            elif phase == "ended":
                _logger.info("sqlite.authority_busy_wait_ended label=%s waited_ms=%d", label, waited * 1000)
            else:
                _logger.warning("sqlite.authority_busy_wait_failed label=%s waited_ms=%d", label, waited * 1000)

        @functools.wraps(method)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            deadline = time.monotonic() + AUTHORITY_BUSY_WAIT_SECONDS
            try:
                return await repeat_while_busy(
                    lambda: method(*args, **kwargs),
                    retry=is_authority_busy,
                    seconds_left=lambda: deadline - time.monotonic(),
                    first_delay=0.01,
                    max_delay=0.1,
                    spent=spent,
                    log=log,
                )
            except sqlite3.OperationalError as error:
                # The writer lock, not the lease: the attempt already waited its connection's whole
                # busy timeout (5 s unpooled, 60 s pooled), so it is not repeated. It wrote nothing,
                # so it gets the same answer as a spent lease wait instead of an unexplained 500.
                if spent is None or not writer_busy(error):
                    raise
                _logger.warning("sqlite.authority_busy_wait_failed label=%s reason=writer_lock", label)
                raise spent() from error

        return wrapper

    return decorate


async def run_sqlite_authority_write[T](path: str | Path, operation: Callable[[aiosqlite.Connection], Awaitable[T]]) -> T:
    """Run an explicitly admitted, database-only idempotent write transaction.

    The callback must not commit, perform external effects, or return live cursors.
    Only marked authority conflicts inside the callback can repeat, after full
    rollback and connection release, within the original busy-timeout budget.
    """
    deadline: float | None = None
    retryable = False

    async def attempt() -> T:
        nonlocal deadline, retryable
        retryable = False
        async with open_shared_ctx(str(path)) as db:
            if deadline is None:
                async with db.execute("PRAGMA busy_timeout") as cursor:
                    timeout_ms = (await cursor.fetchone())[0]
                deadline = time.monotonic() + timeout_ms / 1000
            else:
                remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
                # The pragma returns a row; close its cursor so it does not count as an open read (f6d822c69).
                async with db.execute(f"PRAGMA busy_timeout={remaining_ms}"):
                    pass
            await db.execute("BEGIN IMMEDIATE")
            try:
                result = await operation(db)
            except BaseException as error:
                await db.rollback()
                retryable = is_authority_busy(error)
                raise
            await db.commit()
            return result

    # Only a conflict raised by the callback itself repeats: commit, rollback and release
    # failures leave ``retryable`` false and propagate.
    return await repeat_while_busy(
        attempt,
        retry=lambda _error: retryable,
        seconds_left=lambda: None if deadline is None else deadline - time.monotonic(),
        first_delay=0.01,
        max_delay=0.1,
    )


# ── Connection pools ──────────────────────────────────────────────────────
# One mechanism, two pools: read-only readers (``open_pooled_read_ctx``) and the
# read-write connections of ``open_shared_ctx``.
# Per database file we keep a few idle connections; borrowers beyond that simply open
# (and later close) their own. A connection is reused only after its release check
# passes; a block that raised or was cancelled always closes its connection.

# Idle readers kept per database file.
_READ_POOL_MAX_IDLE = 4
# Idle read-write connections kept per database file. Must exceed the number of blocks in
# flight at once: 116 benchmark (real schema) with 16 concurrent blocks and a cap of 8 still
# paid 53ms CPU per block; with a cap of 32, 32 concurrent blocks cost 0.66ms each.
_SHARED_POOL_MAX_IDLE = 32
# Distinct database files with idle connections, per pool; least recently used files are closed.
_READ_POOL_MAX_FILES = 8
_SHARED_POOL_MAX_FILES = 16


@dataclass
class _ReadPool:
    # (st_dev, st_ino) of the file the idle connections were opened on. A file that was
    # replaced or removed since must never be used through an old handle.
    identity: tuple[int, int]
    idle: list[aiosqlite.Connection] = field(default_factory=list)


_pool_generation = 0
_read_pools: OrderedDict[str, _ReadPool] = OrderedDict()
_shared_pools: OrderedDict[str, _ReadPool] = OrderedDict()
_POOL_MAX_FILES = {id(_read_pools): _READ_POOL_MAX_FILES, id(_shared_pools): _SHARED_POOL_MAX_FILES}


def _file_identity(path: str) -> tuple[int, int] | None:
    # A bare stat() of the database file, like the one SQLite's own open() performs in C.
    # Not ``Path.stat``: borrowing a connection is not application file IO, and callers
    # that forbid file IO inside a transaction (e.g. skill admission) patch ``Path``.
    try:
        stat = os.stat(path)  # noqa: PTH116 -- See above: transaction file-IO guards treat Path.stat as application IO.
    except OSError:
        return None
    return (stat.st_dev, stat.st_ino)


async def _close_quietly(conn: aiosqlite.Connection) -> None:
    # A connection that cannot close cleanly is dropped anyway.
    with suppress(Exception):
        await conn.close()


def _daemonize(conn: aiosqlite.Connection) -> None:
    # aiosqlite starts one non-daemon worker thread per connection on first await.
    # An idle pooled connection must never keep the interpreter alive at exit.
    worker = getattr(conn, "_thread", None)
    if isinstance(worker, threading.Thread) and not worker.is_alive():
        worker.daemon = True


async def _run_setup(conn: aiosqlite.Connection, sql: str) -> None:
    """Run one connection setup statement and close its cursor before the connection is handed out.

    A cursor that is only dropped is closed later, by a task the event loop has not run yet when the
    borrower's first statement reaches the connection. Until then it counts as an open read, and a
    statement that finds an open read may not wait for a busy authority lease: it fails at once with
    "database is locked". On 116 (1.7.47, 2026-11-16) a file lookup on a newly opened pooled
    connection failed that way while another Turn held the lease, and the ``add_chart`` call that
    made it failed with it.
    """
    cursor = await conn.execute(sql)
    await cursor.close()


async def _open_pooled_reader(path: str) -> aiosqlite.Connection:
    conn = diagnostic_connect(path)
    _daemonize(conn)
    await conn
    try:
        await _run_setup(conn, "PRAGMA busy_timeout=5000")
        await _run_setup(conn, "PRAGMA query_only=ON")
    except BaseException:
        await conn.close()
        raise
    return conn


def _is_clean_reader(conn: aiosqlite.Connection) -> bool:
    try:
        return not conn.in_transaction
    except ValueError:  # already closed
        return False


async def _reader_reusable(conn: aiosqlite.Connection) -> bool:
    if not _is_clean_reader(conn):
        return False
    conn.row_factory = None
    conn.text_factory = str
    await conn._execute(reset_domain_connection, conn._conn)  # noqa: SLF001 -- Release domain snapshots before metadata reuse.
    await conn._execute(clear_diagnostics, conn._conn)  # noqa: SLF001 -- Reset metadata on the connection worker.
    return True


class _TrackedConnection(DomainConnection):
    """Remembers every cursor it hands out, so a pooled connection can close them on release.

    CPython's ``Connection.execute`` / ``executemany`` / ``executescript`` create their
    cursor in C without going through ``self.cursor()``, so all four are overridden here
    (same semantics: new cursor, run the statement on it). An unclosed cursor with rows
    left keeps a read transaction open: the next borrower would read an old WAL snapshot
    and a later ``BEGIN IMMEDIATE`` could fail with ``SQLITE_BUSY_SNAPSHOT``.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.bowhead_cursors: weakref.WeakSet[sqlite3.Cursor] = weakref.WeakSet()

    def cursor(self, *args: object, **kwargs: object) -> sqlite3.Cursor:  # type: ignore[override]
        cur = super().cursor(*args, **kwargs)  # type: ignore[arg-type]
        self.bowhead_cursors.add(cur)
        return cur

    def execute(self, sql: str, parameters: object = (), /) -> sqlite3.Cursor:  # type: ignore[override]
        return self.cursor().execute(sql, parameters)  # type: ignore[arg-type]

    def executemany(self, sql: str, parameters: object, /) -> sqlite3.Cursor:  # type: ignore[override]
        return self.cursor().executemany(sql, parameters)  # type: ignore[arg-type]

    def executescript(self, sql_script: str, /) -> sqlite3.Cursor:  # type: ignore[override]
        return self.cursor().executescript(sql_script)


# Connection state an ``open_shared_ctx`` block may have changed, back to what a fresh
# connection starts with. ``query_only`` / ``foreign_keys=OFF`` / ``synchronous=FULL`` /
# ``legacy_alter_table`` are all set by some caller in this codebase.
_SHARED_BASELINE = (
    f"PRAGMA busy_timeout={WRITER_BUSY_TIMEOUT_MS};"
    "PRAGMA main.synchronous=NORMAL;"
    "PRAGMA foreign_keys=ON;"
    "PRAGMA main.secure_delete=ON;"
    "PRAGMA query_only=OFF;"
    "PRAGMA legacy_alter_table=OFF;"
)


def _scrub_raw(raw: sqlite3.Connection) -> bool:
    """Runs on the connection's worker thread. True ⇒ the connection is as good as fresh."""
    # Callbacks first: the scrub's own statements below must not reach a borrower's
    # trace callback, be vetoed by its authorizer, or be interrupted by its progress handler.
    raw.set_authorizer(None)
    raw.set_progress_handler(None, 0)
    raw.set_trace_callback(None)
    for cur in list(getattr(raw, "bowhead_cursors", ())):
        cur.close()
    if raw.in_transaction:
        return False
    probe = raw.execute("SELECT 1 FROM temp.sqlite_master LIMIT 1")
    has_temp_objects = probe.fetchone() is not None
    probe.close()
    if has_temp_objects:
        return False
    raw.executescript(_SHARED_BASELINE)
    # The diagnostic connection follows busy_timeout only through single-statement pragmas, so a value a
    # borrower lowered (run_sqlite_authority_write's remaining budget) would otherwise outlive this scrub.
    if hasattr(raw, "bowhead_busy_timeout_ms"):
        raw.bowhead_busy_timeout_ms = WRITER_BUSY_TIMEOUT_MS
    raw.row_factory = None
    raw.text_factory = str
    raw.isolation_level = ""
    reset_domain_connection(raw)
    clear_diagnostics(raw)
    return True


async def _scrub_for_reuse(conn: aiosqlite.Connection) -> bool:
    """Release check for pooled ``open_shared_ctx`` connections; any doubt ⇒ close it."""
    try:
        return bool(await conn._execute(_scrub_raw, conn._conn))  # noqa: SLF001 —— must run on the connection's own thread
    except Exception:  # noqa: BLE001 —— a connection we cannot scrub is closed instead of reused
        return False


async def _open_pooled_shared(path: str) -> aiosqlite.Connection:
    conn = diagnostic_connect(path, factory=_TrackedConnection)
    _daemonize(conn)
    await conn
    try:
        await _run_setup(conn, f"PRAGMA busy_timeout={WRITER_BUSY_TIMEOUT_MS}")
        await _run_setup(conn, "PRAGMA main.synchronous=NORMAL")
        await _run_setup(conn, "PRAGMA foreign_keys=ON")
        await _run_setup(conn, "PRAGMA main.secure_delete=ON")
    except BaseException:
        await conn.close()
        raise
    return conn


@asynccontextmanager
async def _borrow(
    key: str,
    pools: OrderedDict[str, _ReadPool],
    max_idle: int,
    opener: Callable[[str], AsyncIterator[aiosqlite.Connection]] | Callable[[str], object],
    reusable: Callable[[aiosqlite.Connection], object],
) -> AsyncIterator[aiosqlite.Connection]:
    generation = _pool_generation
    identity = _file_identity(key)
    stale: list[aiosqlite.Connection] = []
    pool = pools.get(key)
    if pool is not None and pool.identity != identity:
        stale.extend(pool.idle)
        del pools[key]
        pool = None
    conn = pool.idle.pop() if pool is not None and pool.idle else None
    if stale:
        await sqlite_capacity.finish_cleanup(_close_all(stale))
    if conn is None:
        conn = await opener(key)  # type: ignore[misc]
        identity = _file_identity(key)
    conn.open_slot.track("borrowed")
    stop_watch = _watch_shared_transaction(conn) if pools is _shared_pools else None
    completed = False
    try:
        yield conn
        completed = True
    finally:
        if stop_watch is not None:
            stop_watch()

        async def release() -> None:
            try:
                keep = completed and identity is not None and await reusable(conn) and generation == _pool_generation  # type: ignore[misc]
            except BaseException:
                await _close_quietly(conn)
                raise
            to_close = _release(pools, max_idle, key, identity, conn) if keep else [conn]
            for old in to_close:
                await _close_quietly(old)

        # Drain cleanup before propagating either deadline or direct Task cancellation.
        await sqlite_capacity.finish_cleanup(release())


def _release(
    pools: OrderedDict[str, _ReadPool],
    max_idle: int,
    key: str,
    identity: tuple[int, int] | None,
    conn: aiosqlite.Connection,
) -> list[aiosqlite.Connection]:
    """Put ``conn`` back into its pool; list what must be closed instead."""
    if identity is None or _file_identity(key) != identity:
        return [conn]
    to_close: list[aiosqlite.Connection] = []
    pool = pools.get(key)
    if pool is None or pool.identity != identity:
        if pool is not None:
            to_close.extend(pool.idle)
        pool = _ReadPool(identity=identity)
        pools[key] = pool
    pools.move_to_end(key)
    combined_idle = sum(len(entry.idle) for registry in (_read_pools, _shared_pools) for entry in registry.values())
    slot = getattr(conn, "open_slot", None)
    if (slot and slot.reserve_only) or len(pool.idle) >= max_idle or combined_idle >= min(32, max(1, sqlite_capacity.LIMIT // 8)):
        to_close.append(conn)
    else:
        pool.idle.append(conn)
    conn.open_slot.track("idle")
    while len(pools) > _POOL_MAX_FILES[id(pools)]:
        _evicted_key, evicted = pools.popitem(last=False)
        to_close.extend(evicted.idle)
    return to_close


@asynccontextmanager
async def open_pooled_read_ctx(path: str | Path) -> AsyncIterator[aiosqlite.Connection]:
    """Borrow a read-only connection; it goes back to a small per-file idle pool.

    Contract for callers: read only (``query_only`` rejects writes), and close every
    cursor (``async with db.execute(...) as cur``) so no statement keeps an old read
    snapshot alive on a connection the next caller will reuse. Callers may set
    ``row_factory``; it is reset before reuse. A connection whose block raised or was
    cancelled, or that is left inside a transaction, is closed instead of reused.
    """
    async with _borrow(str(path), _read_pools, _READ_POOL_MAX_IDLE, _open_pooled_reader, _reader_reusable) as conn:
        yield conn


async def close_pooled_readers() -> None:
    """Close every idle pooled connection, read-only and read-write (app shutdown, test isolation)."""
    global _pool_generation
    _pool_generation += 1
    pools = [*_read_pools.values(), *_shared_pools.values()]
    _read_pools.clear()
    _shared_pools.clear()
    await sqlite_capacity.finish_cleanup(_close_all([conn for pool in pools for conn in pool.idle]))


async def _close_all(connections: list[aiosqlite.Connection]) -> None:
    for conn in connections:
        await _close_quietly(conn)


async def phase0_self_test(path: str, store_names: list[str]) -> None:
    """Boot-time probe: each `store_name` triggers a connect + write + delete
    against `_phase0_probe`. Raises if any step fails. Called from
    `bowhead_gateway.main` startup; uvicorn exits non-zero on raise per
    `tech-design-r2.md` §3 Phase 0 protocol.

    PK collision mitigation (r3.1 P0 Eng catch): each insert uses
    `INSERT OR REPLACE` so a stale row from a partial-fail prior boot
    (crash between INSERT and DELETE) doesn't crash-loop the container.
    """
    async with open_shared_ctx(path) as conn:
        for name in store_names:
            # INSERT OR REPLACE: defends against partial-fail leftover row
            # (Eng P0 catch: plain INSERT with PRIMARY KEY raises on retry)
            await conn.execute(
                "INSERT OR REPLACE INTO _phase0_probe (k, ts) VALUES (?, ?)",
                (name, time.time()),
            )
            await conn.execute("DELETE FROM _phase0_probe WHERE k = ?", (name,))
        await conn.commit()


def _reset_bootstrap_for_test() -> None:
    """UT helper — reset the module-level bootstrap-done dict between tests."""
    _bootstrap_done.clear()
