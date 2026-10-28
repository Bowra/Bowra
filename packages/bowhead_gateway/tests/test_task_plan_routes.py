"""Sprint multi-agent-v1.5 PR-#20 — task-plan WebSocket route 单测."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from bowhead_core.agents.complexity import Complexity
from bowhead_core.agents.task_plan import (
    TaskPlanOutcome,
    UserDecision,
    build_task_plan,
)
from bowhead_gateway.routes.task_plan import (
    _active_ws,
    _pending_decisions,
    get_task_plan_service_for_session,
    router,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    # dev-mode test: resolve_ws_user_id / _status auth resolve to the dev stub (session_store None →
    # WS ownership skipped). A non-dev app would close the WS (1008) / 401 the _status endpoint.
    app.state.settings = SimpleNamespace(dev_default_user="u_dev", auth_mode="dev")
    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    """每 test 起始 clear module state."""
    _active_ws.clear()
    _pending_decisions.clear()
    yield
    _active_ws.clear()
    _pending_decisions.clear()


# -- Status endpoint --


def test_status_endpoint_empty(client: TestClient) -> None:
    r = client.get("/ws/task-plan/_status")
    assert r.status_code == 200
    body = r.json()
    assert body["active_ws_sessions"] == []
    assert body["pending_decisions"] == []


# -- WebSocket lifecycle --


def test_ws_connect_disconnect(client: TestClient) -> None:
    with client.websocket_connect("/ws/task-plan/s_test"):
        # registered as active
        r = client.get("/ws/task-plan/_status")
        assert "s_test" in r.json()["active_ws_sessions"]
        # Disconnect without sending any message.
    # active cleared after disconnection.
    r = client.get("/ws/task-plan/_status")
    assert r.json()["active_ws_sessions"] == []


def test_ws_ping_pong(client: TestClient) -> None:
    with client.websocket_connect("/ws/task-plan/s_test") as ws:
        ws.send_json({"type": "ping"})
        msg = ws.receive_json()
        assert msg["type"] == "pong"


def test_ws_unknown_message_no_crash(client: TestClient) -> None:
    """未知 type 不应崩 ws."""
    with client.websocket_connect("/ws/task-plan/s_test") as ws:
        ws.send_json({"type": "unknown"})
        # No reply, but next ping still works.
        ws.send_json({"type": "ping"})
        msg = ws.receive_json()
        assert msg["type"] == "pong"


# -- TaskPlanCardService factory --


@pytest.mark.asyncio
async def test_service_emit_plan_no_ws_silent() -> None:
    """没 WS connect 时 emit_plan 应静默 log, 不 crash."""
    service = get_task_plan_service_for_session("s_no_ws")
    # Direct emit_plan call must not raise.
    plan = build_task_plan(
        user_question="q",
        routing_decision_reason="r",
        complexity=Complexity.HIGH,
        tasks_with_estimates=[],
    )
    await service.emit_plan(plan)  # Silently OK.
    await service.emit_warning(plan, 60)  # Silently OK.


@pytest.mark.asyncio
async def test_service_wait_decision_timeout() -> None:
    """没人发 decision → 返 None (timeout)."""
    service = get_task_plan_service_for_session("s_timeout")
    # Use 0.1s timeout to speed up test.
    decision = await service.wait_decision("s_timeout", 1)  # 1 second
    # wait_for's internal 1s timeout → return None.
    assert decision is None


# -- end-to-end via TestClient --


def test_ws_decision_resolves_pending_future(client: TestClient) -> None:
    """模拟: backend 注册 future + frontend send decision msg → future resolved."""
    session_id = "s_e2e"

    # Setup: simulate backend awaiting decision
    loop = asyncio.new_event_loop()
    future: asyncio.Future[UserDecision] = loop.create_future()
    _pending_decisions[session_id] = future

    with client.websocket_connect(f"/ws/task-plan/{session_id}") as ws:
        # Frontend sends decision.
        ws.send_json(
            {
                "type": "decision",
                "outcome": "confirm",
                "nonce": "test-nonce",
                "decided_at": int(time.time()),
            }
        )
        # WS handler runs synchronously on event loop; brief sleep lets it run.
        time.sleep(0.1)

    # Future should resolve.
    assert future.done()
    decision = future.result()
    assert decision.outcome == TaskPlanOutcome.CONFIRM
    assert decision.nonce == "test-nonce"


def test_ws_decision_with_edit_outcome(client: TestClient) -> None:
    session_id = "s_edit"
    loop = asyncio.new_event_loop()
    future: asyncio.Future[UserDecision] = loop.create_future()
    _pending_decisions[session_id] = future

    with client.websocket_connect(f"/ws/task-plan/{session_id}") as ws:
        ws.send_json(
            {
                "type": "decision",
                "outcome": "edit",
                "nonce": "test-nonce-2",
                "decided_at": int(time.time()),
                "edited_plan": {"tasks": ["x"]},
            }
        )
        time.sleep(0.1)

    assert future.done()
    decision = future.result()
    assert decision.outcome == TaskPlanOutcome.EDIT
    assert decision.edited_plan == {"tasks": ["x"]}


def test_ws_decision_no_pending_future_silent(client: TestClient) -> None:
    """收到 decision 但没 pending future, 不应 crash."""
    session_id = "s_no_future"
    with client.websocket_connect(f"/ws/task-plan/{session_id}") as ws:
        ws.send_json(
            {
                "type": "decision",
                "outcome": "confirm",
                "nonce": "x",
                "decided_at": int(time.time()),
            }
        )
        time.sleep(0.05)
        # WS still alive (next ping works)
        ws.send_json({"type": "ping"})
        msg = ws.receive_json()
        assert msg["type"] == "pong"


def test_ws_replaces_old_session_connection(client: TestClient) -> None:
    """同 session 新连进, 老的应自动 close."""
    session_id = "s_replace"
    with client.websocket_connect(f"/ws/task-plan/{session_id}"):
        # First connection registered
        r = client.get("/ws/task-plan/_status")
        assert session_id in r.json()["active_ws_sessions"]
        # Connect again — should auto-close ws1
        with client.websocket_connect(f"/ws/task-plan/{session_id}") as ws2:
            r = client.get("/ws/task-plan/_status")
            assert session_id in r.json()["active_ws_sessions"]
            # Ping ws2 should work
            ws2.send_json({"type": "ping"})
            msg = ws2.receive_json()
            assert msg["type"] == "pong"
