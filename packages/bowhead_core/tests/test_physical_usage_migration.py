"""Physical receipts preserve old statistics and advance all eight targets."""

import sqlite3
from importlib import import_module

import pytest
from alembic import command
from test_office_upload_confirmation_migration import _config

pytestmark = pytest.mark.integration
REVISION = import_module("bowhead_core.migrations.versions.0135_physical_usage_receipts")


@pytest.mark.parametrize("target", ["core", "files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"])
def test_upgrade_preserves_historical_usage_without_inventing_receipts(tmp_path, target):
    path = tmp_path / "existing.db"
    config = _config(path, target)
    command.upgrade(config, REVISION.down_revision)
    with sqlite3.connect(path) as db:
        if target == "core":
            db.execute("INSERT INTO llm_usage_events(id,reason,agent_type,tokens_input,created_at) VALUES('lu_old','turn','lead',23,'2026-10-01')")
    command.upgrade(config, REVISION.revision)
    command.upgrade(config, REVISION.revision)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (REVISION.revision,)
        if target == "core":
            assert db.execute("SELECT tokens_input,receipt_json,receipt_state FROM llm_usage_events WHERE id='lu_old'").fetchone() == (23, None, None)
        else:
            assert db.execute("SELECT name FROM sqlite_master WHERE name='trg_physical_usage_identity'").fetchone() is None


def test_failed_expansion_rolls_back_columns_and_revision(tmp_path, monkeypatch):
    from sqlalchemy.engine import Connection

    path = tmp_path / "existing.db"
    config = _config(path, "core")
    command.upgrade(config, REVISION.down_revision)
    original = Connection.exec_driver_sql

    def fail(self, statement, *args, **kwargs):
        if "ADD COLUMN receipt_state" in statement:
            raise RuntimeError("injected DDL failure")
        return original(self, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "exec_driver_sql", fail)
    with pytest.raises(RuntimeError, match="injected"):
        command.upgrade(config, REVISION.revision)
    with sqlite3.connect(path) as db:
        assert "receipt_json" not in {row[1] for row in db.execute("PRAGMA table_info(llm_usage_events)")}
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (REVISION.down_revision,)
