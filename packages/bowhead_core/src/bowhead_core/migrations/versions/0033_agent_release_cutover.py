"""Agent M2 writer cutover marker and revisioned aggregate guards.

This expand-only revision adds the durable cutover marker but deliberately
leaves it NULL.  The offline Agent release baseline job is the only operation
allowed to set the marker and raise the persisted application version floor,
after it has reconciled every legacy Agent under a writer-stop window.

Revision ID: 0033_agent_release_cutover
Revises: 0032_agent_release_events
Create Date: 2026-09-03
"""

from __future__ import annotations

from typing import Any

from alembic import context

revision = "0033_agent_release_cutover"
down_revision = "0032_agent_release_events"
branch_labels = None
depends_on = None


_MARKER_COLUMN = "release_semantics_activated_at"

_LIFECYCLE_INVARIANT = """
(
  NEW.lifecycle_state NOT IN ('active', 'quarantined', 'archived')
  OR (
    NEW.lifecycle_state = 'quarantined'
    AND (
      NEW.status <> 'disabled'
      OR NEW.archived_at IS NOT NULL
      OR NEW.archived_by IS NOT NULL
    )
  )
  OR (
    NEW.lifecycle_state = 'archived'
    AND (
      NEW.status <> 'disabled'
      OR NEW.archived_at IS NULL
      OR NEW.archived_by IS NULL
    )
  )
  OR (
    NEW.lifecycle_state = 'active'
    AND (NEW.archived_at IS NOT NULL OR NEW.archived_by IS NOT NULL)
  )
)
"""

