"""Add the artifact workspace ownership field.

This additive migration preserves the original revision identifiers and
operations; export preparation changes only the explanatory module docstring.
"""

from __future__ import annotations

from alembic import context

revision = "0096_artifact_workspace_id"
down_revision = "0095_reference_mcp_prompt_results"
branch_labels = None
depends_on = None

TABLE = "artifacts"
COLUMN = "workspace_id"
INDEX = "idx_artifacts_workspace"
# ⚠ 不能拿 revision id 当 savepoint 名：SQLite 的标识符不允许以数字开头，
# `SAVEPOINT 0096_...` 会直接 "unrecognized token"（本批实测撞到过）。
SAVEPOINT_NAME = "artifact_workspace_id"


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    columns = {str(row[1]) for row in connection.exec_driver_sql(f"PRAGMA table_info({TABLE})").fetchall()}
    # 空 set = 表不存在（非 core 目标 / 极早期库）→ no-op，别 CREATE。
    if not columns:
        return
    if COLUMN in columns:
        # 已经有列（store 的 ALTER 循环先跑过）：只补索引，保持幂等。
        connection.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS {INDEX} ON {TABLE}({COLUMN}, deleted_at, created_at DESC)",
        )
        return
    connection.exec_driver_sql(f"SAVEPOINT {SAVEPOINT_NAME}")
    try:
        connection.exec_driver_sql(f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} TEXT")
        actual = {str(row[1]): (str(row[2]).upper(), row[3], row[4]) for row in connection.exec_driver_sql(f"PRAGMA table_info({TABLE})").fetchall()}
        # 必须是 nullable、无默认值：NOT NULL DEFAULT '' 会把「推不出归属」伪造成
        # 一个真实的空间归属，批4 量出来的 NULL 占比就会恒为 0（假成功）。
        if actual.get(COLUMN) != ("TEXT", 0, None):
            raise RuntimeError("artifact workspace_id schema drift")
        connection.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS {INDEX} ON {TABLE}({COLUMN}, deleted_at, created_at DESC)",
        )
        connection.exec_driver_sql(f"RELEASE SAVEPOINT {SAVEPOINT_NAME}")
    except BaseException:
        connection.exec_driver_sql(f"ROLLBACK TO SAVEPOINT {SAVEPOINT_NAME}")
        connection.exec_driver_sql(f"RELEASE SAVEPOINT {SAVEPOINT_NAME}")
        raise


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
