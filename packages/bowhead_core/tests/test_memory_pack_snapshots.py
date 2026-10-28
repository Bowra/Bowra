"""V4 Sprint 1 PRD-D-4-4 · MemoryPackSnapshotsStore tests.

Covers:
  - save: insert + char_count, idempotency (PRIMARY KEY turn_id), skip empty
  - get_by_turn_for_user: cross-user isolation (returns None on user mismatch)
  - get_by_turn_for_admin: cross-team isolation, NULL team_id orphan filter,
    empty admin_team_id returns None
  - hard_delete_old: TTL purge with datetime() normalize
  - sections_breakdown JSON round-trip + non-ASCII content
"""

from __future__ import annotations

import aiosqlite
import pytest
from bowhead_core.memory_pack_snapshots import MemoryPackSnapshotsStore


@pytest.fixture
async def store(tmp_path):
    db = tmp_path / "snap.db"
    s = MemoryPackSnapshotsStore(db)
    await s.initialize()
    return s


@pytest.fixture
def base():
    return {
        "turn_id": "t_abc123",
        "user_id": "u1",
        "team_id": "org1",
        "workspace_id": "ws1",
        "content": "memory pack content here",
        "sections_breakdown": {"notes": 12, "entries": 8},
        "pack_version": "v1",
    }


@pytest.mark.asyncio
async def test_initialize_renames_legacy_org_id_before_creating_team_index(tmp_path):
    db_path = tmp_path / "legacy-snap.db"
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(
            """
            CREATE TABLE memory_pack_snapshots (
                turn_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                org_id TEXT,
                workspace_id TEXT,
                content TEXT NOT NULL,
                char_count INTEGER NOT NULL,
                sections_breakdown_json TEXT,
                pack_version TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO memory_pack_snapshots VALUES
                ('t_legacy', 'u_legacy', 'team_legacy', 'ws_legacy', 'content', 7, '{}', 'v1', '2026-07-01T00:00:00+00:00');
            """
        )
        await db.commit()

    store = MemoryPackSnapshotsStore(db_path)
    await store.initialize()

    async with aiosqlite.connect(db_path) as db:
        columns = {row[1] for row in await db.execute_fetchall("PRAGMA table_info(memory_pack_snapshots)")}
    assert "team_id" in columns
    assert "org_id" not in columns
    snapshot = await store.get_by_turn_for_admin(turn_id="t_legacy", admin_team_id="team_legacy")
    assert snapshot is not None


# ─── save ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_save_inserts_row_and_returns_true(store, base):
    ok = await store.save(**base)
    assert ok is True

    snap = await store.get_by_turn_for_user(turn_id="t_abc123", user_id="u1")
    assert snap is not None
    assert snap.user_id == "u1"
    assert snap.team_id == "org1"
    assert snap.workspace_id == "ws1"
    assert snap.content == "memory pack content here"
    assert snap.char_count == len("memory pack content here")
    assert snap.sections_breakdown == {"notes": 12, "entries": 8}
    assert snap.pack_version == "v1"


@pytest.mark.asyncio
async def test_save_empty_content_skipped(store, base):
    base["content"] = ""
    ok = await store.save(**base)
    assert ok is False

    snap = await store.get_by_turn_for_user(turn_id="t_abc123", user_id="u1")
    assert snap is None


@pytest.mark.asyncio
async def test_save_idempotent_on_duplicate_turn_id(store, base):
    ok1 = await store.save(**base)
    assert ok1 is True

    # 2nd save same turn_id: PRIMARY KEY conflict → IGNORE → returns False
    base2 = dict(base, content="DIFFERENT — should be ignored", user_id="u_other")
    ok2 = await store.save(**base2)
    assert ok2 is False

    # Original row preserved
    async with aiosqlite.connect(store._db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id, content FROM memory_pack_snapshots WHERE turn_id=?",
            ("t_abc123",),
        ) as cur:
            row = await cur.fetchone()
    assert row["user_id"] == "u1"
    assert row["content"] == "memory pack content here"


