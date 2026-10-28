"""migration 0016_plan_replan 测试（数据库迁移.md 门禁）.

空库 / 1.0.0 fixture → head、重复执行幂等、双路接管（store 已自建列的库再跑
revision 不得 duplicate-column）。镜像 test_alembic_plan_step_verify.py 三件套
（graph-mode 批 3：plan_steps.superseded_by/input_manifest_json + plans.replan_count）。
"""

from __future__ import annotations

import sqlite3

from bowhead_cli.migrate import TARGET_FILES, upgrade
from bowhead_core.migrations import chain_head
from bowhead_core.migrations.baseline_schema import initial_schema

# Never hardcode revision numbers; see bowhead_core.migrations.chain_head.
HEAD_REVISION = chain_head()


def _create_1_0_fixture(db_path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()


def _cols(connection: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]  # noqa: S608


def test_plan_replan_fixture_upgrades_and_is_idempotent(tmp_path) -> None:
    core_path = tmp_path / TARGET_FILES["core"]
    _create_1_0_fixture(core_path)

    assert upgrade(f"sqlite+aiosqlite:///{core_path}") == tuple(TARGET_FILES)
    # Repeated execution is idempotent.
    assert upgrade(f"sqlite+aiosqlite:///{core_path}") == tuple(TARGET_FILES)

    with sqlite3.connect(core_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD_REVISION,)
        assert "superseded_by" in _cols(connection, "plan_steps")
        assert "input_manifest_json" in _cols(connection, "plan_steps")
        assert "replan_count" in _cols(connection, "plans")


def test_plan_replan_empty_database_upgrades_to_head(tmp_path) -> None:
    core_path = tmp_path / TARGET_FILES["core"]

    assert upgrade(f"sqlite+aiosqlite:///{core_path}") == tuple(TARGET_FILES)

    with sqlite3.connect(core_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD_REVISION,)
        assert "superseded_by" in _cols(connection, "plan_steps")
        assert "replan_count" in _cols(connection, "plans")


def test_plan_replan_store_created_columns_not_duplicated(tmp_path) -> None:
    """双路接管：store initialize() 先加了列的库，revision 必须 no-op."""
    core_path = tmp_path / TARGET_FILES["core"]
    _create_1_0_fixture(core_path)
    with sqlite3.connect(core_path) as connection:
        # Simulate plan_store.initialize() guard ALTERs already executed.
        connection.execute("ALTER TABLE plan_steps ADD COLUMN superseded_by TEXT")
        connection.execute("ALTER TABLE plan_steps ADD COLUMN input_manifest_json TEXT")
        connection.execute("ALTER TABLE plans ADD COLUMN replan_count INTEGER NOT NULL DEFAULT 0")
        connection.commit()

    # Duplicate columns make upgrade raise OperationalError; guards must prevent this.
    assert upgrade(f"sqlite+aiosqlite:///{core_path}") == tuple(TARGET_FILES)

    with sqlite3.connect(core_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD_REVISION,)
        names = _cols(connection, "plan_steps")
        assert names.count("superseded_by") == 1
        assert names.count("input_manifest_json") == 1
        assert _cols(connection, "plans").count("replan_count") == 1
