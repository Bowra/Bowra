"""0189 adds the continuation ledger and the single guarded completed->running reopen."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

pytestmark = pytest.mark.unit
MIGRATION = import_module("bowhead_core.migrations.versions.0189_reference_primary_continuation")
_NEW = set(MIGRATION.NEW_OBJECTS)


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_adds_the_ledger_and_replaces_only_the_execution_transition(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before_rows, before_schema = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    after_rows = snapshot(path)
    assert after_rows.pop("chat_reference_primary_continuations", []) == []
    assert after_rows == before_rows
    after_schema = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == after_schema
    if target != "core":
        assert after_schema == before_schema
        return
    changed = {item[1] for item in set(before_schema) ^ set(after_schema)}
    assert changed == _NEW | {MIGRATION.TRANSITION_NAME, "sqlite_autoindex_chat_reference_primary_continuations_1"}
    with sqlite3.connect(path) as db:
        sql = db.execute("SELECT sql FROM sqlite_schema WHERE name=?", (MIGRATION.TRANSITION_NAME,)).fetchone()[0]
        assert "OLD.status='completed' AND NEW.status='running'" in sql
        assert "continuation.state='started'" in sql
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_a_changed_execution_transition_is_refused(tmp_path):
    path = tmp_path / TARGET_FILES["core"]
    _upgrade(path, MIGRATION.down_revision, "core")
    with sqlite3.connect(path) as db:
        db.execute(f"DROP TRIGGER {MIGRATION.TRANSITION_NAME}")
        db.execute(
            f"CREATE TRIGGER {MIGRATION.TRANSITION_NAME} BEFORE UPDATE ON turn_agent_execution WHEN 0 BEGIN SELECT RAISE(ABORT,'changed'); END"
        )
    with pytest.raises(Exception, match="schema drift"):
        _upgrade(path, MIGRATION.revision, "core")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT 1 FROM sqlite_schema WHERE name='chat_reference_primary_continuations'").fetchone() is None


def test_rollback_restores_a_snapshot_instead_of_downgrading():
    with pytest.raises(NotImplementedError):
        MIGRATION.downgrade()
