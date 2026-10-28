"""M158-18 phase 4 · project_memory cron aggregator tests (含 Q2 真聚合 + Q10/Q12 优雅退出)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from bowhead_core.project_memory_aggregator import (
    _cron_interval_seconds,
    aggregate_once,
    start_project_memory_cron,
    stop_project_memory_cron,
)


@pytest.mark.asyncio
async def test_aggregate_once_heartbeat_only_when_no_data(tmp_path):
    """无 user data 时仅写 _cron_heartbeat (不报 patterns)."""
    from bowhead_core.user_memory import ProjectMemoryStore, UserMemoryStore

    user_store = UserMemoryStore(tmp_path / "bowhead.db")
    proj_store = ProjectMemoryStore(tmp_path / "bowhead.db")
    await user_store.initialize()
    await proj_store.initialize()

    stats = await aggregate_once(user_store, proj_store)
    assert stats["heartbeat_written"] == 1
    assert stats["patterns_written"] == 0
    assert stats["users_scanned"] == 0


@pytest.mark.asyncio
async def test_aggregate_once_real_aggregation_cross_users(tmp_path):
    """Q2 真聚合: 多 user 的 task_plan_pick 跨人累计."""
    from bowhead_core.user_memory import ProjectMemoryStore, UserMemoryStore

    user_store = UserMemoryStore(tmp_path / "bowhead.db")
    proj_store = ProjectMemoryStore(tmp_path / "bowhead.db")
    await user_store.initialize()
    await proj_store.initialize()

    # Simulate PreferenceWriter writes (M158-11 path):
    # user_1 picks "竞品份额对比" 3 times, "白地市场" once.
    # user_2 picks "竞品份额对比" twice.
    # user_3 picks "白地市场" once.
    await user_store.upsert(
        user_id="user_1",
        kind="preference",
        key="task_plan_pick_竞品份额对比",
        value={"pick_count": 3, "last_label": "竞品份额对比"},
    )
    await user_store.upsert(
        user_id="user_1",
        kind="preference",
        key="task_plan_pick_白地市场",
        value={"pick_count": 1, "last_label": "白地市场"},
    )
    await user_store.upsert(
        user_id="user_2",
        kind="preference",
        key="task_plan_pick_竞品份额对比",
        value={"pick_count": 2, "last_label": "竞品份额对比"},
    )
    await user_store.upsert(
        user_id="user_3",
        kind="preference",
        key="task_plan_pick_白地市场",
        value={"pick_count": 1, "last_label": "白地市场"},
    )

    stats = await aggregate_once(user_store, proj_store)
    assert stats["users_scanned"] == 3
    assert stats["patterns_written"] == 2  # 2 unique labels
    assert stats["errors"] == 0

    # Verify aggregation: 竞品份额对比 5 picks/2 users; 白地市场 2 picks/2 users.
    items = await proj_store.list_for_workspace("*")
    by_key = {e.key: e for e in items}
    pat_share = by_key["task_plan_pick:竞品份额对比"].parsed_value()
    pat_white = by_key["task_plan_pick:白地市场"].parsed_value()
    assert pat_share["total_picks"] == 5
    assert pat_share["user_count"] == 2
    assert pat_white["total_picks"] == 2
    assert pat_white["user_count"] == 2


@pytest.mark.asyncio
async def test_aggregate_once_top20_cap(tmp_path):
    """生成 25 个 unique label 时, 仅写 top-20 (按 total_picks 降序)."""
    from bowhead_core.user_memory import ProjectMemoryStore, UserMemoryStore

    user_store = UserMemoryStore(tmp_path / "bowhead.db")
    proj_store = ProjectMemoryStore(tmp_path / "bowhead.db")
    await user_store.initialize()
    await proj_store.initialize()
    for i in range(25):
        await user_store.upsert(
            user_id=f"u{i}",
            kind="preference",
            key=f"task_plan_pick_label_{i}",
            value={"pick_count": 25 - i},  # Descending: i=0 picks=25, i=24 picks=1.
        )
    stats = await aggregate_once(user_store, proj_store)
    assert stats["patterns_written"] == 20
    items = await proj_store.list_for_workspace("*")
    pat_keys = [e.key for e in items if e.key.startswith("task_plan_pick:")]
    assert len(pat_keys) == 20  # Write the top 20.


@pytest.mark.asyncio
async def test_aggregate_once_idempotent(tmp_path):
    """二次跑覆盖 heartbeat + pattern, 不重复行."""
    from bowhead_core.user_memory import ProjectMemoryStore, UserMemoryStore

    user_store = UserMemoryStore(tmp_path / "bowhead.db")
    proj_store = ProjectMemoryStore(tmp_path / "bowhead.db")
    await user_store.initialize()
    await proj_store.initialize()
    await user_store.upsert(
        user_id="u1",
        kind="preference",
        key="task_plan_pick_x",
        value={"pick_count": 1},
    )
    await aggregate_once(user_store, proj_store)
    await aggregate_once(user_store, proj_store)
    items = await proj_store.list_for_workspace("*")
    pat_count = len([e for e in items if e.kind == "aggregated_pattern"])
    # heartbeat + 1 pattern = 2 rows (distinct unique keys; rerun overwrites).
    assert pat_count == 2


@pytest.mark.asyncio
async def test_aggregate_once_swallows_proj_store_error(tmp_path):
    """project_memory_store 抛错不应崩 — fail-safe."""
    from bowhead_core.user_memory import UserMemoryStore

    user_store = UserMemoryStore(tmp_path / "bowhead.db")
    await user_store.initialize()
    proj_store = AsyncMock()
    proj_store.upsert = AsyncMock(side_effect=RuntimeError("disk full"))
    stats = await aggregate_once(user_store, proj_store)
    assert stats["errors"] >= 1


@pytest.mark.asyncio
async def test_stop_project_memory_cron_clean_shutdown(tmp_path):
    """Q12 fix: lifespan shutdown cancel + await, 任务确实结束."""
    from bowhead_core.user_memory import ProjectMemoryStore, UserMemoryStore

    user_store = UserMemoryStore(tmp_path / "bowhead.db")
    proj_store = ProjectMemoryStore(tmp_path / "bowhead.db")
    await user_store.initialize()
    await proj_store.initialize()
    task = start_project_memory_cron(user_store, proj_store)
    # Cancel quickly (before the startup 5s sleep completes).
    await stop_project_memory_cron(task)
    assert task.done() or task.cancelled()


@pytest.mark.asyncio
async def test_stop_project_memory_cron_handles_none():
    """stop 接 None 不爆."""
    await stop_project_memory_cron(None)


def test_cron_interval_clamped_low_min_60s(monkeypatch):
    monkeypatch.setenv("BOWHEAD_PROJECT_MEMORY_CRON_HOURS", "0.001")
    assert _cron_interval_seconds() == 60.0


def test_cron_interval_default_168_hours(monkeypatch):
    monkeypatch.delenv("BOWHEAD_PROJECT_MEMORY_CRON_HOURS", raising=False)
    assert _cron_interval_seconds() == 168 * 3600
