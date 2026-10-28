"""Record the originating question for artifacts.

This additive migration preserves the original revision identifiers and
operations; export preparation changes only the explanatory module docstring.
"""

from __future__ import annotations

from alembic import context

revision = "0021_artifact_origin_query"
down_revision = "0020_local_oidc_userinfo_only"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    columns = {
        str(row[1])
        for row in connection.exec_driver_sql("PRAGMA table_info(artifacts)").fetchall()
    }
    # 空 set = 表还不存在（全新库，store 尚未 initialize）→ no-op，别 CREATE：
    # 建表的权威是 artifact_store.SCHEMA，在这里复制一份就等于第二个真源。
    if columns and "origin_query" not in columns:
        connection.exec_driver_sql("ALTER TABLE artifacts ADD COLUMN origin_query TEXT")


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
