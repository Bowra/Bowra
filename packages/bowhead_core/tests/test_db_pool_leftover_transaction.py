"""A pooled connection kept after its borrower left a transaction open must hold no cross-file lease.

The pool rolls back a leftover transaction that changed no rows and keeps the connection (116, 1.7.56: such
leftovers closed about 50 execution connections a minute). Closing used to release every authority lease; a kept
connection must release them too, or an idle connection holding core's shared lease would refuse core writers.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from bowhead_core._db import close_pooled_readers, open_shared_ctx
from bowhead_core.sqlite_domain_runtime import connect_sync
from bowhead_core.sqlite_domains import domain_database_path
from test_sqlite_domain_stores import domain_directory as domain_directory

from bowhead_core import _db

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _core_exclusive_write(core: Path) -> None:
    """A fenced core update, the way an archive or permission change takes core's exclusive lease."""
    with connect_sync(core) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE workspaces SET status=CASE status WHEN 'active' THEN 'archived' ELSE 'active' END WHERE id='ws_1'").close()
        conn.rollback()


async def test_a_connection_left_reading_core_in_a_transaction_holds_no_lease_once_returned(domain_directory: Path) -> None:
    await close_pooled_readers()
    core = domain_directory / "bowhead.db"
    execution = domain_database_path(core, "execution")
    async with open_shared_ctx(str(execution)) as db:
        await db.execute("BEGIN")
        cursor = await db.execute("SELECT id FROM users ORDER BY id")  # an attached core table: core's shared lease
        assert await cursor.fetchone() is not None  # more rows remain: the cursor is still an active reader
        borrowed = db
    try:
        _core_exclusive_write(core)  # fails at once with "authority lease is busy" while a shared holder remains
    except sqlite3.OperationalError as error:  # pragma: no cover -- the failure this test guards against
        pytest.fail(f"a returned connection still holds core's lease: {error}")
    async with open_shared_ctx(str(execution)) as again:
        assert again is borrowed  # it was kept, not closed
    del cursor
    await close_pooled_readers()


async def test_a_connection_whose_release_left_a_lease_is_closed_not_kept(domain_directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # If the reset ever leaves a lease behind, the connection must be closed, which releases it.
    await close_pooled_readers()
    core = domain_directory / "bowhead.db"
    execution = domain_database_path(core, "execution")
    monkeypatch.setattr(_db, "reset_domain_connection", lambda _raw: None)
    async with open_shared_ctx(str(execution)) as db:
        await db.execute("BEGIN")
        async with db.execute("SELECT id FROM users ORDER BY id") as cursor:
            await cursor.fetchone()
        borrowed = db
    _core_exclusive_write(core)
    async with open_shared_ctx(str(execution)) as again:
        assert again is not borrowed
    await close_pooled_readers()
