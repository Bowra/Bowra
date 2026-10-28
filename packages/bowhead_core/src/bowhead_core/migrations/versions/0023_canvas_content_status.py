"""Add canvases.content_status — compose 期判定的「这一轮有没有实质产出」。

缺口：「我的报告」在积累点进去零结论的条目（116 全库实测：478 份有段落的 canvas 里
42 份有可见段却零实质内容，按月 3 → 26 → 40 在涨；用户看到的是「2026-08-25 20:30 报告」
这种以时间戳当标题的条目）。

**为什么判定必须在 compose 期、而不是报告库事后反推**（评估见
`docs/sprints/empty-report-library-2026-08-25/assessment.md`，两条都是 116 实测）：

- 误杀：`cv_b30947684740` 是完整报告（9 处引注、营业额 -5.2%/利润 -18.7%/客流 +3.8%、
  正文内嵌 markdown 表格、`[figure:]`、`## 建议`），却一个 `chart`/`table`/`recommendations`
  **段**都没有——散文写手把这些全写进 narrative 里了。按 section 种类判会把它藏掉，
  且这一类正在变多（plan-report-assembly 批 3 把子 agent 推向亲笔散文路）。
- 漏杀：`cv_7254cde96c3e` 标题逐字「2026年Q1综合经营分析报告 — 数据不足，无法编制」，
  却带着 `kpi_row` + 3 张 `insight_card` + `recommendations`。

也不能复用管线既有的失败信号：那 42 份里 37 份是 `status=completed` + `error_kind=(none)`
（BI 正常返回，只是零行）。「没有结论」是内容判断，不是错误状态。

本 revision 只做 expand（加列）。判据见 `canvas_content_status.assess_canvas_content`
（精度优先，拿不准返 None ⇒ 不写）；写入方见 `report_writer_loop.compose_report` 收尾。
**不做历史回填**——存量行留 NULL，语义是「未判定」，与「判过且为空」不同义。
**本批不改读侧行为**：报告库照旧显示，先记录、后使用。

与 `canvas_store.initialize()` 的 `_ensure_column` **双路维护**，两边都幂等
（同 0021 artifacts / 0011 plan_steps 先例）。

★2026-08-26 订正（review §5.10 R-5）：本段原本写着「`canvases` 表由 store 创建而非
baseline，故空库场景下 store 的 initialize() 会带着新列一次建好，本 revision 即 no-op」
——**错的**。`canvases` **在 1.0.0 baseline 里**（解 `baseline_schema.INITIAL_SCHEMA_B85`
可见），纯 alembic 建的空库跑完 `upgrade head` 后 `canvases` 已带 `content_status`，
本 revision **不是** no-op。下面那个「表不存在就跳过」的探测**仍然保留**（防御性、
不引入第二个建表真源），只是它的理由是「别在这里 CREATE」，不是「空库轮不到我」。

Revision ID: 0023_canvas_content_status
Revises: 0022_powerbi_service_principal
Create Date: 2026-08-25
"""

from __future__ import annotations

from alembic import context

revision = "0023_canvas_content_status"
down_revision = "0022_powerbi_service_principal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(canvases)").fetchall()}
    # 空 set = 表还不存在（全新库，store 尚未 initialize）→ no-op，别 CREATE：
    # 建表的权威是 canvas_store.SCHEMA，在这里复制一份就等于第二个真源。
    if columns and "content_status" not in columns:
        connection.exec_driver_sql("ALTER TABLE canvases ADD COLUMN content_status TEXT")


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
