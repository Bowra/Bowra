"""Preserve precise system Skill scopes.

Revision ID: 0079_system_skill_snapshot
Revises: 0078_reference_bi_continuation
Create Date: 2026-09-15 01:09:45.338689

"""
from alembic import context

revision = '0079_system_skill_snapshot'
down_revision = '0078_reference_bi_continuation'
branch_labels = None
depends_on = None

TRIGGER = 'trg_system_precise_skill_snapshot'
DDL = """CREATE TRIGGER trg_system_precise_skill_snapshot BEFORE INSERT ON system_agent_effective_revision
WHEN json_extract(NEW.definition_json,'$.schema_version')=2 AND NOT COALESCE(
  json_type(NEW.definition_json,'$.skill_runtime_scope') IN ('object','null')
  AND json_type(NEW.definition_json,'$.skill_grants') IN ('array','null')
  AND (json_type(NEW.definition_json,'$.skill_runtime_scope')='object' OR json_type(NEW.definition_json,'$.skill_grants')='array')
  AND (json_type(NEW.definition_json,'$.skill_runtime_scope')='null' OR (
    json_type(NEW.definition_json,'$.skill_runtime_scope.schema_version')='integer'
    AND json_extract(NEW.definition_json,'$.skill_runtime_scope.schema_version')=1
    AND json_type(NEW.definition_json,'$.skill_runtime_scope.read_names')='array'
    AND json_type(NEW.definition_json,'$.skill_runtime_scope.invoke_names')='array'
  )),0)
BEGIN SELECT RAISE(ABORT,'system precise Skill snapshot is malformed'); END"""


def upgrade() -> None:
    if context.config.attributes['bowhead_target'] != 'core':
        return
    db = context.get_bind()
    if db.exec_driver_sql('SELECT 1 FROM sqlite_master WHERE name=?', (TRIGGER,)).fetchone():
        raise RuntimeError('system Skill snapshot schema drift')
    db.exec_driver_sql('SAVEPOINT system_skill_snapshot_upgrade')
    try:
        db.exec_driver_sql(DDL)
    except BaseException:
        db.exec_driver_sql('ROLLBACK TO SAVEPOINT system_skill_snapshot_upgrade')
        db.exec_driver_sql('RELEASE SAVEPOINT system_skill_snapshot_upgrade')
        raise
    db.exec_driver_sql('RELEASE SAVEPOINT system_skill_snapshot_upgrade')


def downgrade() -> None:
    raise NotImplementedError('Forward-only; restore the matching snapshot to roll back.')
