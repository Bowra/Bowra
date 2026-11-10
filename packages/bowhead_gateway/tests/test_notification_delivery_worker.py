"""The delivery worker rechecks at send time, sends fixed copy, retries and records outcomes."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from importlib import import_module
from types import SimpleNamespace

import pytest
from bowhead_core.notification_service import NotificationService
from bowhead_core.notification_store import NotificationStore
from bowhead_core.product_identity import product_name

from bowhead_gateway import notification_delivery_worker as worker

MIGRATION = import_module("bowhead_core.migrations.versions.0226_notification_deliveries")
LATER = datetime(2999, 1, 1, tzinfo=UTC)


def _run(coro):
    return asyncio.run(coro)


class _Users:
    def __init__(self) -> None:
        self.departed: set[str] = set()
        self.unlisted: set[str] = set()
        self.emails = {"u_admin": "admin@example.com", "u_owner": "owner@example.com"}

    async def get_user_team_id(self, user_id):
        return "team_legacy"

    async def get_workspace_team_id(self, workspace_id):
        return "team_b"

    async def list_team_users_by_roles(self, team_id, roles, *, include_creator=True):
        return ["u_admin"]

    async def filter_active_team_members(self, team_id, user_ids):
        return {uid for uid in user_ids if uid not in self.departed}

    async def get_user(self, user_id):
        return SimpleNamespace(email=self.emails.get(user_id, ""))

    async def team_member_emails(self, team_id):
        return {email for uid, email in self.emails.items() if uid not in self.departed | self.unlisted}


class _Settings:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    async def get(self, user_id, key):
        return self.values.get((user_id, key))


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = NotificationStore(str(tmp_path / "n.db"))
    _run(store.initialize())
    with closing(sqlite3.connect(store.db_path)) as db:
        for statement in (MIGRATION.TABLE, MIGRATION.INDEX, MIGRATION.NOTIFICATION_INDEX):
            db.execute(statement)
        db.commit()
    users = _Users()
    settings = _Settings()
    sent: list[tuple[str, str, str]] = []
    outcome = {"result": (True, None)}

    async def fake_send(to, subject, body, *, team_id=None, store=None):
        sent.append((to, subject, body))
        return outcome["result"]

    smtp = {"configured": True}

    async def fake_smtp(team_id, store):
        return object() if smtp["configured"] else None

    monkeypatch.setattr("bowhead_core.smtp_notifier.send_notification", fake_send)
    monkeypatch.setattr("bowhead_core.email_config.resolve_team_smtp_config", fake_smtp)
    app = SimpleNamespace(state=SimpleNamespace(user_store=users, user_settings_store=settings, public_entry_store=None, scheduled_store=None))
    service = NotificationService(store=store, user_store=users, delivery_enabled=True)
    return SimpleNamespace(store=store, users=users, settings=settings, sent=sent, outcome=outcome, smtp=smtp, app=app, service=service)


def _approval():
    return SimpleNamespace(
        id="apr_1", user_id="u_req", workspace_id="ws_b", status="pending", tool_name="crm_update", risk_level="high", turn_id="t", session_id="s"
    )


def _status(env) -> list[tuple[str, str | None, int]]:
    with closing(sqlite3.connect(env.store.db_path)) as db:
        return db.execute("SELECT status, outcome_code, attempts FROM notification_deliveries ORDER BY id").fetchall()


def _tick(env, now=LATER):
    return _run(worker.deliver_once(env.app, env.store, owner="w1", now=now))


def test_a_reminder_waits_for_its_delay(env):
    _run(env.service.emit_approval_required(_approval()))
    _tick(env, now=datetime.now(UTC) + timedelta(minutes=10))
    assert env.sent == [] and _status(env) == [("pending", None, 0)]


def test_a_still_waiting_approval_emails_the_administrator_with_fixed_copy(env):
    _run(env.service.emit_approval_required(_approval()))

    _tick(env)

    assert _status(env) == [("sent", None, 1)]
    ((to, subject, body),) = env.sent
    assert to == "admin@example.com"
    assert subject == f"[{product_name()}] An approval is waiting for you: crm_update"
    assert f"Open {product_name()} and handle it from your notifications." in body and "Settings → Profile" in body


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("read", "read"),
        ("archive", "archived"),
        ("resolve", "resolved"),
        ("depart", "not_member"),
        ("unlisted_address", "not_member"),
        ("opt_out", "opted_out"),
        ("no_smtp", "smtp_unconfigured"),
    ],
)
def test_send_time_checks_skip_with_their_reason(env, change, code):
    item = _run(env.service.emit_approval_required(_approval()))
    if change == "read":
        _run(env.store.mark_read(notification_id=item.id, user_id="u_admin"))
    elif change == "archive":
        _run(env.store.archive(notification_id=item.id, user_id="u_admin"))
    elif change == "resolve":
        _run(env.store.resolve_by_dedupe(team_id="team_b", dedupe_key="approval.required:apr_1", reason="approved"))
    elif change == "depart":
        env.users.departed.add("u_admin")
    elif change == "unlisted_address":
        # Still a member, but the address is not on the Team's verified member allowlist.
        env.users.unlisted.add("u_admin")
    elif change == "opt_out":
        env.settings.values[("u_admin", worker.REMINDER_SETTING_KEY)] = "off"
    else:
        env.smtp["configured"] = False

    _tick(env)

    assert env.sent == []
    assert _status(env) == [("skipped", code, 0)]


def test_the_personal_switch_does_not_stop_an_email_the_owner_configured(env):
    env.settings.values[("u_owner", worker.REMINDER_SETTING_KEY)] = "off"
    job = SimpleNamespace(id="sj_1", user_id="u_owner", workspace_id="ws_b", name="Daily")
    _run(env.service.emit_routine_alert(job, run_id="t_1", session_id="s_1", level="urgent"))

    _tick(env)

    assert _status(env) == [("sent", None, 1)]
    _to, subject, body = env.sent[0]
    assert subject == f"[{product_name()} routine alert] Daily" and "Daily" in body


def test_transient_failures_retry_on_the_outbox_schedule_then_die(env):
    _run(env.service.emit_approval_required(_approval()))
    env.outcome["result"] = (False, "ConnectionRefusedError: [Errno 61]")

    now = LATER
    for attempt in range(1, 5):
        _tick(env, now=now)
        status, code, attempts = _status(env)[0]
        assert (status, code, attempts) == ("pending", "send_failed", attempt)
        now = now + timedelta(seconds=worker._BACKOFF[attempt] + 1)
    _tick(env, now=now)

    assert _status(env) == [("dead", "retries_exhausted", 5)]
    with closing(sqlite3.connect(env.store.db_path)) as db:
        (ref,) = db.execute("SELECT diagnostic_ref FROM notification_deliveries").fetchone()
    assert ref and ref.startswith("ndg_") and "@" not in ref


def test_a_rejection_is_not_retried(env):
    _run(env.service.emit_approval_required(_approval()))
    env.outcome["result"] = (False, "SMTPRecipientsRefused: {'admin@example.com': (550, b'no')}")

    _tick(env)
    _tick(env, now=LATER + timedelta(days=1))

    assert _status(env) == [("failed", "rejected", 1)]
    assert len(env.sent) == 1


# ── Scheduled results through recorded deliveries ────────────────────────────


def test_a_scheduled_result_becomes_recorded_emails_for_member_addresses_only(env, monkeypatch):
    from bowhead_gateway._scheduled_runner import _deliver_run_result

    direct: list[str] = []

    async def direct_send(to, *args, **kwargs):
        direct.append(to)
        return True, None

    monkeypatch.setattr("bowhead_core.smtp_notifier.send_notification", direct_send)

    async def members(team_id):
        return {"owner@example.com": "u_owner", "admin@example.com": "u_admin"}

    env.users.team_member_ids_by_email = members
    env.app.state.notification_service = env.service
    job = SimpleNamespace(
        id="sj_1",
        user_id="u_owner",
        workspace_id="ws_b",
        name="Daily",
        public_entry_id=None,
        notify_channels={"email": ["Owner@Example.com", "outsider@evil.example"], "deliver_on_success": True},
    )

    _run(_deliver_run_result(env.app, job, summary="Revenue up", session_id="s_1", run_id="t_1", team_id="team_b", locale="en-US"))

    assert direct == [], "the runner no longer sends directly"
    rows = _run(env.store.deliveries_for_dedupe_keys(["routine.delivered:sj_1:t_1"]))
    assert [(r.user_id, r.channel, r.status) for r in rows] == [("u_owner", "email_delivery", "pending")]


class _Jobs:
    def __init__(self, job) -> None:
        self.job = job

    async def get(self, job_id):
        return self.job if job_id == self.job.id else None


def _routine(env, monkeypatch, emails: list[str]):
    """A task emailing its results to ``emails``, and its run reference."""
    from bowhead_core.types import ScheduledRunRef

    async def members(team_id):
        return {"owner@example.com": "u_owner", "admin@example.com": "u_admin"}

    async def english(*args, **kwargs):
        return "en-US"

    monkeypatch.setattr("bowhead_gateway._scheduled_runner._delivery_locale", english)
    env.users.team_member_ids_by_email = members
    job = SimpleNamespace(
        id="sj_1",
        user_id="u_owner",
        workspace_id="ws_b",
        name="Daily",
        public_entry_id=None,
        query="",
        notify_channels={"email": emails, "deliver_on_success": True},
    )
    env.app.state.notification_service = env.service
    env.app.state.scheduled_store = _Jobs(job)
    run = ScheduledRunRef(job_id="sj_1", run_id="t_1", owner_id="u_owner", workspace_id="ws_b", job_name="Daily", session_id="s_1")
    return job, run


def _notify(env, monkeypatch, run, probe=None):
    """The run's Agent calls routine_notify at the urgent level; returns the tool result."""
    from bowhead_core.tool_registry import ToolRegistry
    from bowhead_gateway._scheduled_runner import routine_result_email_reaches_owner

    from bowhead_core import routine_notify_tool

    monkeypatch.setenv("FEATURE_ROUTINE_NOTIFY", "1")
    routine_notify_tool.register_routine_notify_tool(
        ToolRegistry(),
        service_provider=lambda: env.service,
        result_email_reaches_owner=probe or (lambda r: routine_result_email_reaches_owner(env.app, r)),
    )
    return _run(routine_notify_tool._handler({"level": "urgent", "reason": "r"}, SimpleNamespace(scheduled_run=run)))


