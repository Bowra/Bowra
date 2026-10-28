"""Slack connection schema is an additive core migration."""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade

MIGRATION = import_module("bowhead_core.migrations.versions.0091_slack_app_settings")


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_slack_schema_upgrades_each_target_once(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.revision, target)
    _upgrade(path, MIGRATION.revision, target)
    with sqlite3.connect(path) as db:
        objects = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {"slack_connections", "slack_oauth_states", "slack_identity_bindings", "slack_workspace_bindings", "slack_event_dedup"}
    assert expected <= objects if target == "core" else not (expected & objects)
    if target == "core":
        with sqlite3.connect(path) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(slack_connections)")}
        assert "slack_app_id" in columns
