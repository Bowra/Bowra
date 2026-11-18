"""Retain the exact Skills a user selected for an ordinary Lead Turn.

Revision ID: 0229_turn_selected_skills
Revises: 0228_artifact_versions

docs/sprints/skill-invocation-2026-11-17/tech-design.md §10.2. A Skill-only `/`
send becomes an ordinary Lead Turn whose canonical source keeps the selected
`SkillResourceRef` values (1 to 3, exact version). The nullable column lives
with the immutable Chat source in the execution file; reference Turns, which
keep their own `input_document_json`, and document revisions cannot carry it.
"""

from alembic import context

revision = "0229_turn_selected_skills"
down_revision = "0228_artifact_versions"
branch_labels = None
depends_on = None

_DDL = (
    "ALTER TABLE chat_turn_user_sources ADD COLUMN selected_skills_json TEXT",
    """CREATE TRIGGER trg_chat_selected_skills_no_replace BEFORE INSERT ON chat_turn_user_sources
       WHEN EXISTS(SELECT 1 FROM chat_turn_user_sources old WHERE old.turn_id=NEW.turn_id
          AND (old.selected_skills_json IS NOT NULL OR NEW.selected_skills_json IS NOT NULL))
       BEGIN SELECT RAISE(ABORT,'chat_selected_skills_immutable'); END""",
    """CREATE TRIGGER trg_chat_selected_skills_insert BEFORE INSERT ON chat_turn_user_sources
       WHEN NEW.selected_skills_json IS NOT NULL AND CASE
         WHEN typeof(NEW.selected_skills_json)!='text'
           OR length(CAST(NEW.selected_skills_json AS BLOB))>2048
           OR NOT json_valid(NEW.selected_skills_json) THEN 1
         ELSE json_type(NEW.selected_skills_json) IS NOT 'array'
           OR json_array_length(NEW.selected_skills_json) NOT BETWEEN 1 AND 3
           OR NEW.input_document_json IS NOT NULL
           OR NEW.document_revision_target_json IS NOT NULL
           OR EXISTS(SELECT 1 FROM json_each(NEW.selected_skills_json) item WHERE
                item.type IS NOT 'object'
                OR (SELECT count(*) FROM json_each(item.value))!=4
                OR EXISTS(SELECT 1 FROM json_each(item.value) WHERE key NOT IN ('kind','source_kind','id','version'))
                OR json_extract(item.value,'$.kind') IS NOT 'skill'
                OR json_extract(item.value,'$.source_kind') NOT IN ('builtin','team','user')
                OR json_type(item.value,'$.id') IS NOT 'text'
                OR length(json_extract(item.value,'$.id')) NOT BETWEEN 1 AND 512
                OR json_type(item.value,'$.version') IS NOT 'text'
                OR json_extract(item.value,'$.version') NOT GLOB 'sha256:*'
                OR length(json_extract(item.value,'$.version'))!=71)
         END
       BEGIN SELECT RAISE(ABORT,'chat_selected_skills_invalid'); END""",
    """CREATE TRIGGER trg_chat_selected_skills_immutable BEFORE UPDATE ON chat_turn_user_sources
       WHEN OLD.selected_skills_json IS NOT NEW.selected_skills_json
       BEGIN SELECT RAISE(ABORT,'chat_selected_skills_immutable'); END""",
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "execution":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT turn_selected_skills")
    try:
        for statement in _DDL:
            db.exec_driver_sql(statement)
        db.exec_driver_sql("RELEASE turn_selected_skills")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO turn_selected_skills")
        db.exec_driver_sql("RELEASE turn_selected_skills")
        raise


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
