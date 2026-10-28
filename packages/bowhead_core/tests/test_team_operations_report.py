"""Team operations reporting preserves historical zeros and team boundaries."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.session_store import SessionStore
from bowhead_core.team_operations_report import get_team_operations_report, set_team_report_timezone

from bowhead_core import migrations

pytestmark = pytest.mark.integration
MIGRATION = import_module("bowhead_core.migrations.versions.0172_team_operations")


def config(path: Path, target: str = "core") -> Config:
    result = Config()
    result.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    result.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    result.attributes["bowhead_target"] = target
    return result


@pytest.fixture(scope="module")
def migrated_db(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("team_operations") / "core.db"
    command.upgrade(config(path), MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO teams(id,name,created_at) VALUES ('team_one','One','2026-10-01')")
    command.upgrade(config(path), "head")
    return path


def test_migration_preserves_team_and_defaults_historical_operation_rows_to_zero(migrated_db: Path):
    with sqlite3.connect(migrated_db) as db:
        assert db.execute("SELECT name,report_timezone FROM teams WHERE id='team_one'").fetchone() == ("One", "Asia/Shanghai")
        assert db.execute("SELECT COUNT(*) FROM turn_operation_usage").fetchone() == (0,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.anyio
async def test_report_uses_complete_local_days_and_scopes_every_source_to_team(migrated_db: Path):
    with sqlite3.connect(migrated_db) as db:
        db.execute("INSERT INTO teams(id,name,created_at) VALUES ('team_other','Other','2026-10-01')")
        for team, space, session, turn in (
            ("team_one", "ws_one", "s_one", "t_one"),
            ("team_other", "ws_other", "s_other", "t_other"),
        ):
            db.execute(
                "INSERT INTO workspaces(id,team_id,name,created_at,updated_at) VALUES (?,?,?,?,?)", (space, team, space, "2026-10-01", "2026-10-01")
            )
            db.execute(
                "INSERT INTO sessions(id,workspace_id,user_id,started_at,last_active_at) VALUES (?,?,?,?,?)",
                (session, space, "usr_1", "2026-10-11T16:30:00+00:00", "2026-10-11T16:30:00+00:00"),
            )
            db.execute(
                "INSERT INTO turns(id,session_id,seq,user_text,started_at) VALUES (?,?,?,?,?)",
                (turn, session, 1, "hello", "2026-10-11T16:30:00+00:00"),
            )
            db.execute(
                "INSERT INTO turn_operation_usage(turn_id,skill_loads,tool_loads,tool_calls,recorded_at) VALUES (?,?,?,?,?)",
                (turn, 2, 3, 4, "2026-10-11T16:40:00+00:00"),
            )
            db.execute(
                "INSERT INTO llm_usage_events(id,team_id,reason,agent_type,tokens_input,tokens_output,created_at) VALUES (?,?,?,?,?,?,?)",
                (f"lu_{team}", team, "turn", "lead", 10, 20, "2026-10-11T16:35:00+00:00"),
            )
            db.execute(
                "INSERT INTO user_audit_log(id,user_id,team_id,event_type,created_at) VALUES (?,?,?,?,?)",
                (f"aud_{team}", "usr_1", team, "team_login_success", "2026-10-11T16:20:00+00:00"),
            )
    await SessionStore(str(migrated_db)).persist_turn_operation_usage(turn_id="t_one", skill_loads=1, tool_loads=2, tool_calls=3)
    report = await get_team_operations_report(migrated_db, team_id="team_one", days=7, now=datetime(2026, 10, 13, tzinfo=UTC))
    day = next(row for row in report["days"] if row["day"] == "2026-10-12")
    assert report["timezone"] == "Asia/Shanghai"
    assert (day["login_users"], day["sessions"], day["input_tokens"], day["output_tokens"]) == (1, 1, 10, 20)
    assert (day["skill_loads"], day["tool_loads"], day["tool_calls"]) == (2, 3, 4)
    assert next(row for row in report["days"] if row["day"] == "2026-10-11")["sessions"] == 0
    assert all(value == 0 for row in report["days"] if row["day"] != "2026-10-12" for key, value in row.items() if key != "day")
    await set_team_report_timezone(migrated_db, team_id="team_one", timezone="UTC")
    regrouped = await get_team_operations_report(migrated_db, team_id="team_one", days=7, now=datetime(2026, 10, 13, tzinfo=UTC))
    assert next(row for row in regrouped["days"] if row["day"] == "2026-10-11")["sessions"] == 1
    assert next(row for row in regrouped["days"] if row["day"] == "2026-10-12")["sessions"] == 0
