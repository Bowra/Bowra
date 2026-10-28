"""Physical LLM receipts for the initial legacy reference runtime only.

Revision ID: 0071_reference_model_invocation
Revises: 0070_reference_model_context

No Terminal effect copy, historical inference, money amount or billing writer.
"""

from __future__ import annotations

import re

from alembic import context

revision = '0071_reference_model_invocation'
down_revision = '0070_reference_model_context'
branch_labels = None
depends_on = None

DDL = (
    """
    CREATE TABLE chat_reference_model_invocations (
      turn_id TEXT NOT NULL,
      invocation_id TEXT NOT NULL PRIMARY KEY CHECK(length(invocation_id)=36 AND substr(invocation_id,1,4)='rmi_'
        AND substr(invocation_id,5) NOT GLOB '*[^0-9a-f]*'),
      context_id TEXT NOT NULL,
      schema_version INTEGER NOT NULL CHECK(schema_version=1),
      execution_id TEXT NOT NULL REFERENCES turn_agent_execution(id),
      created_generation INTEGER NOT NULL CHECK(created_generation=1),
      subagent_id TEXT NOT NULL,
      provider TEXT NOT NULL CHECK(length(provider) BETWEEN 1 AND 256),
      model TEXT NOT NULL CHECK(length(model) BETWEEN 1 AND 256),
      adapter_contract_key TEXT NOT NULL CHECK(length(adapter_contract_key) BETWEEN 1 AND 256),
      mode TEXT NOT NULL CHECK(mode IN ('invoke','stream')),
      request_digest TEXT NOT NULL CHECK(length(request_digest)=64 AND request_digest NOT GLOB '*[^0-9a-f]*'),
      status TEXT NOT NULL CHECK(status IN ('pending','completed','not_applied','delivery_unknown')),
      response_digest TEXT CHECK(length(response_digest)=64 AND response_digest NOT GLOB '*[^0-9a-f]*'),
      input_tokens INTEGER CHECK(input_tokens IS NULL OR typeof(input_tokens)='integer' AND input_tokens>=0),
      output_tokens INTEGER CHECK(output_tokens IS NULL OR typeof(output_tokens)='integer' AND output_tokens>=0),
      cached_tokens INTEGER CHECK(cached_tokens IS NULL OR typeof(cached_tokens)='integer' AND cached_tokens>=0),
      retention_state TEXT NOT NULL CHECK(retention_state IN ('available','purged')),
      created_at TEXT NOT NULL,
      ended_at TEXT,
      FOREIGN KEY(turn_id,context_id) REFERENCES chat_reference_model_contexts(turn_id,context_id) ON DELETE CASCADE,
      CHECK((status='pending' AND ended_at IS NULL AND response_digest IS NULL AND input_tokens IS NULL AND output_tokens IS NULL AND cached_tokens IS NULL)
        OR (status='delivery_unknown' AND ended_at IS NOT NULL AND response_digest IS NULL AND input_tokens IS NULL AND output_tokens IS NULL AND cached_tokens IS NULL)
        OR (status='completed' AND ended_at IS NOT NULL AND response_digest IS NOT NULL AND input_tokens IS NOT NULL AND output_tokens IS NOT NULL AND cached_tokens IS NOT NULL)
        OR (status='not_applied' AND ended_at IS NOT NULL AND response_digest IS NOT NULL AND input_tokens IS NOT NULL AND output_tokens IS NOT NULL AND cached_tokens IS NOT NULL AND input_tokens=0 AND output_tokens=0 AND cached_tokens=0))
    )
    """,
    "CREATE INDEX idx_reference_model_invocation_context ON chat_reference_model_invocations(turn_id,context_id)",
    "CREATE INDEX idx_reference_model_invocation_execution ON chat_reference_model_invocations(execution_id,created_generation,status)",
    """
    CREATE TRIGGER trg_reference_model_invocation_scope BEFORE INSERT ON chat_reference_model_invocations
    WHEN NEW.status<>'pending' OR NEW.retention_state<>'available' OR NOT EXISTS (
      SELECT 1 FROM chat_reference_model_contexts c JOIN turns t ON t.id=c.turn_id
      JOIN turn_agent_execution e ON e.id=c.execution_id JOIN chat_reference_plan_retention p ON p.turn_id=c.turn_id
      WHERE c.turn_id=NEW.turn_id AND c.context_id=NEW.context_id AND c.execution_id=NEW.execution_id
        AND c.created_generation=NEW.created_generation AND c.subagent_id=NEW.subagent_id
        AND t.execution_generation=NEW.created_generation AND t.status='running' AND e.status='running'
        AND c.retention_state='available' AND p.available AND p.deadline>julianday('now'))
      OR EXISTS (SELECT 1 FROM chat_reference_model_invocations WHERE execution_id=NEW.execution_id
        AND created_generation=NEW.created_generation AND status IN ('pending','delivery_unknown'))
    BEGIN SELECT RAISE(ABORT,'reference model invocation unavailable'); END
    """,
    """
    CREATE TRIGGER trg_reference_model_invocation_no_replace BEFORE INSERT ON chat_reference_model_invocations
    WHEN EXISTS (SELECT 1 FROM chat_reference_model_invocations WHERE invocation_id=NEW.invocation_id)
    BEGIN SELECT RAISE(ABORT,'reference model invocation identity is immutable'); END
    """,
    """
    CREATE TRIGGER trg_reference_model_invocation_immutable BEFORE UPDATE ON chat_reference_model_invocations
    WHEN NEW.turn_id IS NOT OLD.turn_id OR NEW.invocation_id IS NOT OLD.invocation_id OR NEW.context_id IS NOT OLD.context_id OR NEW.schema_version IS NOT OLD.schema_version
      OR NEW.execution_id IS NOT OLD.execution_id OR NEW.created_generation IS NOT OLD.created_generation
      OR NEW.subagent_id IS NOT OLD.subagent_id OR NEW.provider IS NOT OLD.provider OR NEW.model IS NOT OLD.model
      OR NEW.adapter_contract_key IS NOT OLD.adapter_contract_key OR NEW.mode IS NOT OLD.mode
      OR NEW.request_digest IS NOT OLD.request_digest OR NEW.created_at IS NOT OLD.created_at
      OR NOT (NEW.retention_state=OLD.retention_state OR (OLD.retention_state='available' AND NEW.retention_state='purged'
        AND EXISTS (SELECT 1 FROM chat_reference_plans WHERE turn_id=OLD.turn_id AND retention_state='purged')))
      OR NOT ((NEW.status=OLD.status AND NEW.response_digest IS OLD.response_digest AND NEW.input_tokens IS OLD.input_tokens
        AND NEW.output_tokens IS OLD.output_tokens AND NEW.cached_tokens IS OLD.cached_tokens AND NEW.ended_at IS OLD.ended_at)
        OR (OLD.status='pending' AND NEW.status IN ('completed','not_applied','delivery_unknown')))
    BEGIN SELECT RAISE(ABORT,'reference model invocation is immutable'); END
    """,
    """
    CREATE TRIGGER trg_reference_model_invocation_no_active_delete BEFORE DELETE ON chat_reference_model_invocations
    WHEN EXISTS (SELECT 1 FROM chat_reference_model_contexts WHERE turn_id=OLD.turn_id AND context_id=OLD.context_id)
    BEGIN SELECT RAISE(ABORT,'reference model invocation must follow parent lifecycle'); END
    """,
    """
    CREATE TRIGGER trg_reference_model_invocation_purge AFTER UPDATE OF retention_state ON chat_reference_plans
    WHEN NEW.retention_state='purged'
    BEGIN UPDATE chat_reference_model_invocations SET retention_state='purged'
      WHERE turn_id=NEW.turn_id AND retention_state='available'; END
    """,
    """
    CREATE TRIGGER trg_reference_model_invocation_completion BEFORE UPDATE OF status ON turns
    WHEN NEW.status='completed' AND EXISTS (
      SELECT 1 FROM chat_reference_model_invocations WHERE turn_id=NEW.id
        AND created_generation=NEW.execution_generation AND status IN ('pending','delivery_unknown'))
    BEGIN SELECT RAISE(ABORT,'reference model invocation unresolved'); END
    """,
)


def upgrade() -> None:
    if context.config.attributes['bowhead_target'] != 'core':
        return
    db = context.get_bind()
    names = [re.search(r'CREATE (?:TABLE|INDEX|TRIGGER) (\w+)', sql).group(1) for sql in DDL]
    if db.exec_driver_sql('SELECT 1 FROM sqlite_master WHERE name IN (' + ','.join('?' for _ in names) + ')', tuple(names)).fetchone():
        raise RuntimeError('reference model invocation schema drift')
    db.exec_driver_sql('SAVEPOINT reference_model_invocation_upgrade')
    try:
        for sql in DDL:
            db.exec_driver_sql(sql)
    except BaseException:
        db.exec_driver_sql('ROLLBACK TO SAVEPOINT reference_model_invocation_upgrade')
        db.exec_driver_sql('RELEASE SAVEPOINT reference_model_invocation_upgrade')
        raise
    db.exec_driver_sql('RELEASE SAVEPOINT reference_model_invocation_upgrade')


def downgrade() -> None:
    raise NotImplementedError('Forward-only; restore from snapshot to roll back.')
