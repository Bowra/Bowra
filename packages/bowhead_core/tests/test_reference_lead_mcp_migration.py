"""MCP Lead migration preserves calls, ownership guards and atomic upgrades."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core.reference_mcp_runtime import ReferenceMcpHandlerScope, _saved_result
from test_chat_reference_storage import _historical_fixture, _upgrade
from test_reference_mcp_preparation import _claimed_runtime
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0128_reference_lead_mcp")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_lead_mcp_upgrade_preserves_all_rows_and_is_idempotent(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, previous_schema = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == before
    current = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == current
    if target != "core":
        assert current == previous_schema
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.asyncio
async def test_existing_mcp_result_keeps_its_original_identity_and_ciphertext(tmp_path):
    case, runtime, ctx, _, read, _, client = await _claimed_runtime(tmp_path, turn_id="t_mcp_legacy_migration")
    scope = await ReferenceMcpHandlerScope.bind(
        scope=runtime.scope,
        turn_id=runtime.turn_id,
        owner_id=runtime.owner_id,
        generation=1,
        index=runtime.mcp_index,
        access=runtime.mcp_access,
        bindings=runtime.bindings,
        view=ctx.tool_view,
    )
    result = await scope.wrap_view(ctx.tool_view, cipher=runtime.cipher).get(read.id).handler({"year": 2025}, ctx)
    assert result["ok"] is True and len(client.calls) == 1
    historical = tmp_path / "historical.db"
    _historical_fixture(case.fixture.db_path, historical, MIGRATION.down_revision)
    before = snapshot(historical)
    _upgrade(historical, MIGRATION.revision)
    assert snapshot(historical) == before
    with sqlite3.connect(historical) as db:
        db.row_factory = sqlite3.Row
        saved = db.execute("SELECT * FROM chat_reference_mcp_calls").fetchone()
    assert _saved_result(saved, scope=runtime.scope, cipher=runtime.cipher) == result
    assert len(client.calls) == 1


@pytest.mark.parametrize("failure", ["drift", "copy", "recreate"])
def test_lead_mcp_copy_migration_is_atomic(tmp_path, monkeypatch, failure):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    if failure == "drift":
        with sqlite3.connect(path) as db:
            db.execute("DROP TRIGGER trg_reference_mcp_call_scope")
    before, previous_schema = snapshot(path), schema(path)
    original = Connection.exec_driver_sql

    def fail(self, statement, *args, **kwargs):
        result = original(self, statement, *args, **kwargs)
        if (failure == "copy" and statement.startswith("INSERT INTO chat_reference_mcp_calls_next")) or (
            failure == "recreate" and statement == MIGRATION.GUARD
        ):
            raise RuntimeError("injected MCP Lead migration failure")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fail)
    with pytest.raises(RuntimeError):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == previous_schema and snapshot(path) == before
