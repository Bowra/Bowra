"""FEATURE_ROUTINE_NOTIFY in the scheduled runner: "only when flagged" delivery and the urgent email."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.types import ScheduledRunRef
from bowhead_gateway._scheduled_runner import _deliver_run_result, send_routine_alert_email
from bowhead_gateway.routes.scheduled import _checked_notify_channels
from fastapi import HTTPException

from bowhead_gateway import _scheduled_runner

pytestmark = pytest.mark.asyncio


class _Notifications:
    def __init__(self, flagged: bool) -> None:
        self.flagged = flagged
        self.delivered = 0

    async def routine_alert_raised(self, job, *, run_id):
        return self.flagged

    async def emit_routine_delivered(self, *args, **kwargs):
        self.delivered += 1


def _job(deliver: str | None):
    channels = {"deliver_on_success": True, **({"deliver": deliver} if deliver else {})}
    return SimpleNamespace(id="sj_1", user_id="u_owner", workspace_id="ws_b", name="Daily sales", public_entry_id=None, notify_channels=channels)


async def _deliver(job, notifications):
    app = SimpleNamespace(state=SimpleNamespace(public_entry_store=None, notification_service=notifications))
    await _deliver_run_result(app, job, summary="done", session_id="s_1", run_id="t_sched_1", team_id="team_b", locale="en-US")


@pytest.mark.parametrize(
    ("switch", "deliver", "flagged", "delivered"),
    [
        ("1", "on_alert", False, 0),  # only when flagged, nothing flagged: no delivery
        ("1", "on_alert", True, 1),
        ("1", "always", False, 1),
        ("1", None, False, 1),  # a task saved before the mode existed delivers every run
        ("0", "on_alert", False, 1),  # switch off: the mode is ignored, so turning it off never silences a task
    ],
)
async def test_delivery_mode(monkeypatch, switch, deliver, flagged, delivered):
    monkeypatch.setenv("FEATURE_ROUTINE_NOTIFY", switch)
    notifications = _Notifications(flagged)

    await _deliver(_job(deliver), notifications)

    assert notifications.delivered == delivered


async def test_an_unknown_alert_state_still_delivers(monkeypatch):
    monkeypatch.setenv("FEATURE_ROUTINE_NOTIFY", "1")

    class Broken(_Notifications):
        async def routine_alert_raised(self, job, *, run_id):
            raise RuntimeError("store unavailable")

    notifications = Broken(False)
    await _deliver(_job("on_alert"), notifications)
    assert notifications.delivered == 1


def test_an_unknown_delivery_mode_is_rejected_with_its_allowed_values():
    assert _checked_notify_channels({"deliver": "on_alert"}) == {"deliver": "on_alert"}
    assert _checked_notify_channels(None) is None
    with pytest.raises(HTTPException) as exc:
        _checked_notify_channels({"deliver": "sometimes"})
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "scheduled_notify_deliver_invalid"
    assert "on_alert" in exc.value.detail["message"]


class _Users:
    def __init__(self, members: set[str]) -> None:
        self.members = members

    async def get_workspace_team_id(self, workspace_id):
        return "team_b" if workspace_id == "ws_b" else None

    async def get_user(self, user_id):
        return SimpleNamespace(email="Owner@Example.com")

    async def team_member_emails(self, team_id):
        return self.members if team_id == "team_b" else set()


class _Jobs:
    def __init__(self, job) -> None:
        self.job = job

    async def get(self, job_id):
        return self.job if job_id == self.job.id else None


def _alert_app(job, members: set[str]):
    return SimpleNamespace(state=SimpleNamespace(user_store=_Users(members), scheduled_store=_Jobs(job), public_entry_store=None))


_RUN = ScheduledRunRef(job_id="sj_1", run_id="t_sched_1", owner_id="u_owner", workspace_id="ws_b", job_name="Daily sales", session_id="s_1")


async def test_the_urgent_email_goes_to_the_verified_owner_with_fixed_copy(monkeypatch):
    sent: list[tuple[str, str, str]] = []

    async def fake_send(to, subject, body, *, team_id=None, store=None):
        sent.append((to, subject, body))
        return True, None

    async def english(*args, **kwargs):
        return "en-US"

    monkeypatch.setattr("bowhead_core.smtp_notifier.send_notification", fake_send)
    monkeypatch.setattr(_scheduled_runner, "_delivery_locale", english)

    await send_routine_alert_email(_alert_app(_job(None), {"owner@example.com"}), _RUN, item=None)

    assert len(sent) == 1
    to, subject, body = sent[0]
    assert to == "Owner@Example.com"
    from bowhead_core.product_identity import product_name

    assert subject == f"[{product_name()} routine alert] Daily sales"
    assert "Daily sales" in body and f"Open {product_name()} and check your notifications" in body


async def test_no_urgent_email_when_the_owner_is_not_a_member_of_the_tasks_team(monkeypatch):
    sent: list[str] = []

    async def fake_send(to, subject, body, *, team_id=None, store=None):
        sent.append(to)
        return True, None

    monkeypatch.setattr("bowhead_core.smtp_notifier.send_notification", fake_send)

    await send_routine_alert_email(_alert_app(_job(None), {"someone-else@example.com"}), _RUN, item=None)
    other_owner = ScheduledRunRef(job_id="sj_1", run_id="t", owner_id="u_other", workspace_id="ws_b", job_name="x", session_id="s")
    await send_routine_alert_email(_alert_app(_job(None), {"owner@example.com"}), other_owner, item=None)

    assert sent == []
