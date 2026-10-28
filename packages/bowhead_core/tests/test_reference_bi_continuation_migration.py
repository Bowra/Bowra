"""All targets use the real current-head startup and preserve the merge leaf."""

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

MIGRATION = import_module("bowhead_core.migrations.versions.0078_reference_bi_continuation")
pytestmark = pytest.mark.integration
claimed_case, continuation_case, receiver_case, start_case = _claimed_case, _continuation_case, _receiver_case, _start_case


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_all_targets_upgrade_to_current_head_without_rewriting_prior_rows(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision, target)
    actual = snapshot(path)
    assert {name: actual[name] for name in before} == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchall() == [(MIGRATION.revision,)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    current_schema = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == actual and schema(path) == current_schema
    empty = tmp_path / "empty.db"
    _upgrade(empty, MIGRATION.revision, target)
    assert schema(empty) == current_schema


@pytest.mark.asyncio
@pytest.mark.parametrize("start_case", ["team"], indirect=True)
@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
@pytest.mark.parametrize("claimed_case", ["agent_baseline"], indirect=True)
@pytest.mark.parametrize("failure", [None, "rename", "copy", "drift"])
async def test_real_prior_model_receipt_is_preserved_or_migration_fully_rolls_back(continuation_case, tmp_path, monkeypatch, failure):
    from sqlalchemy.engine import Connection

    case = continuation_case
    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=workspace_question)
        await pause(case)
    path = tmp_path / "old.db"
    _historical_fixture(case.path, path, MIGRATION.down_revision)
    if failure == "drift":
        with sqlite3.connect(path) as db:
            db.execute("CREATE INDEX unexpected_ledger_index ON chat_reference_model_invocations(status)")
    before, before_schema = snapshot(path), schema(path)
    assert len(before["chat_reference_model_invocations"]) == 1
    original = Connection.exec_driver_sql

    def fault(self, statement, *args, **kwargs):
        if failure == "rename" and statement.startswith("ALTER TABLE chat_reference_model_invocations_0078"):
            raise RuntimeError("injected migration failure")
        result = original(self, statement, *args, **kwargs)
        if failure == "copy" and statement.startswith("INSERT INTO chat_reference_model_invocations_0078"):
            original(self, "DELETE FROM chat_reference_model_invocations_0078")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fault)
    if failure:
        with pytest.raises(
            RuntimeError, match={"rename": "injected migration failure", "copy": "receipt copy mismatch", "drift": "receipt schema drift"}[failure]
        ):
            _upgrade(path, MIGRATION.revision)
        assert snapshot(path) == before and schema(path) == before_schema
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)
    else:
        _upgrade(path, MIGRATION.revision)
        after = snapshot(path)
        assert {name: after[name] for name in before} == before
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
