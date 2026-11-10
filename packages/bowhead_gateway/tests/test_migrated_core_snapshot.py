"""Business fixtures receive independent copies of the real migrated schema."""

import sqlite3

import pytest
from bowhead_core.sqlite_domain_upgrade import SOURCE_REVISION

pytestmark = pytest.mark.integration


def test_copies_preserve_schema_and_isolate_writes(tmp_path, migrated_core_snapshot, initialize_migrated_core_database):
    first, second = tmp_path / "first.db", tmp_path / "second.db"
    for path in (first, second):
        initialize_migrated_core_database(path)
    with sqlite3.connect(migrated_core_snapshot) as connection:
        schema = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    with sqlite3.connect(first) as connection:
        connection.execute("CREATE TABLE snapshot_probe (value TEXT)")
        connection.execute("INSERT INTO snapshot_probe VALUES ('private change')")
    for path in (migrated_core_snapshot, second):
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall() == schema
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (SOURCE_REVISION,)
            assert connection.execute("SELECT name FROM sqlite_master WHERE name='snapshot_probe'").fetchone() is None
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
