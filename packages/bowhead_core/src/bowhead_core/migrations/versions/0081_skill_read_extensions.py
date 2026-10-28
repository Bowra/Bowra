"""Retain read-only extensions and current policy invocation evidence.

Revision ID: 0081_skill_read_extensions
Revises: 0080_agent_runtime_extensions
Create Date: 2026-09-15 03:36:04.648981

"""
from __future__ import annotations

revision: str = '0081_skill_read_extensions'
down_revision = '0080_agent_runtime_extensions'
branch_labels = None
depends_on = None

import re
from importlib import import_module

from alembic import context

BASE = import_module('bowhead_core.migrations.versions.0072_skill_baseline_composition')
CONTINUATION = import_module('bowhead_core.migrations.versions.0078_reference_bi_continuation')
TRIGGER = 'trg_reference_skill_extension_composition'
TABLE = 'chat_reference_extension_checks'
POLICY = 'initial-primary-skill-extension.v1'
CONTEXT_TRIGGER = BASE.CONTEXT_TRIGGER
PREVIOUS_CONTEXT = CONTINUATION.REPLACEMENTS[CONTEXT_TRIGGER][1]
PREVIOUS_BI = next(sql for sql in CONTINUATION.DDL if 'CREATE TRIGGER trg_reference_bi_continuation_scope ' in sql)
REPLACEMENTS = {
    CONTEXT_TRIGGER: (PREVIOUS_CONTEXT, PREVIOUS_CONTEXT.replace("'initial-primary-baseline.v1'", "'initial-primary-baseline.v1','" + POLICY + "'")),
    'trg_reference_bi_continuation_scope': (PREVIOUS_BI, PREVIOUS_BI.replace("'initial-primary-baseline.v1'", "'initial-primary-baseline.v1','" + POLICY + "'")),
}
COMPOSITION_DDL = (BASE.DDL.replace(BASE.TRIGGER, TRIGGER)
    .replace('initial-primary-skill-read.v2', POLICY)
    .replace('reference:primary:skill-baseline', 'reference:primary:skill-extension')
    .replace("='skill_baseline'", "='skill_extension'")
    .replace("json_extract(m.value,'$.provenance')='baseline'", "json_extract(m.value,'$.provenance') IN ('baseline','readonly_extension')")
    .replace(" AND NEW.resolution_seq=1", """
 AND e.agent_source_kind='team'
 AND EXISTS (SELECT 1 FROM team_subagent_revision_extensions x WHERE x.team_id=e.team_id AND x.agent_id=e.agent_id
   AND x.revision_ref=e.revision_ref AND x.definition_hash=json_extract(NEW.composition_json,'$.baseline.source_definition_hash')
   AND x.extension_policy_digest=json_extract(NEW.composition_json,'$.extension_policy_digest'))
 AND length(json_extract(NEW.composition_json,'$.extension_digest'))=71
 AND EXISTS (SELECT 1 FROM json_each(NEW.composition_json,'$.members') m WHERE json_extract(m.value,'$.provenance')='readonly_extension')
 AND NEW.resolution_seq=1"""))
