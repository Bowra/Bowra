"""Recorded notification emails (FEATURE_NOTIFICATION_DELIVERY): the revision and the write path."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from importlib import import_module

import aiosqlite
import pytest
from bowhead_core.notification_service import NotificationService
from bowhead_core.notification_store import NotificationStore

MIGRATION = import_module("bowhead_core.migrations.versions.0226_notification_deliveries")


def _run(coro):
    return asyncio.run(coro)


def _store_with_deliveries(tmp_path) -> NotificationStore:
    """A legacy-layout store plus the revision's table: what a migrated notifications file holds."""
    store = NotificationStore(str(tmp_path / "n.db"))
    _run(store.initialize())
    with closing(sqlite3.connect(store.db_path)) as db:
        for statement in (MIGRATION.TABLE, MIGRATION.INDEX, MIGRATION.NOTIFICATION_INDEX):
            db.execute(statement)
        db.commit()
    return store


def _rows(store: NotificationStore) -> list[tuple]:
    with closing(sqlite3.connect(store.db_path)) as db:
        return db.execute("SELECT user_id, channel, status, not_before, attempts FROM notification_deliveries ORDER BY user_id, channel").fetchall()


class _Users:
    def __init__(self, departed: set[str] | None = None) -> None:
        self.departed = departed or set()

    async def get_user_team_id(self, user_id):
        return "team_legacy"

    async def get_workspace_team_id(self, workspace_id):
        return "team_b"

    async def list_team_users_by_roles(self, team_id, roles, *, include_creator=True):
        return ["u_admin"]

    async def filter_active_team_members(self, team_id, user_ids):
        return {uid for uid in user_ids if uid not in self.departed}


def test_fresh_upgrade_adds_the_table_to_the_notifications_file_only(tmp_path):
    from bowhead_cli.migrate import upgrade
    from bowhead_core.account_erasure import _POLICY
    from bowhead_core.sqlite_domains import DOMAIN_TARGET_FILES

    core = tmp_path / "sqlite" / "bowhead.db"
    upgrade(f"sqlite:///{core}")

    for domain, filename in DOMAIN_TARGET_FILES.items():
        with closing(sqlite3.connect(core.with_name(filename))) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            assert ("notification_deliveries" in tables) is (domain == "notifications"), domain
    with closing(sqlite3.connect(core.with_name(DOMAIN_TARGET_FILES["notifications"]))) as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(notification_deliveries)")]
        objects = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE tbl_name='notification_deliveries'")}
    # Account erasure reviews the exact column list.
    assert columns == _POLICY["tables"]["notification_deliveries"]["columns"]
    assert {"idx_notification_deliveries_due", "idx_notification_deliveries_item", "trg_account_retired_notification_deliveries_insert"} <= objects


def test_a_retired_account_cannot_receive_a_delivery_row(tmp_path):
    from bowhead_cli.migrate import upgrade
    from bowhead_core.sqlite_domain_runtime import connect_sync

    core = tmp_path / "sqlite" / "bowhead.db"
    upgrade(f"sqlite:///{core}")
    with closing(connect_sync(str(core))) as db:
        db.execute(
            "INSERT INTO account_deletions(user_id,operation_id,requested_at,purge_after,time_source) VALUES('u_gone','op_1','t','2099','request')"
        )
        db.commit()

    store = NotificationStore(str(core))
    _run(store.initialize())
    item = _run(
        store.write(
            team_id="team_1",
            category="approval",
            type="approval.required",
            priority="p1",
            title="t",
            body=None,
            subject_type="approval",
            subject_id="apr_1",
            actor_user_id=None,
            action_url=None,
            actions=None,
            metadata={},
            dedupe_key="approval.required:apr_1",
            recipients=[("u_admin", None)],
            escalate_after_s=1800,
        )
    ).item
    from bowhead_core.sqlite_domains import domain_database_path

    with (
        closing(connect_sync(domain_database_path(str(core), "notifications"))) as db,
        pytest.raises(sqlite3.DatabaseError, match="account is deleted"),
    ):
        db.execute(
            "INSERT INTO notification_deliveries(id,notification_id,user_id,channel,status,not_before,created_at,updated_at) "
            "VALUES('ndl_x',?,'u_gone','email_escalation','pending','t','t','t')",
            (item.id,),
        )


