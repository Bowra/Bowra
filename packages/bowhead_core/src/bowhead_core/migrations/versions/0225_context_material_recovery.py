"""Version native material pointers and optional retained report-read observations."""

revision = "0225_context_material_recovery"
down_revision = "0224_document_task_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Optional JSON fields preserve existing events, checkpoints and source stores.
    pass