@pytest.mark.asyncio
async def test_save_with_null_team_workspace(store, base):
    """team_id and workspace_id can be NULL (dev/legacy/no-team user)."""
    base["team_id"] = None
    base["workspace_id"] = None
    ok = await store.save(**base)
    assert ok is True

    snap = await store.get_by_turn_for_user(turn_id="t_abc123", user_id="u1")
    assert snap is not None
    assert snap.team_id is None
    assert snap.workspace_id is None


# ─── get_by_turn_for_user ───────────────────────────────


@pytest.mark.asyncio
async def test_get_by_turn_for_user_cross_user_returns_none(store, base):
    """User A can NOT read user B's turn snapshot."""
    await store.save(**base)
    snap = await store.get_by_turn_for_user(turn_id="t_abc123", user_id="u_other")
    assert snap is None


@pytest.mark.asyncio
async def test_get_by_turn_for_user_unknown_turn_returns_none(store):
    snap = await store.get_by_turn_for_user(turn_id="t_nonexistent", user_id="u1")
    assert snap is None


# ─── get_by_turn_for_admin ──────────────────────────────


@pytest.mark.asyncio
async def test_get_by_turn_for_admin_same_team_returns_row(store, base):
    await store.save(**base)
    snap = await store.get_by_turn_for_admin(
        turn_id="t_abc123",
        admin_team_id="org1",
    )
    assert snap is not None
    assert snap.user_id == "u1"


@pytest.mark.asyncio
async def test_get_by_turn_for_admin_cross_team_returns_none(store, base):
    """Admin from team X must not see team Y's turns."""
    await store.save(**base)
    snap = await store.get_by_turn_for_admin(
        turn_id="t_abc123",
        admin_team_id="team_other",
    )
    assert snap is None


@pytest.mark.asyncio
async def test_get_by_turn_for_admin_null_team_orphan_returns_none(store, base):
    """NULL team_id rows (dev/legacy orphan) are not in any admin scope."""
    base["team_id"] = None
    await store.save(**base)
    snap = await store.get_by_turn_for_admin(
        turn_id="t_abc123",
        admin_team_id="org1",
    )
    assert snap is None


@pytest.mark.asyncio
async def test_get_by_turn_for_admin_empty_admin_team_returns_none(store, base):
    """Defensive: admin without team_id can't be scoped."""
    await store.save(**base)
    snap = await store.get_by_turn_for_admin(
        turn_id="t_abc123",
        admin_team_id="",
    )
    assert snap is None


# ─── hard_delete_old (cron TTL) ─────────────────────────


@pytest.mark.asyncio
async def test_hard_delete_old_purges_old_keeps_fresh(store, base):
    # Save 2 snapshots
    await store.save(**base)
    await store.save(**dict(base, turn_id="t_fresh", content="fresh content"))

    # Age first one to 200 days
    async with aiosqlite.connect(store._db_path) as db:
        await db.execute(
            "UPDATE memory_pack_snapshots SET created_at=datetime('now', '-200 days') WHERE turn_id=?",
            ("t_abc123",),
        )
        await db.commit()

    # Default 180d TTL: old removed, fresh kept
    purged = await store.hard_delete_old(max_age_days=180)
    assert purged == 1

    assert await store.get_by_turn_for_user(turn_id="t_abc123", user_id="u1") is None
    fresh = await store.get_by_turn_for_user(turn_id="t_fresh", user_id="u1")
    assert fresh is not None


@pytest.mark.asyncio
async def test_hard_delete_old_zero_when_all_fresh(store, base):
    await store.save(**base)
    purged = await store.hard_delete_old(max_age_days=180)
    assert purged == 0


@pytest.mark.asyncio
async def test_hard_delete_old_handles_iso_t_format(store, base):
    """Spike #19: created_at is ISO 'T' but SQL datetime('now') is ' ' format —
    datetime() normalization is required for the WHERE clause to compare them."""
    await store.save(**base)
    # _now() produces ISO with T. Verify datetime() handles it.
    async with aiosqlite.connect(store._db_path) as db:
        await db.execute(
            "UPDATE memory_pack_snapshots SET created_at='2020-01-01T00:00:00+00:00' WHERE turn_id=?",
            ("t_abc123",),
        )
        await db.commit()
    purged = await store.hard_delete_old(max_age_days=30)
    assert purged == 1


