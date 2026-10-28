"""migration 0011_plan_step_verify 测试（数据库迁移.md 门禁）.

空库 / 1.0.0 fixture → head、重复执行幂等、双路接管（store 已自建列的库再跑
revision 不得 duplicate-column）。镜像 test_alembic_builtin_subagent_override.py
三件套 + graph-mode review B1 的双所有权场景。
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


def _step_columns(connection: sqlite3.Connection) -> dict[str, str]:
    return {str(row[1]): str(row[4] or "") for row in connection.execute("PRAGMA table_info(plan_steps)")}


def test_plan_verify_fixture_upgrades_and_is_idempotent(tmp_path) -> None:
    core_path = tmp_path / TARGET_FILES["core"]
    _create_1_0_fixture(core_path)

    assert upgrade(f"sqlite+aiosqlite:///{core_path}") == tuple(TARGET_FILES)
    # Repeated execution is idempotent.
    assert upgrade(f"sqlite+aiosqlite:///{core_path}") == tuple(TARGET_FILES)

    for filename in TARGET_FILES.values():
        with sqlite3.connect(tmp_path / filename) as connection:
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD_REVISION,)

    with sqlite3.connect(core_path) as connection:
        cols = _step_columns(connection)
        assert cols["success_criteria"] == "'[]'"
        assert "verify_json" in cols


def test_plan_verify_empty_database_upgrades_to_head(tmp_path) -> None:
    core_path = tmp_path / TARGET_FILES["core"]

    assert upgrade(f"sqlite+aiosqlite:///{core_path}") == tuple(TARGET_FILES)

    with sqlite3.connect(core_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD_REVISION,)
        assert "success_criteria" in _step_columns(connection)


def test_plan_verify_store_created_columns_not_duplicated(tmp_path) -> None:
    """双路接管（review B1）：store initialize() 先加了列的库，revision 必须 no-op."""
    core_path = tmp_path / TARGET_FILES["core"]
    _create_1_0_fixture(core_path)
    with sqlite3.connect(core_path) as connection:
        # Simulate plan_store.initialize() guard ALTERs already executed.
        connection.execute("ALTER TABLE plan_steps ADD COLUMN success_criteria TEXT NOT NULL DEFAULT '[]'")
        connection.execute("ALTER TABLE plan_steps ADD COLUMN verify_json TEXT")
        connection.commit()

    # Duplicate columns make upgrade raise OperationalError; guards must prevent this.
    assert upgrade(f"sqlite+aiosqlite:///{core_path}") == tuple(TARGET_FILES)

    with sqlite3.connect(core_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD_REVISION,)
        names = [str(row[1]) for row in connection.execute("PRAGMA table_info(plan_steps)")]
        assert names.count("success_criteria") == 1
        assert names.count("verify_json") == 1
