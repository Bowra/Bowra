"""Freeze the exact SQLite schema created by Bowhead 1.0.0.

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-29
"""

from __future__ import annotations

from alembic import context

from bowhead_core.migrations.baseline_schema import initial_schema


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    target = context.config.attributes["bowhead_target"]
    connection = context.get_bind()
    raw_connection = connection.connection.driver_connection
    raw_connection.executescript(initial_schema(target))


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
