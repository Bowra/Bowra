"""Durable immutable Office upload confirmation; no business-row backfill."""

from __future__ import annotations

from alembic import context

revision = "0043_office_upload_confirmation"
down_revision = "0042_catalog_localization"
branch_labels = None
depends_on = None

_OBJECTS = (
    (
        "table",
        "artifact_upload_confirmations",
        """CREATE TABLE artifact_upload_confirmations (
          artifact_id TEXT PRIMARY KEY,
          quarantine_path TEXT NOT NULL UNIQUE,
          candidate_path TEXT NOT NULL UNIQUE,
          filename TEXT NOT NULL,
          mime TEXT NOT NULL,
          declared_size INTEGER NOT NULL CHECK(declared_size > 0),
          expected_sha256 TEXT,
          user_id TEXT NOT NULL,
          workspace_id TEXT NOT NULL,
          session_id TEXT,
          upload_expires_at REAL NOT NULL,
          phase TEXT NOT NULL DEFAULT 'pending'
            CHECK(phase IN ('pending','copying','validating','ready','rejected','expired')),
          epoch INTEGER NOT NULL DEFAULT 0 CHECK(epoch >= 0),
          lease_owner TEXT,
          lease_until REAL NOT NULL DEFAULT 0,
          candidate_sha256 TEXT,
          actual_size INTEGER,
          check_profile TEXT,
          package_info_json TEXT,
          error_code TEXT,
          cleanup_after REAL NOT NULL,
          CHECK(quarantine_path <> candidate_path),
          CHECK(candidate_sha256 IS NULL OR
            (length(candidate_sha256)=64 AND candidate_sha256 NOT GLOB '*[^0-9a-f]*')),
          CHECK(expected_sha256 IS NULL OR
            (length(expected_sha256)=64 AND expected_sha256 NOT GLOB '*[^0-9a-f]*')),
          CHECK(phase <> 'rejected' OR error_code IS NOT NULL),
          CHECK(phase <> 'ready' OR (candidate_sha256 IS NOT NULL AND actual_size IS NOT NULL AND actual_size > 0
            AND check_profile IS NOT NULL AND package_info_json IS NOT NULL))
        )""",
    ),
    (
        "index",
        "idx_upload_confirmation_cleanup",
        "CREATE INDEX idx_upload_confirmation_cleanup ON artifact_upload_confirmations(cleanup_after, artifact_id)",
    ),
    (
        "trigger",
        "trg_upload_confirmation_identity",
        """CREATE TRIGGER trg_upload_confirmation_identity
        BEFORE UPDATE ON artifact_upload_confirmations
        WHEN NEW.artifact_id IS NOT OLD.artifact_id
          OR NEW.quarantine_path IS NOT OLD.quarantine_path
          OR NEW.candidate_path IS NOT OLD.candidate_path
          OR NEW.filename IS NOT OLD.filename OR NEW.mime IS NOT OLD.mime
          OR NEW.declared_size IS NOT OLD.declared_size
          OR NEW.expected_sha256 IS NOT OLD.expected_sha256
          OR NEW.user_id IS NOT OLD.user_id OR NEW.workspace_id IS NOT OLD.workspace_id
          OR NEW.session_id IS NOT OLD.session_id
          OR NEW.upload_expires_at IS NOT OLD.upload_expires_at
          OR (OLD.candidate_sha256 IS NOT NULL AND NEW.candidate_sha256 IS NOT OLD.candidate_sha256)
          OR (OLD.phase IN ('ready','rejected','expired') AND NEW.phase IS NOT OLD.phase)
          OR (OLD.phase='ready' AND (NEW.actual_size IS NOT OLD.actual_size
            OR NEW.check_profile IS NOT OLD.check_profile OR NEW.package_info_json IS NOT OLD.package_info_json))
        BEGIN SELECT RAISE(ABORT, 'upload confirmation identity is immutable'); END""",
    ),
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    for kind, name, ddl in _OBJECTS:
        sql = connection.exec_driver_sql("SELECT sql FROM sqlite_schema WHERE type=? AND name=?", (kind, name)).scalar()
        if sql is None:
            connection.exec_driver_sql(ddl)
        elif "".join(sql.lower().split()) != "".join(ddl.lower().split()):
            raise RuntimeError(f"upload confirmation schema drift: {name}")


def downgrade() -> None:
    raise NotImplementedError("Forward-only migration; retain confirmation receipts on rollback.")
