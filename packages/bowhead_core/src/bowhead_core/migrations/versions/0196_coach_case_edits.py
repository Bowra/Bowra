"""Register exact existing-case edits in immutable Coach suggestion JSON."""

from alembic import context

revision = "0196_coach_case_edits"
down_revision = "0195_model_output_budget"
branch_labels = None
depends_on = None

TRIGGER = "trg_coach_case_edit_scope"
DDL = """CREATE TRIGGER trg_coach_case_edit_scope BEFORE INSERT ON agent_change_proposal
WHEN EXISTS (SELECT 1 FROM json_each(NEW.suggested_cases_json) c WHERE json_type(c.value,'$.edit') IS NOT NULL)
AND NOT COALESCE(
  NEW.source_type='coach' AND NEW.base_suite_revision_id IS NOT NULL
  AND json_type(NEW.provenance_json,'$.case_edit_intent_hash')='text'
  AND length(json_extract(NEW.provenance_json,'$.case_edit_intent_hash'))=71
  AND substr(json_extract(NEW.provenance_json,'$.case_edit_intent_hash'),1,7)='sha256:'
  AND substr(json_extract(NEW.provenance_json,'$.case_edit_intent_hash'),8) NOT GLOB '*[^0-9a-f]*'
  AND NOT EXISTS (
    SELECT 1 FROM json_each(NEW.suggested_cases_json) c
    WHERE json_type(c.value,'$.edit') IS NOT NULL AND NOT COALESCE(
      json_type(c.value,'$.edit')='object'
      AND json_type(c.value,'$.edit.changed_fields')='array'
      AND json_array_length(json_extract(c.value,'$.edit.changed_fields'))>0
      AND json_type(c.value,'$.edit.before')='object'
      AND EXISTS (
        SELECT 1 FROM agent_eval_case_revision r
        WHERE r.id=json_extract(c.value,'$.edit.case_revision_id')
          AND r.suite_revision_id=NEW.base_suite_revision_id
          AND r.case_hash=json_extract(c.value,'$.edit.case_hash')
          AND r.case_key=json_extract(c.value,'$.edit.case_key')
          AND r.case_key=json_extract(c.value,'$.case_spec.case_key')
          AND COALESCE(json_extract(r.expected_contract_json,'$.held_out'),0)!=1
      ),0)
  ),0)
BEGIN SELECT RAISE(ABORT,'Coach case edit requires its original visible case and intent'); END"""


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    if db.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name=?", (TRIGGER,)).fetchone():
        raise RuntimeError("Coach case edit guard schema drift")
    db.exec_driver_sql("SAVEPOINT coach_case_edits")
    try:
        db.exec_driver_sql(DDL)
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT coach_case_edits")
        db.exec_driver_sql("RELEASE SAVEPOINT coach_case_edits")
        raise
    db.exec_driver_sql("RELEASE SAVEPOINT coach_case_edits")


def downgrade() -> None:
    raise NotImplementedError("Restore the paired database snapshot to roll back.")
