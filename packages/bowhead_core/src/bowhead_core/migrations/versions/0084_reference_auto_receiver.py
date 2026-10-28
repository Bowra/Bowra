"""Keep automatic reference receiver origin in the existing primary ledger.

Revision ID: 0084_reference_auto_receiver
Revises: 0083_reference_read_checkpoint

Core only. Existing roots and execution IDs are copied byte-for-byte; a new
origin does not fabricate a user Agent reference or another execution ledger.
"""

from __future__ import annotations

from importlib import import_module

from alembic import context

revision = "0084_reference_auto_receiver"
down_revision = "0083_reference_read_checkpoint"
branch_labels = None
depends_on = None

TABLE = "turn_agent_binding"
TEMP = "turn_agent_binding__auto_receiver"
GUARD = "trg_reference_automatic_receiver_source"
_BASE = import_module("bowhead_core.migrations.versions.0034_agent_turn_binding")
_BASE_TABLE = next(sql for kind, name, sql in _BASE._OBJECTS if kind == "table" and name == TABLE)
OLD_TABLE = _BASE_TABLE.rsplit(")", 1)[0] + ", primary_execution_id TEXT REFERENCES turn_agent_execution(id) DEFERRABLE INITIALLY DEFERRED)"
OLD_PRIMARY = """
CREATE TRIGGER trg_turn_agent_binding_primary_execution BEFORE INSERT ON turn_agent_binding
WHEN NEW.primary_execution_id IS NOT NULL
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM turn_agent_execution e
    JOIN chat_reference_plans p ON p.turn_id=e.turn_id AND p.retention_state='available'
    JOIN chat_turn_user_sources s ON s.turn_id=e.turn_id AND s.input_document_json IS NOT NULL
    WHERE e.id=NEW.primary_execution_id AND e.turn_id=NEW.turn_id AND e.team_id=NEW.team_id
      AND e.agent_id=NEW.requested_agent_id AND e.agent_source_kind=NEW.agent_source_kind
      AND e.revision_ref=NEW.revision_ref AND e.release_id IS NEW.release_id
      AND e.capability_fingerprint=NEW.capability_fingerprint AND e.policy_fingerprint=NEW.policy_fingerprint
      AND e.parent_execution_id IS NULL AND e.status='admitted'
      AND e.dispatch_source='explicit' AND e.dispatch_key='reference:primary'
      AND NEW.binding_source='mention' AND s.user_id=NEW.user_id
  ) THEN RAISE(ABORT,'reference primary execution mismatch') END;
END
"""
PRIMARY = OLD_PRIMARY.replace(
    "AND e.dispatch_source='explicit' AND e.dispatch_key='reference:primary'\n      AND NEW.binding_source='mention'",
    "AND e.dispatch_key='reference:primary' AND ((e.dispatch_source='explicit' AND NEW.binding_source='mention')\n"
    "        OR (e.dispatch_source='lead' AND NEW.binding_source='lead'))",
)
_OLD_OBJECTS = {
    (kind, name): sql for kind, name, sql in _BASE._OBJECTS if kind != "table" and name.startswith(("trg_turn_agent_binding_", "idx_turn_agent_binding_"))
}
_OLD_OBJECTS.update({
    ("index", "idx_turn_agent_binding_primary_execution"): "CREATE UNIQUE INDEX idx_turn_agent_binding_primary_execution ON turn_agent_binding(primary_execution_id) WHERE primary_execution_id IS NOT NULL",
    ("trigger", "trg_turn_agent_binding_primary_execution"): OLD_PRIMARY,
    ("trigger", "trg_turn_agent_binding_primary_no_replace"): """
      CREATE TRIGGER trg_turn_agent_binding_primary_no_replace BEFORE INSERT ON turn_agent_binding
      WHEN EXISTS (SELECT 1 FROM turn_agent_binding WHERE turn_id=NEW.turn_id AND primary_execution_id IS NOT NULL)
      BEGIN SELECT RAISE(ABORT,'reference primary binding is immutable'); END
    """,
})
SOURCE_GUARD = f"""
CREATE TRIGGER {GUARD} BEFORE INSERT ON {TABLE}
WHEN NEW.binding_source='lead'
BEGIN
  SELECT CASE WHEN NEW.primary_execution_id IS NULL OR NOT EXISTS (
    SELECT 1 FROM chat_reference_plans p
    JOIN chat_turn_user_sources s ON s.turn_id=p.turn_id AND s.user_id=NEW.user_id
    WHERE p.turn_id=NEW.turn_id AND p.source_turn_id=NEW.turn_id
      AND p.logical_source_turn_id=NEW.turn_id AND p.snapshot_owner_turn_id=NEW.turn_id
      AND json_valid(s.input_document_json) AND json_extract(s.input_document_json,'$.schema_version')=1
      AND (SELECT COUNT(*) FROM json_each(s.input_document_json,'$.nodes') n WHERE json_extract(n.value,'$.type')='reference') BETWEEN 1 AND 3
      AND NOT EXISTS (SELECT 1 FROM json_each(s.input_document_json,'$.nodes') n
        WHERE json_extract(n.value,'$.type')='reference' AND COALESCE(json_extract(n.value,'$.resource.kind')='skill',0)=0)
      AND json_array_length(p.plan_json,'$.admissions')=(SELECT COUNT(*) FROM json_each(s.input_document_json,'$.nodes') n WHERE json_extract(n.value,'$.type')='reference')
      AND NOT EXISTS (SELECT 1 FROM json_each(p.plan_json,'$.admissions') a
        WHERE COALESCE(json_extract(a.value,'$.operation')='read' AND json_extract(a.value,'$.resource.kind')='skill',0)=0)
      AND NOT EXISTS (SELECT 1 FROM json_each(s.input_document_json,'$.nodes') n
        WHERE json_extract(n.value,'$.type')='reference' AND NOT EXISTS (
          SELECT 1 FROM json_each(p.plan_json,'$.admissions') a
          WHERE json_extract(a.value,'$.node_id')=json_extract(n.value,'$.node_id')
            AND json_extract(a.value,'$.resource.source_kind')=json_extract(n.value,'$.resource.source_kind')
            AND json_extract(a.value,'$.resource.id')=json_extract(n.value,'$.resource.id')
            AND json_extract(a.value,'$.resource.version')=json_extract(n.value,'$.resource.version')))
  ) THEN RAISE(ABORT,'automatic reference receiver source mismatch') END;
END
"""


