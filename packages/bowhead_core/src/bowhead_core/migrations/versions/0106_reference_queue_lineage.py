"""Keep accepted queue origin separate from the current dispatch parent.

Revision ID: 0106_reference_queue_lineage
Revises: 0105_reference_attachment_checkpoint
"""
from importlib import import_module

from alembic import context

revision = "0106_reference_queue_lineage"
down_revision = "0105_reference_attachment_checkpoint"
branch_labels = None
depends_on = None

FOLLOW_UP = import_module("bowhead_core.migrations.versions.0092_reference_follow_up_runtime")
PREVIOUS = import_module("bowhead_core.migrations.versions.0105_reference_attachment_checkpoint")
OLD_ORIGIN = FOLLOW_UP.INITIAL_OR_EXACT_FOLLOW_UP
NEW_ORIGIN = OLD_ORIGIN.replace(
    "JOIN turns qc ON qc.id=q.materialized_turn_id",
    "JOIN turns qc ON qc.id=q.materialized_turn_id\n      JOIN turns qh ON qh.id=d.parent_turn_id AND qh.session_id=q.session_id",
).replace(
    "AND q.accepted_parent_turn_id=d.parent_turn_id",
    """AND EXISTS (
          WITH RECURSIVE lineage(turn_id) AS (
            SELECT qh.id
            UNION
            SELECT prior.parent_turn_id FROM chat_turn_spawn_dispatch prior
            JOIN lineage ON lineage.turn_id=prior.turn_id
            JOIN turns ancestor ON ancestor.id=prior.parent_turn_id AND ancestor.session_id=q.session_id
            WHERE prior.session_id=q.session_id AND prior.dispatch_kind='follow_up'
              AND prior.turn_id<>q.accepted_parent_turn_id
          )
          SELECT 1 FROM lineage WHERE turn_id=q.accepted_parent_turn_id
            AND NOT EXISTS (SELECT 1 FROM lineage WHERE turn_id=q.materialized_turn_id)
        )""",
)
CURRENT = {
    name: sql for name, sql in {
        **FOLLOW_UP.DDL,
        **PREVIOUS.CURRENT,
        PREVIOUS.PREVIOUS.CAPTURE: PREVIOUS.SCOPE_DDL,
    }.items() if OLD_ORIGIN in sql
}
if len(CURRENT) != 8 or any(sql.count(OLD_ORIGIN) != 1 for sql in CURRENT.values()):
    raise RuntimeError("reference queue lineage source drift")
DDL = {name: sql.replace(OLD_ORIGIN, NEW_ORIGIN) for name, sql in CURRENT.items()}


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    for name, sql in CURRENT.items():
        row = db.exec_driver_sql("SELECT type,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if row is None or row[0] != "trigger" or FOLLOW_UP._normalized(row[1]) != FOLLOW_UP._normalized(sql):
            raise RuntimeError("reference queue lineage schema drift")
    db.exec_driver_sql("SAVEPOINT reference_queue_lineage")
    try:
        for name, sql in DDL.items():
            db.exec_driver_sql(f"DROP TRIGGER {name}")
            db.exec_driver_sql(sql)
        db.exec_driver_sql("RELEASE SAVEPOINT reference_queue_lineage")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT reference_queue_lineage")
        db.exec_driver_sql("RELEASE SAVEPOINT reference_queue_lineage")
        raise


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore the matching database snapshot to roll back.")
