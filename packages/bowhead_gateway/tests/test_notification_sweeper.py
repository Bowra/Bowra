from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import aiosqlite
import pytest
from bowhead_core.notification_store import NotificationStore
from bowhead_gateway.notification_sweeper import sweep_notification_archive_once

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_notification_sweeper_prunes_expired_archives(tmp_path: Path) -> None:
    db_path = tmp_path / "notifications.db"
    store = NotificationStore(str(db_path))
    await store.initialize()
    item = await store.upsert_item(
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
    )
    await store.add_recipients(item.id, [("user_1", "requester")])
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
