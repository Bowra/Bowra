"""Bounded KB maintenance checkpoints and resumable user-authorized rebuilds."""
from alembic import context

revision = "0121_kb_maintenance"
down_revision = "0120_kb_generations"
branch_labels = None
depends_on = None


def upgrade():
    if context.config.attributes["bowhead_target"] != "files":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT kb_maintenance")
    try:
        db.exec_driver_sql("ALTER TABLE kb_files ADD COLUMN source_observation_json TEXT")
        db.exec_driver_sql("CREATE TABLE kb_maintenance_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.exec_driver_sql("""CREATE TABLE kb_rebuild_jobs (
            job_id TEXT PRIMARY KEY, team_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL, items_json TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL CHECK(state IN ('running','cancelling','paused','completed','cancelled')),
            outcomes_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        db.exec_driver_sql("CREATE INDEX idx_kb_rebuild_owner ON kb_rebuild_jobs(team_id,user_id,workspace_id,created_at)")
        db.exec_driver_sql("RELEASE SAVEPOINT kb_maintenance")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT kb_maintenance")
        db.exec_driver_sql("RELEASE SAVEPOINT kb_maintenance")
        raise


def downgrade():
    raise NotImplementedError("Forward-only; restore matching snapshots.")
