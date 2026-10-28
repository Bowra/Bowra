from __future__ import annotations

from types import SimpleNamespace

import httpx2
import pytest
from bowhead_core.entitlements import EntitlementDenied
from bowhead_core.terminal_protocol import (
    TerminalProtocolFutureVersionError,
    TerminalScope,
    TerminalStatus,
)
from bowhead_core.terminal_protocol_store import (
    SessionActiveClaimSummary,
    SessionProtocolFacts,
)
from bowhead_core.terminal_runtime_factory import (
    TerminalRuntimeAdmissionError,
    TerminalWriterSnapshot,
)
from bowhead_gateway.datarc_context import DatarcTurnContext
from bowhead_gateway.main import create_app
from bowhead_gateway.routes import auth as auth_routes
from bowhead_gateway.routes import chat as chat_routes
from bowhead_gateway.settings import Settings

from bowhead_core import get_event_registry


class FakeSessionStore:
    def __init__(self, active_turn_id: str | None, append_result: bool = True) -> None:
        self.active_turn_id = active_turn_id
        self.append_result = append_result
        self.append_calls: list[dict[str, str | None]] = []

    async def get_active_turn(self, session_id: str) -> str | None:
        return self.active_turn_id

    async def append_steer_buffer(
        self,
        *,
        session_id: str,
        text: str,
        expected_turn_id: str | None = None,
    ) -> bool:
        self.append_calls.append(
            {
                "session_id": session_id,
                "text": text,
                "expected_turn_id": expected_turn_id,
            }
        )
        return self.append_result


class FakeOrchestrator:
    def __init__(self, store: FakeSessionStore) -> None:
        self.session_store = store


class ActiveV1SessionStore:
    def __init__(
        self,
        *,
        owner: str = "u_owner",
        active_identities: tuple[tuple[str, int | None], ...] = (("t_v1", 1),),
    ) -> None:
        self.owner = owner
        self.active_identities = active_identities
        self.identity_reads = 0

    async def get_session(self, session_id: str):
        assert session_id == "s_v1"
        return SimpleNamespace(
            id=session_id,
            user_id=self.owner,
            workspace_id="ws_1",
            deleted_at=None,
        )

    async def get_active_turn_identity(self, session_id: str):
        assert session_id == "s_v1"
        index = min(self.identity_reads, len(self.active_identities) - 1)
        self.identity_reads += 1
        return self.active_identities[index]


class ActiveV1UserStore:
    def __init__(self, *, member_role: str | None) -> None:
        self.member_role = member_role
        self.add_member_calls = 0

    async def get_workspace_team_id(self, workspace_id: str) -> str:
        assert workspace_id == "ws_1"
        return "team_1"

    async def get_user(self, user_id: str):
        return SimpleNamespace(
            id=user_id,
            status="active",
            credit_balance=100,
        )

    async def get_team(self, team_id: str):
        assert team_id == "team_1"
        return SimpleNamespace(team_credit_balance=100)

    async def get_team_llm_config_with_version(self, team_id: str):
        assert team_id == "team_1"
        return {}, 0

    async def get_team_role(self, user_id: str, team_id: str) -> str:
        assert user_id
        assert team_id == "team_1"
        return "member"

    async def get_member_role(self, workspace_id: str, user_id: str) -> str | None:
        assert workspace_id == "ws_1"
        assert user_id
        return self.member_role

    async def get_workspace(self, workspace_id: str) -> dict[str, str]:
        assert workspace_id == "ws_1"
        return {"id": workspace_id, "status": "active", "team_id": "team_1"}

    async def add_member(self, **_kwargs) -> None:
        self.add_member_calls += 1


