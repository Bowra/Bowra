"""Add team-scoped Slack channel connections.

Revision ID: 0084_slack_channel
Revises: 0083_reference_read_checkpoint
"""

from alembic import context

revision = "0084_slack_channel"
down_revision = "0083_reference_read_checkpoint"
branch_labels = None
depends_on = None


_DDL = """
CREATE TABLE slack_connections (
  id TEXT PRIMARY KEY CHECK(id LIKE 'slc_%'),
  team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT UNIQUE,
  slack_team_id TEXT NOT NULL DEFAULT '' UNIQUE,
  client_id TEXT NOT NULL DEFAULT '',
  client_secret_ref TEXT,
  signing_secret_ref TEXT,
  bot_token_ref TEXT,
  bot_user_id TEXT NOT NULL DEFAULT '',
  channel_answers_enabled INTEGER NOT NULL DEFAULT 0 CHECK(channel_answers_enabled IN (0,1)),
  status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','verified','enabled','disabled')),
  created_by_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  updated_by_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_slack_connections_lookup ON slack_connections(id, status);
CREATE INDEX idx_slack_connections_team_lookup ON slack_connections(slack_team_id, status);
CREATE TABLE slack_oauth_states (
  state TEXT PRIMARY KEY,
  connection_id TEXT NOT NULL REFERENCES slack_connections(id) ON DELETE CASCADE,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE slack_identity_bindings (
  connection_id TEXT NOT NULL REFERENCES slack_connections(id) ON DELETE CASCADE,
  slack_user_id TEXT NOT NULL,
  bowhead_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(connection_id, slack_user_id),
  UNIQUE(connection_id, bowhead_user_id)
);
CREATE TABLE slack_workspace_bindings (
  connection_id TEXT NOT NULL REFERENCES slack_connections(id) ON DELETE CASCADE,
  conversation_id TEXT NOT NULL,
  slack_user_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(connection_id, conversation_id, slack_user_id)
);
CREATE TABLE slack_event_dedup (
  connection_id TEXT NOT NULL REFERENCES slack_connections(id) ON DELETE CASCADE,
  event_id TEXT NOT NULL,
  received_at TEXT NOT NULL,
  PRIMARY KEY(connection_id, event_id)
);
"""


def upgrade() -> None:
    if context.config.attributes.get("bowhead_target") != "core":
        return
    db = context.get_bind()
    for statement in _DDL.split(";\n"):
        if statement.strip():
            db.exec_driver_sql(statement)


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore a matching backup to roll back.")
