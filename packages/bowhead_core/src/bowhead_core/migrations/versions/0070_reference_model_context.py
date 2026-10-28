"""Retained logical context preparation facts; no inferred provider usage.

Revision ID: 0070_reference_model_context
Revises: 0069_skill_reference_composition

Expand-only core sidecar. Existing one-per-execution delegate/output evidence
keeps its cardinality and constraints; model preparations are many per execution.
"""

from __future__ import annotations

import re

from alembic import context

revision = "0070_reference_model_context"
down_revision = "0069_skill_reference_composition"
branch_labels = None
depends_on = None

DDL = (
    """
    CREATE TABLE chat_reference_model_contexts (
      turn_id TEXT NOT NULL,
      context_id TEXT NOT NULL CHECK(length(context_id)=36 AND substr(context_id,1,4)='ric_'
        AND substr(context_id,5) NOT GLOB '*[^0-9a-f]*'),
      schema_version INTEGER NOT NULL CHECK(schema_version=1),
      kind TEXT NOT NULL CHECK(kind='context_assembled'),
      resolution_seq INTEGER NOT NULL CHECK(resolution_seq=1),
      execution_id TEXT NOT NULL REFERENCES turn_agent_execution(id),
      created_generation INTEGER NOT NULL CHECK(created_generation>=1),
      subagent_id TEXT NOT NULL,
      composition_digest TEXT NOT NULL CHECK(length(composition_digest)=71 AND substr(composition_digest,1,7)='sha256:'),
      input_hash TEXT NOT NULL CHECK(length(input_hash)=71 AND substr(input_hash,1,7)='sha256:'),
      required_content_hash TEXT NOT NULL CHECK(length(required_content_hash)=71 AND substr(required_content_hash,1,7)='sha256:'),
      retention_state TEXT NOT NULL CHECK(retention_state IN ('available','purged')),
      created_at TEXT NOT NULL,
      PRIMARY KEY(turn_id,context_id),
      FOREIGN KEY(turn_id,resolution_seq) REFERENCES chat_reference_resolutions(turn_id,resolution_seq) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX idx_reference_model_context_execution ON chat_reference_model_contexts(execution_id,created_generation)",
    """
    CREATE TRIGGER trg_reference_model_context_scope BEFORE INSERT ON chat_reference_model_contexts
    WHEN NEW.retention_state<>'available' OR NOT EXISTS (
      SELECT 1 FROM chat_reference_resolutions r JOIN turns t ON t.id=r.turn_id
      JOIN turn_agent_execution e ON e.id=r.execution_id
      JOIN chat_reference_plan_retention p ON p.turn_id=r.turn_id
      WHERE r.turn_id=NEW.turn_id AND r.resolution_seq=NEW.resolution_seq
        AND r.execution_id=NEW.execution_id AND r.composition_digest=NEW.composition_digest
        AND r.created_generation=NEW.created_generation AND t.execution_generation=NEW.created_generation
        AND t.status='running' AND e.status='running' AND p.available AND p.deadline>julianday('now')
        AND json_extract(r.composition_json,'$.runtime_policy')='initial-primary-skill-read.v1')
    BEGIN SELECT RAISE(ABORT,'reference model context scope mismatch'); END
    """,
    """
    CREATE TRIGGER trg_reference_model_context_no_replace BEFORE INSERT ON chat_reference_model_contexts
    WHEN EXISTS (SELECT 1 FROM chat_reference_model_contexts WHERE turn_id=NEW.turn_id AND context_id=NEW.context_id)
    BEGIN SELECT RAISE(ABORT,'reference model context identity is immutable'); END
    """,
    """
    CREATE TRIGGER trg_reference_model_context_immutable BEFORE UPDATE ON chat_reference_model_contexts
    WHEN NEW.turn_id IS NOT OLD.turn_id OR NEW.context_id IS NOT OLD.context_id
      OR NEW.schema_version IS NOT OLD.schema_version OR NEW.kind IS NOT OLD.kind
      OR NEW.resolution_seq IS NOT OLD.resolution_seq OR NEW.execution_id IS NOT OLD.execution_id
      OR NEW.created_generation IS NOT OLD.created_generation OR NEW.subagent_id IS NOT OLD.subagent_id
      OR NEW.composition_digest IS NOT OLD.composition_digest OR NEW.input_hash IS NOT OLD.input_hash
      OR NEW.required_content_hash IS NOT OLD.required_content_hash OR NEW.created_at IS NOT OLD.created_at
      OR NOT (NEW.retention_state=OLD.retention_state OR (OLD.retention_state='available' AND NEW.retention_state='purged'
        AND EXISTS (SELECT 1 FROM chat_reference_plans WHERE turn_id=OLD.turn_id AND retention_state='purged')))
    BEGIN SELECT RAISE(ABORT,'reference model context is immutable'); END
    """,
    """
    CREATE TRIGGER trg_reference_model_context_no_active_delete BEFORE DELETE ON chat_reference_model_contexts
    WHEN EXISTS (SELECT 1 FROM chat_reference_resolutions WHERE turn_id=OLD.turn_id AND resolution_seq=OLD.resolution_seq)
    BEGIN SELECT RAISE(ABORT,'reference model context must follow parent lifecycle'); END
    """,
    """
    CREATE TRIGGER trg_reference_model_context_purge AFTER UPDATE OF retention_state ON chat_reference_plans
    WHEN NEW.retention_state='purged'
    BEGIN UPDATE chat_reference_model_contexts SET retention_state='purged'
      WHERE turn_id=NEW.turn_id AND retention_state='available'; END
    """,
)


def upgrade() -> None:
    if context.config.attributes['bowhead_target'] != 'core':
        return
    db = context.get_bind()
    names = [re.search(r'CREATE (?:TABLE|INDEX|TRIGGER) (\w+)', sql).group(1) for sql in DDL]
    if db.exec_driver_sql('SELECT 1 FROM sqlite_master WHERE name IN (' + ','.join('?' for _ in names) + ')', tuple(names)).fetchone():
        raise RuntimeError('reference model context schema drift')
    db.exec_driver_sql('SAVEPOINT reference_model_context_upgrade')
    try:
        for sql in DDL:
            db.exec_driver_sql(sql)
    except BaseException:
        db.exec_driver_sql('ROLLBACK TO SAVEPOINT reference_model_context_upgrade')
        db.exec_driver_sql('RELEASE SAVEPOINT reference_model_context_upgrade')
        raise
    db.exec_driver_sql('RELEASE SAVEPOINT reference_model_context_upgrade')


def downgrade() -> None:
    raise NotImplementedError('Forward-only; restore from snapshot to roll back.')
