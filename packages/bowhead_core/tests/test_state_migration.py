"""Tests for sprint η — turns.hot_state_json incremental migration.

η-#3: SessionStore.initialize() inline-migrates `turns.hot_state_json` column
(matches existing pattern for digest_json / agent_type / skill_applied).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiosqlite
import pytest
from bowhead_core.session_store import SessionStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_hot_state_json_column_added_on_initialize(tmp_path: Path) -> None:
    """Fresh DB initialization adds hot_state_json column to turns table."""
    db = tmp_path / "test_eta_migration.db"
    store = SessionStore(db)
    await store.initialize()
    async with aiosqlite.connect(str(db)) as conn:
        cur = await conn.execute("PRAGMA table_info(turns)")
        cols = {row[1] for row in await cur.fetchall()}
    assert "hot_state_json" in cols


@pytest.mark.asyncio
async def test_hot_state_json_idempotent_on_re_initialize(tmp_path: Path) -> None:
    """Calling initialize() twice doesn't error or duplicate column."""
    db = tmp_path / "test_eta_idempotent.db"
    store = SessionStore(db)
    await store.initialize()
    await store.initialize()  # 2nd call should be no-op for hot_state_json
    async with aiosqlite.connect(str(db)) as conn:
        cur = await conn.execute("PRAGMA table_info(turns)")
        cols = [row[1] for row in await cur.fetchall()]
    # Ensure column appears exactly once
    assert cols.count("hot_state_json") == 1


@pytest.mark.asyncio
async def test_hot_state_json_nullable(tmp_path: Path) -> None:
    """Default value is NULL — old turns without state hydrate via fallback path."""
    db = tmp_path / "test_eta_null.db"
    store = SessionStore(db)
    await store.initialize()
    async with aiosqlite.connect(str(db)) as conn:
        cur = await conn.execute("PRAGMA table_info(turns)")
        rows = await cur.fetchall()
    # Find hot_state_json row; SQLite PRAGMA: (cid, name, type, notnull, dflt_value, pk)
    hot_row = next((r for r in rows if r[1] == "hot_state_json"), None)
    assert hot_row is not None
    assert hot_row[3] == 0  # notnull=0 → nullable
