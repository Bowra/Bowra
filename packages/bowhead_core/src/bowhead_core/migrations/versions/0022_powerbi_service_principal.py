"""Replace the experimental delegated Power BI registry with Service Principal storage.

The old table was created by a startup-only experimental store and contains refresh-token
credentials, which must never be selected by the Service Principal runtime. Preserve it under a
revision-specific legacy name for operator recovery, then create the authoritative team-scoped
connection and DAX-template tables.

Revision ID: 0022_powerbi_service_principal
Revises: 0021_artifact_origin_query
"""

from __future__ import annotations

from alembic import context

revision = "0022_powerbi_service_principal"
down_revision = "0021_artifact_origin_query"
branch_labels = None
depends_on = None


_CREATE_CONNECTION_TABLE = """
CREATE TABLE IF NOT EXISTS pbi_connection (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    name TEXT NOT NULL,
    auth_ref TEXT NOT NULL,
    secret_hint TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'archived')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_CREATE_CONNECTION_TEAM_INDEX = "CREATE INDEX IF NOT EXISTS idx_pbi_connection_team ON pbi_connection(team_id, status)"

_CREATE_TEMPLATE_TABLE = """
CREATE TABLE IF NOT EXISTS pbi_dax_template (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    name TEXT NOT NULL,
    dax_template TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(connection_id, name),
    FOREIGN KEY(connection_id) REFERENCES pbi_connection(id) ON DELETE CASCADE
);
"""

_CREATE_TEMPLATE_CONNECTION_INDEX = "CREATE INDEX IF NOT EXISTS idx_pbi_dax_template_connection ON pbi_dax_template(team_id, connection_id)"


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    existing = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pbi_connection'"
    ).fetchone()
    legacy = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pbi_connection_legacy_0022'"
    ).fetchone()
    if existing and not legacy:
        connection.exec_driver_sql("ALTER TABLE pbi_connection RENAME TO pbi_connection_legacy_0022")
        connection.exec_driver_sql("DROP INDEX IF EXISTS idx_pbi_connection_team")
    connection.exec_driver_sql(_CREATE_CONNECTION_TABLE)
    connection.exec_driver_sql(_CREATE_CONNECTION_TEAM_INDEX)
    connection.exec_driver_sql(_CREATE_TEMPLATE_TABLE)
    connection.exec_driver_sql(_CREATE_TEMPLATE_CONNECTION_INDEX)


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
