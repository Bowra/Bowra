"""0029_canvas_surface_target 的最小迁移测试（CLAUDE.md：实现/迁移测试/文档同一提交）。

要证三件事：
1. **已部署的老库**（`canvas_sections` 已存在、无该列）跑 `upgrade head` 后长出
   `surface_target`，且**存量行留 NULL**——语义＝「未声明，沿用轮级判定」，
   与「声明了 canvas」不同义，不做历史回填。
2. **幂等**：与 `canvas_store.initialize()` 的 `_ensure_column` 双路维护
   （同 0023 content_status / 0021 artifacts 先例），任意顺序、重复跑都不炸。
3. 列真的可写可读（不是加了个没人用的列）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.migrations.baseline_schema import initial_schema

pytestmark = pytest.mark.unit


def _chain_head() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from bowhead_core import migrations

    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"revision 链分叉了，heads={heads}"
    return heads[0]


def _columns(db_path: Path, table: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


@pytest.mark.asyncio
async def test_upgrade_adds_surface_target_and_leaves_existing_rows_null(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    with sqlite3.connect(db_path) as conn:
        # 1.0.0 baseline canvas_sections **already lacked** surface_target
        # (observed: even hidden_for_chat_mode absent, added by ALTER after D6),
        # making it closer to deployed legacy DB than a hand-built table.
        conn.executescript(initial_schema("core"))
        # Existing rows belong to actual canvas: orphan sections violate canvas_sections→canvases FK;
        # whole-DB foreign_key_check since 0073 correctly rejects upgrade.
        conn.execute(
            "INSERT INTO canvases(id,session_id,workspace_id,user_id,created_at,updated_at) "
            "VALUES ('cv_old','s_old','ws_old','u_old','2026-01-01','2026-01-01')"
        )
        conn.execute(
            "INSERT INTO canvas_sections(id,canvas_id,seq,kind,payload,created_at,updated_at) "
            "VALUES ('sec_old','cv_old',1,'chart','{}','2026-01-01','2026-01-01')"
        )
        conn.commit()

    assert "surface_target" not in _columns(db_path, "canvas_sections")
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")

    assert "surface_target" in _columns(db_path, "canvas_sections")
    with sqlite3.connect(db_path) as conn:
        # Existing rows stay NULL: undeclared ≠ declared canvas, which would block terminal D6.
        assert conn.execute("SELECT surface_target FROM canvas_sections WHERE id='sec_old'").fetchone() == (None,)
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (_chain_head(),)


@pytest.mark.asyncio
async def test_migration_and_store_ensure_column_are_both_idempotent(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(initial_schema("core"))
        conn.commit()

    store = CanvasStore(db_path)
    await store.initialize()
    await store.initialize()
    assert "surface_target" in _columns(db_path, "canvas_sections")

    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    assert "surface_target" in _columns(db_path, "canvas_sections")


@pytest.mark.asyncio
async def test_column_round_trips(tmp_path) -> None:
    """加了列不等于列真被用上——写进去要读得出来。"""
    store = CanvasStore(tmp_path / "c.db")
    await store.initialize()
    canvas = await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    sec = await store.append_section(canvas_id=canvas.id, kind="chart", payload={"kind": "bar"}, by_turn_id="t1")

    rows = await store.list_sections_appended_by_turn("t1")
    assert next(r for r in rows if r.id == sec.id).surface_target is None, "默认未声明"

    await store.set_section_surface_target(sec.id, "chat")
    rows = await store.list_sections_appended_by_turn("t1")
    assert next(r for r in rows if r.id == sec.id).surface_target == "chat"
