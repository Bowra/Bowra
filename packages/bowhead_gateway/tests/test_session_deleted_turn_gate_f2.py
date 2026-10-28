"""feedback-0713 F2 — 软删会话拒收新 turn（隐形计费收口）.

R1 软删只收敛列表面（store get/ensure 穿透, 锁在 test_canvas_delete_session_r1）,
但穿透面让软删会话仍可发 turn 烧 credit。边界拍板（2026-07-14）:

- web send / clarification resume → **410 拒绝**（不复活: 删除是用户明确决定）
- IM（feishu/teams, 线程↔session 绑定确定性无法换绑）→ 用户再发消息=明确要
  继续用 → **复活**（回列表可见=消除隐形性）
- scheduler（headless 无用户信号）→ run 失败 + job 暂停（照 owner_lost_access 先例）
- steer-drain follow-up → 不 spawn（复用 _drain_ok 清 active 机制）
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx2
import pytest
from bowhead_core.session_store import SessionStore
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings
from bowhead_gateway.turn_service import is_session_deleted


@pytest.fixture
async def store(tmp_path) -> SessionStore:
    s = SessionStore(tmp_path / "s.db")
    await s.initialize()
    return s


# ── Helper semantics ───────────────────────────────────────────────────────


async def test_is_session_deleted_truth_table(store) -> None:
    ses = await store.ensure_session(session_id="s_gate", workspace_id="w", user_id="u1")
    orch = SimpleNamespace(session_store=store)
    assert await is_session_deleted(orch, ses.id) is False  # Live session not blocked.
    await store.soft_delete_session(ses.id)
    assert await is_session_deleted(orch, ses.id) is True  # Soft-deleted session blocked.
    assert await is_session_deleted(orch, "s_missing") is False  # Missing=new session; not blocked.
    assert await is_session_deleted(orch, "") is False
    assert await is_session_deleted(SimpleNamespace(), ses.id) is False  # No store: fail open.


async def test_session_dto_carries_deleted_at(store) -> None:
    """turn 准入判定读 Session.deleted_at —— DTO 必须携带（此前不映射该列）."""
    ses = await store.ensure_session(session_id="s_dto", workspace_id="w", user_id="u1")
    assert (await store.get_session(ses.id)).deleted_at is None
    await store.soft_delete_session(ses.id)
    assert (await store.get_session(ses.id)).deleted_at is not None


# ── revive (IM channel only) ────────────────────────────────────────────────


async def test_revive_session_restores_list_visibility(store) -> None:
    ses = await store.ensure_session(session_id="s_rev", workspace_id="w", user_id="u1")
    await store.soft_delete_session(ses.id)
    assert await store.list_sessions(user_id="u1") == []
    assert await store.revive_session(ses.id) is True
    assert [s.id for s in await store.list_sessions(user_id="u1")] == [ses.id]
    assert (await store.get_session(ses.id)).deleted_at is None
    assert await store.revive_session(ses.id) is False  # Idempotent: undeleted returns False.


# ── HTTP surface: web send 410 ──────────────────────────────────────────────


@pytest.fixture
async def app_ctx(tmp_path):
    sessions = SessionStore(tmp_path / "gw.db")
    await sessions.initialize()
    app = create_app()
    app.state.settings = Settings()
    app.state.session_store = sessions
    # send()'s 410 gate needs only orchestrator.session_store; no other path entered.
    app.state.orchestrator = SimpleNamespace(session_store=sessions)
    app.state.datarc = None
    return app


@pytest.mark.integration
async def test_send_on_soft_deleted_session_410(app_ctx) -> None:
    sessions: SessionStore = app_ctx.state.session_store
    ses = await sessions.ensure_session(session_id="s_dead", workspace_id="w", user_id="u1")
    await sessions.soft_delete_session(ses.id)

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post("/api/v1/chat/send", json={"message": "还在吗", "session_id": ses.id, "workspace_id": "w"})
    assert r.status_code == 410
    assert r.json()["detail"]["error"]["code"] == "session_deleted"
    # Rejection ≠ revival: session stays soft-deleted.
    assert (await sessions.get_session(ses.id)).deleted_at is not None


@pytest.mark.integration
async def test_send_on_live_session_passes_gate(app_ctx) -> None:
    """活会话不被 410 拦（后续可能因 stub orchestrator 报别的错, 只锁门的选择性）."""
    sessions: SessionStore = app_ctx.state.session_store
    ses = await sessions.ensure_session(session_id="s_live", workspace_id="w", user_id="u1")

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post("/api/v1/chat/send", json={"message": "hi", "session_id": ses.id, "workspace_id": "w"})
    assert r.status_code != 410
