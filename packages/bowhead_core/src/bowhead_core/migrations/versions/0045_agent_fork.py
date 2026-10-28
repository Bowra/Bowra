"""Agent M4-C Fork policy and immutable lineage.

The revision is expand-only.  Existing Agents default to ``not_allowed`` when
no policy row exists, and no Agent is copied by the migration itself.

Revision ID: 0045_agent_fork
Revises: 0044_agent_taxonomy
Create Date: 2026-09-06
"""

from __future__ import annotations

from typing import Any

from alembic import context

revision = "0045_agent_fork"
down_revision = "0044_agent_taxonomy"
branch_labels = None
depends_on = None


_OBJECTS: tuple[tuple[str, str, str], ...] = (
    (
        "table",
        "team_subagent_fork_policy",
        """
        CREATE TABLE team_subagent_fork_policy (
          team_id         TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          source_agent_id TEXT NOT NULL,
          policy          TEXT NOT NULL
            CHECK(policy IN ('not_allowed','recipients','team_members')),
          row_version     INTEGER NOT NULL DEFAULT 1 CHECK(row_version >= 1),
          updated_by      TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
          created_at      TEXT NOT NULL,
          updated_at      TEXT NOT NULL,
          PRIMARY KEY(team_id,source_agent_id),
          FOREIGN KEY(team_id,source_agent_id)
            REFERENCES team_subagent(team_id,id) ON DELETE RESTRICT
        ) WITHOUT ROWID
        """,
    ),
    (
        "table",
        "team_subagent_fork_lineage",
        """
        CREATE TABLE team_subagent_fork_lineage (
          id                       TEXT PRIMARY KEY CHECK(id LIKE 'afk_%'),
          team_id                  TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          fork_agent_id            TEXT NOT NULL,
          source_agent_id          TEXT NOT NULL,
          source_kind              TEXT NOT NULL CHECK(source_kind IN ('team','system')),
          source_revision_ref      TEXT NOT NULL,
          source_release_id        TEXT,
          source_owner_user_id     TEXT,
          source_display_name      TEXT NOT NULL CHECK(length(source_display_name) BETWEEN 1 AND 240),
          policy_snapshot_json     TEXT NOT NULL CHECK(
                                     length(policy_snapshot_json) BETWEEN 2 AND 4000
                                     AND json_valid(policy_snapshot_json)
                                     AND json_type(policy_snapshot_json)='object'
                                   ),
          source_snapshot_hash     TEXT NOT NULL CHECK(source_snapshot_hash LIKE 'sha256:%'),
          fork_revision_ref        TEXT NOT NULL,
          dependency_gaps_json     TEXT NOT NULL CHECK(
                                     length(dependency_gaps_json) BETWEEN 2 AND 32000
                                     AND json_valid(dependency_gaps_json)
                                     AND json_type(dependency_gaps_json)='array'
                                   ),
          idempotency_hash         TEXT NOT NULL CHECK(idempotency_hash LIKE 'sha256:%'),
          request_hash             TEXT NOT NULL CHECK(request_hash LIKE 'sha256:%'),
          created_by               TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
          created_at               TEXT NOT NULL,
          UNIQUE(team_id,fork_agent_id),
          UNIQUE(team_id,created_by,idempotency_hash),
          FOREIGN KEY(team_id,fork_agent_id)
            REFERENCES team_subagent(team_id,id) ON DELETE RESTRICT
        )
        """,
    ),
    (
        "index",
        "idx_team_subagent_fork_source",
        """
        CREATE INDEX idx_team_subagent_fork_source
        ON team_subagent_fork_lineage(team_id,source_agent_id,created_at DESC,id)
        """,
    ),
    (
        "trigger",
        "trg_team_subagent_fork_policy_insert",
        """
        CREATE TRIGGER trg_team_subagent_fork_policy_insert
        BEFORE INSERT ON team_subagent_fork_policy
        WHEN NEW.row_version<>1
          OR NOT EXISTS (
            SELECT 1
            FROM team_subagent agent
            JOIN users actor ON actor.id=NEW.updated_by AND actor.status='active'
            JOIN team_members member
              ON member.team_id=agent.team_id AND member.user_id=actor.id
             AND member.status='active'
            WHERE agent.team_id=NEW.team_id AND agent.id=NEW.source_agent_id
              AND COALESCE(agent.owner_user_id,agent.created_by)=NEW.updated_by
          )
        BEGIN
          SELECT RAISE(ABORT, 'agent fork policy owner mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_team_subagent_fork_policy_update",
        """
        CREATE TRIGGER trg_team_subagent_fork_policy_update
        BEFORE UPDATE ON team_subagent_fork_policy
        WHEN NEW.team_id IS NOT OLD.team_id
          OR NEW.source_agent_id IS NOT OLD.source_agent_id
          OR NEW.created_at IS NOT OLD.created_at
          OR NEW.row_version<>OLD.row_version+1
          OR NEW.updated_at<=OLD.updated_at
          OR NOT EXISTS (
            SELECT 1
            FROM team_subagent agent
            JOIN users actor ON actor.id=NEW.updated_by AND actor.status='active'
            JOIN team_members member
              ON member.team_id=agent.team_id AND member.user_id=actor.id
             AND member.status='active'
            WHERE agent.team_id=NEW.team_id AND agent.id=NEW.source_agent_id
              AND COALESCE(agent.owner_user_id,agent.created_by)=NEW.updated_by
          )
        BEGIN
          SELECT RAISE(ABORT, 'agent fork policy update mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_team_subagent_fork_policy_delete",
        """
        CREATE TRIGGER trg_team_subagent_fork_policy_delete
        BEFORE DELETE ON team_subagent_fork_policy
        BEGIN
          SELECT RAISE(ABORT, 'agent fork policy history is immutable');
        END
        """,
    ),
    (
        "trigger",
        "trg_team_subagent_fork_lineage_insert",
        """
        CREATE TRIGGER trg_team_subagent_fork_lineage_insert
        BEFORE INSERT ON team_subagent_fork_lineage
        WHEN NOT EXISTS (
          SELECT 1
          FROM team_subagent fork
          JOIN users actor ON actor.id=NEW.created_by AND actor.status='active'
          JOIN team_members member
            ON member.team_id=fork.team_id AND member.user_id=actor.id
           AND member.status='active'
          WHERE fork.team_id=NEW.team_id AND fork.id=NEW.fork_agent_id
            AND COALESCE(fork.owner_user_id,fork.created_by)=NEW.created_by
            AND fork.audience_mode='private' AND fork.visibility='creator_only'
            AND fork.definition_mode='revisioned'
            AND fork.live_revision_id=NEW.fork_revision_ref
            AND fork.live_release_id IS NOT NULL
            AND (
              NEW.source_kind<>'team'
              OR EXISTS (
                SELECT 1 FROM team_subagent source
                WHERE source.team_id=NEW.team_id AND source.id=NEW.source_agent_id
                  AND source.live_revision_id=NEW.source_revision_ref
                  AND source.live_release_id=NEW.source_release_id
                  AND COALESCE(source.owner_user_id,source.created_by)=NEW.source_owner_user_id
                  AND EXISTS (
                    SELECT 1 FROM team_subagent_fork_policy current_policy
                    WHERE current_policy.team_id=NEW.team_id
                      AND current_policy.source_agent_id=NEW.source_agent_id
                      AND current_policy.policy=json_extract(
                        CASE WHEN json_valid(NEW.policy_snapshot_json)
                          THEN NEW.policy_snapshot_json ELSE '{}' END,
                        '$.policy'
                      )
                      AND current_policy.row_version=json_extract(
                        CASE WHEN json_valid(NEW.policy_snapshot_json)
                          THEN NEW.policy_snapshot_json ELSE '{}' END,
                        '$.row_version'
                      )
                      AND current_policy.policy IN ('recipients','team_members')
                  )
              )
            )
        )
        BEGIN
          SELECT RAISE(ABORT, 'agent fork lineage scope mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_team_subagent_fork_lineage_update",
        """
        CREATE TRIGGER trg_team_subagent_fork_lineage_update
        BEFORE UPDATE ON team_subagent_fork_lineage
        BEGIN
          SELECT RAISE(ABORT, 'agent fork lineage is immutable');
        END
        """,
    ),
    (
        "trigger",
        "trg_team_subagent_fork_lineage_delete",
        """
        CREATE TRIGGER trg_team_subagent_fork_lineage_delete
        BEFORE DELETE ON team_subagent_fork_lineage
        BEGIN
          SELECT RAISE(ABORT, 'agent fork lineage is immutable');
        END
        """,
    ),
)


def _canonical_sql(value: str) -> str:
    return "".join(value.lower().replace("if not exists", "").split())


def _object_sql(connection: Any, *, object_type: str, name: str) -> str | None:
    row = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_schema WHERE type=? AND name=?",
        (object_type, name),
    ).fetchone()
    return str(row[0]) if row is not None and row[0] is not None else None


def _ensure_object(
    connection: Any,
    *,
    object_type: str,
    name: str,
    ddl: str,
) -> None:
    existing = _object_sql(connection, object_type=object_type, name=name)
    if existing is None:
        connection.exec_driver_sql(ddl)
        existing = _object_sql(connection, object_type=object_type, name=name)
    if existing is None or _canonical_sql(existing) != _canonical_sql(ddl):
        raise RuntimeError(f"0045 found incompatible {object_type} semantics: {name}")


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    for object_type, name, ddl in _OBJECTS:
        _ensure_object(connection, object_type=object_type, name=name, ddl=ddl)


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
