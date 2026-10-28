"""ws-datarc-decouple —— Bowhead 空间 id 绝不被北极九章工作区 id 顶替.

团队连接收口（9e186d5d）后本用例的播种形状变了：北极九章的 client 来自**团队保存的
连接**、身份来自**认证用户的已验证邮箱**、当前工作区来自 **per-user 阶梯**。部署级
``app.state.datarc_identity`` 与 ws 键的 ``project_memory_store`` 默认值都已退役。
被守的不变量本身没变：``TurnRequest.workspace_id`` 恒为 Bowhead 的空间 id，北极九章
工作区只进 ``active_datarc_project``。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import httpx2
import jwt
import pytest
from bowhead_cli.migrate import TARGET_FILES, upgrade
from bowhead_core._db import open_shared_ctx
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.chat_turn_source import current_attachment_positions
from bowhead_core.events import EventBusRegistry, OrchestratorEvent
from bowhead_core.session_store import SessionStore
from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore
from bowhead_gateway.main import create_app
from bowhead_gateway.routes import chat as chat_routes
from bowhead_gateway.settings import Settings
from cryptography.fernet import Fernet
from fakes_tool_context import FakeUserMemoryToolContext

from bowhead_bi_bridge import BIConnectionError, BIPermissionError

_JWT_SECRET = "test_secret_chat_datarc_decouple"
_TEAM_ID = "team_decouple"
_DATARC_CLIENT = object()


def _jwt(user_id: str = "u_alice", team_id: str = _TEAM_ID) -> str:
    payload = {
        "sub": user_id,
        "team_id": team_id,
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    return jwt.encode(payload, _JWT_SECRET, algorithm="HS256")


class FakeOrchestrator:
    def __init__(
        self,
        session_store: SessionStore,
        event_registry: EventBusRegistry,
    ) -> None:
        self.session_store = session_store
        self.event_registry = event_registry
        self.dispatch_owner_id = "test_datarc_decouple"
        self._cancel_events: dict[str, asyncio.Event] = {}


class FakeConnectionStore:
    """团队保存的北极九章连接——client 的唯一来源。"""

    def __init__(self) -> None:
        self.teams: list[str] = []

    async def list_connections(self, team_id: str) -> list[Any]:
        self.teams.append(team_id)
        return [SimpleNamespace(id="conn_decouple", base_url="https://datarc.example")]

    async def get_connection_auth(self, conn_id: str, team_id: str) -> str:
        return "auth_decouple"


class FakeDatarcFactory:
    def get_client(self, base_url: str, auth_key: str) -> object:
        return _DATARC_CLIENT


class FakeToolContextService:
    """可及集合——唯一可及工作区会被阶梯自动选中（DefaultContextLadder 档3）。"""

    async def accessible_workspaces(self, datarc_client: object, user_id: str) -> tuple[Any, ...]:
        from bowhead_gateway.connector_context_service import DatarcWorkspaceRef

        assert datarc_client is _DATARC_CLIENT  # Never a deployment singleton.
        return (DatarcWorkspaceRef("datarc_ws_456", "零售集团"),)

    async def accessible_workspaces_ex(
        self,
        datarc_client: object,
        user_id: str,
    ) -> tuple[tuple[Any, ...], str]:
        return await self.accessible_workspaces(datarc_client, user_id), ""


class FakeMemberResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    async def resolve(self, identity: str, workspace_id: str, *, datarc: Any = None) -> str:
        self.calls.append((identity, workspace_id, datarc))
        return "member_456"


@pytest.mark.asyncio
async def test_chat_send_keeps_bowhead_workspace_and_uses_datarc_project(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    await _exercise_chat_send(monkeypatch, tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("upload_owner", ["u_alice", "u_other"])
async def test_ordinary_chat_preserves_authorized_template_attachment_source(monkeypatch, tmp_path, upload_owner) -> None:
    await _exercise_chat_send(monkeypatch, tmp_path, upload_owner=upload_owner)


async def _exercise_chat_send(monkeypatch, tmp_path, *, upload_owner=None, member_error=None) -> None:
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", _JWT_SECRET)
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    core_path = tmp_path / TARGET_FILES["core"]
    assert upgrade(f"sqlite+aiosqlite:///{core_path}", target="core") == ("core",)
    session_store = SessionStore(core_path)
    await session_store.initialize()
    turn_log_store = TurnLogStore(str(core_path))
    await turn_log_store.initialize()
    registry = EventBusRegistry(
        default_log_sink=OrderedTurnLogSink(turn_log_store),
    )
    app = create_app()
    if app.state.edition == "community":
        from bowhead_core.entitlements import CommunityResolver
        from bowhead_core.installation import community_setup_token, initialize_installation, setup_community_owner
        from bowhead_core.user_store import UserStore

        identity = await initialize_installation(str(core_path), "community", admitted_fresh=True)
        await setup_community_owner(
            str(core_path),
            setup_token=community_setup_token(identity),
            team_name="Synthetic Chat Setup",
            email="setup-owner@example.test",
            password="SyntheticSetupOwner7!",
        )
        app.state.user_store = UserStore(str(core_path))
        async with open_shared_ctx(core_path) as db:
            await db.execute(
                "INSERT INTO teams(id,name,status,created_at) VALUES (?,?,?,?)",
                (_TEAM_ID, "Synthetic Datarc Team", "active", "2026-10-26"),
            )
            await db.commit()
        app.state.entitlements = CommunityResolver(mode="local", user_store=app.state.user_store)
    resolver = FakeMemberResolver()
    if member_error is not None:

        async def fail_member(identity, workspace_id, *, datarc=None):
            resolver.calls.append((identity, workspace_id, datarc))
            raise member_error

        resolver.resolve = fail_member
    conn_store = FakeConnectionStore()
    captured: list[Any] = []
    worker_finished = asyncio.Event()
    app.state.settings = Settings()
    app.state.orchestrator = FakeOrchestrator(session_store, registry)
    app.state.session_store = session_store
    app.state.turn_log_store = turn_log_store
    app.state.datarc_connection_store = conn_store
    app.state.datarc_client_factory = FakeDatarcFactory()
    app.state.tool_context_service = FakeToolContextService()
    app.state.user_memory_store = FakeUserMemoryToolContext()
    app.state.member_resolver = resolver
    body = {"workspace_id": "ws_123", "message": "Write an email" if member_error else "this year GMV"}
    refs = []
    if upload_owner is not None:
        artifacts = ArtifactStore(db_path=core_path, backend=LocalBackend(tmp_path / "blobs"))
        app.state.artifact_store = artifacts
        for filename in ("data.csv", "template.pptx"):
            artifact = await artifacts.put_bytes(
                content=b"Synthetic attachment metadata fixture",
                filename=filename,
                mime="application/octet-stream",
                created_by=upload_owner,
            )
            refs.append({"fileId": artifact.id, "filename": artifact.filename, "mime": artifact.mime, "size": artifact.size})
            async with open_shared_ctx(core_path) as db:
                await db.execute(
                    "INSERT INTO artifact_upload_bindings(artifact_id,workspace_id,user_id,created_at) VALUES (?,?,?,?)",
                    (artifact.id, "ws_123", upload_owner, "2026-10-02"),
                )
                await db.commit()
        body.update(
            message="The first file is data. Use the second file as the template and return PPTX and PDF.",
            attachments=[ref["fileId"] for ref in refs],
            attachment_refs=refs,
        )

    async def _verified_email(_request: Any) -> str:
        return "alice@example.com"

    # Identity = authenticated user's **verified** email (deployment datarc_identity removed). Replace authentication here;
    # this test targets workspace_id / active_datarc_project separation.
    monkeypatch.setattr(chat_routes, "_caller_verified_email", _verified_email)

    # Authenticate the synthetic caller independently of the Datarc namespace-routing fixture.
    from bowhead_gateway.routes import auth as auth_routes

    async def _coach_user(_request: Any) -> Any:
        return SimpleNamespace(id="u_alice", team_id=_TEAM_ID)

    monkeypatch.setattr(auth_routes, "_current_user", _coach_user)

    from bowhead_gateway import turn_service

    async def _team_runtime(*_args: Any, **_kwargs: Any) -> Any:
        return turn_service.TeamModelRuntimeResolution(
            router=object(),
            result="team_safe_success",
            reason="resolved",
        )

    monkeypatch.setattr(turn_service, "resolve_team_model_runtime", _team_runtime)

    async def capture_run_turn(
        _orchestrator: FakeOrchestrator,
        req: Any,
        user_store: Any = None,
        team_id: str | None = None,
        metrics_log: Any = None,
        entitlements: Any = None,
        admitted_legacy_bus: Any = None,
        cleanup_grace_s: float = 5.0,
    ) -> None:
        assert admitted_legacy_bus is not None
        assert cleanup_grace_s == 5.0
        # Read through a new store before any work: admission must commit the
        # original request and authorized attachment order, not backfill later.
        source = await SessionStore(core_path).get_chat_turn_user_source(turn_id=req.turn_id, user_id="u_alice", workspace_id="ws_123")
        assert source is not None and source.user_text == req.text
        if refs:
            assert source.attachment_ids == tuple(ref["fileId"] for ref in refs)
            assert [ref["fileId"] for ref in source.attachment_refs] == [ref["fileId"] for ref in refs]
            assert current_attachment_positions(source) == {ref["fileId"]: index for index, ref in enumerate(refs, 1)}
        assert await admitted_legacy_bus.emit(
            OrchestratorEvent(
                event_type="final_answer",
                turn_id=req.turn_id,
                payload={"text": "ok"},
            ),
        )
        assert await _orchestrator.session_store.mark_completed(
            turn_id=req.turn_id,
            intent="lookup",
            final_answer="ok",
            tokens_input=0,
            tokens_output=0,
            cost_usd=0.0,
        )
        assert await admitted_legacy_bus.emit(
            OrchestratorEvent(
                event_type="done",
                turn_id=req.turn_id,
                payload={"status": "completed"},
            ),
        )
        captured.append(req)
        worker_finished.set()

    monkeypatch.setattr(chat_routes, "_run_turn_and_cleanup", capture_run_turn)

    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        resp = await client.post(
            "/api/v1/chat/send",
            json=body,
            headers={"Authorization": f"Bearer {_jwt()}"},
        )

    if upload_owner == "u_other":
        assert resp.status_code == 409 and "attachment_unavailable" in resp.text
        assert not captured
        async with open_shared_ctx(core_path) as db, db.execute("SELECT COUNT(*) FROM turns") as cursor:
            assert (await cursor.fetchone())[0] == 0
        return
    assert resp.status_code == 200, resp.text
    await asyncio.wait_for(worker_finished.wait(), timeout=1)

    # Resolve client only through caller's team connection.
    assert conn_store.teams and set(conn_store.teams) == {_TEAM_ID}
    # Identity = authenticated verified email; member resolution must carry the team client.
    assert resolver.calls == [("alice@example.com", "datarc_ws_456", _DATARC_CLIENT)]
    assert captured
    turn_req = captured[0]
    source = await session_store.get_chat_turn_user_source(turn_id=turn_req.turn_id, user_id="u_alice", workspace_id="ws_123")
    assert source is not None and source.user_text == turn_req.text
    # ★ Actual invariant: pass Bowhead space id unchanged, never substitute the Datarc workspace.
    assert turn_req.workspace_id == "ws_123"
    if member_error is None:
        assert turn_req.active_datarc_project == "datarc_ws_456"
        assert turn_req.member_uid == "member_456"
        assert turn_req.datarc_client is _DATARC_CLIENT
    else:
        assert turn_req.active_datarc_project is None
        assert turn_req.member_uid == ""
        assert turn_req.datarc_client is None
        assert turn_req.datarc_unavailable is True
        assert turn_req.accessible_datarc_workspaces == ()
        assert turn_req.vector_client is None
        assert turn_req.datarc_absence_reason == ("connection_rejected" if isinstance(member_error, BIPermissionError) else "transient")
        from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

        ctx = ToolExecutionContext(
            workspace_id=turn_req.workspace_id,
            member_uid=turn_req.member_uid,
            user_id=turn_req.user_id,
            turn_id=turn_req.turn_id,
            datarc=turn_req.datarc_client,
            active_datarc_project=turn_req.active_datarc_project,
            datarc_absence_reason=turn_req.datarc_absence_reason,
        )
        reg = build_default_registry()
        inv = await reg.execute("bi_schema", {}, ctx)
        assert inv.error and "DatarcUnavailableError" in inv.error
        if isinstance(member_error, BIPermissionError):
            assert "expired or was revoked" in inv.error
        from bowhead_core.tool_registry import ToolSpec
        from bowhead_router.types import ToolSchema

        async def unrelated_tool(args, ctx):
            return {"ok": True}

        reg.register(
            ToolSpec(schema=ToolSchema(name="unrelated_task", description="Non-Datarc work", input_schema={"type": "object"}), handler=unrelated_tool)
        )
        inv = await reg.execute("unrelated_task", {}, ctx)
        assert inv.error is None and inv.result == {"ok": True}

    async def _wait_dispatch_settled() -> None:
        while (
            await session_store.get_chat_spawn_dispatch_projection_state(
                turn_id=turn_req.turn_id,
            )
            != "settled"
        ):
            await asyncio.sleep(0)

    await asyncio.wait_for(_wait_dispatch_settled(), timeout=1)
    await registry.close(turn_req.turn_id)


@pytest.mark.parametrize(
    "member_error", [BIPermissionError("access rejected", source="http"), BIConnectionError("timeout")], ids=["expired-key", "network-failure"]
)
async def test_chat_starts_without_datarc_when_member_lookup_fails(monkeypatch, tmp_path, member_error):
    await _exercise_chat_send(monkeypatch, tmp_path, member_error=member_error)


@pytest.mark.parametrize("code", ["datarc_identity_required", "datarc_unavailable", "ws_access_denied"])
async def test_optional_datarc_lookup_keeps_other_access_checks(monkeypatch, code):
    import logging

    from bowhead_gateway.datarc_context import DatarcTurnContext
    from fastapi import HTTPException

    async def denied(*args, **kwargs):
        raise HTTPException(403, detail={"error": {"code": code}})

    monkeypatch.setattr(chat_routes, "_resolve_datarc_member_uid", denied)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    context = DatarcTurnContext(active_datarc_project="1", datarc_project_for_calls="1", datarc_client=object())
    if code == "ws_access_denied":
        with pytest.raises(HTTPException):
            await chat_routes._resolve_optional_datarc_member(request, context, ws_enforce=True, log=logging.getLogger(__name__))
    else:
        degraded, member = await chat_routes._resolve_optional_datarc_member(request, context, ws_enforce=True, log=logging.getLogger(__name__))
        assert member == "" and degraded.datarc_client is None
        assert degraded.active_datarc_project is None and degraded.datarc_unavailable
