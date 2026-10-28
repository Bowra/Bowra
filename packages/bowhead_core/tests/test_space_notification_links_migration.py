"""Stored notification data repairs preserve delivery and processing state."""

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

MIGRATION = import_module("bowhead_core.migrations.versions.0098_space_notification_links")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_migration_preserves_schema_and_other_database_targets(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, previous = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    # `0098` is a pure data repair: within this step, rows and DDL of every target database must remain byte-for-byte unchanged.
    # Compare at `MIGRATION.revision`, not head: later revisions can legitimately use copy-migrate / expand-contract under `数据库迁移.md`
    # to rebuild or retire other tables (e.g. `0122` rebuilds
    # `team_builtin_tool` and removes old Power BI tables); that is unrelated to `0098`. The data repaired by `0098`
    # remains unchanged by later revisions, independently enforced at head by the next case.
    assert snapshot(path) == before
    assert schema(path) == previous


async def test_upgrade_repairs_notification_links_and_preserves_recipient_state(tmp_path):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    cases = [
        ("old", "space_assignment_requested", "/team/settings/workspaces?from=notification", "/team/settings/spaces/?from=notification"),
        (
            "tab",
            "space_assignment_requested",
            "/team/settings/?tab=workspaces&from=notification#members",
            "/team/settings/spaces/?from=notification#members",
        ),
        ("current", "space_assignment_requested", "/team/settings/spaces/?from=notification", "/team/settings/spaces/?from=notification"),
        ("external", "space_assignment_requested", "https://example.com/team/settings/workspaces", "https://example.com/team/settings/workspaces"),
        ("other", "other", "/team/settings/workspaces", "/team/settings/workspaces"),
    ]
    with sqlite3.connect(path) as db:
        for name, kind, url, _ in cases:
            actions = json.dumps([{"id": "review", "label": "处理", "kind": "link", "url": url}])
            db.execute(
                "INSERT INTO notification_items(id,team_id,category,type,title,action_url,actions_json,dedupe_key,state,created_at,updated_at) "
                "VALUES (?,'team_a','space_assignment',?,'有成员需要分配空间',?,?,?,'open','2026-08-28','2026-08-28')",
                (name, kind, url, actions, name),
            )
            db.execute(
                "INSERT INTO notification_recipients(notification_id,user_id,role,delivered_at,read_at,archived_at) "
                "VALUES (?,'user_a','owner','2026-08-28','2026-08-29','2026-08-30')",
                (name,),
            )
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
            assert (title, created, updated, state) == ("有成员需要分配空间", "2026-08-28", "2026-08-28", "open")
        assert db.execute("SELECT * FROM notification_recipients ORDER BY notification_id").fetchall() == recipients
    store = NotificationStore(str(path))
    with sqlite3.connect(path) as db:
        db.execute("UPDATE notification_recipients SET archived_at=NULL WHERE notification_id='old'")
    items, _ = await store.list_for_user(user_id="user_a", tab="all", limit=30)
    assert items[0].action_url == "/team/settings/spaces/?from=notification"
    assert items[0].actions[0]["url"] == items[0].action_url
    repaired = ("notification_items", "notification_recipients")
    after = {name: rows for name, rows in snapshot(path).items() if name in repaired}
    assert set(after) == set(repaired)
    _upgrade(path, "head")
    # This assertion proves that later revisions do not rewrite the links and recipient state repaired by `0098`,
    # rather than forbidding later revisions from rebuilding or retiring other tables (`0122` legitimately removes old Power BI tables).
    assert {name: rows for name, rows in snapshot(path).items() if name in repaired} == after


def test_failed_data_repair_rolls_back_all_changes(tmp_path, monkeypatch):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        for name in ("first", "second"):
            db.execute(
                "INSERT INTO notification_items(id,team_id,category,type,title,action_url,dedupe_key,created_at,updated_at) "
                "VALUES (?,'team_a','space_assignment','space_assignment_requested','Test','/team/settings/workspaces',?,'2026-08-28','2026-08-28')",
                (name, name),
            )
    before = snapshot(path)
    original = Connection.exec_driver_sql
    updates = 0

    def fail(self, statement, *args, **kwargs):
        nonlocal updates
        result = original(self, statement, *args, **kwargs)
        if statement.startswith("UPDATE notification_items"):
            updates += 1
            if updates == 2:
                raise RuntimeError("injected notification repair failure")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fail)
    with pytest.raises(RuntimeError, match="injected notification repair failure"):
        _upgrade(path, MIGRATION.revision)
    assert updates == 2
    assert snapshot(path) == before
