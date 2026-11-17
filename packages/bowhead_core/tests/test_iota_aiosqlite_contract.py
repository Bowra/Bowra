"""artifact_store.py reaches its database only through ``_open_db``.

``_open_db`` borrows a pooled connection and applies the file store's settings.
A connection opened anywhere else in the module would skip both: on the 116
schema each new connection costs about 30 ms of CPU before its first statement.
"""

from __future__ import annotations

import ast
from pathlib import Path

ARTIFACT_STORE = Path(__file__).resolve().parents[1] / "src/bowhead_core/artifact_store.py"

OPENERS = frozenset({"aiosqlite.connect", "sqlite3.connect", "diagnostic_connect", "connect_sync", "open_shared_ctx", "open_pooled_read_ctx"})


def _called(node: ast.Call) -> str:
    function = node.func
    if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
        return f"{function.value.id}.{function.attr}"
    return function.id if isinstance(function, ast.Name) else ""


def test_file_store_connections_come_only_from_open_db() -> None:
    tree = ast.parse(ARTIFACT_STORE.read_text(encoding="utf-8"))
    helper = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_open_db")
    inside = set(ast.walk(helper))
    opens = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and _called(node) in OPENERS]
    elsewhere = [(_called(node), node.lineno) for node in opens if node not in inside]
    assert elsewhere == [], f"Connections must be taken through _open_db(); found {elsewhere}"
    assert [_called(node) for node in opens if node in inside] == ["open_shared_ctx"], "_open_db() borrows from the shared pool, once"


async def test_consecutive_file_store_blocks_reuse_one_connection(tmp_path, monkeypatch) -> None:
    from bowhead_core.artifact_store import _open_db

    from bowhead_core import _db

    await _db.close_pooled_readers()
    path = str(tmp_path / "files.db")
    opened = []
    connect = _db.diagnostic_connect
    monkeypatch.setattr(_db, "diagnostic_connect", lambda *args, **kwargs: opened.append(1) or connect(*args, **kwargs))
    try:
        for _ in range(3):
            async with _open_db(path) as db:
                async with db.execute("PRAGMA main.synchronous") as cursor:
                    assert (await cursor.fetchone())[0] == 2, "a file record commits with synchronous=FULL, as on a new connection"
                async with db.execute("PRAGMA foreign_keys") as cursor:
                    assert (await cursor.fetchone())[0] == 1
        assert len(opened) == 1
        async with _db.open_shared_ctx(path) as db, db.execute("PRAGMA main.synchronous") as cursor:
            assert (await cursor.fetchone())[0] == 1, "the pool's own setting is back for the next borrower"
        assert len(opened) == 1
    finally:
        await _db.close_pooled_readers()


async def test_a_failed_file_store_block_does_not_hand_its_connection_on(tmp_path, monkeypatch) -> None:
    import pytest
    from bowhead_core.artifact_store import _open_db

    from bowhead_core import _db

    await _db.close_pooled_readers()
    path = str(tmp_path / "files.db")
    opened = []
    connect = _db.diagnostic_connect
    monkeypatch.setattr(_db, "diagnostic_connect", lambda *args, **kwargs: opened.append(1) or connect(*args, **kwargs))
    try:
        async with _open_db(path) as db:
            await db.execute("CREATE TABLE records (id TEXT PRIMARY KEY)")
            await db.commit()
        with pytest.raises(RuntimeError):
            async with _open_db(path) as db:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute("INSERT INTO records VALUES ('unfinished')")
                raise RuntimeError("the block failed inside its transaction")
        async with _open_db(path) as db, db.execute("SELECT count(*) FROM records") as cursor:
            assert (await cursor.fetchone())[0] == 0, "the unfinished write was rolled back with its connection"
        assert len(opened) == 2
    finally:
        await _db.close_pooled_readers()