class ActiveV1Factory:
    def __init__(self, events: list[str]) -> None:
        self.steer_calls = 0
        self.durable_steer_calls = 0
        self.control_calls = 0
        self.cancel_commit_calls = 0
        self.recovery_calls = 0
        self.current_digest = "auth_digest"
        self.events = events
        self.cancel_status = TerminalStatus.CANCELLED
        self.last_steer: dict[str, object] | None = None

    async def prepare_web_auth_fence(self, **_kwargs) -> str:
        return self.current_digest

    async def steer_web_turn(self, **kwargs):
        self.steer_calls += 1
        self.last_steer = kwargs
        if kwargs["auth_fence_vector_digest"] != self.current_digest:
            raise TerminalRuntimeAdmissionError("terminal_auth_fence_changed")
        self.durable_steer_calls += 1
        return SimpleNamespace(
            active_turn_id="t_v1",
            active_generation=1,
            scope=TerminalScope("team_1", "s_v1", "t_v1", 1),
        )

    async def authorize_active_web_control(self, **_kwargs):
        self.control_calls += 1
        return SimpleNamespace(turn_id="t_v1", generation=1)

    async def commit_active_web_cancel(self, _scope):
        self.cancel_commit_calls += 1
        self.events.append("commit")
        return SimpleNamespace(status=self.cancel_status)

    async def trigger_recovery(self, *_args, **_kwargs) -> None:
        self.recovery_calls += 1


class ActiveV1Orchestrator:
    def __init__(self, store: ActiveV1SessionStore, events: list[str]) -> None:
        self.session_store = store
        self.cancel_calls: list[str] = []
        self.events = events
        self.fail_signal = False

    def signal_cancel_generation(self, turn_id: str, generation: int) -> bool:
        self.events.append("signal")
        if self.fail_signal:
            raise RuntimeError("generation signal unavailable")
        self.cancel_calls.append(f"{turn_id}:{generation}")
        return True


class ActiveV1ProtocolStore:
    def __init__(self, session_store: ActiveV1SessionStore) -> None:
        self.session_store = session_store

    async def read_session_protocol_facts(self, **_kwargs):
        turn_id, version = await self.session_store.get_active_turn_identity(
            "s_v1",
        )
        if version is None:
            return SessionProtocolFacts(
                has_v1_history=False,
                legacy_active_turn_id=turn_id,
            )
        if version != 1:
            raise TerminalProtocolFutureVersionError("future")
        return SessionProtocolFacts(
            has_v1_history=True,
            v1_active_claim=SessionActiveClaimSummary(
                turn_id=turn_id,
                protocol_version=1,
                generation=1,
                claim_epoch=1,
            ),
        )

    async def read_current_scope(self, *, team_id: str, turn_id: str):
        assert team_id == "team_1"
        assert turn_id == "t_v1"
        return TerminalScope("team_1", "s_v1", "t_v1", 1)

    async def read_web_chat_action(self, **_kwargs):
        return None


class FrozenEntitlements:
    def __init__(self) -> None:
        self.calls = 0

    async def assert_can_run_turn(self, team_id: str):
        assert team_id == "team_1"
        self.calls += 1
        raise EntitlementDenied(
            "suspended",
            403,
            "team is frozen",
        )


def _active_v1_app(
    *,
    session_owner: str,
    member_role: str | None,
    active_identities: tuple[tuple[str, int | None], ...] = (("t_v1", 1),),
):
    app = create_app()
    app.state.settings = Settings()
    user_store = ActiveV1UserStore(member_role=member_role)
    events: list[str] = []
    orchestrator = ActiveV1Orchestrator(
        ActiveV1SessionStore(
            owner=session_owner,
            active_identities=active_identities,
        ),
        events,
    )
    factory = ActiveV1Factory(events)
    app.state.user_store = user_store
    app.state.orchestrator = orchestrator
    app.state.session_store = orchestrator.session_store
    app.state.access_control_store = None
    app.state.terminal_runtime_factory = factory
    app.state.terminal_protocol_store = ActiveV1ProtocolStore(
        orchestrator.session_store,
    )
    app.state.terminal_writer_service = SimpleNamespace(
        ready=True,
        fresh_admission_ready=False,
        writer_snapshot=lambda: TerminalWriterSnapshot(
            instance_id="gateway_1",
            lease_epoch=1,
            admission_epoch=1,
            rollout_config_generation="test",
            recovery_only=True,
        ),
    )
    return app, user_store, orchestrator, factory


@pytest.mark.asyncio
async def test_chat_send_stale_expected_turn_completes_without_buffer_or_fresh_turn() -> None:
    store = FakeSessionStore(active_turn_id="t_new")
    app = create_app()
    app.state.settings = Settings()
    app.state.orchestrator = FakeOrchestrator(store)

    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        resp = await client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": "ws_1",
                "session_id": "s_1",
                "message": "stale steer",
                "client_info": {
                    "expected_turn_id": "t_old",
                    "user_msg_id": "u_1",
                },
            },
        )

    assert resp.status_code == 200
    assert resp.json() == {
        "session_id": "s_1",
        "turn_id": "t_new",
        "stream_url": "",
        "status": "completed",
        "execution_generation": 1,
    }
    assert store.append_calls == []


