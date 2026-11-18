"""Keep the Space edit counter and grant usage time out of the core mutation fence.

Revision ID: 0230_unfence_routine_metadata
Revises: 0229_turn_selected_skills

docs/sprints/fence-narrowing-2026-11-18/tech-design.md. A Space metadata save
bumps ``workspaces.revision`` and a Google Workspace token refresh stamps
``builtin_tool_user_grant.last_used_at``. Neither column can revoke access,
reassign scope, delete, or race a retention pin or purge claim, but each change
took the exclusive core lease and failed at once while a running Turn held its
shared side (116, 1.7.49: two Space saves answered 500). Status, team, identity
and credential columns keep their fences. Like ``0222``, this revision owns the
two replaced trigger definitions; fresh installs, whose ``0221`` already
excludes the columns, end with byte-identical triggers.
"""

from alembic import context

from bowhead_core.sqlite_domain_runtime import _AUTHORITY

revision = "0230_unfence_routine_metadata"
down_revision = "0229_turn_selected_skills"
branch_labels = None
depends_on = None

# The full set of unfenced update columns for each replaced table, frozen at this revision.
UNFENCED = {
    "workspaces": ("name", "name_key", "description", "last_synced_at", "updated_at", "revision"),
    "builtin_tool_user_grant": ("last_used_at",),
}


def update_trigger(table: str) -> str:
    columns = [column for column in _AUTHORITY["core"][table] if column not in UNFENCED[table]]
    names = ",".join(f'"{column}"' for column in columns)
    changed = " OR ".join(f'OLD."{column}" IS NOT NEW."{column}"' for column in columns)
    return (
        f'CREATE TRIGGER "trg_domain_mutation_{table}_update" BEFORE UPDATE OF {names} ON "{table}" WHEN {changed} '
        f"BEGIN SELECT bowhead_domain_mutation_v2('{table}','update'); END"
    )


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT unfence_routine_metadata")
    try:
        for table in UNFENCED:
            db.exec_driver_sql(f'DROP TRIGGER "trg_domain_mutation_{table}_update"')
            db.exec_driver_sql(update_trigger(table))
        db.exec_driver_sql("RELEASE unfence_routine_metadata")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO unfence_routine_metadata")
        db.exec_driver_sql("RELEASE unfence_routine_metadata")
        raise


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
