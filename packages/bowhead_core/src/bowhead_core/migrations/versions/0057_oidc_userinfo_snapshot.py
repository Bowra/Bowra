"""Persist the latest encrypted OIDC UserInfo snapshot.

Revision ID: 0057_oidc_userinfo_snapshot
Revises: 0056_plan_step_agent_choice
Create Date: 2026-09-08
"""

from __future__ import annotations

from alembic import context

revision = "0057_oidc_userinfo_snapshot"
down_revision = "0056_plan_step_agent_choice"
branch_labels = None
depends_on = None


_COLUMNS: tuple[tuple[str, str], ...] = (
    ("userinfo_encrypted", "TEXT"),
    ("userinfo_captured_at", "TEXT"),
    ("userinfo_schema_version", "INTEGER"),
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    columns = {
        str(row[1]): row
        for row in connection.exec_driver_sql(
            "PRAGMA table_info(oidc_identities)",
        ).fetchall()
    }
    if not columns:
        raise RuntimeError("OIDC identities table is missing")
    for name, column_type in _COLUMNS:
        current = columns.get(name)
        if current is None:
            connection.exec_driver_sql(
                f"ALTER TABLE oidc_identities ADD COLUMN {name} {column_type}",
            )
            continue
        if str(current[2]).upper() != column_type or int(current[3]) != 0:
            raise RuntimeError(f"OIDC UserInfo snapshot column drift: {name}")


def downgrade() -> None:
    raise NotImplementedError("Forward-only; retain encrypted OIDC UserInfo snapshots.")
