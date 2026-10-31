"""Version exact model-decision budgets in existing sealed continuation payloads.

Historical payloads retain their original bytes and counting semantics. No
settings backfill, table change or new continuation capability is introduced.
"""

revision = "0200_agent_runtime_budget"
down_revision = "0199_knowledge_source_bindings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError("Restore the paired database snapshot to roll back.")
