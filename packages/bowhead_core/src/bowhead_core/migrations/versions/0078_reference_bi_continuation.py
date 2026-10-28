"""Retain the first native BI clarification under its original reference execution.

Revision ID: 0078_reference_bi_continuation
Revises: 0077_merge_agent_reference
Create Date: 2026-09-14 23:53:18.527456

"""
from __future__ import annotations

import re
from importlib import import_module

from alembic import context

revision = "0078_reference_bi_continuation"
down_revision = "0077_merge_agent_reference"
branch_labels = None
depends_on = None

PREVIOUS = import_module("bowhead_core.migrations.versions.0076_reference_primary_baseline")
RUNTIME = import_module("bowhead_core.migrations.versions.0066_reference_runtime")
INVOCATIONS = import_module("bowhead_core.migrations.versions.0071_reference_model_invocation")
TABLE = "chat_reference_bi_continuations"
RESUME_FENCE = """EXISTS (
 SELECT 1 FROM chat_reference_bi_continuations c JOIN chat_turn_spawn_dispatch d ON d.turn_id=c.turn_id
 WHERE c.turn_id=r.turn_id AND c.execution_id=r.execution_id AND c.composition_digest=r.composition_digest
 AND c.state='started' AND c.retention_state='available' AND c.resume_generation=NEW.created_generation
 AND c.subagent_id=NEW.subagent_id AND julianday(c.expires_at)>julianday('now')
 AND d.state='spawned' AND d.owner_id IS NOT NULL AND d.lease_kind='resume'
 AND d.lease_execution_generation=c.resume_generation AND d.dispatch_kind='start' AND d.parent_turn_id IS NULL
 AND d.cancel_requested_at IS NULL AND julianday(d.lease_expires_at)>julianday('now')
)"""
PREVIOUS_EVIDENCE = next(sql for sql in RUNTIME.DDL if "CREATE TRIGGER trg_chat_reference_evidence_scope " in sql)
PREVIOUS_COMPLETION = """
      CREATE TRIGGER trg_reference_primary_completion BEFORE UPDATE OF status ON turns
      WHEN NEW.status='completed' AND EXISTS (SELECT 1 FROM chat_reference_plans p WHERE p.turn_id=NEW.id)
        AND NOT EXISTS (
          SELECT 1 FROM chat_reference_resolutions r JOIN turn_agent_binding b ON b.turn_id=r.turn_id
          JOIN turn_agent_execution e ON e.id=r.execution_id JOIN chat_reference_evidence o
            ON o.turn_id=r.turn_id AND o.execution_id=e.id AND o.resolution_seq=r.resolution_seq
          WHERE r.turn_id=NEW.id AND r.resolution_seq=1 AND r.created_generation=NEW.execution_generation
            AND b.primary_execution_id=e.id AND e.status='completed' AND o.kind='output_received')
      BEGIN SELECT RAISE(ABORT,'required reference primary is incomplete'); END
"""
COMPLETION_RESUME_FENCE = """EXISTS (
 SELECT 1 FROM chat_reference_bi_continuations c
 WHERE c.turn_id=NEW.id AND c.execution_id=r.execution_id AND c.composition_digest=r.composition_digest
 AND c.resume_generation=NEW.execution_generation AND c.state IN ('started','closed') AND c.retention_state='available'
 AND o.created_generation=NEW.execution_generation AND o.subagent_id=c.subagent_id
)"""
REPLACEMENTS = {
    PREVIOUS.CONTEXT_TRIGGER: (PREVIOUS.CONTEXT_DDL, PREVIOUS.CONTEXT_DDL.replace(
        "r.created_generation=NEW.created_generation", f"(r.created_generation=NEW.created_generation OR {RESUME_FENCE})")),
    "trg_chat_reference_evidence_scope": (PREVIOUS_EVIDENCE, PREVIOUS_EVIDENCE.replace(
        "r.created_generation=NEW.created_generation", f"(r.created_generation=NEW.created_generation OR {RESUME_FENCE})")),
    "trg_reference_primary_completion": (PREVIOUS_COMPLETION, PREVIOUS_COMPLETION.replace(
        "r.created_generation=NEW.execution_generation", f"(r.created_generation=NEW.execution_generation OR {COMPLETION_RESUME_FENCE})")),
}
DDL = (
    """CREATE TABLE chat_reference_bi_continuations (
      turn_id TEXT PRIMARY KEY REFERENCES chat_reference_plans(turn_id) ON DELETE CASCADE,
      execution_id TEXT NOT NULL UNIQUE REFERENCES turn_agent_execution(id),
      schema_version INTEGER NOT NULL CHECK(schema_version=1),
      created_generation INTEGER NOT NULL CHECK(created_generation=1),
      subagent_id TEXT NOT NULL,
      source_hash TEXT NOT NULL CHECK(length(source_hash)=71 AND substr(source_hash,1,7)='sha256:'),
      composition_digest TEXT NOT NULL CHECK(length(composition_digest)=71 AND substr(composition_digest,1,7)='sha256:'),
      checkpoint_hash TEXT NOT NULL CHECK(length(checkpoint_hash)=71 AND substr(checkpoint_hash,1,7)='sha256:'),
      ciphertext TEXT CHECK(length(CAST(ciphertext AS BLOB))<=2097152),
      retention_state TEXT NOT NULL CHECK(retention_state IN ('available','purged')),
      clarification_id TEXT UNIQUE,
      state TEXT NOT NULL CHECK(state IN ('captured','ready','reserved','started','closed')),
      resume_generation INTEGER CHECK(resume_generation>=2),
      answer_hash TEXT CHECK(length(answer_hash)=71 AND substr(answer_hash,1,7)='sha256:'),
      started_at TEXT,
      created_at TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      CHECK((retention_state='available' AND ciphertext IS NOT NULL) OR (retention_state='purged' AND ciphertext IS NULL)),
      CHECK(state='closed'
        OR (state='captured' AND clarification_id IS NULL AND resume_generation IS NULL AND answer_hash IS NULL AND started_at IS NULL)
        OR (state='ready' AND clarification_id IS NOT NULL AND resume_generation IS NULL AND answer_hash IS NULL AND started_at IS NULL)
        OR (state='reserved' AND clarification_id IS NOT NULL AND resume_generation IS NOT NULL AND answer_hash IS NOT NULL AND started_at IS NULL)
        OR (state='started' AND clarification_id IS NOT NULL AND resume_generation IS NOT NULL AND answer_hash IS NOT NULL AND started_at IS NOT NULL))
    )""",
    """CREATE TRIGGER trg_reference_bi_continuation_scope BEFORE INSERT ON chat_reference_bi_continuations
    WHEN NEW.state<>'captured' OR NEW.retention_state<>'available' OR NOT EXISTS (
      SELECT 1 FROM chat_reference_resolutions r JOIN chat_reference_plans p ON p.turn_id=r.turn_id
      JOIN chat_reference_plan_retention a ON a.turn_id=p.turn_id
      JOIN turns t ON t.id=r.turn_id JOIN turn_agent_execution e ON e.id=r.execution_id
      JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id
      JOIN chat_reference_evidence v ON v.turn_id=t.id AND v.execution_id=e.id AND v.kind='delegate_started'
      WHERE r.turn_id=NEW.turn_id AND r.resolution_seq=1 AND r.created_generation=1
      AND r.execution_id=NEW.execution_id AND r.composition_digest=NEW.composition_digest
      AND p.source_hash=NEW.source_hash AND NEW.expires_at=p.expires_at AND a.available AND a.deadline>julianday('now')
      AND json_extract(r.composition_json,'$.runtime_policy') IN ('initial-primary-skill-read.v2','initial-primary-skill-baseline.v1','initial-primary-baseline.v1')
      AND t.execution_generation=1 AND t.status='running' AND e.status='running' AND v.subagent_id=NEW.subagent_id
      AND d.state='spawned' AND d.owner_id IS NOT NULL AND d.lease_kind='turn' AND d.lease_execution_generation=1
      AND d.dispatch_kind='start' AND d.parent_turn_id IS NULL AND d.cancel_requested_at IS NULL
      AND julianday(d.lease_expires_at)>julianday('now')
      AND (SELECT COUNT(*) FROM chat_reference_model_invocations i WHERE i.execution_id=e.id AND i.status='completed')=1
      AND NOT EXISTS (SELECT 1 FROM chat_reference_model_invocations i WHERE i.execution_id=e.id AND i.status IN ('pending','delivery_unknown'))
      AND NOT EXISTS (SELECT 1 FROM chat_reference_evidence o WHERE o.execution_id=e.id AND o.kind='output_received')
    ) BEGIN SELECT RAISE(ABORT,'reference BI continuation scope mismatch'); END""",
    """CREATE TRIGGER trg_reference_bi_continuation_no_replace BEFORE INSERT ON chat_reference_bi_continuations
    WHEN EXISTS(SELECT 1 FROM chat_reference_bi_continuations WHERE turn_id=NEW.turn_id OR execution_id=NEW.execution_id)
    BEGIN SELECT RAISE(ABORT,'reference BI continuation identity is immutable'); END""",
    """CREATE TRIGGER trg_reference_bi_continuation_immutable BEFORE UPDATE ON chat_reference_bi_continuations
    WHEN NEW.turn_id IS NOT OLD.turn_id OR NEW.execution_id IS NOT OLD.execution_id OR NEW.schema_version IS NOT OLD.schema_version
      OR NEW.created_generation IS NOT OLD.created_generation OR NEW.subagent_id IS NOT OLD.subagent_id
      OR NEW.source_hash IS NOT OLD.source_hash OR NEW.composition_digest IS NOT OLD.composition_digest
      OR NEW.checkpoint_hash IS NOT OLD.checkpoint_hash OR NEW.created_at IS NOT OLD.created_at OR NEW.expires_at IS NOT OLD.expires_at
      OR (OLD.clarification_id IS NOT NULL AND NEW.clarification_id IS NOT OLD.clarification_id)
      OR NOT ((NEW.retention_state IS OLD.retention_state AND NEW.ciphertext IS OLD.ciphertext)
        OR (OLD.retention_state='available' AND NEW.retention_state='purged' AND NEW.ciphertext IS NULL))
      OR NOT ((OLD.state='captured' AND NEW.state IN ('ready','closed'))
        OR (OLD.state='ready' AND NEW.state IN ('reserved','closed'))
        OR (OLD.state='reserved' AND NEW.state IN ('ready','started','closed'))
        OR (OLD.state='started' AND NEW.state='closed') OR (OLD.state='closed' AND NEW.state='closed'))
      OR (OLD.state IN ('started','closed') AND (NEW.resume_generation IS NOT OLD.resume_generation
        OR NEW.answer_hash IS NOT OLD.answer_hash OR NEW.started_at IS NOT OLD.started_at))
    BEGIN SELECT RAISE(ABORT,'reference BI continuation transition rejected'); END""",
    """CREATE TRIGGER trg_reference_bi_continuation_no_active_delete BEFORE DELETE ON chat_reference_bi_continuations
    WHEN EXISTS(SELECT 1 FROM chat_reference_plans WHERE turn_id=OLD.turn_id)
    BEGIN SELECT RAISE(ABORT,'reference BI continuation must follow parent lifecycle'); END""",
    """CREATE TRIGGER trg_reference_bi_continuation_purge AFTER UPDATE OF retention_state ON chat_reference_plans
    WHEN NEW.retention_state='purged'
    BEGIN UPDATE chat_reference_bi_continuations SET retention_state='purged',ciphertext=NULL,state='closed'
      WHERE turn_id=NEW.turn_id AND retention_state='available'; END""",
)


