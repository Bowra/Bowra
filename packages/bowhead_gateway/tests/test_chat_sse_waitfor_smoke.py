"""R4 smoke: SSE wait_for 包 subscribe() 不丢事件 (W11-8 stall/keepalive 改写后).

背景: chat.py:357 用 `_asyncio.wait_for(sub_iter.__anext__(), timeout=1.0)` 包
bus.subscribe() 实现 stall 探测 + heartbeat. 担心 wait_for cancel 内部 __anext__()
时, 正在 await 的 self._queue.get() 被取消会丢 item. 理论上 asyncio.Queue.get
cancel-safe (waiter 从 _getters list 移除, 不出队), 但需 smoke 实跑确认.

测试方法:
- 不走 orchestrator, 直接对 turn 的 bus emit N=50 个 partial_answer + 1 done
- Receive SSE through httpx2.ASGITransport and count partial_answer events.
- 期望: 50/50 全收到, 顺序保留, dedupe 不误删

也顺带验:
- keepalive 心跳每 ~15s 出 (fast 测短点)
- streaming.state paused 在 STALL_S 内无 chunk 时 emit
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx2
import pytest
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings

from bowhead_core import OrchestratorEvent, get_event_registry

# Real SessionStore objects always expose ``db_path``; the worker hands it to the
# physical-usage receipt observer (cfd302117). The observer only opens it when a
# physical model call starts, and these doubles stub ``run_turn``, so it stays unopened.
_DOUBLE_SESSION_DB_PATH = "/nonexistent/test-session-store.db"


@pytest.fixture(autouse=True)
def _instance_encryption_root(monkeypatch):
    """Deployments always set BOWHEAD_FERNET_KEY; the receipt observer's cipher requires it."""
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")


@pytest.fixture
async def gateway_app_minimal(monkeypatch, completed_community_user_store):
    """最简 gateway, 仅 SSE endpoint 工作 — 不需要 orchestrator/datarc."""
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "dev")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")
    app = create_app()
    app.state.user_store = completed_community_user_store
    app.state.settings = Settings()
    app.state.settings.auth_mode = "dev"
    # chat.py stream endpoint skips replay when turn_log_store/session_store is None,
    # subscribing directly to the bus, exactly the desired path.
    # dev-mode settings so /stream's assert_turn_access resolves the dev stub (session_store None →
    # turn-ownership skipped); a non-dev app would 401 the stream before it starts.
    app.state.settings = SimpleNamespace(dev_default_user="u_dev", auth_mode="dev")
    yield app


@pytest.fixture
async def gateway_app_prod_minimal(monkeypatch, completed_community_user_store):
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    app = create_app()
    app.state.user_store = completed_community_user_store
    app.state.settings = Settings()
    app.state.settings.auth_mode = "prod"
    yield app


@pytest.mark.integration
async def test_sse_requires_auth_without_explicit_dev_fallback(gateway_app_prod_minimal) -> None:
    transport = httpx2.ASGITransport(app=gateway_app_prod_minimal)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        r = await client.get("/api/v1/chat/stream/t_no_auth")
    assert r.status_code == 401


@pytest.mark.integration
async def test_sse_reconnect_projects_durable_parent_child_follow_up(
    gateway_app_minimal,
) -> None:
    class _RecoveryStore:
        dispatch_projection_state = "settled"
        settle_during_relation = False
        relation_visible = True

        async def get_turn(self, turn_id: str):
            if turn_id == "t_recovery_parent":
                return SimpleNamespace(
                    session_id="s_recovery",
                    status="failed",
                    error_kind="dispatch_owner_lost",
                )
            return SimpleNamespace(
                session_id="s_recovery",
                status="failed",
                error_kind="steer_recovery_required",
                execution_generation=4,
            )

        async def get_session(self, _session_id: str):
            return SimpleNamespace(user_id="u_dev", workspace_id="ws_dev")

        async def get_chat_follow_up(self, *, parent_turn_id: str):
            assert parent_turn_id == "t_recovery_parent"
            if not self.relation_visible:
                return None
            if self.settle_during_relation:
                self.dispatch_projection_state = "settled"
                self.settle_during_relation = False
            return ("t_recovery_child", 2)

        async def get_chat_spawn_dispatch_projection_state(
            self,
            *,
            turn_id: str,
        ):
            assert turn_id == "t_recovery_parent"
            return self.dispatch_projection_state

        async def get_chat_turn_user_source(self, **kwargs):
            assert kwargs["turn_id"] == "t_recovery_child"
            return SimpleNamespace(
                attachment_refs=(
                    {
                        "fileId": "art_follow_up",
                        "filename": "follow-up.zip",
                        "size": 42,
                        "mime": "application/zip",
                        "bundleArtifactIds": ["art_child"],
                    },
                ),
            )

    recovery_store = _RecoveryStore()
    gateway_app_minimal.state.session_store = recovery_store
    registry = get_event_registry()
    retired_bus = await registry.get_or_create("t_recovery_parent")
    await retired_bus.emit(
        OrchestratorEvent(
            event_type="emoji_ack",
            turn_id="t_recovery_parent",
            payload={"target_msg_id": "u_live_only", "emoji": "🤝"},
        )
    )
    await registry.close("t_recovery_parent", expected=retired_bus)
    transport = httpx2.ASGITransport(app=gateway_app_minimal)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.get("/api/v1/chat/stream/t_recovery_parent")

    assert response.status_code == 200
    assert "event: turn.follow_up" in response.text
    assert '"follow_up_turn_id": "t_recovery_child"' in response.text
    assert '"reason": "steer_recovery"' in response.text
    assert '"buffered_count": 2' in response.text
    assert '"execution_generation": 4' in response.text
    assert '"fileId": "art_follow_up"' in response.text
    assert '"bundleArtifactIds": ["art_child"]' in response.text
    assert '"seq": 2' in response.text
    assert '"evt_seq": 2' in response.text

    # A different Gateway may see the durable relation while the owner is
    # still draining its ordered log tail.  No local bus exists here, so the
    # dispatch row is the only safe cross-process projection fence.
    recovery_store.dispatch_projection_state = "active"
    recovery_store.settle_during_relation = True
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        active_response = await client.get(
            "/api/v1/chat/stream/t_recovery_parent",
        )
    assert active_response.status_code == 200
    assert "event: turn.follow_up" not in active_response.text
    assert "event: done" not in active_response.text
    assert recovery_store.dispatch_projection_state == "settled"

    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        settled_response = await client.get(
            "/api/v1/chat/stream/t_recovery_parent",
        )
    assert settled_response.text.count("event: turn.follow_up") == 1
    assert settled_response.text.index("event: turn.follow_up") < settled_response.text.index(
        "event: done",
    )

    # Even without a steer relation, a terminal Turn row can precede the
    # remote owner's ordered event tail. Do not synthesize done from a replay
    # snapshot taken while its dispatch was still active.
    recovery_store.relation_visible = False
    recovery_store.dispatch_projection_state = "active"
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        active_without_relation = await client.get(
            "/api/v1/chat/stream/t_recovery_parent",
        )
    assert "event: done" not in active_without_relation.text
    assert "event: turn.follow_up" not in active_without_relation.text

    recovery_store.dispatch_projection_state = "settled"
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        settled_without_relation = await client.get(
            "/api/v1/chat/stream/t_recovery_parent",
        )
    assert "event: turn.follow_up" not in settled_without_relation.text
    assert settled_without_relation.text.count("event: done") == 1

    # The relation can become durable only after an SSE client is already
    # subscribed (persistent follow-up log sink exhaustion).  Closing that bus
    # must project follow-up on the current connection before synthetic done;
    # otherwise the browser accepts done and never reconnects to find the child.
    class _LiveCloseStore:
        terminal = False
        relation_failures = 1

        async def get_turn(self, turn_id: str):
            if turn_id == "t_live_close_parent":
                return SimpleNamespace(
                    session_id="s_live_close",
                    status="failed" if self.terminal else "running",
                    error_kind="dispatch_owner_lost" if self.terminal else None,
                )
            return SimpleNamespace(
                session_id="s_live_close",
                status="failed",
                error_kind="steer_recovery_required",
            )

        async def get_session(self, _session_id: str):
            return SimpleNamespace(user_id="u_dev")

        async def get_chat_follow_up(self, *, parent_turn_id: str):
            assert parent_turn_id == "t_live_close_parent"
            if not self.terminal:
                return None
            if self.relation_failures:
                self.relation_failures -= 1
                raise RuntimeError("transient relation read failure")
            return ("t_live_close_child", 3)

        async def get_chat_spawn_dispatch_projection_state(
            self,
            *,
            turn_id: str,
        ):
            assert turn_id == "t_live_close_parent"
            return "settled"

    live_store = _LiveCloseStore()
    gateway_app_minimal.state.session_store = live_store
    registry = get_event_registry()
    live_bus = await registry.get_or_create("t_live_close_parent")
    subscriber_started = asyncio.Event()
    original_subscribe = live_bus.subscribe

    async def subscribe_with_barrier():
        subscriber_started.set()
        async for event in original_subscribe():
            yield event

    live_bus.subscribe = subscribe_with_barrier
    transport = httpx2.ASGITransport(app=gateway_app_minimal)

    async def close_after_relation_is_durable() -> None:
        # Prove the request has completed its initial absent relation read and
        # entered the live subscribe branch before making the relation durable.
        await subscriber_started.wait()
        live_store.terminal = True
        await registry.close("t_live_close_parent", expected=live_bus)

    close_task = asyncio.create_task(close_after_relation_is_durable())
    try:
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
            first_response = await client.get(
                "/api/v1/chat/stream/t_live_close_parent",
            )
    finally:
        await close_task
        await registry.close("t_live_close_parent")

    # An unavailable relation read is not equivalent to absence.  EOF without
    # done makes EventSource reconnect instead of permanently terminating.
    assert first_response.status_code == 200
    assert "event: done" not in first_response.text
    assert "event: turn.follow_up" not in first_response.text

    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.get("/api/v1/chat/stream/t_live_close_parent")

    assert response.status_code == 200
    follow_up_offset = response.text.index("event: turn.follow_up")
    done_offset = response.text.index("event: done")
    assert follow_up_offset < done_offset
    assert response.text.count("event: turn.follow_up") == 1
    assert '"follow_up_turn_id": "t_live_close_child"' in response.text
    assert '"buffered_count": 3' in response.text
    assert '"seq": 1' in response.text
    assert '"evt_seq": 1' in response.text