def _urgent_alert(env, monkeypatch, run, probe=None):
    """The run's Agent calls routine_notify at the urgent level."""
    assert _notify(env, monkeypatch, run, probe)["status"] == "queued"


def _five_earlier_alerts(env, job):
    for i in range(5):
        _run(env.service.emit_routine_alert(job, run_id=f"t_earlier_{i}", session_id="s_0", level="info"))


def _deliver(env, job, summary="Revenue down 30%"):
    from bowhead_gateway._scheduled_runner import _deliver_run_result

    _run(_deliver_run_result(env.app, job, summary=summary, session_id="s_1", run_id="t_1", team_id="team_b", locale="en-US"))


def _alert_rows(env):
    return [(r.status, r.outcome_code) for r in _run(env.store.deliveries_for_dedupe_keys(["routine.alert:sj_1:t_1"]))]


def test_an_urgent_alert_rides_in_the_owners_result_email(env, monkeypatch):
    job, run = _routine(env, monkeypatch, ["Owner@Example.com", "admin@example.com"])

    _urgent_alert(env, monkeypatch, run)
    _tick(env, now=datetime.now(UTC) + timedelta(minutes=1))
    assert env.sent == [], "the alert email waits for the run's result email"

    _deliver(env, job)
    _tick(env)

    by_address = {to: (subject, body) for to, subject, body in env.sent}
    assert sorted(by_address) == ["admin@example.com", "owner@example.com"], "one email each, no separate alert email"
    subject, body = by_address["owner@example.com"]
    assert subject == f"[{product_name()} routine · urgent] Daily"
    assert body.startswith("This run was flagged urgent.") and "Revenue down 30%" in body
    assert by_address["admin@example.com"][0] == f"[{product_name()} routine] Daily", "only the owner's copy carries the alert"
    assert "flagged urgent" not in by_address["admin@example.com"][1]
    assert _alert_rows(env) == [("skipped", "merged_into_result")]


