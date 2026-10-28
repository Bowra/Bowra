"""Snapshot v3 guards install atomically without rewriting historical intent."""

import json
import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0195_model_output_budget")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_all_targets_upgrade_without_backfill_and_repeat_safely(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == before
    installed = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == installed
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v3_requires_valid_budget_and_preserves_precise_skill_shape():
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE system_agent_effective_revision(definition_json TEXT)")
        db.execute(MIGRATION.DDL)
        for value in (
            {"schema_version": 1, "max_output_tokens": 8000},
            {"schema_version": 2, "max_output_tokens": 4000},
            {"schema_version": 3, "max_output_tokens": None},
            {"schema_version": 3, "max_output_tokens": 256000},
        ):
            db.execute("INSERT INTO system_agent_effective_revision VALUES (?)", (json.dumps(value),))
        for patch in (
            {},
            {"max_output_tokens": 0},
            {"max_output_tokens": True},
            {"max_output_tokens": "32000"},
            {"max_output_tokens": None, "skill_grants": []},
        ):
            with pytest.raises(sqlite3.IntegrityError, match="output budget snapshot is malformed"):
                db.execute("INSERT INTO system_agent_effective_revision VALUES (?)", (json.dumps({"schema_version": 3, **patch}),))
        assert db.execute("SELECT COUNT(*) FROM system_agent_effective_revision").fetchone()[0] == 4


def test_guard_creation_failure_rolls_back_ddl_and_ledger(tmp_path, monkeypatch):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    before = schema(path)
    original = Connection.exec_driver_sql

    def fault(self, sql, *args, **kwargs):
        result = original(self, sql, *args, **kwargs)
        if sql.startswith("CREATE TRIGGER " + MIGRATION.TRIGGER):
            raise RuntimeError("injected post-DDL failure")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fault)
    with pytest.raises(RuntimeError, match="injected post-DDL failure"):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == before


@pytest.mark.parametrize("version", [1, 2])
def test_historical_snapshot_numeric_allowance_is_preserved(version):
    from bowhead_core.subagent import _materialize_system_profile_snapshot, _system_profile_snapshot
    from test_system_skill_snapshot import profile

    definition = _system_profile_snapshot(profile())
    definition.update(schema_version=version, max_output_tokens=8000)
    if version == 1:
        definition.pop("skill_runtime_scope")
        definition.pop("skill_grants")
    assert _materialize_system_profile_snapshot(definition, expected_name="compute").max_output_tokens == 8000