@pytest.mark.integration
async def test_sse_live_close_without_relation_waits_for_dispatch_recovery(
    gateway_app_minimal,
) -> None:
    """A closed local bus cannot bypass the pre-replay dispatch fence.

    Preterminal materialization can fail after accepted steer, close the bus,
    and leave recovery to create the relation. The already-connected client
    must EOF without synthetic parent done while dispatch is still active.
    """

    class _RecoveryPendingStore:
        terminal = False
        relation_visible = False
        dispatch_state = "active"

        async def get_turn(self, turn_id: str):
            if turn_id == "t_live_pending_parent":
                return SimpleNamespace(
                    session_id="s_live_pending",
                    status="failed" if self.terminal else "running",
                    error_kind="dispatch_owner_lost" if self.terminal else None,
                )
            return SimpleNamespace(
                session_id="s_live_pending",
                status="failed",
                error_kind="steer_recovery_required",
            )

        async def get_session(self, _session_id: str):
            return SimpleNamespace(user_id="u_dev")

        async def get_chat_follow_up(self, *, parent_turn_id: str):
            assert parent_turn_id == "t_live_pending_parent"
            if not self.relation_visible:
                return None
            return ("t_live_pending_child", 2)

        async def get_chat_spawn_dispatch_projection_state(
            self,
            *,
            turn_id: str,
        ):
            assert turn_id == "t_live_pending_parent"
            return self.dispatch_state

    recovery_store = _RecoveryPendingStore()
    gateway_app_minimal.state.session_store = recovery_store
    registry = get_event_registry()
    live_bus = await registry.get_or_create("t_live_pending_parent")
    subscriber_started = asyncio.Event()
    original_subscribe = live_bus.subscribe

    async def subscribe_with_barrier():
        subscriber_started.set()
        async for event in original_subscribe():
            yield event

    live_bus.subscribe = subscribe_with_barrier
    transport = httpx2.ASGITransport(app=gateway_app_minimal)

    async def close_before_recovery_relation() -> None:
        await subscriber_started.wait()
        recovery_store.terminal = True
        await registry.close("t_live_pending_parent", expected=live_bus)

    close_task = asyncio.create_task(close_before_recovery_relation())
    try:
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
            active_response = await client.get(
                "/api/v1/chat/stream/t_live_pending_parent",
            )
    finally:
        await close_task
        await registry.close("t_live_pending_parent")

    assert "event: done" not in active_response.text
    assert "event: turn.follow_up" not in active_response.text

    recovery_store.relation_visible = True
    recovery_store.dispatch_state = "settled"
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        settled_response = await client.get(
            "/api/v1/chat/stream/t_live_pending_parent",
        )

    assert settled_response.text.count("event: turn.follow_up") == 1
    assert settled_response.text.index("event: turn.follow_up") < settled_response.text.index(
        "event: done",
    )


@pytest.mark.integration
@pytest.mark.parametrize("current_dispatch_state", ["active", "settled"])
async def test_sse_live_close_rechecks_untracked_to_active_projection(
    gateway_app_minimal,
    current_dispatch_state: str,
) -> None:
    """Any post-connect dispatch transition invalidates untracked replay."""

    class _LateSteerStore:
        terminal = False
        dispatch_state = "untracked"

        async def get_turn(self, _turn_id: str):
            return SimpleNamespace(
                session_id="s_late_steer",
                status="failed" if self.terminal else "running",
                error_kind="dispatch_owner_lost" if self.terminal else None,
            )

        async def get_session(self, _session_id: str):
            return SimpleNamespace(user_id="u_dev")

        async def get_chat_follow_up(self, *, parent_turn_id: str):
            assert parent_turn_id == "t_late_steer"
            return

        async def get_chat_spawn_dispatch_projection_state(
            self,
            *,
            turn_id: str,
        ):
            assert turn_id == "t_late_steer"
            return self.dispatch_state

    store = _LateSteerStore()
    gateway_app_minimal.state.session_store = store
    registry = get_event_registry()
    bus = await registry.get_or_create("t_late_steer")
    subscriber_started = asyncio.Event()
    original_subscribe = bus.subscribe

    async def subscribe_with_barrier():
        subscriber_started.set()
        async for event in original_subscribe():
            yield event

    bus.subscribe = subscribe_with_barrier

    async def accept_steer_then_close() -> None:
        await subscriber_started.wait()
        store.dispatch_state = current_dispatch_state
        store.terminal = True
        await registry.close("t_late_steer", expected=bus)

    close_task = asyncio.create_task(accept_steer_then_close())
    try:
        transport = httpx2.ASGITransport(app=gateway_app_minimal)
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
            response = await client.get("/api/v1/chat/stream/t_late_steer")
    finally:
        await close_task
        await registry.close("t_late_steer", expected=bus)

    assert response.status_code == 200
    assert "event: done" not in response.text
    assert "event: turn.follow_up" not in response.text


