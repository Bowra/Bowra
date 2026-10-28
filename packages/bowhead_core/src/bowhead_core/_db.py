"""Sprint ω Phase 0 — shared SQLite connection helper.

Per spike Q9 audit: 17 stores opened raw `aiosqlite.connect(...)` with default
`busy_timeout=0` against the shared `./.data/sqlite/bowhead.db`. WAL is set
once by `_ref_audit.init_ref_audit` (persistent on file), but `busy_timeout`
is **per-connection** — every store paid `SQLITE_BUSY` instantly on contention.

This module exposes:

- `bootstrap_shared_db(path)` — one-time startup hook. Sets WAL (persistent),
  creates the `_phase0_probe` table used by `phase0_self_test`. Idempotent.
- `open_shared_ctx(path)` — async context manager. All shared-db writers must
  route through this. Sets the full PRAGMA triple per-connection:
  `busy_timeout=5000`, `synchronous=NORMAL`, `foreign_keys=ON`,
  `secure_delete=ON`. Drop-in for `async with aiosqlite.connect(path) as db:`.
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

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

# Module-level bootstrap-done dict. Single-process gateway (Dockerfile.app
# uvicorn CMD lacks --workers — verified Phase 0 SRE seat r3.1) so this is
# safe. If multi-worker is ever added, replace with file-lock (`fcntl.flock`)
# OR check `PRAGMA journal_mode` before set.
_bootstrap_done: dict[str, bool] = {}


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
    async with aiosqlite.connect(path) as conn:
        # WAL is persistent on the file — `_ref_audit.init_ref_audit` also
        # sets it. Setting twice is no-op.
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA secure_delete=ON")
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
    """Drop-in replacement for `async with aiosqlite.connect(path) as db:`.

    Applies per-connection PRAGMAs:
    - `busy_timeout=5000` — wait 5s on contention before raising SQLITE_BUSY.
      Spike Q9 finding: 17 stores had default 0, two concurrent writers
      raced on single-writer lock with no wait.
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


async def _open_pooled_reader(path: str) -> aiosqlite.Connection:
    conn = aiosqlite.connect(path)
    _daemonize(conn)
    await conn
    try:
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA query_only=ON")
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
    return True


class _TrackedConnection(sqlite3.Connection):
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
    "PRAGMA busy_timeout=5000;"
    "PRAGMA synchronous=NORMAL;"
    "PRAGMA foreign_keys=ON;"
    "PRAGMA secure_delete=ON;"
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
    raw.row_factory = None
    raw.text_factory = str
    raw.isolation_level = ""
    return True


async def _scrub_for_reuse(conn: aiosqlite.Connection) -> bool:
    """Release check for pooled ``open_shared_ctx`` connections; any doubt ⇒ close it."""
    try:
        return bool(await conn._execute(_scrub_raw, conn._conn))  # noqa: SLF001 —— must run on the connection's own thread
    except Exception:  # noqa: BLE001 —— a connection we cannot scrub is closed instead of reused
        return False


async def _open_pooled_shared(path: str) -> aiosqlite.Connection:
    conn = aiosqlite.connect(path, factory=_TrackedConnection)
    _daemonize(conn)
    await conn
    try:
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA secure_delete=ON")
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
    identity = _file_identity(key)
    stale: list[aiosqlite.Connection] = []
    pool = pools.get(key)
    if pool is not None and pool.identity != identity:
        stale.extend(pool.idle)
        del pools[key]
        pool = None
    conn = pool.idle.pop() if pool is not None and pool.idle else None
    for old in stale:
        await _close_quietly(old)
    if conn is None:
        conn = await opener(key)  # type: ignore[misc]
        identity = _file_identity(key)
    completed = False
    try:
        yield conn
        completed = True
    finally:
        keep = completed and identity is not None and await reusable(conn)  # type: ignore[misc]
        to_close = _release(pools, max_idle, key, identity, conn) if keep else [conn]
        for old in to_close:
            await _close_quietly(old)


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
    if len(pool.idle) >= max_idle:
        to_close.append(conn)
    else:
        pool.idle.append(conn)
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
    pools = [*_read_pools.values(), *_shared_pools.values()]
    _read_pools.clear()
    _shared_pools.clear()
    for pool in pools:
        for conn in pool.idle:
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
