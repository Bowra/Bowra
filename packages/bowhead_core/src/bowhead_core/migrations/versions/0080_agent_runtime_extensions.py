"""Bind published optional Skill policies to exact revisions.

Revision ID: 0080_agent_runtime_extensions
Revises: 0079_system_skill_snapshot
Create Date: 2026-09-15 02:14:50.831994

"""
from alembic import context

revision = '0080_agent_runtime_extensions'
down_revision = '0079_system_skill_snapshot'
branch_labels = None
depends_on = None

TABLE = 'team_subagent_revision_extensions'
DDL = [
    """CREATE TABLE team_subagent_revision_extensions (
      revision_ref TEXT PRIMARY KEY,
      team_id TEXT NOT NULL,
      agent_id TEXT NOT NULL,
      definition_hash TEXT NOT NULL CHECK(length(definition_hash)=71 AND definition_hash LIKE 'sha256:%'),
      extension_policy_digest TEXT NOT NULL CHECK(length(extension_policy_digest)=71 AND extension_policy_digest LIKE 'sha256:%'),
      created_at TEXT NOT NULL,
      FOREIGN KEY(team_id,agent_id,revision_ref) REFERENCES team_subagent_revision(team_id,subagent_id,id) ON DELETE CASCADE
    )""",
    """CREATE TRIGGER trg_agent_extensions_revision_insert BEFORE INSERT ON team_subagent_revision_extensions
    WHEN NOT EXISTS(SELECT 1 FROM team_subagent_revision r
      WHERE r.id=NEW.revision_ref AND r.team_id=NEW.team_id AND r.subagent_id=NEW.agent_id
        AND r.definition_hash=NEW.definition_hash
        AND json_type(r.definition_json,'$.runtime_extensions')='object'
        AND json_type(r.definition_json,'$.runtime_extensions.schema_version')='integer'
        AND json_extract(r.definition_json,'$.runtime_extensions.schema_version')=1)
    BEGIN SELECT RAISE(ABORT,'Agent runtime extension proof mismatch'); END""",
    """CREATE TRIGGER trg_agent_extensions_revision_update BEFORE UPDATE ON team_subagent_revision_extensions
    BEGIN SELECT RAISE(ABORT,'Agent runtime extension proof is immutable'); END""",
    """CREATE TRIGGER trg_agent_extensions_revision_delete BEFORE DELETE ON team_subagent_revision_extensions
    WHEN EXISTS(SELECT 1 FROM team_subagent_revision r WHERE r.id=OLD.revision_ref)
    BEGIN SELECT RAISE(ABORT,'Agent runtime extension proof is retained with its revision'); END""",
]

# Old writers cannot publish or dispatch an unproved optional permission field.
for table, suffix in (('team_subagent_release_candidate', 'candidate'), ('team_subagent_release', 'release')):
    DDL.append(f"""CREATE TRIGGER trg_agent_extensions_{suffix}_insert BEFORE INSERT ON {table}
    WHEN EXISTS(SELECT 1 FROM team_subagent_revision r WHERE r.id=NEW.revision_id
      AND json_type(r.definition_json,'$.runtime_extensions') IS NOT NULL
      AND json_type(r.definition_json,'$.runtime_extensions')!='null'
      AND NOT EXISTS(SELECT 1 FROM team_subagent_revision_extensions p
        WHERE p.revision_ref=r.id AND p.team_id=r.team_id AND p.agent_id=r.subagent_id AND p.definition_hash=r.definition_hash))
    BEGIN SELECT RAISE(ABORT,'Agent runtime extension publication proof required'); END""")
DDL.append("""CREATE TRIGGER trg_agent_extensions_execution_insert BEFORE INSERT ON turn_agent_execution
WHEN NEW.agent_source_kind='team' AND EXISTS(SELECT 1 FROM team_subagent_revision r WHERE r.id=NEW.revision_ref
  AND json_type(r.definition_json,'$.runtime_extensions') IS NOT NULL
  AND json_type(r.definition_json,'$.runtime_extensions')!='null'
  AND NOT EXISTS(SELECT 1 FROM team_subagent_revision_extensions p
    WHERE p.revision_ref=r.id AND p.team_id=r.team_id AND p.agent_id=r.subagent_id AND p.definition_hash=r.definition_hash))
BEGIN SELECT RAISE(ABORT,'Agent runtime extension execution proof required'); END""")
OBJECT_NAMES = [TABLE, 'trg_agent_extensions_revision_insert', 'trg_agent_extensions_revision_update',
    'trg_agent_extensions_revision_delete', 'trg_agent_extensions_candidate_insert', 'trg_agent_extensions_release_insert', 'trg_agent_extensions_execution_insert']


def upgrade() -> None:
    if context.config.attributes['bowhead_target'] != 'core':
        return
    db = context.get_bind()
    for name in OBJECT_NAMES:
        if db.exec_driver_sql('SELECT 1 FROM sqlite_master WHERE name=?', (name,)).fetchone():
            raise RuntimeError('Agent runtime extension schema drift')
    db.exec_driver_sql('SAVEPOINT agent_runtime_extensions_upgrade')
    try:
        for sql in DDL:
            db.exec_driver_sql(sql)
    except BaseException:
        db.exec_driver_sql('ROLLBACK TO SAVEPOINT agent_runtime_extensions_upgrade')
        db.exec_driver_sql('RELEASE SAVEPOINT agent_runtime_extensions_upgrade')
        raise
    db.exec_driver_sql('RELEASE SAVEPOINT agent_runtime_extensions_upgrade')


def downgrade() -> None:
    raise NotImplementedError('Forward-only; restore matching backup and compatible workers.')
