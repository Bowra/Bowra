"""Retain exact Turn-owned planning attempts without rewriting legacy charges."""

import re
from importlib import import_module

from alembic import context

revision = "0149_document_planning_attempts"
down_revision = "0148_document_revisions"
branch_labels = None
depends_on = None

PREVIOUS = import_module("bowhead_core.migrations.versions.0130_document_drafts")
RECOVERY = import_module("bowhead_core.migrations.versions.0131_document_recovery")
CALLS = "document_model_calls"
USAGE = "document_usage_receipts"
TABLES = (CALLS, USAGE)


def _name(sql):
    return re.search(r"CREATE (?:TABLE|(?:UNIQUE )?INDEX|TRIGGER) (\w+)", sql).group(1)


def _sql(value):
    return "".join(value.lower().replace('"', '').split())


OLD_OBJECTS = {
    _name(sql): sql
    for sql in (*PREVIOUS.DDL, *RECOVERY.DDL)
    if _name(sql) in {
        CALLS, USAGE, "trg_document_model_call_admission", "trg_document_model_call_identity",
        "idx_document_usage_pending", "trg_document_usage_admission", "trg_document_usage_identity", "trg_document_usage_delete",
    }
}
OLD_OBJECTS["trg_document_model_calls_delete"] = """CREATE TRIGGER trg_document_model_calls_delete BEFORE DELETE ON document_model_calls
                  BEGIN SELECT RAISE(ABORT,'document execution journal is retained'); END"""

CALL_TABLE = OLD_OBJECTS[CALLS].replace(
    "PRIMARY KEY(job_id,call_no)", "PRIMARY KEY(job_id,attempt_no,call_no)",
).replace("PRIMARY KEY(job_id,attempt_no,call_no)", "attempt_no INTEGER NOT NULL DEFAULT 1 CHECK(attempt_no IN (1,2)), PRIMARY KEY(job_id,attempt_no,call_no)", 1)
USAGE_TABLE = OLD_OBJECTS[USAGE].replace(
    "PRIMARY KEY(job_id,call_no)", "attempt_no INTEGER NOT NULL DEFAULT 1 CHECK(attempt_no IN (1,2)), PRIMARY KEY(job_id,call_no)", 1,
).replace("PRIMARY KEY(job_id,call_no)", "PRIMARY KEY(job_id,attempt_no,call_no)").replace(
    "FOREIGN KEY(job_id,call_no) REFERENCES document_model_calls(job_id,call_no)",
    "FOREIGN KEY(job_id,attempt_no,call_no) REFERENCES document_model_calls(job_id,attempt_no,call_no)",
)

# This predicate checks persisted scheduling authority, not a browser pointer.
WORKER = """SELECT 1 FROM document_jobs j JOIN turns t ON t.id=j.origin_turn_id
 JOIN sessions s ON s.id=t.session_id JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id
 WHERE j.id=NEW.job_id AND j.status='queued' AND t.status='running'
 AND s.id=j.session_id AND s.active_turn_id=t.id AND s.user_id=j.requested_by AND s.workspace_id=j.workspace_id AND s.deleted_at IS NULL
 AND d.session_id=s.id AND d.state='spawned' AND d.owner_id=NEW.owner_id
 AND d.lease_execution_generation=NEW.creation_generation AND t.execution_generation=NEW.creation_generation
 AND d.lease_kind IN ('turn','resume') AND d.cancel_requested_at IS NULL AND julianday(d.lease_expires_at)>julianday('now')"""