_TRIGGERS: tuple[tuple[str, str], ...] = (
    (
        "team_subagent_invariant_guard_update",
        f"""
        CREATE TRIGGER team_subagent_invariant_guard_update
        BEFORE UPDATE ON team_subagent
        WHEN (
          (
            EXISTS (
              SELECT 1 FROM team_subagent_schema_meta
              WHERE singleton=1 AND audience_semantics_activated_at IS NOT NULL
            )
            AND NEW.audience_mode NOT IN ('private','custom','teamwide')
          )
          OR (
            EXISTS (
              SELECT 1 FROM team_subagent_schema_meta
              WHERE singleton=1 AND lifecycle_semantics_activated_at IS NOT NULL
            )
            AND ({_LIFECYCLE_INVARIANT} OR NEW.row_version < OLD.row_version)
          )
        )
        BEGIN
          SELECT RAISE(ABORT, 'team_subagent_invariant_violation');
        END
        """,
    ),
    (
        "team_subagent_invariant_guard_insert",
        f"""
        CREATE TRIGGER team_subagent_invariant_guard_insert
        BEFORE INSERT ON team_subagent
        WHEN (
          (
            EXISTS (
              SELECT 1 FROM team_subagent_schema_meta
              WHERE singleton=1 AND owner_semantics_activated_at IS NOT NULL
            )
            AND NEW.owner_user_id IS NULL
          )
          OR (
            EXISTS (
              SELECT 1 FROM team_subagent_schema_meta
              WHERE singleton=1 AND audience_semantics_activated_at IS NOT NULL
            )
            AND NEW.audience_mode NOT IN ('private','custom','teamwide')
          )
          OR (
            EXISTS (
              SELECT 1 FROM team_subagent_schema_meta
              WHERE singleton=1 AND lifecycle_semantics_activated_at IS NOT NULL
            )
            AND {_LIFECYCLE_INVARIANT}
          )
        )
        BEGIN
          SELECT RAISE(ABORT, 'team_subagent_invariant_violation');
        END
        """,
    ),
    (
        "team_subagent_revisioned_writer_guard_update",
        """
        CREATE TRIGGER team_subagent_revisioned_writer_guard_update
        BEFORE UPDATE ON team_subagent
        WHEN EXISTS (
          SELECT 1 FROM team_subagent_schema_meta
          WHERE singleton=1 AND release_semantics_activated_at IS NOT NULL
        )
        AND OLD.definition_mode='revisioned'
        AND (
          NEW.definition_mode IS NOT OLD.definition_mode
          OR NEW.display_name IS NOT OLD.display_name
          OR NEW.description IS NOT OLD.description
          OR NEW.system_prompt IS NOT OLD.system_prompt
          OR NEW.tool_refs_json IS NOT OLD.tool_refs_json
          OR NEW.model IS NOT OLD.model
          OR NEW.max_iterations IS NOT OLD.max_iterations
          OR NEW.skills_json IS NOT OLD.skills_json
          OR NEW.output_schema_json IS NOT OLD.output_schema_json
          OR NEW.audience_mode IS NOT OLD.audience_mode
          OR NEW.visibility IS NOT OLD.visibility
          OR NEW.live_revision_id IS NOT OLD.live_revision_id
          OR NEW.live_release_id IS NOT OLD.live_release_id
          OR NEW.release_sequence IS NOT OLD.release_sequence
          OR NEW.last_release_event_id IS NOT OLD.last_release_event_id
        )
        AND NOT (
          OLD.lifecycle_state='archived'
          AND NEW.lifecycle_state='active'
          AND NEW.status='enabled'
          AND NEW.archived_at IS NULL
          AND NEW.archived_by IS NULL
          AND NEW.definition_mode IS OLD.definition_mode
          AND NEW.display_name IS OLD.display_name
          AND NEW.description IS OLD.description
          AND NEW.system_prompt IS OLD.system_prompt
          AND NEW.tool_refs_json IS OLD.tool_refs_json
          AND NEW.model IS OLD.model
          AND NEW.max_iterations IS OLD.max_iterations
          AND NEW.skills_json IS OLD.skills_json
          AND NEW.output_schema_json IS OLD.output_schema_json
          AND NEW.live_revision_id IS OLD.live_revision_id
          AND NEW.live_release_id IS OLD.live_release_id
          AND NEW.release_sequence IS OLD.release_sequence
          AND NEW.last_release_event_id IS OLD.last_release_event_id
        )
        AND NOT (
          NEW.definition_mode='revisioned'
          AND NEW.live_revision_id IS NOT OLD.live_revision_id
          AND NEW.live_release_id IS NOT OLD.live_release_id
          AND NEW.release_sequence=OLD.release_sequence+1
          AND NEW.last_release_event_id IS NOT OLD.last_release_event_id
          AND EXISTS (
            SELECT 1 FROM team_subagent_release release
            WHERE release.team_id=OLD.team_id
              AND release.subagent_id=OLD.id
              AND release.id=NEW.live_release_id
              AND release.revision_id=NEW.live_revision_id
              AND release.release_sequence=NEW.release_sequence
          )
          AND EXISTS (
            SELECT 1 FROM team_subagent_release_event event
            WHERE event.team_id=OLD.team_id
              AND event.subagent_id=OLD.id
              AND event.id=NEW.last_release_event_id
              AND event.release_id=NEW.live_release_id
              AND event.event_type IN ('release.activated','audience.contracted')
          )
        )
        BEGIN
          SELECT RAISE(ABORT, 'team_subagent_revision_writer_required');
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


def _ensure_marker_column(connection: Any) -> None:
    columns = {str(row[1]): row for row in connection.exec_driver_sql("PRAGMA table_info(team_subagent_schema_meta)").fetchall()}
    if _MARKER_COLUMN not in columns:
        connection.exec_driver_sql(
            f"ALTER TABLE team_subagent_schema_meta ADD COLUMN {_MARKER_COLUMN} TEXT",
        )
        columns = {str(row[1]): row for row in connection.exec_driver_sql("PRAGMA table_info(team_subagent_schema_meta)").fetchall()}
    column = columns.get(_MARKER_COLUMN)
    if column is None or (str(column[2]).upper(), int(column[3]), column[4], int(column[5])) != ("TEXT", 0, None, 0):
        raise RuntimeError("0033 found incompatible release cutover marker column")


def _replace_trigger(connection: Any, *, name: str, ddl: str) -> None:
    existing = _object_sql(connection, object_type="trigger", name=name)
    if existing is not None and _canonical_sql(existing) != _canonical_sql(ddl):
        connection.exec_driver_sql(f"DROP TRIGGER {name}")
        existing = None
    if existing is None:
        connection.exec_driver_sql(ddl)
    persisted = _object_sql(connection, object_type="trigger", name=name)
    if persisted is None or _canonical_sql(persisted) != _canonical_sql(ddl):
        raise RuntimeError(f"0033 found incompatible trigger semantics: {name}")


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    _ensure_marker_column(connection)
    for name, ddl in _TRIGGERS:
        _replace_trigger(connection, name=name, ddl=ddl)


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