@pytest.mark.asyncio
async def test_chat_send_matching_expected_turn_buffers_with_atomic_guard() -> None:
    store = FakeSessionStore(active_turn_id="t_active")
    app = create_app()
    app.state.settings = Settings()
    app.state.orchestrator = FakeOrchestrator(store)

    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        resp = await client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": "ws_1",
                "session_id": "s_1",
                "message": "取消",
                "client_info": {
                    "expected_turn_id": "t_active",
                    "user_msg_id": "u_1",
                },
            },
        )

    assert resp.status_code == 200
    assert resp.json() == {
        "session_id": "s_1",
        "turn_id": "t_active",
        "stream_url": "/api/v1/chat/stream/t_active",
        "status": "buffered",
        "execution_generation": 1,
    }
    assert store.append_calls == [
        {
            "session_id": "s_1",
            "text": "取消",
            "expected_turn_id": "t_active",
        }
    ]
    await get_event_registry().close("t_active")


@pytest.mark.asyncio
async def test_chat_send_active_legacy_steer_with_attachments_fails_before_buffer() -> None:
    store = FakeSessionStore(active_turn_id="t_active")
    app = create_app()
    app.state.settings = Settings()
    app.state.orchestrator = FakeOrchestrator(store)

    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": "ws_1",
                "session_id": "s_1",
                "message": "带附件的补充",
                "attachments": ["art_1"],
                "client_info": {
                    "expected_turn_id": "t_active",
                    "user_msg_id": "u_1",
                },
            },
        )

    assert response.status_code == 422
    assert response.json()["detail"]["error"]["code"] == ("request_validation_failed")
    assert store.append_calls == []


@pytest.mark.asyncio
async def test_active_v1_steer_flag_off_revalidates_revoked_member_without_auto_enroll(
    monkeypatch,
) -> None:
    monkeypatch.setenv("FEATURE_TURN_TERMINAL_PROTOCOL_V1_WRITER", "0")

    async def resolve(_request):
        return "u_owner", "team_1"

    monkeypatch.setattr(auth_routes, "resolve_user_or_stub", resolve)
    app, user_store, orchestrator, factory = _active_v1_app(
        session_owner="u_owner",
        member_role=None,
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": "ws_1",
                "session_id": "s_v1",
                "message": "继续分析",
            },
        )

    assert response.status_code == 403
    assert response.json()["detail"]["error"]["code"] == "access_context_denied"
    assert user_store.add_member_calls == 0
    assert factory.steer_calls == 0
    assert orchestrator.cancel_calls == []


@pytest.mark.asyncio
async def test_future_protocol_flag_off_revalidates_revoked_member_before_version_error(
    monkeypatch,
) -> None:
    monkeypatch.setenv("FEATURE_TURN_TERMINAL_PROTOCOL_V1_WRITER", "0")

    async def resolve(_request):
        return "u_owner", "team_1"

    monkeypatch.setattr(auth_routes, "resolve_user_or_stub", resolve)
    app, user_store, orchestrator, factory = _active_v1_app(
        session_owner="u_owner",
        member_role=None,
        active_identities=(("t_future", 2),),
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
    ) as client:
        response = await client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": "ws_1",
                "session_id": "s_v1",
                "message": "继续分析",
            },
        )

    assert response.status_code == 403
    assert response.json()["detail"]["error"]["code"] == ("access_context_denied")
    assert user_store.add_member_calls == 0
    assert factory.steer_calls == 0
    assert orchestrator.cancel_calls == []


