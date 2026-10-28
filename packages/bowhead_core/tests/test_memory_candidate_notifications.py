"""memory-revive S1 · `memory.candidate` 兜底通知（badge）——chat 内联卡的
轮询窗口外送达面。同 test_scheduled_notifications 的 harness 风格。"""

from __future__ import annotations

import asyncio

from bowhead_core.notification_service import NotificationService
from bowhead_core.notification_store import ACTION_REQUIRED_TYPES, NotificationStore


def _run(coro):
    return asyncio.run(coro)


class _Users:
    async def get_user_team_id(self, user_id):
        return "o1"


class _NoTeamUsers:
    async def get_user_team_id(self, user_id):
        return None


def _svc(tmp_path, users=None):
    store = NotificationStore(str(tmp_path / "n.db"))
    _run(store.initialize())
    return NotificationService(store=store, user_store=users or _Users()), store


def test_memory_candidate_action_required_badge(tmp_path):
    svc, store = _svc(tmp_path)
    item = _run(svc.emit_memory_candidates(user_id="u1", created_count=2, preview="偏好环比"))
    assert item is not None and item.type == "memory.candidate"
    assert item.action_url == "/settings/memory/"
    assert item.actions[0]["url"] == "/settings/memory/"
    assert "memory.candidate" in ACTION_REQUIRED_TYPES  # drives bell badge
    assert "2 条" in (item.body or "") and "偏好环比" in (item.body or "")
    assert _run(store.notification_count(user_id="u1")).action_required == 1


def test_memory_candidate_dedupe_per_user(tmp_path):
    """连续多次蒸馏折叠为一条 badge 条目——不做 per-candidate 轰炸。"""
    svc, store = _svc(tmp_path)
    _run(svc.emit_memory_candidates(user_id="u1", created_count=1))
    _run(svc.emit_memory_candidates(user_id="u1", created_count=3))
    assert _run(store.notification_count(user_id="u1")).action_required == 1


def test_memory_candidate_zero_count_noop(tmp_path):
    svc, store = _svc(tmp_path)
    assert _run(svc.emit_memory_candidates(user_id="u1", created_count=0)) is None
    assert _run(store.notification_count(user_id="u1")).action_required == 0


def test_memory_candidate_no_team_soft_skip(tmp_path):
    svc, _store = _svc(tmp_path, users=_NoTeamUsers())
    assert _run(svc.emit_memory_candidates(user_id="u1", created_count=1)) is None


def test_resolve_memory_candidates_clears_badge(tmp_path):
    """review-fix P1: 决策完 pending 清零 → resolve → action badge 归零。"""
    svc, store = _svc(tmp_path)
    _run(svc.emit_memory_candidates(user_id="u1", created_count=2))
    assert _run(store.notification_count(user_id="u1")).action_required == 1
    _run(svc.resolve_memory_candidates(user_id="u1"))
    assert _run(store.notification_count(user_id="u1")).action_required == 0


def test_resolve_then_new_candidates_reopens(tmp_path):
    """resolve 后新候选到来 → reopen → badge 重亮（reopen_resolved 语义保持）。"""
    svc, store = _svc(tmp_path)
    _run(svc.emit_memory_candidates(user_id="u1", created_count=1))
    _run(svc.resolve_memory_candidates(user_id="u1"))
    _run(svc.emit_memory_candidates(user_id="u1", created_count=1))
    assert _run(store.notification_count(user_id="u1")).action_required == 1


def test_a_new_batch_of_suggestions_shows_its_own_time_not_the_first_batchs(tmp_path, monkeypatch):
    """★★ 116 实测（2026-09-14）：今天 06:10 学到的记忆建议，在首页「待处理」卡上显示「53天前」。
    `memory.candidate` 按用户一条，每批 upsert 同一行，`created_at` 停在 07-23 第一批那天；
    首页与铃铛都渲染 `createdAt`，列表还按它排序。两个发通知入口（新建议 / 即将过期）都要刷新。

    ★ 突变验证：去掉 `emit_memory_candidates` 的 `refresh_created_at=True` ⇒ 红；
    去掉 `emit_memory_candidates_expiring` 那处 ⇒ 红。
    """
    import bowhead_core.notification_store as module

    clock = {"t": "2026-07-23T07:15:42+00:00"}
    monkeypatch.setattr(module, "_now", lambda: clock["t"])
    svc, _store = _svc(tmp_path)

    first = _run(svc.emit_memory_candidates(user_id="u1", created_count=1))
    clock["t"] = "2026-09-14T06:10:39+00:00"
    second = _run(svc.emit_memory_candidates(user_id="u1", created_count=2))
    assert second.id == first.id
    assert second.created_at == "2026-09-14T06:10:39+00:00", "今天这批建议必须显示今天"

    clock["t"] = "2026-09-15T00:00:00+00:00"
    expiring = _run(svc.emit_memory_candidates_expiring(user_id="u1", expiring_count=1))
    assert expiring.id == first.id
    assert expiring.created_at == "2026-09-15T00:00:00+00:00", "即将过期的提醒也是新的一次"


def test_expiring_candidates_link_to_settings_memory(tmp_path):
    """The standalone `/memory` page no longer exists; both candidate notices open Settings > Memory."""
    svc, _store = _svc(tmp_path)
    item = _run(svc.emit_memory_candidates_expiring(user_id="u1", expiring_count=3))
    assert item is not None
    assert item.action_url == "/settings/memory/"
    assert item.actions[0]["url"] == "/settings/memory/"
