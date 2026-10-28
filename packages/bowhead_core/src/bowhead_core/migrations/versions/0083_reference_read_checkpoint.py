"""Retain exact method reads before the first BI workspace gate

Revision ID: 0083_reference_read_checkpoint
Revises: 0082_extension_policy_epoch
Create Date: 2026-09-15 05:42:55.654947

"""
from importlib import import_module

from alembic import context

revision = "0083_reference_read_checkpoint"
down_revision = "0082_extension_policy_epoch"
branch_labels = None
depends_on = None

PREVIOUS = import_module("bowhead_core.migrations.versions.0081_skill_read_extensions")
TABLE = "chat_reference_bi_continuations"
SCOPE = "trg_reference_bi_continuation_scope"
IMMUTABLE = "trg_reference_read_checkpoint_immutable"
OLD_SCOPE = PREVIOUS.REPLACEMENTS[SCOPE][1]
SCOPE_DDL = OLD_SCOPE.replace("i.status='completed')=1", "i.status='completed')=NEW.captured_iterations")
SCOPE_DDL = SCOPE_DDL.replace("WHEN NEW.state<>'captured'", "WHEN NOT ((NEW.checkpoint_version=1 AND NEW.captured_iterations=1) OR (NEW.checkpoint_version=2 AND NEW.captured_iterations BETWEEN 2 AND 33)) OR NEW.state<>'captured'")
IMMUTABLE_DDL = """CREATE TRIGGER trg_reference_read_checkpoint_immutable BEFORE UPDATE ON chat_reference_bi_continuations
WHEN NEW.checkpoint_version IS NOT OLD.checkpoint_version OR NEW.captured_iterations IS NOT OLD.captured_iterations
BEGIN SELECT RAISE(ABORT,'reference read checkpoint identity is immutable'); END"""


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    current = db.exec_driver_sql("SELECT type,sql FROM sqlite_master WHERE name=?", (SCOPE,)).fetchone()
    columns = {row[1] for row in db.exec_driver_sql(f"PRAGMA table_info({TABLE})").fetchall()}
    if (current is None or current[0] != "trigger" or PREVIOUS.BASE._normalized(current[1]) != PREVIOUS.BASE._normalized(OLD_SCOPE)
        or {"checkpoint_version", "captured_iterations"} & columns
        or db.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name=?", (IMMUTABLE,)).fetchone()):
        raise RuntimeError("reference read checkpoint schema drift")
    db.exec_driver_sql("SAVEPOINT reference_read_checkpoint_upgrade")
    try:
        db.exec_driver_sql(f"ALTER TABLE {TABLE} ADD COLUMN checkpoint_version INTEGER NOT NULL DEFAULT 1 CHECK(typeof(checkpoint_version)='integer' AND checkpoint_version IN (1,2))")
        db.exec_driver_sql(f"ALTER TABLE {TABLE} ADD COLUMN captured_iterations INTEGER NOT NULL DEFAULT 1 CHECK(typeof(captured_iterations)='integer' AND captured_iterations BETWEEN 1 AND 33)")
        db.exec_driver_sql(f"DROP TRIGGER {SCOPE}")
        db.exec_driver_sql(SCOPE_DDL)
        db.exec_driver_sql(IMMUTABLE_DDL)
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT reference_read_checkpoint_upgrade")
        db.exec_driver_sql("RELEASE SAVEPOINT reference_read_checkpoint_upgrade")
        raise
    db.exec_driver_sql("RELEASE SAVEPOINT reference_read_checkpoint_upgrade")


def downgrade() -> None:
    raise NotImplementedError("Forward-only; retain compatible workers or restore matching backup.")
