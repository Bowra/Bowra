"""B 线批 4：团队技能带上「导入出处」——作者明知有阻断项仍强开的痕迹。

Revision ID: 0064_team_skill_import_provenance
Revises: 0063_chat_reference_storage
Create Date: 2026-09-09

设计权威：docs/sprints/agent-feedback-loop-2026-09-08/tech-design.md §2.9（跨线接口：
skill-import 线 interface-requests.md §1 的 ⑨）。

一份上传技能清点出阻断项后不生效，用户可以点「仍然启用」强开（`skill_instances.import_status='overridden'`）。
它之后还能 promote 到团队，而**团队审核者看到的是一份干净的候选**——看不到它当初有几条阻断项。
这一列就是把那个事实带过去。

- 只加列，不回填：存量行 NULL＝「没有导入出处」（既有的团队技能本来就不是这条路进来的）。
- 不复用 `risk_report_json`：那是平台判的风险，这是**作者的知情选择**，混在一起审批面分不清是谁的判断。
- 值由 promote 路由**服务端从实例现算**（跨 session 定案：客户端可传＝作者能传一份 red_count:0 瞒过审核）。
"""

from __future__ import annotations

from alembic import context

revision = "0064_team_skill_import_provenance"
down_revision = "0063_chat_reference_storage"
branch_labels = None
depends_on = None

_COLUMN = "import_provenance_json"


def _columns(connection, table: str) -> set[str]:
    rows = connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    existing = _columns(connection, "team_skill")
    if not existing:
        raise RuntimeError("team_skill missing: core schema must exist before 0064")
    if _COLUMN not in existing:
        connection.exec_driver_sql(f"ALTER TABLE team_skill ADD COLUMN {_COLUMN} TEXT")


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore from snapshot to roll back.")
