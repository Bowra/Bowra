"""Retain exact Canvas material in the original document/Artifact lifecycle."""

from importlib import import_module

from alembic import context

revision = "0204_document_canvas_inputs"
down_revision = "0203_chart_date_display"
branch_labels = None
depends_on = None

TABLE = """CREATE TABLE document_canvas_inputs (
 job_id TEXT PRIMARY KEY NOT NULL REFERENCES document_jobs(id),
 artifact_id TEXT NOT NULL, path TEXT NOT NULL,
 sha256 TEXT NOT NULL CHECK(length(sha256)=64),
 size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 1 AND 33554432),
 manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)
   AND json_extract(manifest_json,'$.schema_version')=1
   AND json_extract(manifest_json,'$.sha256')=sha256
   AND json_extract(manifest_json,'$.size_bytes')=size_bytes),
 source_job_id TEXT REFERENCES document_jobs(id),
 verified_at TEXT, cleanup_claimed_at TEXT, cleanup_after REAL NOT NULL
)"""

ADMISSION = """CREATE TRIGGER trg_document_canvas_admission BEFORE INSERT ON document_canvas_inputs
WHEN NEW.cleanup_claimed_at IS NOT NULL OR EXISTS(SELECT 1 FROM document_canvas_inputs WHERE job_id=NEW.job_id)
 OR NOT EXISTS(SELECT 1 FROM document_jobs j JOIN sessions s ON s.id=j.session_id JOIN artifacts a ON a.id=NEW.artifact_id
   WHERE j.id=NEW.job_id AND j.status='queued' AND s.deleted_at IS NULL
    AND s.user_id=j.requested_by AND s.workspace_id=j.workspace_id
    AND json(json_extract(j.request_json,'$.canvas_input'))=NEW.manifest_json
    AND json_extract(j.request_json,'$.canvas_input.session_id')=j.session_id
    AND a.created_by=j.requested_by AND a.workspace_id=j.workspace_id AND a.deleted_at IS NULL
    AND a.path=NEW.path AND a.size=NEW.size_bytes AND a.source='document_canvas_input'
    AND a.filename='canvas-material.json' AND a.mime='application/json'
    AND a.producer_kind='template_document_internal_v1' AND a.kind='tool_data' AND a.inject='list' AND a.scope='user'
    AND ((NEW.source_job_id IS NULL AND NEW.verified_at IS NULL AND a.status='document_staging'
       AND json_type(j.request_json,'$.proposal.canvas')='object')
      OR (NEW.source_job_id IS NOT NULL AND NEW.verified_at IS NOT NULL AND a.status='ready'
       AND json_extract(j.request_json,'$.canvas_source_job_id')=NEW.source_job_id
       AND EXISTS(SELECT 1 FROM document_canvas_inputs c JOIN document_jobs p ON p.id=c.job_id
         WHERE c.job_id=NEW.source_job_id AND c.artifact_id=NEW.artifact_id AND c.path=NEW.path AND c.sha256=NEW.sha256
          AND c.size_bytes=NEW.size_bytes AND c.manifest_json=NEW.manifest_json AND c.verified_at IS NOT NULL
          AND c.cleanup_claimed_at IS NULL AND p.team_id=j.team_id AND p.workspace_id=j.workspace_id
          AND p.requested_by=j.requested_by AND p.session_id=j.session_id
          AND (EXISTS(SELECT 1 FROM document_revisions r WHERE r.parent_job_id=p.id AND r.job_id=j.id)
            OR EXISTS(SELECT 1 FROM document_continuations r WHERE r.parent_job_id=p.id AND r.job_id=j.id))))))
BEGIN SELECT RAISE(ABORT,'document Canvas input unavailable'); END"""

PROGRESS = """CREATE TRIGGER trg_document_canvas_progress BEFORE UPDATE ON document_canvas_inputs
WHEN NEW.job_id IS NOT OLD.job_id OR NEW.artifact_id IS NOT OLD.artifact_id OR NEW.path IS NOT OLD.path
 OR NEW.sha256 IS NOT OLD.sha256 OR NEW.size_bytes IS NOT OLD.size_bytes OR NEW.manifest_json IS NOT OLD.manifest_json
 OR NEW.source_job_id IS NOT OLD.source_job_id
 OR (OLD.verified_at IS NOT NULL AND NEW.verified_at IS NOT OLD.verified_at)
 OR (NEW.verified_at IS NOT OLD.verified_at AND (NEW.verified_at IS NULL OR OLD.cleanup_claimed_at IS NOT NULL
   OR NOT EXISTS(SELECT 1 FROM document_jobs WHERE id=OLD.job_id AND status='queued')))
 OR (OLD.cleanup_claimed_at IS NOT NULL AND NEW.cleanup_claimed_at IS NOT OLD.cleanup_claimed_at)
 OR (NEW.cleanup_claimed_at IS NOT OLD.cleanup_claimed_at AND NOT EXISTS(SELECT 1 FROM document_jobs
   WHERE id=OLD.job_id AND status='cancelled' AND julianday(updated_at)<=julianday(NEW.cleanup_claimed_at)-1))
BEGIN SELECT RAISE(ABORT,'document Canvas identity is immutable'); END"""