def test_without_the_switch_no_delivery_rows_are_written(tmp_path):
    store = _store_with_deliveries(tmp_path)
    svc = NotificationService(store=store, user_store=_Users())

    _run(svc.emit_approval_required(_approval()))

    assert _rows(store) == []


def _approval(approval_id: str = "apr_1"):
    from types import SimpleNamespace

    return SimpleNamespace(
        id=approval_id,
        user_id="u_req",
        workspace_id="ws_b",
        status="pending",
        tool_name="crm_update",
        risk_level="high",
        turn_id="t_1",
        session_id="ses_1",
    )


def test_a_waiting_approval_schedules_one_reminder_per_administrator_after_thirty_minutes(tmp_path, monkeypatch):
    import bowhead_core.notification_store as module

    monkeypatch.setattr(module, "_now", lambda: "2026-11-08T09:00:00+00:00")
    store = _store_with_deliveries(tmp_path)
    svc = NotificationService(store=store, user_store=_Users(), delivery_enabled=True)

    _run(svc.emit_approval_required(_approval()))
    _run(svc.emit_approval_required(_approval()))  # the startup backfill re-emits: still one row

    assert _rows(store) == [("u_admin", "email_escalation", "pending", "2026-11-08T09:30:00+00:00", 0)]


def test_a_reopened_occurrence_starts_its_reminder_over(tmp_path, monkeypatch):
    import bowhead_core.notification_store as module

    clock = {"t": "2026-11-08T09:00:00+00:00"}
    monkeypatch.setattr(module, "_now", lambda: clock["t"])
    store = _store_with_deliveries(tmp_path)
    write = {
        "team_id": "team_b",
        "category": "async_job",
        "type": "routine.needs_input",
        "priority": "p1",
        "title": "t",
        "body": None,
        "subject_type": "scheduled_job",
        "subject_id": "sj_1",
        "actor_user_id": None,
        "action_url": None,
        "actions": None,
        "metadata": {},
        "dedupe_key": "routine.needs_input:sj_1:run_1",
        "recipients": [("u_owner", "owner")],
        "escalate_after_s": 1800,
        "reopen_resolved": True,
    }
    _run(store.write(**write))
    with closing(sqlite3.connect(store.db_path)) as db:
        db.execute("UPDATE notification_deliveries SET status='sent', attempts=1")
        db.commit()
    _run(store.resolve_by_dedupe(team_id="team_b", dedupe_key="routine.needs_input:sj_1:run_1", reason="answered"))
    clock["t"] = "2026-11-09T09:00:00+00:00"
    _run(store.write(**write))

    assert _rows(store) == [("u_owner", "email_escalation", "pending", "2026-11-09T09:30:00+00:00", 0)]


def test_configured_emails_go_only_to_active_members_on_a_new_occurrence(tmp_path):
    from types import SimpleNamespace

    store = _store_with_deliveries(tmp_path)
    svc = NotificationService(store=store, user_store=_Users(departed={"u_left"}), delivery_enabled=True)
    job = SimpleNamespace(id="sj_1", user_id="u_owner", workspace_id="ws_b", name="Daily", failure_count=0)

    _run(svc.emit_routine_delivered(job, run_id="t_1", session_id="s_1", summary="done", email_user_ids=["u_owner", "u_peer", "u_left"]))
    with closing(sqlite3.connect(store.db_path)) as db:
        db.execute("UPDATE notification_deliveries SET status='sent', attempts=1")
        db.commit()
    # A repeated emit of the same run is not a new occurrence: nothing is sent again.
    _run(svc.emit_routine_delivered(job, run_id="t_1", session_id="s_1", summary="done", email_user_ids=["u_owner", "u_peer"]))

    assert [(r[0], r[1], r[2]) for r in _rows(store)] == [("u_owner", "email_delivery", "sent"), ("u_peer", "email_delivery", "sent")]