@pytest.mark.integration
async def test_sse_untracked_legacy_terminal_keeps_synthetic_done_fallback(
    gateway_app_minimal,
) -> None:
    """Legacy Turns have no dispatch row and no owner-loss writer race."""

    class _LegacyStore:
        terminal = False

        async def get_turn(self, _turn_id: str):
            return SimpleNamespace(
                session_id="s_legacy_terminal",
                status="failed" if self.terminal else "running",
                error_kind="legacy_worker_crash" if self.terminal else None,
            )

        async def get_session(self, _session_id: str):
            return SimpleNamespace(user_id="u_dev")

        async def get_chat_follow_up(self, *, parent_turn_id: str):
            assert parent_turn_id == "t_legacy_terminal"
            return

        async def get_chat_spawn_dispatch_projection_state(
            self,
            *,
            turn_id: str,
        ):
            assert turn_id == "t_legacy_terminal"
            return "untracked"

    legacy_store = _LegacyStore()
    gateway_app_minimal.state.session_store = legacy_store
    registry = get_event_registry()
    bus = await registry.get_or_create("t_legacy_terminal")
    subscriber_started = asyncio.Event()
    original_subscribe = bus.subscribe

    async def subscribe_with_barrier():
        subscriber_started.set()
        async for event in original_subscribe():
            yield event

    bus.subscribe = subscribe_with_barrier

    async def close_legacy_bus_without_terminal_event() -> None:
        await subscriber_started.wait()
        legacy_store.terminal = True
        await registry.close("t_legacy_terminal", expected=bus)

    transport = httpx2.ASGITransport(app=gateway_app_minimal)
    close_task = asyncio.create_task(close_legacy_bus_without_terminal_event())
    try:
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
            connected_response = await client.get(
                "/api/v1/chat/stream/t_legacy_terminal",
            )
    finally:
        await close_task
        await registry.close("t_legacy_terminal", expected=bus)

    # StopAsyncIteration guard keeps the original crash fallback.
    assert connected_response.text.count("event: done") == 1
    assert '"status": "failed"' in connected_response.text

    # A later reconnect uses the terminal shortcut and must not loop forever.
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        replay_response = await client.get(
            "/api/v1/chat/stream/t_legacy_terminal",
        )
    assert replay_response.text.count("event: done") == 1
    assert '"status": "failed"' in replay_response.text


@pytest.mark.integration
@pytest.mark.parametrize(
    "terminal_status",
    ("completed", "failed", "cancelled", "aborted"),
)
async def test_sse_synthetic_done_uses_exact_durable_status(
    gateway_app_minimal,
    terminal_status: str,
) -> None:
    """Synthetic replay is transport recovery, not a new business status."""

    class _TerminalStore:
        db_path = _DOUBLE_SESSION_DB_PATH

        async def get_turn(self, _turn_id: str):
            return SimpleNamespace(
                session_id="s_synthetic_exact",
                status=terminal_status,
                error_kind=None,
            )

        async def get_session(self, _session_id: str):
            return SimpleNamespace(user_id="u_dev")

        async def get_chat_follow_up(self, *, parent_turn_id: str):
            assert parent_turn_id == f"t_synthetic_{terminal_status}"
            return

        async def get_chat_spawn_dispatch_projection_state(
            self,
            *,
            turn_id: str,
        ):
            assert turn_id == f"t_synthetic_{terminal_status}"
            return "untracked"

    gateway_app_minimal.state.session_store = _TerminalStore()
    transport = httpx2.ASGITransport(app=gateway_app_minimal)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.get(
            f"/api/v1/chat/stream/t_synthetic_{terminal_status}",
        )

    assert response.text.count("event: done") == 1
    assert f'"status": "{terminal_status}"' in response.text


@pytest.mark.integration
async def test_sse_completed_turn_ignores_legacy_peer_error_terminal(
    gateway_app_minimal,
) -> None:
    """A historical peer error cannot suppress exact synthetic completion."""

    class _CompletedStore:
        async def get_turn(self, _turn_id: str):
            return SimpleNamespace(
                session_id="s_legacy_peer_error",
                status="completed",
                error_kind=None,
            )

        async def get_session(self, _session_id: str):
            return SimpleNamespace(user_id="u_dev")

        async def get_chat_follow_up(self, *, parent_turn_id: str):
            assert parent_turn_id == "t_legacy_peer_error"
            return

        async def get_chat_spawn_dispatch_projection_state(self, *, turn_id: str):
            assert turn_id == "t_legacy_peer_error"
            return "untracked"

    class _LegacyLogStore:
        async def list_events(self, _turn_id: str):
            return [
                SimpleNamespace(
                    event_type="error",
                    occurred_at="2026-07-01T00:00:01+00:00",
                    payload={
                        "parent_agent_id": "sub_legacy",
                        "message": "subagent_internal_error",
                        "evt_seq": 1,
                    },
                )
            ]

    gateway_app_minimal.state.session_store = _CompletedStore()
    gateway_app_minimal.state.turn_log_store = _LegacyLogStore()
    transport = httpx2.ASGITransport(app=gateway_app_minimal)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.get("/api/v1/chat/stream/t_legacy_peer_error")

    assert "event: error" not in response.text
    assert response.text.count("event: done") == 1
    assert '"status": "completed"' in response.text


@pytest.mark.integration
async def test_sse_untracked_relation_is_not_a_parent_tail_barrier(
    gateway_app_minimal,
) -> None:
    """Parent terminal does not settle a legacy producer's ordered event tail."""

    class _LegacyRelationStore:
        async def get_turn(self, turn_id: str):
            if turn_id.endswith("_child"):
                return SimpleNamespace(
                    session_id="s_legacy_relation",
                    status="failed",
                    error_kind="steer_recovery_required",
                )
            return SimpleNamespace(
                session_id="s_legacy_relation",
                status="failed",
                error_kind="legacy_worker_crash",
            )

        async def get_session(self, _session_id: str):
            return SimpleNamespace(user_id="u_dev")

        async def get_chat_follow_up(self, *, parent_turn_id: str):
            assert parent_turn_id == "t_legacy_relation"
            return "t_legacy_relation_child", 1

        async def get_chat_spawn_dispatch_projection_state(
            self,
            *,
            turn_id: str,
        ):
            assert turn_id == "t_legacy_relation"
            return "untracked"

    legacy_store = _LegacyRelationStore()
    gateway_app_minimal.state.session_store = legacy_store
    transport = httpx2.ASGITransport(app=gateway_app_minimal)

    # The child relation can commit after the Turn status but before an older
    # producer's ordered log tail. Without a durable dispatch settlement row,
    # this connection must EOF so a later retry can discover a real barrier.
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        replay_response = await client.get(
            "/api/v1/chat/stream/t_legacy_relation",
        )
    assert "event: turn.follow_up" not in replay_response.text
    assert "event: done" not in replay_response.text


