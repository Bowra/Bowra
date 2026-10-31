"""Add immutable encrypted model views beside original execution authorities."""

from alembic import context

revision = "0206_model_context"
down_revision = "0205_datarc_context"
branch_labels = None
depends_on = None

DDL = (
    "ALTER TABLE turns ADD COLUMN operation_kind TEXT CHECK(operation_kind IS NULL OR operation_kind='context_maintenance')",
    "ALTER TABLE chat_turn_user_sources ADD COLUMN context_maintenance_json TEXT CHECK(context_maintenance_json IS NULL OR (json_valid(context_maintenance_json) AND json_type(context_maintenance_json)='object'))",
    """CREATE TABLE continuation_payload_segments (
      kind TEXT NOT NULL CHECK(kind IN ('ordinary','helper')),
      owner_id TEXT NOT NULL,
      ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 127),
      session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
      turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
      clarification_id TEXT REFERENCES clarification_requests(request_id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
      checkpoint_id TEXT REFERENCES subagent_resume_checkpoints(id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
      digest TEXT NOT NULL CHECK(length(digest)=64 AND digest NOT GLOB '*[^0-9a-f]*'),
      byte_count INTEGER NOT NULL CHECK(byte_count BETWEEN 1 AND 524288),
      ciphertext TEXT NOT NULL CHECK(length(ciphertext) BETWEEN 1 AND 2097152),
      PRIMARY KEY(kind,owner_id,ordinal),
      CHECK((kind='ordinary' AND clarification_id=owner_id AND clarification_id IS NOT NULL AND checkpoint_id IS NULL)
        OR (kind='helper' AND checkpoint_id=owner_id AND checkpoint_id IS NOT NULL AND clarification_id IS NULL))
    )""",
    """CREATE TRIGGER trg_continuation_payload_immutable BEFORE UPDATE ON continuation_payload_segments
      BEGIN SELECT RAISE(ABORT,'continuation payload is immutable'); END""",
    """CREATE TRIGGER trg_continuation_payload_scope BEFORE INSERT ON continuation_payload_segments
      WHEN NOT EXISTS(SELECT 1 FROM turns t JOIN sessions s ON s.id=t.session_id
        WHERE t.id=NEW.turn_id AND s.id=NEW.session_id AND s.deleted_at IS NULL)
      BEGIN SELECT RAISE(ABORT,'continuation payload scope mismatch'); END""",
    """CREATE TRIGGER trg_continuation_payload_helper_purge AFTER UPDATE OF ciphertext ON subagent_resume_checkpoints
      WHEN NEW.ciphertext IS NULL
      BEGIN DELETE FROM continuation_payload_segments WHERE checkpoint_id=NEW.id; END""",
    """CREATE TABLE model_context_segments (
      id TEXT PRIMARY KEY CHECK(id LIKE 'mcs_%'),
      session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
      execution_id TEXT NOT NULL REFERENCES turn_agent_execution(id) ON DELETE CASCADE,
      lane TEXT NOT NULL CHECK(lane IN ('ordinary','coordination')),
      digest TEXT NOT NULL CHECK(length(digest)=64 AND digest NOT GLOB '*[^0-9a-f]*'),
      byte_count INTEGER NOT NULL CHECK(byte_count BETWEEN 1 AND 524288),
      ciphertext TEXT NOT NULL CHECK(length(ciphertext) BETWEEN 1 AND 2097152),
      created_at TEXT NOT NULL,
      UNIQUE(session_id,execution_id,lane,digest)
    )""",
    """CREATE TABLE model_context_generations (
      id TEXT PRIMARY KEY CHECK(id LIKE 'mcg_%'),
      session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
      execution_id TEXT NOT NULL REFERENCES turn_agent_execution(id) ON DELETE CASCADE,
      turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
      lane TEXT NOT NULL CHECK(lane IN ('ordinary','coordination')),
      worker_generation INTEGER NOT NULL CHECK(worker_generation>=1),
      kind TEXT NOT NULL CHECK(kind IN ('checkpoint','compact')),
      parent_id TEXT,
      source_refs_json TEXT NOT NULL CHECK(json_valid(source_refs_json) AND json_type(source_refs_json)='array'),
      snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json) AND json_type(snapshot_json)='object'),
      policy_json TEXT NOT NULL CHECK(json_valid(policy_json) AND json_type(policy_json)='object'),
      origin_json TEXT NOT NULL CHECK(json_valid(origin_json) AND json_type(origin_json)='object'),
      operation_id TEXT UNIQUE,
      completion_json TEXT CHECK(completion_json IS NULL OR (json_valid(completion_json) AND json_type(completion_json)='object')),
      created_at TEXT NOT NULL,
      UNIQUE(id,session_id,execution_id,lane),
      CHECK((kind='checkpoint' AND operation_id IS NULL AND completion_json IS NULL)
        OR (kind='compact' AND operation_id IS NOT NULL AND completion_json IS NOT NULL))
    )""",
    "CREATE INDEX idx_model_context_generations_session ON model_context_generations(session_id,created_at,id)",
    """CREATE TABLE model_context_heads (
      session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
      execution_id TEXT NOT NULL REFERENCES turn_agent_execution(id) ON DELETE CASCADE,
      lane TEXT NOT NULL CHECK(lane IN ('ordinary','coordination')),
      generation_id TEXT NOT NULL,
      PRIMARY KEY(execution_id,lane),
      FOREIGN KEY(generation_id,session_id,execution_id,lane)
        REFERENCES model_context_generations(id,session_id,execution_id,lane) ON DELETE CASCADE
    )""",
    """CREATE TABLE model_context_conversation_heads (
      session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
      generation_id TEXT NOT NULL REFERENCES model_context_generations(id) ON DELETE CASCADE,
      published_turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
      published_turn_seq INTEGER NOT NULL CHECK(published_turn_seq>=1)
    )""",
    """CREATE TRIGGER trg_model_context_segment_scope BEFORE INSERT ON model_context_segments
      WHEN NOT EXISTS(SELECT 1 FROM turn_agent_execution e JOIN turns t ON t.id=e.turn_id
        JOIN sessions s ON s.id=t.session_id JOIN workspaces w ON w.id=s.workspace_id
        WHERE e.id=NEW.execution_id AND s.id=NEW.session_id AND e.team_id=w.team_id AND s.deleted_at IS NULL)
      BEGIN SELECT RAISE(ABORT,'model context segment scope mismatch'); END""",
    """CREATE TRIGGER trg_model_context_generation_scope BEFORE INSERT ON model_context_generations
      WHEN NOT EXISTS(SELECT 1 FROM turn_agent_execution e JOIN turns t ON t.id=e.turn_id
        JOIN sessions s ON s.id=t.session_id JOIN workspaces w ON w.id=s.workspace_id
        WHERE e.id=NEW.execution_id AND t.id=NEW.turn_id AND s.id=NEW.session_id
          AND e.team_id=w.team_id AND s.deleted_at IS NULL
          AND ((COALESCE(t.terminal_protocol_version,0)=0 AND t.execution_generation=NEW.worker_generation)
            OR (t.terminal_protocol_version=1 AND t.current_generation=NEW.worker_generation)))
      OR (NEW.parent_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM model_context_generations p
        WHERE p.id=NEW.parent_id AND p.session_id=NEW.session_id AND p.execution_id=NEW.execution_id AND p.lane=NEW.lane))
      BEGIN SELECT RAISE(ABORT,'model context generation scope mismatch'); END""",
    """CREATE TRIGGER trg_model_context_segment_immutable BEFORE UPDATE ON model_context_segments
      BEGIN SELECT RAISE(ABORT,'model context segment is immutable'); END""",
    """CREATE TRIGGER trg_model_context_generation_immutable BEFORE UPDATE ON model_context_generations
      BEGIN SELECT RAISE(ABORT,'model context generation is immutable'); END""",
    """CREATE TRIGGER trg_model_context_segment_no_replace BEFORE INSERT ON model_context_segments
      WHEN EXISTS(SELECT 1 FROM model_context_segments WHERE id=NEW.id)
      BEGIN SELECT RAISE(ABORT,'model context segment already exists'); END""",
    """CREATE TRIGGER trg_model_context_generation_no_replace BEFORE INSERT ON model_context_generations
      WHEN EXISTS(SELECT 1 FROM model_context_generations WHERE id=NEW.id)
      BEGIN SELECT RAISE(ABORT,'model context generation already exists'); END""",
    """CREATE TRIGGER trg_model_context_conversation_scope_insert BEFORE INSERT ON model_context_conversation_heads
      WHEN NOT EXISTS(SELECT 1 FROM model_context_generations g JOIN turns t ON t.id=g.turn_id
        WHERE g.id=NEW.generation_id AND g.session_id=NEW.session_id AND t.id=NEW.published_turn_id
          AND t.session_id=NEW.session_id AND t.seq=NEW.published_turn_seq AND g.lane='ordinary')
      BEGIN SELECT RAISE(ABORT,'model context conversation scope mismatch'); END""",
    """CREATE TRIGGER trg_model_context_conversation_scope_update BEFORE UPDATE ON model_context_conversation_heads
      WHEN NEW.session_id IS NOT OLD.session_id OR NEW.published_turn_seq<OLD.published_turn_seq
      OR NOT EXISTS(SELECT 1 FROM model_context_generations g JOIN turns t ON t.id=g.turn_id
        WHERE g.id=NEW.generation_id AND g.session_id=NEW.session_id AND t.id=NEW.published_turn_id
          AND t.session_id=NEW.session_id AND t.seq=NEW.published_turn_seq AND g.lane='ordinary')
      BEGIN SELECT RAISE(ABORT,'model context conversation scope mismatch'); END""",
    """CREATE TRIGGER trg_model_context_session_purge AFTER UPDATE OF deleted_at ON sessions
      WHEN NEW.deleted_at IS NOT NULL
      BEGIN
        DELETE FROM model_context_conversation_heads WHERE session_id=NEW.id;
        DELETE FROM model_context_heads WHERE session_id=NEW.id;
        DELETE FROM model_context_generations WHERE session_id=NEW.id;
        DELETE FROM model_context_segments WHERE session_id=NEW.id;
        DELETE FROM continuation_payload_segments WHERE session_id=NEW.id;
      END""",
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT model_context")
    try:
        for statement in DDL:
            db.exec_driver_sql(statement)
        db.exec_driver_sql("RELEASE SAVEPOINT model_context")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT model_context")
        db.exec_driver_sql("RELEASE SAVEPOINT model_context")
        raise


def downgrade() -> None:
    raise NotImplementedError("Restore the paired database snapshot to roll back.")