def _sql(value: str) -> str:
    return "".join(value.lower().replace('"', '').split())


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    row = db.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
    objects = {(kind, name): sql for kind, name, sql in db.exec_driver_sql("SELECT type,name,sql FROM sqlite_master WHERE tbl_name=? AND sql IS NOT NULL AND type<>'table'", (TABLE,))}
    if row is None or _sql(row[0]) != _sql(OLD_TABLE) or {key: _sql(sql) for key, sql in objects.items()} != {key: _sql(sql) for key, sql in _OLD_OBJECTS.items()}:
        raise RuntimeError("automatic reference binding schema drift")
    if db.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name IN (?,?)", (TEMP, GUARD)).fetchone():
        raise RuntimeError("automatic reference binding schema drift")
    # This table has no incoming FKs. Preserve its outbound FKs and all triggers
    # in other tables; legacy_alter_table only permits the brief missing-table
    # interval inside this locked savepoint without rewriting those triggers.
    legacy = db.exec_driver_sql("PRAGMA legacy_alter_table").scalar()
    db.exec_driver_sql("SAVEPOINT reference_auto_receiver")
    try:
        db.exec_driver_sql("PRAGMA legacy_alter_table=ON")
        table = OLD_TABLE.replace(f"CREATE TABLE {TABLE}", f"CREATE TABLE {TEMP}", 1).replace("'schedule','plan'", "'schedule','plan','lead'", 1)
        db.exec_driver_sql(table)
        db.exec_driver_sql(f"INSERT INTO {TEMP} SELECT * FROM {TABLE}")
        if db.exec_driver_sql(f"SELECT 1 FROM (SELECT * FROM {TABLE} EXCEPT SELECT * FROM {TEMP}) UNION ALL SELECT 1 FROM (SELECT * FROM {TEMP} EXCEPT SELECT * FROM {TABLE})").fetchone():
            raise RuntimeError("automatic reference binding copy mismatch")
        db.exec_driver_sql(f"DROP TABLE {TABLE}")
        db.exec_driver_sql(f"ALTER TABLE {TEMP} RENAME TO {TABLE}")
        for (_, name), sql in objects.items():
            db.exec_driver_sql(PRIMARY if name == "trg_turn_agent_binding_primary_execution" else sql)
        db.exec_driver_sql(SOURCE_GUARD)
        if db.exec_driver_sql(f"PRAGMA foreign_key_check({TABLE})").fetchone():
            raise RuntimeError("automatic reference binding foreign key mismatch")
        db.exec_driver_sql("RELEASE SAVEPOINT reference_auto_receiver")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT reference_auto_receiver")
        db.exec_driver_sql("RELEASE SAVEPOINT reference_auto_receiver")
        raise
    finally:
        db.exec_driver_sql(f"PRAGMA legacy_alter_table={int(legacy)}")


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore from snapshot to roll back.")
