"""Retain original attachment data in the existing BI checkpoint.

Revision ID: 0105_reference_attachment_checkpoint
Revises: 0104_reference_prompt_continuation
"""
from __future__ import annotations

from importlib import import_module

from alembic import context

revision = "0105_reference_attachment_checkpoint"
down_revision = "0104_reference_prompt_continuation"
branch_labels = None
depends_on = None

BASE = import_module("bowhead_core.migrations.versions.0078_reference_bi_continuation")
READ = import_module("bowhead_core.migrations.versions.0083_reference_read_checkpoint")
PREVIOUS = import_module("bowhead_core.migrations.versions.0104_reference_prompt_continuation")
TABLE = BASE.TABLE
SHADOW = TABLE + "_0105"
OLD_TABLE = BASE.DDL[0].replace(
    "expires_at TEXT NOT NULL,",
    "expires_at TEXT NOT NULL, checkpoint_version INTEGER NOT NULL DEFAULT 1 CHECK(typeof(checkpoint_version)='integer' AND checkpoint_version IN (1,2)), captured_iterations INTEGER NOT NULL DEFAULT 1 CHECK(typeof(captured_iterations)='integer' AND captured_iterations BETWEEN 1 AND 33),",
)
TABLE_DDL = OLD_TABLE.replace("checkpoint_version IN (1,2)", "checkpoint_version IN (1,2,3)")
CURRENT = {
    **{sql.split()[2]: sql for sql in BASE.DDL[2:]},
    "trg_reference_primary_completion": BASE.REPLACEMENTS["trg_reference_primary_completion"][1],
    READ.IMMUTABLE: READ.IMMUTABLE_DDL,
    "trg_chat_reference_evidence_scope": PREVIOUS.FOLLOW_UP.DDL["trg_chat_reference_evidence_scope"],
    **PREVIOUS.DDL,
}
SCOPE_DDL = CURRENT[PREVIOUS.CAPTURE].replace(
    "(NEW.checkpoint_version=2 AND NEW.captured_iterations BETWEEN 2 AND 33)",
    "(NEW.checkpoint_version=2 AND NEW.captured_iterations BETWEEN 2 AND 33) OR (NEW.checkpoint_version=3 AND NEW.captured_iterations BETWEEN 1 AND 33)",
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    current = db.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
    related = dict(db.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND sql LIKE ?", (f"%{TABLE}%",)).fetchall())
    if (current is None or BASE._normalized(current[0]) != BASE._normalized(OLD_TABLE)
        or set(related) != set(CURRENT)
        or any(BASE._normalized(related[name]) != BASE._normalized(sql) for name, sql in CURRENT.items())
        or db.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name=? OR (type IN ('index','view') AND sql LIKE ?)", (SHADOW, f"%{TABLE}%")).fetchone()):
        raise RuntimeError("reference attachment checkpoint schema drift")
    db.exec_driver_sql("SAVEPOINT reference_attachment_checkpoint")
    try:
        db.exec_driver_sql(TABLE_DDL.replace(f"CREATE TABLE {TABLE}", f"CREATE TABLE {SHADOW}"))
        db.exec_driver_sql(f"INSERT INTO {SHADOW} SELECT * FROM {TABLE}")
        if (db.exec_driver_sql(f"SELECT COUNT(*) FROM {SHADOW}").scalar() != db.exec_driver_sql(f"SELECT COUNT(*) FROM {TABLE}").scalar()
            or db.exec_driver_sql(f"SELECT 1 FROM (SELECT * FROM {SHADOW} EXCEPT SELECT * FROM {TABLE}) LIMIT 1").fetchone()
            or db.exec_driver_sql(f"SELECT 1 FROM (SELECT * FROM {TABLE} EXCEPT SELECT * FROM {SHADOW}) LIMIT 1").fetchone()):
            raise RuntimeError("reference attachment checkpoint copy mismatch")
        # Suspend incoming triggers too, so rename never resolves a missing table.
        for name in CURRENT:
            db.exec_driver_sql(f"DROP TRIGGER {name}")
        db.exec_driver_sql(f"DROP TABLE {TABLE}")
        db.exec_driver_sql(f"ALTER TABLE {SHADOW} RENAME TO {TABLE}")
        for name, sql in CURRENT.items():
            db.exec_driver_sql(SCOPE_DDL if name == PREVIOUS.CAPTURE else sql)
        db.exec_driver_sql("RELEASE SAVEPOINT reference_attachment_checkpoint")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT reference_attachment_checkpoint")
        db.exec_driver_sql("RELEASE SAVEPOINT reference_attachment_checkpoint")
        raise


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore the matching database snapshot to roll back.")