@pytest.mark.integration
async def test_sse_relation_visible_with_registered_bus_drains_live_generation(
    gateway_app_minimal,
) -> None:
    """A visible relation is not a safe projection barrier while its bus lives."""

    class _RelationStore:
        async def get_turn(self, turn_id: str):
            return SimpleNamespace(
                session_id="s_barrier",
                status="completed" if turn_id == "t_barrier_parent" else "running",
                error_kind=None,
            )

        async def get_session(self, _session_id: str):
            return SimpleNamespace(user_id="u_dev")

        async def get_chat_follow_up(self, *, parent_turn_id: str):
            assert parent_turn_id == "t_barrier_parent"
            # Upgrade the subscriber-only generation after the route's first
            # checkpoint.  The post-query checkpoint must observe and drain it.
            assert await registry.get_or_create(parent_turn_id) is bus
            return ("t_barrier_child", 1)

    gateway_app_minimal.state.session_store = _RelationStore()
    registry = get_event_registry()
    bus = await registry.get_or_create(
        "t_barrier_parent",
        producer=False,
    )
    subscriber_started = asyncio.Event()
    original_subscribe = bus.subscribe

    async def subscribe_with_barrier():
        subscriber_started.set()
        async for event in original_subscribe():
            yield event

    bus.subscribe = subscribe_with_barrier
    await bus.emit(
        OrchestratorEvent(
            event_type="partial_answer",
            turn_id="t_barrier_parent",
            payload={"text": "prior"},
        )
    )

    async def finish_live_generation() -> None:
        await subscriber_started.wait()
        await bus.emit(
            OrchestratorEvent(
                event_type="turn.follow_up",  # type: ignore[arg-type]
                turn_id="t_barrier_parent",
                payload={
                    "follow_up_turn_id": "t_barrier_child",
                    "follow_up_stream_url": "/api/v1/chat/stream/t_barrier_child",
                    "reason": "steer_drain",
                    "buffered_count": 1,
                },
            )
        )
        await bus.emit(
            OrchestratorEvent(
                event_type="done",
                turn_id="t_barrier_parent",
                payload={"turn_id": "t_barrier_parent", "status": "completed"},
            )
        )

    finish_task = asyncio.create_task(finish_live_generation())
    try:
        transport = httpx2.ASGITransport(app=gateway_app_minimal)
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
            response = await client.get("/api/v1/chat/stream/t_barrier_parent")
    finally:
        await finish_task
        await registry.close("t_barrier_parent", expected=bus)

    assert response.status_code == 200
    assert response.text.count("event: turn.follow_up") == 1
    assert '"seq": 2' in response.text
    assert '"evt_seq": 2' in response.text
    assert response.text.index("event: partial_answer") < response.text.index("event: turn.follow_up")
    assert response.text.index("event: turn.follow_up") < response.text.index("event: done")


@pytest.mark.integration
async def test_sse_storage_outage_without_producer_eofs_instead_of_creating_bus(
    gateway_app_minimal,
) -> None:
    class _UnavailableStore:
        outage = False
        turn_reads = 0

        async def get_turn(self, _turn_id: str):
            if not self.outage:
                return SimpleNamespace(
                    session_id="s_outage",
                    status="running",
                    error_kind=None,
                )
            self.turn_reads += 1
            if self.turn_reads == 1:
                # Pre-stream authorization succeeds; the storage outage begins
                # during the generator's durable discovery reads.
                return SimpleNamespace(
                    session_id="s_outage",
                    status="running",
                    error_kind=None,
                )
            raise RuntimeError("turn query unavailable")

        async def get_session(self, _session_id: str):
            return SimpleNamespace(user_id="u_dev")

        async def get_chat_follow_up(self, *, parent_turn_id: str):
            assert parent_turn_id == "t_storage_outage"
            if not self.outage:
                return
            raise RuntimeError("relation query unavailable")

    store = _UnavailableStore()
    gateway_app_minimal.state.session_store = store
    registry = get_event_registry()
    # Simulate a mirror/subscriber on a non-owner Gateway.  It must not count as
    # producer provenance, and the SSE route must not create/upgrade one itself.
    subscriber_bus = await registry.get_or_create(
        "t_storage_outage",
        producer=False,
    )
    assert not await registry.emit_if_local_producer(
        "t_storage_outage",
        OrchestratorEvent(
            event_type="emoji_ack",
            turn_id="t_storage_outage",
            payload={"target_msg_id": "u_remote", "emoji": "🤝"},
        ),
    )
    assert subscriber_bus.snapshot() == []
    transport = httpx2.ASGITransport(app=gateway_app_minimal)
    try:
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
            healthy_response = await client.get(
                "/api/v1/chat/stream/t_storage_outage",
            )
        producer_bus, _floor = await registry.projection_checkpoint(
            "t_storage_outage",
        )
        assert healthy_response.status_code == 200
        assert "event: keepalive" not in healthy_response.text
        assert producer_bus is None

        store.outage = True
        store.turn_reads = 0
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
            response = await client.get("/api/v1/chat/stream/t_storage_outage")
    finally:
        await registry.close("t_storage_outage", expected=subscriber_bus)

    producer_bus, _floor = await registry.projection_checkpoint("t_storage_outage")
    assert response.status_code == 200
    assert "event: done" not in response.text
    assert "event: keepalive" not in response.text
    assert producer_bus is None


@pytest.mark.integration
async def test_sse_partial_answer_no_loss(gateway_app_minimal) -> None:
    """50 个 partial_answer + 1 done → SSE 客户端应 100% 收到."""
    transport = httpx2.ASGITransport(app=gateway_app_minimal)
    turn_id = "t_smoke_waitfor_01"
    registry = get_event_registry()
    bus = await registry.get_or_create(turn_id)
    N = 50

    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        # Emit in background. Default fanout: subscription timing is irrelevant; ring retains all events from seq1,
        # so late subscribers read from the start. Legacy flag OFF queues emitted events; late subscription
        # drains everything at once too (unbounded queue).
        async def producer() -> None:
            await asyncio.sleep(0.05)  # Let stream subscribe first.
            for i in range(N):
                await bus.emit(
                    OrchestratorEvent(
                        event_type="partial_answer",
                        turn_id=turn_id,
                        payload={"text": f"chunk {i}", "delta": f"chunk {i}"},
                    )
                )
                # No sleep simulates rapid pushes; verify wait_for(__anext__()) loses no dense events.
            await bus.emit(
                OrchestratorEvent(
                    event_type="done",
                    turn_id=turn_id,
                    payload={"turn_id": turn_id, "status": "completed", "duration_ms": 100, "tool_invocation_count": 0},
                )
            )

        prod_task = asyncio.create_task(producer())

        events: list[dict] = []
        try:
            async with client.stream(
                "GET",
                f"/api/v1/chat/stream/{turn_id}",
                timeout=httpx2.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
            ) as stream:
                cur_event: dict | None = None
                async for line in stream.aiter_lines():
                    line = line.strip()
                    if line.startswith("event:"):
                        cur_event = {"event": line[len("event:") :].strip()}
                    elif line.startswith("data:") and cur_event is not None:
                        try:
                            cur_event["data"] = json.loads(line[len("data:") :].strip())
                        except json.JSONDecodeError:
                            cur_event["data"] = line[len("data:") :].strip()
                        events.append(cur_event)
                        if cur_event["event"] == "done":
                            break
                        cur_event = None
        finally:
            await prod_task
            await registry.close(turn_id)

    # Counts: N partial_answer + 1 done.
    partials = [e for e in events if e["event"] == "partial_answer"]
    dones = [e for e in events if e["event"] == "done"]

    assert len(partials) == N, f"丢事件: 期望 {N} 个 partial_answer, 实收 {len(partials)}"
    assert len(dones) == 1, f"done 事件异常: 实收 {len(dones)}"

    # Order preserved: i increases monotonically across N chunks.
    indices = [int(e["data"]["delta"].split()[1]) for e in partials]
    assert indices == list(range(N)), f"顺序错乱或丢部分: {indices[:10]}..."


