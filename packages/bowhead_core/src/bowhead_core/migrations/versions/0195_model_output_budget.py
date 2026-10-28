"""Admit automatic output in v3 system snapshots without rewriting v1/v2."""

from alembic import context

revision = "0195_model_output_budget"
down_revision = "0194_document_model_selection"
branch_labels = None
depends_on = None

TRIGGER = "trg_system_output_budget_snapshot"
DDL = """CREATE TRIGGER trg_system_output_budget_snapshot BEFORE INSERT ON system_agent_effective_revision
WHEN json_extract(NEW.definition_json,'$.schema_version')=3 AND NOT COALESCE(
  (json_type(NEW.definition_json,'$.max_output_tokens')='null' OR
    (json_type(NEW.definition_json,'$.max_output_tokens')='integer'
     AND json_extract(NEW.definition_json,'$.max_output_tokens')>0))
  AND (
    (json_type(NEW.definition_json,'$.skill_runtime_scope') IS NULL
     AND json_type(NEW.definition_json,'$.skill_grants') IS NULL)
    OR (
      json_type(NEW.definition_json,'$.skill_runtime_scope') IN ('object','null')
      AND json_type(NEW.definition_json,'$.skill_grants') IN ('array','null')
      AND (json_type(NEW.definition_json,'$.skill_runtime_scope')='object'
           OR json_type(NEW.definition_json,'$.skill_grants')='array')
      AND (json_type(NEW.definition_json,'$.skill_runtime_scope')='null' OR (
        json_type(NEW.definition_json,'$.skill_runtime_scope.schema_version')='integer'
        AND json_extract(NEW.definition_json,'$.skill_runtime_scope.schema_version')=1
        AND json_type(NEW.definition_json,'$.skill_runtime_scope.read_names')='array'
        AND json_type(NEW.definition_json,'$.skill_runtime_scope.invoke_names')='array'))
    )),0)
BEGIN SELECT RAISE(ABORT,'system output budget snapshot is malformed'); END"""


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    if db.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name=?", (TRIGGER,)).fetchone():
        raise RuntimeError("system output budget snapshot schema drift")
    db.exec_driver_sql("SAVEPOINT system_output_budget")
    try:
        db.exec_driver_sql(DDL)
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT system_output_budget")
        db.exec_driver_sql("RELEASE SAVEPOINT system_output_budget")
        raise
    db.exec_driver_sql("RELEASE SAVEPOINT system_output_budget")


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore the matching snapshot to roll back.")
