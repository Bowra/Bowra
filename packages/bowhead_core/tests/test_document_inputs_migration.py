"""0047 -> input retention, repeatability, preserved data and schema drift."""

import sqlite3
from importlib import import_module

import pytest
from alembic import command
from artifact_row_helpers import artifact_columns, artifact_rows_by_column
from test_document_admission_migration import _config

pytestmark = pytest.mark.integration
MIGRATION = import_module("bowhead_core.migrations.versions.0048_document_inputs")


def test_input_migration_preserves_existing_data_without_backfill(tmp_path):
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO artifacts(id,filename,mime,size,source,path,created_at) VALUES ('art_old','old.pptx','application/octet-stream',7,'upload','/synthetic','2026-09-06')"
        )
        preserved_columns = artifact_columns(db)
        before = artifact_rows_by_column(db, preserved_columns)
    command.upgrade(config, MIGRATION.revision)
    command.stamp(config, MIGRATION.down_revision)
    # Replay this revision only against its own schema. Later revisions replace
    # input guards, so relabeling a head database as 0047 would fabricate drift.
    command.upgrade(config, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        # Verify replay reached this revision (checking after stamp head is tautological and meaningless).
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with sqlite3.connect(path) as db:
        assert artifact_rows_by_column(db, preserved_columns) == before
        for table in ("document_input_sets", "document_input_copies", "document_artifact_refs"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT artifact_id FROM document_input_copies WHERE cleanup_after<=? ORDER BY cleanup_after,artifact_id LIMIT 200",
            (0,),
        ).fetchall()
        assert any("idx_document_input_cleanup" in row[3] for row in plan)


@pytest.mark.parametrize("kind", ["table", "index", "trigger"])
def test_input_schema_drift_fails_upgrade(tmp_path, kind):
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, MIGRATION.revision)
    command.stamp(config, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        if kind == "table":
            db.execute("ALTER TABLE document_input_copies ADD COLUMN unexpected TEXT")
        elif kind == "index":
            db.execute("DROP INDEX idx_document_refs_active")
            db.execute("CREATE INDEX idx_document_refs_active ON document_artifact_refs(owner_id)")
        else:
            db.execute("DROP TRIGGER trg_document_ref_purge")
            db.execute(
                "CREATE TRIGGER trg_document_ref_purge BEFORE INSERT ON artifact_purge_claims WHEN 0 BEGIN SELECT RAISE(ABORT,'disabled'); END"
            )
    with pytest.raises(RuntimeError, match="schema drift"):
        command.upgrade(config, "head")
