"""Ranking expansion preserves history on all deployment database targets."""

import sqlite3
from importlib import import_module

import pytest
from alembic import command
from test_office_upload_confirmation_migration import _config

pytestmark = pytest.mark.integration
REVISION = import_module("bowhead_core.migrations.versions.0146_agent_ranking_snapshots")


@pytest.mark.parametrize("target", ["core", "files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"])
def test_upgrade_preserves_existing_rows_and_does_not_invent_rankings(tmp_path, target):
    path = tmp_path / "existing.db"
    config = _config(path, target)
    command.upgrade(config, REVISION.down_revision)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE ranking_upgrade_control(value TEXT)")
        db.execute("INSERT INTO ranking_upgrade_control VALUES('preserved')")
    command.upgrade(config, REVISION.revision)
    command.upgrade(config, REVISION.revision)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (REVISION.revision,)
        assert db.execute("SELECT value FROM ranking_upgrade_control").fetchone() == ("preserved",)
        if target == "core":
            assert db.execute("SELECT COUNT(*) FROM agent_ranking_snapshot").fetchone() == (0,)
            assert db.execute(
                "SELECT julianday(complete_days_from)=julianday('now','start of day','+1 day') FROM agent_ranking_coverage"
            ).fetchone() == (1,)
        else:
            assert db.execute("SELECT name FROM sqlite_master WHERE name='agent_ranking_snapshot'").fetchone() is None


def test_failed_expansion_leaves_no_partial_coverage_or_revision(tmp_path, monkeypatch):
    from sqlalchemy.engine import Connection

    path = tmp_path / "existing.db"
    config = _config(path, "core")
    command.upgrade(config, REVISION.down_revision)
    original = Connection.exec_driver_sql

    def fail(self, statement, *args, **kwargs):
        if "CREATE TABLE agent_ranking_snapshot" in statement:
            raise RuntimeError("injected ranking DDL failure")
        return original(self, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "exec_driver_sql", fail)
    with pytest.raises(RuntimeError, match="injected"):
        command.upgrade(config, REVISION.revision)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='agent_ranking_coverage'").fetchone() is None
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (REVISION.down_revision,)
