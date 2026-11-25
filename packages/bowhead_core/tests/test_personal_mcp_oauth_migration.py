"""0233 lets a personal MCP connection store a sign-in (``auth_type='oauth'``) and keeps everything else."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from importlib import import_module

import pytest
from bowhead_core.sqlite_domain_runtime import connect_sync
from bowhead_core.sqlite_domains import DOMAIN_TARGET_FILES
from bowhead_core.sqlite_upgrade import database_head
from test_context_material_recovery_migration import advance_to
from test_sqlite_domain_upgrade import directory as directory
from test_sqlite_domain_upgrade import legacy_source as legacy_source
from test_sqlite_mutation_migration import evidence

MIGRATION = import_module("bowhead_core.migrations.versions.0233_personal_mcp_oauth")
pytestmark = pytest.mark.integration


def _dependents(db: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return db.execute(
        "SELECT type,name,sql FROM sqlite_schema WHERE tbl_name='personal_mcp_connection' AND type IN ('index','trigger') ORDER BY type,name"
    ).fetchall()


def test_upgrade_widens_auth_type_and_keeps_rows_guards_and_indexes(directory):  # noqa: F811
    advance_to(directory, MIGRATION.down_revision)
    core = directory / DOMAIN_TARGET_FILES["core"]
    with closing(connect_sync(core)) as db:
        db.execute(
            "INSERT INTO personal_mcp_connection VALUES ('pcm_old','u_owner','CRM','https://crm.example/mcp','bearer','sealed','ready',3,'t','t')"
        )
        db.execute("INSERT INTO personal_mcp_tool VALUES ('pmt_old','pcm_old','search','d','{}','sha','unknown',1)")
        db.commit()
    with closing(sqlite3.connect(core)) as db:
        dependents = _dependents(db)
    assert {name for _, name, _ in dependents} >= {"personal_mcp_owner", "personal_mcp_owner_immutable"}
    before = evidence(directory)

    advance_to(directory, MIGRATION.revision)
    assert evidence(directory) == before
    assert all(database_head(directory / name) == MIGRATION.revision for name in DOMAIN_TARGET_FILES.values())
    with closing(sqlite3.connect(core)) as db:
        assert _dependents(db) == dependents
        sql = db.execute("SELECT sql FROM sqlite_schema WHERE name='personal_mcp_connection'").fetchone()[0]
        assert "'api_key','oauth'" in sql
        assert db.execute("SELECT * FROM personal_mcp_connection").fetchall() == [
            ("pcm_old", "u_owner", "CRM", "https://crm.example/mcp", "bearer", "sealed", "ready", 3, "t", "t")
        ]
        assert db.execute("PRAGMA foreign_key_check(personal_mcp_tool)").fetchall() == []
    with closing(connect_sync(core)) as db:
        db.execute(
            "INSERT INTO personal_mcp_connection VALUES ('pcm_new','u_owner','Notes','https://notes.example/mcp','oauth','sealed','authorizing',1,'t','t')"
        )
        with pytest.raises(sqlite3.DatabaseError, match="personal_mcp_owner_immutable"):
            db.execute("UPDATE personal_mcp_connection SET owner_user_id='u_other' WHERE id='pcm_old'")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO personal_mcp_connection VALUES ('pcm_bad','u_owner','X','https://x.example/mcp','basic','sealed','ready',1,'t','t')"
            )
        db.rollback()

    after = evidence(directory)
    advance_to(directory, MIGRATION.revision)
    assert evidence(directory) == after


def test_rebuild_refuses_an_unknown_table_shape(tmp_path):
    with closing(sqlite3.connect(tmp_path / "core.db")) as db:
        db.execute("CREATE TABLE personal_mcp_connection (id TEXT PRIMARY KEY, auth_type TEXT)")
        with pytest.raises(RuntimeError, match="does not match the 0100 schema"):
            MIGRATION.widen_auth_type(db.execute)
