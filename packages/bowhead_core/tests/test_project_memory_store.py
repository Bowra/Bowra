"""M158-10 · ProjectMemoryStore unit tests."""

from __future__ import annotations

import pytest
from bowhead_core.user_memory import ProjectMemoryStore, UserMemoryStore


@pytest.fixture
async def store(tmp_path):
    s = ProjectMemoryStore(tmp_path / "bowhead.db")
    await s.initialize()
    return s


@pytest.mark.asyncio
async def test_initialize_creates_schema(tmp_path):
    s = ProjectMemoryStore(tmp_path / "bowhead.db")
    await s.initialize()
    # Reuse the user_memory schema; list_for_workspace must return an empty list without failing.
    items = await s.list_for_workspace("ws_test")
    assert items == []


@pytest.mark.asyncio
async def test_upsert_and_list(store):
    await store.upsert(
        workspace_id="ws_test",
        kind="pattern",
        key="war_zone_strategy",
        value={"cluster_size": 23, "common_subq": ["share", "white_space"]},
    )
    items = await store.list_for_workspace("ws_test")
    assert len(items) == 1
    assert items[0].kind == "pattern"
    assert items[0].key == "war_zone_strategy"
    parsed = items[0].parsed_value()
    assert parsed["cluster_size"] == 23


@pytest.mark.asyncio
async def test_upsert_idempotent(store):
    """同 (kind, key) 二次 upsert 覆盖, 不重复行."""
    await store.upsert(
        workspace_id="ws_test",
        kind="pattern",
        key="x",
        value={"v": 1},
    )
    await store.upsert(
        workspace_id="ws_test",
        kind="pattern",
        key="x",
        value={"v": 2},
    )
    items = await store.list_for_workspace("ws_test")
    assert len(items) == 1
    assert items[0].parsed_value()["v"] == 2


@pytest.mark.asyncio
async def test_increment_counter_zero_to_n(store):
    n = await store.increment_counter(
        workspace_id="ws_test",
        kind="pick_count",
        key="competitive_share",
    )
    assert n == 1
    n = await store.increment_counter(
        workspace_id="ws_test",
        kind="pick_count",
        key="competitive_share",
    )
    assert n == 2
    n = await store.increment_counter(
        workspace_id="ws_test",
        kind="pick_count",
        key="competitive_share",
        delta=5,
    )
    assert n == 7


@pytest.mark.asyncio
async def test_workspace_isolation(store):
    """不同 workspace_id 互不串数据."""
    await store.upsert(
        workspace_id="ws_a",
        kind="pattern",
        key="x",
        value={"v": 1},
    )
    await store.upsert(
        workspace_id="ws_b",
        kind="pattern",
        key="x",
        value={"v": 2},
    )
    items_a = await store.list_for_workspace("ws_a")
    items_b = await store.list_for_workspace("ws_b")
    assert len(items_a) == 1
    assert len(items_b) == 1
    assert items_a[0].parsed_value()["v"] == 1
    assert items_b[0].parsed_value()["v"] == 2


@pytest.mark.asyncio
async def test_user_and_project_share_table_no_collision(tmp_path):
    """user_memory 跟 project_memory 同 sqlite 文件 + 同 schema, 由 subject_type 隔离."""
    db = tmp_path / "bowhead.db"
    user_store = UserMemoryStore(db)
    proj_store = ProjectMemoryStore(db)
    await user_store.initialize()
    await proj_store.initialize()  # Must be idempotent (CREATE TABLE IF NOT EXISTS).

    await user_store.upsert(
        user_id="u1",
        kind="preference",
        key="x",
        value={"v": "user"},
    )
    await proj_store.upsert(
        workspace_id="ws_x",
        kind="pattern",
        key="x",
        value={"v": "project"},
    )

    user_items = await user_store.list_for_user("u1")
    proj_items = await proj_store.list_for_workspace("ws_x")
    assert len(user_items) == 1
    assert len(proj_items) == 1
    assert user_items[0].subject_type == "user"
    assert proj_items[0].subject_type == "project"
    assert user_items[0].parsed_value()["v"] == "user"
    assert proj_items[0].parsed_value()["v"] == "project"
