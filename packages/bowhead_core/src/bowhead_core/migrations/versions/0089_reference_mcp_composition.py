"""Bind exact MCP read composition to its canonical source and plan.

Revision ID: 0089_reference_mcp_composition
Revises: 0088_reference_capability_skill

The new guard is additive and core-only.  Existing rows are not rewritten.
"""

from __future__ import annotations

import re

from alembic import context

revision = "0089_reference_mcp_composition"
down_revision = "0088_reference_capability_skill"
branch_labels = None
depends_on = None

TRIGGER = "trg_reference_mcp_composition"

DDL = f"""
CREATE TRIGGER {TRIGGER} BEFORE INSERT ON chat_reference_resolutions
WHEN json_extract(NEW.composition_json,'$.runtime_policy')='initial-primary-mcp-read.v1'
  OR EXISTS (
    SELECT 1 FROM chat_reference_plans p, json_each(p.plan_json,'$.admissions') a
    WHERE p.turn_id=NEW.turn_id AND json_extract(a.value,'$.resource.kind') IN ('mcp_source','mcp_tool')
  )
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM chat_reference_plans p
    JOIN chat_turn_user_sources s ON s.turn_id=p.turn_id
    JOIN turn_agent_binding b ON b.turn_id=p.turn_id AND b.primary_execution_id=NEW.execution_id
    WHERE p.turn_id=NEW.turn_id AND p.source_turn_id=NEW.source_turn_id
      AND p.logical_source_turn_id=NEW.source_turn_id AND p.snapshot_owner_turn_id=NEW.source_turn_id
      AND NEW.resolution_seq=1 AND NEW.created_generation=1
      AND NEW.kind='composition' AND NEW.idempotency_key='reference:primary'
      AND b.binding_source='mention' AND b.agent_source_kind='team'
      AND json_extract(NEW.composition_json,'$.runtime_policy')='initial-primary-mcp-read.v1'
      AND json_extract(NEW.composition_json,'$.receiver_kind')='agent'
      AND json_extract(NEW.composition_json,'$.delegation_role')='primary'
      AND json_type(NEW.composition_json,'$.receiver_source') IS NULL
      AND json_type(NEW.composition_json,'$.node_id')='text'
      AND json_extract(NEW.composition_json,'$.resource.kind')='agent'
      AND json_extract(NEW.composition_json,'$.resource.source_kind')='team'
      AND json_extract(NEW.composition_json,'$.resource.id')=b.requested_agent_id
      AND json_extract(NEW.composition_json,'$.resource.version')=b.revision_ref
      AND json_extract(NEW.composition_json,'$.resource.release_id')=b.release_id
      AND json_extract(NEW.composition_json,'$.resource.assignment_id')=b.assignment_id
      AND json_extract(NEW.composition_json,'$.policy_fingerprint')=b.policy_fingerprint
      AND json_type(NEW.composition_json,'$.mcp_policy_epoch')='integer'
      AND json_extract(NEW.composition_json,'$.mcp_policy_epoch')>=0
      AND length(json_extract(NEW.composition_json,'$.mcp_selection_digest'))=71
      AND substr(json_extract(NEW.composition_json,'$.mcp_selection_digest'),1,7)='sha256:'
      AND json_type(NEW.composition_json,'$.mcp_admissions')='array'
      AND json_type(NEW.composition_json,'$.mcp_operations')='array'
      AND json_array_length(NEW.composition_json,'$.mcp_admissions') BETWEEN 1 AND 6
      AND json_array_length(NEW.composition_json,'$.mcp_operations')>=1
      AND json_array_length(p.plan_json,'$.admissions')=
        (SELECT COUNT(*) FROM json_each(s.input_document_json,'$.nodes') n WHERE json_extract(n.value,'$.type')='reference')
      AND json_array_length(NEW.composition_json,'$.mcp_admissions')=
        (SELECT COUNT(*) FROM json_each(p.plan_json,'$.admissions') a
          WHERE json_extract(a.value,'$.resource.kind') IN ('mcp_source','mcp_tool'))
      AND (SELECT COUNT(*) FROM json_each(s.input_document_json,'$.nodes') n
        WHERE json_extract(n.value,'$.type')='reference' AND json_extract(n.value,'$.resource.kind')='agent')=1
      AND (SELECT COUNT(*) FROM json_each(s.input_document_json,'$.nodes') n
        WHERE json_extract(n.value,'$.type')='reference' AND json_extract(n.value,'$.resource.kind')='mcp_source') BETWEEN 0 AND 3
      AND (SELECT COUNT(*) FROM json_each(s.input_document_json,'$.nodes') n
        WHERE json_extract(n.value,'$.type')='reference' AND json_extract(n.value,'$.resource.kind')='mcp_tool') BETWEEN 0 AND 3
      AND (SELECT COUNT(*) FROM json_each(s.input_document_json,'$.nodes') n WHERE json_extract(n.value,'$.type')='reference')=
        (SELECT COUNT(*) FROM json_each(s.input_document_json,'$.nodes') n
          WHERE json_extract(n.value,'$.type')='reference'
            AND json_extract(n.value,'$.resource.kind') IN ('agent','mcp_source','mcp_tool'))
      AND NOT EXISTS (SELECT 1 FROM json_each(p.plan_json,'$.admissions') a WHERE NOT (
        COALESCE(json_extract(a.value,'$.resource.kind')='agent' AND json_extract(a.value,'$.operation')='delegate',0)
        OR COALESCE(json_extract(a.value,'$.resource.kind')='mcp_source' AND json_extract(a.value,'$.operation')='provide',0)
        OR COALESCE(json_extract(a.value,'$.resource.kind')='mcp_tool' AND json_extract(a.value,'$.operation')='invoke',0)))
      AND NOT EXISTS (
        SELECT 1 FROM json_each(p.plan_json,'$.admissions') a
        WHERE json_extract(a.value,'$.resource.kind') IN ('mcp_source','mcp_tool') AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.composition_json,'$.mcp_admissions') c
          WHERE json_extract(c.value,'$.node_id')=json_extract(a.value,'$.node_id')
            AND json_extract(c.value,'$.operation')=json_extract(a.value,'$.operation')
            AND json_extract(c.value,'$.policy_digest')=json_extract(a.value,'$.policy_digest')
            AND json_extract(c.value,'$.resource.kind')=json_extract(a.value,'$.resource.kind')
            AND json_extract(c.value,'$.resource.source_id')=json_extract(a.value,'$.resource.source_id')
            AND json_extract(c.value,'$.resource.local_contract_revision')=
              json_extract(a.value,'$.resource.local_contract_revision')
            AND json_extract(c.value,'$.resource.connection_binding_id')=
              json_extract(a.value,'$.resource.connection_binding_id')
            AND json_extract(c.value,'$.resource.connection_subject_fingerprint')=
              json_extract(a.value,'$.resource.connection_subject_fingerprint')
            AND json_extract(c.value,'$.resource.operation_name') IS
              json_extract(a.value,'$.resource.operation_name')
            AND json_extract(c.value,'$.resource.operation_contract_revision') IS
              json_extract(a.value,'$.resource.operation_contract_revision')
            AND json_type(c.value,'$.expanded_operation_names')='array'
            AND json_array_length(c.value,'$.expanded_operation_names')>=1))
      AND NOT EXISTS (
        SELECT 1 FROM json_each(NEW.composition_json,'$.mcp_admissions') c
        WHERE (json_extract(c.value,'$.resource.kind')='mcp_tool' AND (
            json_array_length(c.value,'$.expanded_operation_names')!=1
            OR json_extract(c.value,'$.expanded_operation_names[0]')!=
              json_extract(c.value,'$.resource.operation_name')))
          OR EXISTS (
            SELECT 1 FROM json_each(c.value,'$.expanded_operation_names') a,
              json_each(c.value,'$.expanded_operation_names') z
            WHERE a.key<z.key AND a.value=z.value)
          OR NOT EXISTS (
          SELECT 1 FROM json_each(p.plan_json,'$.admissions') a
          JOIN json_each(s.input_document_json,'$.nodes') n
          WHERE json_extract(a.value,'$.node_id')=json_extract(c.value,'$.node_id')
            AND json_extract(n.value,'$.node_id')=json_extract(c.value,'$.node_id')
            AND json_extract(n.value,'$.type')='reference'
            AND json_extract(a.value,'$.resource.kind')=json_extract(c.value,'$.resource.kind')
            AND json_extract(n.value,'$.resource.kind')=json_extract(c.value,'$.resource.kind')
            AND json_extract(a.value,'$.resource.source_id')=json_extract(c.value,'$.resource.source_id')
            AND json_extract(n.value,'$.resource.source_id')=json_extract(c.value,'$.resource.source_id')
            AND json_extract(a.value,'$.resource.local_contract_revision')=
              json_extract(c.value,'$.resource.local_contract_revision')
            AND json_extract(n.value,'$.resource.local_contract_revision')=
              json_extract(c.value,'$.resource.local_contract_revision')
            AND json_extract(a.value,'$.resource.connection_binding_id')=
              json_extract(c.value,'$.resource.connection_binding_id')
            AND json_extract(n.value,'$.resource.connection_binding_id')=
              json_extract(c.value,'$.resource.connection_binding_id')
            AND json_extract(a.value,'$.resource.connection_subject_fingerprint')=
              json_extract(c.value,'$.resource.connection_subject_fingerprint')
            AND json_extract(n.value,'$.resource.connection_subject_fingerprint')=
              json_extract(c.value,'$.resource.connection_subject_fingerprint')
            AND json_extract(a.value,'$.resource.operation_name') IS
              json_extract(c.value,'$.resource.operation_name')
            AND json_extract(n.value,'$.resource.operation_name') IS
              json_extract(c.value,'$.resource.operation_name')
            AND json_extract(a.value,'$.resource.operation_contract_revision') IS
              json_extract(c.value,'$.resource.operation_contract_revision')
            AND json_extract(n.value,'$.resource.operation_contract_revision') IS
              json_extract(c.value,'$.resource.operation_contract_revision')
            AND json_extract(a.value,'$.operation')=json_extract(c.value,'$.operation')))
      AND NOT EXISTS (
        SELECT 1 FROM json_each(NEW.composition_json,'$.mcp_admissions') c,
          json_each(c.value,'$.expanded_operation_names') name
        WHERE name.type!='text' OR NOT EXISTS (
          SELECT 1 FROM json_each(NEW.composition_json,'$.mcp_operations') op
          WHERE json_extract(op.value,'$.resource.kind')='mcp_tool'
            AND json_extract(op.value,'$.resource.source_id')=json_extract(c.value,'$.resource.source_id')
            AND json_extract(op.value,'$.resource.local_contract_revision')=
              json_extract(c.value,'$.resource.local_contract_revision')
            AND json_extract(op.value,'$.resource.connection_binding_id')=
              json_extract(c.value,'$.resource.connection_binding_id')
            AND json_extract(op.value,'$.resource.connection_subject_fingerprint')=
              json_extract(c.value,'$.resource.connection_subject_fingerprint')
            AND json_extract(op.value,'$.resource.operation_name')=name.value))
      AND NOT EXISTS (
        SELECT 1 FROM json_each(NEW.composition_json,'$.mcp_operations') op
        WHERE json_extract(op.value,'$.resource.kind')!='mcp_tool'
          OR json_type(op.value,'$.source_revision')!='text'
          OR json_type(op.value,'$.parameter_schema_json')!='text'
          OR COALESCE(CASE
            WHEN json_valid(json_extract(op.value,'$.parameter_schema_json'))
            THEN json_type(json_extract(op.value,'$.parameter_schema_json'))
            END,'invalid')!='object'
          OR length(json_extract(op.value,'$.policy_digest'))!=71
          OR substr(json_extract(op.value,'$.policy_digest'),1,7)!='sha256:'
          OR length(json_extract(op.value,'$.resource.local_contract_revision'))!=71
          OR substr(json_extract(op.value,'$.resource.local_contract_revision'),1,7)!='sha256:'
          OR length(json_extract(op.value,'$.resource.connection_subject_fingerprint'))!=71
          OR substr(json_extract(op.value,'$.resource.connection_subject_fingerprint'),1,7)!='sha256:'
          OR length(json_extract(op.value,'$.resource.operation_contract_revision'))!=71
          OR substr(json_extract(op.value,'$.resource.operation_contract_revision'),1,7)!='sha256:'
          OR NOT EXISTS (
            SELECT 1 FROM json_each(NEW.composition_json,'$.mcp_admissions') c,
              json_each(c.value,'$.expanded_operation_names') name
            WHERE json_extract(c.value,'$.resource.source_id')=json_extract(op.value,'$.resource.source_id')
              AND name.value=json_extract(op.value,'$.resource.operation_name')))
      AND NOT EXISTS (
        SELECT 1 FROM json_each(NEW.composition_json,'$.mcp_operations') a,
          json_each(NEW.composition_json,'$.mcp_operations') z
        WHERE a.key<z.key
          AND json_extract(a.value,'$.resource.source_id')=json_extract(z.value,'$.resource.source_id')
          AND json_extract(a.value,'$.resource.operation_name')=json_extract(z.value,'$.resource.operation_name'))
      AND EXISTS (
        SELECT 1 FROM json_each(p.plan_json,'$.admissions') a
        JOIN json_each(s.input_document_json,'$.nodes') n
        WHERE json_extract(a.value,'$.resource.kind')='agent'
          AND json_extract(a.value,'$.operation')='delegate'
          AND json_extract(n.value,'$.type')='reference'
          AND json_extract(n.value,'$.node_id')=json_extract(a.value,'$.node_id')
          AND json_extract(n.value,'$.resource.kind')='agent'
          AND json_extract(n.value,'$.resource.source_kind')=json_extract(a.value,'$.resource.source_kind')
          AND json_extract(n.value,'$.resource.id')=json_extract(a.value,'$.resource.id')
          AND json_extract(n.value,'$.resource.version')=json_extract(a.value,'$.resource.version')
          AND json_extract(n.value,'$.resource.release_id') IS json_extract(a.value,'$.resource.release_id')
          AND json_extract(n.value,'$.resource.assignment_id') IS json_extract(a.value,'$.resource.assignment_id')
          AND json_extract(NEW.composition_json,'$.node_id')=json_extract(a.value,'$.node_id')
          AND json_extract(NEW.composition_json,'$.resource.source_kind')=json_extract(a.value,'$.resource.source_kind')
          AND json_extract(NEW.composition_json,'$.resource.id')=json_extract(a.value,'$.resource.id')
          AND json_extract(NEW.composition_json,'$.resource.version')=json_extract(a.value,'$.resource.version')
          AND json_extract(NEW.composition_json,'$.resource.release_id') IS json_extract(a.value,'$.resource.release_id')
          AND json_extract(NEW.composition_json,'$.resource.assignment_id') IS json_extract(a.value,'$.resource.assignment_id')
          AND json_extract(NEW.composition_json,'$.policy_fingerprint')=json_extract(a.value,'$.policy_digest'))
  ) THEN RAISE(ABORT,'reference MCP composition mismatch') END;
END
"""


def _normalized(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip()).casefold()


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    if db.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name=?", (TRIGGER,)).fetchone() is not None:
        raise RuntimeError("reference MCP composition schema drift")
    db.exec_driver_sql("SAVEPOINT reference_mcp_composition")
    try:
        db.exec_driver_sql(DDL)
        row = db.exec_driver_sql("SELECT type,sql FROM sqlite_master WHERE name=?", (TRIGGER,)).fetchone()
        if row is None or row[0] != "trigger" or _normalized(row[1]) != _normalized(DDL):
            raise RuntimeError("reference MCP composition schema drift")
        db.exec_driver_sql("RELEASE SAVEPOINT reference_mcp_composition")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT reference_mcp_composition")
        db.exec_driver_sql("RELEASE SAVEPOINT reference_mcp_composition")
        raise


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore from snapshot to roll back.")
