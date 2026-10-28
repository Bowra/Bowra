"""Freeze request inputs as private artifacts with durable retention/cleanup."""

from __future__ import annotations

from alembic import context

revision = "0048_document_inputs"
down_revision = "0047_document_admission"
branch_labels = None
depends_on = None

_OBJECTS = (
    ("table", "document_input_sets", """CREATE TABLE document_input_sets (
        job_id TEXT PRIMARY KEY NOT NULL REFERENCES document_jobs(id),
        team_id TEXT NOT NULL, workspace_id TEXT NOT NULL, owner_user_id TEXT NOT NULL,
        request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
        input_count INTEGER NOT NULL CHECK(input_count BETWEEN 1 AND 32),
        created_at TEXT NOT NULL, frozen_at TEXT,
        FOREIGN KEY(job_id,team_id,workspace_id,owner_user_id) REFERENCES document_jobs(id,team_id,workspace_id,requested_by)
    )"""),
    ("table", "document_input_copies", """CREATE TABLE document_input_copies (
        artifact_id TEXT PRIMARY KEY NOT NULL, job_id TEXT NOT NULL REFERENCES document_input_sets(job_id),
        input_index INTEGER NOT NULL CHECK(input_index BETWEEN 0 AND 31),
        source_artifact_id TEXT NOT NULL, source_path TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
        filename TEXT NOT NULL, mime TEXT NOT NULL, size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 0 AND 33554432),
        sha256 TEXT NOT NULL CHECK(length(sha256)=64),
        role TEXT NOT NULL CHECK(role IN ('template','reference','data')),
        verified_at TEXT, cleanup_claimed_at TEXT, cleanup_after REAL NOT NULL,
        UNIQUE(job_id,input_index), UNIQUE(job_id,source_artifact_id)
    )"""),
    ("table", "document_artifact_refs", """CREATE TABLE document_artifact_refs (
        team_id TEXT NOT NULL, workspace_id TEXT NOT NULL, owner_user_id TEXT NOT NULL,
        owner_kind TEXT NOT NULL CHECK(owner_kind='job'), owner_id TEXT NOT NULL,
        ref_key TEXT NOT NULL, artifact_id TEXT NOT NULL,
        artifact_sha256 TEXT NOT NULL CHECK(length(artifact_sha256)=64),
        retention_class TEXT NOT NULL CHECK(retention_class='execution'), released_at TEXT,
        PRIMARY KEY(owner_kind,owner_id,ref_key),
        FOREIGN KEY(owner_id,team_id,workspace_id,owner_user_id) REFERENCES document_jobs(id,team_id,workspace_id,requested_by)
    )"""),
    ("index", "idx_document_refs_active", "CREATE INDEX idx_document_refs_active ON document_artifact_refs(artifact_id) WHERE released_at IS NULL"),
    ("index", "idx_document_input_cleanup", "CREATE INDEX idx_document_input_cleanup ON document_input_copies(cleanup_after,artifact_id)"),
    ("trigger", "trg_document_input_set_admission", """CREATE TRIGGER trg_document_input_set_admission BEFORE INSERT ON document_input_sets
        WHEN NEW.frozen_at IS NOT NULL OR NOT EXISTS(SELECT 1 FROM document_jobs j JOIN sessions s ON s.id=j.session_id
          WHERE j.id=NEW.job_id AND j.team_id=NEW.team_id AND j.workspace_id=NEW.workspace_id AND j.requested_by=NEW.owner_user_id
            AND j.request_hash=NEW.request_hash AND j.status='queued' AND s.deleted_at IS NULL
            AND json_array_length(j.request_json,'$.proposal.inputs')=NEW.input_count)
        BEGIN SELECT RAISE(ABORT,'document input set unavailable'); END"""),
    ("trigger", "trg_document_input_set_freeze", """CREATE TRIGGER trg_document_input_set_freeze BEFORE UPDATE ON document_input_sets
        WHEN NEW.job_id IS NOT OLD.job_id OR NEW.team_id IS NOT OLD.team_id OR NEW.workspace_id IS NOT OLD.workspace_id
          OR NEW.owner_user_id IS NOT OLD.owner_user_id OR NEW.request_hash IS NOT OLD.request_hash
          OR NEW.input_count IS NOT OLD.input_count OR NEW.created_at IS NOT OLD.created_at
          OR OLD.frozen_at IS NOT NULL OR NEW.frozen_at IS NULL
          OR NOT EXISTS(SELECT 1 FROM document_jobs WHERE id=OLD.job_id AND status='queued')
          OR (SELECT count(*) FROM document_input_copies c JOIN artifacts a ON a.id=c.artifact_id
               JOIN document_artifact_refs r ON r.owner_id=c.job_id AND r.artifact_id=c.artifact_id
               WHERE c.job_id=OLD.job_id AND c.verified_at IS NOT NULL AND c.cleanup_claimed_at IS NULL
                 AND r.released_at IS NULL AND a.status='ready' AND a.deleted_at IS NULL)<>OLD.input_count
        BEGIN SELECT RAISE(ABORT,'document input set cannot freeze'); END"""),
    ("trigger", "trg_document_input_copy_admission", """CREATE TRIGGER trg_document_input_copy_admission BEFORE INSERT ON document_input_copies
        WHEN NEW.verified_at IS NOT NULL OR NEW.cleanup_claimed_at IS NOT NULL
          OR NOT EXISTS(SELECT 1 FROM document_input_sets f JOIN document_jobs j ON j.id=f.job_id
            JOIN artifacts a ON a.id=NEW.artifact_id JOIN artifacts s ON s.id=NEW.source_artifact_id
            WHERE f.job_id=NEW.job_id AND f.frozen_at IS NULL AND j.status='queued'
              AND json_extract(j.request_json,'$.proposal.inputs['||NEW.input_index||'].artifact_id')=NEW.source_artifact_id
              AND json_extract(j.request_json,'$.proposal.inputs['||NEW.input_index||'].role')=NEW.role
              AND a.path=NEW.path AND a.created_by=f.owner_user_id AND a.size=NEW.size_bytes
              AND a.filename=NEW.filename AND a.mime=NEW.mime AND a.producer_kind='template_document_internal_v1'
              AND a.source='document_input' AND a.kind='tool_data' AND a.inject='list' AND a.scope='user'
              AND a.status='document_staging' AND a.deleted_at IS NULL AND a.canvas_id IS NULL AND a.created_by_turn IS NULL
              AND s.created_by=f.owner_user_id AND s.path=NEW.source_path AND s.deleted_at IS NULL AND s.status='ready'
              AND COALESCE(s.producer_kind,'')<>'template_document_internal_v1')
        BEGIN SELECT RAISE(ABORT,'document input copy unavailable'); END"""),
    ("trigger", "trg_document_input_copy_progress", """CREATE TRIGGER trg_document_input_copy_progress BEFORE UPDATE ON document_input_copies
        WHEN NEW.artifact_id IS NOT OLD.artifact_id OR NEW.job_id IS NOT OLD.job_id OR NEW.input_index IS NOT OLD.input_index
          OR NEW.source_artifact_id IS NOT OLD.source_artifact_id OR NEW.source_path IS NOT OLD.source_path OR NEW.path IS NOT OLD.path
          OR NEW.filename IS NOT OLD.filename OR NEW.mime IS NOT OLD.mime OR NEW.size_bytes IS NOT OLD.size_bytes
          OR NEW.sha256 IS NOT OLD.sha256 OR NEW.role IS NOT OLD.role
          OR (OLD.verified_at IS NOT NULL AND NEW.verified_at IS NOT OLD.verified_at)
          OR (NEW.verified_at IS NOT OLD.verified_at AND (OLD.cleanup_claimed_at IS NOT NULL
              OR NOT EXISTS(SELECT 1 FROM document_jobs WHERE id=OLD.job_id AND status='queued')))
          OR (OLD.cleanup_claimed_at IS NOT NULL AND NEW.cleanup_claimed_at IS NOT OLD.cleanup_claimed_at)
          OR (NEW.cleanup_claimed_at IS NOT OLD.cleanup_claimed_at AND NOT EXISTS(
              SELECT 1 FROM document_jobs WHERE id=OLD.job_id AND status='cancelled'
                AND julianday(updated_at)<=julianday(NEW.cleanup_claimed_at)-1))
        BEGIN SELECT RAISE(ABORT,'document input identity is immutable'); END"""),
    ("trigger", "trg_document_ref_admission", """CREATE TRIGGER trg_document_ref_admission BEFORE INSERT ON document_artifact_refs
        WHEN NEW.released_at IS NOT NULL OR EXISTS(SELECT 1 FROM artifact_purge_claims WHERE artifact_id=NEW.artifact_id)
          OR NOT EXISTS(SELECT 1 FROM document_input_copies c JOIN document_input_sets f ON f.job_id=c.job_id
            JOIN document_jobs j ON j.id=f.job_id JOIN artifacts a ON a.id=c.artifact_id
            WHERE c.artifact_id=NEW.artifact_id AND c.job_id=NEW.owner_id AND c.sha256=NEW.artifact_sha256
              AND NEW.ref_key='input:'||c.source_artifact_id AND f.team_id=NEW.team_id AND f.workspace_id=NEW.workspace_id
              AND f.owner_user_id=NEW.owner_user_id AND j.status='queued' AND c.cleanup_claimed_at IS NULL
              AND a.path=c.path AND a.deleted_at IS NULL)
        BEGIN SELECT RAISE(ABORT,'document reference unavailable'); END"""),
    ("trigger", "trg_document_ref_release", """CREATE TRIGGER trg_document_ref_release BEFORE UPDATE ON document_artifact_refs
        WHEN NEW.team_id IS NOT OLD.team_id OR NEW.workspace_id IS NOT OLD.workspace_id OR NEW.owner_user_id IS NOT OLD.owner_user_id
          OR NEW.owner_kind IS NOT OLD.owner_kind OR NEW.owner_id IS NOT OLD.owner_id OR NEW.ref_key IS NOT OLD.ref_key
          OR NEW.artifact_id IS NOT OLD.artifact_id OR NEW.artifact_sha256 IS NOT OLD.artifact_sha256
          OR NEW.retention_class IS NOT OLD.retention_class OR OLD.released_at IS NOT NULL OR NEW.released_at IS NULL
          OR NOT EXISTS(SELECT 1 FROM document_input_copies WHERE artifact_id=OLD.artifact_id AND cleanup_claimed_at IS NOT NULL)
        BEGIN SELECT RAISE(ABORT,'document reference is immutable'); END"""),
    ("trigger", "trg_document_ref_purge", """CREATE TRIGGER trg_document_ref_purge BEFORE INSERT ON artifact_purge_claims
        WHEN EXISTS(SELECT 1 FROM document_artifact_refs WHERE artifact_id=NEW.artifact_id AND released_at IS NULL)
        BEGIN SELECT RAISE(ABORT,'artifact_in_use'); END"""),
    ("trigger", "trg_document_internal_artifact_update", """CREATE TRIGGER trg_document_internal_artifact_update BEFORE UPDATE ON artifacts
        WHEN EXISTS(SELECT 1 FROM document_input_copies c WHERE c.artifact_id=OLD.id)
          AND (NEW.id IS NOT OLD.id OR NEW.path IS NOT OLD.path OR NEW.created_by IS NOT OLD.created_by
            OR NEW.producer_kind IS NOT OLD.producer_kind OR NEW.source IS NOT OLD.source OR NEW.kind IS NOT OLD.kind
            OR NEW.scope IS NOT OLD.scope OR NEW.inject IS NOT OLD.inject OR NEW.trust IS NOT OLD.trust
            OR NEW.mime IS NOT OLD.mime OR NEW.size IS NOT OLD.size OR NEW.filename IS NOT OLD.filename
            OR NEW.canvas_id IS NOT OLD.canvas_id OR NEW.created_by_turn IS NOT OLD.created_by_turn
            OR (NEW.status IS NOT OLD.status AND NOT(OLD.status='document_staging' AND NEW.status='ready'
                AND EXISTS(SELECT 1 FROM document_input_copies c JOIN document_jobs j ON j.id=c.job_id
                    WHERE c.artifact_id=OLD.id AND c.verified_at IS NOT NULL AND c.cleanup_claimed_at IS NULL AND j.status='queued')))
            OR (NEW.deleted_at IS NOT OLD.deleted_at AND EXISTS(
                SELECT 1 FROM document_artifact_refs WHERE artifact_id=OLD.id AND released_at IS NULL))
            OR (NEW.deleted_at IS NULL AND EXISTS(
                SELECT 1 FROM document_input_copies WHERE artifact_id=OLD.id AND cleanup_claimed_at IS NOT NULL)))
        BEGIN SELECT RAISE(ABORT,'artifact_in_use'); END"""),
    ("trigger", "trg_document_internal_artifact_delete", """CREATE TRIGGER trg_document_internal_artifact_delete BEFORE DELETE ON artifacts
        WHEN EXISTS(SELECT 1 FROM document_artifact_refs WHERE artifact_id=OLD.id AND released_at IS NULL)
        BEGIN SELECT RAISE(ABORT,'artifact_in_use'); END"""),
    ("trigger", "trg_document_internal_artifact_recreate", """CREATE TRIGGER trg_document_internal_artifact_recreate BEFORE INSERT ON artifacts
        WHEN EXISTS(SELECT 1 FROM document_input_copies WHERE artifact_id=NEW.id OR path=NEW.path)
        BEGIN SELECT RAISE(ABORT,'document input identity exists'); END"""),
    ("trigger", "trg_document_internal_artifact_alias", """CREATE TRIGGER trg_document_internal_artifact_alias BEFORE UPDATE OF path ON artifacts
        WHEN EXISTS(SELECT 1 FROM document_input_copies WHERE path=NEW.path AND artifact_id<>OLD.id)
        BEGIN SELECT RAISE(ABORT,'document input identity exists'); END"""),
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    objects = list(_OBJECTS)
    for table, key in (("document_input_sets", "job_id=NEW.job_id"), ("document_input_copies", "artifact_id=NEW.artifact_id OR path=NEW.path OR (job_id=NEW.job_id AND (input_index=NEW.input_index OR source_artifact_id=NEW.source_artifact_id))"),
                       ("document_artifact_refs", "owner_kind=NEW.owner_kind AND owner_id=NEW.owner_id AND ref_key=NEW.ref_key")):
        for operation in ("DELETE", "INSERT"):
            name = f"trg_{table}_{operation.lower()}"
            condition = f"WHEN EXISTS(SELECT 1 FROM {table} WHERE {key})" if operation == "INSERT" else ""
            objects.append(("trigger", name, f"CREATE TRIGGER {name} BEFORE {operation} ON {table} {condition} BEGIN SELECT RAISE(ABORT,'document input journal is retained'); END"))
    for table in ("session_attachments", "turn_attachments", "canvas_export_snapshot_assets"):
        for operation in ("INSERT", "UPDATE"):
            name = f"trg_{table}_document_internal_{operation.lower()}"
            objects.append(("trigger", name, f"""CREATE TRIGGER {name} BEFORE {operation} ON {table}
                WHEN EXISTS(SELECT 1 FROM artifacts WHERE id=NEW.artifact_id AND producer_kind='template_document_internal_v1')
                BEGIN SELECT RAISE(ABORT,'document internal artifact is not a user reference'); END"""))
    connection = context.get_bind()
    for kind, name, ddl in objects:
        sql = connection.exec_driver_sql("SELECT sql FROM sqlite_schema WHERE type=? AND name=?", (kind,name)).scalar()
        if sql is None:
            connection.exec_driver_sql(ddl)
        elif "".join(sql.lower().split()) != "".join(ddl.lower().split()):
            raise RuntimeError(f"document input schema drift: {name}")


def downgrade() -> None:
    raise NotImplementedError("Forward-only; retain document references and cleanup identities.")
