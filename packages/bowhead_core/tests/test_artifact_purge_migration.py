"""Previous revision -> head, durable deletion identity and reference fences."""

from __future__ import annotations

import sqlite3
from importlib import import_module
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from artifact_row_helpers import artifact_columns, artifact_rows_by_column

from bowhead_core import migrations

pytestmark = pytest.mark.integration
_MIGRATION = import_module("bowhead_core.migrations.versions.0046_artifact_purge_claims")


def _config(path: Path, target: str = "core") -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = target
    return config


def _artifact(db: sqlite3.Connection, *, aid="art_old", path="/synthetic/key", deleted="2020-01-01") -> None:
    db.execute(
        "INSERT INTO artifacts(id,filename,mime,size,source,path,created_by,created_at,deleted_at) "
        "VALUES (?,'old.txt','text/plain',4,'upload',?,'u_owner','2020-01-01',?)",
        (aid, path, deleted),
    )


def test_upgrade_keeps_existing_files_without_backfilled_claims(tmp_path) -> None:
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, _MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        _artifact(db)
        preserved_columns = artifact_columns(db)
        before = artifact_rows_by_column(db, preserved_columns)
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with sqlite3.connect(path) as db:
        assert artifact_rows_by_column(db, preserved_columns) == before
        assert db.execute("SELECT count(*) FROM artifact_purge_claims").fetchone()[0] == 0
        plan = db.execute("EXPLAIN QUERY PLAN SELECT id FROM artifacts WHERE path=?", ("/synthetic/key",)).fetchall()
        assert any("idx_artifacts_storage_path" in row[3] for row in plan)
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == migrations.chain_head()


@pytest.mark.parametrize("target", ["files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"])
def test_non_core_migration_is_noop(tmp_path, target) -> None:
    path = tmp_path / "other.db"
    command.upgrade(_config(path, target), "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT 1 FROM sqlite_schema WHERE name='artifact_purge_claims'").fetchone() is None


@pytest.mark.parametrize("kind", ["table", "index", "trigger"])
def test_schema_drift_rejected(tmp_path, kind) -> None:
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, _MIGRATION.revision)
    command.stamp(config, _MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        if kind == "table":
            db.execute("ALTER TABLE artifact_purge_claims ADD COLUMN unexpected TEXT")
        elif kind == "index":
            db.execute("DROP INDEX idx_artifact_purge_pending")
            db.execute("CREATE INDEX idx_artifact_purge_pending ON artifact_purge_claims(path)")
        else:
            db.execute("DROP TRIGGER trg_session_attachments_purge_update")
            db.execute(
                "CREATE TRIGGER trg_session_attachments_purge_update BEFORE UPDATE ON session_attachments WHEN 0 BEGIN SELECT RAISE(ABORT,'disabled'); END"
            )
    with pytest.raises(RuntimeError, match="schema drift"):
        command.upgrade(config, "head")


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "core.db"
    command.upgrade(_config(path), "head")
    with sqlite3.connect(path) as db:
        _artifact(db)
        db.execute(
            "INSERT INTO artifact_purge_claims(artifact_id,path,created_by,claimed_at) VALUES ('art_old','/synthetic/key','u_owner','2026-09-06')"
        )
        yield db


@pytest.mark.parametrize(
    "assignment", ["artifact_id='other'", "path='/other'", "created_by='u_other'", "claimed_at='2000'", "completed_at='2026-09-06'"]
)
def test_claim_cannot_change_identity_or_complete_while_artifact_exists(db, assignment) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
        db.execute(f"UPDATE artifact_purge_claims SET {assignment}")


def test_completed_tombstone_blocks_reuse_and_never_expires(db) -> None:
    db.execute("DELETE FROM artifacts WHERE id='art_old'")
    db.execute("UPDATE artifact_purge_claims SET completed_at='2026-09-06'")
    for statement in ("DELETE FROM artifact_purge_claims", "UPDATE artifact_purge_claims SET completed_at=NULL"):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(statement)
    for aid, path in (("art_old", "/new/path"), ("art_new", "/synthetic/key")):
        with pytest.raises(sqlite3.IntegrityError, match="purge in progress"):
            _artifact(db, aid=aid, path=path)


def test_replace_cannot_reset_a_pending_claim(db) -> None:
    before = db.execute("SELECT * FROM artifact_purge_claims").fetchone()
    # SQLite REPLACE is delete + insert, not UPDATE; it must not bypass identity.
    with pytest.raises(sqlite3.IntegrityError, match="purge unavailable"):
        db.execute(
            "INSERT OR REPLACE INTO artifact_purge_claims(artifact_id,path,created_by,claimed_at) VALUES ('art_old','/synthetic/key','u_owner','2000')"
        )
    assert db.execute("SELECT * FROM artifact_purge_claims").fetchone() == before


@pytest.mark.parametrize("table", ["session_attachments", "turn_attachments", "canvas_export_snapshot_assets"])
@pytest.mark.parametrize("operation", ["insert", "update"])
def test_new_references_and_reactivation_reject_claim_even_without_fk_enforcement(db, table, operation) -> None:
    # These legacy stores may open SQLite without foreign_keys=ON; the fence must
    # still work. Minimal raw reference fixtures exercise exactly that boundary.
    columns = {
        "session_attachments": "session_id,artifact_id,pinned_at,detached_at",
        "turn_attachments": "turn_id,artifact_id,seq",
        "canvas_export_snapshot_assets": "snapshot_id,artifact_id,section_id,role,mime,size_bytes,scope,pinned_at",
    }
    values = {
        "session_attachments": "'s_test',?,'2026-09-06',NULL",
        "turn_attachments": "'t_test',?,0",
        "canvas_export_snapshot_assets": "'exs_test',?,'sec','image','image/png',4,'user','2026-09-06'",
    }
    if operation == "insert":
        sql, params = f"INSERT INTO {table}({columns[table]}) VALUES ({values[table]})", ("art_old",)
    else:
        db.execute(f"INSERT INTO {table}({columns[table]}) VALUES ({values[table]})", ("art_other",))
        sql, params = f"UPDATE {table} SET artifact_id='art_old'", ()
    with pytest.raises(sqlite3.IntegrityError, match="purge in progress"):
        db.execute(sql, params)


def test_detached_session_reference_cannot_be_reactivated(db) -> None:
    db.execute("INSERT INTO session_attachments VALUES ('s_test','art_old','2026-09-06','2026-09-06')")
    with pytest.raises(sqlite3.IntegrityError, match="purge in progress"):
        db.execute("UPDATE session_attachments SET detached_at=NULL")


@pytest.mark.parametrize("assignment", ["deleted_at=NULL", "path='/other'", "id='new'", "created_by='other'", "canvas_id='cv_other'"])
def test_claimed_artifact_cannot_be_restored_or_retargeted(db, assignment) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="purge in progress"):
        db.execute(f"UPDATE artifacts SET {assignment}")


def test_live_or_aliased_path_cannot_be_claimed(db) -> None:
    _artifact(db, aid="art_live", path="/live", deleted=None)
    _artifact(db, aid="art_alias", path="/live")
    for aid, path in (("art_live", "/live"), ("art_alias", "/live"), ("art_missing", "/missing")):
        with pytest.raises(sqlite3.IntegrityError, match="purge unavailable"):
            db.execute("INSERT INTO artifact_purge_claims(artifact_id,path,created_by,claimed_at) VALUES (?,?,'u_owner','2026-09-06')", (aid, path))