def _normalized(sql: str) -> str:
    return " ".join(sql.split()).rstrip(";")


def _invocation_objects():
    return {re.search(r"CREATE (TABLE|INDEX|TRIGGER) (\w+)", sql).group(2): sql for sql in INVOCATIONS.DDL}


def _expand_invocation_generation(db) -> None:
    """SQLite CHECK requires copy-migrate; preserve every original receipt."""
    table = "chat_reference_model_invocations"
    shadow = table + "_0078"
    ddl = INVOCATIONS.DDL[0].replace(f"CREATE TABLE {table} (", f"CREATE TABLE {shadow} (").replace(
        "CHECK(created_generation=1)", "CHECK(created_generation>=1)")
    db.exec_driver_sql(ddl)
    db.exec_driver_sql(f"INSERT INTO {shadow} SELECT * FROM {table}")
    if (
        db.exec_driver_sql(f"SELECT COUNT(*) FROM {shadow}").scalar() != db.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar()
        or db.exec_driver_sql(f"SELECT 1 FROM (SELECT * FROM {shadow} EXCEPT SELECT * FROM {table}) LIMIT 1").fetchone()
        or db.exec_driver_sql(f"SELECT 1 FROM (SELECT * FROM {table} EXCEPT SELECT * FROM {shadow}) LIMIT 1").fetchone()
    ):
        raise RuntimeError("reference model receipt copy mismatch")
    # Incoming triggers are also suspended within the same savepoint so SQLite
    # never has to resolve a missing ledger while renaming the replacement.
    for name, sql in _invocation_objects().items():
        kind = re.search(r"CREATE (TABLE|INDEX|TRIGGER)", sql).group(1)
        if kind != "TABLE":
            db.exec_driver_sql(f"DROP {kind} {name}")
    db.exec_driver_sql(f"DROP TABLE {table}")
    db.exec_driver_sql(f"ALTER TABLE {shadow} RENAME TO {table}")
    for sql in INVOCATIONS.DDL[1:]:
        db.exec_driver_sql(sql)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    names = [re.search(r"CREATE (?:TABLE|TRIGGER) (\w+)", sql).group(1) for sql in DDL]
    if db.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name IN (" + ",".join("?" for _ in names) + ")", tuple(names)).fetchone():
        raise RuntimeError("reference BI continuation schema drift")
    for name, (expected, _replacement) in REPLACEMENTS.items():
        row = db.exec_driver_sql("SELECT type,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if row is None or row[0] != "trigger" or _normalized(row[1]) != _normalized(expected):
            raise RuntimeError("reference BI continuation schema drift")
    expected_ledger = _invocation_objects()
    actual_ledger = dict(db.exec_driver_sql(
        "SELECT name,sql FROM sqlite_master WHERE sql LIKE '%chat_reference_model_invocations%' AND sql IS NOT NULL"
    ).fetchall())
    if set(actual_ledger) != set(expected_ledger) or any(_normalized(actual_ledger[name]) != _normalized(sql) for name, sql in expected_ledger.items()):
        raise RuntimeError("reference model receipt schema drift")
    db.exec_driver_sql("SAVEPOINT reference_bi_continuation_upgrade")
    try:
        _expand_invocation_generation(db)
        for sql in DDL:
            db.exec_driver_sql(sql)
        for name, (_expected, replacement) in REPLACEMENTS.items():
            db.exec_driver_sql(f"DROP TRIGGER {name}")
            db.exec_driver_sql(replacement)
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT reference_bi_continuation_upgrade")
        db.exec_driver_sql("RELEASE SAVEPOINT reference_bi_continuation_upgrade")
        raise
    db.exec_driver_sql("RELEASE SAVEPOINT reference_bi_continuation_upgrade")


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore from snapshot to roll back.")
