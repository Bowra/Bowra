"""Retain Feishu authorization callback addresses independently of entry metadata."""

from alembic import context

revision = "0175_oauth_callback_uri"
down_revision = "0174_slack_oauth_entry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    columns = {row[1] for row in db.exec_driver_sql("PRAGMA table_info(oauth_state)")}
    if "redirect_uri" not in columns:
        db.exec_driver_sql("ALTER TABLE oauth_state ADD COLUMN redirect_uri TEXT")


def downgrade() -> None:
    raise NotImplementedError("Restore matching database snapshots to roll back.")
