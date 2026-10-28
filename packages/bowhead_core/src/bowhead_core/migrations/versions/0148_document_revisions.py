"""Bind exact review successors and retain their private source bytes."""

from importlib import import_module

from alembic import context

revision = "0148_document_revisions"
down_revision = "0147_document_revision_source"
branch_labels = None
depends_on = None

DDL = (
    """CREATE TABLE document_revisions (
      parent_job_id TEXT PRIMARY KEY NOT NULL REFERENCES document_jobs(id),
      job_id TEXT NOT NULL UNIQUE REFERENCES document_jobs(id),
      draft_digest TEXT NOT NULL CHECK(length(draft_digest)=64),
      instruction TEXT NOT NULL CHECK(length(instruction) BETWEEN 1 AND 8000)
    )""",
    """CREATE TABLE document_revision_inputs (
      job_id TEXT NOT NULL REFERENCES document_jobs(id),
      input_index INTEGER NOT NULL CHECK(input_index BETWEEN 0 AND 31),
      artifact_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('template','reference','data')),
      filename TEXT NOT NULL, mime TEXT NOT NULL, path TEXT NOT NULL,
      sha256 TEXT NOT NULL CHECK(length(sha256)=64),
      size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 0 AND 33554432),
      PRIMARY KEY(job_id,input_index), UNIQUE(job_id,artifact_id)
    )""",
    "CREATE INDEX idx_document_revision_input_artifact ON document_revision_inputs(artifact_id)",
    """CREATE TRIGGER trg_document_revision_admission BEFORE INSERT ON document_revisions
      WHEN NOT EXISTS(SELECT 1 FROM document_jobs p JOIN document_executions e ON e.job_id=p.id
        JOIN document_jobs n ON n.id=NEW.job_id JOIN chat_turn_user_sources u ON u.turn_id=n.origin_turn_id
        WHERE p.id=NEW.parent_job_id AND p.status='queued' AND e.state='review' AND e.draft_digest=NEW.draft_digest
          AND n.status='queued' AND p.requested_by=n.requested_by AND p.team_id=n.team_id
          AND p.workspace_id=n.workspace_id AND p.session_id=n.session_id AND p.origin_turn_id<>n.origin_turn_id
          AND json_extract(u.document_revision_target_json,'$.job_id')=p.id
          AND json_extract(u.document_revision_target_json,'$.session_id')=p.session_id
          AND json_extract(u.document_revision_target_json,'$.draft_digest')=e.draft_digest
          AND json_extract(n.request_json,'$.revision.target.job_id')=p.id
          AND json_extract(n.request_json,'$.revision.target.session_id')=p.session_id
          AND json_extract(n.request_json,'$.revision.target.draft_digest')=e.draft_digest
          AND json_extract(n.request_json,'$.revision.instruction')=NEW.instruction)
      BEGIN SELECT RAISE(ABORT,'document revision unavailable'); END""",
    """CREATE TRIGGER trg_document_revision_input_admission BEFORE INSERT ON document_revision_inputs
      WHEN EXISTS(SELECT 1 FROM artifact_purge_claims WHERE artifact_id=NEW.artifact_id)
        OR NOT EXISTS(SELECT 1 FROM document_revisions r JOIN document_jobs n ON n.id=r.job_id
          JOIN document_jobs p ON p.id=r.parent_job_id JOIN document_executions e ON e.job_id=p.id
          JOIN document_input_sets f ON f.job_id=p.id
          JOIN document_input_copies c ON c.job_id=p.id AND c.input_index=NEW.input_index
          JOIN artifacts a ON a.id=NEW.artifact_id
          WHERE r.job_id=NEW.job_id AND n.status='queued' AND p.status='queued' AND e.state='review'
            AND e.draft_digest=r.draft_digest AND f.frozen_at IS NOT NULL
            AND c.verified_at IS NOT NULL AND c.cleanup_claimed_at IS NULL
            AND c.role=NEW.role AND c.filename=NEW.filename
            AND a.created_by=n.requested_by AND a.workspace_id=n.workspace_id
            AND a.producer_kind='template_document_internal_v1' AND a.status='ready' AND a.deleted_at IS NULL
            AND a.path=NEW.path AND a.mime=NEW.mime AND a.size=NEW.size_bytes
            AND json_extract(n.request_json,'$.proposal.inputs['||NEW.input_index||'].artifact_id')=NEW.artifact_id
            AND json_extract(n.request_json,'$.proposal.inputs['||NEW.input_index||'].role')=NEW.role
            AND ((NEW.role='template' AND EXISTS(SELECT 1 FROM document_draft_files d
              WHERE d.job_id=p.id AND d.name='candidate.pptx' AND d.artifact_id=NEW.artifact_id
                AND d.verified_at IS NOT NULL AND d.sha256=NEW.sha256 AND d.size_bytes=NEW.size_bytes))
              OR (NEW.role<>'template' AND c.artifact_id=NEW.artifact_id AND c.sha256=NEW.sha256 AND c.size_bytes=NEW.size_bytes)))
          AND NOT EXISTS(SELECT 1 FROM document_continuations d JOIN document_jobs n ON n.id=d.job_id
            JOIN document_jobs p ON p.id=d.parent_job_id JOIN document_executions e ON e.job_id=p.id
            JOIN document_revision_inputs i ON i.job_id=p.id AND i.input_index=NEW.input_index
            JOIN artifacts a ON a.id=i.artifact_id
            WHERE d.job_id=NEW.job_id AND p.status='queued' AND n.status='queued' AND e.state='question'
              AND p.requested_by=n.requested_by AND p.workspace_id=n.workspace_id AND p.team_id=n.team_id AND p.session_id=n.session_id
              AND i.artifact_id=NEW.artifact_id AND i.role=NEW.role AND i.filename=NEW.filename AND i.mime=NEW.mime
              AND i.path=NEW.path AND i.sha256=NEW.sha256 AND i.size_bytes=NEW.size_bytes
              AND a.created_by=n.requested_by AND a.workspace_id=n.workspace_id AND a.status='ready' AND a.deleted_at IS NULL
              AND a.path=i.path AND a.size=i.size_bytes AND a.mime=i.mime
              AND json_extract(n.request_json,'$.proposal.inputs['||NEW.input_index||'].artifact_id')=NEW.artifact_id
              AND json_extract(n.request_json,'$.proposal.inputs['||NEW.input_index||'].role')=NEW.role)
      BEGIN SELECT RAISE(ABORT,'document revision input unavailable'); END""",
    """CREATE TRIGGER trg_document_revision_source_update BEFORE UPDATE ON artifacts
      WHEN (NEW.deleted_at IS NOT OLD.deleted_at OR NEW.path IS NOT OLD.path OR NEW.size IS NOT OLD.size
        OR NEW.mime IS NOT OLD.mime OR NEW.created_by IS NOT OLD.created_by OR NEW.workspace_id IS NOT OLD.workspace_id
        OR NEW.status IS NOT OLD.status OR NEW.producer_kind IS NOT OLD.producer_kind)
        AND EXISTS(SELECT 1 FROM document_revision_inputs i JOIN document_jobs j ON j.id=i.job_id
          WHERE i.artifact_id=OLD.id AND j.status='queued')
      BEGIN SELECT RAISE(ABORT,'artifact_in_use'); END""",
    """CREATE TRIGGER trg_document_revision_source_delete BEFORE DELETE ON artifacts
      WHEN EXISTS(SELECT 1 FROM document_revision_inputs i JOIN document_jobs j ON j.id=i.job_id
        WHERE i.artifact_id=OLD.id AND j.status='queued')
      BEGIN SELECT RAISE(ABORT,'artifact_in_use'); END""",
    """CREATE TRIGGER trg_document_revision_source_purge BEFORE INSERT ON artifact_purge_claims
      WHEN EXISTS(SELECT 1 FROM document_revision_inputs i JOIN document_jobs j ON j.id=i.job_id
        WHERE i.artifact_id=NEW.artifact_id AND j.status='queued')
      BEGIN SELECT RAISE(ABORT,'artifact_in_use'); END""",
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    previous = next(
        ddl
        for _, name, ddl in import_module("bowhead_core.migrations.versions.0048_document_inputs")._OBJECTS
        if name == "trg_document_input_copy_admission"
    )
    actual = connection.exec_driver_sql("SELECT sql FROM sqlite_schema WHERE name='trg_document_input_copy_admission'").scalar()
    if actual is None or "".join(actual.lower().split()) != "".join(previous.lower().split()):
        raise RuntimeError("document input admission schema drift")
    expanded = previous.replace(
        "AND COALESCE(s.producer_kind,'')<>'template_document_internal_v1'",
        """AND (COALESCE(s.producer_kind,'')<>'template_document_internal_v1' OR EXISTS(
          SELECT 1 FROM document_revision_inputs ri WHERE ri.job_id=j.id AND ri.input_index=NEW.input_index
            AND ri.artifact_id=s.id AND ri.role=NEW.role AND ri.path=NEW.source_path
            AND ri.sha256=NEW.sha256 AND ri.size_bytes=NEW.size_bytes AND ri.filename=NEW.filename AND ri.mime=NEW.mime))""",
    )
    connection.exec_driver_sql("SAVEPOINT document_revisions")
    try:
        for statement in DDL:
            connection.exec_driver_sql(statement)
        for table, key in (
            ("document_revisions", "parent_job_id=NEW.parent_job_id OR job_id=NEW.job_id"),
            ("document_revision_inputs", "job_id=NEW.job_id AND (input_index=NEW.input_index OR artifact_id=NEW.artifact_id)"),
        ):
            for operation in ("INSERT", "UPDATE", "DELETE"):
                condition = f"WHEN EXISTS(SELECT 1 FROM {table} WHERE {key})" if operation == "INSERT" else ""
                connection.exec_driver_sql(
                    f"""CREATE TRIGGER trg_{table}_{operation.lower()} BEFORE {operation} ON {table} {condition}
                    BEGIN SELECT RAISE(ABORT,'document revision identity is retained'); END"""
                )
        connection.exec_driver_sql("DROP TRIGGER trg_document_input_copy_admission")
        connection.exec_driver_sql(expanded)
    except BaseException:
        connection.exec_driver_sql("ROLLBACK TO SAVEPOINT document_revisions")
        connection.exec_driver_sql("RELEASE SAVEPOINT document_revisions")
        raise
    connection.exec_driver_sql("RELEASE SAVEPOINT document_revisions")


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")
