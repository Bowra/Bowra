"""Agent Marketplace M2-A deterministic listing safety reports.

Revision ID: 0028_agent_listing_safety
Revises: 0027_team_openapi
Create Date: 2026-09-02
"""

from __future__ import annotations

from alembic import context

revision = "0028_agent_listing_safety"
down_revision = "0027_team_openapi"
branch_labels = None
depends_on = None


_DDL: tuple[str, ...] = (
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_listing_safety_parent
    ON team_subagent(team_id,id)
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_listing_safety_report (
      id                       TEXT PRIMARY KEY CHECK(id LIKE 'alsr_%'),
      team_id                  TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
      agent_id                 TEXT NOT NULL,
      listing_hash             TEXT NOT NULL CHECK(listing_hash LIKE 'sha256:%'),
      scanner_version          TEXT NOT NULL,
      deterministic_status     TEXT NOT NULL CHECK(deterministic_status IN ('pass','blocked','error')),
      semantic_status          TEXT NOT NULL CHECK(semantic_status IN ('not_run','pass','needs_review','blocked','error')),
      effective_status         TEXT NOT NULL CHECK(effective_status IN ('pass','needs_review','blocked')),
      team_dictionary_revision TEXT NOT NULL,
      created_at               TEXT NOT NULL,
      completed_at             TEXT NOT NULL,
      UNIQUE(team_id,agent_id,listing_hash,scanner_version,team_dictionary_revision),
      FOREIGN KEY(team_id,agent_id) REFERENCES team_subagent(team_id,id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_agent_listing_safety_agent_time
    ON agent_listing_safety_report(team_id,agent_id,created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_agent_listing_safety_effective
    ON agent_listing_safety_report(team_id,effective_status,created_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_listing_safety_finding (
      report_id    TEXT NOT NULL REFERENCES agent_listing_safety_report(id) ON DELETE CASCADE,
      ordinal      INTEGER NOT NULL CHECK(ordinal >= 0),
      rule_family  TEXT NOT NULL,
      field_path   TEXT NOT NULL,
      severity     TEXT NOT NULL CHECK(severity IN ('critical','high','medium','low')),
      disposition  TEXT NOT NULL CHECK(disposition IN ('open','acknowledged','resolved')),
      reviewer_id  TEXT,
      reviewed_at  TEXT,
      PRIMARY KEY(report_id,ordinal)
    ) WITHOUT ROWID
    """,
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    for statement in _DDL:
        connection.exec_driver_sql(statement)


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
