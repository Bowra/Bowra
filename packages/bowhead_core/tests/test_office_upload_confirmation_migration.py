"""Real 0042 -> head upgrade, drift rejection and immutable receipt boundaries."""

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
# Select the immutable revision under test, not a copied current-head value.
_MIGRATION = import_module("bowhead_core.migrations.versions.0043_office_upload_confirmation")


def _config(path: Path, target: str = "core") -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = target
    return config


def test_existing_upload_survives_upgrade_and_rerun_without_fake_receipt(tmp_path) -> None:
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, _MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO artifacts(id, filename, mime, size, source, path, created_by, created_at) VALUES ('art_legacy', 'old.pptx', 'application/zip', 123, 'upload', '/original', 'u_owner', '2026-09-05')"
        )
        preserved_columns = artifact_columns(db)
        original = artifact_rows_by_column(db, preserved_columns)
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with sqlite3.connect(path) as db:
        assert artifact_rows_by_column(db, preserved_columns) == original
        assert db.execute("SELECT count(*) FROM artifact_upload_confirmations").fetchone()[0] == 0
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == migrations.chain_head()


@pytest.mark.parametrize("kind", ["table", "index", "trigger"])
def test_same_name_schema_drift_fails_closed(tmp_path, kind) -> None:
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, _MIGRATION.revision)
    command.stamp(config, _MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        if kind == "table":
            db.execute("ALTER TABLE artifact_upload_confirmations ADD COLUMN unrelated TEXT")
        elif kind == "index":
            db.execute("DROP INDEX idx_upload_confirmation_cleanup")
            db.execute("CREATE INDEX idx_upload_confirmation_cleanup ON artifact_upload_confirmations(artifact_id)")
        else:
            db.execute("DROP TRIGGER trg_upload_confirmation_identity")
            db.execute(
                "CREATE TRIGGER trg_upload_confirmation_identity BEFORE UPDATE ON artifact_upload_confirmations WHEN 0 BEGIN SELECT RAISE(ABORT, 'disabled'); END"
            )
    with pytest.raises(RuntimeError, match="schema drift"):
        command.upgrade(config, "head")


def test_non_core_target_does_not_gain_document_tables(tmp_path) -> None:
    path = tmp_path / "files.db"
    command.upgrade(_config(path, "files"), "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT 1 FROM sqlite_schema WHERE name='artifact_upload_confirmations'").fetchone() is None


@pytest.fixture
def receipt_db(tmp_path):
    path = tmp_path / "core.db"
    command.upgrade(_config(path), "head")
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO artifact_upload_confirmations(artifact_id,quarantine_path,candidate_path,filename,mime,declared_size,user_id,workspace_id,upload_expires_at,cleanup_after) VALUES ('art_test','quarantine','candidate','x.pptx','application/zip',10,'u_owner','ws_1',100,200)"
        )
        yield db


@pytest.mark.parametrize(
    "assignment",
    [
        "candidate_path='other'",
        "quarantine_path='other'",
        "user_id='other'",
        "workspace_id='ws_other'",
        "session_id='s_other'",
        "filename='other.pptx'",
        "declared_size=11",
        "expected_sha256='a'",
    ],
)
def test_receipt_identity_cannot_be_retargeted(receipt_db, assignment) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        receipt_db.execute(f"UPDATE artifact_upload_confirmations SET {assignment}")


def test_ready_requires_evidence_and_freezes_proof(receipt_db) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        receipt_db.execute("UPDATE artifact_upload_confirmations SET phase='ready'")
    digest = "a" * 64
    receipt_db.execute(
        "UPDATE artifact_upload_confirmations SET phase='ready',candidate_sha256=?,actual_size=10,check_profile='test',package_info_json='{}'",
        (digest,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        receipt_db.execute("UPDATE artifact_upload_confirmations SET candidate_sha256=?", ("b" * 64,))
    with pytest.raises(sqlite3.IntegrityError):
        receipt_db.execute("UPDATE artifact_upload_confirmations SET phase='copying'")
    receipt_db.execute("UPDATE artifact_upload_confirmations SET cleanup_after=300")
