"""批2/c4 — tool_context_select 的 resolve 写偏好分支 + session 软偏好作用域.

PRD §3.3b/c：用户点选工作区 → resolve 端点 server 侧写 user 偏好
（value=id 优先、label 兜底匹配旧 FE）；remember 两态 explicit/session。
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import TYPE_CHECKING

import httpx2
import pytest
from bowhead_core.clarification_store import ClarificationCandidate, ClarificationStore
from bowhead_core.user_memory import ProjectMemoryStore, UserMemoryStore
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


async def _seed(app, *, resume_context: dict | None = None):
    store: ClarificationStore = app.state.clarification_store
    req = await store.put(
        turn_id="t_ws_pick",
        user_id="u_dev_alice",
        workspace_id="ws_a",
        token="",
        rationale="选工作区",
        candidates=[
            ClarificationCandidate(value="42", label="零售集团", kind="tool_context"),
            ClarificationCandidate(value="7", label="华东大区", kind="tool_context"),
        ],
        kind="tool_context_select",
    )
    if resume_context is not None:
        await store.set_resume_context(req.request_id, resume_context)
    return req


@pytest.mark.integration
async def test_resolve_by_value_writes_explicit_pref(http, app_ctx):
    req = await _seed(app_ctx)
    r = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={"chosen_value": "7"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "noop"
    ctx = await app_ctx.state.user_memory_store.get_tool_context("u_dev_alice", "datarc")
    assert ctx == {**ctx, "id": "7", "name": "华东大区", "source": "explicit"}
    assert "session_id" not in ctx


@pytest.mark.integration
async def test_resolve_by_label_fallback_normalizes_to_id(http, app_ctx):
    """旧 FE 只回传 label → server 端 label 兜底匹配、偏好仍写 id."""
    req = await _seed(app_ctx)
    r = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={"chosen_value": "零售集团"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["chosen_value"] == "42"  # Normalize primary to Datarc workspace ID.
    ctx = await app_ctx.state.user_memory_store.get_tool_context("u_dev_alice", "datarc")
    assert ctx["id"] == "42"


@pytest.mark.integration
async def test_resolve_remember_false_writes_session_pref(http, app_ctx):
    req = await _seed(
        app_ctx,
        resume_context={
            "session_id": "s_soft",
            "user_text": "x",
            "plan_reapprove_plan_id": "plan_test_only",
        },
    )
    r = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={"chosen_value": "7", "remember": False},
    )
    assert r.status_code == 200, r.text
    # BUG1 fix: remember=False writes session overlay (@session key), not permanent default.
    assert await app_ctx.state.user_memory_store.get_tool_context("u_dev_alice", "datarc") is None
    sess = await app_ctx.state.user_memory_store.get_tool_context(
        "u_dev_alice",
        "datarc",
        session_scoped=True,
    )
    assert sess["source"] == "session"
    assert sess["session_id"] == "s_soft"


@pytest.mark.integration
async def test_resolve_skip_writes_nothing(http, app_ctx):
    req = await _seed(app_ctx)
    r = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={"skip": True},
    )
    assert r.status_code == 200, r.text
    assert await app_ctx.state.user_memory_store.get_tool_context("u_dev_alice", "datarc") is None


# ─── BUG2 regression: skip '让模型判断' → Datarc workspace directory+routing instruction, not a dead end ──


class _StubOrch:
    def __init__(self):
        self.calls: list = []
        self.session_store = _StubSessionStore()

    async def run_turn(self, req):
        self.calls.append(req)

        class _S:
            tokens_input = 0
            tokens_output = 0

        return _S()


class _StubSessionStore:
    async def is_turn_resume_ready(self, _turn_id: str) -> bool:
        return True

    async def get_session(self, _session_id: str):
        return None

    async def get_turn(self, _turn_id: str):
        return SimpleNamespace(
            status="clarification_pending",
            execution_generation=1,
        )


async def _capture_resume_spawn(orchestrator, turn_req, **_kwargs):
    orchestrator.calls.append(turn_req)
    return SimpleNamespace(execution_generation=2)


@pytest.mark.integration
async def test_skip_routes_via_catalog_not_deadend(http, app_ctx, monkeypatch):
    """BUG2：tool_context_select「让模型判断」(skip) → resumed turn 注入工作区目录
    + 路由指令（用 tool_switch_context 先切再查），而非死胡同空答。"""
    import asyncio

    app_ctx.state.orchestrator = _StubOrch()
    monkeypatch.setattr(
        "bowhead_gateway.turn_service.spawn_resume_turn",
        _capture_resume_spawn,
    )
    req = await _seed(
        app_ctx,
        resume_context={
            "user_text": "今年营业额多少",
            "session_id": "s_skip",
            "parent_turn_id": "t_ws_pick",
            "workspace_id": "ws_a",
            "user_id": "u_dev_alice",
            "member_uid": "m",
            "team_id": None,
            "kb_role": "user",
            "paradigm_v2": False,
            "snapshot_at": "2026-07-09T00:00:00Z",
            "dispatch_window": [],
        },
    )
    r = await http.post(f"/api/v1/clarifications/{req.request_id}/resolve", json={"skip": True})
    assert r.status_code == 200, r.text
    rr = await http.post(f"/api/v1/clarifications/{req.request_id}/resume_turn")
    assert rr.status_code == 200, rr.text
    await asyncio.sleep(0.05)
    orch = app_ctx.state.orchestrator
    assert orch.calls, "resume_turn 应 spawn 一个 turn"
    text = orch.calls[0].text
    assert "tool_switch_context" in text  # Routing instruction.
    assert "零售集团" in text and "华东大区" in text  # Datarc workspace directory.
    assert "今年营业额多少" in text  # Original question retained.


@pytest.mark.integration
async def test_skip_wires_switcher_when_per_user(http, app_ctx, monkeypatch):
    """BUG3（BUG2 完成，真机复现）：per-user 模式下 skip「让模型判断」→ resumed
    TurnRequest 必须带 datarc_context_switcher，否则 Lead 照指令调 tool_switch_context
    会命中 ctx.datarc_context_switcher=None →「本通道不支持会话内切换」→ 空 dead-end。
    与 legacy 对照：legacy 下 None（结构化拒绝，行为不变）。"""
    import asyncio

    app_ctx.state.orchestrator = _StubOrch()
    monkeypatch.setattr(
        "bowhead_gateway.turn_service.spawn_resume_turn",
        _capture_resume_spawn,
    )
    req = await _seed(
        app_ctx,
        resume_context={
            "user_text": "今年营业额多少",
            "session_id": "s_skip_pu",
            "parent_turn_id": "t_ws_pick",
            "workspace_id": "ws_a",
            "user_id": "u_dev_alice",
            "member_uid": "m",
            "team_id": None,
            "kb_role": "user",
            "paradigm_v2": False,
            "snapshot_at": "2026-07-09T00:00:00Z",
            "dispatch_window": [],
        },
    )
    r = await http.post(f"/api/v1/clarifications/{req.request_id}/resolve", json={"skip": True})
    assert r.status_code == 200, r.text
    rr = await http.post(f"/api/v1/clarifications/{req.request_id}/resume_turn")
    assert rr.status_code == 200, rr.text
    await asyncio.sleep(0.05)
    orch = app_ctx.state.orchestrator
    assert orch.calls, "resume_turn 应 spawn 一个 turn"
    assert orch.calls[0].datarc_context_switcher is not None, "per-user resume 必须注入 switcher，否则 tool_switch_context 空转 → dead-end"
    assert callable(orch.calls[0].datarc_context_switcher)


# ─── connector-context T1#6: tool discriminator round-trip (red/green test for B1 broken chain) ──


@pytest.mark.integration
async def test_resolve_uses_row_tool_discriminator(http, app_ctx):
    """store 行 tool="pbi" → resolve 提交进 pbi 的存储键（修前恒 datarc——tool #2
    的选择会被静默写进 datarc 键，B1 跨写事故形状；修后红转绿）."""
    store: ClarificationStore = app_ctx.state.clarification_store
    req = await store.put(
        turn_id="t_ws_pick_pbi",
        user_id="u_dev_alice",
        workspace_id="ws_a",
        token="",
        rationale="选工作区",
        candidates=[
            ClarificationCandidate(value="w1", label="仓库一", kind="tool_context"),
            ClarificationCandidate(value="w2", label="仓库二", kind="tool_context"),
        ],
        kind="tool_context_select",
        tool="pbi",
    )
    r = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={"chosen_value": "w2"},
    )
    assert r.status_code == 200, r.text
    umem = app_ctx.state.user_memory_store
    pbi_ctx = await umem.get_tool_context("u_dev_alice", "pbi")
    assert pbi_ctx is not None and pbi_ctx["id"] == "w2"
    # Cross-write guard: datarc key must stay clean (B1 incident wrote wrong key without errors).
    assert await umem.get_tool_context("u_dev_alice", "datarc") is None


@pytest.mark.integration
async def test_resolve_null_tool_falls_back_datarc(http, app_ctx):
    """行 tool=NULL（改列前存量 pending 行）→ 兜底 "datarc"（back-compat 锁——
    datarc 是改列前唯一写入者，语义精确；新 connector 不得依赖此兜底）."""
    req = await _seed(app_ctx)  # _seed omits tool → NULL.
    r = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={"chosen_value": "7"},
    )
    assert r.status_code == 200, r.text
    ctx = await app_ctx.state.user_memory_store.get_tool_context("u_dev_alice", "datarc")
    assert ctx is not None and ctx["id"] == "7"
