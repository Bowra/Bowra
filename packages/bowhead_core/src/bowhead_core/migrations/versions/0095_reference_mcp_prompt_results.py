"""Bind MCP Prompt result snapshots to the exact primary composition.

Revision ID: 0095_reference_mcp_prompt_results
Revises: 0094_reference_mcp_prompt_composition

The guards are additive and core-only. Existing rows are not rewritten.
"""

from __future__ import annotations

import re
from importlib import import_module

from alembic import context

revision = "0095_reference_mcp_prompt_results"
down_revision = "0094_reference_mcp_prompt_composition"
branch_labels = None
depends_on = None

SNAPSHOT_TRIGGER = "trg_reference_mcp_prompt_result_snapshot"
RESOLUTION_TRIGGER = "trg_reference_mcp_prompt_result_resolution"
CONTENT_PREFIX = "reference:primary:mcp-prompt-result:"
AUTO = import_module("bowhead_core.migrations.versions.0087_reference_capability")
AUTO_TRIGGER = AUTO.COMPOSITION_TRIGGER
PREVIOUS_AUTO_DDL = AUTO.COMPOSITION_DDL
AUTO_DDL = PREVIOUS_AUTO_DDL.replace(
    "WHEN NOT EXISTS (",
    "WHEN json_extract(NEW.composition_json,'$.runtime_policy') IS NOT 'initial-primary-mcp-prompt-results.v1'\n    AND NOT EXISTS (",
    1,
)

SNAPSHOT_DDL = f"""
CREATE TRIGGER {SNAPSHOT_TRIGGER} BEFORE INSERT ON chat_reference_content_snapshots
WHEN json_extract(NEW.descriptor_json,'$.content_kind')='prompt_result'
  OR NEW.content_key GLOB '{CONTENT_PREFIX}*'
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM chat_reference_resolutions c
    JOIN chat_reference_plans p ON p.turn_id=c.turn_id
    JOIN json_each(c.composition_json,'$.prompts') prompt
    JOIN json_each(p.plan_json,'$.admissions') admitted
      ON json_extract(admitted.value,'$.node_id')=json_extract(prompt.value,'$.node_id')
    WHERE c.turn_id=NEW.turn_id AND c.resolution_seq=1
      AND c.kind='composition' AND c.idempotency_key='reference:primary'
      AND json_extract(c.composition_json,'$.runtime_policy')='initial-primary-mcp-prompt.v1'
      AND NEW.content_key='{CONTENT_PREFIX}' || CAST(CAST(prompt.key AS INTEGER)+1 AS TEXT)
      AND NEW.schema_version=1 AND NEW.created_generation=c.created_generation
      AND NEW.created_policy_epoch=json_extract(c.composition_json,'$.prompt_policy_epoch')
      AND NEW.retention_state='available' AND NEW.ciphertext IS NOT NULL
      AND NEW.content_hash=json_extract(NEW.descriptor_json,'$.content_hash')
      AND NEW.byte_count=json_extract(NEW.descriptor_json,'$.byte_count')
      AND json_extract(NEW.descriptor_json,'$.schema_version')=1
      AND json_extract(NEW.descriptor_json,'$.content_key')=NEW.content_key
      AND json_extract(NEW.descriptor_json,'$.content_kind')='prompt_result'
      AND json_extract(NEW.descriptor_json,'$.node_id')=json_extract(prompt.value,'$.node_id')
      AND json_extract(NEW.descriptor_json,'$.resource.kind')='mcp_prompt'
      AND json_extract(NEW.descriptor_json,'$.resource.source_id')=json_extract(prompt.value,'$.resource.source_id')
      AND json_extract(NEW.descriptor_json,'$.resource.prompt_name')=json_extract(prompt.value,'$.resource.prompt_name')
      AND json_extract(NEW.descriptor_json,'$.resource.local_contract_revision')=
        json_extract(prompt.value,'$.resource.local_contract_revision')
      AND json_extract(NEW.descriptor_json,'$.resource.prompt_contract_revision')=
        json_extract(prompt.value,'$.resource.prompt_contract_revision')
      AND json_extract(NEW.descriptor_json,'$.resource.connection_binding_id')=
        json_extract(prompt.value,'$.resource.connection_binding_id')
      AND json_extract(NEW.descriptor_json,'$.resource.connection_subject_fingerprint')=
        json_extract(prompt.value,'$.resource.connection_subject_fingerprint')
      AND json_extract(admitted.value,'$.operation')='expand'
      AND json_extract(admitted.value,'$.policy_digest')=json_extract(prompt.value,'$.policy_digest')
      AND json_extract(admitted.value,'$.resource.source_id')=json_extract(prompt.value,'$.resource.source_id')
      AND json_extract(admitted.value,'$.resource.prompt_name')=json_extract(prompt.value,'$.resource.prompt_name')
  ) THEN RAISE(ABORT,'reference MCP Prompt result snapshot mismatch') END;
END
"""

