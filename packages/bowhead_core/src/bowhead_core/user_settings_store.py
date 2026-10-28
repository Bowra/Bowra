"""V2.1.5 · UserSettingsStore — 持久化用户级 settings (key/value).

当前用途:
- pptx_template_artifact_id (用户上传的 PPT 模板 art_xxx, V2.1)
- ui_language / output_language (i18n v1)

设计:
- 单表 user_settings(user_id, key, value), PK (user_id, key)
- value 是 str (复杂结构序列化为 JSON 自行解析)
- Shares the SQLite database with personal memory.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from bowhead_memory.privacy_guard import ensure_memory_write_allowed

from bowhead_core._db import open_shared_ctx

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_settings (
    user_id     TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
CREATE INDEX IF NOT EXISTS idx_settings_user ON user_settings(user_id);
"""


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


class UserSettingsStore:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)

    @property
    def db_path(self) -> str:
        return self._path

    async def init(self) -> None:
        async with open_shared_ctx(self._path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def get(self, user_id: str, key: str) -> str | None:
        value, _ = await self.get_with_updated_at(user_id, key)
        return value

    async def get_with_updated_at(
        self,
        user_id: str,
        key: str,
    ) -> tuple[str | None, str | None]:
        async with (
            open_shared_ctx(self._path) as db,
            db.execute(
                "SELECT value, updated_at FROM user_settings WHERE user_id=? AND key=?",
                (user_id, key),
            ) as cur,
        ):
            row = await cur.fetchone()
            return (str(row[0]), str(row[1])) if row else (None, None)

    async def set(self, user_id: str, key: str, value: str) -> None:
        async with open_shared_ctx(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            if key == "memory_dreaming_paused":
                await ensure_memory_write_allowed(
                    db,
                    user_id=user_id,
                )
            await db.execute(
                "INSERT INTO user_settings (user_id, key, value, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at",
                (user_id, key, value, _now()),
            )
            await db.commit()

    async def update_value(
        self,
        user_id: str,
        key: str,
        mutate: Callable[[str | None], str | None],
    ) -> tuple[str | None, str | None]:
        """BEGIN IMMEDIATE 事务内 读→mutate→写/删，返回 (before, after)。

        配置副驾批 1-a（tech-design-batch1 §6.2）：appearance 稀疏 JSON 的字段级合并
        收进单事务，根治「GET-merge-PUT」丢失更新。mutate 为纯同步函数；返回 None =
        删除该 key。mutate 抛错 → rollback（不半写）。
        """
        async with open_shared_ctx(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                if key == "memory_dreaming_paused":
                    await ensure_memory_write_allowed(db, user_id=user_id)
                async with db.execute(
                    "SELECT value FROM user_settings WHERE user_id=? AND key=?",
                    (user_id, key),
                ) as cur:
                    row = await cur.fetchone()
                before = str(row[0]) if row else None
                after = mutate(before)
                if after is None:
                    await db.execute(
                        "DELETE FROM user_settings WHERE user_id=? AND key=?",
                        (user_id, key),
                    )
                else:
                    await db.execute(
                        "INSERT INTO user_settings (user_id, key, value, updated_at) "
                        "VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(user_id, key) DO UPDATE SET "
                        "value=excluded.value, updated_at=excluded.updated_at",
                        (user_id, key, after, _now()),
                    )
                await db.commit()
                return before, after
            except BaseException:
                await db.rollback()
                raise

    async def delete(self, user_id: str, key: str) -> None:
        async with open_shared_ctx(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            if key == "memory_dreaming_paused":
                await ensure_memory_write_allowed(
                    db,
                    user_id=user_id,
                )
            await db.execute(
                "DELETE FROM user_settings WHERE user_id=? AND key=?",
                (user_id, key),
            )
            await db.commit()

    async def list_for_user(self, user_id: str) -> dict[str, str]:
        async with (
            open_shared_ctx(self._path) as db,
            db.execute(
                "SELECT key, value FROM user_settings WHERE user_id=?",
                (user_id,),
            ) as cur,
        ):
            rows = await cur.fetchall()
            return dict(rows)


# Known keys (avoid scattered magic strings).
KEY_PPTX_TEMPLATE_ARTIFACT_ID = "pptx_template_artifact_id"
KEY_UI_LANGUAGE = "ui_language"  # i18n v1: "zh-CN" | "en-US" | "auto"
KEY_OUTPUT_LANGUAGE = "output_language"  # i18n v1: "zh-CN" | "en-US" | "auto"