@pytest.mark.integration
async def test_sse_stall_detection_emits_paused(gateway_app_minimal) -> None:
    """5 秒无 chunk → SSE endpoint 应 emit streaming.state=paused."""
    transport = httpx2.ASGITransport(app=gateway_app_minimal)
    turn_id = "t_smoke_waitfor_stall_01"
    registry = get_event_registry()
    bus = await registry.get_or_create(turn_id)

    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:

        async def producer() -> None:
            # Emit a partial_answer so client has content.
            await asyncio.sleep(0.05)
            await bus.emit(
                OrchestratorEvent(
                    event_type="partial_answer",
                    turn_id=turn_id,
                    payload={"text": "hello", "delta": "hello"},
                )
            )
            # Wait 31s to trigger STALL_S=30.0 paused (B5: 5s falsely triggered between BI tools, hence 30s).
            await asyncio.sleep(31.0)
            # Emit done so stream exits.
            await bus.emit(
                OrchestratorEvent(
                    event_type="done",
                    turn_id=turn_id,
                    payload={"turn_id": turn_id, "status": "completed", "duration_ms": 100, "tool_invocation_count": 0},
                )
            )

        prod_task = asyncio.create_task(producer())

        events: list[dict] = []
        try:
            async with client.stream(
                "GET",
                f"/api/v1/chat/stream/{turn_id}",
                timeout=httpx2.Timeout(connect=5.0, read=45.0, write=5.0, pool=5.0),
            ) as stream:
                cur_event: dict | None = None
                async for line in stream.aiter_lines():
                    line = line.strip()
                    if line.startswith("event:"):
                        cur_event = {"event": line[len("event:") :].strip()}
                    elif line.startswith("data:") and cur_event is not None:
                        try:
                            cur_event["data"] = json.loads(line[len("data:") :].strip())
                        except json.JSONDecodeError:
                            cur_event["data"] = line[len("data:") :].strip()
                        events.append(cur_event)
                        if cur_event["event"] == "done":
                            break
                        cur_event = None
        finally:
            await prod_task
            await registry.close(turn_id)

    # Expect 1 partial_answer + 1 streaming.state=paused + 1 streaming.state=active + 1 done.
    paused = [e for e in events if e["event"] == "streaming.state" and isinstance(e.get("data"), dict) and e["data"].get("state") == "paused"]
    active = [e for e in events if e["event"] == "streaming.state" and isinstance(e.get("data"), dict) and e["data"].get("state") == "active"]

    assert len(paused) == 1, f"应发 1 个 paused, 实发 {len(paused)} (events: {[e['event'] for e in events]})"
    assert len(active) >= 1, "stall 后真事件来时应 emit active 转换"


# ── Bus contract tests (fast, deterministic, no HTTP) ───────────────
# fix/sse-fanout-bus: default fanout ring buffer; core contract: any number of concurrent
# subscribers each receive all events in the current bus generation, seq1 through terminal. Flag-OFF legacy single-consumer queue
# is the rollback path; pinned-OFF tests preserve old physical behavior.
# HTTP e2e validation remains local dogfood (httpx2 ASGITransport mid-stream disconnect
# teardown is unreliable and hangs).
#
# Implementation discipline (tech-design §5):
# - Wrap all consumer awaits in wait_for(timeout): lost-wakeup bugs must produce quick
#   assertion failures rather than hanging CI;
# - Establish subscribers parked in cond.wait with repeated sleep(0) (entering generator + async with
#   + wait needs at least 2 scheduling steps; one is insufficient);
# - Explicit yields orchestrate interleaving, without gather races.

from bowhead_core.events import (  # noqa: E402
    EventBusRegistry,
    TurnEventBus,
    _fanout_enabled,
)


def _ev(event_type: str, turn_id: str = "t_contract", **payload) -> OrchestratorEvent:
    return OrchestratorEvent(event_type=event_type, turn_id=turn_id, payload={"turn_id": turn_id, **payload})


async def _settle(n: int = 3) -> None:
    """让出 n 个调度步 —— 保证订阅者 task 真正 parked 进 cond.wait."""
    for _ in range(n):
        await asyncio.sleep(0)


async def _drain_subscription(
    bus: TurnEventBus,
    *,
    slow: bool = False,
    timeout: float = 1.0,
) -> list[OrchestratorEvent]:
    """消费一个订阅直到正常终止. slow=True 在每个事件后让出一步,
    逼出 fanout 的 wait→催醒→catch-up 多轮循环路径."""
    out: list[OrchestratorEvent] = []
    it = bus.subscribe().__aiter__()
    while True:
        try:
            ev = await asyncio.wait_for(it.__anext__(), timeout=timeout)
        except StopAsyncIteration:
            return out
        out.append(ev)
        if slow:
            await asyncio.sleep(0)


# ---- Fanout contract ----------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("terminal", ["done", "error"])
async def test_bus_fanout_two_concurrent_subscribers_full_stream(
    monkeypatch,
    terminal,
) -> None:
    """核心新合同: 并发双订阅者(一快一慢)各收逐 seq 相同的全量序列含终态.

    旧单消费者队列在此场景互偷事件 (实证 split [A,C,D]/[B]) —— fanout 根治."""
    monkeypatch.setenv("BOWHEAD_SSE_FANOUT", "1")
    bus = TurnEventBus()
    fast = asyncio.create_task(_drain_subscription(bus))
    slow = asyncio.create_task(_drain_subscription(bus, slow=True))
    await _settle()
    for i in range(10):
        await bus.emit(_ev("partial_answer", i=i))
        await asyncio.sleep(0)  # Explicit yield: deterministic consumer interleaving.
    await bus.emit(_ev(terminal))
    got_fast = await asyncio.wait_for(fast, timeout=2.0)
    got_slow = await asyncio.wait_for(slow, timeout=2.0)
    assert [e.seq for e in got_fast] == list(range(1, 12))
    assert [e.seq for e in got_slow] == list(range(1, 12))
    assert got_fast[-1].event_type == terminal
    assert [e.event_type for e in got_fast] == [e.event_type for e in got_slow]


@pytest.mark.integration
async def test_bus_fanout_mid_turn_attach_gets_full_history(monkeypatch) -> None:
    """mid-turn attach 从 cursor 0 读全量 —— 重连/双 tab 不再依赖 log_store
    replay 补缺 (落库是 fire-and-forget, 有 race)."""
    monkeypatch.setenv("BOWHEAD_SSE_FANOUT", "1")
    bus = TurnEventBus()
    for i in range(3):
        await bus.emit(_ev("tool_result", i=i))
    late = asyncio.create_task(_drain_subscription(bus))
    await _settle()
    await bus.emit(_ev("partial_answer"))
    await bus.emit(_ev("done"))
    got = await asyncio.wait_for(late, timeout=2.0)
    assert [e.seq for e in got] == [1, 2, 3, 4, 5]
    assert got[-1].event_type == "done"


@pytest.mark.integration
async def test_bus_fanout_abandoned_getter_steals_nothing(monkeypatch) -> None:
    """复刻旧 steal 复现步骤 (泄漏 pending __anext__ 不 cancel) —— fanout 下
    弃 getter 只推进自己 generator 的私有 cursor, 其他订阅者仍收全量."""
    monkeypatch.setenv("BOWHEAD_SSE_FANOUT", "1")
    bus = TurnEventBus()
    sub1 = bus.subscribe().__aiter__()
    leaked = asyncio.create_task(sub1.__anext__())
    await _settle()
    other = asyncio.create_task(_drain_subscription(bus))
    await _settle()
    await bus.emit(_ev("done", status="completed"))
    got = await asyncio.wait_for(other, timeout=2.0)
    assert [e.event_type for e in got] == ["done"]
    # Leaked task also receives done (unread task result), harmlessly, without stealing.
    stolen = await asyncio.wait_for(leaked, timeout=1.0)
    assert stolen.event_type == "done"


@pytest.mark.integration
@pytest.mark.parametrize("terminal", ["done", "error"])
async def test_bus_fanout_late_subscriber_after_terminal_gets_history(
    monkeypatch,
    terminal,
) -> None:
    """终态后才 subscribe → 收全量历史 + 正常终止 (bus 已 _closed)."""
    monkeypatch.setenv("BOWHEAD_SSE_FANOUT", "1")
    bus = TurnEventBus()
    await bus.emit(_ev("partial_answer"))
    await bus.emit(_ev(terminal))
    got = await _drain_subscription(bus)
    assert [e.seq for e in got] == [1, 2]
    assert got[-1].event_type == terminal


