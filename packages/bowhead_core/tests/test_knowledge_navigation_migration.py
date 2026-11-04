"""Navigation changes only the files ledger and are atomic on installation failure."""

import sqlite3
from importlib import import_module
from types import SimpleNamespace

import pytest
from alembic import command
from core_legacy_database import TARGET_FILES
from sqlalchemy import create_engine
from test_office_upload_confirmation_migration import _config

pytestmark = pytest.mark.integration
MIGRATION = import_module("bowhead_core.migrations.versions.0213_knowledge_navigation")


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_navigation_preserves_existing_schemas_and_advances_every_ledger(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    config = _config(path, target)
    command.upgrade(config, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        before = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
    command.upgrade(config, MIGRATION.revision)
    command.upgrade(config, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        after = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
        assert all(after[name] == sql for name, sql in before.items())
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
        assert len(after.keys() - before.keys()) == (9 if target == "files" else 0)
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_failed_navigation_installation_leaves_no_partial_tables(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'interrupted.db'}")
    with engine.connect() as db:

        class Interrupted:
            def exec_driver_sql(self, sql):
                if sql.startswith("CREATE INDEX"):
                    raise RuntimeError("navigation installation interrupted")
                return db.exec_driver_sql(sql)

        monkeypatch.setattr(
            MIGRATION, "context", SimpleNamespace(config=SimpleNamespace(attributes={"bowhead_target": "files"}), get_bind=lambda: Interrupted())
        )
        with pytest.raises(RuntimeError, match="installation interrupted"):
            MIGRATION.upgrade()
        assert db.exec_driver_sql("SELECT name FROM sqlite_master WHERE name LIKE 'kb_source_%'").fetchall() == []
