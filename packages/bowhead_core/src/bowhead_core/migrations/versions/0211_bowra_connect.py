"""Add encrypted, short-lived Bowra connection handoffs."""

from alembic import context

revision = "0211_bowra_connect"
down_revision = "0210_datarc_preference_backfill"
branch_labels = None
depends_on = None

TABLE_SQL = """CREATE TABLE bowra_connect_pending (
 token_hash TEXT PRIMARY KEY,
 team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 kind TEXT NOT NULL CHECK(kind IN ('authorization','import')),
 payload_cipher TEXT NOT NULL CHECK(payload_cipher LIKE 'FERNET:%'),
 expires_at REAL NOT NULL
)"""


def upgrade():
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT bowra_connect")
    try:
        db.exec_driver_sql(TABLE_SQL)
        db.exec_driver_sql("CREATE INDEX idx_bowra_connect_expiry ON bowra_connect_pending(expires_at)")
        db.exec_driver_sql("RELEASE SAVEPOINT bowra_connect")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT bowra_connect")
        db.exec_driver_sql("RELEASE SAVEPOINT bowra_connect")
        raise


def downgrade():
    raise NotImplementedError("Restore the matching database archive to roll back.")
