"""Version shared KB policy and budgets in sealed execution checkpoints.

Ordinary Lead uses ordinary-lead-loop.v12; helper resume material uses version 2.
Existing checkpoints retain their original bytes and legacy routing policy.
"""

revision = "0197_knowledge_read_state"
down_revision = "0196_coach_case_edits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError("Restore the paired database snapshot to roll back.")
