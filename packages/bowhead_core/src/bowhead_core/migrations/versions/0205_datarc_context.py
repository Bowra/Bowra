"""Register scoped Datarc preferences and accepted source continuation evidence."""

revision = "0205_datarc_context"
down_revision = "0204_document_canvas_inputs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError("Restore the paired database snapshot to roll back.")
