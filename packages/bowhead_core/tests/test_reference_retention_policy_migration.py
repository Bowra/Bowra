"""0068 appends a policy fence without rewriting original material identities."""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade

pytestmark = pytest.mark.integration
MIGRATION = import_module("bowhead_core.migrations.versions.0068_reference_retention_policy")


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_and_repeat_preserve_all_previous_columns(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    with sqlite3.connect(path) as db:
        names = [
            r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name<>'alembic_version' AND name NOT LIKE 'sqlite_%'")
        ]
        columns = {name: ",".join('"' + r[1] + '"' for r in db.execute(f"PRAGMA table_info({name})")) for name in names}
        before = {name: db.execute(f"SELECT {cols} FROM {name}").fetchall() for name, cols in columns.items()}
        schema = db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    _upgrade(path, MIGRATION.revision, target)
    _upgrade(path, MIGRATION.revision, target)
    with sqlite3.connect(path) as db:
        assert {name: db.execute(f"SELECT {cols} FROM {name}").fetchall() for name, cols in columns.items()} == before
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
        if target != "core":
            assert db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall() == schema
        else:
            assert db.execute("SELECT COUNT(*) FROM team_reference_retention_policies").fetchone() == (0,)
            assert db.execute("SELECT * FROM chat_reference_plan_retention").fetchall() == []


@pytest.mark.parametrize("drift", ["table", "view", "trigger", "plan_column", "queue_column"])
def test_drift_fails_before_ddl(tmp_path, drift):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        if drift.endswith("column"):
            table = "chat_reference_plans" if drift == "plan_column" else "chat_reference_queued_inputs"
            db.execute(f"ALTER TABLE {table} ADD COLUMN retention_policy_version TEXT")
        else:
            name = {
                "table": "team_reference_retention_policies",
                "view": "chat_reference_queue_retention",
                "trigger": "trg_reference_policy_completion",
            }[drift]
            db.execute(f"CREATE TABLE {name}(x TEXT)")
        schema = db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    with pytest.raises(RuntimeError, match="reference retention policy schema drift"):
        _upgrade(path, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall() == schema
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)


@pytest.mark.asyncio
async def test_populated_previous_release_gets_version_zero_and_current_policy_applies(tmp_path, monkeypatch):
    import test_chat_reference_storage as storage
    from bowhead_core.chat_reference_retention import ReferenceRetentionResult, purge_expired_reference_material
    from bowhead_core.reference_retention_policy import set_retention_policy
    from test_reference_retention_policy import _old_material

    case = await storage.case.__wrapped__(tmp_path)
    await _old_material(case, monkeypatch)
    path = tmp_path / "previous.db"
    storage._historical_fixture(case.path, path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        before = db.execute("SELECT plan_json,expires_at FROM chat_reference_plans").fetchall()
        ciphertext = db.execute("SELECT ciphertext FROM chat_reference_content_snapshots").fetchall()
    _upgrade(path, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT plan_json,expires_at FROM chat_reference_plans").fetchall() == before
        assert db.execute("SELECT ciphertext FROM chat_reference_content_snapshots").fetchall() == ciphertext
        assert db.execute("SELECT retention_policy_version FROM chat_reference_plans").fetchone() == (0,)
        assert db.execute("SELECT retention_policy_version FROM chat_reference_queued_inputs").fetchone() == (0,)
    await set_retention_policy(path, team_id=case.scope.team_id, user_id=case.scope.user_id, days=1, expected_version=0)
    assert await purge_expired_reference_material(path) == ReferenceRetentionResult(1, 1)