@pytest.mark.asyncio
async def test_legacy_to_future_identity_race_returns_retry_without_mutation(
    monkeypatch,
) -> None:
    monkeypatch.setenv("FEATURE_TURN_TERMINAL_PROTOCOL_V1_WRITER", "0")

    async def resolve(_request):
        return "u_owner", "team_1"

    monkeypatch.setattr(auth_routes, "resolve_user_or_stub", resolve)
    app, user_store, orchestrator, factory = _active_v1_app(
        session_owner="u_owner",
        member_role="member",
        active_identities=(("t_legacy", None), ("t_future", 2)),
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
    ) as client:
        response = await client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": "ws_1",
                "session_id": "s_v1",
                "message": "继续分析",
            },
        )

    assert response.status_code == 503
    assert response.json()["detail"]["error"]["code"] == ("terminal_dependency_recovering")
    assert user_store.add_member_calls == 0
    assert factory.steer_calls == 0
    assert orchestrator.cancel_calls == []
    assert orchestrator.session_store.identity_reads == 2


@pytest.mark.asyncio
async def test_active_v1_former_cancel_text_rejects_cross_user_before_steer(
    monkeypatch,
) -> None:
    monkeypatch.setenv("FEATURE_TURN_TERMINAL_PROTOCOL_V1_WRITER", "0")

    async def resolve(_request):
        return "u_attacker", "team_1"

    monkeypatch.setattr(auth_routes, "resolve_user_or_stub", resolve)
    app, user_store, orchestrator, factory = _active_v1_app(
        session_owner="u_victim",
        member_role="member",
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": "ws_1",
                "session_id": "s_v1",
                "message": "取消",
            },
        )

    assert response.status_code == 403
    assert response.json()["detail"]["error"]["code"] == "access_context_denied"
    assert user_store.add_member_calls == 0
    assert factory.control_calls == 0
    assert orchestrator.cancel_calls == []


@pytest.mark.asyncio
async def test_active_v1_legacy_client_requires_refresh_without_legacy_buffer(
    monkeypatch,
) -> None:
    monkeypatch.setenv("FEATURE_TURN_TERMINAL_PROTOCOL_V1_WRITER", "0")

    async def resolve(_request):
        return "u_owner", "team_1"

    monkeypatch.setattr(auth_routes, "resolve_user_or_stub", resolve)
    app, user_store, orchestrator, factory = _active_v1_app(
        session_owner="u_owner",
        member_role="member",
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": "ws_1",
                "session_id": "s_v1",
                "message": "取消",
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["code"] == ("client_refresh_required")
    assert user_store.add_member_calls == 0
    assert factory.steer_calls == 0
    assert factory.durable_steer_calls == 0
    assert factory.control_calls == 0
    assert factory.cancel_commit_calls == 0
    assert factory.events == []
    assert orchestrator.cancel_calls == []


@pytest.mark.asyncio
async def test_typed_action_steer_miss_uses_exact_persisted_v1_scope_with_latch_off(
    monkeypatch,
) -> None:
    monkeypatch.setenv("FEATURE_TURN_TERMINAL_PROTOCOL_V1_WRITER", "0")

    async def current_user(_request):
        return SimpleNamespace(id="u_owner", team_id="team_1")

    async def datarc_context(*_args, **_kwargs):
        return DatarcTurnContext()

    monkeypatch.setattr(auth_routes, "_current_user", current_user)
    monkeypatch.setattr(
        chat_routes,
        "resolve_datarc_turn_context",
        datarc_context,
    )
    from bowhead_gateway import turn_service

    async def team_runtime(*_args, **_kwargs):
        return turn_service.TeamModelRuntimeResolution(
            router=object(),
            result="team_safe_success",
            reason="resolved",
        )

    monkeypatch.setattr(
        turn_service,
        "resolve_team_model_runtime",
        team_runtime,
    )
    app, _user_store, orchestrator, factory = _active_v1_app(
        session_owner="u_owner",
        member_role="member",
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post(
            "/api/v1/chat/send",
            headers={"x-bowhead-terminal-protocol-capability": "1"},
            json={
                "workspace_id": "ws_1",
                "session_id": "s_v1",
                "message": "继续分析",
                "client_info": {
                    "action_kind": "steer",
                    "expected_execution_generation": "1",
                    "expected_turn_id": "t_v1",
                    "request_id": "act_steer_0123456789abcdef0123456789abcdef",
                    "user_msg_id": "u_r_0123456789abcdef0123456789abcdef",
                },
            },
        )
        stale_generation = await client.post(
            "/api/v1/chat/send",
            headers={"x-bowhead-terminal-protocol-capability": "1"},
            json={
                "workspace_id": "ws_1",
                "session_id": "s_v1",
                "message": "过期 generation",
                "client_info": {
                    "action_kind": "steer",
                    "expected_execution_generation": "2",
                    "expected_turn_id": "t_v1",
                    "request_id": "act_steer_1123456789abcdef0123456789abcdef",
                    "user_msg_id": "u_r_1123456789abcdef0123456789abcdef",
                },
            },
        )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "session_id": "s_v1",
        "turn_id": "t_v1",
        "stream_url": "/api/v1/chat/stream/t_v1",
        "status": "buffered",
        "execution_generation": 1,
        "terminal_protocol_version": 1,
    }
    assert factory.last_steer is not None
    assert factory.last_steer["expected_turn_id"] == "t_v1"
    assert factory.last_steer["expected_execution_generation"] == 1
    assert factory.last_steer["request_id"] == ("act_steer_0123456789abcdef0123456789abcdef")
    assert factory.last_steer["user_msg_id"] == ("u_r_0123456789abcdef0123456789abcdef")
    assert stale_generation.status_code == 409
    assert stale_generation.json()["detail"]["error"]["code"] == ("client_refresh_required")
    assert factory.steer_calls == 1
    assert orchestrator.cancel_calls == []


@pytest.mark.asyncio
async def test_active_v1_former_cancel_text_is_denied_when_entitlement_frozen(
    monkeypatch,
) -> None:
    monkeypatch.setenv("FEATURE_TURN_TERMINAL_PROTOCOL_V1_WRITER", "0")

    async def resolve(_request):
        return "u_owner", "team_1"

    monkeypatch.setattr(auth_routes, "resolve_user_or_stub", resolve)
    app, _user_store, orchestrator, factory = _active_v1_app(
        session_owner="u_owner",
        member_role="member",
    )
    entitlements = FrozenEntitlements()
    app.state.entitlements = entitlements
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": "ws_1",
                "session_id": "s_v1",
                "message": "取消",
            },
        )

    assert response.status_code == 403
    assert response.json()["detail"]["error"]["code"] == "suspended"
    assert entitlements.calls == 1
    assert factory.steer_calls == 0
    assert factory.control_calls == 0
    assert factory.cancel_commit_calls == 0
    assert factory.events == []
    assert orchestrator.cancel_calls == []


