"""Initial reference composition and actual primary dispatch/output evidence.

Revision ID: 0066_reference_runtime
Revises: 0065_reference_primary_execution

Expand-only, core only. No historical compositions or usage are inferred.
"""

from __future__ import annotations

import re

from alembic import context

revision = "0066_reference_runtime"
down_revision = "0065_reference_primary_execution"
branch_labels = None
depends_on = None

DDL = (
    """
    CREATE TABLE chat_reference_resolutions (
      turn_id TEXT NOT NULL REFERENCES chat_reference_plans(turn_id) ON DELETE CASCADE,
      resolution_seq INTEGER NOT NULL CHECK(resolution_seq>=1),
      schema_version INTEGER NOT NULL CHECK(schema_version=1),
      kind TEXT NOT NULL CHECK(kind='composition'),
      idempotency_key TEXT NOT NULL,
      source_turn_id TEXT NOT NULL REFERENCES turns(id),
      created_generation INTEGER NOT NULL CHECK(created_generation>=1),
      execution_id TEXT NOT NULL REFERENCES turn_agent_execution(id),
      composition_digest TEXT NOT NULL CHECK(length(composition_digest)=71 AND substr(composition_digest,1,7)='sha256:'),
      composition_json TEXT NOT NULL CHECK(COALESCE(json_valid(composition_json)
        AND json_type(composition_json)='object' AND json_type(composition_json,'$.schema_version')='integer'
        AND json_extract(composition_json,'$.schema_version')=schema_version
        AND json_extract(composition_json,'$.source_turn_id')=source_turn_id
        AND json_extract(composition_json,'$.execution_id')=execution_id
        AND length(CAST(composition_json AS BLOB))<=1048576,0)),
      created_at TEXT NOT NULL,
      PRIMARY KEY(turn_id,resolution_seq),
      UNIQUE(turn_id,idempotency_key)
    )
    """,
    "CREATE INDEX idx_chat_reference_resolutions_source ON chat_reference_resolutions(source_turn_id,execution_id)",
    """
    CREATE TABLE chat_reference_evidence (
      turn_id TEXT NOT NULL,
      evidence_key TEXT NOT NULL,
      schema_version INTEGER NOT NULL CHECK(schema_version=1),
      resolution_seq INTEGER NOT NULL,
      execution_id TEXT NOT NULL REFERENCES turn_agent_execution(id),
      created_generation INTEGER NOT NULL CHECK(created_generation>=1),
      kind TEXT NOT NULL CHECK(kind IN ('delegate_started','output_received')),
      subagent_id TEXT NOT NULL,
      content_hash TEXT NOT NULL CHECK(length(content_hash)=71 AND substr(content_hash,1,7)='sha256:'),
      ciphertext TEXT CHECK(length(CAST(ciphertext AS BLOB))<=2097152),
      retention_state TEXT NOT NULL CHECK(retention_state IN ('available','purged')),
      created_at TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      PRIMARY KEY(turn_id,evidence_key),
      UNIQUE(execution_id,kind),
      FOREIGN KEY(turn_id,resolution_seq) REFERENCES chat_reference_resolutions(turn_id,resolution_seq) ON DELETE CASCADE,
      CHECK((kind='delegate_started' AND ciphertext IS NULL)
        OR (kind='output_received' AND ((retention_state='available' AND ciphertext IS NOT NULL)
          OR (retention_state='purged' AND ciphertext IS NULL))))
    )
    """,
    "CREATE INDEX idx_chat_reference_evidence_expiry ON chat_reference_evidence(retention_state,expires_at)",
    """
    CREATE TRIGGER trg_chat_reference_resolution_scope BEFORE INSERT ON chat_reference_resolutions
    WHEN NOT EXISTS (
      SELECT 1 FROM chat_reference_plans p JOIN turn_agent_binding b ON b.turn_id=p.turn_id
      JOIN turn_agent_execution e ON e.id=b.primary_execution_id JOIN turns t ON t.id=e.turn_id
      WHERE p.turn_id=NEW.turn_id AND p.source_turn_id=NEW.source_turn_id AND e.id=NEW.execution_id
        AND e.turn_id=NEW.turn_id AND e.status IN ('admitted','running')
        AND t.execution_generation=NEW.created_generation AND t.status='running'
        AND p.retention_state='available')
    BEGIN SELECT RAISE(ABORT,'reference composition scope mismatch'); END
    """,
    """
    CREATE TRIGGER trg_chat_reference_evidence_scope BEFORE INSERT ON chat_reference_evidence
    WHEN NOT EXISTS (
      SELECT 1 FROM chat_reference_resolutions r JOIN turns t ON t.id=r.turn_id
      JOIN turn_agent_execution e ON e.id=r.execution_id
      WHERE r.turn_id=NEW.turn_id AND r.resolution_seq=NEW.resolution_seq AND r.execution_id=NEW.execution_id
        AND r.created_generation=NEW.created_generation AND t.execution_generation=NEW.created_generation
        AND t.status='running' AND e.status='running')
    OR (NEW.kind='output_received' AND NOT EXISTS (
      SELECT 1 FROM chat_reference_evidence d WHERE d.turn_id=NEW.turn_id AND d.execution_id=NEW.execution_id
        AND d.kind='delegate_started' AND d.subagent_id=NEW.subagent_id AND d.resolution_seq=NEW.resolution_seq))
    BEGIN SELECT RAISE(ABORT,'reference evidence scope mismatch'); END
    """,
)