@pytest.mark.parametrize("case", ["owner_not_a_recipient", "probe_failed"])
def test_an_owner_without_a_result_email_gets_the_alert_email_at_once(env, monkeypatch, case):
    emails = ["admin@example.com"] if case == "owner_not_a_recipient" else ["owner@example.com"]
    job, run = _routine(env, monkeypatch, emails)

    async def failing(r):
        raise RuntimeError("scheduled store unavailable")

    _urgent_alert(env, monkeypatch, run, probe=failing if case == "probe_failed" else None)
    _tick(env, now=datetime.now(UTC) + timedelta(seconds=5))

    assert [(to, subject) for to, subject, _ in env.sent] == [("owner@example.com", f"[{product_name()} routine alert] Daily")]
    assert _alert_rows(env) == [("sent", None)]


@pytest.mark.parametrize("end", ["no_result_email", "wait_ended"])
def test_a_held_alert_email_goes_alone_when_no_result_email_carries_it(env, monkeypatch, end):
    from bowhead_core.notification_service import ROUTINE_ALERT_EMAIL_HOLD_S

    job, run = _routine(env, monkeypatch, ["owner@example.com"])
    _urgent_alert(env, monkeypatch, run)

    if end == "no_result_email":
        # The run failed, paused for a question, or its task stopped emailing results.
        job.notify_channels = {}
        _deliver(env, job)
        now = datetime.now(UTC) + timedelta(seconds=5)
    else:
        # The run never reached delivery (for example the process restarted).
        now = datetime.now(UTC) + timedelta(seconds=ROUTINE_ALERT_EMAIL_HOLD_S + 5)
    _tick(env, now=now)

    assert [subject for _to, subject, _ in env.sent] == [f"[{product_name()} routine alert] Daily"]