def test_an_urgent_alert_schedules_one_immediate_email_to_the_owner(tmp_path):
    from types import SimpleNamespace

    store = _store_with_deliveries(tmp_path)
    svc = NotificationService(store=store, user_store=_Users(), delivery_enabled=True)
    job = SimpleNamespace(id="sj_1", user_id="u_owner", workspace_id="ws_b", name="Daily")

    _run(svc.emit_routine_alert(job, run_id="t_1", session_id="s_1", level="attention"))
    assert _rows(store) == []
    _run(svc.emit_routine_alert(job, run_id="t_2", session_id="s_1", level="urgent"))
    assert [(r[0], r[1], r[2]) for r in _rows(store)] == [("u_owner", "email_delivery", "pending")]


def test_lease_and_settle(tmp_path):
    store = _store_with_deliveries(tmp_path)
    svc = NotificationService(store=store, user_store=_Users(), delivery_enabled=True)
    from types import SimpleNamespace

    job = SimpleNamespace(id="sj_1", user_id="u_owner", workspace_id="ws_b", name="Daily", failure_count=0)
    _run(svc.emit_routine_delivered(job, run_id="t_1", session_id="s_1", summary="done", email_user_ids=["u_owner"]))

    leased = _run(store.lease_due_deliveries(owner="w1", now="2999-01-01T00:00:00+00:00", lease_until="2999-01-01T00:05:00+00:00"))
    assert len(leased) == 1
    # A second worker sees nothing while the lease holds.
    assert _run(store.lease_due_deliveries(owner="w2", now="2999-01-01T00:01:00+00:00", lease_until="2999-01-01T00:06:00+00:00")) == []
    assert _run(store.settle_delivery(delivery_id=leased[0].id, owner="w2", status="sent")) is False
    assert _run(store.settle_delivery(delivery_id=leased[0].id, owner="w1", status="sent")) is True
    rows = _run(store.deliveries_for_dedupe_keys(["routine.delivered:sj_1:t_1"]))
    assert [(r.status, r.attempts, r.item_dedupe_key) for r in rows] == [("sent", 1, "routine.delivered:sj_1:t_1")]


def test_rows_are_removed_with_their_item(tmp_path):
    store = _store_with_deliveries(tmp_path)
    svc = NotificationService(store=store, user_store=_Users(), delivery_enabled=True)
    _run(svc.emit_approval_required(_approval()))

    async def delete_items() -> int:
        async with aiosqlite.connect(store.db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("DELETE FROM notification_items")
            await db.commit()
            async with db.execute("SELECT COUNT(*) FROM notification_deliveries") as cur:
                return (await cur.fetchone())[0]

    assert len(_rows(store)) == 1
    assert _run(delete_items()) == 0


def test_the_member_allowlist_follows_team_membership_not_the_legacy_team(tmp_path):
    """An email allowlist keyed on ``users.team_id`` dropped a multi-Team member from every Team but
    their default one, and kept a departed member in it."""
    from bowhead_core.user_store import UserStore

    async def scenario():
        users = UserStore(tmp_path / "u.db")
        await users.initialize()
        team_a = await users.create_team(name="A", max_seats=10)
        team_b = await users.create_team(name="B", max_seats=10)
        both = await users.create_user(team_id=team_a.id, email="Both@Example.com", password="Passw0rd!")
        left = await users.create_user(team_id=team_b.id, email="left@example.com", password="Passw0rd!")
        async with aiosqlite.connect(users.db_path) as db:
            await db.execute(
                "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES ('tm_x',?,?,'member','active','t')",
                (both.id, team_b.id),
            )
            await db.execute("UPDATE team_members SET status='removed' WHERE user_id=? AND team_id=?", (left.id, team_b.id))
            await db.commit()
        return both, await users.team_member_ids_by_email(team_b.id), await users.team_member_emails(team_b.id)

    both, by_email, emails = _run(scenario())
    assert by_email == {"both@example.com": both.id}
    assert emails == {"both@example.com"}
