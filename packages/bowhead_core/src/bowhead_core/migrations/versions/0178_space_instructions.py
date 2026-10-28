"""Add versioned Space instructions and the version each Turn received."""

from alembic import context

revision = "0178_space_instructions"
down_revision = "0177_chart_numeric_display"
branch_labels = None
depends_on = None


STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS space_instruction_versions (
        workspace_id TEXT NOT NULL,
        version INTEGER NOT NULL CHECK(version >= 1),
        content TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        restored_from INTEGER,
        PRIMARY KEY (workspace_id, version)
    )""",
    """CREATE TABLE IF NOT EXISTS turn_space_instructions (
        turn_id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        version INTEGER NOT NULL CHECK(version >= 1),
        created_at TEXT NOT NULL
    )""",
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    for statement in STATEMENTS:
        db.exec_driver_sql(statement)


def downgrade() -> None:
    raise NotImplementedError("Restore matching database snapshots to roll back.")
