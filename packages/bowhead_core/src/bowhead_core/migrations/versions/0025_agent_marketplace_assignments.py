"""Agent Marketplace M1：个人 assignment、团队默认、消费侧投影与迁移标记。

本 revision 只做 additive schema expand；存量可见 Agent 的 assignment 回填由独立、
可重入的 ``bowhead admin backfill-agent-assignments`` 命令完成。只有回填对账成功后，
命令才会写 ``assignment_cutover_complete_at``，runtime 不会在半迁移状态下缩小候选池。

Revision ID: 0025_agent_marketplace_assignments
Revises: 0024_memory_item_metadata
Create Date: 2026-08-30
"""

from __future__ import annotations

from alembic import context

revision = "0025_agent_marketplace_assignments"
down_revision = "0024_memory_item_metadata"
branch_labels = None
depends_on = None


_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS agent_assignment (
      id                       TEXT PRIMARY KEY CHECK(id LIKE 'asa_%'),
      team_id                  TEXT NOT NULL,
      user_id                  TEXT NOT NULL,
      agent_id                 TEXT NOT NULL,
      agent_source_kind        TEXT NOT NULL CHECK(agent_source_kind IN ('team','system')),
      personal_state           TEXT NOT NULL CHECK(personal_state IN ('active','disabled','removed')),
      access_state             TEXT NOT NULL CHECK(access_state IN ('granted','revoked')),
      source                   TEXT NOT NULL CHECK(source IN (
                                 'user_hired','migration_existing_access','team_default','former_team_default'
                               )),
      source_policy_version    INTEGER,
      row_version              INTEGER NOT NULL DEFAULT 1 CHECK(row_version >= 1),
      hired_at                 TEXT NOT NULL,
      current_cycle_started_at TEXT NOT NULL,
      personal_state_changed_at TEXT NOT NULL,
      access_state_changed_at  TEXT NOT NULL,
      removed_at               TEXT,
      created_at               TEXT NOT NULL,
      updated_at               TEXT NOT NULL,
      UNIQUE(team_id, user_id, agent_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_assignment_user_state "
    "ON agent_assignment(team_id, user_id, personal_state, access_state)",
    "CREATE INDEX IF NOT EXISTS idx_agent_assignment_agent_state "
    "ON agent_assignment(team_id, agent_id, personal_state, access_state)",
    """
    CREATE TABLE IF NOT EXISTS agent_default_policy (
      team_id           TEXT NOT NULL,
      agent_id          TEXT NOT NULL,
      agent_source_kind TEXT NOT NULL CHECK(agent_source_kind IN ('team','system')),
      enabled           INTEGER NOT NULL CHECK(enabled IN (0,1)),
      policy_version    INTEGER NOT NULL CHECK(policy_version >= 1),
      row_version       INTEGER NOT NULL DEFAULT 1 CHECK(row_version >= 1),
      changed_by        TEXT NOT NULL,
      created_at        TEXT NOT NULL,
      updated_at        TEXT NOT NULL,
      PRIMARY KEY(team_id, agent_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_default_opt_out (
      team_id                   TEXT NOT NULL,
      user_id                   TEXT NOT NULL,
      agent_id                  TEXT NOT NULL,
      policy_version_at_opt_out INTEGER NOT NULL,
      opted_out_at              TEXT NOT NULL,
      cleared_at                TEXT,
      row_version               INTEGER NOT NULL DEFAULT 1 CHECK(row_version >= 1),
      PRIMARY KEY(team_id, user_id, agent_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_catalog_projection (
      team_id                 TEXT NOT NULL,
      agent_id                TEXT NOT NULL,
      source_kind             TEXT NOT NULL CHECK(source_kind IN ('team','system')),
      source_version          TEXT NOT NULL,
      compiler_version        TEXT NOT NULL,
      canonical_name          TEXT NOT NULL,
      canonical_summary       TEXT NOT NULL,
      owner_label             TEXT,
      capability_summary_json TEXT NOT NULL,
      facet_json              TEXT NOT NULL,
      risk_summary_json       TEXT NOT NULL,
      safety_summary_json     TEXT NOT NULL,
      dependency_json         TEXT NOT NULL,
      search_text             TEXT NOT NULL,
      projection_hash         TEXT NOT NULL,
      built_at                TEXT NOT NULL,
      PRIMARY KEY(team_id, agent_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_catalog_search "
    "ON agent_catalog_projection(team_id, source_kind, canonical_name)",
    """
    CREATE TABLE IF NOT EXISTS agent_name_claim (
      team_id        TEXT NOT NULL,
      locale         TEXT NOT NULL,
      normalized_key TEXT NOT NULL,
      holder_kind    TEXT NOT NULL CHECK(holder_kind IN ('agent','candidate','legacy_conflict','system')),
      holder_id      TEXT NOT NULL,
      agent_id       TEXT,
      claimed_at     TEXT NOT NULL,
      PRIMARY KEY(team_id, locale, normalized_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_name_conflict_group (
      team_id        TEXT NOT NULL,
      normalized_key TEXT NOT NULL,
      agent_id       TEXT NOT NULL,
      detected_at    TEXT NOT NULL,
      resolved_at    TEXT,
      PRIMARY KEY(team_id, normalized_key, agent_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_marketplace_schema_meta (
      singleton                         INTEGER PRIMARY KEY CHECK(singleton = 1),
      schema_version                    INTEGER NOT NULL,
      assignment_cutover_complete_at    TEXT,
      assignment_backfill_expected      INTEGER,
      assignment_backfill_inserted      INTEGER,
      assignment_backfill_digest        TEXT,
      updated_at                        TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_assignment_backfill_run (
      id             TEXT PRIMARY KEY CHECK(id LIKE 'abf_%'),
      status         TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
      cursor_team_id TEXT,
      cursor_user_id TEXT,
      expected_count INTEGER NOT NULL DEFAULT 0,
      inserted_count INTEGER NOT NULL DEFAULT 0,
      conflict_count INTEGER NOT NULL DEFAULT 0,
      error_count    INTEGER NOT NULL DEFAULT 0,
      source_build   TEXT,
      started_at     TEXT NOT NULL,
      updated_at     TEXT NOT NULL,
      finished_at    TEXT
    )
    """,
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    for statement in _DDL:
        connection.exec_driver_sql(statement)
    connection.exec_driver_sql(
        """
        INSERT INTO agent_marketplace_schema_meta(
          singleton, schema_version, assignment_cutover_complete_at, updated_at
        )
        VALUES (
          1,
          1,
          NULL,
          strftime('%Y-%m-%dT%H:%M:%fZ','now')
        )
        ON CONFLICT(singleton) DO UPDATE SET
          schema_version=MAX(agent_marketplace_schema_meta.schema_version, excluded.schema_version),
          updated_at=excluded.updated_at
        """,
    )


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
