"""Sprint ι r2 P1 #1: artifact_stdout MUST CASCADE-delete with parent artifact.

Failing this test usually means a new aiosqlite.connect() call site bypassed
the _open_db() helper (PRAGMA foreign_keys default is OFF on SQLite).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiosqlite
import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend, _open_db

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.asyncio


async def test_cascade_deletes_stdout_when_artifact_hard_deleted(tmp_path: Path) -> None:
    """When parent artifact hard-deleted, child stdout row should CASCADE."""
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    from bowhead_core import migrations

    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{tmp_path / 'test.db'}")
    config.attributes["bowhead_target"] = "core"
    command.upgrade(config, "head")
    store = ArtifactStore(
        db_path=str(tmp_path / "test.db"),
        backend=LocalBackend(root_dir=tmp_path / "files"),
    )
    await store.initialize()

    art = await store.put_bytes(
        content=b"x,y\n1,2\n",
        filename="test.csv",
        mime="text/csv",
        source="python_sandbox",
        created_by="user_A",
    )
    async with _open_db(store.db_path) as db:
        await db.execute(
            "INSERT INTO artifact_stdout(artifact_id, stdout, created_at) VALUES (?, ?, ?)",
            (art.id, "hello stdout", "2026-05-09T00:00:00Z"),
        )
        await db.commit()

    assert await store.soft_delete(art.id, user_id="user_A")
    # Force backdate to bypass 30-day window.
    async with _open_db(store.db_path) as db:
        await db.execute(
            "UPDATE artifacts SET deleted_at = ? WHERE id = ?",
            ("1970-01-01T00:00:00Z", art.id),
        )
        await db.commit()
    res = await store.hard_delete_old_soft_deleted(days=0)
    assert res["rows_removed"] == 1

    # FK CASCADE deleted the stdout row.
    async with _open_db(store.db_path) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM artifact_stdout WHERE artifact_id = ?",
            (art.id,),
        )
        row = await cur.fetchone()
    assert row is not None and row[0] == 0, "artifact_stdout row should CASCADE-delete with parent artifact"


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