# ─── sections_breakdown JSON round-trip ─────────────────


@pytest.mark.asyncio
async def test_sections_breakdown_json_round_trip(store, base):
    base["sections_breakdown"] = {"notes": 100, "entries": 200}
    await store.save(**base)
    snap = await store.get_by_turn_for_user(turn_id="t_abc123", user_id="u1")
    assert snap is not None
    assert snap.sections_breakdown == {"notes": 100, "entries": 200}


@pytest.mark.asyncio
async def test_sections_breakdown_empty_dict_round_trip(store, base):
    base["sections_breakdown"] = {}
    await store.save(**base)
    snap = await store.get_by_turn_for_user(turn_id="t_abc123", user_id="u1")
    assert snap is not None
    assert snap.sections_breakdown == {}


# ─── unicode + large content ────────────────────────────


@pytest.mark.asyncio
async def test_save_unicode_content_round_trip(store, base):
    base["content"] = "用户偏好: 中文回答\n📌 重要事实"
    await store.save(**base)
    snap = await store.get_by_turn_for_user(turn_id="t_abc123", user_id="u1")
    assert snap is not None
    assert snap.content == "用户偏好: 中文回答\n📌 重要事实"
    assert snap.char_count == len("用户偏好: 中文回答\n📌 重要事实")


@pytest.mark.asyncio
async def test_save_large_content_no_truncation(store, base):
    """char_count is whatever was passed in; store does not truncate."""
    base["content"] = "x" * 50_000
    await store.save(**base)
    snap = await store.get_by_turn_for_user(turn_id="t_abc123", user_id="u1")
    assert snap is not None
    assert snap.char_count == 50_000
    assert len(snap.content) == 50_000


# ─── initialize idempotent ──────────────────────────────


@pytest.mark.asyncio
async def test_initialize_idempotent(tmp_path):
    """Calling initialize twice on the same DB does not error (CREATE TABLE
    IF NOT EXISTS + CREATE INDEX IF NOT EXISTS pattern)."""
    db = tmp_path / "snap.db"
    s = MemoryPackSnapshotsStore(db)
    await s.initialize()
    await s.initialize()  # second call must not raise
    # And it still works:
    ok = await s.save(
        turn_id="t1",
        user_id="u1",
        team_id="org1",
        workspace_id="ws1",
        content="hi",
        sections_breakdown={"notes": 2},
        pack_version="v1",
    )
    assert ok is True


@pytest.mark.asyncio
async def test_the_executor_block_is_persisted_in_the_same_snapshot_row(tmp_path):
    """★ 收口 review 第 5 条：执行体段此前**从不落库**。

    「live / 落库 / 重放三者一致」这条不变量被 chat-pipeline.md、REPORT_WRITING.md、
    memory_pack.py、memory_injection.py **四处**白纸黑字写着，可 `memory_pack_snapshots`
    根本没有 `executor_block` 列 ⇒ 对 peer 那条路它是假的：翻开
    `FEATURE_MEMORY_DOWNSTREAM` 之后，每个执行体和报告主笔实际收到的那串字节，
    在 `/api/v1/memory/replay` 里完全看不见，「这一轮到底注入了什么」只答得出 Lead 那半。

    ★ 突变验证：把 `save()` 的 `executor_block` 参数从 INSERT 里去掉 ⇒ 本用例红。
    """
    store = MemoryPackSnapshotsStore(tmp_path / "s.db")
    await store.initialize()
    ok = await store.save(
        turn_id="t1",
        user_id="u1",
        team_id=None,
        workspace_id=None,
        content="LEAD-PACK",
        sections_breakdown={"notes": 0, "entries": 9},
        pack_version="canonical_v1",
        executor_block="PEER-BLOCK",
    )
    assert ok is True

    import aiosqlite

    async with aiosqlite.connect(store._db_path) as db:  # noqa: SLF001
        [(block,)] = await db.execute_fetchall("SELECT executor_block FROM memory_pack_snapshots WHERE turn_id='t1'")
    assert block == "PEER-BLOCK", "执行体拿到的字节必须与 Lead 的 pack 落在同一行快照里"
