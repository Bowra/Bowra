"""skill-import 批1b：聊天 zip 包的清点结果，按根包 artifact 落一行。

Revision ID: 0060_bundle_inventory
Revises: 0059_feedback_candidates
Create Date: 2026-09-09

设计权威：docs/sprints/skill-import-adaptation-2026-09-08/tech-design.md §2 / §3.4。
纯加表（无 copy-migrate）；仅 core 目标，其余七个 SQLite 目标 no-op；forward-only。
根包 artifact 由 KB 包路径保留（`chat_attachment_bundle_members.bundle_artifact_id`），
清点 JSON 与它同键；批 2 的 `<bundle_import>` 块从 `attachment_refs[].file_id` 查回来。
"""

from __future__ import annotations

from alembic import context

revision = "0060_bundle_inventory"
down_revision = "0059_feedback_candidates"
branch_labels = None
depends_on = None

_DDL = """
CREATE TABLE chat_attachment_bundle_inventory (
    bundle_artifact_id TEXT PRIMARY KEY,
    created_by         TEXT NOT NULL,
    inventory_json     TEXT NOT NULL
        CHECK(typeof(inventory_json) = 'text' AND json_valid(inventory_json) = 1
              AND length(CAST(inventory_json AS BLOB)) <= 65536),
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
)
"""
_EXPECTED_COLUMNS = {"bundle_artifact_id", "created_by", "inventory_json", "created_at", "updated_at"}


def _columns(connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()}


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    existing = _columns(connection, "chat_attachment_bundle_inventory")
    if not existing:
        connection.exec_driver_sql(_DDL)
        return
    if existing != _EXPECTED_COLUMNS:
        raise RuntimeError(f"bundle inventory table drift: {sorted(existing)} != {sorted(_EXPECTED_COLUMNS)}")


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore from snapshot to roll back.")
