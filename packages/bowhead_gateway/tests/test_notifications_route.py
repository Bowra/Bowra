from typing import Any

import pytest
from bowhead_core.notification_store import ACTION_REQUIRED_TYPES, NotificationForUser
from bowhead_gateway.routes.notifications import (
    _to_dto,
)


def test_notification_dto_normalizes_legacy_scheduled_links() -> None:
    notification = NotificationForUser(
        id="n_1",
        team_id="team_1",
        category="async_job",
        type="scheduled.failed",
        priority="p1",
        title="Scheduled job failed",
        body=None,
        subject_type="scheduled_job",
        subject_id="job_1",
        actor_user_id=None,
        action_url="/settings/scheduled",
        actions=[{"id": "view", "label": "View", "kind": "link", "url": "/settings/scheduled"}],
        metadata={},
        dedupe_key="scheduled.failed:job_1",
        state="open",
        resolved_at=None,
        resolved_reason=None,
        created_at="2026-07-17T00:00:00+00:00",
        updated_at="2026-07-17T00:00:00+00:00",
        recipient_user_id="user_1",
        recipient_role="owner",
        delivered_at="2026-07-17T00:00:00+00:00",
        read_at=None,
        archived_at=None,
    )

    dto = _to_dto(notification)

    assert dto.actionUrl == "/team/settings/scheduled/"
    assert dto.actions[0].url == "/team/settings/scheduled/"


@pytest.mark.parametrize(("stored", "expected"), [("normal", "p2"), ("", "p2"), ("p1", "p1"), ("p2", "p2"), ("p3", "p3")])
def test_notification_dto_reads_legacy_priorities_in_shared_vocabulary(stored: str, expected: str) -> None:
    notification = NotificationForUser(
        id="n_2",
        team_id="team_1",
        category="share",
        type="skill_approval_submitted",
        priority=stored,
        title="Skill submitted",
        body=None,
        subject_type="team_skill",
        subject_id="sk_1",
        actor_user_id="user_2",
        action_url="/team/settings/workflows",
        actions=[],
        metadata={},
        dedupe_key="skill_approval:submitted:sk_1:2026-07-17T00:00:00+00:00",
        state="open",
        resolved_at=None,
        resolved_reason=None,
        created_at="2026-07-17T00:00:00+00:00",
        updated_at="2026-07-17T00:00:00+00:00",
        recipient_user_id="user_1",
        recipient_role=None,
        delivered_at="2026-07-17T00:00:00+00:00",
        read_at=None,
        archived_at=None,
    )

    dto = _to_dto(notification)

    # Rows stored before routes used p1/p2/p3 read like any other ordinary update.
    assert dto.priority == expected
    assert dto.actionUrl == "/team/settings/workflows/"


@pytest.mark.parametrize("type_", sorted(ACTION_REQUIRED_TYPES) + ["routine.delivered", "unknown.update"])
@pytest.mark.parametrize("state", ["open", "resolved"])
def test_notification_action_projection_uses_server_classification(type_: str, state: str) -> None:
    from dataclasses import fields

    # The stored dataclass contains heterogeneous scalar, collection and optional fields.
    values: dict[str, Any] = {field.name: None for field in fields(NotificationForUser)}
    values.update(
        id="n",
        team_id="team",
        category="test",
        type=type_,
        priority="p2",
        title="Title",
        actions=[],
        metadata={},
        dedupe_key="key",
        state=state,
        created_at="now",
        updated_at="now",
        recipient_user_id="user",
        delivered_at="now",
    )
    row = NotificationForUser(**values)
    assert _to_dto(row).actionRequired is (type_ in ACTION_REQUIRED_TYPES and state == "open")


# ── FEATURE_NOTIFICATION_REACH: browser candidates and Team labels ─────────────


class _Users:
    def __init__(self, teams: list[dict[str, str]]) -> None:
        self.teams = teams

    async def list_active_teams_for_user(self, user_id):
        return self.teams

    async def get_team_member_labels(self, team_id, user_ids):
        return {}


