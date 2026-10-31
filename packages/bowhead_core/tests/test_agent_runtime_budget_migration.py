"""Runtime budget payload compatibility does not rewrite existing state."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0200_agent_runtime_budget")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_runtime_budget_upgrade_preserves_data_and_schema(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, previous = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == before
    assert schema(path) == previous
    _upgrade(path, MIGRATION.revision, target)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