RESOLUTION_DDL = f"""
CREATE TRIGGER {RESOLUTION_TRIGGER} BEFORE INSERT ON chat_reference_resolutions
WHEN json_extract(NEW.composition_json,'$.runtime_policy')='initial-primary-mcp-prompt-results.v1'
  OR (NEW.resolution_seq=2 AND EXISTS (
    SELECT 1 FROM chat_reference_resolutions c
    WHERE c.turn_id=NEW.turn_id AND c.resolution_seq=1
      AND json_extract(c.composition_json,'$.runtime_policy')='initial-primary-mcp-prompt.v1'
  ))
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM chat_reference_resolutions c
    WHERE c.turn_id=NEW.turn_id AND c.resolution_seq=1
      AND NEW.resolution_seq=2 AND NEW.schema_version=1 AND NEW.kind='composition'
      AND NEW.idempotency_key='reference:primary:mcp-prompt-results'
      AND NEW.source_turn_id=c.source_turn_id
      AND NEW.created_generation=c.created_generation
      AND NEW.execution_id=c.execution_id
      AND json_extract(c.composition_json,'$.runtime_policy')='initial-primary-mcp-prompt.v1'
      AND json_extract(NEW.composition_json,'$.schema_version')=1
      AND json_extract(NEW.composition_json,'$.runtime_policy')='initial-primary-mcp-prompt-results.v1'
      AND json_extract(NEW.composition_json,'$.source_turn_id')=NEW.source_turn_id
      AND json_extract(NEW.composition_json,'$.execution_id')=NEW.execution_id
      AND json_extract(NEW.composition_json,'$.primary_composition_digest')=c.composition_digest
      AND json_extract(NEW.composition_json,'$.prompt_selection_digest')=
        json_extract(c.composition_json,'$.prompt_selection_digest')
      AND json_type(NEW.composition_json,'$.results')='array'
      AND json_array_length(NEW.composition_json,'$.results')=
        json_array_length(c.composition_json,'$.prompts')
      AND json_array_length(NEW.composition_json,'$.results') BETWEEN 1 AND 3
      AND NOT EXISTS (
        SELECT 1 FROM json_each(NEW.composition_json,'$.results') result
        WHERE NOT EXISTS (
          SELECT 1 FROM json_each(c.composition_json,'$.prompts') prompt
          JOIN chat_reference_content_snapshots snapshot
            ON snapshot.turn_id=NEW.turn_id
           AND snapshot.content_key='{CONTENT_PREFIX}' || CAST(CAST(prompt.key AS INTEGER)+1 AS TEXT)
          WHERE prompt.key=result.key
            AND json_extract(result.value,'$.node_id')=json_extract(prompt.value,'$.node_id')
            AND json_extract(result.value,'$.resource.kind')='mcp_prompt'
            AND json_extract(result.value,'$.resource.source_id')=json_extract(prompt.value,'$.resource.source_id')
            AND json_extract(result.value,'$.resource.prompt_name')=json_extract(prompt.value,'$.resource.prompt_name')
            AND json_extract(result.value,'$.resource.local_contract_revision')=
              json_extract(prompt.value,'$.resource.local_contract_revision')
            AND json_extract(result.value,'$.resource.prompt_contract_revision')=
              json_extract(prompt.value,'$.resource.prompt_contract_revision')
            AND json_extract(result.value,'$.resource.connection_binding_id')=
              json_extract(prompt.value,'$.resource.connection_binding_id')
            AND json_extract(result.value,'$.resource.connection_subject_fingerprint')=
              json_extract(prompt.value,'$.resource.connection_subject_fingerprint')
            AND json_extract(result.value,'$.content.content_key')=snapshot.content_key
            AND json_extract(result.value,'$.content.content_kind')='prompt_result'
            AND json_extract(result.value,'$.content.node_id')=json_extract(prompt.value,'$.node_id')
            AND json_extract(result.value,'$.content.content_hash')=snapshot.content_hash
            AND json_extract(result.value,'$.content.byte_count')=snapshot.byte_count
            AND json_extract(result.value,'$.content.materialization_hash')=
              json_extract(snapshot.descriptor_json,'$.materialization_hash')
            AND json_extract(result.value,'$.content.resource.source_id')=
              json_extract(prompt.value,'$.resource.source_id')
            AND json_extract(result.value,'$.content.resource.prompt_name')=
              json_extract(prompt.value,'$.resource.prompt_name')
            AND json_extract(result.value,'$.content.resource.local_contract_revision')=
              json_extract(prompt.value,'$.resource.local_contract_revision')
            AND json_extract(result.value,'$.content.resource.prompt_contract_revision')=
              json_extract(prompt.value,'$.resource.prompt_contract_revision')
            AND json_extract(result.value,'$.content.resource.connection_binding_id')=
              json_extract(prompt.value,'$.resource.connection_binding_id')
            AND json_extract(result.value,'$.content.resource.connection_subject_fingerprint')=
              json_extract(prompt.value,'$.resource.connection_subject_fingerprint')
            AND snapshot.descriptor_json=json_extract(result.value,'$.content')
            AND snapshot.created_generation=NEW.created_generation
            AND snapshot.created_policy_epoch=json_extract(c.composition_json,'$.prompt_policy_epoch')
            AND snapshot.retention_state='available' AND snapshot.ciphertext IS NOT NULL
        )
      )
  ) THEN RAISE(ABORT,'reference MCP Prompt result resolution mismatch') END;
END
"""


