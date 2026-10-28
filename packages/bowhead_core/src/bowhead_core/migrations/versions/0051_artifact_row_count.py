"""Add artifacts.row_count —— 表格类产物的行数。

缺口：行数**只在写入那一刻算得出**，而系统当时就把它扔了。`tool_registry._fetch_rows`
落 BI 产物时已经有 `len(csv_rows)`，只塞进给模型的 `_how_to_use_in_sandbox` 提示串，
不落库；此后任何人想问「这份数据多少行」都只能重新读 blob（09-02 存储切换后 89% 的
blob 已不存在，连重读都做不到）。

两个真实需求都卡在这一列上：
  · 近重复辨识 —— artifact-addressing sprint 实测「轮内近重复数据集 22% 只差行数」
    （同一 query 不同 limit），而行数不在 artifact 记录上，当时因「要再走一次迁移」搁置。
  · 取数规模的复发告警 —— 见 `_fetch_rows` 里的 `bi_fetch_rows.oversized` 一条。

本 revision 只做 expand（加列）。**不做历史回填**：存量行留 NULL，语义是「未记录」，
与 0 行不同义；且存量 blob 大半已随 s3→local 切换失效，回填也读不出来。

与 artifact_store.initialize() 的 ALTER 循环**双路维护**，两边都幂等（同 0021 先例）。
artifacts 表由 store 创建而非 baseline，故先探测表是否存在：空库场景下 store 的
initialize() 会带着新列一次建好，本 revision 即 no-op。

背景：docs/sprints/artifact-volume-2026-09-04/tech-design.md §4.3

Revision ID: 0051_artifact_row_count
Revises: 0050_agent_behavior_descriptor
Create Date: 2026-09-06
"""

from __future__ import annotations

from alembic import context

revision = "0051_artifact_row_count"
down_revision = "0050_agent_behavior_descriptor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(artifacts)").fetchall()}
    # 空 set = 表还不存在（全新库，store 尚未 initialize）→ no-op，别 CREATE：
    # 建表的权威是 artifact_store.SCHEMA，在这里复制一份就等于第二个真源。
    if columns and "row_count" not in columns:
        connection.exec_driver_sql("ALTER TABLE artifacts ADD COLUMN row_count INTEGER")


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
