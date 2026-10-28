"""Record which adopted suggestion a Space instructions version came from."""

from alembic import context

revision = "0181_space_instruction_source"
down_revision = "0180_coach_draft_undo"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    columns = {row[1] for row in db.exec_driver_sql("PRAGMA table_info(space_instruction_versions)").fetchall()}
    if "source_candidate_id" not in columns:
        # Nullable without a default: ADD COLUMN copies no rows, and historical versions keep null.
        db.exec_driver_sql("ALTER TABLE space_instruction_versions ADD COLUMN source_candidate_id TEXT")
    db.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_space_instruction_versions_source "
        "ON space_instruction_versions(source_candidate_id) WHERE source_candidate_id IS NOT NULL"
    )


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")
