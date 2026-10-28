"""Extend the existing usage ledger with scoped physical-call receipts."""

from alembic import context

revision = "0135_physical_usage_receipts"
down_revision = "0134_installation_identity"
branch_labels = None
depends_on = None

COLUMNS = (
    "receipt_json TEXT CHECK(receipt_json IS NULL OR json_valid(receipt_json))",
    "receipt_state TEXT CHECK(receipt_state IS NULL OR receipt_state IN ('pending','completed','not_applied','unknown','usage_missing'))",
    "response_digest TEXT",
    "outcome_code TEXT",
    "settled_at TEXT",
    "source_ciphertext TEXT",
    "source_expires_at TEXT",
)


def upgrade():
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT physical_usage_receipts")
    try:
        for column in COLUMNS:
            db.exec_driver_sql(f"ALTER TABLE llm_usage_events ADD COLUMN {column}")
        db.exec_driver_sql("""
            CREATE TRIGGER trg_physical_usage_identity BEFORE UPDATE ON llm_usage_events
            WHEN (OLD.receipt_json IS NULL AND NEW.receipt_json IS NOT NULL) OR (OLD.receipt_json IS NOT NULL AND (
              NEW.id IS NOT OLD.id OR NEW.team_id IS NOT OLD.team_id OR NEW.user_id IS NOT OLD.user_id
              OR NEW.workspace_id IS NOT OLD.workspace_id OR NEW.turn_id IS NOT OLD.turn_id
              OR NEW.reason IS NOT OLD.reason OR NEW.agent_type IS NOT OLD.agent_type
              OR NEW.provider IS NOT OLD.provider OR NEW.model IS NOT OLD.model
              OR NEW.streamed IS NOT OLD.streamed OR NEW.created_at IS NOT OLD.created_at
              OR NEW.receipt_json IS NOT OLD.receipt_json OR NEW.source_expires_at IS NOT OLD.source_expires_at
              OR (NEW.source_ciphertext IS NOT NULL AND NEW.source_ciphertext IS NOT OLD.source_ciphertext)
              OR NEW.receipt_state IS NULL
              OR (OLD.receipt_state <> 'pending' AND (
                NEW.receipt_state IS NOT OLD.receipt_state OR NEW.tokens_input IS NOT OLD.tokens_input
                OR NEW.tokens_output IS NOT OLD.tokens_output OR NEW.tokens_cached IS NOT OLD.tokens_cached
                OR NEW.response_digest IS NOT OLD.response_digest OR NEW.outcome_code IS NOT OLD.outcome_code
                OR NEW.settled_at IS NOT OLD.settled_at))))
            BEGIN SELECT RAISE(ABORT,'physical usage receipt is immutable'); END
        """)
        db.exec_driver_sql("""
            CREATE TRIGGER trg_physical_usage_turn_delete AFTER DELETE ON turns
            BEGIN DELETE FROM llm_usage_events WHERE turn_id=OLD.id AND receipt_json IS NOT NULL; END
        """)
        db.exec_driver_sql("RELEASE SAVEPOINT physical_usage_receipts")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT physical_usage_receipts")
        db.exec_driver_sql("RELEASE SAVEPOINT physical_usage_receipts")
        raise


def downgrade():
    raise NotImplementedError("Restore the matching database snapshot to roll back.")
