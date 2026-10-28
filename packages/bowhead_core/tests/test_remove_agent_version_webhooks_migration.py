"""Removing Agent version webhooks drops their tables and keeps automation grants."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_skill_invocation_migration import schema

pytestmark = pytest.mark.unit
MIGRATION = import_module("bowhead_core.migrations.versions.0190_remove_agent_version_webhooks")
WEBHOOK_TABLES = {"agent_version_webhook_delivery", "agent_version_webhook_event", "agent_version_webhook_subscription"}


def _objects(path, *, kind: str) -> set[str]:
    with sqlite3.connect(path) as db:
        return {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type=?", (kind,))}


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_drops_webhook_tables_with_their_indexes_and_triggers(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    if target == "core":
        assert _objects(path, kind="table") >= WEBHOOK_TABLES
        with sqlite3.connect(path) as db:
            # A pending account-deletion membership row for a dropped table must not outlive it.
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("INSERT INTO account_deletion_rows(operation_id,table_name,row_key) VALUES('op_1','agent_version_webhook_event','avwe_1')")
            db.execute("INSERT INTO account_deletion_rows(operation_id,table_name,row_key) VALUES('op_1','agent_version_automation_grant','avag_1')")
    before = schema(path)

    _upgrade(path, MIGRATION.revision, target)

    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == MIGRATION.revision
        if target == "core":
            rows = db.execute("SELECT table_name FROM account_deletion_rows ORDER BY table_name").fetchall()
            assert rows == [("agent_version_automation_grant",)]
    if target != "core":
        assert schema(path) == before
        return
    assert not WEBHOOK_TABLES & _objects(path, kind="table")
    assert not {name for name in _objects(path, kind="index") | _objects(path, kind="trigger") if "webhook" in name}
    # Grants, their index and the rest of the 0075 schema stay.
    assert "agent_version_automation_grant" in _objects(path, kind="table")
    assert "idx_agent_automation_grants" in _objects(path, kind="index")

    # Re-running the upgrade is a no-op.
    after = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == after
