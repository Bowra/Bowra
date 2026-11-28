"""Add optional Team-scoped SCIM provisioning and group publication."""

from alembic import context

revision = "0237_team_directory"
down_revision = "0236_wecom_integration"
branch_labels = depends_on = None

DDL = (
    "ALTER TABLE groups ADD COLUMN directory_managed INTEGER NOT NULL DEFAULT 0 CHECK(directory_managed IN (0,1))",
    """CREATE TABLE scim_connections (
        id TEXT PRIMARY KEY, team_id TEXT NOT NULL UNIQUE REFERENCES teams(id),
        tenant_url TEXT NOT NULL, token_hash TEXT, expires_at TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE scim_users (
        id TEXT PRIMARY KEY, connection_id TEXT NOT NULL REFERENCES scim_connections(id), user_id TEXT NOT NULL REFERENCES users(id),
        user_name TEXT NOT NULL COLLATE NOCASE, external_id TEXT, attributes TEXT NOT NULL, active INTEGER NOT NULL CHECK(active IN (0,1)),
        version INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(connection_id,user_name), UNIQUE(connection_id,user_id), UNIQUE(connection_id,id))""",
    "CREATE INDEX idx_scim_users_external ON scim_users(connection_id,external_id)",
    """CREATE TABLE scim_groups (
        id TEXT PRIMARY KEY, connection_id TEXT NOT NULL REFERENCES scim_connections(id),
        group_id TEXT UNIQUE REFERENCES groups(id), display_name TEXT NOT NULL COLLATE NOCASE, external_id TEXT,
        version INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(connection_id,id))""",
    "CREATE INDEX idx_scim_groups_external ON scim_groups(connection_id,external_id)",
    "CREATE INDEX idx_scim_groups_name ON scim_groups(connection_id,display_name)",
    """CREATE TABLE scim_members (
        connection_id TEXT NOT NULL, group_id TEXT NOT NULL, user_id TEXT NOT NULL,
        PRIMARY KEY(connection_id,group_id,user_id),
        FOREIGN KEY(connection_id,group_id) REFERENCES scim_groups(connection_id,id) ON DELETE CASCADE,
        FOREIGN KEY(connection_id,user_id) REFERENCES scim_users(connection_id,id) ON DELETE CASCADE)""",
    "CREATE INDEX idx_scim_members_user ON scim_members(connection_id,user_id)",
)


def upgrade():
    if context.config.attributes["bowhead_target"] == "core":
        for sql in DDL:
            context.get_bind().exec_driver_sql(sql)


def downgrade():
    pass
