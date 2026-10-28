"""Add owner-bound config copilot draft persistence (批 1-a).

配置副驾草稿/提议存储（tech-design-batch1-draft-store.md §2）：
- kind='lite_proposal'：轻确认条提议——终态服务端真源 + proposal_id 可验证引用。
  三个 TTL 列全 NULL：不过期、不 scrub、不 purge（R7「撤销无时限」拍板；rows 与
  turn_log 事件 payload 逐字节相同，清理无隐私收益只杀 undo）。
- kind='form_draft'：批 1-b ConfigDrawer 草稿（本 revision 只铺表，词表/TTL 届时启用）。

Revision ID: 0007_config_copilot_drafts
Revises: 0006_subagent_conversational_drafts
Create Date: 2026-08-01
"""

from __future__ import annotations

from alembic import context

revision = "0007_config_copilot_drafts"
down_revision = "0006_subagent_conversational_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 仅 core（bowhead.db）target；其余 7 个 SQLite target 显式 no-op（版本账本不分叉）。
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS config_copilot_draft (
          id               TEXT PRIMARY KEY,
          team_id          TEXT NOT NULL,
          owner_user_id    TEXT NOT NULL,
          kind             TEXT NOT NULL
            CHECK (kind IN ('lite_proposal','form_draft')),
          form_id          TEXT NOT NULL,
          workspace_id     TEXT,
          turn_id          TEXT,
          session_id       TEXT,
          status           TEXT NOT NULL,
          payload_json     TEXT,
          row_version      INTEGER NOT NULL DEFAULT 1,
          expires_at       TEXT,
          content_scrub_at TEXT,
          purge_at         TEXT,
          applied_at       TEXT,
          reverted_at      TEXT,
          created_at       TEXT NOT NULL,
          updated_at       TEXT NOT NULL
        )
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS idx_config_copilot_draft_owner
        ON config_copilot_draft(team_id, owner_user_id, updated_at)
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS idx_config_copilot_draft_scrub
        ON config_copilot_draft(content_scrub_at)
        WHERE content_scrub_at IS NOT NULL
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS idx_config_copilot_draft_purge
        ON config_copilot_draft(purge_at)
        WHERE purge_at IS NOT NULL
        """,
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS idx_config_copilot_draft_expiry
        ON config_copilot_draft(status, expires_at)
        WHERE expires_at IS NOT NULL
        """,
    )


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
