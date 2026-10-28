"""批 2a UT — artifacts.origin_query（产出这份数据的那条查询原文）。

覆盖：
  - 新库：initialize() 带出 origin_query 列
  - 老库（无该列）：initialize() 用 ALTER 补上，存量行读成 None（=未记录）
  - put_bytes 落值 + 读回；不传时保持 NULL；空串/纯空白归一成 None
  - **降级不失败**：列缺失时 put_bytes 照常成功（provenance 丢了也不该断数据交付）
  - **负对照**：改动没有顺带动 BI 工具的模型面返回形状（那是批 2b 的事）

设计：docs/sprints/artifact-addressing-2026-08-19/tech-design-batch2.md §3.3
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiosqlite
import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend

if TYPE_CHECKING:
    from pathlib import Path


async def _store(tmp_path: Path, name: str = "art.db") -> ArtifactStore:
    s = ArtifactStore(
        db_path=str(tmp_path / name),
        backend=LocalBackend(root_dir=tmp_path / "blobs"),
    )
    await s.initialize()
    return s


@pytest.mark.asyncio
async def test_fresh_db_has_origin_query_column(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    async with aiosqlite.connect(s.db_path) as db:
        cur = await db.execute("PRAGMA table_info(artifacts)")
        cols = {row[1] for row in await cur.fetchall()}
    assert "origin_query" in cols


@pytest.mark.asyncio
async def test_put_bytes_persists_and_reads_back_origin(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    q = "2026年1-7月 每月 销售数量 大区经理辖区"
    art = await s.put_bytes(
        created_by=None,
        content=b"a,b\n1,2\n",
        filename="x.csv",
        mime="text/csv",
        source="bi_fetch_rows",
        origin_query=q,
    )
    assert art.origin_query == q
    got = await s.get(art.id)
    assert got is not None
    assert got.origin_query == q, "落库后必须读得回来——批 2b 要靠它渲 origin="


@pytest.mark.asyncio
async def test_origin_absent_stays_none_and_blank_normalizes(tmp_path: Path) -> None:
    """NULL = 未记录；空串/空白**不是**「查询为空」，归一成未记录。"""
    s = await _store(tmp_path)
    a1 = await s.put_bytes(created_by=None, content=b"x", filename="a.csv", mime="text/csv", source="upload")
    a2 = await s.put_bytes(created_by=None, content=b"x", filename="b.csv", mime="text/csv", source="upload", origin_query="   ")
    assert (await s.get(a1.id)).origin_query is None  # type: ignore[union-attr]
    assert (await s.get(a2.id)).origin_query is None  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_legacy_db_upgrades_via_alter_and_old_rows_read_none(tmp_path: Path) -> None:
    """老库没这列 → initialize() ALTER 补上；存量行读成 None，不炸。"""
    db_path = tmp_path / "legacy.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE artifacts (
                id TEXT PRIMARY KEY, filename TEXT, mime TEXT, size INTEGER,
                source TEXT, path TEXT, canvas_id TEXT, created_by TEXT,
                created_at TEXT
            )
        """)
        await db.execute(
            "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("art_legacy00001", "old.csv", "text/csv", 5, "bi_fetch_rows", "/tmp/old", None, "u1", "2026-01-01T00:00:00+00:00"),
        )
        await db.commit()

    s = ArtifactStore(db_path=str(db_path), backend=LocalBackend(root_dir=tmp_path / "blobs"))
    await s.initialize()

    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("PRAGMA table_info(artifacts)")
        assert "origin_query" in {row[1] for row in await cur.fetchall()}
    old = await s.get("art_legacy00001")
    assert old is not None
    assert old.origin_query is None, "存量行 = 未记录，不能伪造成空查询"


@pytest.mark.asyncio
async def test_missing_column_degrades_without_failing_the_write(tmp_path: Path) -> None:
    """列缺失（没跑迁移的降级 box）：provenance 丢掉，但数据交付**不能**失败。

    负对照式的设计取向 —— 与 derived_from 的既有降级一致：宁可少一条溯源，
    也不能因为溯源写不进去而让整次 bi 取数报错。
    """
    db_path = tmp_path / "nocol.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE artifacts (
                id TEXT PRIMARY KEY, filename TEXT, mime TEXT, size INTEGER,
                source TEXT, path TEXT, canvas_id TEXT, created_by TEXT,
                created_at TEXT
            )
        """)
        await db.commit()

    # Deliberately **do not** call initialize(), simulating an unmigrated database.
    # First prove the column is actually absent, or the test would pass when it exists, making the assertion ineffective.
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("PRAGMA table_info(artifacts)")
        assert "origin_query" not in {row[1] for row in await cur.fetchall()}, "前提没成立：本测试测的就是缺列那条路"

    s = ArtifactStore(db_path=str(db_path), backend=LocalBackend(root_dir=tmp_path / "blobs"))
    art = await s.put_bytes(
        created_by=None,
        content=b"x",
        filename="c.csv",
        mime="text/csv",
        source="bi_fetch_rows",
        origin_query="ONC产品线 2026年1-7月 销售数量",
    )
    assert art.id, "写入必须成功"
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("SELECT id FROM artifacts WHERE id=?", (art.id,))
        assert await cur.fetchone() is not None


def test_bi_result_shape_unchanged_negative_control() -> None:
    """负对照：批 2a **不动模型面**。

    origin 暴露给模型是批 2b。本闸钉住「2a 没顺手改返回形状」——若哪天有人在 2a 里
    提前把 origin 塞进 BI 结果，这里会红，提醒他那是下一批的判据。
    读的是源码字面量而不是跑一次真 BI：该分支要真 datarc，UT 里跑不了。
    """
    from pathlib import Path as _P

    from bowhead_core import tool_registry as _tr

    # Derive the path from the module, not repository-relative paths, which fail or read another file when cwd changes.
    src = _P(_tr.__file__).read_text(encoding="utf-8")
    marker = 'out["artifact_filename"] = art.filename'
    assert marker in src, "锚点没了：本闸失效，需重新对齐（别直接删）"
    head = src.split(marker)[0]
    tail = src[len(head) + len(marker) :][:400]
    assert 'out["origin' not in tail, "origin 进模型面返回是批 2b 的判据，不该在 2a 出现"
