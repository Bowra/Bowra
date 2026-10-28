"""Add per-team overrides for built-in (system manifest) subagents.

内置子代理管理 sprint（docs/sprints/builtin-subagent-admin/tech-design.md §2.1）：
内置定义仍然只跟代码走（manifests/system/*.yaml → PROFILES），本表只存「团队改了什么」。
无行 = 完全使用代码默认值；删行 = 恢复默认。NULL 列 = 该字段不覆盖（区别于「覆盖为空」，
后者对 skills 是 '[]'）。

`name` 不加外键或枚举 CHECK：内置集合随代码版本变化，写死会让下个版本删掉某个内置时旧行变成
孤儿约束冲突。孤儿行由读取侧忽略（name 不在 registry 里 → skip）。

Revision ID: 0009_builtin_subagent_override
Revises: 0008_terminal_protocol_v1
Create Date: 2026-08-04
"""

from __future__ import annotations

from alembic import context

revision = "0009_builtin_subagent_override"
down_revision = "0008_terminal_protocol_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Team governance state lives only in bowhead.db. The seven KB targets explicitly
    # no-op so every SQLite target advances on the same revision chain.
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS team_builtin_subagent_override (
          team_id         TEXT NOT NULL,
          name            TEXT NOT NULL,
          enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
          display_name    TEXT,
          description     TEXT,
          model           TEXT,
          skills_json     TEXT,
          prompt_append   TEXT,
          max_iterations  INTEGER
            CHECK (max_iterations IS NULL OR max_iterations >= 1),
          row_version     INTEGER NOT NULL DEFAULT 1
            CHECK (row_version >= 1),
          updated_by      TEXT,
          created_at      TEXT NOT NULL,
          updated_at      TEXT NOT NULL,
          PRIMARY KEY (team_id, name)
        )
        """
    )


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
