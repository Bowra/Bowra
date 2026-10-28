"""Historical memory notification links reach the current personal settings page."""

from __future__ import annotations

import json
import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core.notification_store import NotificationStore
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0158_memory_notification_links")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_migration_changes_no_schema_or_other_database_targets(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, previous_schema = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    if target != "core":
        assert snapshot(path) == before
    assert schema(path) == previous_schema


async def test_upgrade_repairs_only_memory_links_and_preserves_notification_state(tmp_path):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    cases = [
        ("old", "memory.candidate", "/memory", "/settings/memory/"),
        ("suffix", "memory.candidate", "/memory/?item=mem_1#review", "/settings/memory/?item=mem_1#review"),
        ("current", "memory.candidate", "/settings/memory/", "/settings/memory/"),
        ("external", "memory.candidate", "https://example.com/memory", "https://example.com/memory"),
        ("other", "other", "/memory", "/memory"),
    ]
    with sqlite3.connect(path) as db:
        for name, kind, url, _ in cases:
            actions = json.dumps([{"id": "review", "kind": "link", "url": url}])
            db.execute(
                "INSERT INTO notification_items(id,team_id,category,type,title,action_url,actions_json,dedupe_key,state,created_at,updated_at) "
                "VALUES (?,'team_a','memory',?,'Review memory',?,?,?,'open','2026-10-01','2026-10-02')",
                (name, kind, url, actions, name),
            )
            db.execute(
                "INSERT INTO notification_recipients(notification_id,user_id,role,delivered_at,read_at,archived_at) "
                "VALUES (?,'user_a','owner','2026-10-01','2026-10-02',NULL)",
                (name,),
            )
        db.execute("UPDATE notification_items SET state='resolved',resolved_at='2026-10-03',resolved_reason='decided' WHERE id='suffix'")
        db.execute("UPDATE notification_recipients SET archived_at='2026-10-04' WHERE notification_id='suffix'")
        recipients = db.execute("SELECT * FROM notification_recipients ORDER BY notification_id").fetchall()
    _upgrade(path, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        for name, _, _, expected in cases:
            url, actions, title, created, updated, state = db.execute(
                "SELECT action_url,actions_json,title,created_at,updated_at,state FROM notification_items WHERE id=?",
                (name,),
            ).fetchone()
            assert url == expected
            assert json.loads(actions)[0]["url"] == expected
            assert (title, created, updated, state) == (
                "Review memory",
                "2026-10-01",
                "2026-10-02",
                "resolved" if name == "suffix" else "open",
            )
        assert db.execute("SELECT * FROM notification_recipients ORDER BY notification_id").fetchall() == recipients
    _upgrade(path, "head")
    store = NotificationStore(str(path))
    items, _ = await store.list_for_user(user_id="user_a", tab="all", limit=30)
    old = next(item for item in items if item.id == "old")
    assert old.action_url == old.actions[0]["url"] == "/settings/memory/"
    assert old.read_at == "2026-10-02"
    before = snapshot(path)
    _upgrade(path, "head")
    assert snapshot(path) == before
