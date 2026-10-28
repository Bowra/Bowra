"""Remove advisory public-entry OAuth callback status records.

Revision ID: 0002_remove_public_entry_provider_status
Revises: 0001_initial
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import context


revision = "0002_remove_public_entry_provider_status"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    connection.exec_driver_sql("DROP TABLE IF EXISTS public_entry_provider_status")


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
