"""Migration 0153 adds the per-turn memory request ledger to core and advances every target."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0153_memory_turn_requests")
#: The frozen `1.0.0` baseline revision that existing deployments adopt before later revisions.
BASELINE = import_module("bowhead_core.migrations.versions.0001_initial")
pytestmark = pytest.mark.integration


def _tables(path):
    with sqlite3.connect(path) as db:
        return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_preserves_rows_and_adds_the_ledger_only_to_core(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before = snapshot(path)
    previous = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    after = snapshot(path)
    assert after.pop("memory_turn_requests", []) == []
    assert after == before
    installed = schema(path)
    added = {row[1] for row in set(installed) - set(previous)}
    if target == "core":
        assert added == {
            "memory_turn_requests",
            "sqlite_autoindex_memory_turn_requests_1",
            "idx_memory_turn_requests_turn",
            "trg_memory_turn_requests_turn_delete",
            "trg_memory_turn_requests_user_delete",
        }
    else:
        assert added == set()
    # Repeat upgrade is a no-op.
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == installed
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)


def test_empty_database_and_the_1_0_0_baseline_both_reach_head_with_the_ledger(tmp_path):
    empty = tmp_path / "empty.db"
    _upgrade(empty, "head")
    baseline = tmp_path / "baseline.db"
    _upgrade(baseline, BASELINE.revision)
    with sqlite3.connect(baseline) as db:
        db.execute("INSERT INTO teams(id,name,created_at) VALUES('team_old','Old team','2026-01-01')")
        db.execute("INSERT INTO users(id,team_id,email,created_at) VALUES('usr_old','team_old','old@example.com','2026-01-01')")
    _upgrade(baseline, "head")
    _upgrade(baseline, "head")
    for path in (empty, baseline):
        assert "memory_turn_requests" in _tables(path)
    with sqlite3.connect(baseline) as db:
        assert db.execute("SELECT email FROM users WHERE id='usr_old'").fetchone() == ("old@example.com",)
    with sqlite3.connect(empty) as a, sqlite3.connect(baseline) as b:
        query = "SELECT sql FROM sqlite_master WHERE name LIKE '%memory_turn_requests%' ORDER BY name"
        assert a.execute(query).fetchall() == b.execute(query).fetchall()


def test_rows_leave_with_their_turn_and_with_the_account(tmp_path):
    path = tmp_path / "core.db"
    _upgrade(path, "head")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO users(id,team_id,email,created_at) VALUES('usr_a','team_a','a@example.com','2026-01-01')")
        db.execute("INSERT INTO turns(id,session_id,seq,user_text,started_at) VALUES('t_1','s_1',1,'remember this','2026-01-01')")
        db.execute("INSERT INTO turns(id,session_id,seq,user_text,started_at) VALUES('t_2','s_1',2,'and this','2026-01-01')")
        for user, turn in (("usr_a", "t_1"), ("usr_a", "t_2"), ("usr_b", "t_2")):
            db.execute(
                "INSERT INTO memory_turn_requests(user_id,turn_id,source,outcome,created_at) VALUES(?,?,'remember_tool','remembered','now')",
                (user, turn),
            )
        db.execute("DELETE FROM turns WHERE id='t_1'")
        assert db.execute("SELECT user_id,turn_id FROM memory_turn_requests ORDER BY user_id").fetchall() == [
            ("usr_a", "t_2"),
            ("usr_b", "t_2"),
        ]
        db.execute("DELETE FROM users WHERE id='usr_a'")
        assert db.execute("SELECT user_id,turn_id FROM memory_turn_requests").fetchall() == [("usr_b", "t_2")]


def test_closed_vocabularies_reject_unknown_codes(tmp_path):
    path = tmp_path / "core.db"
    _upgrade(path, "head")
    with sqlite3.connect(path) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO memory_turn_requests(user_id,turn_id,source,outcome,created_at) VALUES('u','t','model_prose','remembered','now')")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO memory_turn_requests(user_id,turn_id,source,outcome,created_at) VALUES('u','t','capture','maybe','now')")


def test_failed_installation_leaves_no_partial_ledger(tmp_path, monkeypatch):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision, "core")
    before = schema(path)
    execute = Connection.exec_driver_sql

    def interrupted(self, statement, *args, **kwargs):
        if statement.startswith("CREATE TRIGGER trg_memory_turn_requests_user_delete"):
            raise RuntimeError("injected migration failure")
        return execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "exec_driver_sql", interrupted)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        _upgrade(path, MIGRATION.revision, "core")
    assert schema(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)