def upgrade() -> None:
    if context.config.attributes['bowhead_target'] != 'core':
        return
    db = context.get_bind()
    names = [re.search(r"CREATE (?:TABLE|INDEX|TRIGGER) (\w+)", sql).group(1) for sql in DDL]
    names += [f"trg_{table}_{suffix}" for table in ('chat_reference_resolutions', 'chat_reference_evidence')
              for suffix in ('no_replace', 'immutable', 'no_active_delete')]
    names += ['trg_reference_primary_completion', 'trg_reference_evidence_purge']
    if db.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name IN (" + ','.join('?' for _ in names) + ")", tuple(names)).fetchone():
        raise RuntimeError('reference runtime schema drift')
    for sql in DDL:
        db.exec_driver_sql(sql)
    for table in ('chat_reference_resolutions','chat_reference_evidence'):
        db.exec_driver_sql(f"""
          CREATE TRIGGER trg_{table}_no_replace BEFORE INSERT ON {table}
          WHEN EXISTS (SELECT 1 FROM {table} WHERE turn_id=NEW.turn_id AND
            {'resolution_seq=NEW.resolution_seq OR turn_id=NEW.turn_id AND idempotency_key=NEW.idempotency_key' if table.endswith('resolutions') else 'evidence_key=NEW.evidence_key OR execution_id=NEW.execution_id AND kind=NEW.kind'})
          BEGIN SELECT RAISE(ABORT,'reference runtime identity is immutable'); END
        """)
    db.exec_driver_sql("""
      CREATE TRIGGER trg_chat_reference_resolutions_immutable BEFORE UPDATE ON chat_reference_resolutions
      BEGIN SELECT RAISE(ABORT,'reference composition is immutable'); END
    """)
    db.exec_driver_sql("""
      CREATE TRIGGER trg_chat_reference_evidence_immutable BEFORE UPDATE ON chat_reference_evidence
      WHEN NEW.turn_id IS NOT OLD.turn_id OR NEW.evidence_key IS NOT OLD.evidence_key
        OR NEW.schema_version IS NOT OLD.schema_version OR NEW.resolution_seq IS NOT OLD.resolution_seq
        OR NEW.execution_id IS NOT OLD.execution_id OR NEW.created_generation IS NOT OLD.created_generation
        OR NEW.kind IS NOT OLD.kind OR NEW.subagent_id IS NOT OLD.subagent_id OR NEW.content_hash IS NOT OLD.content_hash
        OR NEW.created_at IS NOT OLD.created_at OR NEW.expires_at IS NOT OLD.expires_at
        OR NOT ((NEW.ciphertext IS OLD.ciphertext AND NEW.retention_state=OLD.retention_state)
          OR (OLD.retention_state='available' AND NEW.retention_state='purged' AND NEW.ciphertext IS NULL))
      BEGIN SELECT RAISE(ABORT,'reference evidence is immutable'); END
    """)

    for table in ('chat_reference_resolutions', 'chat_reference_evidence'):
        db.exec_driver_sql(f"""
          CREATE TRIGGER trg_{table}_no_active_delete BEFORE DELETE ON {table}
          WHEN EXISTS (SELECT 1 FROM {'chat_reference_plans' if table.endswith('resolutions') else 'chat_reference_resolutions'} WHERE turn_id=OLD.turn_id {'AND resolution_seq=OLD.resolution_seq' if table.endswith('evidence') else ''})
          BEGIN SELECT RAISE(ABORT,'reference runtime must follow parent lifecycle'); END
        """)
    db.exec_driver_sql("""
      CREATE TRIGGER trg_reference_primary_completion BEFORE UPDATE OF status ON turns
      WHEN NEW.status='completed' AND EXISTS (SELECT 1 FROM chat_reference_plans p WHERE p.turn_id=NEW.id)
        AND NOT EXISTS (
          SELECT 1 FROM chat_reference_resolutions r JOIN turn_agent_binding b ON b.turn_id=r.turn_id
          JOIN turn_agent_execution e ON e.id=r.execution_id JOIN chat_reference_evidence o
            ON o.turn_id=r.turn_id AND o.execution_id=e.id AND o.resolution_seq=r.resolution_seq
          WHERE r.turn_id=NEW.id AND r.resolution_seq=1 AND r.created_generation=NEW.execution_generation
            AND b.primary_execution_id=e.id AND e.status='completed' AND o.kind='output_received')
      BEGIN SELECT RAISE(ABORT,'required reference primary is incomplete'); END
    """)
    db.exec_driver_sql("""
      CREATE TRIGGER trg_reference_evidence_purge AFTER UPDATE OF retention_state ON chat_reference_plans
      WHEN NEW.retention_state='purged'
      BEGIN UPDATE chat_reference_evidence SET retention_state='purged',ciphertext=NULL
        WHERE turn_id=NEW.turn_id AND retention_state='available'; END
    """)


def downgrade() -> None:
    raise NotImplementedError('Forward-only; restore from snapshot to roll back.')
