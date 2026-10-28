"""Prompt Lead upgrade retains historical material and restores failed upgrades."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core.mcp_prompt_expansion import prepare_mcp_prompt_expansion
from bowhead_core.reference_mcp_prompt_execution import materialize_reference_mcp_prompt_results
from test_chat_reference_storage import _historical_fixture, _upgrade
from test_reference_mcp_prompt_materialization import _claimed_case, _remote_result
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0129_reference_lead_prompt")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_prompt_lead_upgrade_preserves_all_targets(tmp_path, target):
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
async def test_historical_prompt_ciphertext_and_position_are_preserved(tmp_path):
    turn_id = "t_prompt_legacy_upgrade"
    case, admission, source, scope, cipher = await _claimed_case(tmp_path, turn_id=turn_id)

    async def fetch(resource, parameters, _revision):
        return prepare_mcp_prompt_expansion(resource=resource, parameters=parameters, result=_remote_result("Original material"))

    result = await materialize_reference_mcp_prompt_results(
        index=admission.mcp_index,
        access=admission.mcp_access,
        cipher=cipher,
        scope=scope,
        turn_id=turn_id,
        owner_id="prompt-worker",
        expected_generation=1,
        fetch=fetch,
    )
    historical = tmp_path / "historical.db"
    _historical_fixture(case.fixture.db_path, historical, MIGRATION.down_revision)
    before = snapshot(historical)
    assert before["chat_reference_content_snapshots"]
    _upgrade(historical, MIGRATION.revision)
    assert snapshot(historical) == before
    with sqlite3.connect(historical) as db:
        seq, material = db.execute(
            "SELECT resolution_seq,composition_json FROM chat_reference_resolutions WHERE idempotency_key='reference:primary:mcp-prompt-results'"
        ).fetchone()
    assert seq == 2 and material == result.resolution.canonical_json()


@pytest.mark.parametrize("failure", ["drift", "recreate"])
def test_prompt_lead_upgrade_is_atomic(tmp_path, monkeypatch, failure):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    if failure == "drift":
        with sqlite3.connect(path) as db:
            db.execute("DROP TRIGGER trg_reference_mcp_prompt_result_resolution")
    before, previous_schema = snapshot(path), schema(path)
    original = Connection.exec_driver_sql

    def fail(self, statement, *args, **kwargs):
        result = original(self, statement, *args, **kwargs)
        if failure == "recreate" and statement == MIGRATION.GUARD:
            raise RuntimeError("injected Prompt Lead migration failure")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fail)
    with pytest.raises(RuntimeError):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == previous_schema and snapshot(path) == before
