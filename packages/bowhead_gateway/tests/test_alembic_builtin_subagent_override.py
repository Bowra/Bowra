"""migration 0009_builtin_subagent_override 测试（数据库迁移.md 门禁）.

空库 / 1.0.0 fixture → head、重复执行幂等、八目标顺序、表结构与 CHECK 约束。
镜像 test_alembic_config_drafts.py 三件套。
"""

from __future__ import annotations

import sqlite3

import pytest
from bowhead_cli.migrate import TARGET_FILES, upgrade
from bowhead_core.migrations import chain_head
from bowhead_core.migrations.baseline_schema import initial_schema

# Never hardcode revision numbers; see bowhead_core.migrations.chain_head.
HEAD_REVISION = chain_head()

_INSERT = """
INSERT INTO team_builtin_subagent_override(
    team_id, name, enabled, row_version, created_at, updated_at
) VALUES (?, ?, ?, ?, 't', 't')
"""


def _create_1_0_fixture(db_path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()


def _objects(connection: sqlite3.Connection, kind: str) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type=?", (kind,))}


def test_override_revision_upgrades_all_eight_targets(tmp_path) -> None:
    core_path = tmp_path / TARGET_FILES["core"]
    _create_1_0_fixture(core_path)

    assert upgrade(f"sqlite+aiosqlite:///{core_path}") == tuple(TARGET_FILES)
    # Repeated execution is idempotent.
    assert upgrade(f"sqlite+aiosqlite:///{core_path}") == tuple(TARGET_FILES)

    for filename in TARGET_FILES.values():
        with sqlite3.connect(tmp_path / filename) as connection:
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD_REVISION,)

    with sqlite3.connect(core_path) as connection:
        assert "team_builtin_subagent_override" in _objects(connection, "table")
        columns = {str(row[1]): bool(row[3]) for row in connection.execute("PRAGMA table_info(team_builtin_subagent_override)")}
        # Only primary keys, enabled, row_version, and two timestamps are NOT NULL;
        # all others nullable: NULL carries the no-override semantics.
        assert columns["team_id"] is True
        assert columns["name"] is True
        assert columns["enabled"] is True
        assert columns["row_version"] is True
        for nullable in (
            "display_name",
            "description",
            "model",
            "skills_json",
            "prompt_append",
            "max_iterations",
            "primary_capability",
            "updated_by",
        ):
            assert columns[nullable] is False, nullable


def test_override_empty_database_upgrades_to_head(tmp_path) -> None:
    core_path = tmp_path / TARGET_FILES["core"]

    assert upgrade(f"sqlite+aiosqlite:///{core_path}") == tuple(TARGET_FILES)

    with sqlite3.connect(core_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD_REVISION,)
        assert "team_builtin_subagent_override" in _objects(connection, "table")


def test_override_primary_key_and_check_constraints(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _create_1_0_fixture(db_path)
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")

    with sqlite3.connect(db_path) as connection:
        connection.execute(_INSERT, ("team_1", "consultant", 0, 1))
        # (team_id, name) unique: one delta per team/builtin.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(_INSERT, ("team_1", "consultant", 1, 1))
        # Same-name builtins in other teams have independent deltas (schema guarantees cross-team isolation).
        connection.execute(_INSERT, ("team_2", "consultant", 1, 1))
        # enabled accepts only 0/1.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(_INSERT, ("team_3", "consultant", 7, 1))
        # max_iterations, if provided, must be >=1.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO team_builtin_subagent_override(
                    team_id, name, enabled, max_iterations, row_version, created_at, updated_at
                ) VALUES ('team_4','consultant',1,0,1,'t','t')
                """,
            )
