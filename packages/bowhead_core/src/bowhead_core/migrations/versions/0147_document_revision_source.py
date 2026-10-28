"""Retain exact document revision intent with the immutable Chat source."""

from alembic import context

revision = "0147_document_revision_source"
down_revision = "0146_agent_ranking_snapshots"
branch_labels = None
depends_on = None

_DDL = (
    "ALTER TABLE chat_turn_user_sources ADD COLUMN document_revision_target_json TEXT",
    """CREATE TRIGGER trg_chat_document_revision_no_replace BEFORE INSERT ON chat_turn_user_sources
       WHEN EXISTS(SELECT 1 FROM chat_turn_user_sources old WHERE old.turn_id=NEW.turn_id
          AND (old.document_revision_target_json IS NOT NULL OR NEW.document_revision_target_json IS NOT NULL))
       BEGIN SELECT RAISE(ABORT,'chat_document_revision_immutable'); END""",
    """CREATE TRIGGER trg_chat_document_revision_insert BEFORE INSERT ON chat_turn_user_sources
       WHEN NEW.document_revision_target_json IS NOT NULL AND CASE
         WHEN typeof(NEW.document_revision_target_json)!='text'
           OR length(CAST(NEW.document_revision_target_json AS BLOB))>1024
           OR NOT json_valid(NEW.document_revision_target_json) THEN 1
         ELSE json_type(NEW.document_revision_target_json) IS NOT 'object'
           OR NEW.input_document_json IS NOT NULL
           OR (SELECT count(*) FROM json_each(NEW.document_revision_target_json))!=5
           OR EXISTS(SELECT 1 FROM json_each(NEW.document_revision_target_json)
                     WHERE key NOT IN ('kind','version','session_id','job_id','draft_digest'))
           OR json_extract(NEW.document_revision_target_json,'$.kind') IS NOT 'document_revision'
           OR json_type(NEW.document_revision_target_json,'$.version') IS NOT 'integer'
           OR json_extract(NEW.document_revision_target_json,'$.version') IS NOT 1
           OR json_type(NEW.document_revision_target_json,'$.session_id') IS NOT 'text'
           OR json_extract(NEW.document_revision_target_json,'$.session_id') IS NOT NEW.session_id
           OR NEW.session_id NOT GLOB 's_?*' OR length(NEW.session_id)>128
           OR NEW.session_id GLOB '*[^A-Za-z0-9_]*'
           OR json_type(NEW.document_revision_target_json,'$.job_id') IS NOT 'text'
           OR json_extract(NEW.document_revision_target_json,'$.job_id') NOT GLOB 'djob_?*'
           OR length(json_extract(NEW.document_revision_target_json,'$.job_id'))>128
           OR json_extract(NEW.document_revision_target_json,'$.job_id') GLOB '*[^A-Za-z0-9_]*'
           OR json_type(NEW.document_revision_target_json,'$.draft_digest') IS NOT 'text'
           OR length(json_extract(NEW.document_revision_target_json,'$.draft_digest'))!=64
           OR json_extract(NEW.document_revision_target_json,'$.draft_digest') GLOB '*[^0-9a-f]*'
         END
       BEGIN SELECT RAISE(ABORT,'chat_document_revision_invalid'); END""",
    """CREATE TRIGGER trg_chat_document_revision_immutable BEFORE UPDATE ON chat_turn_user_sources
       WHEN OLD.document_revision_target_json IS NOT NEW.document_revision_target_json
         OR (OLD.document_revision_target_json IS NOT NULL AND (
           OLD.turn_id IS NOT NEW.turn_id OR OLD.session_id IS NOT NEW.session_id
           OR OLD.user_id IS NOT NEW.user_id OR OLD.workspace_id IS NOT NEW.workspace_id
           OR OLD.user_text IS NOT NEW.user_text OR OLD.attachment_ids_json IS NOT NEW.attachment_ids_json
           OR OLD.attachment_refs_json IS NOT NEW.attachment_refs_json OR OLD.parts_json IS NOT NEW.parts_json
           OR OLD.input_document_json IS NOT NEW.input_document_json OR OLD.created_at IS NOT NEW.created_at))
       BEGIN SELECT RAISE(ABORT,'chat_document_revision_immutable'); END""",
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT document_revision_source")
    try:
        for statement in _DDL:
            db.exec_driver_sql(statement)
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT document_revision_source")
        db.exec_driver_sql("RELEASE SAVEPOINT document_revision_source")
        raise
    db.exec_driver_sql("RELEASE SAVEPOINT document_revision_source")


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")
