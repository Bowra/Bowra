"""切点水位戳 `artifact_workspace_schema_meta`（空间隔离批4 ②）。

★★ 本 revision **只建表、不打戳**。2026-09-17 review 纠正了原设计：仓里的同族 idiom
（`0025` 的 `assignment_cutover_complete_at`、`0006` 的 `*_semantics_activated_at`）
**都只声明列、留 NULL**，由 app / admin 命令在前置对账通过后才写。在迁移里取 `now()`
既撞 `数据库迁移.md`「revision 不读取外部服务或时间」，记的也是**错的时刻**——
「schema 升级时刻」不等于「写入点全部接线正确的时刻」（同一次 review 就查出
`kb.py` 三个写入点当时没接线）。

所以这里断言的是「**表建出来了、戳是 NULL、没有人偷偷打戳**」，
以及「读不到戳 ⇒ 判为切点前 ⇒ 逐字节今天的行为」。
"""

from __future__ import annotations

import asyncio
import sqlite3
from importlib import import_module
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.artifact_store import (
    artifact_created_after_cutover,
    read_artifact_workspace_cutover_at,
)

from bowhead_core import migrations

pytestmark = pytest.mark.integration

# ★ Use import_module rather than bare literals: `test_migration_head_pins_are_consistent` forbids
#   bare revision literals under packages/ (hardcoded copies silently become stale with the next revision).
MIGRATION = import_module("bowhead_core.migrations.versions.0099_artifact_workspace_cutover")

TABLE = MIGRATION.TABLE


def _config(path: Path, target: str = "core") -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = target
    return config


def _rows(path: Path):
    with sqlite3.connect(path) as db:
        return db.execute(f"SELECT singleton, activated_at FROM {TABLE}").fetchall()  # noqa: S608


# ───────────────────────── Migration itself ─────────────────────────