@pytest.mark.asyncio
async def test_active_v1_normal_steer_is_denied_when_entitlement_frozen(
    monkeypatch,
) -> None:
    monkeypatch.setenv("FEATURE_TURN_TERMINAL_PROTOCOL_V1_WRITER", "0")

    async def resolve(_request):
        return "u_owner", "team_1"

    monkeypatch.setattr(auth_routes, "resolve_user_or_stub", resolve)
    app, _user_store, orchestrator, factory = _active_v1_app(
        session_owner="u_owner",
        member_role="member",
    )
    entitlements = FrozenEntitlements()
    app.state.entitlements = entitlements
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": "ws_1",
                "session_id": "s_v1",
                "message": "继续分析",
            },
        )

    assert response.status_code == 403
    assert response.json()["detail"]["error"]["code"] == "suspended"
    assert entitlements.calls == 1
    assert factory.steer_calls == 0
    assert orchestrator.cancel_calls == []


@pytest.mark.asyncio
async def test_active_v1_legacy_client_never_reaches_auth_fence(
    monkeypatch,
) -> None:
    monkeypatch.setenv("FEATURE_TURN_TERMINAL_PROTOCOL_V1_WRITER", "0")

    async def resolve(_request):
        return "u_owner", "team_1"

    monkeypatch.setattr(auth_routes, "resolve_user_or_stub", resolve)
    app, user_store, orchestrator, factory = _active_v1_app(
        session_owner="u_owner",
        member_role="member",
    )
    original_member_role = user_store.get_member_role

    async def revoke_after_fence(workspace_id: str, user_id: str):
        role = await original_member_role(workspace_id, user_id)
        factory.current_digest = "auth_changed_after_acl"
        return role

    user_store.get_member_role = revoke_after_fence
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": "ws_1",
                "session_id": "s_v1",
                "message": "继续",
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["code"] == ("client_refresh_required")
    assert factory.steer_calls == 0
    assert factory.durable_steer_calls == 0
    assert orchestrator.cancel_calls == []
