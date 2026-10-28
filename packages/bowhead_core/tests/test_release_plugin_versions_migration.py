"""Migration 0159 records release-time Plugin versions on release candidates."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0159_release_plugin_versions")
#: The frozen `1.0.0` baseline revision that existing deployments adopt before later revisions.
BASELINE = import_module("bowhead_core.migrations.versions.0001_initial")
TRIGGER = "trg_team_subagent_release_candidate_plugin_versions_immutable"
pytestmark = pytest.mark.integration


def _columns(path) -> list[str]:
    with sqlite3.connect(path) as db:
        return [row[1] for row in db.execute("PRAGMA table_info(team_subagent_release_candidate)")]


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_adds_the_column_and_guard_only_to_core(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before = snapshot(path)
    previous = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    installed = schema(path)
    if target == "core":
        assert _columns(path)[-1] == "plugin_versions_json"
        added = {row[1] for row in set(installed) - set(previous)}
        assert added == {"team_subagent_release_candidate", TRIGGER}
    else:
        assert installed == previous
    assert snapshot(path) == before
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == installed
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)


def test_empty_database_and_the_1_0_0_baseline_both_reach_head_with_the_column(tmp_path):
    for name, start in (("empty.db", None), ("baseline.db", BASELINE.revision)):
        path = tmp_path / name
        if start is not None:
            _upgrade(path, start)
        _upgrade(path, "head")
        assert "plugin_versions_json" in _columns(path)
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (TRIGGER,)).fetchone() == (1,)
