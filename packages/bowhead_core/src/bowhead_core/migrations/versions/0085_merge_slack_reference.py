"""Merge Slack channel and reference auto-receiver migration heads.

Revision ID: 0085_merge_slack_reference
Revises: 0084_slack_channel, 0084_reference_auto_receiver
"""

from alembic import context

revision = "0085_merge_slack_reference"
down_revision = ("0084_slack_channel", "0084_reference_auto_receiver")
branch_labels = None
depends_on = None


def upgrade() -> None:
    return


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore a matching backup to roll back.")