DDL = [COMPOSITION_DDL,
    """CREATE TABLE chat_reference_extension_checks (
      invocation_id TEXT PRIMARY KEY REFERENCES chat_reference_model_invocations(invocation_id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
      turn_id TEXT NOT NULL,
      context_id TEXT NOT NULL,
      execution_id TEXT NOT NULL REFERENCES turn_agent_execution(id),
      created_generation INTEGER NOT NULL CHECK(created_generation>=1),
      composition_digest TEXT NOT NULL CHECK(length(composition_digest)=71 AND composition_digest LIKE 'sha256:%'),
      policy_check_json TEXT NOT NULL CHECK(json_valid(policy_check_json) AND json_type(policy_check_json)='object' AND length(policy_check_json)<=4096),
      policy_check_digest TEXT NOT NULL CHECK(length(policy_check_digest)=71 AND policy_check_digest LIKE 'sha256:%'),
      created_at TEXT NOT NULL,
      FOREIGN KEY(turn_id,context_id) REFERENCES chat_reference_model_contexts(turn_id,context_id) ON DELETE CASCADE
    )""",
    """CREATE TRIGGER trg_reference_extension_check_scope BEFORE INSERT ON chat_reference_extension_checks
    WHEN NOT EXISTS(SELECT 1 FROM chat_reference_model_contexts c
      JOIN chat_reference_resolutions r ON r.turn_id=c.turn_id AND r.resolution_seq=c.resolution_seq
      JOIN turn_agent_execution e ON e.id=c.execution_id
      JOIN team_subagent a ON a.team_id=e.team_id AND a.id=e.agent_id
      JOIN team_subagent_release l ON l.id=a.live_release_id AND l.team_id=a.team_id AND l.subagent_id=a.id AND l.revision_id=a.live_revision_id
      JOIN team_subagent_revision_extensions x ON x.revision_ref=l.revision_id AND x.team_id=a.team_id AND x.agent_id=a.id
      WHERE c.turn_id=NEW.turn_id AND c.context_id=NEW.context_id AND c.execution_id=NEW.execution_id
        AND c.created_generation=NEW.created_generation AND c.composition_digest=NEW.composition_digest
        AND c.retention_state='available' AND e.status='running'
        AND json_extract(r.composition_json,'$.runtime_policy')='initial-primary-skill-extension.v1'
        AND json_extract(NEW.policy_check_json,'$.schema_version')=1 AND json_type(NEW.policy_check_json,'$.schema_version')='integer'
        AND json_extract(NEW.policy_check_json,'$.receiver_digest')=json_extract(r.composition_json,'$.receiver_digest')
        AND json_extract(NEW.policy_check_json,'$.original_extension_digest')=json_extract(r.composition_json,'$.extension_digest')
        AND json_extract(NEW.policy_check_json,'$.current_revision_ref')=l.revision_id
        AND json_extract(NEW.policy_check_json,'$.current_release_id')=l.id
        AND json_extract(NEW.policy_check_json,'$.current_definition_hash')=x.definition_hash
        AND json_extract(NEW.policy_check_json,'$.current_extension_policy_digest')=x.extension_policy_digest)
    BEGIN SELECT RAISE(ABORT,'reference extension policy evidence mismatch'); END""",
    """CREATE TRIGGER trg_reference_extension_check_no_replace BEFORE INSERT ON chat_reference_extension_checks
    WHEN EXISTS(SELECT 1 FROM chat_reference_extension_checks WHERE invocation_id=NEW.invocation_id)
    BEGIN SELECT RAISE(ABORT,'reference extension policy evidence immutable'); END""",
    """CREATE TRIGGER trg_reference_extension_check_immutable BEFORE UPDATE ON chat_reference_extension_checks
    BEGIN SELECT RAISE(ABORT,'reference extension policy evidence immutable'); END""",
    """CREATE TRIGGER trg_reference_extension_check_no_delete BEFORE DELETE ON chat_reference_extension_checks
    WHEN EXISTS(SELECT 1 FROM chat_reference_model_invocations WHERE invocation_id=OLD.invocation_id)
    BEGIN SELECT RAISE(ABORT,'reference extension policy evidence follows invocation'); END""",
    """CREATE TRIGGER trg_reference_extension_invocation_proof BEFORE INSERT ON chat_reference_model_invocations
    WHEN EXISTS(SELECT 1 FROM chat_reference_resolutions r WHERE r.turn_id=NEW.turn_id AND r.execution_id=NEW.execution_id
      AND json_extract(r.composition_json,'$.runtime_policy')='initial-primary-skill-extension.v1'
      AND NOT EXISTS(SELECT 1 FROM chat_reference_extension_checks x WHERE x.invocation_id=NEW.invocation_id AND x.turn_id=NEW.turn_id
        AND x.context_id=NEW.context_id AND x.execution_id=NEW.execution_id AND x.created_generation=NEW.created_generation
        AND x.composition_digest=r.composition_digest))
    BEGIN SELECT RAISE(ABORT,'reference extension current policy proof required'); END""",
]
OBJECT_NAMES = [re.search(r'CREATE (?:TABLE|TRIGGER) (\w+)', sql).group(1) for sql in DDL]


def upgrade() -> None:
    if context.config.attributes['bowhead_target'] != 'core':
        return
    db = context.get_bind()
    for name in OBJECT_NAMES:
        if db.exec_driver_sql('SELECT 1 FROM sqlite_master WHERE name=?', (name,)).fetchone():
            raise RuntimeError('reference skill extension schema drift')
    for name, (previous, _) in REPLACEMENTS.items():
        row = db.exec_driver_sql('SELECT type,sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if row is None or row[0]!='trigger' or BASE._normalized(row[1])!=BASE._normalized(previous):
            raise RuntimeError('reference skill extension schema drift')
    db.exec_driver_sql('SAVEPOINT skill_extensions_upgrade')
    try:
        for sql in DDL:
            db.exec_driver_sql(sql)
        for name, (_, replacement) in REPLACEMENTS.items():
            db.exec_driver_sql(f'DROP TRIGGER {name}')
            db.exec_driver_sql(replacement)
    except BaseException:
        db.exec_driver_sql('ROLLBACK TO SAVEPOINT skill_extensions_upgrade')
        db.exec_driver_sql('RELEASE SAVEPOINT skill_extensions_upgrade')
        raise
    db.exec_driver_sql('RELEASE SAVEPOINT skill_extensions_upgrade')


def downgrade() -> None:
    raise NotImplementedError('Forward-only; restore matching backup and compatible workers.')
