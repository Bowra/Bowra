"""Add canvas_sections.surface_target —— 单张图自己声明「这张图去哪」。

chat-canvas-surface 候选 4（`docs/sprints/tool-rename-add-chart-2026-08/`）：
`add_chart` / `add_image` 新增 `target` 参数（chat / canvas），让 agent 在**画图那一刻**
声明这张图的去向，而不是等轮末反推。

为什么必须是**独立的列**、不能复用 `surface_rationale`（review R3）：

  1. `unhide_turn` 的翻案 SQL 只看 `hidden_for_chat_mode`，**不看理由** ⇒ 轮末翻案会把
     「报告轮里显式声明 target=chat 的那张随手图」一起翻上画布，正是新能力的反例；
  2. `mark_turn_hidden_for_chat_mode` 对该轮**所有** append 段无条件覆写
     `surface_rationale` ⇒ 就算把声明写进理由里也会被 turn 级写路径抹掉。

`surface_rationale` 自己的注释写着它是「human-readable why (audit/admin/debug)」——
把一个自称调试用的字段变成控制字段是坏味道。故新增列。

取值：`'chat'` / `'canvas'` / NULL（未声明 ⇒ 沿用轮级判定，存量行全是 NULL，
语义与今天逐字一致）。**不做历史回填**。

与 `canvas_store.initialize()` 的 `_ensure_column` **双路维护**，两边幂等
（同 0023 content_status / 0021 artifacts 先例）。

Revision ID: 0030_canvas_surface_target
Revises: 0029_agent_release_foundation
Create Date: 2026-09-02
"""

from __future__ import annotations

from alembic import context

revision = "0030_canvas_surface_target"
down_revision = "0029_agent_release_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(canvas_sections)").fetchall()}
    # 空 set = 表还不存在（全新库，store 尚未 initialize）→ no-op，别在这里 CREATE：
    # 建表的权威是 canvas_store.SCHEMA，复制一份就是第二个真源（同 0023 的理由）。
    if columns and "surface_target" not in columns:
        connection.exec_driver_sql("ALTER TABLE canvas_sections ADD COLUMN surface_target TEXT")


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
