"""artifacts.row_count —— 加列迁移 + 写入路径 + 复发告警。

行数**只在写入那一刻算得出**：`tool_registry._fetch_rows` 落 BI 产物时手上就有
`len(csv_rows)`，此前只塞进给模型的提示串就扔了。下游想知道只能重读 blob，
而 blob 会被回收（09-02 存储切换后 89% 的存量 blob 已不存在，连重读都做不到）。
"""

from __future__ import annotations

import asyncio
import sqlite3
from importlib import import_module
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.artifact_store import ArtifactStore, LocalBackend

from bowhead_core import migrations

pytestmark = pytest.mark.integration
MIGRATION = import_module("bowhead_core.migrations.versions.0051_artifact_row_count")


def _config(path: Path, target: str = "core") -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = target
    return config


def test_upgrade_adds_column_keeps_rows_and_is_repeatable(tmp_path: Path) -> None:
    """存量库：加列、老行留 NULL（=「未记录」，与 0 行不同义）、重复执行幂等。

    注意 `artifacts` 表由 baseline 建（不是由 store 建——0021 的注释在这点上已过时），
    所以升到 down_revision 之后表已经在了，只是没有本列。
    """
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        assert "row_count" not in {r[1] for r in db.execute("PRAGMA table_info(artifacts)")}, "前置不成立：升级前就已有本列，这个测试证明不了任何事"
        db.execute(
            "INSERT INTO artifacts(id,filename,mime,size,source,path,created_at) VALUES (?,?,?,?,?,?,?)",
            ("art_legacy", "old.csv", "text/csv", 12, "bi_fetch_rows", "/tmp/old.csv", "2026-09-01"),
        )

    command.upgrade(config, "head")
    command.upgrade(config, "head")  # Idempotent: second call must not fail.
    command.stamp(config, MIGRATION.down_revision)
    # Replay only this revision, then restore the ledger to head; later historical revisions (e.g. 0063 table creation) may not be rerunnable on an upgraded database.
    command.upgrade(config, MIGRATION.revision)  # Replaying the same revision must not fail either.
    with sqlite3.connect(path) as db:
        # Verify replay reached this revision (checking after stamp head is tautological and meaningless).
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
    command.stamp(config, "head")

    with sqlite3.connect(path) as db:
        assert "row_count" in {r[1] for r in db.execute("PRAGMA table_info(artifacts)")}
        assert db.execute("SELECT filename,row_count FROM artifacts WHERE id='art_legacy'").fetchone() == ("old.csv", None)


def test_both_maintenance_paths_agree(tmp_path: Path) -> None:
    """双路维护：alembic 链和 store.initialize() 的 ALTER 循环各自都能补上本列。

    钉的是「只改一路」这个具体失败——只加迁移，store 自建的老库（dev / 测试）就没有列；
    只加 ALTER 循环，走 alembic 的生产库就没有。

    ⚠ 路 B 必须从**缺列的老表**起步。直接建新库测不到 ALTER 那一路——新库的列来自
    `SCHEMA` 的 CREATE TABLE，把 ALTER 循环整条删掉测试照样绿（突变实测过）。
    """
    # Path A: Alembic.
    via_alembic = tmp_path / "alembic.db"
    command.upgrade(_config(via_alembic), "head")
    with sqlite3.connect(via_alembic) as db:
        assert "row_count" in {r[1] for r in db.execute("PRAGMA table_info(artifacts)")}
    # Store initialize must also succeed on an already migrated database (ALTER loop must be idempotent).
    asyncio.run(ArtifactStore(db_path=str(via_alembic), backend=LocalBackend(root_dir=tmp_path / "b1")).initialize())

    # Path B: store's own legacy database (missing columns) → initialize() adds them through the ALTER loop.
    via_store = tmp_path / "store.db"
    with sqlite3.connect(via_store) as db:
        db.execute(
            "CREATE TABLE artifacts ("
            "id TEXT PRIMARY KEY, filename TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL, "
            "source TEXT NOT NULL, path TEXT NOT NULL, canvas_id TEXT, created_by TEXT, created_at TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO artifacts(id,filename,mime,size,source,path,created_at) VALUES (?,?,?,?,?,?,?)",
            ("art_old", "legacy.csv", "text/csv", 9, "bi_fetch_rows", "/tmp/legacy.csv", "2026-08-01"),
        )
        assert "row_count" not in {r[1] for r in db.execute("PRAGMA table_info(artifacts)")}

    asyncio.run(ArtifactStore(db_path=str(via_store), backend=LocalBackend(root_dir=tmp_path / "b2")).initialize())
    with sqlite3.connect(via_store) as db:
        assert "row_count" in {r[1] for r in db.execute("PRAGMA table_info(artifacts)")}
        assert db.execute("SELECT filename,row_count FROM artifacts WHERE id='art_old'").fetchone() == ("legacy.csv", None)


@pytest.mark.parametrize("target", ["files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"])
def test_non_core_target_untouched(tmp_path: Path, target: str) -> None:
    path = tmp_path / f"{target}.db"
    command.upgrade(_config(path, target), "head")
    with sqlite3.connect(path) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        assert "artifacts" not in tables, f"{target} 目标不该有 artifacts 表"


@pytest.mark.asyncio
async def test_row_count_round_trips_and_defaults_to_none(tmp_path: Path) -> None:
    """写得进、读得回；不传就是 NULL（非表格产物没有行数这回事）。"""
    store = ArtifactStore(db_path=str(tmp_path / "a.db"), backend=LocalBackend(root_dir=tmp_path / "blob"))
    await store.initialize()

    with_rows = await store.put_bytes(
        content=b"a,b\n1,2\n3,4\n",
        filename="rows.csv",
        mime="text/csv",
        source="bi_fetch_rows",
        created_by="u1",
        row_count=2,
    )
    assert (await store.get(with_rows.id)).row_count == 2

    without = await store.put_bytes(
        content=b"hi",
        filename="note.txt",
        mime="text/plain",
        source="python_sandbox",
        created_by="u1",
    )
    assert (await store.get(without.id)).row_count is None

    # 0 rows is meaningful; never discard it as an unrecorded value.
    empty = await store.put_bytes(
        content=b"a,b\n",
        filename="empty.csv",
        mime="text/csv",
        source="bi_fetch_rows",
        created_by="u1",
        row_count=0,
    )
    assert (await store.get(empty.id)).row_count == 0
