"""Expand checkpoint metadata without rewriting the original BI conversation."""

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

MIGRATION = import_module("bowhead_core.migrations.versions.0083_reference_read_checkpoint")
claimed_case, continuation_case, receiver_case, start_case = _claimed_case, _continuation_case, _receiver_case, _start_case
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_all_targets_expand_once_and_preserve_existing_rows(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, old_schema = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    after, current_schema = snapshot(path), schema(path)
    assert after == before
    if target == "core":
        assert all(row in current_schema for row in old_schema if row[1] not in {MIGRATION.TABLE, MIGRATION.SCOPE})
        with sqlite3.connect(path) as db:
            assert {row[1] for row in db.execute(f"PRAGMA table_info({MIGRATION.TABLE})")} >= {"checkpoint_version", "captured_iterations"}
    else:
        assert current_schema == old_schema
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == after and schema(path) == current_schema
    empty = tmp_path / "empty.db"
    _upgrade(empty, MIGRATION.revision, target)
    assert schema(empty) == current_schema
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("failure", ["drift", "after_columns", "after_scope"])
def test_failed_expansion_leaves_original_schema_and_version_intact(tmp_path, monkeypatch, failure):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    if failure == "drift":
        with sqlite3.connect(path) as db:
            db.execute(f"DROP TRIGGER {MIGRATION.SCOPE}")
            db.execute(f"CREATE TRIGGER {MIGRATION.SCOPE} BEFORE INSERT ON {MIGRATION.TABLE} BEGIN SELECT 1; END")
    before, old_schema = snapshot(path), schema(path)
    original = Connection.exec_driver_sql

    def fault(self, statement, *args, **kwargs):
        result = original(self, statement, *args, **kwargs)
        if (failure == "after_columns" and statement.startswith(f"ALTER TABLE {MIGRATION.TABLE} ADD COLUMN captured_iterations")) or (
            failure == "after_scope" and statement.strip().startswith(f"CREATE TRIGGER {MIGRATION.SCOPE} ")
        ):
            raise RuntimeError("injected DDL failure")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fault)
    with pytest.raises(RuntimeError, match="schema drift|injected DDL failure"):
        _upgrade(path, MIGRATION.revision)
    assert snapshot(path) == before and schema(path) == old_schema
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)


@pytest.mark.asyncio
@pytest.mark.parametrize("start_case", ["team"], indirect=True)
@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
@pytest.mark.parametrize("claimed_case", ["agent_baseline"], indirect=True)
async def test_original_bi_checkpoint_and_receipts_survive_expansion(continuation_case, tmp_path):
    case = continuation_case
    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=workspace_question)
        await pause(case)
    path = tmp_path / "previous.db"
    _historical_fixture(case.path, path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        columns = [row[1] for row in db.execute(f"PRAGMA table_info({MIGRATION.TABLE})")]
        before = db.execute(f"SELECT {','.join(columns)} FROM {MIGRATION.TABLE}").fetchall()
        assert len(before) == 1
    receipts = snapshot(path)["chat_reference_model_invocations"]
    _upgrade(path, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        assert db.execute(f"SELECT {','.join(columns)} FROM {MIGRATION.TABLE}").fetchall() == before
        assert db.execute(f"SELECT checkpoint_version,captured_iterations FROM {MIGRATION.TABLE}").fetchall() == [(1, 1)]
        with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
            db.execute(
                f"UPDATE {MIGRATION.TABLE} SET state='reserved',resume_generation=2,answer_hash=?,captured_iterations=2", ("sha256:" + "a" * 64,)
            )
    assert snapshot(path)["chat_reference_model_invocations"] == receipts
