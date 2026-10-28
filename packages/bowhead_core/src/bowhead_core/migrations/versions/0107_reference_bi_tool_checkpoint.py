"""Preserve the exact BI discovery tool in the existing workspace checkpoint.

Revision ID: 0107_reference_bi_tool_checkpoint
Revises: 0106_reference_queue_lineage
"""
from __future__ import annotations

from importlib import import_module

from alembic import context

revision = "0107_reference_bi_tool_checkpoint"
down_revision = "0106_reference_queue_lineage"
branch_labels = None
depends_on = None

BASE = import_module("bowhead_core.migrations.versions.0078_reference_bi_continuation")
PREVIOUS = import_module("bowhead_core.migrations.versions.0105_reference_attachment_checkpoint")
LINEAGE = import_module("bowhead_core.migrations.versions.0106_reference_queue_lineage")
TABLE = BASE.TABLE
SHADOW = TABLE + "_0107"
OLD_TABLE = PREVIOUS.TABLE_DDL
TABLE_DDL = OLD_TABLE.replace("checkpoint_version IN (1,2,3)", "checkpoint_version IN (1,2,3,4)")
CAPTURE = PREVIOUS.PREVIOUS.CAPTURE
CURRENT = {
    name: LINEAGE.DDL.get(name, sql)
    for name, sql in {**PREVIOUS.CURRENT, CAPTURE: PREVIOUS.SCOPE_DDL}.items()
}
SCOPE_DDL = CURRENT[CAPTURE].replace("NEW.checkpoint_version=3 AND", "NEW.checkpoint_version IN (3,4) AND")
if SCOPE_DDL == CURRENT[CAPTURE]:
    raise RuntimeError("reference BI tool checkpoint source drift")


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    current = db.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
    related = dict(db.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND sql LIKE ?", (f"%{TABLE}%",)).fetchall())
    if (current is None or BASE._normalized(current[0]) != BASE._normalized(OLD_TABLE.replace(f"CREATE TABLE {TABLE}", f'CREATE TABLE "{TABLE}"'))
        or set(related) != set(CURRENT)
        or any(BASE._normalized(related[name]) != BASE._normalized(sql) for name, sql in CURRENT.items())
        or db.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name=? OR (type IN ('index','view') AND sql LIKE ?)", (SHADOW, f"%{TABLE}%")).fetchone()):
        raise RuntimeError("reference BI tool checkpoint schema drift")
    db.exec_driver_sql("SAVEPOINT reference_bi_tool_checkpoint")
    try:
        db.exec_driver_sql(TABLE_DDL.replace(f"CREATE TABLE {TABLE}", f"CREATE TABLE {SHADOW}"))
        db.exec_driver_sql(f"INSERT INTO {SHADOW} SELECT * FROM {TABLE}")
        if (db.exec_driver_sql(f"SELECT COUNT(*) FROM {SHADOW}").scalar() != db.exec_driver_sql(f"SELECT COUNT(*) FROM {TABLE}").scalar()
            or db.exec_driver_sql(f"SELECT 1 FROM (SELECT * FROM {SHADOW} EXCEPT SELECT * FROM {TABLE}) LIMIT 1").fetchone()
            or db.exec_driver_sql(f"SELECT 1 FROM (SELECT * FROM {TABLE} EXCEPT SELECT * FROM {SHADOW}) LIMIT 1").fetchone()):
            raise RuntimeError("reference BI tool checkpoint copy mismatch")
        # Suspend incoming triggers too, so rename never resolves a missing table.
        for name in CURRENT:
            db.exec_driver_sql(f"DROP TRIGGER {name}")
        db.exec_driver_sql(f"DROP TABLE {TABLE}")
        db.exec_driver_sql(f"ALTER TABLE {SHADOW} RENAME TO {TABLE}")
        for name, sql in CURRENT.items():
            db.exec_driver_sql(SCOPE_DDL if name == CAPTURE else sql)
        db.exec_driver_sql("RELEASE SAVEPOINT reference_bi_tool_checkpoint")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT reference_bi_tool_checkpoint")
        db.exec_driver_sql("RELEASE SAVEPOINT reference_bi_tool_checkpoint")
        raise


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore the matching database snapshot to roll back.")
