"""Expand canonical Chat user sources with immutable reference input.

Revision ID: 0058_chat_input_document
Revises: 0057_oidc_userinfo_snapshot
Create Date: 2026-09-08
"""

from __future__ import annotations

from alembic import context

revision = "0058_chat_input_document"
down_revision = "0057_oidc_userinfo_snapshot"
branch_labels = None
depends_on = None

_TRIGGERS: tuple[tuple[str, str], ...] = (
    (
        "trg_chat_input_document_no_replace",
        """
        CREATE TRIGGER trg_chat_input_document_no_replace
        BEFORE INSERT ON chat_turn_user_sources
        WHEN EXISTS (SELECT 1 FROM chat_turn_user_sources old WHERE old.turn_id=NEW.turn_id
                     AND (old.input_document_json IS NOT NULL OR NEW.input_document_json IS NOT NULL))
        BEGIN
          SELECT RAISE(ABORT,'chat_input_document_immutable');
        END
        """,
    ),
    (
        "trg_chat_input_document_insert_guard",
        """
        CREATE TRIGGER trg_chat_input_document_insert_guard
        BEFORE INSERT ON chat_turn_user_sources
        WHEN NEW.input_document_json IS NOT NULL AND CASE
          WHEN typeof(NEW.input_document_json) != 'text'
            OR length(CAST(NEW.input_document_json AS BLOB)) > 262144
            OR json_valid(NEW.input_document_json) = 0 THEN 1
          ELSE json_type(NEW.input_document_json) IS NOT 'object'
            OR (SELECT count(*) FROM json_each(NEW.input_document_json)) != 2
            OR EXISTS (SELECT 1 FROM json_each(NEW.input_document_json)
                       WHERE key NOT IN ('schema_version','nodes'))
            OR json_type(NEW.input_document_json,'$.schema_version') IS NOT 'integer'
            OR json_extract(NEW.input_document_json,'$.schema_version') != 1
            OR json_type(NEW.input_document_json,'$.nodes') IS NOT 'array'
            OR json_array_length(NEW.input_document_json,'$.nodes') > 512
        END
        BEGIN
          SELECT RAISE(ABORT,'chat_input_document_invalid');
        END
        """,
    ),
    (
        "trg_chat_input_document_immutable",
        """
        CREATE TRIGGER trg_chat_input_document_immutable
        BEFORE UPDATE ON chat_turn_user_sources
        WHEN OLD.input_document_json IS NOT NEW.input_document_json
          OR (OLD.input_document_json IS NOT NULL AND (
            OLD.turn_id IS NOT NEW.turn_id OR OLD.session_id IS NOT NEW.session_id
            OR OLD.user_id IS NOT NEW.user_id OR OLD.workspace_id IS NOT NEW.workspace_id
            OR OLD.user_text IS NOT NEW.user_text
            OR OLD.attachment_ids_json IS NOT NEW.attachment_ids_json
            OR OLD.attachment_refs_json IS NOT NEW.attachment_refs_json
            OR OLD.parts_json IS NOT NEW.parts_json OR OLD.created_at IS NOT NEW.created_at
          ))
        BEGIN
          SELECT RAISE(ABORT,'chat_input_document_immutable');
        END
        """,
    ),
)


def _normalized_sql(value: str) -> str:
    return "".join(value.lower().split())


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    columns = {str(row[1]): row for row in connection.exec_driver_sql("PRAGMA table_info(chat_turn_user_sources)").fetchall()}
    if not columns:
        raise RuntimeError("Chat user sources table is missing")
    column = columns.get("input_document_json")
    if column is None:
        connection.exec_driver_sql("ALTER TABLE chat_turn_user_sources ADD COLUMN input_document_json TEXT")
    elif str(column[2]).upper() != "TEXT" or int(column[3]) != 0 or column[4] is not None:
        raise RuntimeError("Chat input document column drift")
    for name, ddl in _TRIGGERS:
        existing = connection.exec_driver_sql("SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?", (name,)).scalar()
        if existing is None:
            connection.exec_driver_sql(ddl)
        elif _normalized_sql(str(existing)) != _normalized_sql(ddl):
            raise RuntimeError(f"Chat input document trigger drift: {name}")


def downgrade() -> None:
    raise NotImplementedError("Forward-only; retain canonical Chat input documents.")
