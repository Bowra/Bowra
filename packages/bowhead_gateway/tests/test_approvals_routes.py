"""Approval HTTP routes 集成测试."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from types import SimpleNamespace
from typing import TYPE_CHECKING

import aiosqlite
import httpx2
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.events import EventBusRegistry, OrchestratorEvent
from bowhead_core.notification_service import NotificationService
from bowhead_core.notification_store import NotificationStore
from bowhead_core.session_store import SessionStore
from bowhead_core.tool_registry import ToolRegistry, ToolSpec
from bowhead_core.user_store import UserStore
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings
from bowhead_router.types import ToolSchema

from bowhead_core import ApprovalStore


class _EventSink:
    def __init__(self) -> None:
        self.rows: list[OrchestratorEvent] = []

    async def __call__(self, event: OrchestratorEvent) -> None:
        self.rows.append(event)


class _FakeTurnLog:
    """Minimal turn_log_store — only max_seq() is used by the reject emit path."""

    def __init__(self, seq: int) -> None:
        self.seq = seq

    async def max_seq(self, _turn_id: str) -> int:
        return self.seq


if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def app_ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")
    db = tmp_path / "bowhead.db"
    assert upgrade(f"sqlite+aiosqlite:///{db}", target="core") == ("core",)
    store = ApprovalStore(db_path=str(tmp_path / "approvals.db"))
    await store.initialize()
    users = UserStore(db)
    await users.initialize()
    team = await users.create_team(name="Acme", max_seats=10)
    admin = await users.create_user(team_id=team.id, email="admin@example.com", password="Passw0rd!")
    owner = await users.create_user(team_id=team.id, email="owner@example.com", password="Passw0rd!")
    member = await users.create_user(
        team_id=team.id,
        email="member@example.com",
        password="Passw0rd!",
    )
    await users.set_team_creator(team.id, admin.id)
    async with aiosqlite.connect(db) as connection:
        await connection.execute(
            """
            INSERT INTO workspaces(
              id,team_id,name,name_key,status,revision,created_at,updated_at
            ) VALUES ('w',?,'Legacy test space','legacy test space',
                      'active',1,'2026-01-01T00:00:00+00:00',
                      '2026-01-01T00:00:00+00:00')
            """,
            (team.id,),
        )
        await connection.commit()
    other_team = await users.create_team(name="Other", max_seats=10)
    outsider = await users.create_user(
        team_id=other_team.id,
        email="outsider@example.com",
        password="Passw0rd!",
    )

    app = create_app()
    app.state.settings = Settings()
    app.state.approval_store = store
    app.state.user_store = users
    notification_store = NotificationStore(str(db))
    await notification_store.initialize()
    app.state.notification_store = notification_store
    app.state.notification_service = NotificationService(notification_store, users)
    app.state.notification_center_enabled = True
    app.state.admin_id = admin.id
    app.state.owner_id = owner.id
    app.state.member_id = member.id
    app.state.outsider_id = outsider.id
    app.state.orchestrator = None
    app.state.datarc = None
    app.state.terminal_protocol_store = _LegacyProtocolStore()
    return app


def _as_admin(app_ctx) -> dict[str, str]:
    return {"x-bowhead-user-id": app_ctx.state.admin_id}


def _as_owner(app_ctx) -> dict[str, str]:
    return {"x-bowhead-user-id": app_ctx.state.owner_id}


def _as_member(app_ctx) -> dict[str, str]:
    return {"x-bowhead-user-id": app_ctx.state.member_id}


def _as_outsider(app_ctx) -> dict[str, str]:
    return {"x-bowhead-user-id": app_ctx.state.outsider_id}


class _ProjectMemory:
    async def get_datarc_last_used_project(self, workspace_id: str) -> str | None:
        return None

    async def get_datarc_default_project(self, workspace_id: str) -> str | None:
        return "dp_approval_route"


class _LegacyProtocolStore:
    async def read_persisted_turn_protocol_binding(
        self,
        *,
        team_id: str,
        turn_id: str,
    ) -> tuple[str, None]:
        assert team_id
        assert turn_id
        return "s", None


class _ConnectionStore:
    async def list_connections(self, team_id: str):
        return [SimpleNamespace(id="conn_approval", base_url="https://datarc.example")]

    async def get_connection_auth(self, conn_id: str, team_id: str) -> str:
        return "auth_approval"


class _DatarcFactory:
    def __init__(self, client: object) -> None:
        self.client = client

    def get_client(self, base_url: str, auth_key: str) -> object:
        return self.client


class _MemberResolver:
    """审批执行按 pin 快照重解析审批人的 memberUid（团队连接出的 client 是唯一入口）."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    async def resolve(self, identity: str, datarc_workspace_id: str, *, datarc: object = None) -> str:
        self.calls.append((identity, datarc_workspace_id, datarc))
        return "m_approver"


