from __future__ import annotations

import aiosqlite
import pytest
from bowhead_core.notification_service import NotificationService
from bowhead_core.notification_store import (
    ACTION_REQUIRED_TYPES,
    NotificationStore,
)
from bowhead_core.user_store import UserStore


@pytest.fixture
async def services(tmp_path):
    db_path = tmp_path / "space-assignment-notifications.db"
    users = UserStore(db_path)
    notifications = NotificationStore(str(db_path))
    await users.initialize()
    await notifications.initialize()
    service = NotificationService(
        store=notifications,
        user_store=users,
    )
    return users, notifications, service


async def _seed_users(users: UserStore) -> tuple[str, str, str]:
    team = await users.create_team(name="Notification Team", max_seats=20)
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


async def test_space_assignment_request_is_atomic_action_item_and_idempotent(
    services,
) -> None:
    users, notifications, service = services
    team_id, owner_id, requester_id = await _seed_users(users)

    first = await service.emit_space_assignment_requested(
        team_id=team_id,
        requester_user_id=requester_id,
    )
    assert first is not None
    assert first.type == "space_assignment_requested"
    assert "space_assignment_requested" in ACTION_REQUIRED_TYPES
    counts = await notifications.notification_count(user_id=owner_id)
    assert counts.action_required == 1

    assert await notifications.archive(
        notification_id=first.id,
        user_id=owner_id,
    )
    repeated = await service.emit_space_assignment_requested(
        team_id=team_id,
        requester_user_id=requester_id,
    )
    assert repeated is not None
    assert repeated.id == first.id
    assert repeated.updated_at == first.updated_at

    async with (
        aiosqlite.connect(notifications.db_path) as db,
        db.execute(
            "SELECT COUNT(*),archived_at FROM notification_recipients WHERE notification_id=? AND user_id=?",
            (first.id, owner_id),
        ) as cur,
    ):
        recipient = await cur.fetchone()
    assert recipient is not None
    assert recipient[0] == 1
    assert recipient[1] is not None


async def test_resolve_request_emits_ready_update_to_requester(
    services,
) -> None:
    users, notifications, service = services
    team_id, owner_id, requester_id = await _seed_users(users)
    requested = await service.emit_space_assignment_requested(
        team_id=team_id,
        requester_user_id=requester_id,
    )
    assert requested is not None

    ready = await service.resolve_space_assignment_request(
        team_id=team_id,
        requester_user_id=requester_id,
    )

    assert ready is not None
    assert ready.type == "space_assignment_ready"
    action_items, _ = await notifications.list_for_user(
        user_id=owner_id,
        tab="action",
    )
    updates, _ = await notifications.list_for_user(
        user_id=requester_id,
        tab="updates",
    )
    assert action_items == []
    assert [item.type for item in updates] == ["space_assignment_ready"]
