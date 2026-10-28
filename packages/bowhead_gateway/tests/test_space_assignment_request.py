from __future__ import annotations

import time

import jwt
import pytest
from bowhead_core.notification_service import NotificationService
from bowhead_core.notification_store import NotificationStore
from bowhead_core.user_store import UserStore
from fastapi import FastAPI
from fastapi.testclient import TestClient

SECRET = "test-secret-long-enough-for-hs256-32-bytes"


@pytest.fixture
async def stores(tmp_path):
    db_path = tmp_path / "space-assignment-request.db"
    users = UserStore(db_path)
    notifications = NotificationStore(str(db_path))
    await users.initialize()
    await notifications.initialize()
    return users, notifications


@pytest.fixture
def client(stores, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    users, notifications = stores
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", SECRET)
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    from bowhead_gateway.routes import admin_members, auth

    app = FastAPI()
    app.state.user_store = users
    app.state.notification_store = notifications
    app.state.notification_service = NotificationService(
        store=notifications,
        user_store=users,
    )

    class Settings:
        auth_mode = "prod"
        dev_default_user = "dev_user"

    app.state.settings = Settings()
    app.include_router(auth.router, prefix="/api/v1/auth")
    app.include_router(admin_members.router)
    return TestClient(app)


def _token(user_id: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": user_id, "iat": now, "exp": now + 3600, "pwd_v": 0},
        SECRET,
        algorithm="HS256",
    )


async def _seed(stores) -> tuple[str, str, str]:
    users, _ = stores
    team = await users.create_team(name="Request Team", max_seats=20)
    owner = await users.create_user(
        team_id=team.id,
        email="owner@example.com",
        password="Passw0rd!",
    )
    requester = await users.create_user(
        team_id=team.id,
        email="requester@example.com",
        password="Passw0rd!",
    )
    await users.set_team_creator(team.id, owner.id)
    return team.id, owner.id, requester.id


async def test_request_space_assignment_notifies_only_governors(
    client: TestClient,
    stores,
) -> None:
    _, owner_id, requester_id = await _seed(stores)

    first = client.post(
        "/api/v1/auth/me/space-assignment-request",
        cookies={"bowhead_jwt": _token(requester_id)},
    )
    repeated = client.post(
        "/api/v1/auth/me/space-assignment-request",
        cookies={"bowhead_jwt": _token(requester_id)},
    )

    assert first.status_code == 202
    assert first.json() == {"status": "submitted"}
    assert repeated.status_code == 202
    _, notifications = stores
    owner_items, _ = await notifications.list_for_user(
        user_id=owner_id,
        tab="action",
    )
    requester_items, _ = await notifications.list_for_user(
        user_id=requester_id,
        tab="all",
    )
    assert len(owner_items) == 1
    assert owner_items[0].metadata == {
        "requesterUserId": requester_id,
    }
    assert requester_items == []


async def test_request_rejected_once_space_access_exists(
    client: TestClient,
    stores,
) -> None:
    team_id, owner_id, requester_id = await _seed(stores)
    users, notifications = stores
    workspace_id = await users.create_local_workspace(
        team_id=team_id,
        name="Ready Space",
        creator_user_id=owner_id,
    )
    await users.add_member(
        workspace_id=workspace_id,
        user_id=requester_id,
    )

    response = client.post(
        "/api/v1/auth/me/space-assignment-request",
        cookies={"bowhead_jwt": _token(requester_id)},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == ("space_access_already_available")
    owner_items, _ = await notifications.list_for_user(
        user_id=owner_id,
        tab="action",
    )
    assert owner_items == []


async def test_admin_grant_resolves_request_and_notifies_requester(
    client: TestClient,
    stores,
) -> None:
    team_id, owner_id, requester_id = await _seed(stores)
    users, notifications = stores
    requested = client.post(
        "/api/v1/auth/me/space-assignment-request",
        cookies={"bowhead_jwt": _token(requester_id)},
    )
    assert requested.status_code == 202
    workspace_id = await users.create_local_workspace(
        team_id=team_id,
        name="Assigned Space",
        creator_user_id=owner_id,
    )

    granted = client.put(
        f"/api/v1/admin/members/{requester_id}/workspace-grants",
        json={"workspace_id": workspace_id, "role": "member"},
        cookies={"bowhead_jwt": _token(owner_id)},
    )

    assert granted.status_code == 200, granted.text
    owner_actions, _ = await notifications.list_for_user(
        user_id=owner_id,
        tab="action",
    )
    requester_updates, _ = await notifications.list_for_user(
        user_id=requester_id,
        tab="updates",
    )
    assert owner_actions == []
    assert [item.type for item in requester_updates] == ["space_assignment_ready"]