class _EmbedFactory:
    def __init__(self, client: object) -> None:
        self.client = client

    async def get_verified_client(
        self,
        base_url: str,
        auth_key: str,
        *,
        project_ids: Sequence[str] = (),
    ) -> object:
        # Actual factory now infers collection tenants from visible workspaces; doubles must follow the real contract,
        # or resolve's except swallows signature TypeErrors as no embeddings.
        self.project_ids = list(project_ids)
        return self.client


@pytest.mark.integration
async def test_list_approvals_empty(app_ctx) -> None:
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.get("/api/v1/approvals", headers=_as_admin(app_ctx))
        assert r.status_code == 200
        assert r.json() == {"items": [], "scope": "pending"}


@pytest.mark.integration
async def test_approval_lifecycle(app_ctx) -> None:
    store: ApprovalStore = app_ctx.state.approval_store
    a = await store.enqueue(
        turn_id="t1",
        session_id="s",
        workspace_id="w",
        member_uid="m",
        user_id=app_ctx.state.owner_id,
        tool_name="bi_update_measures",
        arguments={"payload": {}, "reason": "rename"},
        reason="rename",
        risk_level="high",
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        # list pending
        r = await c.get("/api/v1/approvals", headers=_as_admin(app_ctx))
        assert r.status_code == 200
        assert len(r.json()["items"]) == 1

        # get one
        r = await c.get(f"/api/v1/approvals/{a.id}", headers=_as_admin(app_ctx))
        assert r.status_code == 200
        assert r.json()["toolName"] == "bi_update_measures"

        # reject
        r = await c.post(
            f"/api/v1/approvals/{a.id}/reject",
            json={"reason": "risky"},
            headers=_as_admin(app_ctx),
        )
        assert r.status_code == 200
        assert r.json()["status"] == "rejected"

        # second reject is idempotent (still rejected)
        r = await c.post(f"/api/v1/approvals/{a.id}/reject", json={}, headers=_as_admin(app_ctx))
        assert r.json()["status"] == "rejected"

        # 404 for unknown
        r = await c.get("/api/v1/approvals/apr_deadbeefcafe", headers=_as_admin(app_ctx))
        assert r.status_code == 404


@pytest.mark.integration
async def test_reject_resolves_action_notification_and_notifies_requester(app_ctx) -> None:
    store: ApprovalStore = app_ctx.state.approval_store
    notification_service: NotificationService = app_ctx.state.notification_service
    a = await store.enqueue(
        turn_id="t1",
        session_id="s",
        workspace_id="w",
        member_uid="m",
        user_id=app_ctx.state.owner_id,
        tool_name="bi_update_measures",
        arguments={"payload": {}, "reason": "rename"},
        reason="rename",
        risk_level="high",
    )
    await notification_service.emit_approval_required(a)

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.get("/api/v1/notifications/unread-count", headers=_as_admin(app_ctx))
        assert r.status_code == 200
        assert r.json()["actionRequired"] == 1

        r = await c.post(
            f"/api/v1/approvals/{a.id}/reject",
            json={"reason": "risky"},
            headers=_as_admin(app_ctx),
        )
        assert r.status_code == 200
        assert r.json()["status"] == "rejected"

        r = await c.get("/api/v1/notifications/unread-count", headers=_as_admin(app_ctx))
        assert r.status_code == 200
        assert r.json()["actionRequired"] == 0

        r = await c.get("/api/v1/notifications/unread-count", headers=_as_owner(app_ctx))
        assert r.status_code == 200
        assert r.json()["updatesUnread"] == 1


@pytest.mark.integration
async def test_reject_emits_chat_rejected_event_out_of_band(app_ctx) -> None:
    """R-2: reject 追加一条 chat 终态事件 approval.rejected，走 emit_out_of_band。

    审批决策发生在 turn 已终态（done 已发、bus 已 closed）之后，事件必须经
    out-of-band 播种以正确续接 seq、不撞号（chat-pipeline）。
    """
    store: ApprovalStore = app_ctx.state.approval_store
    a = await store.enqueue(
        turn_id="t_reject_evt",
        session_id="s",
        workspace_id="w",
        member_uid="m",
        user_id=app_ctx.state.owner_id,
        tool_name="bi_update_measures",
        arguments={"payload": {}, "reason": "rename"},
        reason="rename",
        risk_level="high",
    )

    sink = _EventSink()
    event_registry = EventBusRegistry(default_log_sink=sink)
    # Simulate the realistic timeline: the turn already reached terminal (done)
    # and its bus is closed by the time the admin decides on the approval.
    bus = await event_registry.get_or_create("t_reject_evt")
    await bus.emit(OrchestratorEvent(event_type="turn_started", turn_id="t_reject_evt", payload={}))
    await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_reject_evt", payload={"status": "completed"}))
    app_ctx.state.orchestrator = SimpleNamespace(event_registry=event_registry)
    app_ctx.state.turn_log_store = _FakeTurnLog(2)

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(
            f"/api/v1/approvals/{a.id}/reject",
            json={"reason": "risky"},
            headers=_as_admin(app_ctx),
        )
        assert r.status_code == 200
        assert r.json()["status"] == "rejected"

    rejected = [e for e in sink.rows if e.event_type == "approval.rejected"]
    assert len(rejected) == 1
    evt = rejected[0]
    # Continue seq ≥max_seq(=2), without restarting at 0 (done seq2 → rejected seq3).
    assert evt.payload["seq"] == 3
    assert evt.payload["card_id"] == "t_reject_evt"
    assert evt.payload["approval_id"] == a.id
    # Closed bus has no subscribers: out-of-band persists via transient bus, never the closed registered bus.
    assert "approval.rejected" not in [e.event_type for e in bus.snapshot()]

    # Idempotent: second reject makes no transition and emits no second event.
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(f"/api/v1/approvals/{a.id}/reject", json={}, headers=_as_admin(app_ctx))
        assert r.json()["status"] == "rejected"
    assert len([e for e in sink.rows if e.event_type == "approval.rejected"]) == 1


@pytest.mark.integration
async def test_reject_seeds_chat_rejected_event_when_turn_bus_is_absent(app_ctx) -> None:
    """>5s 分支：turn bus 已被 pop（registry 无该 turn）时，仍从 max_seq 播种续接。

    这是 emit_out_of_band docstring 点名的 >5s 坑（裸 get_or_create 会建 seq 从 0 的
    新 bus 撞号）。断言 seq 从 max_seq 续接、且不往 registry 塞新 bus（无泄漏）。
    """
    store: ApprovalStore = app_ctx.state.approval_store
    a = await store.enqueue(
        turn_id="t_reject_absent",
        session_id="s",
        workspace_id="w",
        member_uid="m",
        user_id=app_ctx.state.owner_id,
        tool_name="bi_update_measures",
        arguments={"payload": {}, "reason": "rename"},
        reason="rename",
        risk_level="high",
    )

    sink = _EventSink()
    event_registry = EventBusRegistry(default_log_sink=sink)
    # No get_or_create: registry has no turn bus (simulate removal after 5s delayed close).
    app_ctx.state.orchestrator = SimpleNamespace(event_registry=event_registry)
    app_ctx.state.turn_log_store = _FakeTurnLog(16)

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(f"/api/v1/approvals/{a.id}/reject", json={}, headers=_as_admin(app_ctx))
        assert r.status_code == 200
        assert r.json()["status"] == "rejected"

    rejected = [e for e in sink.rows if e.event_type == "approval.rejected"]
    assert len(rejected) == 1
    assert rejected[0].payload["seq"] == 17  # max_seq(16) →17, no collisions from 0.
    assert "t_reject_absent" not in event_registry._buses  # No leaked bus.


@pytest.mark.integration
async def test_reject_without_orchestrator_still_transitions(app_ctx) -> None:
    """无 orchestrator（event_registry 缺席）时 reject 仍成功转状态，只是不补 chat 事件。"""
    store: ApprovalStore = app_ctx.state.approval_store
    a = await store.enqueue(
        turn_id="t_no_orch",
        session_id="s",
        workspace_id="w",
        member_uid="m",
        user_id=app_ctx.state.owner_id,
        tool_name="bi_update_measures",
        arguments={"payload": {}, "reason": "rename"},
        reason="rename",
        risk_level="high",
    )
    # Fixture defaults app.state.orchestrator=None.
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(f"/api/v1/approvals/{a.id}/reject", json={}, headers=_as_admin(app_ctx))
        assert r.status_code == 200
        assert r.json()["status"] == "rejected"


@pytest.mark.integration
async def test_notification_routes_are_recipient_scoped(app_ctx) -> None:
    store: ApprovalStore = app_ctx.state.approval_store
    notification_service: NotificationService = app_ctx.state.notification_service
    approval = await store.enqueue(
        turn_id="t_scope",
        session_id="s",
        workspace_id="w",
        member_uid="m",
        user_id=app_ctx.state.owner_id,
        tool_name="bi_update_measures",
        arguments={"payload": {}, "reason": "rename"},
        reason="rename",
        risk_level="high",
    )
    item = await notification_service.emit_approval_required(approval)
    assert item is not None

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.get("/api/v1/notifications/unread-count", headers=_as_admin(app_ctx))
        assert r.status_code == 200
        assert r.json()["actionRequired"] == 1
        r = await c.get("/api/v1/notifications?tab=action", headers=_as_admin(app_ctx))
        assert r.status_code == 200
        assert [x["id"] for x in r.json()["items"]] == [item.id]

        for headers in (_as_member(app_ctx), _as_owner(app_ctx), _as_outsider(app_ctx)):
            r = await c.get("/api/v1/notifications/unread-count", headers=headers)
            assert r.status_code == 200
            assert r.json() == {"actionRequired": 0, "updatesUnread": 0, "totalBadge": 0}
            r = await c.get("/api/v1/notifications?tab=all", headers=headers)
            assert r.status_code == 200
            assert r.json()["items"] == []
            r = await c.post(f"/api/v1/notifications/{item.id}/archive", headers=headers)
            assert r.status_code == 404


@pytest.mark.integration
async def test_notification_routes_respect_kill_switch(app_ctx) -> None:
    app_ctx.state.notification_center_enabled = False

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.get("/api/v1/notifications/unread-count", headers=_as_admin(app_ctx))
        assert r.status_code == 404
        assert r.json()["detail"] == "notification center disabled"


@pytest.mark.integration
async def test_notification_archive_updates_count_and_list(app_ctx) -> None:
    store: ApprovalStore = app_ctx.state.approval_store
    notification_service: NotificationService = app_ctx.state.notification_service
    approval = await store.enqueue(
        turn_id="t_archive",
        session_id="s",
        workspace_id="w",
        member_uid="m",
        user_id=app_ctx.state.owner_id,
        tool_name="bi_update_measures",
        arguments={"payload": {}, "reason": "rename"},
        reason="rename",
        risk_level="high",
    )
    item = await notification_service.emit_approval_required(approval)
    assert item is not None

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(f"/api/v1/notifications/{item.id}/archive", headers=_as_admin(app_ctx))
        assert r.status_code == 200

        r = await c.get("/api/v1/notifications/unread-count", headers=_as_admin(app_ctx))
        assert r.status_code == 200
        assert r.json()["actionRequired"] == 0
        r = await c.get("/api/v1/notifications?tab=all", headers=_as_admin(app_ctx))
        assert r.status_code == 200
        assert r.json()["items"] == []


@pytest.mark.integration
async def test_approve_without_executor_is_still_recorded(app_ctx) -> None:
    """app_ctx 没配 orchestrator → approve 记录状态但不 spawn exec task."""
    store: ApprovalStore = app_ctx.state.approval_store
    a = await store.enqueue(
        turn_id="t2",
        session_id="s",
        workspace_id="w",
        member_uid="m",
        user_id=app_ctx.state.owner_id,
        tool_name="bi_update_measures",
        arguments={"payload": {}},
        reason=None,
        risk_level="high",
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(f"/api/v1/approvals/{a.id}/approve", json={}, headers=_as_admin(app_ctx))
        assert r.status_code == 200
        assert r.json()["status"] == "approved"

    # Allow background tasks to run, even no-ops.
    await asyncio.sleep(0.1)
    got = await store.get(a.id)
    assert got is not None
    # No executor: remains approved, never executed.
    assert got.status == "approved"


@pytest.mark.integration
async def test_approve_executor_uses_team_datarc_context(app_ctx) -> None:
    captured = {}
    scoped_datarc = object()
    global_datarc = object()
    vector_client = object()

    async def _handler(_args, ctx):
        captured["ctx"] = ctx
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="approval_route_probe",
                description="approval route probe",
                input_schema={"type": "object", "properties": {}},
            ),
            handler=_handler,
        ),
    )
    app_ctx.state.orchestrator = SimpleNamespace(
        tool_registry=registry,
        event_registry=None,
    )
    app_ctx.state.datarc = global_datarc
    app_ctx.state.datarc_connection_store = _ConnectionStore()
    app_ctx.state.datarc_client_factory = _DatarcFactory(scoped_datarc)
    app_ctx.state.embed_client_factory = _EmbedFactory(vector_client)
    app_ctx.state.member_resolver = _MemberResolver()

    users: UserStore = app_ctx.state.user_store
    # **Approver** re-resolves snapshot workspace's member_uid using their own verified identity (approvals.py
    # fail-closed branch); unverified email →403.
    await users.mark_email_verified_atomic(app_ctx.state.admin_id, None)
    admin_team = await users.get_user_team_id(app_ctx.state.admin_id)
    assert admin_team is not None
    ws_id = await users.create_local_workspace(
        team_id=admin_team,
        name="Sales",
        creator_user_id=app_ctx.state.owner_id,
    )
    sessions = SessionStore(users.db_path)
    await sessions.initialize()
    await sessions.ensure_session(session_id="s", workspace_id=ws_id, user_id=app_ctx.state.owner_id)
    await sessions.record_turn(turn_id="t3", session_id="s", user_text="ordinary approved operation")
    store: ApprovalStore = app_ctx.state.approval_store
    approval = await store.enqueue(
        turn_id="t3",
        session_id="s",
        workspace_id=ws_id,
        member_uid="m",
        user_id=app_ctx.state.owner_id,
        tool_name="approval_route_probe",
        arguments={},
        reason="probe",
        # Batch 2/§3.9 decision 10: approval execution uses the Datarc workspace **snapshotted at enqueue** (approvals.py
        # pinned_project), never ws-keyed project_memory preferences; that deployment-/space-level
        # path retired with team-connection consolidation.
        datarc_workspace_id="dp_approval_route",
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(
            f"/api/v1/approvals/{approval.id}/approve",
            json={},
            headers=_as_admin(app_ctx),
        )
        assert r.status_code == 200
        assert r.json()["status"] == "approved"

    for _ in range(20):
        await asyncio.sleep(0.05)
        if captured:
            break

    assert captured
    ctx = captured["ctx"]
    assert ctx.active_datarc_project == "dp_approval_route"
    assert ctx.datarc is scoped_datarc
    assert ctx.datarc is not global_datarc
    assert ctx.vector_client is vector_client
    assert ctx.session_id == "s"
    assert ctx.reference_source_db_path == users.db_path
    # Re-resolve snapshot workspace with approver identity, via team-connection client, never deployment global_datarc.
    assert app_ctx.state.member_resolver.calls == [("admin@example.com", "dp_approval_route", scoped_datarc)]


@pytest.mark.integration
async def test_approve_executor_never_falls_back_to_deployment_datarc(app_ctx) -> None:
    """A team connection that yields no client leaves the executor without Datarc.

    The registry lists a connection but no client factory is wired, so the resolver returns no
    client and does not mark Datarc unavailable. A deployment-level app.state.datarc is mounted
    and must not be used.
    """
    captured = {}
    deployment_datarc = object()

    async def _handler(_args, ctx):
        captured["ctx"] = ctx
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="approval_no_fallback_probe",
                description="approval no-fallback probe",
                input_schema={"type": "object", "properties": {}},
            ),
            handler=_handler,
        ),
    )
    app_ctx.state.orchestrator = SimpleNamespace(
        tool_registry=registry,
        event_registry=None,
    )
    app_ctx.state.datarc = deployment_datarc
    app_ctx.state.datarc_connection_store = _ConnectionStore()
    app_ctx.state.datarc_client_factory = None
    app_ctx.state.member_resolver = _MemberResolver()

    users: UserStore = app_ctx.state.user_store
    admin_team = await users.get_user_team_id(app_ctx.state.admin_id)
    assert admin_team is not None
    ws_id = await users.create_local_workspace(
        team_id=admin_team,
        name="No fallback",
        creator_user_id=app_ctx.state.owner_id,
    )
    sessions = SessionStore(users.db_path)
    await sessions.initialize()
    await sessions.ensure_session(session_id="s", workspace_id=ws_id, user_id=app_ctx.state.owner_id)
    await sessions.record_turn(turn_id="t_nf", session_id="s", user_text="approved operation")
    store: ApprovalStore = app_ctx.state.approval_store
    approval = await store.enqueue(
        turn_id="t_nf",
        session_id="s",
        workspace_id=ws_id,
        member_uid="m",
        user_id=app_ctx.state.owner_id,
        tool_name="approval_no_fallback_probe",
        arguments={},
        reason="probe",
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(
            f"/api/v1/approvals/{approval.id}/approve",
            json={},
            headers=_as_admin(app_ctx),
        )
        assert r.status_code == 200, r.text

    for _ in range(20):
        await asyncio.sleep(0.05)
        if captured:
            break

    assert captured
    ctx = captured["ctx"]
    assert ctx.datarc is None
    assert app_ctx.state.member_resolver.calls == []


@pytest.mark.integration
async def test_approve_prepare_failure_keeps_approval_pending(
    app_ctx,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _handler(_args, _ctx):
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="approval_prepare_probe",
                description="approval prepare probe",
                input_schema={"type": "object", "properties": {}},
            ),
            handler=_handler,
        ),
    )
    app_ctx.state.orchestrator = SimpleNamespace(
        tool_registry=registry,
        event_registry=None,
    )

    async def _raise_context(*_args, **_kwargs):
        raise RuntimeError("datarc context down")

    monkeypatch.setattr(
        "bowhead_gateway.datarc_context.resolve_datarc_turn_context",
        _raise_context,
    )

    users: UserStore = app_ctx.state.user_store
    admin_team = await users.get_user_team_id(app_ctx.state.admin_id)
    assert admin_team is not None
    ws_id = await users.create_local_workspace(
        team_id=admin_team,
        name="Prepare failure",
        creator_user_id=app_ctx.state.owner_id,
    )
    store: ApprovalStore = app_ctx.state.approval_store
    approval = await store.enqueue(
        turn_id="t_prepare_failure",
        session_id="s",
        workspace_id=ws_id,
        member_uid="m",
        user_id=app_ctx.state.owner_id,
        tool_name="approval_prepare_probe",
        arguments={},
        reason="probe",
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(
            f"/api/v1/approvals/{approval.id}/approve",
            json={},
            headers=_as_admin(app_ctx),
        )

    assert r.status_code == 503
    assert r.json()["detail"] == "approval execution context unavailable"
    got = await store.get(approval.id)
    assert got is not None
    assert got.status == "pending"


@pytest.mark.integration
async def test_duplicate_approve_does_not_spawn_executor_twice(app_ctx) -> None:
    calls: list[object] = []
    scoped_datarc = object()
    vector_client = object()

    async def _handler(_args, ctx):
        calls.append(ctx)
        await asyncio.sleep(0.05)
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="approval_duplicate_probe",
                description="approval duplicate probe",
                input_schema={"type": "object", "properties": {}},
            ),
            handler=_handler,
        ),
    )
    app_ctx.state.orchestrator = SimpleNamespace(
        tool_registry=registry,
        event_registry=None,
    )
    app_ctx.state.project_memory_store = _ProjectMemory()
    app_ctx.state.datarc_connection_store = _ConnectionStore()
    app_ctx.state.datarc_client_factory = _DatarcFactory(scoped_datarc)
    app_ctx.state.embed_client_factory = _EmbedFactory(vector_client)

    users: UserStore = app_ctx.state.user_store
    admin_team = await users.get_user_team_id(app_ctx.state.admin_id)
    assert admin_team is not None
    ws_id = await users.create_local_workspace(
        team_id=admin_team,
        name="Duplicate approval",
        creator_user_id=app_ctx.state.owner_id,
    )
    sessions = SessionStore(users.db_path)
    await sessions.initialize()
    await sessions.ensure_session(session_id="s", workspace_id=ws_id, user_id=app_ctx.state.owner_id)
    await sessions.record_turn(turn_id="t_duplicate", session_id="s", user_text="ordinary approved operation")
    store: ApprovalStore = app_ctx.state.approval_store
    approval = await store.enqueue(
        turn_id="t_duplicate",
        session_id="s",
        workspace_id=ws_id,
        member_uid="m",
        user_id=app_ctx.state.owner_id,
        tool_name="approval_duplicate_probe",
        arguments={},
        reason="probe",
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        first, second = await asyncio.gather(
            c.post(f"/api/v1/approvals/{approval.id}/approve", json={}, headers=_as_admin(app_ctx)),
            c.post(f"/api/v1/approvals/{approval.id}/approve", json={}, headers=_as_admin(app_ctx)),
        )

    assert first.status_code == 200
    assert second.status_code == 200
    for _ in range(20):
        await asyncio.sleep(0.05)
        if calls:
            break
    await asyncio.sleep(0.1)
    assert len(calls) == 1
