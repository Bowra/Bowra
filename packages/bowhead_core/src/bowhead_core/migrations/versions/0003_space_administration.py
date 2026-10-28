"""Add the Space administration expand schema.

Revision ID: 0003_space_administration
Revises: 0002_remove_public_entry_provider_status
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import context

revision = "0003_space_administration"
down_revision = "0002_remove_public_entry_provider_status"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    connection = context.get_bind()
    return {str(row[1]) for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")}


def _add_column(table: str, column: str, definition: str) -> None:
    if column in _columns(table):
        return
    context.get_bind().exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return

    _add_column("workspaces", "name_key", "TEXT")
    _add_column("workspaces", "revision", "INTEGER NOT NULL DEFAULT 1")

    _add_column(
        "team_invite_tokens",
        "assignment_mode",
        "TEXT NOT NULL DEFAULT 'team_only_legacy' CHECK(assignment_mode IN ('spaces', 'team_only', 'team_only_legacy'))",
    )
    _add_column(
        "team_invite_tokens",
        "assignment_revision",
        "INTEGER NOT NULL DEFAULT 1",
    )
    _add_column("team_invite_tokens", "public_entry_id", "TEXT")

    _add_column("scheduled_jobs", "pause_reason", "TEXT")

    connection = context.get_bind()
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS team_invite_workspace_grants (
            invite_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin', 'member', 'viewer')),
            created_at TEXT NOT NULL,
            PRIMARY KEY (invite_id, workspace_id),
            FOREIGN KEY (invite_id)
                REFERENCES team_invite_tokens(id) ON DELETE CASCADE,
            FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS
            ix_team_invite_workspace_grants_workspace
        ON team_invite_workspace_grants(workspace_id, invite_id)
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS artifact_upload_bindings (
            artifact_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL DEFAULT '',
            user_id TEXT NOT NULL,
            session_id TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (artifact_id)
                REFERENCES artifacts(id) ON DELETE CASCADE
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS idx_artifact_upload_bindings_workspace
        ON artifact_upload_bindings(workspace_id, artifact_id)
        """
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS ix_workspaces_team_status_name
        ON workspaces(team_id, status, name_key, id)
        """
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_workspace_status
        ON scheduled_jobs(workspace_id, status)
        """
    )


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
