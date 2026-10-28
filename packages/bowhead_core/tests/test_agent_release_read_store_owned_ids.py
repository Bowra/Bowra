"""`AgentReleaseReadStore.owned_agent_ids` 打真迁移库（批 2b-i）。

建议收件箱的资源 owner 列表车道靠它；路由测试里它被打桩，所以这里用真表
钉住谓词——schema 漂移让这里先红，而不是列表端点在生产里 500。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.agent_release_read_store import AgentReleaseReadStore

from bowhead_core import migrations

pytestmark = pytest.mark.integration


def _migrated_db(tmp_path: Path) -> str:
    path = tmp_path / "core.db"
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = "core"
    command.upgrade(config, "head")
    return str(path)


def _seed(db_path: str, rows: list[tuple[str, str, str, str | None, str]]) -> None:
    with sqlite3.connect(db_path) as con:
        for agent_id, team_id, created_by, owner, lifecycle in rows:
            con.execute(
                """
                INSERT INTO team_subagent
                  (id, team_id, name, created_by, owner_user_id, lifecycle_state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 't', 't')
                """,
                (agent_id, team_id, agent_id, created_by, owner, lifecycle),
            )


def test_owned_ids_follow_coalesce_owner_creator_within_team(tmp_path: Path):
    db_path = _migrated_db(tmp_path)
    _seed(
        db_path,
        [
            ("ag_created", "t1", "u_me", None, "active"),  # Creator fallback
            ("ag_owned", "t1", "u_other", "u_me", "archived"),  # Explicit owner; archived also counts.
            ("ag_transferred", "t1", "u_me", "u_other", "active"),  # Transferred away: exclude.
            ("ag_other_team", "t2", "u_me", None, "active"),  # Cross-team: exclude.
            ("ag_deleted", "t1", "u_me", None, "deleted"),  # Deleted: exclude.
        ],
    )
    owned = asyncio.run(AgentReleaseReadStore(db_path).owned_agent_ids(team_id="t1", actor_user_id="u_me"))
    assert owned == {"ag_created", "ag_owned"}
