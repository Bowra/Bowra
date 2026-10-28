"""memory-revive S2 · entries 空间打标（Model A，Bowhead Space ws_*）：

- workspace_id 列迁移（附加式，老库无列 → ALTER → 读兜底 None）
- upsert 带/不带 ws + COALESCE（不传 ws 不降级已 spaced 行，review M2）
- list_for_user(workspace_id=X) 只返 X + 全局 NULL（红线：过滤键=入参，不反推）
- 跨空间同 key 明确冲突，不再静默覆盖或搬移
"""

from __future__ import annotations

import aiosqlite
import pytest
from bowhead_core.user_memory import MemoryScopeConflictError, UserMemoryStore


@pytest.fixture
async def mem(tmp_path):
    s = UserMemoryStore(tmp_path / "mem.db")
    await s.initialize()
    return s


@pytest.mark.asyncio
async def test_migration_adds_workspace_column_on_legacy_db(tmp_path):
    """老库（无 workspace_id 列）initialize 后 ALTER 补列，读回 None 不炸。"""
    db = tmp_path / "legacy.db"
    async with aiosqlite.connect(str(db)) as conn:
        await conn.execute(
            "CREATE TABLE memory_entries (id TEXT PRIMARY KEY, subject_type TEXT NOT NULL, "
            "subject_id TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, "
            "locked INTEGER NOT NULL DEFAULT 0, lock_reason TEXT, source TEXT NOT NULL DEFAULT 'user_explicit', "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "UNIQUE(subject_type, subject_id, kind, key))"
        )
        await conn.execute(
            "INSERT INTO memory_entries(id, subject_type, subject_id, kind, key, value, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("mem_old", "user", "u1", "preference", "k", '"v"', "2020-01-01", "2020-01-01"),
        )
        await conn.commit()
    s = UserMemoryStore(db)
    await s.initialize()
    entries = await s.list_for_user("u1")
    assert len(entries) == 1
    assert entries[0].workspace_id is None  # Old-row fallback: None.
    assert entries[0].memory_role == "unclassified"
    assert entries[0].memory_policy_version == 0
    assert entries[0].version == 1


@pytest.mark.asyncio
async def test_upsert_stores_and_reads_workspace_id(mem):
    e = await mem.upsert(user_id="u1", kind="preference", key="k", value="v", workspace_id="ws_a")
    assert e.workspace_id == "ws_a"
    got = await mem.list_for_user("u1")
    assert got[0].workspace_id == "ws_a"


@pytest.mark.asyncio
async def test_list_for_user_filters_by_workspace_plus_global(mem):
    await mem.upsert(user_id="u1", kind="preference", key="ka", value="a", workspace_id="ws_a")
    await mem.upsert(user_id="u1", kind="preference", key="kb", value="b", workspace_id="ws_b")
    await mem.upsert(user_id="u1", kind="preference", key="kg", value="g")  # Global NULL
    a = {e.key for e in await mem.list_for_user("u1", workspace_id="ws_a")}
    assert a == {"ka", "kg"}  # ws_a + global fallback, excluding ws_b.
    allk = {e.key for e in await mem.list_for_user("u1")}
    assert allk == {"ka", "kb", "kg"}  # No ws = all entries (/memory page behavior unchanged).


@pytest.mark.asyncio
async def test_coalesce_null_upsert_does_not_downgrade_spaced_row(mem):
    """review M2：不传 ws 的 upsert（如 /memory 页手编）不得把已 spaced 行降级成全局。"""
    await mem.upsert(user_id="u1", kind="preference", key="k", value="v1", workspace_id="ws_a")
    await mem.upsert(user_id="u1", kind="preference", key="k", value="v2")  # No ws passed.
    got = (await mem.list_for_user("u1"))[0]
    assert got.parsed_value() == "v2"  # Update value.
    assert got.workspace_id == "ws_a"  # Retain ws (COALESCE(excluded, current value)).


@pytest.mark.asyncio
async def test_non_null_upsert_rejects_cross_space_move(mem):
    """跨空间同 key 必须显式解决，普通 upsert 不得静默搬移。"""
    await mem.upsert(user_id="u1", kind="preference", key="sales", value="X", workspace_id="ws_a")
    with pytest.raises(MemoryScopeConflictError):
        await mem.upsert(user_id="u1", kind="preference", key="sales", value="Y", workspace_id="ws_b")
    all_rows = await mem.list_for_user("u1")
    assert len(all_rows) == 1
    assert all_rows[0].workspace_id == "ws_a"
    assert all_rows[0].parsed_value() == "X"
    assert len(await mem.list_for_user("u1", workspace_id="ws_a")) == 1
    assert await mem.list_for_user("u1", workspace_id="ws_b") == []


@pytest.mark.asyncio
async def test_initialize_idempotent_second_call(tmp_path):
    """review 补：二次 initialize 不炸（ALTER 已存在列 → OperationalError 吞并的幂等分支）。"""
    db = tmp_path / "idem.db"
    s = UserMemoryStore(db)
    await s.initialize()
    await s.initialize()  # Repeat to hit the column-already-exists branch.
    e = await s.upsert(user_id="u1", kind="preference", key="k", value="v", workspace_id="ws_a")
    assert e.workspace_id == "ws_a"


@pytest.mark.asyncio
async def test_project_and_user_store_coexist_on_shared_table(tmp_path):
    """review 补：memory_entries 同表被 User+Project 共用；both initialize 后 project 行
    （workspace_id NULL）与 user spaced 行共存互不干扰（review M1 的运行时对照）。"""
    from bowhead_core.user_memory import ProjectMemoryStore

    db = tmp_path / "shared.db"
    um = UserMemoryStore(db)
    pm = ProjectMemoryStore(db)
    await um.initialize()
    await pm.initialize()
    await um.upsert(user_id="u1", kind="preference", key="uk", value="uv", workspace_id="ws_a")
    await pm.upsert(workspace_id="ws_a", kind="pattern", key="pk", value="pv")
    # User side reads only subject_type='user'; do not mix in project rows.
    u_entries = await um.list_for_user("u1")
    assert {e.key for e in u_entries} == {"uk"}
    assert u_entries[0].workspace_id == "ws_a"
    # New workspace_id column is NULL on project rows (unused by project reads).
    p_entries = await pm.list_for_workspace("ws_a")
    assert {e.key for e in p_entries} == {"pk"}
    assert p_entries[0].workspace_id is None
