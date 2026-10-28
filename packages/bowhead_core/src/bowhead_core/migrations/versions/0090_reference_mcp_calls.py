"""Persist exact MCP reference call identity and replayable read results.

Revision ID: 0090_reference_mcp_calls
Revises: 0089_reference_mcp_composition

The table is additive and core-only.  It stores no credentials and does not
infer calls for existing reference Turns.
"""

from __future__ import annotations

import re

from alembic import context

revision = "0090_reference_mcp_calls"
down_revision = "0089_reference_mcp_composition"
branch_labels = None
depends_on = None

TABLE = "chat_reference_mcp_calls"
INDEXES = ("uq_reference_mcp_required_call", "idx_reference_mcp_calls_expiry")
TRIGGERS = (
    "trg_reference_mcp_call_scope",
    "trg_reference_mcp_call_no_replace",
    "trg_reference_mcp_call_update",
    "trg_reference_mcp_call_no_delete",
    "trg_reference_mcp_call_purge",
)

DDL = (
    f"""
    CREATE TABLE {TABLE} (
      turn_id TEXT NOT NULL,
      call_id TEXT NOT NULL,
      call_seq INTEGER NOT NULL CHECK(call_seq>=1),
      schema_version INTEGER NOT NULL CHECK(schema_version=1),
      resolution_seq INTEGER NOT NULL CHECK(resolution_seq=1),
      execution_id TEXT NOT NULL REFERENCES turn_agent_execution(id),
      created_generation INTEGER NOT NULL CHECK(created_generation=1),
      owner_id TEXT NOT NULL CHECK(length(owner_id) BETWEEN 1 AND 256),
      node_id TEXT NOT NULL CHECK(length(node_id)=36),
      required_tool INTEGER NOT NULL CHECK(required_tool IN (0,1)),
      source_id TEXT NOT NULL CHECK(length(source_id) BETWEEN 1 AND 256),
      operation_name TEXT NOT NULL CHECK(length(operation_name) BETWEEN 1 AND 512),
      local_contract_revision TEXT NOT NULL CHECK(length(local_contract_revision)=71 AND substr(local_contract_revision,1,7)='sha256:'),
      connection_binding_id TEXT NOT NULL CHECK(length(connection_binding_id) BETWEEN 1 AND 256),
      connection_subject_fingerprint TEXT NOT NULL CHECK(length(connection_subject_fingerprint)=71 AND substr(connection_subject_fingerprint,1,7)='sha256:'),
      operation_contract_revision TEXT NOT NULL CHECK(length(operation_contract_revision)=71 AND substr(operation_contract_revision,1,7)='sha256:'),
      parameter_digest TEXT NOT NULL CHECK(length(parameter_digest)=71 AND substr(parameter_digest,1,7)='sha256:'),
      status TEXT NOT NULL CHECK(status IN ('started','completed','failed','interrupted')),
      result_ok INTEGER CHECK(result_ok IN (0,1)),
      result_hash TEXT CHECK(result_hash IS NULL OR length(result_hash)=71 AND substr(result_hash,1,7)='sha256:'),
      result_ciphertext TEXT CHECK(result_ciphertext IS NULL OR length(CAST(result_ciphertext AS BLOB))<=2097152),
      failure_code TEXT CHECK(failure_code IS NULL OR length(failure_code) BETWEEN 1 AND 128),
      retention_state TEXT NOT NULL CHECK(retention_state IN ('available','purged')),
      started_at TEXT NOT NULL,
      completed_at TEXT,
      expires_at TEXT NOT NULL,
      PRIMARY KEY(turn_id,call_id),
      UNIQUE(turn_id,call_seq),
      FOREIGN KEY(turn_id,resolution_seq) REFERENCES chat_reference_resolutions(turn_id,resolution_seq) ON DELETE CASCADE,
      CHECK((status='started' AND result_ok IS NULL AND result_hash IS NULL AND result_ciphertext IS NULL
              AND failure_code IS NULL AND completed_at IS NULL AND retention_state='available')
        OR (((status='completed' AND result_ok=1) OR (status='failed' AND result_ok=0)) AND result_hash IS NOT NULL
              AND ((retention_state='available' AND result_ciphertext IS NOT NULL)
                OR (retention_state='purged' AND result_ciphertext IS NULL)) AND completed_at IS NOT NULL)
        OR (status='interrupted' AND result_ok IS NULL AND result_hash IS NULL AND result_ciphertext IS NULL
              AND failure_code IS NOT NULL AND completed_at IS NOT NULL))
    )
    """,
    f"CREATE UNIQUE INDEX {INDEXES[0]} ON {TABLE}(turn_id,node_id) WHERE required_tool=1",
    f"CREATE INDEX {INDEXES[1]} ON {TABLE}(retention_state,expires_at)",
    f"""
    CREATE TRIGGER {TRIGGERS[0]} BEFORE INSERT ON {TABLE}
    WHEN NOT EXISTS (
      SELECT 1 FROM chat_reference_resolutions r
      JOIN turns t ON t.id=r.turn_id
      JOIN turn_agent_execution e ON e.id=r.execution_id
      JOIN chat_turn_spawn_dispatch d ON d.turn_id=r.turn_id
      WHERE r.turn_id=NEW.turn_id AND r.resolution_seq=NEW.resolution_seq
        AND r.execution_id=NEW.execution_id AND r.created_generation=NEW.created_generation
        AND json_extract(r.composition_json,'$.runtime_policy')='initial-primary-mcp-read.v1'
        AND e.status='running' AND t.status='running' AND t.execution_generation=NEW.created_generation
        AND d.state='spawned' AND d.owner_id=NEW.owner_id
        AND d.lease_execution_generation=NEW.created_generation AND d.lease_kind='turn'
        AND d.dispatch_kind='start' AND d.parent_turn_id IS NULL AND d.cancel_requested_at IS NULL
        AND julianday(d.lease_expires_at)>julianday(NEW.started_at)
        AND julianday(NEW.expires_at)<=julianday((SELECT expires_at FROM chat_reference_plans WHERE turn_id=NEW.turn_id))
        AND EXISTS (
          SELECT 1 FROM json_each(r.composition_json,'$.mcp_operations') op
          WHERE json_extract(op.value,'$.resource.source_id')=NEW.source_id
            AND json_extract(op.value,'$.resource.operation_name')=NEW.operation_name
            AND json_extract(op.value,'$.resource.local_contract_revision')=NEW.local_contract_revision
            AND json_extract(op.value,'$.resource.connection_binding_id')=NEW.connection_binding_id
            AND json_extract(op.value,'$.resource.connection_subject_fingerprint')=NEW.connection_subject_fingerprint
            AND json_extract(op.value,'$.resource.operation_contract_revision')=NEW.operation_contract_revision)
        AND EXISTS (
          SELECT 1 FROM json_each(r.composition_json,'$.mcp_admissions') a
          WHERE json_extract(a.value,'$.node_id')=NEW.node_id
            AND json_extract(a.value,'$.resource.source_id')=NEW.source_id
            AND ((NEW.required_tool=1
                AND json_extract(a.value,'$.resource.kind')='mcp_tool'
                AND json_extract(a.value,'$.resource.operation_name')=NEW.operation_name
                AND json_extract(a.value,'$.operation')='invoke')
              OR (NEW.required_tool=0
                AND json_extract(a.value,'$.resource.kind')='mcp_source'
                AND json_extract(a.value,'$.operation')='provide'
                AND EXISTS (SELECT 1 FROM json_each(a.value,'$.expanded_operation_names') n WHERE n.value=NEW.operation_name))))
    )
    BEGIN SELECT RAISE(ABORT,'reference MCP call scope mismatch'); END
    """,
    f"""
    CREATE TRIGGER {TRIGGERS[1]} BEFORE INSERT ON {TABLE}
    WHEN EXISTS (SELECT 1 FROM {TABLE} WHERE turn_id=NEW.turn_id
      AND (call_id=NEW.call_id OR call_seq=NEW.call_seq OR required_tool=1 AND node_id=NEW.node_id))
    BEGIN SELECT RAISE(ABORT,'reference MCP call identity is immutable'); END
    """,
    f"""
    CREATE TRIGGER {TRIGGERS[2]} BEFORE UPDATE ON {TABLE}
    WHEN NEW.turn_id IS NOT OLD.turn_id OR NEW.call_id IS NOT OLD.call_id OR NEW.call_seq IS NOT OLD.call_seq
      OR NEW.schema_version IS NOT OLD.schema_version OR NEW.resolution_seq IS NOT OLD.resolution_seq
      OR NEW.execution_id IS NOT OLD.execution_id OR NEW.created_generation IS NOT OLD.created_generation
      OR NEW.owner_id IS NOT OLD.owner_id OR NEW.node_id IS NOT OLD.node_id OR NEW.required_tool IS NOT OLD.required_tool
      OR NEW.source_id IS NOT OLD.source_id OR NEW.operation_name IS NOT OLD.operation_name
      OR NEW.local_contract_revision IS NOT OLD.local_contract_revision
      OR NEW.connection_binding_id IS NOT OLD.connection_binding_id
      OR NEW.connection_subject_fingerprint IS NOT OLD.connection_subject_fingerprint
      OR NEW.operation_contract_revision IS NOT OLD.operation_contract_revision
      OR NEW.parameter_digest IS NOT OLD.parameter_digest OR NEW.started_at IS NOT OLD.started_at
      OR NEW.expires_at IS NOT OLD.expires_at
      OR NOT ((OLD.status='started' AND NEW.status IN ('completed','failed','interrupted')
              AND NEW.retention_state='available')
        OR (OLD.status=NEW.status AND OLD.result_ok IS NEW.result_ok AND OLD.result_hash IS NEW.result_hash
              AND OLD.completed_at IS NEW.completed_at AND OLD.failure_code IS NEW.failure_code
              AND OLD.retention_state='available' AND NEW.retention_state='purged' AND NEW.result_ciphertext IS NULL))
    BEGIN SELECT RAISE(ABORT,'reference MCP call is immutable'); END
    """,
    f"""
    CREATE TRIGGER {TRIGGERS[3]} BEFORE DELETE ON {TABLE}
    WHEN EXISTS (SELECT 1 FROM chat_reference_resolutions WHERE turn_id=OLD.turn_id AND resolution_seq=OLD.resolution_seq)
    BEGIN SELECT RAISE(ABORT,'reference MCP call must follow parent lifecycle'); END
    """,
    f"""
    CREATE TRIGGER {TRIGGERS[4]} AFTER UPDATE OF retention_state ON chat_reference_plans
    WHEN NEW.retention_state='purged'
    BEGIN UPDATE {TABLE} SET retention_state='purged',result_ciphertext=NULL
      WHERE turn_id=NEW.turn_id AND retention_state='available' AND status IN ('completed','failed'); END
    """,
)


