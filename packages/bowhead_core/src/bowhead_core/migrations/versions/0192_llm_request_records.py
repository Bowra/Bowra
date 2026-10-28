"""Store diagnostic LLM request records and retire the unread usage source ciphertext.

Core gains three tables for the operator-only request record
(docs/design/llm-request-record-tech-design.md): one row per actual model send
(`llm_request_records`), its encrypted body chunks deduplicated within the Turn
(`llm_request_chunks`), and content-free reasons why model-visible content was
changed (`llm_request_changes`). Deleting a Turn or soft-deleting its session
removes all three.

`llm_usage_events.source_ciphertext` and `source_expires_at` were written and
purged but never read. The write-once trigger from 0135 references them, so it is
recreated with the same rules minus those two columns before they are dropped.
Every other target advances its ledger unchanged.
"""

from alembic import context

revision = "0192_llm_request_records"
down_revision = "0191_agent_review_release_note"
branch_labels = None
depends_on = None

CHANGE_KINDS = ("tool_result_capped", "tool_result_cleared", "history_turns_dropped", "overflow_shrink", "chat_direct_demoted")

PHYSICAL_USAGE_IDENTITY_TRIGGER = """
    CREATE TRIGGER trg_physical_usage_identity BEFORE UPDATE ON llm_usage_events
    WHEN (OLD.receipt_json IS NULL AND NEW.receipt_json IS NOT NULL) OR (OLD.receipt_json IS NOT NULL AND (
      NEW.id IS NOT OLD.id OR NEW.team_id IS NOT OLD.team_id OR NEW.user_id IS NOT OLD.user_id
      OR NEW.workspace_id IS NOT OLD.workspace_id OR NEW.turn_id IS NOT OLD.turn_id
      OR NEW.reason IS NOT OLD.reason OR NEW.agent_type IS NOT OLD.agent_type
      OR NEW.provider IS NOT OLD.provider OR NEW.model IS NOT OLD.model
      OR NEW.streamed IS NOT OLD.streamed OR NEW.created_at IS NOT OLD.created_at
      OR NEW.receipt_json IS NOT OLD.receipt_json
      OR NEW.receipt_state IS NULL
      OR (OLD.receipt_state <> 'pending' AND (
        NEW.receipt_state IS NOT OLD.receipt_state OR NEW.tokens_input IS NOT OLD.tokens_input
        OR NEW.tokens_output IS NOT OLD.tokens_output OR NEW.tokens_cached IS NOT OLD.tokens_cached
        OR NEW.response_digest IS NOT OLD.response_digest OR NEW.outcome_code IS NOT OLD.outcome_code
        OR NEW.settled_at IS NOT OLD.settled_at))))
    BEGIN SELECT RAISE(ABORT,'physical usage receipt is immutable'); END
"""

TABLES = (
    """
    CREATE TABLE IF NOT EXISTS llm_request_records (
      id TEXT PRIMARY KEY CHECK(id LIKE 'lrq_%'),
      turn_id TEXT NOT NULL,
      session_id TEXT NOT NULL,
      team_id TEXT NOT NULL,
      user_id TEXT NOT NULL,
      workspace_id TEXT NOT NULL,
      reason TEXT NOT NULL,
      usage_event_id TEXT,
      subagent_id TEXT,
      provider TEXT NOT NULL,
      model TEXT NOT NULL,
      family TEXT,
      mode TEXT NOT NULL CHECK(mode IN ('invoke','stream')),
      request_digest TEXT NOT NULL CHECK(length(request_digest)=64),
      chunk_hashes_json TEXT NOT NULL CHECK(json_valid(chunk_hashes_json) AND json_type(chunk_hashes_json)='array'),
      created_at TEXT NOT NULL,
      expires_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_request_chunks (
      turn_id TEXT NOT NULL,
      chunk_hash TEXT NOT NULL CHECK(length(chunk_hash)=64),
      ciphertext TEXT,
      byte_size INTEGER NOT NULL CHECK(byte_size>=0),
      omitted INTEGER NOT NULL CHECK(omitted IN (0,1)),
      created_at TEXT NOT NULL,
      PRIMARY KEY(turn_id, chunk_hash),
      CHECK((omitted=1)=(ciphertext IS NULL))
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS llm_request_changes (
      id TEXT PRIMARY KEY CHECK(id LIKE 'lrc_%'),
      record_id TEXT NOT NULL,
      kind TEXT NOT NULL CHECK(kind IN ({",".join(f"'{kind}'" for kind in CHANGE_KINDS)})),
      detail_ciphertext TEXT NOT NULL,
      created_at TEXT NOT NULL
    )
    """,
)

INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_llm_request_records_turn ON llm_request_records(turn_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_llm_request_records_expiry ON llm_request_records(expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_llm_request_records_usage ON llm_request_records(usage_event_id) WHERE usage_event_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_llm_request_records_user ON llm_request_records(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_llm_request_changes_record ON llm_request_changes(record_id)",
)

TRIGGERS = (
    """
    CREATE TRIGGER IF NOT EXISTS trg_llm_request_turn_delete AFTER DELETE ON turns
    BEGIN
      DELETE FROM llm_request_records WHERE turn_id=OLD.id;
      DELETE FROM llm_request_chunks WHERE turn_id=OLD.id;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_llm_request_session_delete AFTER UPDATE OF deleted_at ON sessions
    WHEN OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL
    BEGIN
      DELETE FROM llm_request_records WHERE session_id=NEW.id;
      DELETE FROM llm_request_chunks WHERE turn_id IN (SELECT id FROM turns WHERE session_id=NEW.id);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_llm_request_record_delete AFTER DELETE ON llm_request_records
    BEGIN DELETE FROM llm_request_changes WHERE record_id=OLD.id; END
    """,
)


def _columns(connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()}


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    connection.exec_driver_sql("SAVEPOINT llm_request_records")
    try:
        usage_columns = _columns(connection, "llm_usage_events")
        if "receipt_json" not in usage_columns:
            raise RuntimeError("llm_usage_events receipts are missing; upgrade through 0135 first")
        retired = [column for column in ("source_ciphertext", "source_expires_at") if column in usage_columns]
        if retired:
            connection.exec_driver_sql("DROP TRIGGER IF EXISTS trg_physical_usage_identity")
            for column in retired:
                connection.exec_driver_sql(f"ALTER TABLE llm_usage_events DROP COLUMN {column}")
            connection.exec_driver_sql(PHYSICAL_USAGE_IDENTITY_TRIGGER)
        for statement in (*TABLES, *INDEXES, *TRIGGERS):
            connection.exec_driver_sql(statement)
    except BaseException:
        connection.exec_driver_sql("ROLLBACK TO SAVEPOINT llm_request_records")
        connection.exec_driver_sql("RELEASE SAVEPOINT llm_request_records")
        raise
    connection.exec_driver_sql("RELEASE SAVEPOINT llm_request_records")


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")
