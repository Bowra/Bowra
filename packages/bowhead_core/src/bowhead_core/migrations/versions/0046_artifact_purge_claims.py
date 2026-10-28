"""Fence blob deletion against restoration and new references; no backfill."""

from __future__ import annotations

from alembic import context

revision = "0046_artifact_purge_claims"
down_revision = "0045_agent_fork"
branch_labels = None
depends_on = None

_OBJECTS = (
    (
        "table",
        "artifact_purge_claims",
        """CREATE TABLE artifact_purge_claims (
            artifact_id TEXT PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            created_by TEXT,
            claimed_at TEXT NOT NULL,
            completed_at TEXT
        )""",
    ),
    (
        "index",
        "idx_artifact_purge_pending",
        "CREATE INDEX idx_artifact_purge_pending ON artifact_purge_claims(artifact_id) WHERE completed_at IS NULL",
    ),
    (
        "index",
        "idx_artifacts_storage_path",
        "CREATE INDEX idx_artifacts_storage_path ON artifacts(path)",
    ),
    (
        "trigger",
        "trg_artifact_purge_admission",
        """CREATE TRIGGER trg_artifact_purge_admission BEFORE INSERT ON artifact_purge_claims
        WHEN NEW.completed_at IS NOT NULL
          OR EXISTS(SELECT 1 FROM artifact_purge_claims WHERE artifact_id=NEW.artifact_id OR path=NEW.path)
          OR NOT EXISTS(SELECT 1 FROM artifacts a WHERE a.id=NEW.artifact_id
            AND a.path=NEW.path AND a.created_by IS NEW.created_by AND a.deleted_at IS NOT NULL)
          OR EXISTS(SELECT 1 FROM artifacts a WHERE a.path=NEW.path AND a.id<>NEW.artifact_id)
          OR EXISTS(SELECT 1 FROM canvas_export_snapshot_assets p
            JOIN canvas_export_snapshots s ON s.id=p.snapshot_id
            WHERE p.artifact_id=NEW.artifact_id AND julianday(s.expires_at)>julianday('now'))
        BEGIN SELECT RAISE(ABORT, 'artifact purge unavailable'); END""",
    ),
    (
        "trigger",
        "trg_artifact_purge_identity",
        """CREATE TRIGGER trg_artifact_purge_identity BEFORE UPDATE ON artifact_purge_claims
        WHEN NEW.artifact_id IS NOT OLD.artifact_id OR NEW.path IS NOT OLD.path
          OR NEW.created_by IS NOT OLD.created_by OR NEW.claimed_at IS NOT OLD.claimed_at
          OR (OLD.completed_at IS NOT NULL AND NEW.completed_at IS NOT OLD.completed_at)
          OR (NEW.completed_at IS NOT NULL AND EXISTS(SELECT 1 FROM artifacts WHERE id=OLD.artifact_id))
        BEGIN SELECT RAISE(ABORT, 'artifact purge identity is immutable'); END""",
    ),
    (
        "trigger",
        "trg_artifact_purge_retained",
        """CREATE TRIGGER trg_artifact_purge_retained BEFORE DELETE ON artifact_purge_claims
        BEGIN SELECT RAISE(ABORT, 'artifact purge tombstone is retained'); END""",
    ),
    (
        "trigger",
        "trg_artifact_purge_no_recreate",
        """CREATE TRIGGER trg_artifact_purge_no_recreate BEFORE INSERT ON artifacts
        WHEN EXISTS(SELECT 1 FROM artifact_purge_claims WHERE artifact_id=NEW.id OR path=NEW.path)
        BEGIN SELECT RAISE(ABORT, 'artifact purge in progress'); END""",
    ),
    (
        "trigger",
        "trg_artifact_purge_no_restore",
        """CREATE TRIGGER trg_artifact_purge_no_restore BEFORE UPDATE ON artifacts
        WHEN (EXISTS(SELECT 1 FROM artifact_purge_claims WHERE artifact_id=OLD.id)
            AND (NEW.id IS NOT OLD.id OR NEW.path IS NOT OLD.path
                OR NEW.created_by IS NOT OLD.created_by OR NEW.canvas_id IS NOT OLD.canvas_id
                OR NEW.deleted_at IS NULL))
          OR (NEW.path IS NOT OLD.path AND EXISTS(SELECT 1 FROM artifact_purge_claims WHERE path=NEW.path))
          OR (NEW.id IS NOT OLD.id AND EXISTS(SELECT 1 FROM artifact_purge_claims WHERE artifact_id=NEW.id))
        BEGIN SELECT RAISE(ABORT, 'artifact purge in progress'); END""",
    ),
)

# Guard both INSERT and UPDATE: upsert/reactivation must not bypass the fence.
# Pre-existing session references do not override the owner's explicit trash action.
_REFERENCE_TABLES = ("session_attachments", "turn_attachments", "canvas_export_snapshot_assets")


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    objects = list(_OBJECTS)
    for table in _REFERENCE_TABLES:
        for operation in ("INSERT", "UPDATE"):
            name = f"trg_{table}_purge_{operation.lower()}"
            active = "NEW.detached_at IS NULL AND " if table == "session_attachments" else ""
            objects.append(
                (
                    "trigger",
                    name,
                    f"""CREATE TRIGGER {name} BEFORE {operation} ON {table}
                    WHEN {active}EXISTS(SELECT 1 FROM artifact_purge_claims WHERE artifact_id=NEW.artifact_id)
                    BEGIN SELECT RAISE(ABORT, 'artifact purge in progress'); END""",
                )
            )
    for kind, name, ddl in objects:
        sql = connection.exec_driver_sql("SELECT sql FROM sqlite_schema WHERE type=? AND name=?", (kind, name)).scalar()
        if sql is None:
            connection.exec_driver_sql(ddl)
        elif "".join(sql.lower().split()) != "".join(ddl.lower().split()):
            raise RuntimeError(f"artifact purge schema drift: {name}")


def downgrade() -> None:
    raise NotImplementedError("Forward-only migration; retain purge fences and tombstones on rollback.")