@pytest.mark.integration
@pytest.mark.parametrize("flag", ["1", "0"])
async def test_bus_registry_close_unblocks_waiting_subscriber_in_both_modes(
    monkeypatch,
    flag,
) -> None:
    """done/error 永不来的 crash 路径: registry.close 唤醒并终止等待中的订阅者
    in both the fanout and rollback queue implementations."""
    monkeypatch.setenv("BOWHEAD_SSE_FANOUT", flag)
    registry = EventBusRegistry()  # Private instance, never global singleton.
    bus = await registry.get_or_create("t_crash")
    sub = asyncio.create_task(_drain_subscription(bus))
    await _settle()
    await bus.emit(_ev("tool_result", turn_id="t_crash"))
    await _settle()  # Subscriber consumes, then parks again in cond.wait.
    await registry.close("t_crash")
    got = await asyncio.wait_for(sub, timeout=2.0)
    assert [e.event_type for e in got] == ["tool_result"]
    producer, _floor = await registry.projection_checkpoint("t_crash")
    assert producer is None
    assert bus._closed
    if flag == "1":
        assert bus.snapshot(), "close 后 ring 数据仍在 (持引用方可读)"


@pytest.mark.integration
async def test_bus_fanout_cancel_in_wait_then_new_subscriber_ok(monkeypatch) -> None:
    """生产最热路径 (每次断连 chat.py finally cancel pending_task): 订阅者在
    cond.wait 中被 cancel → bus 健康, 后续订阅者收全量. 钉死 Condition 的
    cancel-deadlock 史 (3.12 前) 不咬这里."""
    monkeypatch.setenv("BOWHEAD_SSE_FANOUT", "1")
    bus = TurnEventBus()
    dying = asyncio.create_task(_drain_subscription(bus))
    await _settle()  # parked in cond.wait
    dying.cancel()
    await _settle()
    for i in range(3):
        await bus.emit(_ev("partial_answer", i=i))
    await bus.emit(_ev("done"))
    got2 = await _drain_subscription(bus)
    assert [e.seq for e in got2] == [1, 2, 3, 4]
    got3 = await _drain_subscription(bus)  # Third subscriber: Condition does not deadlock.
    assert [e.seq for e in got3] == [1, 2, 3, 4]


@pytest.mark.integration
async def test_bus_fanout_close_then_recreate_same_turn_id(monkeypatch) -> None:
    """默认回收模式: close 已终态的旧 bus → 同 turn_id 新代际正常工作.

    未传 seed 时保留原代际合同: gen-2 seq 重新从 1 计.
    """
    monkeypatch.setenv("BOWHEAD_SSE_FANOUT", "1")
    registry = EventBusRegistry()
    bus1 = await registry.get_or_create("t_resume")
    await bus1.emit(_ev("partial_answer", turn_id="t_resume"))
    await bus1.emit(_ev("done", turn_id="t_resume"))
    assert len(await _drain_subscription(bus1)) == 2
    await registry.close("t_resume")
    bus2 = await registry.get_or_create("t_resume")
    assert bus2 is not bus1
    await bus2.emit(_ev("tool_result", turn_id="t_resume"))
    await bus2.emit(_ev("done", turn_id="t_resume"))
    got = await _drain_subscription(bus2)
    assert [e.seq for e in got] == [1, 2], "gen-2 seq 重新从 1 计 (代际合同)"


@pytest.mark.integration
async def test_bus_fanout_seeded_resume_generation_continues_seq(monkeypatch) -> None:
    """clarification resume 的新 bus 要接在持久化 replay 的 max seq 后面.

    /chat/stream 会先 replay 旧事件, 再 attach live bus. 若 live bus seq 从 1
    重启, FE per-turn seq 去重会把续跑事件当作 replay 重复丢掉.
    """
    monkeypatch.setenv("BOWHEAD_SSE_FANOUT", "1")
    registry = EventBusRegistry()
    bus1 = await registry.get_or_create("t_resume_seed")
    await bus1.emit(_ev("partial_answer", turn_id="t_resume_seed"))
    await bus1.emit(_ev("done", turn_id="t_resume_seed"))
    assert len(await _drain_subscription(bus1)) == 2

    await registry.close("t_resume_seed")
    bus2 = await registry.get_or_create("t_resume_seed", initial_seq=2)
    assert bus2 is not bus1
    await bus2.emit(_ev("tool_result", turn_id="t_resume_seed"))
    await bus2.emit(_ev("done", turn_id="t_resume_seed"))
    got = await _drain_subscription(bus2)
    assert [e.seq for e in got] == [3, 4]


@pytest.mark.integration
async def test_bus_fanout_stale_close_skips_new_generation(monkeypatch) -> None:
    """impl review P1 回归锁: clarification resume 复用同 turn_id (close 旧 bus →
    get_or_create 新代际)。旧 worker 迟到的 deferred close 带 expected 守卫 ——
    registry 已换代时整体跳过, 新代际 bus 不被误关（否则 emit 在落库前早退 =
    事件黑洞）."""
    monkeypatch.setenv("BOWHEAD_SSE_FANOUT", "1")
    registry = EventBusRegistry()
    bus1 = await registry.get_or_create("t_stale_close")
    await bus1.emit(_ev("done", turn_id="t_stale_close"))
    await registry.close("t_stale_close")  # Resume cleanup close.
    bus2 = await registry.get_or_create("t_stale_close")  # New resume generation.
    await registry.close("t_stale_close", expected=bus1)  # Worker1's delayed close.
    assert (await registry.get_or_create("t_stale_close")) is bus2, "stale close 不得把新代际 pop 出 registry"
    assert not bus2._closed, "stale close 不得关掉新代际 bus"
    await bus2.emit(_ev("tool_result", turn_id="t_stale_close"))
    await bus2.emit(_ev("done", turn_id="t_stale_close"))
    got = await _drain_subscription(bus2)
    assert [e.event_type for e in got] == ["tool_result", "done"]
    await registry.close("t_stale_close", expected=bus2)  # Matching guard: normal close.


@pytest.mark.integration
async def test_bus_fanout_emit_after_terminal_is_noop(monkeypatch) -> None:
    """closed bus 上 emit 早退不进 ring —— chat.py crash 分支「无双 error」与
    turn.follow_up 被吞 (§9 留档) 都依赖这个 guard."""
    monkeypatch.setenv("BOWHEAD_SSE_FANOUT", "1")
    bus = TurnEventBus()
    await bus.emit(_ev("done"))
    await bus.emit(_ev("error"))  # Early return.
    got = await _drain_subscription(bus)
    assert [e.event_type for e in got] == ["done"]
    assert len(bus.snapshot()) == 1


