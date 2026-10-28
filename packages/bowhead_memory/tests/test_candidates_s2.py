"""memory-revive S2 · candidates 强化：

- C2 pending 去重**去掉时间窗**（第 8/29 天同值仍抑制，闭合 discovery §5.2 的
  「pending 存活 30d 但去重只挡 7d」不自洽 = 复读机长尾）。
- C3 过期预告 list/mark（窗口 age∈(warn,max) 一生一次，排除过嫩/过老）。
"""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest
from bowhead_memory.candidates import MemoryCandidateStore


@pytest.fixture
async def store(tmp_path):
    s = MemoryCandidateStore(db_path=str(tmp_path / "cand.db"))
    await s.initialize()
    return s


async def _backdate_created(store: MemoryCandidateStore, cid: str, days: int) -> None:
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE memory_candidates SET created_at=datetime('now', ?) WHERE id=?",
            (f"-{days} days", cid),
        )
        await db.commit()


# ─── C2: remove the time window from pending deduplication ─────────


@pytest.mark.asyncio
async def test_pending_dedup_no_time_window_day8(store):
    """第 8 天（越过旧 7d 窗）同值仍抑制——pending 活着就不重提。"""
    c = await store.enqueue_if_new(user_id="u1", kind="preference", key="k", value="月度口径", reason=None, source_turn_id="t_1")
    assert c is not None
    await _backdate_created(store, c.id, 8)
    again = await store.enqueue_if_new(user_id="u1", kind="preference", key="k", value="月度口径", reason=None, source_turn_id="t_2")
    assert again is None  # The old implementation allowed repeats after >7d; S2 must still suppress them.


@pytest.mark.asyncio
async def test_pending_dedup_no_time_window_day29(store):
    """第 29 天（GC 30d auto-reject 之前）同值仍抑制。"""
    c = await store.enqueue_if_new(user_id="u1", kind="preference", key="k", value="环比优先", reason=None, source_turn_id="t_1")
    assert c is not None
    await _backdate_created(store, c.id, 29)
    again = await store.enqueue_if_new(user_id="u1", kind="preference", key="k", value="环比优先", reason=None, source_turn_id="t_2")
    assert again is None


@pytest.mark.asyncio
async def test_pending_dedup_is_atomic_under_concurrency(store):
    async def enqueue(turn_id: str):
        return await store.enqueue_if_new(
            user_id="u1",
            kind="preference",
            key="report_grain",
            value="monthly",
            reason=None,
            source_turn_id=turn_id,
        )

    results = await asyncio.gather(enqueue("t_1"), enqueue("t_2"))

    assert sum(result is not None for result in results) == 1
    pending = await store.list_for_user(user_id="u1")
    assert len(pending) == 1


@pytest.mark.asyncio
async def test_pending_dedup_keeps_space_scopes_distinct(store):
    first = await store.enqueue_if_new(
        user_id="u1",
        kind="preference",
        key="report_grain",
        value="monthly",
        reason=None,
        source_turn_id="t_1",
        workspace_id="ws_one",
    )
    second = await store.enqueue_if_new(
        user_id="u1",
        kind="preference",
        key="report_grain",
        value="monthly",
        reason=None,
        source_turn_id="t_2",
        workspace_id="ws_two",
    )

    assert first is not None
    assert second is not None


@pytest.mark.asyncio
async def test_initialize_canonicalizes_historical_pending_duplicates(
    store,
):
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("DROP INDEX uq_memory_candidate_pending_fingerprint")
        for candidate_id in ("cand_old_a", "cand_old_b"):
            await db.execute(
                """
                INSERT INTO memory_candidates(
                    id, user_id, kind, key, value_json, reason,
                    source_turn_id, created_at, updated_at, workspace_id
                )
                VALUES (?, 'u2', 'preference', 'grain', '"monthly"',
                        NULL, NULL, '2026-01-01T00:00:00+00:00',
                        '2026-01-01T00:00:00+00:00', NULL)
                """,
                (candidate_id,),
            )
        await db.commit()

    await store.initialize()

    async with aiosqlite.connect(store.db_path) as db:
        rows = await db.execute_fetchall(
            """
            SELECT id, status, duplicate_of_candidate_id, value_json
            FROM memory_candidates
            WHERE user_id='u2'
            ORDER BY id
            """,
        )
        index = await db.execute_fetchall(
            """
            SELECT name
            FROM sqlite_master
            WHERE type='index'
              AND name='uq_memory_candidate_pending_fingerprint'
            """,
        )
    assert rows == [
        ("cand_old_a", "pending", None, '"monthly"'),
        ("cand_old_b", "rejected", "cand_old_a", "null"),
    ]
    assert index == [("uq_memory_candidate_pending_fingerprint",)]


# ─── C3: expiry warning list + mark (once per lifetime) ─────────────


@pytest.mark.asyncio
async def test_expiring_list_and_mark_once(store):
    c = await store.enqueue_if_new(user_id="u1", kind="preference", key="k", value="v", reason=None, source_turn_id="t_1")
    assert c is not None
    await _backdate_created(store, c.id, 25)  # age 25 ∈ (23, 30)
    assert await store.list_users_with_unwarned_expiring(warn_days=23, max_age_days=30) == ["u1"]
    n = await store.mark_expiry_warned(user_id="u1", warn_days=23, max_age_days=30)
    assert n == 1
    # Once per lifetime: it does not appear again after marking.
    assert await store.list_users_with_unwarned_expiring(warn_days=23, max_age_days=30) == []


@pytest.mark.asyncio
async def test_expiring_excludes_too_young(store):
    c = await store.enqueue_if_new(user_id="u1", kind="preference", key="k", value="v", reason=None, source_turn_id="t_1")
    await _backdate_created(store, c.id, 10)  # age 10 < 23
    assert await store.list_users_with_unwarned_expiring(warn_days=23, max_age_days=30) == []


@pytest.mark.asyncio
async def test_expiring_excludes_too_old(store):
    """age > max_age_days（归 auto-reject）不预告——上下界不重叠。"""
    c = await store.enqueue_if_new(user_id="u1", kind="preference", key="k", value="v", reason=None, source_turn_id="t_1")
    await _backdate_created(store, c.id, 31)  # age 31 > 30
    assert await store.list_users_with_unwarned_expiring(warn_days=23, max_age_days=30) == []


@pytest.mark.asyncio
async def test_mark_expiry_warned_idempotent(store):
    """已标记的候选二次 mark 返回 0（不重复计数）。"""
    c = await store.enqueue_if_new(user_id="u1", kind="preference", key="k", value="v", reason=None, source_turn_id="t_1")
    await _backdate_created(store, c.id, 25)
    assert await store.mark_expiry_warned(user_id="u1", warn_days=23, max_age_days=30) == 1
    assert await store.mark_expiry_warned(user_id="u1", warn_days=23, max_age_days=30) == 0
