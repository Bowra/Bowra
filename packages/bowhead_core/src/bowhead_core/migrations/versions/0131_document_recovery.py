"""Retain known document usage independently of settlement and bind answers."""

from alembic import context

revision = "0131_document_recovery"
down_revision = "0130_document_drafts"
branch_labels = None
depends_on = None

DDL = (
    """CREATE TABLE document_usage_receipts (
      job_id TEXT NOT NULL, call_no INTEGER NOT NULL,
      response_digest TEXT NOT NULL, tokens_input INTEGER NOT NULL CHECK(tokens_input>=0),
      tokens_output INTEGER NOT NULL CHECK(tokens_output>=0), tokens_cached INTEGER NOT NULL CHECK(tokens_cached>=0),
      created_at TEXT NOT NULL, settled_at TEXT, retry_after REAL NOT NULL DEFAULT 0 CHECK(retry_after>=0),
      PRIMARY KEY(job_id,call_no), FOREIGN KEY(job_id,call_no) REFERENCES document_model_calls(job_id,call_no)
    )""",
    "CREATE INDEX idx_document_usage_pending ON document_usage_receipts(retry_after,job_id) WHERE settled_at IS NULL",
    """CREATE TRIGGER trg_document_usage_admission BEFORE INSERT ON document_usage_receipts
      WHEN NOT EXISTS(SELECT 1 FROM document_model_calls c WHERE c.job_id=NEW.job_id AND c.call_no=NEW.call_no
        AND (c.state IN ('dispatched','unknown') OR (c.state='completed' AND c.response_digest=NEW.response_digest
          AND c.tokens_input=NEW.tokens_input AND c.tokens_output=NEW.tokens_output AND c.tokens_cached=NEW.tokens_cached)))
      BEGIN SELECT RAISE(ABORT,'document usage identity mismatch'); END""",
    """CREATE TRIGGER trg_document_usage_identity BEFORE UPDATE ON document_usage_receipts
      WHEN NEW.job_id IS NOT OLD.job_id OR NEW.call_no IS NOT OLD.call_no OR NEW.response_digest IS NOT OLD.response_digest
        OR NEW.tokens_input IS NOT OLD.tokens_input OR NEW.tokens_output IS NOT OLD.tokens_output
        OR NEW.tokens_cached IS NOT OLD.tokens_cached OR NEW.created_at IS NOT OLD.created_at
        OR (OLD.settled_at IS NOT NULL AND NEW.settled_at IS NOT OLD.settled_at)
      BEGIN SELECT RAISE(ABORT,'document usage receipt is immutable'); END""",
    """CREATE TRIGGER trg_document_usage_delete BEFORE DELETE ON document_usage_receipts
      BEGIN SELECT RAISE(ABORT,'document usage receipt is retained'); END""",
    """CREATE TABLE document_continuations (
      parent_job_id TEXT PRIMARY KEY NOT NULL REFERENCES document_jobs(id),
      job_id TEXT NOT NULL UNIQUE REFERENCES document_jobs(id),
      question_digest TEXT NOT NULL CHECK(length(question_digest)=64),
      instruction TEXT NOT NULL CHECK(length(instruction) BETWEEN 1 AND 8000)
    )""",
    """CREATE TRIGGER trg_document_continuation_admission BEFORE INSERT ON document_continuations
      WHEN NOT EXISTS(SELECT 1 FROM document_jobs p JOIN document_executions e ON e.job_id=p.id
        JOIN document_jobs n ON n.id=NEW.job_id WHERE p.id=NEW.parent_job_id AND p.status='queued' AND e.state='question'
          AND p.team_id=n.team_id AND p.workspace_id=n.workspace_id AND p.requested_by=n.requested_by
          AND p.session_id=n.session_id AND p.origin_turn_id<>n.origin_turn_id AND n.status='queued')
      BEGIN SELECT RAISE(ABORT,'document question unavailable'); END""",
    """CREATE TRIGGER trg_document_continuation_update BEFORE UPDATE ON document_continuations
      BEGIN SELECT RAISE(ABORT,'document answer is immutable'); END""",
    """CREATE TRIGGER trg_document_continuation_delete BEFORE DELETE ON document_continuations
      BEGIN SELECT RAISE(ABORT,'document answer is retained'); END""",
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    connection.exec_driver_sql("SAVEPOINT document_recovery")
    try:
        for ddl in DDL:
            connection.exec_driver_sql(ddl)
        # Completed receipts in 0130 were committed together with settlement.
        # Never invent usage for an unknown historical provider outcome.
        connection.exec_driver_sql("""INSERT INTO document_usage_receipts
          (job_id,call_no,response_digest,tokens_input,tokens_output,tokens_cached,created_at,settled_at)
          SELECT job_id,call_no,response_digest,tokens_input,tokens_output,tokens_cached,created_at,created_at
          FROM document_model_calls WHERE state='completed'""")
        connection.exec_driver_sql("RELEASE SAVEPOINT document_recovery")
    except BaseException:
        connection.exec_driver_sql("ROLLBACK TO SAVEPOINT document_recovery")
        connection.exec_driver_sql("RELEASE SAVEPOINT document_recovery")
        raise


def downgrade() -> None:
    raise NotImplementedError("Forward-only migration; restore a matching database backup.")
