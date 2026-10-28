"""Persist bounded MCP Prompt discovery metadata beside the Tool cache.

Revision ID: 0093_reference_mcp_prompt_cache
Revises: 0092_reference_follow_up_runtime

The cache contains prompts/list metadata only. Prompt expansion results remain
runtime material and are never stored in this source registry column.
"""

from alembic import context

revision = "0093_reference_mcp_prompt_cache"
down_revision = "0092_reference_follow_up_runtime"
branch_labels = None
depends_on = None

TABLE = "team_tool_source"
COLUMN = "prompts_schema_json"


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    columns = {str(row[1]): (str(row[2]).upper(), row[3], row[4]) for row in db.exec_driver_sql(f"PRAGMA table_info({TABLE})").fetchall()}
    if COLUMN in columns:
        raise RuntimeError("MCP Prompt cache schema drift")
    db.exec_driver_sql("SAVEPOINT reference_mcp_prompt_cache")
    try:
        db.exec_driver_sql(
            f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} TEXT NOT NULL DEFAULT '[]'",
        )
        actual = {
            str(row[1]): (str(row[2]).upper(), row[3], row[4])
            for row in db.exec_driver_sql(
                f"PRAGMA table_info({TABLE})",
            ).fetchall()
        }
        if actual.get(COLUMN) != ("TEXT", 1, "'[]'"):
            raise RuntimeError("MCP Prompt cache schema drift")
        db.exec_driver_sql("RELEASE SAVEPOINT reference_mcp_prompt_cache")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT reference_mcp_prompt_cache")
        db.exec_driver_sql("RELEASE SAVEPOINT reference_mcp_prompt_cache")
        raise


def downgrade() -> None:
    raise NotImplementedError(
        "Forward-only; restore the matching database snapshot to roll back.",
    )
