"""`list_unread_by_type` —— 通知中心以外的送达面取数（agent-feedback-loop 批 5）。

它服务的是 chat 内联回执卡：`list_for_user` 按 tab 分页 30 条且不按类型过滤，
一条 p2 回执会被同期的 routine 通知挤走，前端在一页里筛类型不可靠。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bowhead_core.notification_store import NotificationStore

if TYPE_CHECKING:
    from pathlib import Path

_RECEIPT = "feedback_candidate_receipt"


async def _store(tmp_path: Path) -> NotificationStore:
    store = NotificationStore(db_path=str(tmp_path / "n.db"))
    await store.initialize()
    return store


async def _emit(store: NotificationStore, *, type_: str, key: str, users: list[str]):
    return await store.upsert_item_with_recipients(
        team_id="t1",
        category="feedback",
        type=type_,
        priority="p2",
        title=f"title-{key}",
        body=None,
        subject_type="feedback_candidate",
        subject_id=key,
        actor_user_id=None,
        action_url=None,
        actions=[],
        metadata={"candidateId": key},
        dedupe_key=f"k:{key}",
        recipients=[(u, "feedback_contributor") for u in users],
    )


@pytest.mark.asyncio
async def test_returns_only_the_requested_type_for_the_caller(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    await _emit(store, type_=_RECEIPT, key="fbc_1", users=["u1", "u2"])
    await _emit(store, type_="routine.delivered", key="job_1", users=["u1"])
    await _emit(store, type_=_RECEIPT, key="fbc_2", users=["u2"])

    rows = await store.list_unread_by_type(user_id="u1", type=_RECEIPT)
    assert [r.subject_id for r in rows] == ["fbc_1"]  # Exclude another user's record and other types.


@pytest.mark.asyncio
async def test_read_and_archived_drop_out(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    a = await _emit(store, type_=_RECEIPT, key="fbc_a", users=["u1"])
    b = await _emit(store, type_=_RECEIPT, key="fbc_b", users=["u1"])
    await _emit(store, type_=_RECEIPT, key="fbc_c", users=["u1"])

    assert await store.mark_read(notification_id=a.id, user_id="u1") is True
    await store.archive(notification_id=b.id, user_id="u1")

    rows = await store.list_unread_by_type(user_id="u1", type=_RECEIPT)
    # ★ Receipt acceptance marks it read: viewing it in chat and in the notification center shares one state;
    #   the card must not alert once on each surface.
    assert [r.subject_id for r in rows] == ["fbc_c"]


@pytest.mark.asyncio
async def test_limit_is_clamped_not_unbounded(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    for i in range(8):
        await _emit(store, type_=_RECEIPT, key=f"fbc_{i}", users=["u1"])
    assert len(await store.list_unread_by_type(user_id="u1", type=_RECEIPT)) == 5  # Default 5.
    assert len(await store.list_unread_by_type(user_id="u1", type=_RECEIPT, limit=0)) == 1
    assert len(await store.list_unread_by_type(user_id="u1", type=_RECEIPT, limit=999)) == 8
