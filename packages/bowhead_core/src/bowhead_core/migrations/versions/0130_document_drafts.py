"""Private ordinary-PPTX execution, exact draft confirmation and publication."""

from alembic import context

revision = "0130_document_drafts"
down_revision = "0129_reference_lead_prompt"
branch_labels = None
depends_on = None

DDL = (
    """CREATE TABLE document_dispatches (
      job_id TEXT PRIMARY KEY NOT NULL REFERENCES document_jobs(id),
      profile TEXT NOT NULL CHECK(profile='ordinary-pptx-worker-v1'),
      image_id TEXT NOT NULL CHECK(length(image_id)=71 AND substr(image_id,1,7)='sha256:'),
      model_config_version INTEGER NOT NULL CHECK(model_config_version>=0),
      capability_execution_id TEXT NOT NULL UNIQUE REFERENCES turn_capability_execution(id)
    )""",
    """CREATE TABLE document_executions (
      job_id TEXT PRIMARY KEY NOT NULL REFERENCES document_jobs(id),
      state TEXT NOT NULL CHECK(state IN ('preparing','planning','rendering','review','question','published','failed','cancelled')),
      owner TEXT NOT NULL, lease_until REAL NOT NULL,
      image_id TEXT, context_json TEXT CHECK(context_json IS NULL OR json_valid(context_json)),
      response TEXT, draft_json TEXT CHECK(draft_json IS NULL OR json_valid(draft_json)),
      draft_digest TEXT CHECK(draft_digest IS NULL OR length(draft_digest)=64),
      error_code TEXT, question TEXT,
      rate_card_version TEXT NOT NULL, settled_credits INTEGER NOT NULL DEFAULT 0 CHECK(settled_credits>=0),
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      CHECK((state IN ('review','published') AND draft_json IS NOT NULL AND draft_digest IS NOT NULL)
         OR state NOT IN ('review','published'))
    )""",
    """CREATE TABLE document_model_calls (
      job_id TEXT NOT NULL REFERENCES document_executions(job_id), call_no INTEGER NOT NULL CHECK(call_no IN (1,2)),
      state TEXT NOT NULL CHECK(state IN ('dispatched','completed','unknown','not_applied')),
      provider TEXT NOT NULL, model TEXT NOT NULL, request_digest TEXT NOT NULL CHECK(length(request_digest)=64),
      response_digest TEXT, tokens_input INTEGER CHECK(tokens_input>=0), tokens_output INTEGER CHECK(tokens_output>=0),
      tokens_cached INTEGER CHECK(tokens_cached>=0), error_code TEXT, created_at TEXT NOT NULL,
      PRIMARY KEY(job_id,call_no),
      CHECK(state<>'completed' OR (response_digest IS NOT NULL AND tokens_input IS NOT NULL AND tokens_output IS NOT NULL AND tokens_cached IS NOT NULL))
    )""",
    """CREATE TABLE document_draft_files (
      job_id TEXT NOT NULL REFERENCES document_executions(job_id), name TEXT NOT NULL,
      artifact_id TEXT NOT NULL UNIQUE, path TEXT NOT NULL UNIQUE,
      sha256 TEXT NOT NULL CHECK(length(sha256)=64), size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 1 AND 33554432),
      verified_at TEXT, cleanup_after REAL NOT NULL DEFAULT 0, PRIMARY KEY(job_id,name)
    )""",
    """CREATE TABLE document_confirmations (
      job_id TEXT PRIMARY KEY NOT NULL REFERENCES document_executions(job_id),
      team_id TEXT NOT NULL, workspace_id TEXT NOT NULL, user_id TEXT NOT NULL,
      command_id TEXT NOT NULL, draft_digest TEXT NOT NULL CHECK(length(draft_digest)=64), created_at TEXT NOT NULL,
      UNIQUE(team_id,workspace_id,user_id,command_id),
      FOREIGN KEY(job_id,team_id,workspace_id,user_id) REFERENCES document_jobs(id,team_id,workspace_id,requested_by)
    )""",
    "CREATE INDEX idx_document_execution_lease ON document_executions(state,lease_until)",
    "CREATE INDEX idx_document_draft_cleanup ON document_draft_files(cleanup_after,artifact_id)",
    """CREATE TRIGGER trg_document_model_call_admission BEFORE INSERT ON document_model_calls
      WHEN NEW.state<>'dispatched' OR EXISTS(SELECT 1 FROM document_model_calls WHERE job_id=NEW.job_id AND call_no=NEW.call_no)
        OR NOT EXISTS(SELECT 1 FROM document_executions e JOIN document_jobs j ON j.id=e.job_id
          WHERE e.job_id=NEW.job_id AND e.state='planning' AND j.status='queued')
      BEGIN SELECT RAISE(ABORT,'document model dispatch unavailable'); END""",
    """CREATE TRIGGER trg_document_model_call_identity BEFORE UPDATE ON document_model_calls
      WHEN OLD.state<>'dispatched' OR NEW.state NOT IN ('completed','unknown','not_applied')
        OR NEW.job_id IS NOT OLD.job_id OR NEW.call_no IS NOT OLD.call_no OR NEW.provider IS NOT OLD.provider
        OR NEW.model IS NOT OLD.model OR NEW.request_digest IS NOT OLD.request_digest OR NEW.created_at IS NOT OLD.created_at
      BEGIN SELECT RAISE(ABORT,'document model receipt is immutable'); END""",
    """CREATE TRIGGER trg_document_draft_file_admission BEFORE INSERT ON document_draft_files
      WHEN EXISTS(SELECT 1 FROM document_draft_files WHERE job_id=NEW.job_id AND name=NEW.name)
        OR NEW.verified_at IS NOT NULL
        OR NOT EXISTS(SELECT 1 FROM document_executions e JOIN document_jobs j ON j.id=e.job_id
          JOIN artifacts a ON a.id=NEW.artifact_id WHERE e.job_id=NEW.job_id AND e.state='rendering' AND j.status='queued'
            AND a.created_by=j.requested_by AND a.workspace_id=j.workspace_id AND a.path=NEW.path AND a.size=NEW.size_bytes
            AND a.producer_kind='template_document_internal_v1' AND a.status='document_staging' AND a.deleted_at IS NULL)
      BEGIN SELECT RAISE(ABORT,'document draft file unavailable'); END""",
    """CREATE TRIGGER trg_document_dispatch_admission BEFORE INSERT ON document_dispatches
      WHEN NOT EXISTS(SELECT 1 FROM turn_capability_execution c JOIN document_jobs j ON j.id=NEW.job_id
        WHERE c.id=NEW.capability_execution_id AND c.team_id=j.team_id AND c.turn_id=j.origin_turn_id
          AND c.capability_id='cap_template_document' AND c.capability_version=1
          AND c.implementation_id='capi_template_document_native' AND c.implementation_version=1
          AND c.output_contract='document.admission.v1' AND c.status='completed' AND j.status='queued')
      BEGIN SELECT RAISE(ABORT,'document Capability admission unavailable'); END""",
    """CREATE TRIGGER trg_document_execution_admission BEFORE INSERT ON document_executions
      WHEN NEW.state<>'preparing' OR NOT EXISTS (
        SELECT 1 FROM document_jobs j WHERE j.id=NEW.job_id AND j.status='queued')
        OR EXISTS(SELECT 1 FROM document_executions WHERE job_id=NEW.job_id)
      BEGIN SELECT RAISE(ABORT,'document execution unavailable'); END""",
    """CREATE TRIGGER trg_document_execution_identity BEFORE UPDATE ON document_executions
      WHEN NEW.job_id IS NOT OLD.job_id OR NEW.owner IS NOT OLD.owner
        OR NEW.created_at IS NOT OLD.created_at OR NEW.rate_card_version IS NOT OLD.rate_card_version
        OR OLD.state IN ('published','cancelled') AND NEW.state IS NOT OLD.state
        OR OLD.state IN ('failed','question') AND NEW.state NOT IN (OLD.state,'cancelled')
        OR NEW.settled_credits<OLD.settled_credits
        OR (OLD.draft_digest IS NOT NULL AND (NEW.draft_digest IS NOT OLD.draft_digest OR NEW.draft_json IS NOT OLD.draft_json))
        OR (NEW.state='published' AND NOT EXISTS(SELECT 1 FROM document_confirmations c WHERE c.job_id=NEW.job_id AND c.draft_digest=NEW.draft_digest))
      BEGIN SELECT RAISE(ABORT,'document execution identity mismatch'); END""",
    """CREATE TRIGGER trg_document_execution_cancel AFTER UPDATE OF status ON document_jobs
      WHEN NEW.status='cancelled'
      BEGIN UPDATE document_executions SET state='cancelled',updated_at=NEW.updated_at
        WHERE job_id=NEW.id AND state IN ('preparing','planning','rendering','review','question','failed'); END""",
    """CREATE TRIGGER trg_document_confirmation_admission BEFORE INSERT ON document_confirmations
      WHEN NOT EXISTS(SELECT 1 FROM document_executions e JOIN document_jobs j ON j.id=e.job_id
        JOIN sessions s ON s.id=j.session_id JOIN workspaces w ON w.id=j.workspace_id
        JOIN teams t ON t.id=j.team_id JOIN users u ON u.id=j.requested_by
        JOIN team_members tm ON tm.team_id=t.id AND tm.user_id=u.id
        JOIN workspace_members wm ON wm.workspace_id=w.id AND wm.user_id=u.id
        WHERE e.job_id=NEW.job_id AND e.state='review' AND e.draft_digest=NEW.draft_digest AND j.status='queued'
          AND j.team_id=NEW.team_id AND j.workspace_id=NEW.workspace_id AND j.requested_by=NEW.user_id
          AND s.deleted_at IS NULL AND s.user_id=u.id AND s.workspace_id=w.id
          AND w.status='active' AND t.status='active' AND u.status='active' AND tm.status='active' AND wm.status='active'
          AND lower(trim(wm.role)) IN ('admin','ws_admin','owner','workspace_admin','member'))
        OR EXISTS(SELECT 1 FROM document_draft_files WHERE job_id=NEW.job_id AND verified_at IS NULL)
        OR (SELECT count(*) FROM document_draft_files WHERE job_id=NEW.job_id AND name IN ('candidate.pptx','candidate.pdf'))<>2
      BEGIN SELECT RAISE(ABORT,'document confirmation unavailable'); END""",
    """CREATE TRIGGER trg_document_draft_file_identity BEFORE UPDATE ON document_draft_files
      WHEN NEW.job_id IS NOT OLD.job_id OR NEW.name IS NOT OLD.name OR NEW.artifact_id IS NOT OLD.artifact_id
        OR NEW.path IS NOT OLD.path OR NEW.sha256 IS NOT OLD.sha256 OR NEW.size_bytes IS NOT OLD.size_bytes
        OR (NEW.verified_at IS NOT OLD.verified_at AND (OLD.verified_at IS NOT NULL OR NEW.verified_at IS NULL))
        OR NEW.cleanup_after<OLD.cleanup_after
      BEGIN SELECT RAISE(ABORT,'document draft file is immutable'); END""",
    """CREATE TRIGGER trg_document_draft_artifact_identity BEFORE UPDATE ON artifacts
      WHEN EXISTS(SELECT 1 FROM document_draft_files f WHERE f.artifact_id=OLD.id)
        AND (NEW.id IS NOT OLD.id OR NEW.path IS NOT OLD.path OR NEW.mime IS NOT OLD.mime
          OR NEW.size IS NOT OLD.size OR NEW.created_by IS NOT OLD.created_by OR NEW.workspace_id IS NOT OLD.workspace_id
          OR (NEW.deleted_at IS NOT OLD.deleted_at AND EXISTS(SELECT 1 FROM document_draft_files f
            JOIN document_executions e ON e.job_id=f.job_id WHERE f.artifact_id=OLD.id
            AND e.state IN ('preparing','planning','rendering','review')))
          OR (NEW.producer_kind IS NOT OLD.producer_kind AND NOT (
            OLD.producer_kind='template_document_internal_v1' AND NEW.producer_kind='template_document_v1'
            AND EXISTS(SELECT 1 FROM document_draft_files f JOIN document_executions e ON e.job_id=f.job_id
              WHERE f.artifact_id=OLD.id AND e.state='published' AND f.name IN ('candidate.pptx','candidate.pdf')))))
      BEGIN SELECT RAISE(ABORT,'document draft artifact is immutable'); END""",
    """CREATE TRIGGER trg_document_draft_purge BEFORE INSERT ON artifact_purge_claims
      WHEN EXISTS(SELECT 1 FROM document_draft_files f JOIN document_executions e ON e.job_id=f.job_id
        WHERE f.artifact_id=NEW.artifact_id AND e.state IN ('preparing','planning','rendering','review'))
      BEGIN SELECT RAISE(ABORT,'artifact_in_use'); END""",
    """CREATE TRIGGER trg_document_draft_artifact_delete BEFORE DELETE ON artifacts
      WHEN EXISTS(SELECT 1 FROM document_draft_files f WHERE f.artifact_id=OLD.id)
        AND NOT EXISTS(SELECT 1 FROM artifact_purge_claims p WHERE p.artifact_id=OLD.id AND p.path=OLD.path)
      BEGIN SELECT RAISE(ABORT,'document draft requires purge claim'); END""",
    """CREATE TRIGGER trg_document_draft_artifact_reuse BEFORE INSERT ON artifacts
      WHEN EXISTS(SELECT 1 FROM document_draft_files f WHERE f.artifact_id=NEW.id OR f.path=NEW.path)
      BEGIN SELECT RAISE(ABORT,'document draft artifact identity retained'); END""",
    """CREATE TRIGGER trg_document_draft_path_alias BEFORE UPDATE OF path ON artifacts
      WHEN EXISTS(SELECT 1 FROM document_draft_files f WHERE f.path=NEW.path AND f.artifact_id<>OLD.id)
      BEGIN SELECT RAISE(ABORT,'document draft path retained'); END""",
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()

    def ensure(ddl):
        _, kind, name, *_ = ddl.split()
        previous = connection.exec_driver_sql("SELECT sql FROM sqlite_schema WHERE type=? AND name=?", (kind.lower(), name)).scalar()
        if previous is None:
            connection.exec_driver_sql(ddl)
        elif "".join(previous.lower().split()) != "".join(ddl.lower().split()):
            raise RuntimeError(f"document draft schema drift: {name}")

    connection.exec_driver_sql("SAVEPOINT document_drafts")
    try:
        for ddl in DDL:
            ensure(ddl)
        for table in ("document_dispatches", "document_executions", "document_model_calls", "document_draft_files", "document_confirmations"):
            for operation in (("DELETE", "UPDATE") if table in {"document_confirmations", "document_dispatches"} else ("DELETE",)):
                ensure(f"""CREATE TRIGGER trg_{table}_{operation.lower()} BEFORE {operation} ON {table}
                  BEGIN SELECT RAISE(ABORT,'document execution journal is retained'); END""")
        connection.exec_driver_sql("RELEASE SAVEPOINT document_drafts")
    except BaseException:
        connection.exec_driver_sql("ROLLBACK TO SAVEPOINT document_drafts")
        connection.exec_driver_sql("RELEASE SAVEPOINT document_drafts")
        raise


def downgrade() -> None:
    raise NotImplementedError("Forward-only migration; restore a matching database backup.")
