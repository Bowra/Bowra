"""Reset-recovery rows link a still-existing memory to Settings > Memory with `?item=<id>`."""

from __future__ import annotations

import aiosqlite
import anyio
from bowhead_core.user_memory import _governance_target


async def _prepare(db: aiosqlite.Connection) -> None:
    await db.execute("CREATE TABLE memory_entries (id TEXT, subject_type TEXT, subject_id TEXT, deleted_at TEXT)")
    await db.execute("CREATE TABLE memory_notes (id TEXT, subject_type TEXT, subject_id TEXT, deleted_at TEXT)")
    await db.execute("INSERT INTO memory_entries VALUES ('mem_live', 'user', 'u1', NULL)")
    await db.execute("INSERT INTO memory_entries VALUES ('mem_gone', 'user', 'u1', '2026-10-01')")
    await db.execute("INSERT INTO memory_notes VALUES ('note_1', 'user', 'u1', NULL)")


def test_governance_target_points_at_settings_memory_with_item() -> None:
    async def scenario() -> None:
        async with aiosqlite.connect(":memory:") as db:
            await _prepare(db)
            for resource_type, resource_id in (
                ("memory_entry", "mem_live"),
                ("memory_note", "note_1"),
            ):
                target = await _governance_target(db, user_id="u1", resource_type=resource_type, resource_id=resource_id)
                assert target == f"/settings/memory/?item={resource_id}"

    anyio.run(scenario)


def test_governance_target_is_absent_for_deleted_or_foreign_items() -> None:
    async def scenario() -> None:
        async with aiosqlite.connect(":memory:") as db:
            await _prepare(db)
            assert await _governance_target(db, user_id="u1", resource_type="memory_entry", resource_id="mem_gone") is None
            assert await _governance_target(db, user_id="u2", resource_type="memory_entry", resource_id="mem_live") is None
            assert await _governance_target(db, user_id="u1", resource_type="unknown", resource_id="mem_live") is None

    anyio.run(scenario)