@pytest.mark.integration
async def test_worker_crash_path_marks_failed_and_emits_failure_then_done(monkeypatch) -> None:
    """_run_turn_and_cleanup crash 分支 (r3 收窄版): run_turn 抛异常 →
    status 复查后 mark_failed + emit error 进 bus + 守卫式 close."""
    monkeypatch.setenv("BOWHEAD_SSE_FANOUT", "1")
    from bowhead_gateway.routes import chat as chat_mod

    calls: dict[str, object] = {}

    class _Store:
        db_path = _DOUBLE_SESSION_DB_PATH
        status = "running"

        async def get_turn(self, turn_id):
            return SimpleNamespace(status=self.status)

        async def mark_failed(self, *, turn_id, error, error_kind, **kw):
            calls["mark_failed"] = (turn_id, error_kind)
            self.status = "failed"
            return True

    class _Orc:
        session_store = _Store()

        async def run_turn(self, req):
            raise RuntimeError("boom")

    req = SimpleNamespace(
        turn_id="t_crash_ut",
        session_id=None,
        user_id="u_x",
        workspace_id="1",
        execution_generation=1,
        plan_execution_id=None,
    )

    async def _persist_crash_event(_event) -> bool:
        return True

    registry = EventBusRegistry(default_log_sink=_persist_crash_event)
    monkeypatch.setattr(chat_mod, "get_event_registry", lambda: registry)
    bus = await registry.get_or_create(
        "t_crash_ut",
        exclusive_producer=True,
        persist_before_live=True,
    )
    orchestrator = _Orc()
    orchestrator.event_registry = registry
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))  # Fast-forward 5s grace period.
    try:
        await chat_mod._run_turn_and_cleanup(
            orchestrator,
            req,
            admitted_legacy_bus=bus,
            cleanup_grace_s=0,
        )
    finally:
        await registry.close("t_crash_ut")
    assert calls.get("mark_failed") == ("t_crash_ut", "lead_internal_error")
    evs = bus.snapshot()
    assert [e.event_type for e in evs] == ["turn_failed", "done"]
    assert evs[0].payload.get("message") == "internal_error"
    assert evs[0].payload.get("reason_code") == "lead_internal_error"
    assert evs[1].payload.get("error_kind") == "lead_internal_error"
    assert evs[0].payload["diagnostics"] == evs[1].payload["diagnostics"]
    assert evs[0].payload["diagnostics"]["diagnostic_code"] == "chat_turn_failed"
    assert evs[0].payload["diagnostics"]["turn_id"] == "t_crash_ut"
    assert "boom" not in str(evs[0].payload) + str(evs[1].payload)
    assert bus._closed, "error 终态后 bus 应已关闭"


@pytest.mark.integration
async def test_worker_usage_context_accumulates_completed_calls_for_stop(
    monkeypatch,
) -> None:
    """Router call usage reaches the live Turn state before a Stop can win."""

    from bowhead_gateway.routes import chat as chat_mod
    from bowhead_router.usage import record_usage

    turn_id = "t_partial_usage_producer"

    async def _persist(_event) -> bool:
        return True

    registry = EventBusRegistry(default_log_sink=_persist)
    monkeypatch.setattr(chat_mod, "get_event_registry", lambda: registry)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(chat_mod.asyncio, "sleep", lambda _seconds: real_sleep(0))
    bus = await registry.get_or_create(
        turn_id,
        exclusive_producer=True,
        persist_before_live=True,
    )

    class _Orchestrator:
        class _Store:
            db_path = _DOUBLE_SESSION_DB_PATH
            status = "running"

            async def get_turn(self, _turn_id):
                return SimpleNamespace(status=self.status)

        session_store = _Store()

        async def run_turn(self, _req):
            record_usage(
                agent_type="lead",
                provider="test",
                model="test-model",
                tokens_input=11,
                tokens_output=3,
            )
            record_usage(
                agent_type="peer",
                provider="test",
                model="test-model",
                tokens_input=7,
                tokens_output=5,
            )
            self.session_store.status = "completed"
            await bus.emit(
                OrchestratorEvent(
                    event_type="done",
                    turn_id=turn_id,
                    payload={"turn_id": turn_id, "status": "completed"},
                )
            )
            return

    orchestrator = _Orchestrator()
    orchestrator.event_registry = registry
    await chat_mod._run_turn_and_cleanup(
        orchestrator,
        SimpleNamespace(
            turn_id=turn_id,
            session_id=None,
            user_id="u_x",
            workspace_id="ws_1",
            execution_generation=1,
            plan_execution_id=None,
        ),
        admitted_legacy_bus=bus,
        cleanup_grace_s=0,
    )

    assert bus._pi_turn_state["tokens_input"] == 18
    assert bus._pi_turn_state["tokens_output"] == 8
    # Turn-summary llm_count includes Lead + peers (116 previously always reported 1).
    assert bus._turn_llm_call_count == 2


@pytest.mark.integration
async def test_worker_dispatch_owner_loss_defers_all_writes_to_recovery(
    monkeypatch,
) -> None:
    """A stale worker cannot mutate Store or wire state after losing its lease."""
    monkeypatch.setenv("BOWHEAD_SSE_FANOUT", "1")
    from bowhead_gateway.routes import chat as chat_mod

    class _Store:
        db_path = _DOUBLE_SESSION_DB_PATH
        status = "running"

        async def get_turn(self, turn_id):
            return SimpleNamespace(
                status=self.status,
                error_kind=("dispatch_owner_lost" if self.status == "failed" else None),
            )

        async def mark_failed(self, *, turn_id, error, error_kind, **kw):
            raise AssertionError("stale owner must not write canonical failure")

    class _Orc:
        session_store = _Store()

        async def run_turn(self, req):
            raise asyncio.CancelledError("dispatch_owner_lost")

    async def _persist_owner_loss_event(_event) -> bool:
        return True

    turn_id = "t_dispatch_owner_lost_ut"
    req = SimpleNamespace(
        turn_id=turn_id,
        session_id=None,
        user_id="u_x",
        workspace_id="1",
        execution_generation=1,
        plan_execution_id=None,
    )
    registry = EventBusRegistry(default_log_sink=_persist_owner_loss_event)
    monkeypatch.setattr(chat_mod, "get_event_registry", lambda: registry)
    bus = await registry.get_or_create(
        turn_id,
        exclusive_producer=True,
        persist_before_live=True,
    )
    orchestrator = _Orc()
    orchestrator.event_registry = registry
    await chat_mod._run_turn_and_cleanup(
        orchestrator,
        req,
        admitted_legacy_bus=bus,
        cleanup_grace_s=0,
    )

    assert bus.snapshot() == []
    assert not bus._closed
    await registry.close(turn_id, expected=bus)

    class _RecoveredStore:
        db_path = _DOUBLE_SESSION_DB_PATH

        async def get_turn(self, _turn_id):
            return SimpleNamespace(
                status="failed",
                error_kind="dispatch_owner_lost",
            )

        async def mark_failed(self, **_kwargs):
            raise AssertionError("recovery already owns the durable terminal")

    class _RecoveredOrc:
        session_store = _RecoveredStore()

        async def run_turn(self, _req):
            raise asyncio.CancelledError("dispatch_owner_lost")

    recovered_turn_id = "t_dispatch_owner_already_recovered_ut"
    recovered_req = SimpleNamespace(
        turn_id=recovered_turn_id,
        session_id=None,
        user_id="u_x",
        workspace_id="1",
        execution_generation=1,
        plan_execution_id=None,
    )
    recovered_bus = await registry.get_or_create(
        recovered_turn_id,
        exclusive_producer=True,
        persist_before_live=True,
    )
    recovered_orchestrator = _RecoveredOrc()
    recovered_orchestrator.event_registry = registry
    await chat_mod._run_turn_and_cleanup(
        recovered_orchestrator,
        recovered_req,
        admitted_legacy_bus=recovered_bus,
        cleanup_grace_s=0,
    )

    assert recovered_bus.snapshot() == []
    assert not recovered_bus._closed
    await registry.close(recovered_turn_id, expected=recovered_bus)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("owner_loss", "durable_status", "mark_transitioned"),
    [
        (False, "cancelled", None),
        (True, "cancelled", None),
        (False, "completed", None),
        (True, "aborted", None),
        (False, "running", False),
        (True, "running", False),
    ],
)
async def test_worker_failure_after_other_terminal_winner_does_not_emit_failure_terminal(
    monkeypatch,
    owner_loss: bool,
    durable_status: str,
    mark_transitioned: bool | None,
) -> None:
    """Another terminal writer or a lost CAS cannot be replaced by worker failure."""
    monkeypatch.setenv("BOWHEAD_SSE_FANOUT", "1")
    from bowhead_gateway.routes import chat as chat_mod

    persisted = []

    class _TerminalStore:
        db_path = _DOUBLE_SESSION_DB_PATH
        status = durable_status

        async def get_turn(self, _turn_id):
            return SimpleNamespace(status=self.status, error_kind=None)

        async def mark_failed(self, **_kwargs):
            if mark_transitioned is None:
                raise AssertionError("terminal Turn must not transition to failed")
            if not mark_transitioned:
                # Model the canonical terminal writer that won the failed CAS.
                self.status = "cancelled"
            return mark_transitioned

    class _TerminalOrchestrator:
        session_store = _TerminalStore()

        async def run_turn(self, _req):
            if owner_loss:
                raise asyncio.CancelledError("dispatch_owner_lost")
            raise RuntimeError("late worker crash after Stop")

    async def _persist(event) -> bool:
        persisted.append(event)
        return True

    turn_id = f"t_terminal_worker_failure_{durable_status}_{int(owner_loss)}"
    req = SimpleNamespace(
        turn_id=turn_id,
        session_id=None,
        user_id="u_x",
        workspace_id="1",
        execution_generation=1,
        plan_execution_id=None,
    )
    registry = EventBusRegistry(default_log_sink=_persist)
    monkeypatch.setattr(chat_mod, "get_event_registry", lambda: registry)
    bus = await registry.get_or_create(
        turn_id,
        exclusive_producer=True,
        persist_before_live=True,
    )
    orchestrator = _TerminalOrchestrator()
    orchestrator.event_registry = registry

    await chat_mod._run_turn_and_cleanup(
        orchestrator,
        req,
        admitted_legacy_bus=bus,
        cleanup_grace_s=0,
    )

    assert persisted == []
    assert bus.snapshot() == []
    if owner_loss:
        # The stale worker cannot retire transport state.  The spawn task-done
        # fallback closes this exact generation after recovery has decided the
        # durable outcome; model that outer boundary explicitly here.
        assert not bus._closed
        projected_bus, _stable_floor = await registry.projection_checkpoint(turn_id)
        assert projected_bus is bus
        await registry.close(turn_id, expected=bus)
    assert bus._closed
    projected_bus, _stable_floor = await registry.projection_checkpoint(turn_id)
    assert projected_bus is None


