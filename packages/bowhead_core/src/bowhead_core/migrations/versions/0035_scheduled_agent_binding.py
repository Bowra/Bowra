"""Normalized exact Agent pin and built-in routing responsibility.

The row is the durable user confirmation that a routine may execute one
specific immutable Agent revision.  Runtime access and dependency readiness
are still rechecked on every fire; the pin never grants access by itself.

Revision ID: 0035_scheduled_agent_binding
Revises: 0034_agent_turn_binding
Create Date: 2026-09-04
"""

from __future__ import annotations

from typing import Any

from alembic import context

revision = "0035_scheduled_agent_binding"
down_revision = "0034_agent_turn_binding"
branch_labels = None
depends_on = None


_OBJECTS: tuple[tuple[str, str, str], ...] = (
    (
        "table",
        "scheduled_job_agent_binding",
        """
        CREATE TABLE scheduled_job_agent_binding (
          job_id                    TEXT PRIMARY KEY
                                    REFERENCES scheduled_jobs(id) ON DELETE CASCADE,
          assignment_id             TEXT REFERENCES agent_assignment(id) ON DELETE RESTRICT,
          agent_id                  TEXT NOT NULL CHECK(length(agent_id) > 0),
          agent_source_kind         TEXT NOT NULL CHECK(agent_source_kind IN ('team','system')),
          revision_ref              TEXT NOT NULL CHECK(revision_ref LIKE 'arv_%'),
          release_id                TEXT,
          capability_fingerprint    TEXT NOT NULL CHECK(capability_fingerprint LIKE 'sha256:%'),
          dependency_fingerprint    TEXT NOT NULL CHECK(dependency_fingerprint LIKE 'sha256:%'),
          follow_mode               TEXT NOT NULL CHECK(follow_mode='pinned'),
          state                     TEXT NOT NULL CHECK(state IN ('ready','needs_reconfirm','paused')),
          confirmed_by              TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
          confirmed_at              TEXT NOT NULL,
          row_version               INTEGER NOT NULL DEFAULT 1 CHECK(row_version >= 1),
          CHECK(
            (agent_source_kind='team' AND assignment_id IS NOT NULL AND release_id IS NOT NULL)
            OR (agent_source_kind='system' AND release_id IS NULL)
          )
        )
        """,
    ),
    (
        "index",
        "idx_scheduled_agent_binding_state",
        """
        CREATE INDEX idx_scheduled_agent_binding_state
        ON scheduled_job_agent_binding(state, agent_source_kind, agent_id)
        """,
    ),
    (
        "trigger",
        "trg_scheduled_agent_binding_insert",
        """
        CREATE TRIGGER trg_scheduled_agent_binding_insert
        BEFORE INSERT ON scheduled_job_agent_binding
        WHEN NOT EXISTS (
          SELECT 1
          FROM scheduled_jobs job
          JOIN workspaces workspace
            ON workspace.id=job.workspace_id AND workspace.status='active'
          JOIN users confirmer
            ON confirmer.id=NEW.confirmed_by AND confirmer.status='active'
          JOIN team_members member
            ON member.team_id=workspace.team_id
           AND member.user_id=NEW.confirmed_by AND member.status='active'
          WHERE job.id=NEW.job_id AND job.user_id=NEW.confirmed_by
        ) OR (
          NEW.agent_source_kind='team' AND NOT EXISTS (
            SELECT 1
            FROM scheduled_jobs job
            JOIN workspaces workspace ON workspace.id=job.workspace_id
            JOIN agent_assignment assignment
              ON assignment.id=NEW.assignment_id
             AND assignment.team_id=workspace.team_id
             AND assignment.user_id=job.user_id
             AND assignment.agent_id=NEW.agent_id
             AND assignment.agent_source_kind='team'
             AND assignment.personal_state='active'
             AND assignment.access_state='granted'
            JOIN team_subagent agent
              ON agent.team_id=workspace.team_id AND agent.id=NEW.agent_id
             AND agent.status='enabled' AND agent.lifecycle_state='active'
             AND agent.definition_mode='revisioned'
             AND agent.live_revision_id=NEW.revision_ref
             AND agent.live_release_id=NEW.release_id
            JOIN team_subagent_release release
              ON release.team_id=workspace.team_id
             AND release.subagent_id=NEW.agent_id
             AND release.id=NEW.release_id
             AND release.revision_id=NEW.revision_ref
             AND release.capability_fingerprint=NEW.capability_fingerprint
             AND release.dependency_fingerprint=NEW.dependency_fingerprint
            JOIN team_subagent_revision agent_revision
              ON agent_revision.team_id=release.team_id
             AND agent_revision.subagent_id=release.subagent_id
             AND agent_revision.id=release.revision_id
             AND agent_revision.capability_fingerprint=release.capability_fingerprint
             AND agent_revision.dependency_fingerprint=release.dependency_fingerprint
            JOIN agent_safety_report safety
              ON safety.team_id=release.team_id
             AND safety.agent_id=release.subagent_id
             AND safety.id=release.safety_report_id
             AND safety.subject_kind='revision'
             AND safety.subject_ref=agent_revision.id
             AND safety.content_hash=agent_revision.definition_hash
             AND safety.dependency_fingerprint=agent_revision.dependency_fingerprint
             AND safety.status NOT IN ('blocked','unknown')
            WHERE job.id=NEW.job_id
          )
        ) OR (
          NEW.agent_source_kind='system' AND NOT EXISTS (
            SELECT 1
            FROM scheduled_jobs job
            JOIN workspaces workspace ON workspace.id=job.workspace_id
            JOIN system_agent_effective_revision agent_revision
              ON agent_revision.team_id=workspace.team_id
             AND agent_revision.agent_id=NEW.agent_id
             AND agent_revision.id=NEW.revision_ref
            WHERE job.id=NEW.job_id
              AND (NEW.assignment_id IS NULL OR EXISTS (
                SELECT 1 FROM agent_assignment assignment
                WHERE assignment.id=NEW.assignment_id
                  AND assignment.team_id=workspace.team_id
                  AND assignment.user_id=job.user_id
                  AND assignment.agent_id=NEW.agent_id
                  AND assignment.agent_source_kind='system'
                  AND assignment.personal_state='active'
                  AND assignment.access_state='granted'
              ))
          )
        )
        BEGIN
          SELECT RAISE(ABORT, 'scheduled Agent binding mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_scheduled_agent_binding_update",
        """
        CREATE TRIGGER trg_scheduled_agent_binding_update
        BEFORE UPDATE ON scheduled_job_agent_binding
        WHEN NEW.job_id IS NOT OLD.job_id
          OR NEW.follow_mode<>'pinned'
          OR NEW.row_version<>OLD.row_version+1
          OR (
            NEW.state<>'ready' AND (
              NEW.assignment_id IS NOT OLD.assignment_id
              OR NEW.agent_id IS NOT OLD.agent_id
              OR NEW.agent_source_kind IS NOT OLD.agent_source_kind
              OR NEW.revision_ref IS NOT OLD.revision_ref
              OR NEW.release_id IS NOT OLD.release_id
              OR NEW.capability_fingerprint IS NOT OLD.capability_fingerprint
              OR NEW.dependency_fingerprint IS NOT OLD.dependency_fingerprint
              OR NEW.confirmed_by IS NOT OLD.confirmed_by
              OR NEW.confirmed_at IS NOT OLD.confirmed_at
            )
          )
          OR (NEW.state='ready' AND NOT EXISTS (
            SELECT 1
            FROM scheduled_jobs job
            JOIN workspaces workspace
              ON workspace.id=job.workspace_id AND workspace.status='active'
            JOIN users confirmer
              ON confirmer.id=NEW.confirmed_by AND confirmer.status='active'
            JOIN team_members member
              ON member.team_id=workspace.team_id
             AND member.user_id=NEW.confirmed_by AND member.status='active'
            WHERE job.id=NEW.job_id AND job.user_id=NEW.confirmed_by
          ))
          OR (
            NEW.state='ready' AND NEW.agent_source_kind='team' AND NOT EXISTS (
              SELECT 1
              FROM scheduled_jobs job
              JOIN workspaces workspace ON workspace.id=job.workspace_id
              JOIN agent_assignment assignment
                ON assignment.id=NEW.assignment_id
               AND assignment.team_id=workspace.team_id
               AND assignment.user_id=job.user_id
               AND assignment.agent_id=NEW.agent_id
               AND assignment.agent_source_kind='team'
               AND assignment.personal_state='active'
               AND assignment.access_state='granted'
              JOIN team_subagent agent
                ON agent.team_id=workspace.team_id AND agent.id=NEW.agent_id
               AND agent.status='enabled' AND agent.lifecycle_state='active'
               AND agent.definition_mode='revisioned'
               AND agent.live_revision_id=NEW.revision_ref
               AND agent.live_release_id=NEW.release_id
              JOIN team_subagent_release release
                ON release.team_id=workspace.team_id
               AND release.subagent_id=NEW.agent_id
               AND release.id=NEW.release_id
               AND release.revision_id=NEW.revision_ref
               AND release.capability_fingerprint=NEW.capability_fingerprint
               AND release.dependency_fingerprint=NEW.dependency_fingerprint
              JOIN team_subagent_revision agent_revision
                ON agent_revision.team_id=release.team_id
               AND agent_revision.subagent_id=release.subagent_id
               AND agent_revision.id=release.revision_id
               AND agent_revision.capability_fingerprint=release.capability_fingerprint
               AND agent_revision.dependency_fingerprint=release.dependency_fingerprint
              JOIN agent_safety_report safety
                ON safety.team_id=release.team_id
               AND safety.agent_id=release.subagent_id
               AND safety.id=release.safety_report_id
               AND safety.subject_kind='revision'
               AND safety.subject_ref=agent_revision.id
               AND safety.content_hash=agent_revision.definition_hash
               AND safety.dependency_fingerprint=agent_revision.dependency_fingerprint
               AND safety.status NOT IN ('blocked','unknown')
              WHERE job.id=NEW.job_id
            )
          )
          OR (
            NEW.state='ready' AND NEW.agent_source_kind='system' AND NOT EXISTS (
              SELECT 1
              FROM scheduled_jobs job
              JOIN workspaces workspace ON workspace.id=job.workspace_id
              JOIN system_agent_effective_revision agent_revision
                ON agent_revision.team_id=workspace.team_id
               AND agent_revision.agent_id=NEW.agent_id
               AND agent_revision.id=NEW.revision_ref
              WHERE job.id=NEW.job_id
                AND (NEW.assignment_id IS NULL OR EXISTS (
                  SELECT 1 FROM agent_assignment assignment
                  WHERE assignment.id=NEW.assignment_id
                    AND assignment.team_id=workspace.team_id
                    AND assignment.user_id=job.user_id
                    AND assignment.agent_id=NEW.agent_id
                    AND assignment.agent_source_kind='system'
                    AND assignment.personal_state='active'
                    AND assignment.access_state='granted'
                ))
            )
          )
        BEGIN
          SELECT RAISE(ABORT, 'scheduled Agent binding update mismatch');
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
        raise RuntimeError(f"0035 found incompatible {object_type} semantics: {name}")


def _validate_primary_capability_column(connection: Any) -> None:
    rows = connection.exec_driver_sql(
        "PRAGMA table_info(team_builtin_subagent_override)",
    ).fetchall()
    row = next((item for item in rows if str(item[1]) == "primary_capability"), None)
    expected = ("primary_capability", "TEXT", 0, None, 0)
    actual = (
        str(row[1]),
        str(row[2]).upper(),
        int(row[3]),
        row[4],
        int(row[5]),
    ) if row is not None else None
    table_sql = _object_sql(
        connection,
        object_type="table",
        name="team_builtin_subagent_override",
    )
    check = "check(primary_capabilityisnullorprimary_capabilitylike'cap_%')"
    if actual != expected or table_sql is None or check not in _canonical_sql(table_sql):
        raise RuntimeError(
            "0035 found incompatible column semantics: "
            "team_builtin_subagent_override.primary_capability",
        )


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    override_columns = {
        str(row[1])
        for row in connection.exec_driver_sql(
            "PRAGMA table_info(team_builtin_subagent_override)",
        ).fetchall()
    }
    if "primary_capability" not in override_columns:
        connection.exec_driver_sql(
            """
            ALTER TABLE team_builtin_subagent_override
            ADD COLUMN primary_capability TEXT
              CHECK(primary_capability IS NULL OR primary_capability LIKE 'cap_%')
            """,
        )
    _validate_primary_capability_column(connection)
    for object_type, name, ddl in _OBJECTS:
        _ensure_object(
            connection,
            object_type=object_type,
            name=name,
            ddl=ddl,
        )


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
