"""Retain automatic primary composition without a user Agent node

Revision ID: 0086_reference_auto_composition
Revises: 0085_merge_slack_reference
Create Date: 2026-09-15 10:34:30.973091

"""
from __future__ import annotations

from importlib import import_module

from alembic import context

revision = '0086_reference_auto_composition'
down_revision = '0085_merge_slack_reference'
branch_labels = None
depends_on = None

BASE = import_module('bowhead_core.migrations.versions.0072_skill_baseline_composition')
INVOKE = import_module('bowhead_core.migrations.versions.0075_reference_skill_invocation')
SINGLE = import_module('bowhead_core.migrations.versions.0076_reference_primary_baseline')
EXTENSION = import_module('bowhead_core.migrations.versions.0081_skill_read_extensions')
PREVIOUS = {BASE.TRIGGER: BASE.DDL, INVOKE.TRIGGER: INVOKE.DDL, SINGLE.TRIGGER: SINGLE.DDL, EXTENSION.TRIGGER: EXTENSION.COMPOSITION_DDL}
REPLACEMENTS = {name: sql.replace("json_extract(c.descriptor_json,'$.node_id')=json_extract(NEW.composition_json,'$.node_id')", "json_extract(c.descriptor_json,'$.node_id') IS json_extract(NEW.composition_json,'$.node_id')") for name, sql in PREVIOUS.items()}
TRIGGER = 'trg_reference_automatic_composition'
CONTENT_TRIGGER = 'trg_reference_automatic_material'
DDL = (
    f"""CREATE TRIGGER {TRIGGER} BEFORE INSERT ON chat_reference_resolutions
    WHEN NOT EXISTS (
      SELECT 1 FROM turn_agent_binding b JOIN turn_agent_execution e ON e.id=b.primary_execution_id AND e.turn_id=b.turn_id
      WHERE b.turn_id=NEW.turn_id AND e.id=NEW.execution_id AND (
        (b.binding_source='mention' AND e.dispatch_source='explicit'
          AND json_extract(NEW.composition_json,'$.receiver_source') IS NULL
          AND json_type(NEW.composition_json,'$.node_id')='text')
        OR (b.binding_source='lead' AND e.dispatch_source='lead'
          AND json_extract(NEW.composition_json,'$.receiver_source')='lead'
          AND json_type(NEW.composition_json,'$.node_id') IS NULL
          AND json_extract(NEW.composition_json,'$.runtime_policy') IN ('initial-primary-skill-read.v2','initial-primary-skill-baseline.v1','initial-primary-skill-extension.v1')
          AND json_extract(NEW.composition_json,'$.resource.kind')='agent'
          AND json_extract(NEW.composition_json,'$.resource.id')=b.requested_agent_id
          AND json_extract(NEW.composition_json,'$.resource.source_kind')=b.agent_source_kind
          AND json_extract(NEW.composition_json,'$.resource.version')=b.revision_ref
          AND json_extract(NEW.composition_json,'$.resource.release_id') IS b.release_id
          AND json_extract(NEW.composition_json,'$.resource.assignment_id') IS b.assignment_id
          AND json_extract(NEW.composition_json,'$.content.receiver_source')='lead'
          AND json_type(NEW.composition_json,'$.content.node_id') IS NULL)))
    BEGIN SELECT RAISE(ABORT,'reference composition receiver origin mismatch'); END""",
    f"""CREATE TRIGGER {CONTENT_TRIGGER} BEFORE INSERT ON chat_reference_content_snapshots
    WHEN json_extract(NEW.descriptor_json,'$.receiver_source')='lead' AND NOT EXISTS (
      SELECT 1 FROM turn_agent_binding b JOIN turn_agent_execution e ON e.id=b.primary_execution_id AND e.turn_id=b.turn_id
      WHERE b.turn_id=NEW.turn_id AND b.binding_source='lead' AND e.dispatch_source='lead'
        AND json_extract(NEW.descriptor_json,'$.resource.kind')='agent'
        AND json_extract(NEW.descriptor_json,'$.resource.id')=b.requested_agent_id
        AND json_extract(NEW.descriptor_json,'$.resource.source_kind')=b.agent_source_kind
        AND json_extract(NEW.descriptor_json,'$.resource.version')=b.revision_ref
        AND json_extract(NEW.descriptor_json,'$.resource.release_id') IS b.release_id
        AND json_extract(NEW.descriptor_json,'$.resource.assignment_id') IS b.assignment_id
        AND json_type(NEW.descriptor_json,'$.node_id') IS NULL
        AND ((NEW.content_key='reference:primary:skill-baseline' AND json_extract(NEW.descriptor_json,'$.content_kind')='skill_baseline')
          OR (NEW.content_key='reference:primary:skill-extension' AND json_extract(NEW.descriptor_json,'$.content_kind')='skill_extension')))
    BEGIN SELECT RAISE(ABORT,'reference material receiver origin mismatch'); END""",
)


def upgrade() -> None:
    if context.config.attributes['bowhead_target'] != 'core':
        return
    db = context.get_bind()
    for name, sql in PREVIOUS.items():
        row = db.exec_driver_sql('SELECT type,sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if row is None or row[0] != 'trigger' or BASE._normalized(row[1]) != BASE._normalized(sql):
            raise RuntimeError('automatic composition schema drift')
    if db.exec_driver_sql('SELECT 1 FROM sqlite_master WHERE name IN (?,?)', (TRIGGER, CONTENT_TRIGGER)).fetchone():
        raise RuntimeError('automatic composition schema drift')
    db.exec_driver_sql('SAVEPOINT reference_auto_composition')
    try:
        for name, sql in REPLACEMENTS.items():
            db.exec_driver_sql(f'DROP TRIGGER {name}')
            db.exec_driver_sql(sql)
        for sql in DDL:
            db.exec_driver_sql(sql)
        db.exec_driver_sql('RELEASE SAVEPOINT reference_auto_composition')
    except BaseException:
        db.exec_driver_sql('ROLLBACK TO SAVEPOINT reference_auto_composition')
        db.exec_driver_sql('RELEASE SAVEPOINT reference_auto_composition')
        raise


def downgrade() -> None:
    raise NotImplementedError('Forward-only; restore from snapshot to roll back.')
