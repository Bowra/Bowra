"""Pin Slack authorization callback addresses without rewriting legacy state."""

from alembic import context

revision = "0174_slack_oauth_entry"
down_revision = "0173_model_credit_rates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT slack_oauth_entry")
    try:
        db.exec_driver_sql("ALTER TABLE slack_oauth_states ADD COLUMN redirect_uri TEXT")
        db.exec_driver_sql("ALTER TABLE personal_slack_oauth_states ADD COLUMN redirect_uri TEXT")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT slack_oauth_entry")
        db.exec_driver_sql("RELEASE SAVEPOINT slack_oauth_entry")
        raise
    db.exec_driver_sql("RELEASE SAVEPOINT slack_oauth_entry")


def downgrade() -> None:
    raise NotImplementedError("Restore matching database snapshots to roll back.")
