"""Microsoft Teams personal-chat connection storage.

Revision ID: 0074_teams_personal_connection
Revises: 0073_agent_owner_eval_foundation
"""

from __future__ import annotations

from alembic import context

revision = "0074_teams_personal_connection"
down_revision = "0073_agent_owner_eval_foundation"
branch_labels = None
depends_on = None


_DDL = """
CREATE TABLE teams_connections (
  id TEXT PRIMARY KEY CHECK(id LIKE 'tmc_%'),
  team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
  cloud TEXT NOT NULL CHECK(cloud='global'),
  entra_tenant_id TEXT NOT NULL,
  microsoft_app_id TEXT NOT NULL,
  app_type TEXT NOT NULL CHECK(app_type IN ('SingleTenant','UserAssignedMSI')),
  app_secret_ref TEXT,
  managed_identity_client_id TEXT,
  sso_auth_app_id TEXT NOT NULL DEFAULT '',
  sso_audience TEXT NOT NULL DEFAULT '',
  sso_connection_name TEXT NOT NULL DEFAULT '',
  teams_manifest_id TEXT NOT NULL DEFAULT '',
  allowed_scopes_json TEXT NOT NULL DEFAULT '["personal"]' CHECK(allowed_scopes_json='["personal"]'),
  status TEXT NOT NULL CHECK(status IN ('draft','verified','enabled','disabled')) DEFAULT 'draft',
  secret_expires_at TEXT,
  created_by_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  updated_by_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(microsoft_app_id, entra_tenant_id),
  UNIQUE(team_id)
);
CREATE INDEX idx_teams_connections_lookup ON teams_connections(microsoft_app_id, entra_tenant_id, status);

CREATE TABLE teams_identity_bindings (
  entra_tenant_id TEXT NOT NULL,
  entra_oid TEXT NOT NULL,
  bowhead_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(entra_tenant_id, entra_oid),
  UNIQUE(team_id, bowhead_user_id)
);
CREATE TABLE teams_workspace_bindings (
  entra_tenant_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  entra_oid TEXT NOT NULL,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(entra_tenant_id, conversation_id, entra_oid)
);
CREATE TABLE teams_activity_dedup (
  activity_id TEXT PRIMARY KEY,
  received_at TEXT NOT NULL
);
"""


def upgrade() -> None:
    if context.config.attributes.get("bowhead_target") != "core":
        return
    connection = context.get_bind()
    for statement in _DDL.split(";\n"):
        if statement.strip():
            connection.exec_driver_sql(statement)


def downgrade() -> None:
    return
