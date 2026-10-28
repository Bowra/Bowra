"""Merge independent Agent operations and reference baseline histories.

Revision ID: 0077_merge_agent_reference
Revises: 0075_agent_version_operations, 0076_reference_primary_baseline
Create Date: 2026-09-14 22:35:32.569487

"""
revision = "0077_merge_agent_reference"
down_revision = ("0075_agent_version_operations", "0076_reference_primary_baseline")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Join the two immutable histories; Alembic applies each missing branch.

    Both branches concern independent core objects. Their own revisions retain
    all DDL and validation. The seven KB targets still perform no schema writes.
    """
    pass


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore from snapshot to roll back.")
