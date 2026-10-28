"""i18n v1 R4 follow-up · canvas section quarantine 单测.

验证：
- canvas_store.set_quarantine 写 quarantined=1
- list_sections 返的 Section 含 quarantined 字段
- list_quarantined_for_canvas 仅返 quarantined section
- 老 DB schema migration（ALTER TABLE）兼容
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiosqlite
import pytest
from bowhead_core.canvas_store import CanvasStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def store(tmp_path: Path) -> CanvasStore:
    s = CanvasStore(db_path=tmp_path / "canvas.db")
    await s.initialize()
    return s


class TestQuarantine:
    @pytest.mark.asyncio
    async def test_default_not_quarantined(self, store: CanvasStore) -> None:
        canvas = await store.ensure_canvas(
            session_id="s1",
            workspace_id="ws1",
            user_id="u1",
        )
        section = await store.append_section(
            canvas_id=canvas.id,
            kind="metric",
            payload={"value": "1.2亿元"},
        )
        assert section.quarantined is False
        assert section.quarantine_reason is None

    @pytest.mark.asyncio
    async def test_set_quarantine(self, store: CanvasStore) -> None:
        canvas = await store.ensure_canvas(
            session_id="s1",
            workspace_id="ws1",
            user_id="u1",
        )
        section = await store.append_section(
            canvas_id=canvas.id,
            kind="metric",
            payload={"value": "1.2亿元"},
        )
        ok = await store.set_quarantine(
            section_id=section.id,
            quarantined=True,
            reason="legacy off-by-N scale risk",
        )
        assert ok is True
        # Reload to verify quarantined was persisted.
        sections = await store.list_sections(canvas.id)
        assert len(sections) == 1
        assert sections[0].quarantined is True
        assert sections[0].quarantine_reason == "legacy off-by-N scale risk"

    @pytest.mark.asyncio
    async def test_unset_quarantine(self, store: CanvasStore) -> None:
        canvas = await store.ensure_canvas(
            session_id="s1",
            workspace_id="ws1",
            user_id="u1",
        )
        section = await store.append_section(
            canvas_id=canvas.id,
            kind="metric",
            payload={"value": "1.2亿元"},
        )
        await store.set_quarantine(section_id=section.id, quarantined=True, reason="x")
        # Clear quarantine.
        await store.set_quarantine(section_id=section.id, quarantined=False)
        sections = await store.list_sections(canvas.id)
        assert sections[0].quarantined is False

    @pytest.mark.asyncio
    async def test_list_quarantined_for_canvas(self, store: CanvasStore) -> None:
        canvas = await store.ensure_canvas(
            session_id="s1",
            workspace_id="ws1",
            user_id="u1",
        )
        s1 = await store.append_section(canvas_id=canvas.id, kind="m", payload={})
        s2 = await store.append_section(canvas_id=canvas.id, kind="m", payload={})
        s3 = await store.append_section(canvas_id=canvas.id, kind="m", payload={})
        # Mark s1 and s3 quarantined.
        await store.set_quarantine(section_id=s1.id, quarantined=True, reason="r1")
        await store.set_quarantine(section_id=s3.id, quarantined=True, reason="r3")

        quarantined = await store.list_quarantined_for_canvas(canvas.id)
        assert len(quarantined) == 2
        ids = {s.id for s in quarantined}
        assert s1.id in ids and s3.id in ids
        assert s2.id not in ids

    @pytest.mark.asyncio
    async def test_set_quarantine_unknown_section_returns_false(
        self,
        store: CanvasStore,
    ) -> None:
        ok = await store.set_quarantine(
            section_id="nonexistent",
            quarantined=True,
        )
        assert ok is False


class TestMigrationCompat:
    """老 DB（无 quarantined 列）通过 ALTER TABLE 升级."""

    @pytest.mark.asyncio
    async def test_alter_table_idempotent(self, tmp_path: Path) -> None:
        # Create tables with the old schema (no quarantined column).
        db_path = tmp_path / "legacy.db"
        async with aiosqlite.connect(db_path) as db:
            await db.executescript("""
                CREATE TABLE canvases (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    title TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(session_id)
                );
                CREATE TABLE canvas_sections (
                    id TEXT PRIMARY KEY,
                    canvas_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    rev INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE canvas_revisions (
                    id TEXT PRIMARY KEY,
                    canvas_id TEXT NOT NULL,
                    section_id TEXT,
                    op TEXT NOT NULL,
                    prev_payload TEXT,
                    new_payload TEXT,
                    by_turn_id TEXT,
                    created_at TEXT NOT NULL
                );
            """)
            # Existing data.
            await db.execute(
                "INSERT INTO canvases VALUES ('cv1', 's1', 'ws1', 'u1', NULL, 'active', '2025-01-01', '2025-01-01')",
            )
            await db.execute(
                "INSERT INTO canvas_sections VALUES ('sec1', 'cv1', 1, 'metric', '{\"v\": \"34.71亿元\"}', 1, '2025-01-01', '2025-01-01')",
            )
            await db.commit()

        # Trigger migration.
        store = CanvasStore(db_path=db_path)
        await store.initialize()

        # Old section remains readable; quarantined defaults to False.
        sections = await store.list_sections("cv1")
        assert len(sections) == 1
        assert sections[0].quarantined is False
        # Marking quarantine works.
        await store.set_quarantine(section_id="sec1", quarantined=True, reason="legacy")
        sections2 = await store.list_sections("cv1")
        assert sections2[0].quarantined is True
