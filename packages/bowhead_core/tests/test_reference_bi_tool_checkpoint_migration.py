"""Expand only checkpoint versions and preserve encrypted paused work atomically."""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
import respx
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _historical_fixture, _upgrade
from test_reference_bi_continuation import claimed_case as _claimed_case
from test_reference_bi_continuation import continuation_case as _continuation_case
from test_reference_bi_continuation import pause, workspace_question
from test_reference_bi_continuation import receiver_case as _receiver_case
from test_reference_bi_continuation import start_case as _start_case
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

continuation_case, claimed_case, receiver_case, start_case = _continuation_case, _claimed_case, _receiver_case, _start_case
MIGRATION = import_module("bowhead_core.migrations.versions.0107_reference_bi_tool_checkpoint")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_keeps_prior_data_and_is_idempotent(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
        if target == "core":
            assert "checkpoint_version IN (1,2,3,4)" in db.execute("SELECT sql FROM sqlite_master WHERE name=?", (MIGRATION.TABLE,)).fetchone()[0]
            assert MIGRATION.BASE._normalized(
                db.execute("SELECT sql FROM sqlite_master WHERE name=?", (MIGRATION.CAPTURE,)).fetchone()[0]
            ) == MIGRATION.BASE._normalized(MIGRATION.SCOPE_DDL)
    after_schema = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == after_schema and snapshot(path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("start_case", ["team"], indirect=True)
@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
@pytest.mark.parametrize("claimed_case", ["agent_baseline"], indirect=True)
@pytest.mark.parametrize("failure", [None, "copy", "drift"])
async def test_original_encrypted_pause_is_preserved_or_rolled_back(continuation_case, tmp_path, monkeypatch, failure):
    from sqlalchemy.engine import Connection

    case = continuation_case
    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=workspace_question)
        await pause(case)
    path = tmp_path / "old.db"
    _historical_fixture(case.path, path, MIGRATION.down_revision)
    if failure == "drift":
        with sqlite3.connect(path) as db:
            db.execute(f"DROP TRIGGER {MIGRATION.CAPTURE}")
            db.execute(f"CREATE TRIGGER {MIGRATION.CAPTURE} BEFORE INSERT ON users BEGIN SELECT 1; END")
    before, before_schema = snapshot(path), schema(path)
    assert len(before[MIGRATION.TABLE]) == 1
    original = Connection.exec_driver_sql

    def fail(self, statement, *args, **kwargs):
        result = original(self, statement, *args, **kwargs)
        if failure == "copy" and statement.startswith(f"INSERT INTO {MIGRATION.SHADOW}"):
            original(self, f"DELETE FROM {MIGRATION.SHADOW}")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fail)
    if failure:
        with pytest.raises(RuntimeError):
            _upgrade(path, MIGRATION.revision)
        assert schema(path) == before_schema
    else:
        _upgrade(path, MIGRATION.revision)
    assert snapshot(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision if failure else MIGRATION.revision,)
