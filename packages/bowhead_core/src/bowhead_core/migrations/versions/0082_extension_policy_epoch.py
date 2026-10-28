"""Require current access epoch in extension request evidence.

Revision ID: 0082_extension_policy_epoch
Revises: 0081_skill_read_extensions
Create Date: 2026-09-15 04:12:33.217686

"""
revision = '0082_extension_policy_epoch'
down_revision = '0081_skill_read_extensions'
branch_labels = None
depends_on = None

from importlib import import_module

from alembic import context

PREVIOUS = import_module('bowhead_core.migrations.versions.0081_skill_read_extensions')
TRIGGER = 'trg_reference_extension_epoch_check'
SCOPE_TRIGGER = 'trg_reference_extension_check_scope'
PREVIOUS_SCOPE = next(sql for sql in PREVIOUS.DDL if f'CREATE TRIGGER {SCOPE_TRIGGER} ' in sql)
SCOPE_DDL = PREVIOUS_SCOPE.replace("json_extract(NEW.policy_check_json,'$.schema_version')=1", "json_extract(NEW.policy_check_json,'$.schema_version')=2")
DDL = """CREATE TRIGGER trg_reference_extension_epoch_check BEFORE INSERT ON chat_reference_extension_checks
WHEN NOT EXISTS(SELECT 1 FROM turn_agent_execution e
  WHERE e.id=NEW.execution_id AND e.agent_source_kind='team'
    AND json_type(NEW.policy_check_json,'$.current_access_epoch')='integer'
    AND json_extract(NEW.policy_check_json,'$.current_access_epoch')=COALESCE((SELECT epoch FROM access_policy_state WHERE team_id=e.team_id),0))
BEGIN SELECT RAISE(ABORT,'reference extension current epoch required'); END"""


def upgrade() -> None:
    if context.config.attributes['bowhead_target'] != 'core':
        return
    db = context.get_bind()
    row = db.exec_driver_sql('SELECT type,sql FROM sqlite_master WHERE name=?', (SCOPE_TRIGGER,)).fetchone()
    if (db.exec_driver_sql('SELECT 1 FROM sqlite_master WHERE name=?', (TRIGGER,)).fetchone()
        or row is None or row[0]!='trigger' or PREVIOUS.BASE._normalized(row[1])!=PREVIOUS.BASE._normalized(PREVIOUS_SCOPE)):
        raise RuntimeError('reference extension epoch schema drift')
    db.exec_driver_sql('SAVEPOINT extension_epoch_upgrade')
    try:
        db.exec_driver_sql(DDL)
        db.exec_driver_sql(f'DROP TRIGGER {SCOPE_TRIGGER}')
        db.exec_driver_sql(SCOPE_DDL)
    except BaseException:
        db.exec_driver_sql('ROLLBACK TO SAVEPOINT extension_epoch_upgrade')
        db.exec_driver_sql('RELEASE SAVEPOINT extension_epoch_upgrade')
        raise
    db.exec_driver_sql('RELEASE SAVEPOINT extension_epoch_upgrade')


def downgrade() -> None:
    raise NotImplementedError('Forward-only; retain compatible workers or restore matching backup.')
