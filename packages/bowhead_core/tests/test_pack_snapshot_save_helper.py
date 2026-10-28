"""V4 Sprint 1 PRD-D-4-4 · _maybe_save_pack_snapshot helper tests.

Verify the fire-and-forget save path:
  - no-op when store missing
  - no-op when 闸关（`memory_pack_snapshots_enabled=False`）
  - no-op when content empty
  - 闸开 → saves (must not block turn)
  - request lacking team_id / workspace_id → saved with NULL columns
  - store.save 抛错不外溢

★ 2026-08-29（记忆重做 批0④）：闸从 `memory_pack_snapshot_pct` 的百分比改成布尔的
`memory_pack_snapshots_enabled`（跟 `FEATURE_MEMORY_CANONICAL`）。原来的
`test_canary_partial_uses_stable_hash` 随 `_user_in_canary` 一起删掉——那条钉的是一个
**生产从未生效**的灰度旋钮（`BOWHEAD_REPLAY_ENABLED`/`_PCT` 默认 0/0、116 两个 key 都没有）。
真正该钉的是「本函数有没有生产调用点」，那条在 `test_memory_pack_batch0.py`。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from bowhead_core.memory_pack_snapshots import MemoryPackSnapshotsStore
from bowhead_core.turn_runner import _maybe_save_pack_snapshot


@dataclass
class _FakeRequest:
    turn_id: str
    user_id: str
    team_id: str | None = None
    workspace_id: str | None = None


@dataclass
class _FakeOrch:
    memory_pack_snapshots_store: MemoryPackSnapshotsStore | None
    memory_pack_snapshots_enabled: bool = False


async def _drain_bg_tasks() -> None:
    """Wait for fire-and-forget save to complete."""
    from bowhead_core.turn_runner import _PACK_SNAPSHOT_BG_TASKS

    if _PACK_SNAPSHOT_BG_TASKS:
        await asyncio.gather(*list(_PACK_SNAPSHOT_BG_TASKS), return_exceptions=True)


@pytest.fixture
async def store(tmp_path):
    s = MemoryPackSnapshotsStore(tmp_path / "snap.db")
    await s.initialize()
    return s


@pytest.mark.asyncio
async def test_noop_when_store_missing(store):
    orch = _FakeOrch(memory_pack_snapshots_store=None, memory_pack_snapshots_enabled=True)
    req = _FakeRequest(turn_id="t1", user_id="u1", team_id="org1", workspace_id="ws1")
    _maybe_save_pack_snapshot(
        orch=orch,
        request=req,
        content="hello",
        breakdown={},
        pack_version="v1",
    )
    await _drain_bg_tasks()
    # No store, nothing to verify other than no exception raised.


@pytest.mark.asyncio
async def test_noop_when_gate_closed(store):
    orch = _FakeOrch(memory_pack_snapshots_store=store, memory_pack_snapshots_enabled=False)
    req = _FakeRequest(turn_id="t1", user_id="u1", team_id="org1", workspace_id="ws1")
    _maybe_save_pack_snapshot(
        orch=orch,
        request=req,
        content="hello",
        breakdown={},
        pack_version="v1",
    )
    await _drain_bg_tasks()
    snap = await store.get_by_turn_for_user(turn_id="t1", user_id="u1")
    assert snap is None


@pytest.mark.asyncio
async def test_noop_when_content_empty(store):
    orch = _FakeOrch(memory_pack_snapshots_store=store, memory_pack_snapshots_enabled=True)
    req = _FakeRequest(turn_id="t1", user_id="u1", team_id="org1", workspace_id="ws1")
    _maybe_save_pack_snapshot(
        orch=orch,
        request=req,
        content="",
        breakdown={},
        pack_version="v1",
    )
    await _drain_bg_tasks()
    snap = await store.get_by_turn_for_user(turn_id="t1", user_id="u1")
    assert snap is None


@pytest.mark.asyncio
async def test_gate_open_saves(store):
    orch = _FakeOrch(memory_pack_snapshots_store=store, memory_pack_snapshots_enabled=True)
    req = _FakeRequest(turn_id="t1", user_id="u1", team_id="org1", workspace_id="ws1")
    _maybe_save_pack_snapshot(
        orch=orch,
        request=req,
        content="memory block",
        breakdown={"notes": 12, "entries": 0},
        pack_version="v2",
    )
    await _drain_bg_tasks()
    snap = await store.get_by_turn_for_user(turn_id="t1", user_id="u1")
    assert snap is not None
    assert snap.content == "memory block"
    assert snap.pack_version == "v2"
    assert snap.team_id == "org1"
    assert snap.workspace_id == "ws1"
    assert snap.sections_breakdown == {"notes": 12, "entries": 0}


@pytest.mark.asyncio
async def test_request_without_team_workspace_saves_null(store):
    """Dev/legacy path: request lacks team_id/workspace_id (no-team user)."""
    orch = _FakeOrch(memory_pack_snapshots_store=store, memory_pack_snapshots_enabled=True)
    req = _FakeRequest(turn_id="t1", user_id="u1")  # no team/ws
    _maybe_save_pack_snapshot(
        orch=orch,
        request=req,
        content="hello",
        breakdown={"notes": 5},
        pack_version="v2",
    )
    await _drain_bg_tasks()
    snap = await store.get_by_turn_for_user(turn_id="t1", user_id="u1")
    assert snap is not None
    assert snap.team_id is None
    assert snap.workspace_id is None


@pytest.mark.asyncio
async def test_save_failure_does_not_propagate(tmp_path):
    """Store .save() raising must not surface — fire-and-forget swallows."""

    class _BrokenStore:
        async def save(self, **_kwargs):
            raise RuntimeError("disk full")

    orch = _FakeOrch(memory_pack_snapshots_store=_BrokenStore(), memory_pack_snapshots_enabled=True)
    req = _FakeRequest(turn_id="t1", user_id="u1", team_id="org1", workspace_id="ws1")
    # Must not raise
    _maybe_save_pack_snapshot(
        orch=orch,
        request=req,
        content="hello",
        breakdown={},
        pack_version="v2",
    )
    await _drain_bg_tasks()