ARTIFACT = """CREATE TRIGGER trg_document_canvas_artifact BEFORE UPDATE ON artifacts
WHEN EXISTS(SELECT 1 FROM document_canvas_inputs WHERE artifact_id=OLD.id)
 AND (NEW.id IS NOT OLD.id OR NEW.path IS NOT OLD.path OR NEW.created_by IS NOT OLD.created_by
  OR NEW.workspace_id IS NOT OLD.workspace_id OR NEW.producer_kind IS NOT OLD.producer_kind
  OR NEW.source IS NOT OLD.source OR NEW.kind IS NOT OLD.kind OR NEW.scope IS NOT OLD.scope
  OR NEW.inject IS NOT OLD.inject OR NEW.trust IS NOT OLD.trust OR NEW.mime IS NOT OLD.mime
  OR NEW.size IS NOT OLD.size OR NEW.filename IS NOT OLD.filename OR NEW.created_at IS NOT OLD.created_at
  OR (NEW.status IS NOT OLD.status AND NOT (OLD.status='document_staging' AND NEW.status='ready'
    AND EXISTS(SELECT 1 FROM document_canvas_inputs c JOIN document_jobs j ON j.id=c.job_id
      WHERE c.artifact_id=OLD.id AND c.verified_at IS NOT NULL AND c.cleanup_claimed_at IS NULL AND j.status='queued')))
  OR (NEW.deleted_at IS NOT OLD.deleted_at AND EXISTS(SELECT 1 FROM document_artifact_refs WHERE artifact_id=OLD.id AND released_at IS NULL))
  OR (NEW.deleted_at IS NULL AND EXISTS(SELECT 1 FROM document_canvas_inputs WHERE artifact_id=OLD.id)
    AND NOT EXISTS(SELECT 1 FROM document_canvas_inputs WHERE artifact_id=OLD.id AND cleanup_claimed_at IS NULL)))
BEGIN SELECT RAISE(ABORT,'artifact_in_use'); END"""


def _expand_reference_guards(db):
    originals = {name: sql for _, name, sql in import_module("bowhead_core.migrations.versions.0048_document_inputs")._OBJECTS}
    for name in ("trg_document_ref_admission", "trg_document_ref_release"):
        original = originals[name]
        actual = db.exec_driver_sql("SELECT sql FROM sqlite_schema WHERE name=?", (name,)).scalar()
        if actual is None or "".join(actual.lower().split()) != "".join(original.lower().split()):
            raise RuntimeError(f"Document reference schema drift: {name}")
        if name.endswith("admission"):
            expanded = original.replace("OR NOT EXISTS(", "OR NOT (EXISTS(", 1).replace(
                "\n        BEGIN",
                """ OR EXISTS(SELECT 1 FROM document_canvas_inputs c JOIN document_jobs j ON j.id=c.job_id
                JOIN artifacts a ON a.id=c.artifact_id WHERE c.job_id=NEW.owner_id AND c.artifact_id=NEW.artifact_id
                 AND c.sha256=NEW.artifact_sha256 AND NEW.ref_key='canvas' AND c.cleanup_claimed_at IS NULL
                 AND j.team_id=NEW.team_id AND j.workspace_id=NEW.workspace_id AND j.requested_by=NEW.owner_user_id
                 AND j.status='queued' AND a.path=c.path AND a.deleted_at IS NULL))
        BEGIN""",
                1,
            )
        else:
            clause = "NOT EXISTS(SELECT 1 FROM document_input_copies WHERE artifact_id=OLD.artifact_id AND cleanup_claimed_at IS NOT NULL)"
            expanded = original.replace(
                clause,
                "(" + clause + " AND NOT EXISTS(SELECT 1 FROM document_canvas_inputs WHERE job_id=OLD.owner_id AND artifact_id=OLD.artifact_id AND cleanup_claimed_at IS NOT NULL))",
            )
        db.exec_driver_sql(f"DROP TRIGGER {name}")
        db.exec_driver_sql(expanded)


def upgrade():
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT document_canvas_inputs")
    try:
        for sql in (
            TABLE,
            "CREATE INDEX idx_document_canvas_artifact ON document_canvas_inputs(artifact_id)",
            "CREATE INDEX idx_document_canvas_cleanup ON document_canvas_inputs(cleanup_after,job_id)",
            ADMISSION, PROGRESS, ARTIFACT,
        ):
            db.exec_driver_sql(sql)
        db.exec_driver_sql("""CREATE TRIGGER trg_document_canvas_delete BEFORE DELETE ON document_canvas_inputs
          WHEN NOT EXISTS(SELECT 1 FROM account_deletion_rows r JOIN account_deletions d ON d.operation_id=r.operation_id
            WHERE r.table_name='document_canvas_inputs' AND r.row_key=json_array(OLD.job_id)
              AND d.state='purging' AND julianday(d.purge_after)<=julianday('now') AND d.lease_owner IS NOT NULL
              AND julianday(d.lease_expires_at)>julianday('now')
              AND EXISTS(SELECT 1 FROM users u WHERE u.id=d.user_id AND u.status='deleted'))
          BEGIN SELECT RAISE(ABORT,'document Canvas identity is retained'); END""")
        _expand_reference_guards(db)
        db.exec_driver_sql("RELEASE SAVEPOINT document_canvas_inputs")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT document_canvas_inputs")
        db.exec_driver_sql("RELEASE SAVEPOINT document_canvas_inputs")
        raise


def downgrade():
    raise NotImplementedError("Restore the matching database archive to roll back.")
