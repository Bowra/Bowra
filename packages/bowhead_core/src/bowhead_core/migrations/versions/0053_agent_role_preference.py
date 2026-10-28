"""Add assignment-cycle scoped personal Agent role preferences.

The preference is a routing hint, never an access grant.  Database guards bind
it to an active personal assignment and the exact hire cycle; canonical Agent
access and immutable behavior eligibility remain request-time service checks.

Revision ID: 0053_agent_role_preference
Revises: 0052_scheduled_binding_safety_join
Create Date: 2026-09-07
"""

from __future__ import annotations

from alembic import context

revision = "0053_agent_role_preference"
down_revision = "0052_scheduled_binding_safety_join"
branch_labels = None
depends_on = None


_OBJECTS: tuple[tuple[str, str, str], ...] = (
    (
        "table",
        "agent_role_preference",
        """
        CREATE TABLE agent_role_preference (
          team_id                    TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          user_id                    TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
          role_id                    TEXT NOT NULL CHECK(role_id IN (
                                       'data','compute','deck','knowledge','canvas_edit','image'
                                     )),
          assignment_id              TEXT REFERENCES agent_assignment(id) ON DELETE RESTRICT,
          assignment_cycle_started_at TEXT,
          cleared_at                  TEXT,
          row_version                 INTEGER NOT NULL DEFAULT 1 CHECK(row_version>=1),
          created_at                  TEXT NOT NULL CHECK(length(created_at)>0),
          updated_at                  TEXT NOT NULL CHECK(length(updated_at)>0),
          CHECK(
            (assignment_id IS NOT NULL AND assignment_cycle_started_at IS NOT NULL
             AND length(assignment_cycle_started_at)>0 AND cleared_at IS NULL)
            OR
            (assignment_id IS NULL AND assignment_cycle_started_at IS NULL
             AND cleared_at IS NOT NULL AND length(cleared_at)>0)
          ),
          PRIMARY KEY(team_id,user_id,role_id)
        ) WITHOUT ROWID
        """,
    ),
    (
        "index",
        "idx_agent_role_preference_assignment",
        """
        CREATE INDEX idx_agent_role_preference_assignment
        ON agent_role_preference(assignment_id)
        """,
    ),
    (
        "trigger",
        "trg_agent_role_preference_insert_scope",
        """
        CREATE TRIGGER trg_agent_role_preference_insert_scope
        BEFORE INSERT ON agent_role_preference
        WHEN NEW.assignment_id IS NULL OR NOT EXISTS (
          SELECT 1
          FROM teams team
          JOIN users actor ON actor.id=NEW.user_id AND actor.status='active'
          JOIN team_members member
            ON member.team_id=team.id AND member.user_id=actor.id AND member.status='active'
          JOIN agent_assignment assignment
            ON assignment.id=NEW.assignment_id
           AND assignment.team_id=team.id
           AND assignment.user_id=actor.id
           AND assignment.agent_source_kind='team'
           AND assignment.personal_state='active'
           AND assignment.current_cycle_started_at=NEW.assignment_cycle_started_at
          WHERE team.id=NEW.team_id AND team.status='active'
        )
        BEGIN
          SELECT RAISE(ABORT, 'Agent role preference assignment mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_role_preference_update_guard",
        """
        CREATE TRIGGER trg_agent_role_preference_update_guard
        BEFORE UPDATE ON agent_role_preference
        WHEN NEW.team_id IS NOT OLD.team_id
          OR NEW.user_id IS NOT OLD.user_id
          OR NEW.role_id IS NOT OLD.role_id
          OR NEW.created_at IS NOT OLD.created_at
          OR NEW.row_version<>OLD.row_version+1
          OR (
            NEW.assignment_id IS NOT NULL AND NOT EXISTS (
            SELECT 1
            FROM teams team
            JOIN users actor ON actor.id=NEW.user_id AND actor.status='active'
            JOIN team_members member
              ON member.team_id=team.id AND member.user_id=actor.id AND member.status='active'
            JOIN agent_assignment assignment
              ON assignment.id=NEW.assignment_id
             AND assignment.team_id=team.id
             AND assignment.user_id=actor.id
             AND assignment.agent_source_kind='team'
             AND assignment.personal_state='active'
             AND assignment.current_cycle_started_at=NEW.assignment_cycle_started_at
            WHERE team.id=NEW.team_id AND team.status='active'
            )
          )
        BEGIN
          SELECT RAISE(ABORT, 'Agent role preference update mismatch');
        END
        """,
    ),
)


def _normalized_sql(value: str) -> str:
    return "".join(value.lower().split())


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    for kind, name, ddl in _OBJECTS:
        current = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_schema WHERE type=? AND name=?",
            (kind, name),
        ).scalar()
        if current is None:
            connection.exec_driver_sql(ddl)
        elif _normalized_sql(str(current)) != _normalized_sql(ddl):
            raise RuntimeError(f"Agent role preference schema drift: {name}")


def downgrade() -> None:
    raise NotImplementedError("Forward-only; retain personal Agent role preferences.")
