"""Historical document evidence survives the additive recovery migration."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_router.types import TokenUsage
from test_chat_reference_storage import _historical_fixture, _upgrade, assert_rows_survive_upgrade
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db
from test_document_usage import SPEC, _hook
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0131_document_recovery")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_recovery_upgrade_preserves_other_targets_and_repeats(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, old_schema = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert_rows_survive_upgrade(before, snapshot(path))
    current = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == current
    if target != "core":
        assert current == old_schema


@pytest.mark.parametrize("known", [True, False])
async def test_historical_known_usage_is_settled_but_unknown_is_not_invented(ctx, tmp_path, known):
    _, hook = await _hook(ctx)
    await hook.begin(SPEC)
    if known:
        await hook.complete(usage=TokenUsage(600, 200), response_digest="b" * 64)
    else:
        await hook.unknown(reason_code="transport_disconnected")
    path = tmp_path / "historical.db"
    _historical_fixture(ctx.path, path, MIGRATION.down_revision)
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision)
    assert_rows_survive_upgrade(before, snapshot(path))
    with sqlite3.connect(path) as db:
        receipts = db.execute("SELECT tokens_input,tokens_output,settled_at FROM document_usage_receipts").fetchall()
        assert len(receipts) == int(known)
        if known:
            assert receipts[0][:2] == (600, 200) and receipts[0][2] is not None
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_recovery_schema_failure_is_atomic(tmp_path, monkeypatch):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    before = schema(path)
    original = Connection.exec_driver_sql

    def fail(self, statement, *args, **kwargs):
        result = original(self, statement, *args, **kwargs)
        if statement == MIGRATION.DDL[-1]:
            raise RuntimeError("Injected recovery migration failure")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fail)
    with pytest.raises(RuntimeError, match="Injected recovery"):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == before