def _normalized(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip()).casefold()


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    names = (SNAPSHOT_TRIGGER, RESOLUTION_TRIGGER)
    automatic = db.exec_driver_sql(
        "SELECT type,sql FROM sqlite_master WHERE name=?",
        (AUTO_TRIGGER,),
    ).fetchone()
    if automatic is None or automatic[0] != "trigger" or _normalized(automatic[1]) != _normalized(PREVIOUS_AUTO_DDL):
        raise RuntimeError("reference MCP Prompt result schema drift")
    if (
        db.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE name IN (?,?)",
            names,
        ).fetchone()
        is not None
    ):
        raise RuntimeError("reference MCP Prompt result schema drift")
    db.exec_driver_sql("SAVEPOINT reference_mcp_prompt_results")
    try:
        db.exec_driver_sql(f"DROP TRIGGER {AUTO_TRIGGER}")
        db.exec_driver_sql(AUTO_DDL)
        automatic = db.exec_driver_sql(
            "SELECT type,sql FROM sqlite_master WHERE name=?",
            (AUTO_TRIGGER,),
        ).fetchone()
        if automatic is None or automatic[0] != "trigger" or _normalized(automatic[1]) != _normalized(AUTO_DDL):
            raise RuntimeError("reference MCP Prompt result schema drift")
        for name, ddl in ((SNAPSHOT_TRIGGER, SNAPSHOT_DDL), (RESOLUTION_TRIGGER, RESOLUTION_DDL)):
            db.exec_driver_sql(ddl)
            row = db.exec_driver_sql(
                "SELECT type,sql FROM sqlite_master WHERE name=?",
                (name,),
            ).fetchone()
            if row is None or row[0] != "trigger" or _normalized(row[1]) != _normalized(ddl):
                raise RuntimeError("reference MCP Prompt result schema drift")
        db.exec_driver_sql("RELEASE SAVEPOINT reference_mcp_prompt_results")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT reference_mcp_prompt_results")
        db.exec_driver_sql("RELEASE SAVEPOINT reference_mcp_prompt_results")
        raise


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore from snapshot to roll back.")
