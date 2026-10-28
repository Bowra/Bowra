"""批 1-a migration 0007_config_copilot_drafts 测试（数据库迁移.md 门禁）.

空库/1.0.0 fixture → head、重复执行幂等、八目标顺序、表/索引/CHECK/TTL 列 nullable。
镜像 test_alembic_subagent_drafts.py 三件套。
"""

from __future__ import annotations

import sqlite3

import pytest
from bowhead_cli.migrate import TARGET_FILES, upgrade
from bowhead_core.migrations import chain_head
from bowhead_core.migrations.baseline_schema import initial_schema

# Never hardcode revision numbers; see bowhead_core.migrations.chain_head.
HEAD_REVISION = chain_head()

EXPECTED_INDEXES = {
    "idx_config_copilot_draft_owner",
    "idx_config_copilot_draft_scrub",
    "idx_config_copilot_draft_purge",
    "idx_config_copilot_draft_expiry",
}


def _create_1_0_fixture(db_path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()


def _objects(connection: sqlite3.Connection, kind: str) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type=?",
            (kind,),
        )
    }


def test_config_draft_revision_upgrades_all_eight_targets(tmp_path) -> None:
    core_path = tmp_path / TARGET_FILES["core"]
    _create_1_0_fixture(core_path)

    assert upgrade(f"sqlite+aiosqlite:///{core_path}") == tuple(TARGET_FILES)
    # Repeated execution is idempotent.
    assert upgrade(f"sqlite+aiosqlite:///{core_path}") == tuple(TARGET_FILES)

    for filename in TARGET_FILES.values():
        with sqlite3.connect(tmp_path / filename) as connection:
            assert connection.execute(
                "SELECT version_num FROM alembic_version",
            ).fetchone() == (HEAD_REVISION,)

    with sqlite3.connect(core_path) as connection:
        assert "config_copilot_draft" in _objects(connection, "table")
        assert _objects(connection, "index") >= EXPECTED_INDEXES
        columns = {str(row[1]): bool(row[3]) for row in connection.execute("PRAGMA table_info(config_copilot_draft)")}
        # (name -> notnull): lite's three TTL columns must be nullable (decision: unlimited undo time, review P1-1).
        assert columns["expires_at"] is False
        assert columns["content_scrub_at"] is False
        assert columns["purge_at"] is False
        assert columns["team_id"] is True
        assert columns["status"] is True


def test_config_draft_kind_check_constraint(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _create_1_0_fixture(db_path)
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")

    with sqlite3.connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO config_copilot_draft(
                    id, team_id, owner_user_id, kind, form_id, status,
                    row_version, created_at, updated_at
                ) VALUES ('ccd_x','team_1','u_1','bogus_kind','f','proposed',1,'t','t')
                """,
            )
        # Valid kind + all NULL TTLs insert successfully (lite form).
        connection.execute(
            """
            INSERT INTO config_copilot_draft(
                id, team_id, owner_user_id, kind, form_id, status,
                row_version, created_at, updated_at
            ) VALUES ('ccd_ok','team_1','u_1','lite_proposal','f','proposed',1,'t','t')
            """,
        )
