"""Agent catalog taxonomy, governed team tags, and reviewable suggestions.

The revision is expand-only and does not classify or rewrite existing Agents.
Product writes remain behind FEATURE_AGENT_INTELLIGENCE_M4.

Revision ID: 0044_agent_taxonomy
Revises: 0043_office_upload_confirmation
Create Date: 2026-09-05
"""

from __future__ import annotations

from typing import Any

from alembic import context

revision = "0044_agent_taxonomy"
down_revision = "0043_office_upload_confirmation"
branch_labels = None
depends_on = None


_OBJECTS: tuple[tuple[str, str, str], ...] = (
    (
        "table",
        "agent_team_tag",
        """
        CREATE TABLE agent_team_tag (
          id             TEXT PRIMARY KEY CHECK(id LIKE 'att_%'),
          team_id        TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          display_name   TEXT NOT NULL CHECK(length(display_name) BETWEEN 1 AND 120),
          normalized_key TEXT NOT NULL CHECK(length(normalized_key) BETWEEN 1 AND 240),
          status         TEXT NOT NULL CHECK(status IN ('active','inactive','merged')),
          merged_into_id TEXT,
          row_version    INTEGER NOT NULL DEFAULT 1 CHECK(row_version >= 1),
          created_by     TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
          updated_by     TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
          created_at     TEXT NOT NULL,
          updated_at     TEXT NOT NULL,
          UNIQUE(team_id,id),
          UNIQUE(team_id,normalized_key),
          FOREIGN KEY(team_id,merged_into_id)
            REFERENCES agent_team_tag(team_id,id) ON DELETE RESTRICT,
          CHECK(
            (status='merged' AND merged_into_id IS NOT NULL AND merged_into_id<>id)
            OR (status IN ('active','inactive') AND merged_into_id IS NULL)
          )
        )
        """,
    ),
    (
        "index",
        "idx_agent_team_tag_governance",
        """
        CREATE INDEX idx_agent_team_tag_governance
        ON agent_team_tag(team_id,status,updated_at DESC,id)
        """,
    ),
    (
        "table",
        "agent_taxonomy_suggestion",
        """
        CREATE TABLE agent_taxonomy_suggestion (
          id                    TEXT PRIMARY KEY CHECK(id LIKE 'ats_%'),
          team_id               TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          agent_id              TEXT NOT NULL,
          working_copy_id       TEXT NOT NULL,
          source_hash           TEXT NOT NULL CHECK(source_hash LIKE 'sha256:%'),
          taxonomy_version      TEXT NOT NULL CHECK(length(taxonomy_version) BETWEEN 1 AND 80),
          suggestion_json       TEXT NOT NULL CHECK(length(suggestion_json) BETWEEN 2 AND 16000),
          confidence            REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
          state                 TEXT NOT NULL CHECK(state IN ('pending','accepted','dismissed','stale')),
          row_version           INTEGER NOT NULL DEFAULT 1 CHECK(row_version >= 1),
          created_by            TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
          reviewed_by           TEXT REFERENCES users(id) ON DELETE RESTRICT,
          reviewed_at           TEXT,
          created_at            TEXT NOT NULL,
          updated_at            TEXT NOT NULL,
          FOREIGN KEY(team_id,agent_id)
            REFERENCES team_subagent(team_id,id) ON DELETE RESTRICT,
          FOREIGN KEY(working_copy_id)
            REFERENCES team_subagent_working_copy(id) ON DELETE RESTRICT,
          CHECK(
            (state='pending' AND reviewed_by IS NULL AND reviewed_at IS NULL)
            OR (state IN ('accepted','dismissed') AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)
            OR state='stale'
          )
        )
        """,
    ),
    (
        "index",
        "idx_agent_taxonomy_suggestion_manager",
        """
        CREATE INDEX idx_agent_taxonomy_suggestion_manager
        ON agent_taxonomy_suggestion(team_id,agent_id,working_copy_id,state,created_at DESC,id)
        """,
    ),
    (
        "index",
        "uq_agent_taxonomy_suggestion_pending",
        """
        CREATE UNIQUE INDEX uq_agent_taxonomy_suggestion_pending
        ON agent_taxonomy_suggestion(team_id,agent_id,working_copy_id)
        WHERE state='pending'
        """,
    ),
    (
        "trigger",
        "trg_agent_team_tag_insert_scope",
        """
        CREATE TRIGGER trg_agent_team_tag_insert_scope
        BEFORE INSERT ON agent_team_tag
        WHEN NOT EXISTS (
          SELECT 1
          FROM teams team
          JOIN users actor ON actor.id=NEW.created_by AND actor.status='active'
          JOIN team_members member
            ON member.team_id=NEW.team_id AND member.user_id=actor.id
           AND member.status='active'
          WHERE team.id=NEW.team_id AND team.status='active'
        ) OR NEW.updated_by<>NEW.created_by
        BEGIN
          SELECT RAISE(ABORT, 'agent team tag scope mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_team_tag_update",
        """
        CREATE TRIGGER trg_agent_team_tag_update
        BEFORE UPDATE ON agent_team_tag
        WHEN NEW.id IS NOT OLD.id
          OR NEW.team_id IS NOT OLD.team_id
          OR NEW.display_name IS NOT OLD.display_name
          OR NEW.normalized_key IS NOT OLD.normalized_key
          OR NEW.created_by IS NOT OLD.created_by
          OR NEW.created_at IS NOT OLD.created_at
          OR NEW.row_version<>OLD.row_version+1
          OR NEW.updated_at<=OLD.updated_at
          OR OLD.status='merged'
          OR (OLD.status='active' AND NEW.status NOT IN ('inactive','merged'))
          OR (OLD.status='inactive' AND NEW.status NOT IN ('active','merged'))
          OR (
            NEW.status='merged' AND NOT EXISTS (
              SELECT 1 FROM agent_team_tag target
              WHERE target.team_id=NEW.team_id AND target.id=NEW.merged_into_id
                AND target.status='active'
            )
          )
          OR (
            NEW.status IN ('active','inactive') AND NEW.merged_into_id IS NOT NULL
          )
        BEGIN
          SELECT RAISE(ABORT, 'agent team tag update mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_team_tag_delete",
        """
        CREATE TRIGGER trg_agent_team_tag_delete
        BEFORE DELETE ON agent_team_tag
        BEGIN
          SELECT RAISE(ABORT, 'agent team tag history is immutable');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_taxonomy_suggestion_insert_scope",
        """
        CREATE TRIGGER trg_agent_taxonomy_suggestion_insert_scope
        BEFORE INSERT ON agent_taxonomy_suggestion
        WHEN NEW.state<>'pending'
          OR NOT EXISTS (
            SELECT 1
            FROM team_subagent_working_copy working
            JOIN team_subagent agent
              ON agent.team_id=working.team_id AND agent.id=working.subagent_id
            JOIN teams team ON team.id=working.team_id AND team.status='active'
            JOIN users actor ON actor.id=NEW.created_by AND actor.status='active'
            JOIN team_members member
              ON member.team_id=working.team_id AND member.user_id=actor.id
             AND member.status='active'
            WHERE working.team_id=NEW.team_id
              AND working.subagent_id=NEW.agent_id
              AND working.id=NEW.working_copy_id
              AND COALESCE(agent.owner_user_id,agent.created_by)=NEW.created_by
          )
        BEGIN
          SELECT RAISE(ABORT, 'agent taxonomy suggestion scope mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_taxonomy_suggestion_update",
        """
        CREATE TRIGGER trg_agent_taxonomy_suggestion_update
        BEFORE UPDATE ON agent_taxonomy_suggestion
        WHEN NEW.id IS NOT OLD.id
          OR NEW.team_id IS NOT OLD.team_id
          OR NEW.agent_id IS NOT OLD.agent_id
          OR NEW.working_copy_id IS NOT OLD.working_copy_id
          OR NEW.source_hash IS NOT OLD.source_hash
          OR NEW.taxonomy_version IS NOT OLD.taxonomy_version
          OR NEW.suggestion_json IS NOT OLD.suggestion_json
          OR NEW.confidence IS NOT OLD.confidence
          OR NEW.created_by IS NOT OLD.created_by
          OR NEW.created_at IS NOT OLD.created_at
          OR NEW.row_version<>OLD.row_version+1
          OR NEW.updated_at<=OLD.updated_at
          OR OLD.state<>'pending'
          OR NEW.state NOT IN ('accepted','dismissed','stale')
          OR (
            NEW.state IN ('accepted','dismissed')
            AND (NEW.reviewed_by IS NULL OR NEW.reviewed_at IS NULL)
          )
          OR (
            NEW.state='stale'
            AND (NEW.reviewed_by IS NOT NULL OR NEW.reviewed_at IS NOT NULL)
          )
        BEGIN
          SELECT RAISE(ABORT, 'agent taxonomy suggestion update mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_taxonomy_suggestion_delete",
        """
        CREATE TRIGGER trg_agent_taxonomy_suggestion_delete
        BEFORE DELETE ON agent_taxonomy_suggestion
        BEGIN
          SELECT RAISE(ABORT, 'agent taxonomy suggestion history is immutable');
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
        raise RuntimeError(f"0044 found incompatible {object_type} semantics: {name}")


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    for object_type, name, ddl in _OBJECTS:
        _ensure_object(connection, object_type=object_type, name=name, ddl=ddl)


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