def test_upgrade_declares_the_marker_but_never_stamps_it(tmp_path: Path) -> None:
    """★★ 建表 + 单例行，`activated_at` 必须是 **NULL**：迁移**不打戳**。

    这条是本批最容易被「优化」掉的不变量：谁要是顺手在迁移里补一句
    `strftime('now')`，水位就变成「schema 升级时刻」——而那不是判据要的时刻。
    """
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    assert TABLE not in tables, "前置不成立：升级前就有本表，这条测试证明不了任何事"

    command.upgrade(config, "head")
    assert _rows(path) == [(1, None)], "迁移不得打戳（activated_at 必须为 NULL）"

    # Column must be nullable: NOT NULL would force the migration to invent a timestamp immediately.
    with sqlite3.connect(path) as db:
        info = {r[1]: (r[2].upper(), r[3], r[4]) for r in db.execute(f"PRAGMA table_info({TABLE})")}
    assert info["activated_at"][1] == 0, "activated_at 必须 nullable"

    # Replay: stamp back to the parent, then upgrade; `upgrade()` **actually executes again**.
    command.stamp(config, MIGRATION.down_revision)
    # Replay only this revision, then restore the ledger to head: later historical revisions (0063 tables, MCP Prompt structural preflight) may not rerun on upgraded databases.
    command.upgrade(config, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        # Confirm replay actually reached this revision (checking version after stamp head would be vacuous).
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
    command.stamp(config, "head")
    assert _rows(path) == [(1, None)], "重放不得改变单例行"


def test_upgrade_does_not_clobber_an_existing_stamp(tmp_path: Path) -> None:
    """★ 已经打过戳的库，重跑迁移不得把戳抹回 NULL。

    打戳发生在**迁移之后**（由 app / admin 命令），所以「先打戳、再升级一次」
    是真实会发生的顺序——`INSERT OR IGNORE` 必须保住它。
    """
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, "head")
    with sqlite3.connect(path) as db:
        db.execute(f"UPDATE {TABLE} SET activated_at=? WHERE singleton=1", ("2026-09-17T00:00:00.000Z",))  # noqa: S608

    command.stamp(config, MIGRATION.down_revision)
    # Replay only this revision, then restore the ledger to head: later historical revisions (0063 tables, MCP Prompt structural preflight) may not rerun on upgraded databases.
    command.upgrade(config, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        # Confirm replay actually reached this revision (checking version after stamp head would be vacuous).
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
    command.stamp(config, "head")
    assert _rows(path) == [(1, "2026-09-17T00:00:00.000Z")], "重跑迁移把已有的戳抹掉了"


@pytest.mark.parametrize(
    "target",
    ["files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"],
)
def test_non_core_target_untouched(tmp_path: Path, target: str) -> None:
    path = tmp_path / f"{target}.db"
    command.upgrade(_config(path, target), "head")
    with sqlite3.connect(path) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    assert TABLE not in tables, f"{target} 目标不该有 {TABLE}"


def test_downgrade_refuses_instead_of_silently_dropping() -> None:
    """生产不 downgrade：回滚靠同版本库归档。显式拒绝好过悄悄改结构。"""
    with pytest.raises(NotImplementedError):
        MIGRATION.downgrade()


# ───────────────────────── Reader side ─────────────────────────


def test_reader_returns_none_until_someone_stamps(tmp_path: Path) -> None:
    """★★ 三种「还没有水位」读出来都是 None ⇒ 一切判为切点前 ⇒ 逐字节今天的行为。

    ⚠ 正对照**必须**是「手工打戳后读得出值」：迁移后 `activated_at` 是 NULL，
    于是「表不存在」和「已迁移但没打戳」**读出来一模一样**——没有正对照的话，
    reader 整个坏掉也长这样（[[feedback_assert_absence_needs_positive_control]]）。
    """
    # ① Table absent (legacy store-only database; deliberately excluded from initialize())
    store_only = tmp_path / "store_only.db"
    with sqlite3.connect(store_only) as db:
        db.execute("CREATE TABLE artifacts (id TEXT PRIMARY KEY)")
    assert asyncio.run(read_artifact_workspace_cutover_at(str(store_only))) is None

    # ② Migrated but not stamped
    migrated = tmp_path / "core.db"
    command.upgrade(_config(migrated), "head")
    assert asyncio.run(read_artifact_workspace_cutover_at(str(migrated))) is None

    # ③ Positive control: manually stamped value **is readable**, proving the reader works.
    with sqlite3.connect(migrated) as db:
        db.execute(f"UPDATE {TABLE} SET activated_at=? WHERE singleton=1", ("2026-09-17T00:00:00.000Z",))  # noqa: S608
    assert asyncio.run(read_artifact_workspace_cutover_at(str(migrated))) == "2026-09-17T00:00:00.000Z"


@pytest.mark.parametrize(
    ("created_at", "cutover_at", "expected"),
    [
        # No watermark ⇒ nothing is after cutover ⇒ today's behavior byte-for-byte.
        ("2026-09-17T10:00:00Z", None, False),
        ("2026-09-17T10:00:00Z", "", False),
        # Before / after cutover
        ("2026-09-16T23:59:59Z", "2026-09-17T00:00:00.000Z", False),
        ("2026-09-17T00:00:01Z", "2026-09-17T00:00:00.000Z", True),
        # ★ Cross-timezone: string comparison reverses the result ('+08:00' < 'Z'); actual parsing is required.
        ("2026-09-17T09:00:00+08:00", "2026-09-17T02:00:00.000Z", False),
        ("2026-09-17T11:00:00+08:00", "2026-09-17T02:00:00.000Z", True),
        # Legacy date-only (naive) rows are interpreted as UTC.
        ("2026-09-01", "2026-09-17T00:00:00.000Z", False),
        # Parse failure ⇒ before cutover ⇒ fail-open (conservative direction).
        ("not-a-timestamp", "2026-09-17T00:00:00.000Z", False),
        (None, "2026-09-17T00:00:00.000Z", False),
    ],
)
def test_cutover_comparison_truth_table(created_at: str | None, cutover_at: str | None, expected: bool) -> None:
    """★ 判据只有这一份实现，四档判定全靠它。跨时区那两格是真会判反的形状。"""
    assert artifact_created_after_cutover(created_at, cutover_at) is expected