DDL = (
    """CREATE TABLE document_turn_dispatches (
      job_id TEXT PRIMARY KEY NOT NULL REFERENCES document_dispatches(job_id),
      creation_generation INTEGER NOT NULL CHECK(creation_generation>=1), owner_id TEXT NOT NULL,
      source_hash TEXT NOT NULL CHECK(length(source_hash)=64),
      runtime_snapshot_json TEXT NOT NULL CHECK(json_valid(runtime_snapshot_json)),
      runtime_snapshot_digest TEXT NOT NULL CHECK(length(runtime_snapshot_digest)=64), created_at TEXT NOT NULL
    )""",
    """CREATE TABLE document_planning_attempts (
      job_id TEXT NOT NULL REFERENCES document_turn_dispatches(job_id), attempt_no INTEGER NOT NULL CHECK(attempt_no IN (1,2)),
      creation_generation INTEGER NOT NULL CHECK(creation_generation>=1), owner_id TEXT NOT NULL,
      card_id TEXT REFERENCES clarification_requests(request_id),
      instruction TEXT NOT NULL CHECK(length(instruction) BETWEEN 1 AND 8000),
      inputs_digest TEXT NOT NULL CHECK(length(inputs_digest)=64),
      context_json TEXT CHECK(context_json IS NULL OR json_valid(context_json)),
      checkpoint_json TEXT CHECK(checkpoint_json IS NULL OR json_valid(checkpoint_json)),
      checkpoint_digest TEXT CHECK(checkpoint_digest IS NULL OR length(checkpoint_digest)=64), created_at TEXT NOT NULL,
      PRIMARY KEY(job_id,attempt_no), UNIQUE(card_id),
      CHECK((attempt_no=1 AND card_id IS NULL) OR (attempt_no=2 AND card_id IS NOT NULL)),
      CHECK((checkpoint_json IS NULL AND checkpoint_digest IS NULL) OR
        (checkpoint_json IS NOT NULL AND checkpoint_digest IS NOT NULL AND context_json IS NOT NULL AND attempt_no=1))
    )""",
    """CREATE TABLE document_physical_receipt_links (
      job_id TEXT NOT NULL, attempt_no INTEGER NOT NULL, call_no INTEGER NOT NULL CHECK(call_no IN (1,2)),
      receipt_id TEXT NOT NULL UNIQUE REFERENCES llm_usage_events(id),
      PRIMARY KEY(job_id,attempt_no,call_no),
      FOREIGN KEY(job_id,attempt_no) REFERENCES document_planning_attempts(job_id,attempt_no)
    )""",
    f"""CREATE TRIGGER trg_document_turn_dispatch_admission BEFORE INSERT ON document_turn_dispatches
      WHEN NOT EXISTS({WORKER}) OR NOT EXISTS(SELECT 1 FROM document_jobs j JOIN document_dispatches d ON d.job_id=j.id
        WHERE j.id=NEW.job_id AND j.user_source_sha256=NEW.source_hash
          AND json_extract(NEW.runtime_snapshot_json,'$.image_id')=d.image_id
          AND json_extract(NEW.runtime_snapshot_json,'$.model_config_version')=d.model_config_version)
        OR EXISTS(SELECT 1 FROM document_executions WHERE job_id=NEW.job_id)
      BEGIN SELECT RAISE(ABORT,'document Turn dispatch unavailable'); END""",
    f"""CREATE TRIGGER trg_document_planning_attempt_admission BEFORE INSERT ON document_planning_attempts
      WHEN NOT EXISTS({WORKER}) OR NEW.context_json IS NOT NULL OR NEW.checkpoint_json IS NOT NULL
        OR NOT EXISTS(SELECT 1 FROM document_input_sets WHERE job_id=NEW.job_id AND frozen_at IS NOT NULL)
        OR NOT EXISTS(SELECT 1 FROM document_turn_dispatches d WHERE d.job_id=NEW.job_id AND
          ((NEW.attempt_no=1 AND NEW.owner_id=d.owner_id AND NEW.creation_generation=d.creation_generation)
            OR (NEW.attempt_no=2 AND NEW.creation_generation>d.creation_generation
              AND EXISTS(SELECT 1 FROM document_planning_attempts a JOIN clarification_requests q ON q.request_id=NEW.card_id
                JOIN document_jobs j ON j.id=a.job_id JOIN document_executions e ON e.job_id=j.id
                WHERE a.job_id=NEW.job_id AND a.attempt_no=1 AND a.checkpoint_digest IS NOT NULL
                  AND e.state='question' AND e.owner=a.owner_id AND q.status='resolved' AND q.turn_id=j.origin_turn_id
                  AND q.session_id=j.session_id AND q.user_id=j.requested_by AND q.workspace_id=j.workspace_id
                  AND json_extract(q.resume_context_json,'$.__schema_version')=3
                  AND json_extract(q.resume_context_json,'$.continuation.kind')='document_planning'
                  AND json_extract(q.resume_context_json,'$.continuation.checkpoint.job_id')=j.id
                  AND json_extract(q.resume_context_json,'$.continuation.checkpoint.checkpoint_digest')=a.checkpoint_digest))))
      BEGIN SELECT RAISE(ABORT,'document planning attempt unavailable'); END""",
    """CREATE TRIGGER trg_document_planning_attempt_identity BEFORE UPDATE ON document_planning_attempts
      WHEN NEW.job_id IS NOT OLD.job_id OR NEW.attempt_no IS NOT OLD.attempt_no
        OR NEW.creation_generation IS NOT OLD.creation_generation OR NEW.owner_id IS NOT OLD.owner_id
        OR NEW.card_id IS NOT OLD.card_id OR NEW.instruction IS NOT OLD.instruction OR NEW.inputs_digest IS NOT OLD.inputs_digest
        OR NEW.created_at IS NOT OLD.created_at OR (OLD.context_json IS NOT NULL AND NEW.context_json IS NOT OLD.context_json)
        OR (OLD.checkpoint_json IS NOT NULL AND (NEW.checkpoint_json IS NOT OLD.checkpoint_json OR NEW.checkpoint_digest IS NOT OLD.checkpoint_digest))
      BEGIN SELECT RAISE(ABORT,'document planning attempt is immutable'); END""",
    f"""CREATE TRIGGER trg_document_planning_attempt_material BEFORE UPDATE ON document_planning_attempts
      WHEN (NEW.context_json IS NOT OLD.context_json OR NEW.checkpoint_json IS NOT OLD.checkpoint_json)
        AND (NOT EXISTS({WORKER}) OR NOT EXISTS(SELECT 1 FROM document_executions e
          WHERE e.job_id=NEW.job_id AND e.owner=NEW.owner_id AND e.state IN ('preparing','planning','rendering'))
          OR (NEW.checkpoint_json IS NOT NULL AND (
            NOT EXISTS(SELECT 1 FROM document_model_calls c JOIN document_usage_receipts r
              ON r.job_id=c.job_id AND r.attempt_no=c.attempt_no AND r.call_no=c.call_no
              WHERE c.job_id=NEW.job_id AND c.attempt_no=NEW.attempt_no
                AND c.call_no=json_extract(NEW.checkpoint_json,'$.last_call_no') AND c.state='completed' AND r.settled_at IS NOT NULL
                AND c.response_digest=json_extract(NEW.checkpoint_json,'$.response_digest'))
            OR EXISTS(SELECT 1 FROM document_model_calls c LEFT JOIN document_usage_receipts r
              ON r.job_id=c.job_id AND r.attempt_no=c.attempt_no AND r.call_no=c.call_no
              WHERE c.job_id=NEW.job_id AND (c.state<>'completed' OR r.settled_at IS NULL)))))
      BEGIN SELECT RAISE(ABORT,'document checkpoint unavailable'); END""",
    """CREATE TRIGGER trg_document_physical_link_admission BEFORE INSERT ON document_physical_receipt_links
      WHEN NOT EXISTS(SELECT 1 FROM document_planning_attempts a JOIN document_jobs j ON j.id=a.job_id
        JOIN document_executions e ON e.job_id=j.id JOIN llm_usage_events u ON u.id=NEW.receipt_id
        JOIN turns t ON t.id=j.origin_turn_id JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id
        WHERE a.job_id=NEW.job_id AND a.attempt_no=NEW.attempt_no AND e.owner=a.owner_id AND e.state='planning'
          AND j.status='queued' AND t.status='running' AND d.state='spawned' AND d.owner_id=a.owner_id
          AND d.session_id=j.session_id AND d.cancel_requested_at IS NULL AND julianday(d.lease_expires_at)>julianday('now')
          AND t.execution_generation=a.creation_generation AND d.lease_execution_generation=a.creation_generation
          AND u.team_id=j.team_id AND u.user_id=j.requested_by AND u.workspace_id=j.workspace_id AND u.turn_id=t.id
          AND u.receipt_state='pending' AND u.reason='template_document'
          AND json_extract(u.receipt_json,'$.generation')=a.creation_generation)
      BEGIN SELECT RAISE(ABORT,'document physical receipt unavailable'); END""",
)

