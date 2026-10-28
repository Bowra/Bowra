"""Durable exact Agent binding and per-Turn execution identity.

M3-B records the immutable Agent revision selected for a root task or a
dynamic Lead dispatch.  The revision is polymorphic: Team Agents reference an
immutable ``team_subagent_revision`` row while system Agents reference an
immutable effective-definition snapshot.  Triggers enforce the tagged
reference and tenant boundary that SQLite cannot express as one foreign key.

Revision ID: 0034_agent_turn_binding
Revises: 0033_agent_release_cutover
Create Date: 2026-09-04
"""

from __future__ import annotations

from typing import Any

from alembic import context

revision = "0034_agent_turn_binding"
down_revision = "0033_agent_release_cutover"
branch_labels = None
depends_on = None


_OBJECTS: tuple[tuple[str, str, str], ...] = (
    (
        "table",
        "system_agent_effective_revision",
        """
        CREATE TABLE system_agent_effective_revision (
          id                    TEXT PRIMARY KEY CHECK(id LIKE 'arv_%'),
          team_id               TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          agent_id              TEXT NOT NULL CHECK(agent_id LIKE 'system:%'),
          manifest_fingerprint  TEXT NOT NULL CHECK(manifest_fingerprint LIKE 'sha256:%'),
          override_version      INTEGER CHECK(override_version IS NULL OR override_version >= 1),
          definition_json       TEXT NOT NULL CHECK(length(definition_json) > 0),
          definition_hash       TEXT NOT NULL CHECK(definition_hash LIKE 'sha256:%'),
          created_at            TEXT NOT NULL,
          UNIQUE(team_id, agent_id, definition_hash),
          UNIQUE(team_id, agent_id, id)
        )
        """,
    ),
    (
        "table",
        "turn_agent_binding",
        """
        CREATE TABLE turn_agent_binding (
          turn_id                TEXT PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
          team_id                TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          user_id                TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
          assignment_id          TEXT REFERENCES agent_assignment(id) ON DELETE RESTRICT,
          requested_agent_id     TEXT NOT NULL,
          agent_source_kind      TEXT NOT NULL CHECK(agent_source_kind IN ('team','system')),
          revision_ref           TEXT NOT NULL CHECK(revision_ref LIKE 'arv_%'),
          release_id             TEXT,
          binding_source         TEXT NOT NULL CHECK(binding_source IN (
                                   'marketplace','detail','hr','mention','schedule','plan'
                                 )),
          capability_fingerprint TEXT NOT NULL CHECK(capability_fingerprint LIKE 'sha256:%'),
          policy_fingerprint     TEXT NOT NULL CHECK(policy_fingerprint LIKE 'sha256:%'),
          created_at             TEXT NOT NULL
        )
        """,
    ),
    (
        "table",
        "turn_agent_execution",
        """
        CREATE TABLE turn_agent_execution (
          id                   TEXT PRIMARY KEY CHECK(id LIKE 'aex_%'),
          team_id              TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          turn_id              TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
          parent_execution_id  TEXT REFERENCES turn_agent_execution(id) ON DELETE CASCADE,
          agent_id             TEXT NOT NULL,
          agent_source_kind    TEXT NOT NULL CHECK(agent_source_kind IN ('team','system')),
          revision_ref         TEXT NOT NULL CHECK(revision_ref LIKE 'arv_%'),
          release_id           TEXT,
          dispatch_source      TEXT NOT NULL CHECK(dispatch_source IN ('explicit','lead','plan','system')),
          dispatch_key         TEXT NOT NULL CHECK(length(dispatch_key) > 0),
          runtime_profile_name TEXT NOT NULL CHECK(length(runtime_profile_name) > 0),
          capability_fingerprint TEXT NOT NULL CHECK(capability_fingerprint LIKE 'sha256:%'),
          policy_fingerprint   TEXT NOT NULL CHECK(policy_fingerprint LIKE 'sha256:%'),
          status               TEXT NOT NULL CHECK(status IN (
                                 'admitted','running','completed','failed','blocked','cancelled'
                               )),
          created_at           TEXT NOT NULL,
          started_at           TEXT,
          ended_at             TEXT,
          failure_code         TEXT,
          UNIQUE(turn_id, dispatch_source, dispatch_key),
          CHECK(status != 'admitted' OR (
            started_at IS NULL AND ended_at IS NULL AND failure_code IS NULL
          )),
          CHECK(status != 'running' OR (
            started_at IS NOT NULL AND ended_at IS NULL AND failure_code IS NULL
          )),
          CHECK(status IN ('admitted','running') OR ended_at IS NOT NULL),
          CHECK(failure_code IS NULL OR status IN ('failed','blocked'))
        )
        """,
    ),
    (
        "index",
        "idx_turn_agent_execution_turn",
        """
        CREATE INDEX idx_turn_agent_execution_turn
        ON turn_agent_execution(turn_id, created_at, id)
        """,
    ),
    (
        "index",
        "idx_turn_agent_execution_agent",
        """
        CREATE INDEX idx_turn_agent_execution_agent
        ON turn_agent_execution(team_id, agent_source_kind, agent_id, created_at)
        """,
    ),
    (
        "trigger",
        "trg_system_agent_effective_revision_immutable_update",
        """
        CREATE TRIGGER trg_system_agent_effective_revision_immutable_update
        BEFORE UPDATE ON system_agent_effective_revision
        BEGIN
          SELECT RAISE(ABORT, 'system_agent_effective_revision is immutable');
        END
        """,
    ),
    (
        "trigger",
        "trg_system_agent_effective_revision_immutable_delete",
        """
        CREATE TRIGGER trg_system_agent_effective_revision_immutable_delete
        BEFORE DELETE ON system_agent_effective_revision
        BEGIN
          SELECT RAISE(ABORT, 'system_agent_effective_revision is immutable');
        END
        """,
    ),
    (
        "trigger",
        "trg_turn_agent_binding_scope",
        """
        CREATE TRIGGER trg_turn_agent_binding_scope
        BEFORE INSERT ON turn_agent_binding
        WHEN NOT EXISTS (
          SELECT 1
          FROM turns turn_row
          JOIN sessions session_row ON session_row.id=turn_row.session_id
          JOIN workspaces workspace_row
            ON workspace_row.id=session_row.workspace_id AND workspace_row.status='active'
          JOIN users actor ON actor.id=NEW.user_id AND actor.status='active'
          JOIN team_members member
            ON member.team_id=NEW.team_id AND member.user_id=NEW.user_id
           AND member.status='active'
          JOIN teams team ON team.id=NEW.team_id AND team.status='active'
          WHERE turn_row.id=NEW.turn_id
            AND turn_row.status='running'
            AND session_row.user_id=NEW.user_id
            AND workspace_row.team_id=NEW.team_id
        )
        BEGIN
          SELECT RAISE(ABORT, 'turn Agent binding scope mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_turn_agent_binding_assignment",
        """
        CREATE TRIGGER trg_turn_agent_binding_assignment
        BEFORE INSERT ON turn_agent_binding
        WHEN (
          NEW.agent_source_kind='team' AND NEW.assignment_id IS NULL
        ) OR (
          NEW.assignment_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM agent_assignment assignment
            WHERE assignment.id=NEW.assignment_id
              AND assignment.team_id=NEW.team_id
              AND assignment.user_id=NEW.user_id
              AND assignment.agent_id=NEW.requested_agent_id
              AND assignment.agent_source_kind=NEW.agent_source_kind
              AND assignment.personal_state='active'
              AND assignment.access_state='granted'
          )
        )
        BEGIN
          SELECT RAISE(ABORT, 'turn Agent binding assignment mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_turn_agent_binding_revision",
        """
        CREATE TRIGGER trg_turn_agent_binding_revision
        BEFORE INSERT ON turn_agent_binding
        WHEN (
          NEW.agent_source_kind='team'
          AND (
            NEW.release_id IS NULL
            OR NOT EXISTS (
              SELECT 1
              FROM team_subagent_release release
              WHERE release.team_id=NEW.team_id
                AND release.subagent_id=NEW.requested_agent_id
                AND release.id=NEW.release_id
                AND release.revision_id=NEW.revision_ref
                AND release.capability_fingerprint=NEW.capability_fingerprint
            )
          )
        ) OR (
          NEW.agent_source_kind='system'
          AND (
            NEW.release_id IS NOT NULL
            OR NOT EXISTS (
              SELECT 1
              FROM system_agent_effective_revision revision
              WHERE revision.team_id=NEW.team_id
                AND revision.agent_id=NEW.requested_agent_id
                AND revision.id=NEW.revision_ref
            )
          )
        )
        BEGIN
          SELECT RAISE(ABORT, 'turn Agent binding revision mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_turn_agent_binding_immutable_update",
        """
        CREATE TRIGGER trg_turn_agent_binding_immutable_update
        BEFORE UPDATE ON turn_agent_binding
        BEGIN
          SELECT RAISE(ABORT, 'turn_agent_binding is immutable');
        END
        """,
    ),
    (
        "trigger",
        "trg_turn_agent_execution_scope",
        """
        CREATE TRIGGER trg_turn_agent_execution_scope
        BEFORE INSERT ON turn_agent_execution
        WHEN NOT EXISTS (
          SELECT 1
          FROM turns turn_row
          JOIN sessions session_row ON session_row.id=turn_row.session_id
          JOIN workspaces workspace_row
            ON workspace_row.id=session_row.workspace_id AND workspace_row.status='active'
          JOIN users actor ON actor.id=session_row.user_id AND actor.status='active'
          JOIN team_members member
            ON member.team_id=workspace_row.team_id AND member.user_id=actor.id
           AND member.status='active'
          JOIN teams team ON team.id=workspace_row.team_id AND team.status='active'
          WHERE turn_row.id=NEW.turn_id
            AND turn_row.status='running'
            AND workspace_row.team_id=NEW.team_id
        ) OR (
          NEW.parent_execution_id IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM turn_agent_execution parent
            WHERE parent.id=NEW.parent_execution_id
              AND parent.team_id=NEW.team_id
              AND parent.turn_id=NEW.turn_id
          )
        )
        BEGIN
          SELECT RAISE(ABORT, 'turn Agent execution scope mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_turn_agent_execution_revision",
        """
        CREATE TRIGGER trg_turn_agent_execution_revision
        BEFORE INSERT ON turn_agent_execution
        WHEN (
          NEW.agent_source_kind='team'
          AND (
            NEW.release_id IS NULL
            OR NOT EXISTS (
              SELECT 1
              FROM team_subagent_release release
              WHERE release.team_id=NEW.team_id
                AND release.subagent_id=NEW.agent_id
                AND release.id=NEW.release_id
                AND release.revision_id=NEW.revision_ref
                AND release.capability_fingerprint=NEW.capability_fingerprint
            )
          )
        ) OR (
          NEW.agent_source_kind='system'
          AND (
            NEW.release_id IS NOT NULL
            OR NOT EXISTS (
              SELECT 1
              FROM system_agent_effective_revision revision
              WHERE revision.team_id=NEW.team_id
                AND revision.agent_id=NEW.agent_id
                AND revision.id=NEW.revision_ref
            )
          )
        )
        BEGIN
          SELECT RAISE(ABORT, 'turn Agent execution revision mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_turn_agent_execution_identity_immutable",
        """
        CREATE TRIGGER trg_turn_agent_execution_identity_immutable
        BEFORE UPDATE ON turn_agent_execution
        WHEN NEW.id IS NOT OLD.id
          OR NEW.team_id IS NOT OLD.team_id
          OR NEW.turn_id IS NOT OLD.turn_id
          OR NEW.parent_execution_id IS NOT OLD.parent_execution_id
          OR NEW.agent_id IS NOT OLD.agent_id
          OR NEW.agent_source_kind IS NOT OLD.agent_source_kind
          OR NEW.revision_ref IS NOT OLD.revision_ref
          OR NEW.release_id IS NOT OLD.release_id
          OR NEW.dispatch_source IS NOT OLD.dispatch_source
          OR NEW.dispatch_key IS NOT OLD.dispatch_key
          OR NEW.runtime_profile_name IS NOT OLD.runtime_profile_name
          OR NEW.capability_fingerprint IS NOT OLD.capability_fingerprint
          OR NEW.policy_fingerprint IS NOT OLD.policy_fingerprint
          OR NEW.created_at IS NOT OLD.created_at
        BEGIN
          SELECT RAISE(ABORT, 'turn_agent_execution identity is immutable');
        END
        """,
    ),
    (
        "trigger",
        "trg_turn_agent_execution_status_transition",
        """
        CREATE TRIGGER trg_turn_agent_execution_status_transition
        BEFORE UPDATE ON turn_agent_execution
        WHEN NOT (
          (
            NEW.status=OLD.status
            AND NEW.started_at IS OLD.started_at
            AND NEW.ended_at IS OLD.ended_at
            AND NEW.failure_code IS OLD.failure_code
          )
          OR (
            OLD.status='admitted' AND NEW.status='running'
            AND NEW.started_at IS NOT NULL
            AND NEW.ended_at IS NULL
            AND NEW.failure_code IS NULL
          )
          OR (
            OLD.status='admitted' AND NEW.status IN ('failed','blocked','cancelled')
            AND NEW.started_at IS OLD.started_at
            AND NEW.ended_at IS NOT NULL
          )
          OR (
            OLD.status='running' AND NEW.status IN ('completed','failed','blocked','cancelled')
            AND NEW.started_at IS OLD.started_at
            AND NEW.ended_at IS NOT NULL
          )
        )
        BEGIN
          SELECT RAISE(ABORT, 'invalid turn Agent execution transition');
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
        raise RuntimeError(f"0034 found incompatible {object_type} semantics: {name}")


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    for object_type, name, ddl in _OBJECTS:
        _ensure_object(
            connection,
            object_type=object_type,
            name=name,
            ddl=ddl,
        )


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