@pytest.mark.integration
async def test_generic_worker_failure_after_matching_internal_terminal_does_not_duplicate_terminal_pair(
    monkeypatch,
) -> None:
    """A canonical Store terminal is not duplicated by a retrying worker."""
    monkeypatch.setenv("BOWHEAD_SSE_FANOUT", "1")
    from bowhead_gateway.routes import chat as chat_mod

    class _FailedStore:
        db_path = _DOUBLE_SESSION_DB_PATH

        async def get_turn(self, _turn_id):
            return SimpleNamespace(status="failed", error_kind="internal")

        async def mark_failed(self, **_kwargs):
            raise AssertionError("matching failure is already durable")

    class _FailedOrchestrator:
        session_store = _FailedStore()

        async def run_turn(self, _req):
            raise RuntimeError("same internal failure replay")

    persisted = []

    async def _persist(event) -> bool:
        persisted.append(event)
        return True

    turn_id = "t_worker_matching_internal_failure"
    req = SimpleNamespace(
        turn_id=turn_id,
        session_id=None,
        user_id="u_x",
        workspace_id="1",
        execution_generation=1,
        plan_execution_id=None,
    )
    registry = EventBusRegistry(default_log_sink=_persist)
    monkeypatch.setattr(chat_mod, "get_event_registry", lambda: registry)
    bus = await registry.get_or_create(
        turn_id,
        exclusive_producer=True,
        persist_before_live=True,
    )
    orchestrator = _FailedOrchestrator()
    orchestrator.event_registry = registry

    await chat_mod._run_turn_and_cleanup(
        orchestrator,
        req,
        admitted_legacy_bus=bus,
        cleanup_grace_s=0,
    )

    assert persisted == []
    assert bus.snapshot() == []
    assert bus._closed


def test_fanout_flag_parsing(monkeypatch) -> None:
    """unset→ON / 空串视同 unset→ON (.env 残留 `BOWHEAD_SSE_FANOUT=` 不得
    静默关掉默认 ON) / 大小写空白容忍 / 显式关."""
    cases = [(None, True), ("", True), ("0", False), (" TRUE ", True), ("off", False), ("1", True), ("no", False)]
    for raw, expected in cases:
        if raw is None:
            monkeypatch.delenv("BOWHEAD_SSE_FANOUT", raising=False)
        else:
            monkeypatch.setenv("BOWHEAD_SSE_FANOUT", raw)
        assert _fanout_enabled() is expected, f"raw={raw!r}"


@pytest.mark.integration
@pytest.mark.parametrize("flag", ["1", "0"])
async def test_bus_basic_delivery_both_modes(monkeypatch, flag) -> None:
    """基础投递双模式参数化 —— legacy rollback 路径不止靠 2 条窄测试防 rot."""
    monkeypatch.setenv("BOWHEAD_SSE_FANOUT", flag)
    bus = TurnEventBus()
    sub = asyncio.create_task(_drain_subscription(bus))
    await _settle()
    for i in range(3):
        await bus.emit(_ev("partial_answer", i=i))
    await bus.emit(_ev("done"))
    got = await asyncio.wait_for(sub, timeout=2.0)
    assert [e.seq for e in got] == [1, 2, 3, 4]
    assert got[-1].event_type == "done"


# ---- Flag-OFF legacy invariants (rollback path pinned) --------------------
# These two tests describe legacy single-consumer asyncio.Queue behavior (event stealing/handoff), proving
# chat.py event_gen disconnect teardown (finally cancel pending_task) is still needed for correctness
# with flag OFF. Direct TurnEventBus avoids shared registry/concurrent-test pollution.


@pytest.mark.integration
async def test_bus_abandoned_getter_steals_subsequent_event(monkeypatch) -> None:
    """[legacy/flag-OFF] 根因复现: pending getter 不取消就被遗弃 → 偷走后续
    emit 的事件 → 重连 subscriber 收不到 (dogfood t_be1bcc08)."""
    monkeypatch.setenv("BOWHEAD_SSE_FANOUT", "0")
    turn_id = "t_bus_steal_demo"
    bus = TurnEventBus()
    sub1 = bus.subscribe().__aiter__()
    leaked = asyncio.create_task(sub1.__anext__())
    await asyncio.sleep(0)  # Let leaked wait in queue.get(), registering its getter.

    # Leave leaked uncanceled (old chat.py bug); turn continues emitting done.
    await bus.emit(_ev("done", turn_id=turn_id, status="completed"))
    # Residual getter steals done.
    stolen = await asyncio.wait_for(leaked, timeout=1.0)
    assert getattr(stolen, "event_type", None) == "done"

    # Reconnected subscriber has only SENTINEL → immediate StopAsyncIteration (FE never gets done).
    sub2 = bus.subscribe().__aiter__()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(sub2.__anext__(), timeout=1.0)


@pytest.mark.integration
async def test_bus_cancelled_getter_hands_off_to_reconnect(monkeypatch) -> None:
    """[legacy/flag-OFF] 0.60.1 缓解行为: 断连时取消 pending getter → 后续事件
    留在队列, 交给重连 subscriber."""
    monkeypatch.setenv("BOWHEAD_SSE_FANOUT", "0")
    turn_id = "t_bus_handoff_demo"
    bus = TurnEventBus()
    sub1 = bus.subscribe().__aiter__()
    pending = asyncio.create_task(sub1.__anext__())
    await asyncio.sleep(0)

    # chat.py teardown: cancel pending getter (cancel() synchronously cancels underlying getter
    # future; sleep(0) lets task finish cancellation).
    pending.cancel()
    await asyncio.sleep(0)

    # Turn continues emitting done.
    await bus.emit(_ev("done", turn_id=turn_id, status="completed"))
    # Reconnected subscriber must receive done.
    sub2 = bus.subscribe().__aiter__()
    ev = await asyncio.wait_for(sub2.__anext__(), timeout=1.0)
    assert getattr(ev, "event_type", None) == "done"
