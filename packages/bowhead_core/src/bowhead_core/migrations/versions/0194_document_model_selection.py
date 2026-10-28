"""Freeze new document planners' selected models without changing old jobs."""

from alembic import context

revision = "0194_document_model_selection"
down_revision = "0193_subagent_resume_checkpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT document_model_selection")
    try:
        columns = {row[1] for row in db.exec_driver_sql("PRAGMA table_info(document_dispatches)")}
        if not columns:
            raise RuntimeError("document_dispatches is missing; upgrade through 0130 first")
        if "model_selection_json" not in columns:
            db.exec_driver_sql(
                """ALTER TABLE document_dispatches ADD COLUMN model_selection_json TEXT
                CHECK(model_selection_json IS NULL OR (
                  json_valid(model_selection_json) AND json_type(model_selection_json)='object'
                  AND coalesce(json_type(model_selection_json,'$.provider')='text'
                    AND length(json_extract(model_selection_json,'$.provider'))>0
                    AND json_type(model_selection_json,'$.model')='text'
                    AND length(json_extract(model_selection_json,'$.model'))>0,0)))"""
            )
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT document_model_selection")
        db.exec_driver_sql("RELEASE SAVEPOINT document_model_selection")
        raise
    db.exec_driver_sql("RELEASE SAVEPOINT document_model_selection")


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")
