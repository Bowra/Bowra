"""Bowra adds only ephemeral encrypted core rows and preserves other schemas."""

import sqlite3
from importlib import import_module

import pytest
from alembic import command
from test_office_upload_confirmation_migration import _config

pytestmark = pytest.mark.integration
MIGRATION = import_module("bowhead_core.migrations.versions.0211_bowra_connect")


@pytest.mark.parametrize("target", ["core", "files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"])
def test_bowra_migration_is_core_only_and_repeatable(tmp_path, target):
    path = tmp_path / f"{target}.db"
    config = _config(path, target)
    command.upgrade(config, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        before = db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    command.upgrade(config, MIGRATION.revision)
    command.upgrade(config, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        after = db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        if target == "core":
            assert [row for row in after if row[0] not in {"bowra_connect_pending", "idx_bowra_connect_expiry"}] == before
            assert db.execute("SELECT count(*) FROM bowra_connect_pending").fetchone() == (0,)
            with pytest.raises(sqlite3.IntegrityError):
                db.execute("INSERT INTO bowra_connect_pending VALUES('hash','team','user','import','PLAIN:secret',9999999999)")
        else:
            assert after == before


def test_interrupted_installation_rolls_back_table_and_index(tmp_path, monkeypatch):
    from importlib import import_module
    from types import SimpleNamespace

    from sqlalchemy import create_engine

    revision = import_module("bowhead_core.migrations.versions.0211_bowra_connect")
    engine = create_engine(f"sqlite:///{tmp_path / 'interrupted.db'}")
    with engine.connect() as db:

        class Interrupted:
            def exec_driver_sql(self, sql):
                if sql.startswith("CREATE INDEX"):
                    raise RuntimeError("index installation interrupted")
                return db.exec_driver_sql(sql)

        monkeypatch.setattr(
            revision, "context", SimpleNamespace(config=SimpleNamespace(attributes={"bowhead_target": "core"}), get_bind=lambda: Interrupted())
        )
        with pytest.raises(RuntimeError, match="interrupted"):
            revision.upgrade()
        assert db.exec_driver_sql("SELECT name FROM sqlite_master WHERE name='bowra_connect_pending'").fetchone() is None
