"""TeamSettingsStore — team-scoped key/value settings."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:
    from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS team_settings (
    team_id      TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT,
    PRIMARY KEY (team_id, key)
);
CREATE INDEX IF NOT EXISTS idx_team_settings_team ON team_settings(team_id);
"""


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


class TeamSettingsStore:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)

    async def init(self) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def get(self, team_id: str, key: str) -> str | None:
        async with (
            aiosqlite.connect(self._path) as db,
            db.execute(
                "SELECT value FROM team_settings WHERE team_id=? AND key=?",
                (team_id, key),
            ) as cur,
        ):
            row = await cur.fetchone()
            return row[0] if row else None

    @staticmethod
    async def get_with_connection(db: aiosqlite.Connection, team_id: str, key: str) -> str | None:
        """Read a setting without opening or committing an independent transaction."""

        async with db.execute(
            "SELECT value FROM team_settings WHERE team_id=? AND key=?",
            (team_id, key),
        ) as cur:
            row = await cur.fetchone()
        return str(row[0]) if row else None

    @staticmethod
    async def set_with_connection(
        db: aiosqlite.Connection,
        team_id: str,
        key: str,
        value: str,
        updated_by: str,
        *,
        updated_at: str | None = None,
    ) -> None:
        """Write a setting inside a caller-owned transaction."""

        await db.execute(
            "INSERT INTO team_settings (team_id, key, value, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(team_id, key) DO UPDATE SET "
            "value=excluded.value, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (team_id, key, value, updated_at or _now(), updated_by),
        )

    async def set(self, team_id: str, key: str, value: str, updated_by: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT INTO team_settings (team_id, key, value, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(team_id, key) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                (team_id, key, value, _now(), updated_by),
            )
            await db.commit()

    async def compare_and_set(
        self,
        team_id: str,
        key: str,
        *,
        expected_value: str | None,
        value: str | None,
        updated_by: str,
    ) -> bool:
        """Atomically update one setting only when its stored value is unchanged."""

        async with aiosqlite.connect(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT value FROM team_settings WHERE team_id=? AND key=?",
                    (team_id, key),
                ) as cur:
                    row = await cur.fetchone()
                current = str(row[0]) if row else None
                if current != expected_value:
                    await db.rollback()
                    return False
                if value is None:
                    await db.execute("DELETE FROM team_settings WHERE team_id=? AND key=?", (team_id, key))
                else:
                    await db.execute(
                        "INSERT INTO team_settings (team_id, key, value, updated_at, updated_by) "
                        "VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT(team_id, key) DO UPDATE SET "
                        "value=excluded.value, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                        (team_id, key, value, _now(), updated_by),
                    )
                await db.commit()
                return True
            except BaseException:
                await db.rollback()
                raise

    async def delete(self, team_id: str, key: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "DELETE FROM team_settings WHERE team_id=? AND key=?",
                (team_id, key),
            )
            await db.commit()

    async def list_for_team(self, team_id: str) -> dict[str, str]:
        async with (
            aiosqlite.connect(self._path) as db,
            db.execute(
                "SELECT key, value FROM team_settings WHERE team_id=?",
                (team_id,),
            ) as cur,
        ):
            rows = await cur.fetchall()
            return dict(rows)
