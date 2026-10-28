"""0079 adds only a v2 system snapshot guard, preserving all previous rows."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _historical_fixture, _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0079_system_skill_snapshot")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_all_targets_upgrade_repeat_and_keep_previous_rows(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == before
    after_schema = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == after_schema
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_schema_drift_rejected_without_advancing_revision(tmp_path):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE trg_system_precise_skill_snapshot(x TEXT)")
    before = schema(path)
    with pytest.raises(RuntimeError, match="system Skill snapshot schema drift"):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == before


@pytest.mark.asyncio
async def test_upgrade_preserves_actual_legacy_system_revision_and_guards_new_shape(tmp_path, monkeypatch):
    import hashlib
    import json
    from uuid import uuid4

    from bowhead_core.subagent import SubagentRunner, _materialize_system_profile_snapshot, _system_profile_snapshot
    from bowhead_core.tool_registry import ToolExecutionContext
    from test_agent_execution_binding import _setup
    from test_system_skill_snapshot import profile

    fixture = await _setup(tmp_path)
    runner = SubagentRunner(router=None, tool_registry=None, team_subagent_store=fixture.users)
    ctx = ToolExecutionContext(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        member_uid=fixture.member_id,
        workspace_id=fixture.workspace_id,
        turn_id=fixture.turn_id,
        datarc=None,
    )
    original_snapshot = _system_profile_snapshot

    def legacy_snapshot(value):
        result = original_snapshot(value)
        result.update(schema_version=1, max_output_tokens=8000)
        return result

    monkeypatch.setattr("bowhead_core.subagent._system_profile_snapshot", legacy_snapshot)
    binding = await runner.admit_execution_binding(
        profile_name="compute", parent_turn_id=fixture.turn_id, parent_ctx=ctx, dispatch_key="legacy-system"
    )
    assert binding.definition["schema_version"] == 1
    path = tmp_path / "prior.db"
    _historical_fixture(fixture.db_path, path, MIGRATION.down_revision)
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision)
    assert snapshot(path) == before
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        row = dict(db.execute("SELECT * FROM system_agent_effective_revision WHERE id=?", (binding.revision_ref,)).fetchone())
        saved = json.loads(row["definition_json"])
        assert saved == binding.definition and _materialize_system_profile_snapshot(saved, expected_name="compute").skill_runtime_grants is None
        invalid = _system_profile_snapshot(profile())
        invalid.update(schema_version=2, max_output_tokens=8000)
        del invalid["skill_grants"]
        encoded = json.dumps(invalid, sort_keys=True)
        row.update(id="arv_" + uuid4().hex, definition_json=encoded, definition_hash="sha256:" + hashlib.sha256(encoded.encode()).hexdigest())
        with pytest.raises(sqlite3.IntegrityError, match="system precise Skill snapshot is malformed"):
            db.execute(
                "INSERT INTO system_agent_effective_revision(" + ",".join(row) + ") VALUES (" + ",".join("?" for _ in row) + ")", tuple(row.values())
            )


def test_trigger_creation_failure_rolls_back_without_version_advance(tmp_path, monkeypatch):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    before = schema(path)
    original = Connection.exec_driver_sql

    def fault(self, sql, *args, **kwargs):
        result = original(self, sql, *args, **kwargs)
        if sql.startswith("CREATE TRIGGER trg_system_precise_skill_snapshot"):
            raise RuntimeError("injected post-DDL failure")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fault)
    with pytest.raises(RuntimeError, match="injected post-DDL failure"):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)
