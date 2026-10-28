"""Associate exact document checkpoints with the original shared card."""

from alembic import context

revision = "0150_document_clarifications"
down_revision = "0149_document_planning_attempts"
branch_labels = None
depends_on = None

DDL = (
    """CREATE TABLE clarification_document_continuations (
      card_id TEXT PRIMARY KEY NOT NULL REFERENCES clarification_requests(request_id),
      job_id TEXT NOT NULL UNIQUE, attempt_no INTEGER NOT NULL CHECK(attempt_no=1),
      turn_id TEXT NOT NULL UNIQUE REFERENCES turns(id),
      creation_generation INTEGER NOT NULL CHECK(creation_generation>=1), creation_owner_id TEXT NOT NULL,
      checkpoint_digest TEXT NOT NULL CHECK(length(checkpoint_digest)=64),
      source_hash TEXT NOT NULL CHECK(length(source_hash)=64),
      runtime_snapshot_digest TEXT NOT NULL CHECK(length(runtime_snapshot_digest)=64),
      reserved_generation INTEGER CHECK(reserved_generation>creation_generation),
      consumed_generation INTEGER, consumed_owner_id TEXT, created_at TEXT NOT NULL,
      FOREIGN KEY(job_id,attempt_no) REFERENCES document_planning_attempts(job_id,attempt_no),
      CHECK((consumed_generation IS NULL AND consumed_owner_id IS NULL) OR
        (consumed_generation IS NOT NULL AND consumed_owner_id IS NOT NULL AND consumed_generation=reserved_generation))
    )""",
    """CREATE TRIGGER trg_document_clarification_admission BEFORE INSERT ON clarification_document_continuations
      WHEN NEW.reserved_generation IS NOT NULL OR NEW.consumed_generation IS NOT NULL
        OR NOT EXISTS(SELECT 1 FROM clarification_requests q JOIN document_jobs j ON j.id=NEW.job_id
          JOIN document_turn_dispatches d ON d.job_id=j.id JOIN document_planning_attempts a ON a.job_id=j.id AND a.attempt_no=1
          JOIN document_executions e ON e.job_id=j.id JOIN turns t ON t.id=j.origin_turn_id
          JOIN chat_turn_spawn_dispatch w ON w.turn_id=t.id AND w.session_id=j.session_id
          WHERE q.request_id=NEW.card_id AND q.status='pending' AND q.kind='ambiguous_turn'
            AND q.token='' AND q.tool IS NULL AND q.multi_select=0 AND q.candidates_json='[]'
            AND q.turn_id=t.id AND t.id=NEW.turn_id AND q.session_id=j.session_id
            AND q.user_id=j.requested_by AND q.workspace_id=j.workspace_id AND q.created_at=NEW.created_at
            AND t.status='running' AND t.execution_generation=NEW.creation_generation
            AND w.state='spawned' AND w.owner_id=NEW.creation_owner_id AND w.cancel_requested_at IS NULL
            AND w.lease_execution_generation=t.execution_generation AND julianday(w.lease_expires_at)>julianday('now')
            AND d.creation_generation=NEW.creation_generation AND d.owner_id=NEW.creation_owner_id
            AND a.creation_generation=NEW.creation_generation AND a.owner_id=NEW.creation_owner_id
            AND a.checkpoint_digest=NEW.checkpoint_digest AND e.state='question' AND e.owner=NEW.creation_owner_id
            AND q.rationale=e.question AND q.rationale=json_extract(a.checkpoint_json,'$.question')
            AND d.source_hash=NEW.source_hash AND d.runtime_snapshot_digest=NEW.runtime_snapshot_digest
            AND json_extract(q.resume_context_json,'$.__schema_version')=3
            AND json_extract(q.resume_context_json,'$.team_id')=j.team_id
            AND json_extract(q.resume_context_json,'$.turn_id')=q.turn_id
            AND json_extract(q.resume_context_json,'$.user_id')=q.user_id
            AND json_extract(q.resume_context_json,'$.workspace_id')=q.workspace_id
            AND json_extract(q.resume_context_json,'$.session_id')=q.session_id
            AND json_extract(q.resume_context_json,'$.continuation.kind')='document_planning'
            AND json_extract(q.resume_context_json,'$.continuation.version')=1
            AND json_extract(q.resume_context_json,'$.continuation.checkpoint.job_id')=j.id
            AND json_extract(q.resume_context_json,'$.continuation.checkpoint.attempt_no')=1
            AND json_extract(q.resume_context_json,'$.continuation.checkpoint.checkpoint_digest')=NEW.checkpoint_digest
            AND json_extract(q.resume_context_json,'$.continuation.checkpoint.source_hash')=NEW.source_hash
            AND json_extract(q.resume_context_json,'$.continuation.runtime_snapshot_digest')=NEW.runtime_snapshot_digest
            AND json_extract(q.resume_context_json,'$.continuation.creation_generation')=NEW.creation_generation
            AND json_extract(q.resume_context_json,'$.continuation.creation_owner_id')=NEW.creation_owner_id)
      BEGIN SELECT RAISE(ABORT,'document clarification unavailable'); END""",
    """CREATE TRIGGER trg_document_clarification_identity BEFORE UPDATE ON clarification_document_continuations
      WHEN NEW.card_id IS NOT OLD.card_id OR NEW.job_id IS NOT OLD.job_id OR NEW.attempt_no IS NOT OLD.attempt_no
        OR NEW.turn_id IS NOT OLD.turn_id OR NEW.creation_generation IS NOT OLD.creation_generation
        OR NEW.creation_owner_id IS NOT OLD.creation_owner_id OR NEW.checkpoint_digest IS NOT OLD.checkpoint_digest
        OR NEW.source_hash IS NOT OLD.source_hash OR NEW.runtime_snapshot_digest IS NOT OLD.runtime_snapshot_digest
        OR NEW.created_at IS NOT OLD.created_at
        OR (OLD.consumed_generation IS NOT NULL AND (NEW.consumed_generation IS NOT OLD.consumed_generation
          OR NEW.consumed_owner_id IS NOT OLD.consumed_owner_id OR NEW.reserved_generation IS NOT OLD.reserved_generation))
      BEGIN SELECT RAISE(ABORT,'document clarification identity is immutable'); END""",
    """CREATE TRIGGER trg_document_clarification_reservation BEFORE UPDATE OF reserved_generation ON clarification_document_continuations
      WHEN NEW.reserved_generation IS NOT OLD.reserved_generation AND (
        OLD.consumed_generation IS NOT NULL OR EXISTS(SELECT 1 FROM document_planning_attempts WHERE job_id=OLD.job_id AND attempt_no=2)
        OR NOT EXISTS(SELECT 1 FROM turns t JOIN chat_turn_spawn_dispatch w ON w.turn_id=t.id
          JOIN clarification_requests q ON q.request_id=OLD.card_id
          WHERE t.id=OLD.turn_id AND w.session_id=t.session_id AND q.status='resolved' AND w.cancel_requested_at IS NULL
            AND ((OLD.reserved_generation IS NULL AND NEW.reserved_generation=t.execution_generation+1
              AND t.status='clarification_pending' AND w.state IN ('terminal','failed') AND w.lease_execution_generation=t.execution_generation)
            OR (OLD.reserved_generation IS NOT NULL AND NEW.reserved_generation IS NULL
              AND t.status='running' AND t.execution_generation=OLD.reserved_generation AND w.lease_kind='resume'
              AND w.lease_execution_generation=OLD.reserved_generation AND w.state IN ('claimed','spawned')))))
      BEGIN SELECT RAISE(ABORT,'document clarification reservation unavailable'); END""",
    """CREATE TRIGGER trg_document_clarification_consumption BEFORE UPDATE OF consumed_generation,consumed_owner_id ON clarification_document_continuations
      WHEN (NEW.consumed_generation IS NOT OLD.consumed_generation OR NEW.consumed_owner_id IS NOT OLD.consumed_owner_id)
        AND (OLD.consumed_generation IS NOT NULL OR NEW.consumed_generation IS NOT OLD.reserved_generation
          OR NOT EXISTS(SELECT 1 FROM document_planning_attempts a JOIN turns t ON t.id=OLD.turn_id
            JOIN chat_turn_spawn_dispatch w ON w.turn_id=t.id AND w.session_id=t.session_id
            WHERE a.job_id=OLD.job_id AND a.attempt_no=2 AND a.card_id=OLD.card_id
              AND a.creation_generation=NEW.consumed_generation AND a.owner_id=NEW.consumed_owner_id
              AND t.status='running' AND t.execution_generation=NEW.consumed_generation AND w.state='spawned'
              AND w.owner_id=NEW.consumed_owner_id AND w.lease_execution_generation=t.execution_generation
              AND w.cancel_requested_at IS NULL AND julianday(w.lease_expires_at)>julianday('now')))
      BEGIN SELECT RAISE(ABORT,'document clarification consumption unavailable'); END""",
    """CREATE TRIGGER trg_document_attempt_card_reservation BEFORE INSERT ON document_planning_attempts
      WHEN NEW.attempt_no=2 AND NOT EXISTS(SELECT 1 FROM clarification_document_continuations c
        JOIN clarification_requests q ON q.request_id=c.card_id WHERE c.job_id=NEW.job_id AND c.card_id=NEW.card_id
          AND c.reserved_generation=NEW.creation_generation AND c.consumed_generation IS NULL AND q.status='resolved')
      BEGIN SELECT RAISE(ABORT,'document attempt requires its original card reservation'); END""",
    """CREATE TRIGGER trg_document_card_identity BEFORE UPDATE ON clarification_requests
      WHEN EXISTS(SELECT 1 FROM clarification_document_continuations WHERE card_id=OLD.request_id)
        AND (NEW.request_id IS NOT OLD.request_id OR NEW.turn_id IS NOT OLD.turn_id OR NEW.user_id IS NOT OLD.user_id
          OR NEW.workspace_id IS NOT OLD.workspace_id OR NEW.session_id IS NOT OLD.session_id
          OR NEW.kind IS NOT OLD.kind OR NEW.token IS NOT OLD.token OR NEW.tool IS NOT OLD.tool
          OR NEW.rationale IS NOT OLD.rationale OR NEW.candidates_json IS NOT OLD.candidates_json
          OR NEW.multi_select IS NOT OLD.multi_select OR NEW.created_at IS NOT OLD.created_at
          OR NEW.resume_context_json IS NOT OLD.resume_context_json
          OR (OLD.status='resolved' AND NEW.status NOT IN ('resolved','dismissed','expired'))
          OR (OLD.status='resolved' AND (NEW.chosen_value IS NOT OLD.chosen_value OR NEW.chosen_values_json IS NOT OLD.chosen_values_json
            OR NEW.answers_json IS NOT OLD.answers_json OR NEW.chosen_at IS NOT OLD.chosen_at)))
      BEGIN SELECT RAISE(ABORT,'document card identity and answer are immutable'); END""",
    """CREATE TRIGGER trg_document_card_answer BEFORE UPDATE ON clarification_requests
      WHEN NEW.status='resolved' AND OLD.status<>'resolved'
        AND EXISTS(SELECT 1 FROM clarification_document_continuations WHERE card_id=OLD.request_id)
        AND (OLD.status<>'pending' OR NOT json_valid(COALESCE(NEW.answers_json,''))
          OR json_type(NEW.answers_json) IS NOT 'array' OR json_array_length(NEW.answers_json) IS NOT 1
          OR json_extract(NEW.answers_json,'$[0].question') IS NOT OLD.rationale
          OR json_extract(NEW.answers_json,'$[0].source') IS NOT 'custom'
          OR json_type(NEW.answers_json,'$[0].skipped') IS NOT 'false'
          OR json_type(NEW.answers_json,'$[0].option_index') IS NOT 'null'
          OR json_type(NEW.answers_json,'$[0].answer') IS NOT 'text'
          OR length(json_extract(NEW.answers_json,'$[0].answer')) NOT BETWEEN 1 AND 256
          OR NOT EXISTS(SELECT 1 FROM clarification_document_continuations c JOIN turns t ON t.id=c.turn_id
            JOIN chat_turn_spawn_dispatch w ON w.turn_id=t.id AND w.session_id=t.session_id
            WHERE c.card_id=OLD.request_id AND c.reserved_generation IS NULL AND c.consumed_generation IS NULL
              AND t.status='clarification_pending' AND w.state IN ('terminal','failed')
              AND w.lease_execution_generation=t.execution_generation AND w.cancel_requested_at IS NULL))
      BEGIN SELECT RAISE(ABORT,'document answer unavailable'); END""",
    """CREATE TRIGGER trg_document_card_replace BEFORE INSERT ON clarification_requests
      WHEN EXISTS(SELECT 1 FROM clarification_document_continuations WHERE card_id=NEW.request_id)
      BEGIN SELECT RAISE(ABORT,'document checkpoint card is retained'); END""",
    """CREATE TRIGGER trg_document_card_delete BEFORE DELETE ON clarification_requests
      WHEN EXISTS(SELECT 1 FROM clarification_document_continuations WHERE card_id=OLD.request_id)
      BEGIN SELECT RAISE(ABORT,'document checkpoint card is retained'); END""",
    """CREATE TRIGGER trg_document_clarification_delete BEFORE DELETE ON clarification_document_continuations
      BEGIN SELECT RAISE(ABORT,'document checkpoint association is retained'); END""",
    """CREATE TRIGGER trg_document_clarification_replace BEFORE INSERT ON clarification_document_continuations
      WHEN EXISTS(SELECT 1 FROM clarification_document_continuations WHERE card_id=NEW.card_id OR job_id=NEW.job_id OR turn_id=NEW.turn_id)
      BEGIN SELECT RAISE(ABORT,'document checkpoint association is retained'); END""",
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    connection.exec_driver_sql("SAVEPOINT document_clarifications")
    try:
        for statement in DDL:
            connection.exec_driver_sql(statement)
    except BaseException:
        connection.exec_driver_sql("ROLLBACK TO SAVEPOINT document_clarifications")
        connection.exec_driver_sql("RELEASE SAVEPOINT document_clarifications")
        raise
    connection.exec_driver_sql("RELEASE SAVEPOINT document_clarifications")


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")