CALL_ADMISSION = OLD_OBJECTS["trg_document_model_call_admission"].replace(
    "call_no=NEW.call_no)", "call_no=NEW.call_no AND attempt_no=NEW.attempt_no)",
).replace("BEGIN SELECT RAISE", """OR (NEW.attempt_no=2 AND NOT EXISTS(SELECT 1 FROM document_planning_attempts WHERE job_id=NEW.job_id AND attempt_no=2))
        OR (EXISTS(SELECT 1 FROM document_turn_dispatches WHERE job_id=NEW.job_id) AND NOT EXISTS(
          SELECT 1 FROM document_planning_attempts a JOIN document_executions e ON e.job_id=a.job_id
          JOIN document_physical_receipt_links l ON l.job_id=a.job_id AND l.attempt_no=a.attempt_no AND l.call_no=NEW.call_no
          JOIN llm_usage_events u ON u.id=l.receipt_id JOIN document_jobs j ON j.id=a.job_id
          JOIN turns t ON t.id=j.origin_turn_id JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id
          WHERE a.job_id=NEW.job_id AND a.attempt_no=NEW.attempt_no AND a.owner_id=e.owner
            AND d.state='spawned' AND d.owner_id=a.owner_id AND t.status='running' AND d.cancel_requested_at IS NULL
            AND d.session_id=j.session_id AND d.lease_kind IN ('turn','resume') AND julianday(d.lease_expires_at)>julianday('now')
            AND d.lease_execution_generation=a.creation_generation AND t.execution_generation=a.creation_generation
            AND u.receipt_state='pending' AND u.provider=NEW.provider AND u.model=NEW.model
            AND json_extract(u.receipt_json,'$.request_digest')=NEW.request_digest))
      BEGIN SELECT RAISE""")
