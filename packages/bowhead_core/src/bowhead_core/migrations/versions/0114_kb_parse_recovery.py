"""Track KB parse runs and heartbeats so interrupted parses converge.

知识库体验整治 FR1：kb_files 追加 ``parse_attempts``（worker 每次领取 +1，
启动自动恢复据此设上限）与 ``parse_heartbeat_at``（每次状态写入刷新，卡住判定与
启动恢复据此区分孤儿和活着的解析）。仅 files 目标；其余七库 no-op。

两列只由本 revision 创建（KbFilesStore 启动期 DDL 保持 1.0.0 形状，不再加列）。
按列存在与否守卫，重复执行幂等。存量行 parse_attempts=0、心跳为 NULL（视为早于任何启动时刻），不回填。

Revision ID: 0114_kb_parse_recovery
Revises: 0113_reference_lead_skill_only
Create Date: 2026-09-24
"""

from __future__ import annotations

from alembic import context

revision = "0114_kb_parse_recovery"
down_revision = "0113_reference_lead_skill_only"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "files":
        return
    db = context.get_bind()
    columns = {str(row[1]) for row in db.exec_driver_sql("PRAGMA table_info(kb_files)").fetchall()}
    if not columns:
        return
    db.exec_driver_sql("SAVEPOINT kb_parse_recovery")
    try:
        if "parse_attempts" not in columns:
            db.exec_driver_sql("ALTER TABLE kb_files ADD COLUMN parse_attempts INTEGER NOT NULL DEFAULT 0")
        if "parse_heartbeat_at" not in columns:
            db.exec_driver_sql("ALTER TABLE kb_files ADD COLUMN parse_heartbeat_at TEXT")
        db.exec_driver_sql("RELEASE SAVEPOINT kb_parse_recovery")
    except Exception:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT kb_parse_recovery")
        db.exec_driver_sql("RELEASE SAVEPOINT kb_parse_recovery")
        raise


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore the matching database snapshot to roll back.")
