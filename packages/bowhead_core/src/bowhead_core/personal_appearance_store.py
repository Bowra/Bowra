"""Atomic storage for personal appearance config, legacy projection, and undo."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from bowhead_core._db import open_shared_ctx
from bowhead_core.personal_theme_models import (
    KEY_APPEARANCE_THEME_CONFIG_V3,
    KEY_APPEARANCE_THEME_UNDO_V3,
)
from bowhead_core.theme_config import KEY_APPEARANCE_THEME_CONFIG, KEY_TEAM_THEME_POLICY

if TYPE_CHECKING:
    from pathlib import Path

    import aiosqlite

_KEYS = (
    KEY_APPEARANCE_THEME_CONFIG_V3,
    KEY_APPEARANCE_THEME_CONFIG,
    KEY_APPEARANCE_THEME_UNDO_V3,
)


class PersonalAppearanceSchemaUnavailableError(RuntimeError):
    """Raised when the shared user_settings schema is unavailable."""


@dataclass(frozen=True, slots=True)
class PersonalAppearanceRows:
    config_raw: str | None = None
    config_updated_at: str | None = None
    legacy_raw: str | None = None
    legacy_updated_at: str | None = None
    undo_raw: str | None = None
    undo_updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class PersonalAppearanceBaseGuard:
    team_id: str
    team_policy_raw: str | None
    active_revision_id: str | None
    fallback_revision_id: str | None
    selected_revision_id: str | None
    selected_revision_checksum: str


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


class PersonalAppearanceStore:
    """Owns atomic reads and compare-and-write for internal appearance rows."""

    def __init__(self, path: str | Path) -> None:
        self.db_path = str(path)

    async def initialize(self) -> None:
        """Verify the generic settings schema without creating or migrating it."""

        async with open_shared_ctx(self.db_path) as db, db.execute("PRAGMA table_info(user_settings)") as cur:
            rows = await cur.fetchall()
        columns = {str(row[1]) for row in rows}
        required = {"user_id", "key", "value", "updated_at"}
        if not required.issubset(columns):
            raise PersonalAppearanceSchemaUnavailableError("user_settings schema is unavailable")

    async def read(self, user_id: str) -> PersonalAppearanceRows:
        async with open_shared_ctx(self.db_path) as db:
            return await self._read_on_connection(db, user_id)

    async def compare_and_write(
        self,
        *,
        user_id: str,
        expected: PersonalAppearanceRows,
        config_raw: str,
        undo_raw: str | None,
        base_guard: PersonalAppearanceBaseGuard | None = None,
    ) -> bool:
        """Atomically replace V3 config and set/delete receipt if snapshot matches."""

        async with open_shared_ctx(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                current = await self._read_on_connection(db, user_id)
                if current != expected or (base_guard is not None and not await self._base_matches(db, base_guard)):
                    await db.rollback()
                    return False
                updated_at = _now()
                await self._upsert(db, user_id, KEY_APPEARANCE_THEME_CONFIG_V3, config_raw, updated_at)
                if undo_raw is None:
                    await db.execute(
                        "DELETE FROM user_settings WHERE user_id=? AND key=?",
                        (user_id, KEY_APPEARANCE_THEME_UNDO_V3),
                    )
                else:
                    await self._upsert(db, user_id, KEY_APPEARANCE_THEME_UNDO_V3, undo_raw, updated_at)
                await db.commit()
                return True
            except BaseException:
                await db.rollback()
                raise

    @staticmethod
    async def _base_matches(
        db: aiosqlite.Connection,
        guard: PersonalAppearanceBaseGuard,
    ) -> bool:
        async with db.execute(
            "SELECT value FROM team_settings WHERE team_id=? AND key=?",
            (guard.team_id, KEY_TEAM_THEME_POLICY),
        ) as cur:
            policy_row = await cur.fetchone()
        policy_raw = str(policy_row[0]) if policy_row else None
        if policy_raw != guard.team_policy_raw:
            return False

        async with db.execute(
            "SELECT active_revision_id, fallback_revision_id FROM team_branding_state WHERE team_id=?",
            (guard.team_id,),
        ) as cur:
            state_row = await cur.fetchone()
        active_revision_id = str(state_row[0]) if state_row else None
        fallback_revision_id = str(state_row[1]) if state_row else None
        if active_revision_id != guard.active_revision_id or fallback_revision_id != guard.fallback_revision_id:
            return False
        if guard.selected_revision_id is None:
            return True
        async with db.execute(
            "SELECT checksum FROM team_branding_revisions WHERE team_id=? AND id=?",
            (guard.team_id, guard.selected_revision_id),
        ) as cur:
            revision_row = await cur.fetchone()
        return revision_row is not None and str(revision_row[0]) == guard.selected_revision_checksum

    async def _read_on_connection(
        self,
        db: aiosqlite.Connection,
        user_id: str,
    ) -> PersonalAppearanceRows:
        async with db.execute(
            "SELECT key, value, updated_at FROM user_settings WHERE user_id=? AND key IN (?, ?, ?)",
            (user_id, *_KEYS),
        ) as cur:
            rows = await cur.fetchall()
        values = {str(key): (str(value), str(updated_at)) for key, value, updated_at in rows}
        config = values.get(KEY_APPEARANCE_THEME_CONFIG_V3, (None, None))
        legacy = values.get(KEY_APPEARANCE_THEME_CONFIG, (None, None))
        undo = values.get(KEY_APPEARANCE_THEME_UNDO_V3, (None, None))
        return PersonalAppearanceRows(
            config_raw=config[0],
            config_updated_at=config[1],
            legacy_raw=legacy[0],
            legacy_updated_at=legacy[1],
            undo_raw=undo[0],
            undo_updated_at=undo[1],
        )

    @staticmethod
    async def _upsert(
        db: aiosqlite.Connection,
        user_id: str,
        key: str,
        value: str,
        updated_at: str,
    ) -> None:
        await db.execute(
            "INSERT INTO user_settings (user_id, key, value, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET "
            "value=excluded.value, updated_at=excluded.updated_at",
            (user_id, key, value, updated_at),
        )


__all__ = [
    "PersonalAppearanceRows",
    "PersonalAppearanceBaseGuard",
    "PersonalAppearanceSchemaUnavailableError",
    "PersonalAppearanceStore",
]
