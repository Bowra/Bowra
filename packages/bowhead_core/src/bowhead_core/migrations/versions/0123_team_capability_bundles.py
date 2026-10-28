"""Team-managed reusable Tool and Skill capabilities.

Revision ID: 0123_team_capability_bundles
Revises: 0122_builtin_powerbi
"""

from __future__ import annotations

from alembic import context

revision = "0123_team_capability_bundles"
down_revision = "0122_builtin_powerbi"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql(
        """CREATE TABLE team_capability (
          id TEXT PRIMARY KEY CHECK(id LIKE 'tcap_%'),
          team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          name_key TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('active','disabled')),
          current_revision INTEGER NOT NULL CHECK(current_revision >= 1),
          row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version >= 1),
          created_by TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(team_id, name_key),
          UNIQUE(team_id, id)
        )"""
    )
    db.exec_driver_sql(
        """CREATE TABLE team_capability_revision (
          capability_id TEXT NOT NULL REFERENCES team_capability(id) ON DELETE RESTRICT,
          revision INTEGER NOT NULL CHECK(revision >= 1),
          name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
          description TEXT NOT NULL CHECK(length(description) <= 1000),
          content_hash TEXT NOT NULL CHECK(content_hash LIKE 'sha256:%'),
          created_by TEXT NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY(capability_id, revision)
        )"""
    )
    db.exec_driver_sql(
        """CREATE TABLE team_capability_resource (
          capability_id TEXT NOT NULL,
          revision INTEGER NOT NULL,
          kind TEXT NOT NULL CHECK(kind IN ('tool','skill')),
          resource_ref TEXT NOT NULL CHECK(length(resource_ref) BETWEEN 1 AND 200),
          grant_json TEXT CHECK(grant_json IS NULL OR json_valid(grant_json)),
          PRIMARY KEY(capability_id, revision, kind, resource_ref),
          FOREIGN KEY(capability_id,revision)
            REFERENCES team_capability_revision(capability_id,revision) ON DELETE RESTRICT,
          CHECK((kind='tool' AND grant_json IS NULL) OR (kind='skill' AND grant_json IS NOT NULL))
        )"""
    )
    db.exec_driver_sql("CREATE INDEX idx_team_capability_status ON team_capability(team_id,status,updated_at)")
    db.exec_driver_sql(
        """CREATE TABLE team_subagent_capability_binding (
          team_id TEXT NOT NULL,
          subagent_id TEXT NOT NULL,
          revision_id TEXT NOT NULL,
          capability_id TEXT NOT NULL,
          PRIMARY KEY(revision_id,capability_id),
          FOREIGN KEY(team_id,subagent_id,revision_id)
            REFERENCES team_subagent_revision(team_id,subagent_id,id) ON DELETE RESTRICT,
          FOREIGN KEY(team_id,capability_id)
            REFERENCES team_capability(team_id,id) ON DELETE RESTRICT
        )"""
    )
    db.exec_driver_sql("CREATE INDEX idx_team_subagent_capability_binding ON team_subagent_capability_binding(team_id,capability_id,subagent_id)")
    db.exec_driver_sql("ALTER TABLE turn_agent_execution ADD COLUMN resolved_definition_json TEXT")


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
