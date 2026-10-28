"""Rebuild the scheduled Agent binding guards without an unsatisfiable equality.

``0035`` guarded every scheduled Team Agent pin with
``safety.content_hash = agent_revision.definition_hash``.  Those two columns hold
digests over different inputs -- the safety report hashes ``{definition, listing}``
while the revision hashes the definition alone -- so no row any writer produces can
satisfy the trigger.  The pin was therefore rejected for every Team Agent, in the
same way the Python-side admission joins were.

The intent (this safety report describes exactly this revision's content) is kept:
``safety.subject_ref = agent_revision.id`` still binds the report to the revision,
and admission re-derives the content hash with the writer's own formula in
``_validate_team_snapshot``.

Expand-only: the triggers are dropped and recreated, no data is touched.

Revision ID: 0052_scheduled_binding_safety_join
Revises: 0051_artifact_row_count
Create Date: 2026-09-06
"""

from __future__ import annotations

from typing import Any

from alembic import context

revision = "0052_scheduled_binding_safety_join"
down_revision = "0051_artifact_row_count"
branch_labels = None
depends_on = None


_TRIGGERS: tuple[tuple[str, str], ...] = (
    (
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


def _trigger_sql(connection: Any, name: str) -> str | None:
    row = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?",
        (name,),
    ).fetchone()
    return str(row[0]) if row is not None and row[0] is not None else None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    for name, ddl in _TRIGGERS:
        current = _trigger_sql(connection, name)
        if current is not None and _canonical_sql(current) == _canonical_sql(ddl):
            continue
        connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {name}")
        connection.exec_driver_sql(ddl)
        rebuilt = _trigger_sql(connection, name)
        if rebuilt is None or _canonical_sql(rebuilt) != _canonical_sql(ddl):
            raise RuntimeError(f"0052 could not install trigger: {name}")


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
