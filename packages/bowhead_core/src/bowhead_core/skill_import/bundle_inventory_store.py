"""聊天 zip 包的清点结果落库/读回（表由 0060 建，本模块无 initialize DDL）."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import aiosqlite

MAX_INVENTORY_JSON_BYTES = 64 * 1024


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


async def save_bundle_inventory(db_path: str, *, bundle_artifact_id: str, created_by: str, inventory_json: str) -> bool:
    """UPSERT；超上界的 JSON 不落（调用方应先 serialize_report 裁边）。返回是否写入."""
    if len(inventory_json.encode("utf-8")) > MAX_INVENTORY_JSON_BYTES:
        return False
    now = _now()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO chat_attachment_bundle_inventory(bundle_artifact_id, created_by, inventory_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(bundle_artifact_id) DO UPDATE SET inventory_json=excluded.inventory_json, updated_at=excluded.updated_at
            """,
            (bundle_artifact_id, created_by, inventory_json, now, now),
        )
        await db.commit()
    return True


async def get_bundle_inventory(db_path: str, *, bundle_artifact_id: str, created_by: str | None = None) -> dict[str, Any] | None:
    """读回；给了 created_by 就按本人过滤（清点结果只给上传者看）."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT created_by, inventory_json FROM chat_attachment_bundle_inventory WHERE bundle_artifact_id=?",
            (bundle_artifact_id,),
        ) as cur:
            row = await cur.fetchone()
    if row is None or (created_by is not None and row["created_by"] != created_by):
        return None
    try:
        data = json.loads(row["inventory_json"])
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None
