"""0023_canvas_content_status 的最小迁移测试（CLAUDE.md：实现/迁移测试/文档同一提交）。

两件事要证：
1. **已部署的老库**（`canvases` 已存在、无该列）跑 `upgrade head` 后长出 `content_status`，
   且**存量行留 NULL**（语义＝未判定，与「判过且为空」不同义，不做历史回填）。
2. **幂等**：与 `canvas_store.initialize()` 的 `_ensure_column` 双路维护，
   两条路各跑一次、任意顺序都不炸（同 0021 artifacts / 0011 plan_steps 先例）。
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
async def test_upgrade_adds_content_status_and_leaves_existing_rows_null(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(initial_schema("core"))
        # Simulate a deployed old database: canvases exists without content_status.
        conn.execute("DROP TABLE IF EXISTS canvases")
        conn.execute(
            "CREATE TABLE canvases (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, workspace_id TEXT NOT NULL, "
            "user_id TEXT NOT NULL, title TEXT, status TEXT NOT NULL DEFAULT 'active', "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(session_id))"
        )
        conn.execute(
            "INSERT INTO canvases(id,session_id,workspace_id,user_id,title,status,created_at,updated_at) "
            "VALUES ('cv_old','s_old','w','u','旧报告','active','2026-01-01','2026-01-01')"
        )
        conn.commit()

    assert "content_status" not in _columns(db_path, "canvases")
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")

    assert "content_status" in _columns(db_path, "canvases")
    with sqlite3.connect(db_path) as conn:
        # Existing rows remain NULL; no historical backfill. Undecided differs from judged empty.
        assert conn.execute("SELECT content_status FROM canvases WHERE id='cv_old'").fetchone() == (None,)
        # Assert migration-chain head, without hardcoding its number or every new migration would fail this test.
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (_chain_head(),)


@pytest.mark.asyncio
async def test_migration_and_store_ensure_column_are_both_idempotent(tmp_path) -> None:
    """双路维护的幂等性：store 先建、迁移再跑；以及各自重复跑，都不许炸。"""
    db_path = tmp_path / "bowhead.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(initial_schema("core"))
        conn.commit()

    # Store path creates the column first.
    store = CanvasStore(db_path)
    await store.initialize()
    await store.initialize()  # Repeated initialize is idempotent
    assert "content_status" in _columns(db_path, "canvases")

    # Migration path runs afterward; an existing column must be a no-op, not an error.
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    assert "content_status" in _columns(db_path, "canvases")


@pytest.mark.asyncio
async def test_set_content_status_ignores_none_and_persists_a_verdict(tmp_path) -> None:
    """`None`＝判据没把握 ⇒ **保持原值**，别把「未判定」写成结论、也别覆盖已有判断。"""
    store = CanvasStore(tmp_path / "c.db")
    await store.initialize()
    canvas = await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")

    await store.set_content_status(canvas.id, None)
    assert _read_status(tmp_path / "c.db", canvas.id) is None

    await store.set_content_status(canvas.id, "empty")
    assert _read_status(tmp_path / "c.db", canvas.id) == "empty"

    # None must not erase an existing verdict.
    await store.set_content_status(canvas.id, None)
    assert _read_status(tmp_path / "c.db", canvas.id) == "empty"


def _read_status(db_path: Path, canvas_id: str):
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT content_status FROM canvases WHERE id=?", (canvas_id,)).fetchone()
    return row[0] if row else None
