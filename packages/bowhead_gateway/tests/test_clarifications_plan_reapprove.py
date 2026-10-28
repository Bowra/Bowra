"""planning-mode-audit WP-2 — plan 预检 picker 的 resolve 闭环 gateway 测试.

锁三件事(review 契约-F4 / 事实-C2):
1. resume_context 带 plan_reapprove_plan_id → resolve 返 action="plan_reapprove"
   + reapprove_plan_id, **绝不** turn_resume(否则在 plan 执行 turn_id 上 spawn
   普通 Lead 轮 = 双烧 token + 覆盖终态)。
2. 已 resolved 的幂等重放同镜像。
3. resume_turn 对该形状 409(防旧 FE)。
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import httpx2
import pytest
from bowhead_core.clarification_store import (
    ClarificationCandidate,
    ClarificationStore,
)
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def app_ctx(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "dev")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")

    clarify = ClarificationStore(tmp_path / "c.db")
    await clarify.initialize()
    with sqlite3.connect(tmp_path / "c.db") as connection:
        connection.execute(
            "CREATE TABLE clarification_resolution_outbox ("
            "request_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, "
            "state TEXT NOT NULL, channel TEXT NOT NULL, work_json TEXT NOT NULL, "
            "payload_json TEXT, lease_owner TEXT, lease_expires_at TEXT, "
            "attempt_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
    from bowhead_core.user_memory import ProjectMemoryStore, UserMemoryStore

    user_mem = UserMemoryStore(tmp_path / "m.db")
    await user_mem.initialize()
    proj_mem = ProjectMemoryStore(tmp_path / "m.db")
    await proj_mem.initialize()
    app = create_app()
    app.state.settings = Settings()
    app.state.settings.auth_mode = "dev"
    app.state.clarification_store = clarify
    app.state.user_memory_store = user_mem
    app.state.project_memory_store = proj_mem
    from bowhead_core.events import get_event_registry

    registry = get_event_registry()
    old_sink = registry._default_log_sink

    async def _accept_durable_event(_event) -> bool:
        return True

    registry.set_log_sink(_accept_durable_event)
    try:
        yield app
    finally:
        registry.set_log_sink(old_sink)


@pytest.fixture
async def http(app_ctx):
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        yield c


async def _seed_plan_picker(app_ctx) -> str:
    store: ClarificationStore = app_ctx.state.clarification_store
    req = await store.put(
        turn_id="t_plan_exec",
        user_id="u_dev_alice",
        workspace_id="ws_1",
        token="",
        rationale="你在北极九章有 2 个可访问的工作区，执行该计划需要先选择用哪个。",
        candidates=[
            ClarificationCandidate(value="1", label="商超零售场景", hint="", confidence="", kind="tool_context"),
            ClarificationCandidate(value="6", label="商户收单数据", hint="", confidence="", kind="tool_context"),
        ],
        kind="tool_context_select",
        multi_select=False,
        session_id="s_1",
        tool="datarc",
    )
    await store.set_resume_context(req.request_id, {"plan_reapprove_plan_id": "plan_abc123"})
    return req.request_id


@pytest.mark.asyncio
async def test_resolve_returns_plan_reapprove_never_turn_resume(http, app_ctx) -> None:
    rid = await _seed_plan_picker(app_ctx)
    r = await http.post(
        f"/api/v1/clarifications/{rid}/resolve",
        json={"chosen_value": "1", "remember": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "plan_reapprove"
    assert body["reapprove_plan_id"] == "plan_abc123"
    assert body["resume_turn_id"] is None


@pytest.mark.asyncio
async def test_resolved_replay_mirrors_plan_reapprove(http, app_ctx) -> None:
    rid = await _seed_plan_picker(app_ctx)
    r1 = await http.post(f"/api/v1/clarifications/{rid}/resolve", json={"chosen_value": "1"})
    assert r1.status_code == 200
    # Idempotent replay (double-click/retry), same image; never switch to turn_resume.
    r2 = await http.post(f"/api/v1/clarifications/{rid}/resolve", json={"chosen_value": "6"})
    assert r2.status_code == 200
    body = r2.json()
    assert body["action"] == "plan_reapprove"
    assert body["reapprove_plan_id"] == "plan_abc123"
    assert body["chosen_value"] == "1"  # First answer locked.


@pytest.mark.asyncio
async def test_resume_turn_rejects_plan_picker_409(http, app_ctx) -> None:
    rid = await _seed_plan_picker(app_ctx)
    await http.post(f"/api/v1/clarifications/{rid}/resolve", json={"chosen_value": "1"})
    r = await http.post(f"/api/v1/clarifications/{rid}/resume_turn")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "clarification_plan_reapprove_required"


@pytest.mark.asyncio
async def test_normal_tool_context_resolve_unaffected(http, app_ctx, monkeypatch) -> None:
    """回归锁: 普通 tool_context_select(带常规 resume_context)仍走 turn_resume。"""
    from bowhead_gateway.routes import clarifications as clarification_routes

    async def _resume_stub(*_args, **_kwargs):
        return clarification_routes.ResumeTurnResultDTO(
            ok=True,
            request_id="req_stub",
            turn_id="t_normal",
            stream_url="/api/v1/chat/stream/t_normal?execution_generation=2",
            execution_generation=2,
        )

    monkeypatch.setattr(clarification_routes, "resume_turn", _resume_stub)

    class _ReadySessionStore:
        async def is_turn_resume_ready(self, _turn_id: str) -> bool:
            return True

    class _Orchestrator:
        session_store = _ReadySessionStore()

    app_ctx.state.orchestrator = _Orchestrator()
    store: ClarificationStore = app_ctx.state.clarification_store
    req = await store.put(
        turn_id="t_normal",
        user_id="u_dev_alice",
        workspace_id="ws_1",
        token="",
        rationale="选工作区",
        candidates=[ClarificationCandidate(value="1", label="a", hint="", confidence="", kind="tool_context")],
        kind="tool_context_select",
        multi_select=False,
        session_id="s_1",
        tool="datarc",
    )
    await store.set_resume_context(req.request_id, {"user_text": "查营业额", "session_id": "s_1", "parent_turn_id": "t_normal"})
    r = await http.post(f"/api/v1/clarifications/{req.request_id}/resolve", json={"chosen_value": "1"})
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "turn_resume"
    assert body["resume_turn_id"] == "t_normal"
    assert body.get("reapprove_plan_id") is None


@pytest.mark.asyncio
async def test_expired_midrun_card_resolves_to_ack_never_reapprove(http, app_ctx) -> None:
    """批3(review R1-F11, 实现 review P1-2 行为断言): 步被修订/重规划 supersede 时
    定向过期的 mid-run 卡——答它必须得 ack(200), 绝不 409、绝不自动 reapprove
    (答一张死卡重跑整张新图=意外执行触发器)。"""
    store: ClarificationStore = app_ctx.state.clarification_store
    req = await store.put(
        turn_id="t_plan_exec_mid",
        user_id="u_dev_alice",
        workspace_id="ws_1",
        token="",
        rationale="执行中需要选择工作区。",
        candidates=[
            ClarificationCandidate(value="1", label="商超零售场景", hint="", confidence="", kind="tool_context"),
        ],
        kind="tool_context_select",
        multi_select=False,
        session_id="s_1",
        tool="datarc",
    )
    await store.set_resume_context(req.request_id, {"plan_midrun_plan_id": "plan_mid1", "session_id": "s_1"})
    expired = await store.expire_pending_for_turn("t_plan_exec_mid", plan_id="plan_mid1")
    assert expired == [req.request_id]
    r = await http.post(f"/api/v1/clarifications/{req.request_id}/resolve", json={"chosen_value": "1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "plan_midrun_ack"
    assert body["skipped"] is True
    assert body.get("reapprove_plan_id") in (None, "")
