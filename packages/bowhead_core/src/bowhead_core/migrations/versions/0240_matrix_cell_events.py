"""Add cell history and a visibility setting to the matrix target.

Only the matrix target changes (document matrix step 1, S4). ``kb_matrix_cell_events`` records
who changed a cell and when: edits, keep-as-text, undoing an edit, verifying and undoing it, AI
values, a rerun superseding a manual value and the automatic retry. Each row is written in the
same transaction as the cell and carries the cell's new ``seq``; it holds no cell content.
``kb_matrices`` gains ``visibility`` by ``ADD COLUMN`` with the constant default ``'space'``, so
existing matrices keep today's behaviour and the table is not rebuilt; only-me matrices (S8) use
it later. The whole revision runs in one savepoint.
"""

from alembic import context

revision = "0240_matrix_cell_events"
down_revision = "0239_knowledge_bases"
branch_labels = None
depends_on = None

STATEMENTS = (
    "ALTER TABLE kb_matrices ADD COLUMN visibility TEXT NOT NULL DEFAULT 'space' CHECK(visibility IN ('space','private'))",
    """CREATE TABLE kb_matrix_cell_events (
        event_id TEXT PRIMARY KEY,
        matrix_id TEXT NOT NULL,
        file_id TEXT NOT NULL,
        column_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        team_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('edited','kept_as_text','edit_reverted','verified','verify_reverted',
                                          'ai_value','edit_superseded','auto_retried')),
        actor_user_id TEXT,
        run_id TEXT,
        cell_seq INTEGER NOT NULL,
        batch_id TEXT,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX idx_kb_matrix_cell_events_cell ON kb_matrix_cell_events(matrix_id, file_id, column_id, created_at)",
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "matrix":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT matrix_cell_events")
    try:
        for statement in STATEMENTS:
            db.exec_driver_sql(statement)
        db.exec_driver_sql("RELEASE matrix_cell_events")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO matrix_cell_events")
        db.exec_driver_sql("RELEASE matrix_cell_events")
        raise


def downgrade() -> None:
    raise NotImplementedError("Restore the paired database snapshot to roll back.")
