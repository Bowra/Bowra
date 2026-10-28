"""WorkspaceSettingsStore — 持久化 workspace 级 settings (key/value).

i18n v1 用途:
- default_ui_language: workspace 范围 UI 语言兜底（用户未设时）
- default_data_language: workspace 范围 DB 实际存储内容语言（admin 一次性设置，BI 查询拼语言提示用）
- week_start_day: BI 时间分组的"周"起点（"monday" | "sunday"）
- i18n_v1: workspace 级 feature flag（v1 灰度开关，默认 false）

设计:
- 镜像 user_settings_store 的 KV 形态：(workspace_id, key, value, updated_at)
- value 是 str (复杂结构序列化为 JSON)
- 跟 user_settings 共享同一 SQLite DB
- 连接走 open_shared_ctx（配置副驾批 1-a 顺手修：裸 aiosqlite.connect 无 busy 等待，
  活跃会话 WAL 写锁下并发写会立刻 SQLITE_BUSY——batch1 review P2-2）
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from bowhead_core._db import open_shared_ctx

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_settings (
    workspace_id TEXT NOT NULL,
    key          TEXT NOT NULL,
    value        TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (workspace_id, key)
);
CREATE INDEX IF NOT EXISTS idx_ws_settings_workspace ON workspace_settings(workspace_id);
"""


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


class WorkspaceSettingsStore:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)

    async def init(self) -> None:
        async with open_shared_ctx(self._path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def get(self, workspace_id: str, key: str) -> str | None:
        async with (
            open_shared_ctx(self._path) as db,
            db.execute(
                "SELECT value FROM workspace_settings WHERE workspace_id=? AND key=?",
                (workspace_id, key),
            ) as cur,
        ):
            row = await cur.fetchone()
            return row[0] if row else None

    async def set(self, workspace_id: str, key: str, value: str) -> None:
        async with open_shared_ctx(self._path) as db:
            await db.execute(
                "INSERT INTO workspace_settings (workspace_id, key, value, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(workspace_id, key) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at",
                (workspace_id, key, value, _now()),
            )
            await db.commit()

    async def delete(self, workspace_id: str, key: str) -> None:
        async with open_shared_ctx(self._path) as db:
            await db.execute(
                "DELETE FROM workspace_settings WHERE workspace_id=? AND key=?",
                (workspace_id, key),
            )
            await db.commit()

    async def compare_and_apply(self, workspace_id: str, changes: dict[str, tuple[str | None, str | None]]) -> dict[str, str | None] | None:
        """Apply a field batch atomically, preserving changes made after proposal.

        Values already at the desired state are safe to retry after a domain
        commit whose proposal-status update failed. Any other mismatch rejects
        the entire batch. Missing rows remain distinct from explicit defaults.
        """
        async with open_shared_ctx(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            before: dict[str, str | None] = {}
            for key, (expected, desired) in changes.items():
                async with db.execute("SELECT value FROM workspace_settings WHERE workspace_id=? AND key=?", (workspace_id, key)) as cur:
                    row = await cur.fetchone()
                current = row[0] if row else None
                if current != expected and current != desired:
                    await db.rollback()
                    return None
                before[key] = current
            for key, (_, desired) in changes.items():
                if before[key] == desired:
                    continue
                if desired is None:
                    await db.execute("DELETE FROM workspace_settings WHERE workspace_id=? AND key=?", (workspace_id, key))
                else:
                    await db.execute(
                        "INSERT INTO workspace_settings (workspace_id, key, value, updated_at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(workspace_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                        (workspace_id, key, desired, _now()),
                    )
            await db.commit()
            return before

    async def list_for_workspace(self, workspace_id: str) -> dict[str, str]:
        async with (
            open_shared_ctx(self._path) as db,
            db.execute(
                "SELECT key, value FROM workspace_settings WHERE workspace_id=?",
                (workspace_id,),
            ) as cur,
        ):
            rows = await cur.fetchall()
            return dict(rows)


# Known keys (avoid scattered magic strings).
KEY_DEFAULT_UI_LANGUAGE = "default_ui_language"
KEY_DEFAULT_DATA_LANGUAGE = "default_data_language"
KEY_WEEK_START_DAY = "week_start_day"
KEY_I18N_V1_ENABLED = "i18n_v1"