@pytest.mark.parametrize("called", [True, False])
def test_a_run_whose_alert_the_daily_limit_refused_still_delivers_its_plain_result(env, monkeypatch, called):
    # 116 dogfood N8: in "only when flagged" mode a refused alert also withheld the result, so a
    # condition that stayed true went silent for the rest of the day.
    monkeypatch.setenv("FEATURE_ROUTINE_NOTIFY", "1")
    job, run = _routine(env, monkeypatch, ["owner@example.com"])
    job.notify_channels["deliver"] = "on_alert"
    _five_earlier_alerts(env, job)
    if called:
        out = _notify(env, monkeypatch, run)
        assert out["status"] == "rate_limited" and "do not say the owner was alerted" in out["message"]

    _deliver(env, job)
    _tick(env)

    rows, _ = _run(env.store.list_for_user(user_id="u_owner"))
    if not called:
        assert env.sent == [] and "routine.delivered" not in {r.type for r in rows}, "a run without an alert still delivers nothing"
        return
    # The plain result: no sixth alert in the notifications, no urgent email, no urgent mark.
    assert [(to, subject) for to, subject, _ in env.sent] == [("owner@example.com", f"[{product_name()} routine] Daily")]
    assert "flagged urgent" not in env.sent[0][2]
    assert sorted(r.type for r in rows) == ["routine.alert"] * 5 + ["routine.delivered"]


def test_a_refused_alert_does_not_count_toward_the_daily_limit(env, monkeypatch):
    from bowhead_core.types import ScheduledRunRef

    job, run = _routine(env, monkeypatch, ["owner@example.com"])
    _five_earlier_alerts(env, job)
    assert _notify(env, monkeypatch, run)["status"] == "rate_limited"
    # The oldest alert leaves the 24-hour window: four alerts reached the owner in it.
    stale = (datetime.now(UTC) - timedelta(hours=25)).isoformat(timespec="seconds")
    with closing(sqlite3.connect(env.store.db_path)) as db:
        db.execute("UPDATE notification_items SET created_at=? WHERE dedupe_key='routine.alert:sj_1:t_earlier_0'", (stale,))
        db.commit()

    later = ScheduledRunRef(job_id="sj_1", run_id="t_2", owner_id="u_owner", workspace_id="ws_b", job_name="Daily", session_id="s_2")
    assert _notify(env, monkeypatch, later)["status"] == "queued"


def test_run_history_shows_each_runs_delivery_outcome_without_addresses(env):
    from bowhead_gateway.routes.scheduled import ScheduledJobRunDTO, _attach_deliveries

    job = SimpleNamespace(id="sj_1", user_id="u_owner", workspace_id="ws_b", name="Daily", failure_count=0)
    _run(env.service.emit_routine_delivered(job, run_id="t_1", session_id="s_1", summary="done", email_user_ids=["u_owner"]))
    env.smtp["configured"] = False
    _tick(env)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(notification_service=env.service, notification_store=env.store)))
    runs = [
        ScheduledJobRunDTO(id="r1", job_id="sj_1", turn_id="t_1", started_at="t", status="success"),
        ScheduledJobRunDTO(id="r2", job_id="sj_1", turn_id="t_2", started_at="t", status="success"),
    ]

    _run(_attach_deliveries(request, "sj_1", runs))

    assert [d.model_dump() for d in runs[0].deliveries] == [
        {"kind": "result", "status": "skipped", "outcome_code": "smtp_unconfigured", "diagnostic_ref": None, "attempts": 0}
    ]
    assert runs[1].deliveries == []
    assert "@" not in runs[0].model_dump_json()


def test_the_reminder_setting_accepts_only_on_or_off():
    from bowhead_gateway.routes.user_settings import _validate_setting_value
    from fastapi import HTTPException

    assert _validate_setting_value("notification_email_reminders", "off") == "off"
    with pytest.raises(HTTPException):
        _validate_setting_value("notification_email_reminders", "sometimes")
