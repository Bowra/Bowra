"""Protect the shared bounded-question envelope without changing old cards."""

from alembic import context

revision = "0136_clarification_rounds"
down_revision = "0135_physical_usage_receipts"
branch_labels = None
depends_on = None


def upgrade():
    if context.config.attributes["bowhead_target"] != "core":
        return
    context.get_bind().exec_driver_sql("""
        CREATE TRIGGER trg_clarification_round_identity BEFORE UPDATE ON clarification_requests
        WHEN json_valid(OLD.resume_context_json) AND json_type(OLD.resume_context_json,'$.question_round') IS NOT NULL
        AND (NEW.request_id IS NOT OLD.request_id OR NEW.created_at IS NOT OLD.created_at
          OR NEW.kind IS NOT OLD.kind OR NEW.tool IS NOT OLD.tool OR NEW.token IS NOT OLD.token
          OR NEW.turn_id IS NOT OLD.turn_id OR NEW.user_id IS NOT OLD.user_id
          OR NEW.workspace_id IS NOT OLD.workspace_id OR NEW.session_id IS NOT OLD.session_id
          OR NEW.resume_context_json IS NOT OLD.resume_context_json)
        BEGIN SELECT RAISE(ABORT,'clarification round identity is immutable'); END
    """)


def downgrade():
    raise NotImplementedError("Restore the matching database snapshot to roll back.")
