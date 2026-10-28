"""Admit model contexts for committed MCP Prompt material, using existing tables.

Revision ID: 0102_reference_mcp_prompt_model
Revises: 0101_personal_mcp_session_allowance
"""
from __future__ import annotations

from importlib import import_module

from alembic import context

revision = '0102_reference_mcp_prompt_model'
down_revision = '0101_personal_mcp_session_allowance'
branch_labels = None
depends_on = None

PREVIOUS = import_module('bowhead_core.migrations.versions.0092_reference_follow_up_runtime')
TRIGGER = 'trg_reference_model_context_scope'
PREVIOUS_DDL = PREVIOUS.DDL[TRIGGER]
POLICIES = "json_extract(r.composition_json,'$.runtime_policy') IN ('initial-primary-skill-read.v1','initial-primary-skill-read.v2','initial-primary-skill-baseline.v1','initial-primary-baseline.v1','initial-primary-skill-extension.v1')"
PROMPT = """(
  json_extract(r.composition_json,'$.runtime_policy')='initial-primary-mcp-prompt.v1'
  AND NEW.created_generation=1
  AND EXISTS (
    SELECT 1 FROM chat_reference_resolutions result
    WHERE result.turn_id=r.turn_id AND result.resolution_seq=2
      AND result.idempotency_key='reference:primary:mcp-prompt-results'
      AND result.execution_id=r.execution_id AND result.created_generation=r.created_generation
      AND json_extract(result.composition_json,'$.runtime_policy')='initial-primary-mcp-prompt-results.v1'
      AND json_extract(result.composition_json,'$.primary_composition_digest')=r.composition_digest
      AND NOT EXISTS (
        SELECT 1 FROM json_each(result.composition_json,'$.results') item
        WHERE NOT EXISTS (
          SELECT 1 FROM chat_reference_content_snapshots s
          WHERE s.turn_id=result.turn_id
            AND s.content_key=json_extract(item.value,'$.content.content_key')
            AND s.content_hash=json_extract(item.value,'$.content.content_hash')
            AND s.retention_state='available' AND s.ciphertext IS NOT NULL
        )
      )
  )
)"""
if PREVIOUS_DDL.count(POLICIES) != 1:
    raise RuntimeError('reference MCP Prompt model source drift')
DDL = PREVIOUS_DDL.replace(POLICIES, f'({POLICIES} OR {PROMPT})')


def upgrade() -> None:
    if context.config.attributes['bowhead_target'] != 'core':
        return
    db = context.get_bind()
    row = db.exec_driver_sql('SELECT type,sql FROM sqlite_master WHERE name=?', (TRIGGER,)).fetchone()
    if row is None or row[0] != 'trigger' or PREVIOUS._normalized(row[1]) != PREVIOUS._normalized(PREVIOUS_DDL):
        raise RuntimeError('reference MCP Prompt model schema drift')
    db.exec_driver_sql('SAVEPOINT reference_mcp_prompt_model')
    try:
        db.exec_driver_sql(f'DROP TRIGGER {TRIGGER}')
        db.exec_driver_sql(DDL)
        db.exec_driver_sql('RELEASE SAVEPOINT reference_mcp_prompt_model')
    except BaseException:
        db.exec_driver_sql('ROLLBACK TO SAVEPOINT reference_mcp_prompt_model')
        db.exec_driver_sql('RELEASE SAVEPOINT reference_mcp_prompt_model')
        raise


def downgrade() -> None:
    raise NotImplementedError('Forward-only; restore the matching database snapshot to roll back.')
