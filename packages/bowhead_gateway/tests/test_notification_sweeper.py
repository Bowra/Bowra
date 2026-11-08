from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING

import aiosqlite
import pytest
from bowhead_core.notification_service import NotificationService
from bowhead_core.notification_store import NotificationStore
from bowhead_core.notification_types import REGISTRY
from bowhead_gateway.notification_sweeper import sweep_notification_archive_once

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_notification_sweeper_prunes_expired_archives(tmp_path: Path) -> None:
    db_path = tmp_path / "notifications.db"
    store = NotificationStore(str(db_path))
    await store.initialize()
    item = (
        await store.write(
            team_id="team_1",
            category="approval",
            type="approval.rejected",
            priority="p2",
            title="Archived update",
            body=None,
            subject_type="approval",
            subject_id="apr_1",
            actor_user_id="admin_1",
            action_url="/?session=s1",
            actions=[],
            metadata={},
            dedupe_key="approval.rejected:apr_1",
            recipients=[("user_1", "requester")],
        )
    ).item
    assert await store.archive(notification_id=item.id, user_id="user_1") is True

    now = datetime(2026, 6, 10, tzinfo=UTC)
    old_archived_at = (now - timedelta(days=181)).isoformat(timespec="seconds")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE notification_items SET created_at=?, updated_at=? WHERE id=?",
            (old_archived_at, old_archived_at, item.id),
        )
        await db.execute(
            "UPDATE notification_recipients SET archived_at=? WHERE notification_id=?",
            (old_archived_at, item.id),
        )
        await db.commit()

    counts = await sweep_notification_archive_once(
        store=store,
        retention_days=180,
        now=now,
    )

    assert counts == {"recipients_deleted": 1, "items_deleted": 1}
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM notification_items") as cur:
            item_count = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM notification_recipients") as cur:
            recipient_count = (await cur.fetchone())[0]
    assert item_count == 0
    assert recipient_count == 0


class _Users:
    async def get_user_team_id(self, user_id):
        return "team_1"

    async def get_workspace_team_id(self, workspace_id):
        return "team_1"

    async def filter_active_team_members(self, team_id, user_ids):
        return set(user_ids)

    async def get_team_skill(self, team_id, skill_id):
        return {
            "sk_pending": SimpleNamespace(approval="pending", candidate_state=None),
            "sk_candidate": SimpleNamespace(approval="approved", candidate_state="pending"),
            "sk_done": SimpleNamespace(approval="approved", candidate_state=None),
        }.get(skill_id)


class _AccessRequests:
    async def get(self, request_id, *, team_id=None):
        return {"acr_pending": SimpleNamespace(status="pending"), "acr_done": SimpleNamespace(status="denied")}.get(request_id)


class _Scheduled:
    async def get(self, job_id):
        return {"sj_paused": SimpleNamespace(status="paused"), "sj_resumed": SimpleNamespace(status="active")}.get(job_id)

    async def list_awaiting_runs(self):
        return [SimpleNamespace(id="run_waiting")]


@pytest.mark.asyncio
async def test_notification_sweeper_closes_action_items_whose_subject_moved_on(tmp_path: Path) -> None:
    """A producer that missed its resolve call cannot keep a badge lit: each tick re-reads the
    subject of every open action item it knows how to check."""
    store = NotificationStore(str(tmp_path / "notifications.db"))
    await store.initialize()
    service = NotificationService(store=store, user_store=_Users())

    async def put(type_: str, dedupe_key: str, subject_type: str, subject_id: str, metadata: dict) -> None:
        await store.write(
            team_id="team_1",
            category="test",
            type=type_,
            priority="p1",
            title="t",
            body=None,
            subject_type=subject_type,
            subject_id=subject_id,
            actor_user_id=None,
            action_url=None,
            actions=None,
            metadata=metadata,
            dedupe_key=dedupe_key,
            recipients=[("u_admin", None)],
        )

    still_waiting = [
        ("access_requested", "access.requested:acr_pending", "report", "r1", {"requestId": "acr_pending"}),
        ("scheduled.paused", "scheduled.paused:sj_paused", "scheduled_job", "sj_paused", {}),
        # The per-Team "model not configured" pause is about the Team, not one task: left alone.
        ("scheduled.paused", "scheduled.paused:model:team_1", "team", "team_1", {}),
        ("routine.needs_input", "routine.needs_input:sj_paused:run_waiting", "scheduled_job", "sj_paused", {"runId": "run_waiting"}),
        ("skill_approval_submitted", "skill_approval:submitted:sk_pending:t", "team_skill", "sk_pending", {}),
        ("skill_approval_submitted", "skill_approval:submitted:sk_candidate:t", "team_skill", "sk_candidate", {}),
    ]
    moved_on = [
        ("access_requested", "access.requested:acr_done", "report", "r1", {"requestId": "acr_done"}),
        ("access_requested_escalated", "access.escalated:acr_missing", "report", "r1", {"requestId": "acr_missing"}),
        ("scheduled.paused", "scheduled.paused:sj_resumed", "scheduled_job", "sj_resumed", {}),
        ("scheduled.failed", "scheduled.failed:sj_deleted", "scheduled_job", "sj_deleted", {}),
        ("routine.needs_input", "routine.needs_input:sj_paused:run_answered", "scheduled_job", "sj_paused", {"runId": "run_answered"}),
        ("skill_approval_submitted", "skill_approval:submitted:sk_done:t", "team_skill", "sk_done", {}),
        ("skill_approval_submitted", "skill_approval:submitted:sk_gone:t", "team_skill", "sk_gone", {}),
    ]
    for row in still_waiting + moved_on:
        await put(*row)
    keep = {row[1] for row in still_waiting}

    await sweep_notification_archive_once(
        store=store,
        notification_service=service,
        access_request_store=_AccessRequests(),
        scheduled_store=_Scheduled(),
        user_store=_Users(),
    )

    open_items = await store.list_open_items(types=REGISTRY.keys())
    assert {item.dedupe_key for item in open_items} == keep
    assert (await store.notification_count(user_id="u_admin")).action_required == len(keep)