NEW_OBJECTS = {
    name: sql.replace("c.call_no=NEW.call_no", "c.call_no=NEW.call_no AND c.attempt_no=NEW.attempt_no").replace(
        "NEW.call_no IS NOT OLD.call_no", "NEW.call_no IS NOT OLD.call_no OR NEW.attempt_no IS NOT OLD.attempt_no",
    )
    for name, sql in OLD_OBJECTS.items() if name not in TABLES
}
NEW_OBJECTS["trg_document_model_call_admission"] = CALL_ADMISSION
NEW_OBJECTS["trg_document_usage_admission"] = NEW_OBJECTS["trg_document_usage_admission"].replace(
    "WHEN NOT EXISTS", "WHEN EXISTS(SELECT 1 FROM document_usage_receipts WHERE job_id=NEW.job_id AND attempt_no=NEW.attempt_no AND call_no=NEW.call_no) OR NOT EXISTS", 1,
)
OLD_EXECUTION_GUARD = next(sql for sql in PREVIOUS.DDL if _name(sql) == "trg_document_execution_identity")
RESUME_EXECUTION = """(OLD.state='question' AND NEW.state='planning' AND EXISTS(
 SELECT 1 FROM document_planning_attempts a JOIN document_planning_attempts p ON p.job_id=a.job_id AND p.attempt_no=1
 JOIN document_jobs j ON j.id=a.job_id JOIN turns t ON t.id=j.origin_turn_id
 JOIN sessions s ON s.id=t.session_id JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id
 WHERE a.job_id=OLD.job_id AND a.attempt_no=2 AND a.owner_id=NEW.owner AND p.owner_id=OLD.owner
 AND p.checkpoint_digest IS NOT NULL AND j.status='queued' AND t.status='running' AND s.active_turn_id=t.id
 AND d.state='spawned' AND d.owner_id=a.owner_id AND d.session_id=s.id AND d.lease_kind='resume'
 AND d.lease_execution_generation=a.creation_generation AND t.execution_generation=a.creation_generation
 AND d.cancel_requested_at IS NULL AND julianday(d.lease_expires_at)>julianday('now')))"""
EXECUTION_GUARD = OLD_EXECUTION_GUARD.replace(
    "NEW.owner IS NOT OLD.owner", f"(NEW.owner IS NOT OLD.owner AND NOT {RESUME_EXECUTION})",
).replace(
    "OLD.state IN ('failed','question') AND NEW.state NOT IN (OLD.state,'cancelled')",
    f"(OLD.state IN ('failed','question') AND NEW.state NOT IN (OLD.state,'cancelled') AND NOT {RESUME_EXECUTION})",
)


