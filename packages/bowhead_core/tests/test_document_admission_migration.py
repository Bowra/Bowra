"""Append-only core migration, preserved old data, non-core no-op and drift."""

import sqlite3
from importlib import import_module
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from bowhead_core import migrations

pytestmark = pytest.mark.integration
MIGRATION = import_module("bowhead_core.migrations.versions.0047_document_admission")


def _config(path: Path, target: str = "core") -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = target
    return config


def test_upgrade_preserves_old_files_and_does_not_backfill_jobs(tmp_path) -> None:
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO artifacts(id,filename,mime,size,source,path,created_at) VALUES ('art_old','old.pptx','application/octet-stream',7,'upload','/synthetic','2026-09-06')"
        )
        # Later revisions add artifact columns (e.g. 0051 row_count, 0096 workspace_id);
        # compare existing data column by column using only columns present before upgrading.
        columns = ",".join(f'"{row[1]}"' for row in db.execute("PRAGMA table_info(artifacts)"))
        before = db.execute(f"SELECT {columns} FROM artifacts").fetchall()  # noqa: S608
    command.upgrade(config, MIGRATION.revision)
    # Replay at its historical schema: later revisions legitimately replace triggers.
    command.stamp(config, MIGRATION.down_revision)
    command.upgrade(config, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        # Verify replay reached this revision (checking after stamp head is tautological and meaningless).
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with sqlite3.connect(path) as db:
        assert db.execute(f"SELECT {columns} FROM artifacts").fetchall() == before  # noqa: S608
        assert db.execute("SELECT count(*) FROM document_jobs").fetchone()[0] == 0
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT job_id FROM document_job_links WHERE team_id=? AND workspace_id=? AND owner_user_id=? AND session_id=? AND job_id>? ORDER BY job_id LIMIT 50",
            ("t", "w", "u", "s", ""),
        ).fetchall()
        assert any("idx_document_links_session" in row[3] for row in plan)


@pytest.mark.parametrize("target", ["files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"])
def test_non_core_target_has_no_document_schema(tmp_path, target) -> None:
    path = tmp_path / "other.db"
    command.upgrade(_config(path, target), "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_schema WHERE name LIKE 'document_%'").fetchall() == []


@pytest.mark.parametrize("kind", ["table", "index", "trigger"])
def test_drift_is_rejected_instead_of_reusing_incompatible_objects(tmp_path, kind) -> None:
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, MIGRATION.revision)
    command.stamp(config, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        if kind == "table":
            db.execute("ALTER TABLE document_jobs ADD COLUMN unexpected TEXT")
        elif kind == "index":
            db.execute("DROP INDEX idx_document_links_session")
            db.execute("CREATE INDEX idx_document_links_session ON document_job_links(job_id)")
        else:
            db.execute("DROP TRIGGER trg_document_job_identity")
            db.execute("CREATE TRIGGER trg_document_job_identity BEFORE UPDATE ON document_jobs WHEN 0 BEGIN SELECT RAISE(ABORT,'disabled'); END")
    with pytest.raises(RuntimeError, match="schema drift"):
        command.upgrade(config, "head")