def _client(tmp_path, monkeypatch, *, reach: bool, multi_team: bool = True, teams=None):
    import asyncio
    from types import SimpleNamespace

    from bowhead_core.notification_store import NotificationStore
    from bowhead_gateway.routes import notifications as notifications_route
    from bowhead_gateway.routes.auth import _current_user
    from bowhead_gateway.routes.auth import _store as user_store_dep
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("FEATURE_NOTIFICATION_REACH", "1" if reach else "0")
    monkeypatch.setenv("FEATURE_MULTI_TEAM_AUTHZ", "1" if multi_team else "0")
    store = NotificationStore(str(tmp_path / "n.db"))
    asyncio.run(store.initialize())
    app = FastAPI()
    app.include_router(notifications_route.router, prefix="/api/v1/notifications")
    app.state.notification_store = store
    users = _Users(teams if teams is not None else [{"id": "team_a", "name": "Team A"}, {"id": "team_b", "name": "Team B"}])
    app.dependency_overrides[_current_user] = lambda: SimpleNamespace(id="u_1", team_id="team_a")
    app.dependency_overrides[user_store_dep] = lambda: users
    return TestClient(app), store


def _put(store, *, key: str, type_: str, priority: str, team_id: str = "team_a", user_id: str = "u_1"):
    import asyncio

    return asyncio.run(
        store.write(
            team_id=team_id,
            category="test",
            type=type_,
            priority=priority,
            title="t",
            body=None,
            subject_type="approval",
            subject_id=key,
            actor_user_id=None,
            action_url="/admin/approvals/",
            actions=[{"id": "review", "label": "Review", "kind": "link", "url": "/admin/approvals/"}],
            metadata={},
            dedupe_key=key,
            recipients=[(user_id, None)],
        )
    ).item


def test_browser_candidates_are_off_without_the_switch(tmp_path, monkeypatch):
    client, store = _client(tmp_path, monkeypatch, reach=False)
    _put(store, key="approval.required:a1", type_="approval.required", priority="p1")

    assert client.get("/api/v1/notifications/browser-candidates").status_code == 404
    assert client.get("/api/v1/notifications/unread-count").json()["latestBrowserCandidateAt"] is None


def test_browser_candidates_are_only_unread_urgent_open_action_items_after_since(tmp_path, monkeypatch):
    import asyncio

    client, store = _client(tmp_path, monkeypatch, reach=True)
    urgent = _put(store, key="approval.required:a1", type_="approval.required", priority="p1")
    _put(store, key="skill_approval:submitted:s1:t", type_="skill_approval_submitted", priority="p2")
    _put(store, key="approval.execution_failed:a2", type_="approval.execution_failed", priority="p1")
    _put(store, key="approval.required:other", type_="approval.required", priority="p1", user_id="u_2")
    read = _put(store, key="approval.required:a3", type_="approval.required", priority="p1")
    asyncio.run(store.mark_read(notification_id=read.id, user_id="u_1"))
    resolved = _put(store, key="approval.required:a4", type_="approval.required", priority="p1")
    asyncio.run(store.resolve_by_dedupe(team_id="team_a", dedupe_key="approval.required:a4", reason="approved"))
    assert resolved.id != urgent.id

    latest = client.get("/api/v1/notifications/unread-count").json()["latestBrowserCandidateAt"]
    body = client.get("/api/v1/notifications/browser-candidates").json()

    assert [row["id"] for row in body["items"]] == [urgent.id]
    assert latest == body["items"][0]["deliveredAt"]
    later = client.get("/api/v1/notifications/browser-candidates", params={"since": latest}).json()
    assert later["items"] == []


def test_items_name_their_team_only_for_members_of_several_teams(tmp_path, monkeypatch):
    client, store = _client(tmp_path, monkeypatch, reach=True)
    _put(store, key="approval.required:b1", type_="approval.required", priority="p1", team_id="team_b")
    _put(store, key="memory.candidate:u_1", type_="memory.candidate", priority="p2", team_id="team_b")

    items = {row["type"]: row for row in client.get("/api/v1/notifications").json()["items"]}

    assert items["approval.required"]["teamName"] == "Team B"
    assert items["approval.required"]["currentTeam"] is False
    # A personal item lives in the user's own namespace: no Team label and no switch.
    assert items["memory.candidate"]["scope"] == "personal"
    assert items["memory.candidate"]["teamName"] is None
    assert items["memory.candidate"]["currentTeam"] is True


def test_no_team_labels_for_a_single_team_or_without_multi_team(tmp_path, monkeypatch):
    for kwargs in ({"teams": [{"id": "team_a", "name": "Team A"}]}, {"multi_team": False}):
        client, store = _client(tmp_path / str(len(kwargs)) / next(iter(kwargs)), monkeypatch, reach=True, **kwargs)
        _put(store, key="approval.required:a1", type_="approval.required", priority="p1")
        row = client.get("/api/v1/notifications").json()["items"][0]
        assert row["teamName"] is None and row["currentTeam"] is True