def _normalized(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip()).casefold()


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    names = (TABLE, *INDEXES, *TRIGGERS)
    if db.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE name IN (" + ",".join("?" for _ in names) + ")",
        names,
    ).fetchone():
        raise RuntimeError("reference MCP call schema drift")
    db.exec_driver_sql("SAVEPOINT reference_mcp_calls")
    try:
        for sql in DDL:
            db.exec_driver_sql(sql)
        rows = db.exec_driver_sql(
            "SELECT name,type,sql FROM sqlite_master WHERE name IN (" + ",".join("?" for _ in names) + ")",
            names,
        ).fetchall()
        actual = {row[0]: (row[1], _normalized(row[2])) for row in rows}
        expected = {}
        for sql in DDL:
            match = re.search(r"CREATE (TABLE|(?:UNIQUE )?INDEX|TRIGGER) (\w+)", sql, re.I)
            assert match is not None
            kind = "index" if "INDEX" in match.group(1).upper() else match.group(1).lower()
            expected[match.group(2)] = (kind, _normalized(sql))
        if actual != expected:
            raise RuntimeError("reference MCP call schema drift")
        db.exec_driver_sql("RELEASE SAVEPOINT reference_mcp_calls")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT reference_mcp_calls")
        db.exec_driver_sql("RELEASE SAVEPOINT reference_mcp_calls")
        raise


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore from snapshot to roll back.")
