"""Index session_attachments by artifact so artifact → origin session lookups stop scanning.

`session_attachments` 只有主键 ``(session_id, artifact_id)`` 与 ``(session_id, detached_at)`` 两个索引，
而「这个产物最早 pin 在哪个会话」（``ArtifactStore.origin_sessions``：文件列表的来源列、产物空间归属梯子的
pin 一级、全局搜索的文件来源）按 ``artifact_id`` 查并按 ``pinned_at`` 排序 ⇒ 每次都是全表扫描 + 临时排序。
2026-09-24 review 实测（16 万行）：单条 5.9ms → 0.04ms。

仅 core 目标；其余七库 no-op。只建索引、不改表不回填；``IF NOT EXISTS`` 幂等，缺表时 no-op。
索引只由本 revision 创建（SessionStore 启动期 DDL 保持 1.0.0 形状）。

Revision ID: 0115_session_attachments_artifact_index
Revises: 0114_kb_parse_recovery
Create Date: 2026-09-24
"""

from __future__ import annotations

from alembic import context

revision = "0115_session_attachments_artifact_index"
down_revision = "0114_kb_parse_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    if not db.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_attachments'").fetchone():
        return
    db.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_session_attachments_artifact ON session_attachments(artifact_id, pinned_at)")


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore the matching database snapshot to roll back.")
