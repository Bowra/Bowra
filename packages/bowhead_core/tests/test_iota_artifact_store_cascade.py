"""Artifact connections preserve foreign-key enforcement."""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiosqlite
import pytest
from bowhead_core.artifact_store import _open_db

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.asyncio


async def test_pragma_foreign_keys_set_on_each_open_db(tmp_path: Path) -> None:
    """Belt-and-braces: every _open_db() context must have FK ON."""
    db_path = str(tmp_path / "fk.db")
    async with _open_db(db_path) as db:
        cur = await db.execute("PRAGMA foreign_keys")
        row = await cur.fetchone()
    assert row is not None and row[0] == 1, "PRAGMA foreign_keys must be ON inside _open_db()"


async def test_cascade_no_op_without_open_db(tmp_path: Path) -> None:
    """Counter-test: aiosqlite.connect() WITHOUT _open_db() does NOT CASCADE.

    Documents WHY _open_db() is mandatory. If SQLite ever defaults
    PRAGMA foreign_keys=ON in a future version, this test will start failing
    and we can simplify _open_db() (drop the PRAGMA call).
    """
    db_path = str(tmp_path / "noop.db")
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(
            """
            CREATE TABLE parent(id TEXT PRIMARY KEY);
            CREATE TABLE child(p TEXT, FOREIGN KEY(p) REFERENCES parent(id) ON DELETE CASCADE);
            """,
        )
        await db.execute("INSERT INTO parent VALUES ('a')")
        await db.execute("INSERT INTO child VALUES ('a')")
        await db.commit()
        # NO pragma set.
        await db.execute("DELETE FROM parent WHERE id = 'a'")
        await db.commit()
        cur = await db.execute("SELECT COUNT(*) FROM child")
        row = await cur.fetchone()
    # If this assert FAILS (row[0] == 0), SQLite default has changed; update _open_db().
    assert row is not None and row[0] == 1, "without PRAGMA foreign_keys=ON, CASCADE is silent no-op (SQLite default)"