def upgrade():
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    actual = {name: sql for name, sql in db.exec_driver_sql("SELECT name,sql FROM sqlite_schema WHERE tbl_name IN (?,?) AND sql IS NOT NULL", TABLES)}
    if {name: _sql(sql) for name, sql in actual.items()} != {name: _sql(sql) for name, sql in OLD_OBJECTS.items()}:
        raise RuntimeError("document planning ledger schema drift")
    guard = db.exec_driver_sql("SELECT sql FROM sqlite_schema WHERE name='trg_document_execution_identity'").scalar()
    if guard is None or _sql(guard) != _sql(OLD_EXECUTION_GUARD):
        raise RuntimeError("document execution schema drift")
    # Replacing both related tables is safe only while no unreviewed table,
    # view or trigger has acquired another dependency on either ledger.
    for name, sql in db.exec_driver_sql("SELECT name,sql FROM sqlite_schema WHERE sql IS NOT NULL"):
        if name not in OLD_OBJECTS and any(re.search(rf"\b{table}\b", sql, re.IGNORECASE) for table in TABLES):
            raise RuntimeError(f"document planning ledger dependency drift: {name}")
    db.exec_driver_sql("SAVEPOINT document_planning_attempts")
    legacy = db.exec_driver_sql("PRAGMA legacy_alter_table").scalar()
    try:
        db.exec_driver_sql("PRAGMA legacy_alter_table=OFF")
        for name, sql in ((CALLS, CALL_TABLE), (USAGE, USAGE_TABLE)):
            temporary = f"{name}_next"
            ddl = sql.replace(f"CREATE TABLE {name}", f"CREATE TABLE {temporary}", 1)
            if name == USAGE:
                ddl = ddl.replace("REFERENCES document_model_calls(", "REFERENCES document_model_calls_next(")
            db.exec_driver_sql(ddl)
            columns = ",".join(row[1] for row in db.exec_driver_sql(f"PRAGMA table_info({name})"))
            db.exec_driver_sql(f"INSERT INTO {temporary}({columns}) SELECT {columns} FROM {name}")
            if db.exec_driver_sql(f"SELECT 1 FROM (SELECT {columns} FROM {name} EXCEPT SELECT {columns} FROM {temporary}) UNION ALL SELECT 1 FROM (SELECT {columns} FROM {temporary} EXCEPT SELECT {columns} FROM {name})").fetchone():
                raise RuntimeError("document planning ledger copy mismatch")
            if db.exec_driver_sql(f"SELECT count(*) FROM {name}").scalar() != db.exec_driver_sql(f"SELECT count(*) FROM {temporary}").scalar():
                raise RuntimeError("document planning ledger row count mismatch")
        for name, sql in OLD_OBJECTS.items():
            if sql.lstrip().startswith("CREATE TRIGGER"):
                db.exec_driver_sql(f"DROP TRIGGER {name}")
        db.exec_driver_sql(f"DROP TABLE {USAGE}")
        db.exec_driver_sql(f"DROP TABLE {CALLS}")
        db.exec_driver_sql(f"ALTER TABLE {CALLS}_next RENAME TO {CALLS}")
        db.exec_driver_sql(f"ALTER TABLE {USAGE}_next RENAME TO {USAGE}")
        for sql in DDL:
            db.exec_driver_sql(sql)
        for sql in NEW_OBJECTS.values():
            db.exec_driver_sql(sql)
        db.exec_driver_sql("DROP TRIGGER trg_document_execution_identity")
        db.exec_driver_sql(EXECUTION_GUARD)
        for table, key in (
            ("document_turn_dispatches", "job_id=NEW.job_id"),
            ("document_planning_attempts", "job_id=NEW.job_id AND attempt_no=NEW.attempt_no"),
            ("document_physical_receipt_links", "(job_id=NEW.job_id AND attempt_no=NEW.attempt_no AND call_no=NEW.call_no) OR receipt_id=NEW.receipt_id"),
        ):
            for operation in ("INSERT", "UPDATE", "DELETE"):
                if table == "document_planning_attempts" and operation == "UPDATE":
                    continue
                condition = f"WHEN EXISTS(SELECT 1 FROM {table} WHERE {key})" if operation == "INSERT" else ""
                db.exec_driver_sql(f"""CREATE TRIGGER trg_{table}_{operation.lower()} BEFORE {operation} ON {table} {condition}
                  BEGIN SELECT RAISE(ABORT,'document planning identity is retained'); END""")
        for table in (*TABLES, "document_turn_dispatches", "document_planning_attempts", "document_physical_receipt_links"):
            if db.exec_driver_sql(f"PRAGMA foreign_key_check({table})").fetchone():
                raise RuntimeError("document planning foreign key mismatch")
        db.exec_driver_sql("RELEASE SAVEPOINT document_planning_attempts")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT document_planning_attempts")
        db.exec_driver_sql("RELEASE SAVEPOINT document_planning_attempts")
        raise
    finally:
        db.exec_driver_sql(f"PRAGMA legacy_alter_table={int(legacy)}")


def downgrade():
    raise NotImplementedError("Forward-only migration; restore a matching database backup.")
