"""Retain encrypted empty-output diagnostics in the original verification item."""

from alembic import context

revision = "0217_probe_diagnostics"
down_revision = "0216_document_task_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] == "core":
        context.get_bind().exec_driver_sql("ALTER TABLE team_llm_verification_items ADD COLUMN diagnostics_ciphertext TEXT")


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database archive to roll back.")
