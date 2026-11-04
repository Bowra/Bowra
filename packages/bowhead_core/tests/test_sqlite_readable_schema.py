"""Business retention sees managed read-only domains without weakening ownership."""

from __future__ import annotations

import sqlite3

import aiosqlite
import pytest
from bowhead_core.artifact_references import active_reference_sql
from bowhead_core.documents.retention import active_ref_sql
from bowhead_core.sqlite_readable_schema import readable_schema_objects, readable_table_exists


@pytest.fixture
async def connected_domains(tmp_path):
    content = tmp_path / "content.db"
    execution = tmp_path / "execution.db"
    with sqlite3.connect(content) as db:
        db.execute("CREATE TABLE artifacts(id TEXT,kind TEXT,source TEXT,producer_kind TEXT,inject TEXT)")
        db.executemany("INSERT INTO artifacts(id) VALUES (?)", [("retained",), ("free",)])
    with sqlite3.connect(execution) as db:
        db.execute("CREATE TABLE document_artifact_refs(artifact_id TEXT,released_at TEXT)")
        db.execute("INSERT INTO document_artifact_refs VALUES ('retained',NULL)")
        db.execute("CREATE TABLE session_attachments(artifact_id TEXT,detached_at TEXT)")
    async with aiosqlite.connect(content, uri=True) as db:
        await db.execute("ATTACH DATABASE ? AS domain_execution", (execution.as_uri() + "?mode=ro",))
        await db.execute("ATTACH DATABASE ':memory:' AS unrelated")
        await db.execute("CREATE TABLE unrelated.unregistered(id TEXT)")
        yield db


@pytest.mark.asyncio
async def test_content_retention_follows_attached_execution_references(connected_domains):
    db = connected_domains
    assert await readable_table_exists(db, "document_artifact_refs")
    assert not await readable_table_exists(db, "unregistered")
    assert {"artifacts", "document_artifact_refs", "session_attachments"} <= await readable_schema_objects(db)
    for expression in (await active_ref_sql(db), await active_reference_sql(db)):
        async with db.execute(f"SELECT id FROM artifacts WHERE {expression}") as cursor:
            assert await cursor.fetchall() == [("retained",)]
    with pytest.raises(aiosqlite.OperationalError, match="readonly"):
        await db.execute("INSERT INTO document_artifact_refs VALUES ('free',NULL)")


@pytest.mark.asyncio
async def test_denied_metadata_is_not_interpreted_as_no_retention(connected_domains):
    db = connected_domains

    def deny_execution_metadata(action, table, column, database, trigger):
        if action == sqlite3.SQLITE_READ and database == "domain_execution" and table in {"sqlite_schema", "sqlite_master"}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    await db.set_authorizer(deny_execution_metadata)
    with pytest.raises(aiosqlite.DatabaseError):
        await readable_table_exists(db, "document_artifact_refs")
    with pytest.raises(aiosqlite.DatabaseError):
        await active_ref_sql(db)
