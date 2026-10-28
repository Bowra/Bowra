"""feedback-0713 R1 — 删报告可选联动软删来源会话.

- delete_session=1 → canvas 删 + 会话软删（list_sessions 不再出现, get_session
  保留可审计 → 定时任务 run 历史深链仍可解析）
- 缺省 → 仅删 canvas, 会话保留（现状语义）
- 会话 owner 与 canvas owner 不一致 → 跳过软删（防错配数据下的越权抹除）
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx2
import pytest
from bowhead_core.session_lifecycle import SessionLifecycleError
from bowhead_core.session_store import SessionStore
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings

from bowhead_core import CanvasStore


@pytest.fixture
async def app_ctx(tmp_path):
    db = tmp_path / "test.db"
    canvas = CanvasStore(db)
    await canvas.initialize()
    sessions = SessionStore(db)
    await sessions.initialize()

    app = create_app()
    app.state.settings = Settings()
    app.state.canvas_store = canvas
    app.state.session_store = sessions
    app.state.legacy_session_lifecycle_allowed = True
    app.state.orchestrator = None
    app.state.datarc = None
    return app


async def _seed(app_ctx, *, session_id: str, canvas_user: str, session_user: str | None = None):
    canvas_store: CanvasStore = app_ctx.state.canvas_store
    session_store: SessionStore = app_ctx.state.session_store
    ses = await session_store.ensure_session(
        session_id=session_id,
        workspace_id="w",
        user_id=session_user or canvas_user,
    )
    cv = await canvas_store.ensure_canvas(session_id=ses.id, workspace_id="w", user_id=canvas_user)
    return ses, cv


@pytest.mark.integration
async def test_delete_with_session_soft_deletes_source_session(app_ctx) -> None:
    uid = app_ctx.state.settings.dev_default_user
    ses, cv = await _seed(app_ctx, session_id="s_r1_link", canvas_user=uid)
    session_store: SessionStore = app_ctx.state.session_store

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.delete(f"/api/v1/canvas/{cv.id}?delete_session=1")
        assert r.status_code == 200

    # Absent from session lists (acceptance ①).
    listed = await session_store.list_sessions(user_id=uid)
    assert all(s.id != ses.id for s in listed)
    # Soft-delete, not hard-delete: get_session still resolves (audit / run-history deep links).
    still = await session_store.get_session(ses.id)
    assert still is not None
    # Canvas actually deleted.
    assert await app_ctx.state.canvas_store.get_canvas(cv.id) is None


@pytest.mark.integration
async def test_delete_without_flag_keeps_session(app_ctx) -> None:
    uid = app_ctx.state.settings.dev_default_user
    ses, cv = await _seed(app_ctx, session_id="s_r1_keep", canvas_user=uid)
    session_store: SessionStore = app_ctx.state.session_store

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.delete(f"/api/v1/canvas/{cv.id}")
        assert r.status_code == 200

    listed = await session_store.list_sessions(user_id=uid)
    assert any(s.id == ses.id for s in listed)  # Current semantics: conversation retained (acceptance ②).


@pytest.mark.integration
async def test_owner_mismatch_skips_session_soft_delete(app_ctx) -> None:
    """错配数据防御：session.user_id ≠ canvas.user_id → 只删 canvas 不动会话."""
    uid = app_ctx.state.settings.dev_default_user
    ses, cv = await _seed(app_ctx, session_id="s_r1_mismatch", canvas_user=uid, session_user="someone_else")
    session_store: SessionStore = app_ctx.state.session_store

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.delete(f"/api/v1/canvas/{cv.id}?delete_session=1")
        assert r.status_code == 200

    listed = await session_store.list_sessions(user_id="someone_else")
    assert any(s.id == ses.id for s in listed)  # Other user's session remains.


@pytest.mark.integration
async def test_missing_coordinator_without_explicit_legacy_mode_fails_closed(
    app_ctx,
) -> None:
    uid = app_ctx.state.settings.dev_default_user
    session, canvas = await _seed(
        app_ctx,
        session_id="s_no_coordinator",
        canvas_user=uid,
    )
    app_ctx.state.legacy_session_lifecycle_allowed = False

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.delete(f"/api/v1/canvas/{canvas.id}?delete_session=1")

    assert response.status_code == 503
    assert response.json()["detail"] == {"error": {"code": "terminal_protocol_writer_not_ready"}}
    assert await app_ctx.state.canvas_store.get_canvas(canvas.id) is not None
    assert (await app_ctx.state.session_store.get_session(session.id)).deleted_at is None


@pytest.mark.integration
async def test_explicit_legacy_mode_without_session_store_fails_atomically(
    app_ctx,
) -> None:
    uid = app_ctx.state.settings.dev_default_user
    _, canvas = await _seed(
        app_ctx,
        session_id="s_missing_store",
        canvas_user=uid,
    )
    app_ctx.state.session_store = None

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.delete(f"/api/v1/canvas/{canvas.id}?delete_session=1")

    assert response.status_code == 503
    assert response.json()["detail"] == {"error": {"code": "terminal_protocol_writer_not_ready"}}
    assert await app_ctx.state.canvas_store.get_canvas(canvas.id) is not None


async def test_soft_delete_session_idempotent_and_filters_list(tmp_path) -> None:
    """store 层语义锁：软删幂等 + 只影响列表面."""
    store = SessionStore(tmp_path / "s.db")
    await store.initialize()
    ses = await store.ensure_session(session_id="s_x", workspace_id="w", user_id="u1")
    assert await store.soft_delete_session(ses.id) is True
    assert await store.soft_delete_session(ses.id) is False  # Idempotent.
    assert await store.list_sessions(user_id="u1") == []
    assert (await store.get_session(ses.id)) is not None


async def test_ensure_session_on_soft_deleted_neither_revives_nor_breaks(tmp_path) -> None:
    """设计不变量锁（review P2）：软删只收敛列表面——ensure_session/get_session
    必须**穿透**软删（resume/IM 线程/定时任务深链/审计全靠它），且不复活
    （list_sessions 仍不含）。将来若有人给 get/ensure 加 deleted 过滤，这里红。
    """
    store = SessionStore(tmp_path / "s.db")
    await store.initialize()
    ses = await store.ensure_session(session_id="s_ghost", workspace_id="w", user_id="u1")
    assert await store.soft_delete_session(ses.id) is True
    again = await store.ensure_session(session_id=ses.id, workspace_id="w", user_id="u1")
    assert again.id == ses.id  # Return existing session unchanged, no failure/new session.
    assert await store.list_sessions(user_id="u1") == []  # No revival.


@pytest.mark.integration
async def test_committed_delete_retry_repairs_grants_and_access_requests(
    app_ctx,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = app_ctx.state.settings.dev_default_user
    calls: list[tuple[str, str, str]] = []

    class Lifecycle:
        async def canvas_delete_replay(self, **_kwargs):
            return SimpleNamespace(
                operation_id="slo_1",
                team_id="team_1",
                session_id="s_deleted",
                actor_audit_ref=uid,
                result_code="deleted",
            )

    class Access:
        async def revoke_grants_for_resource(
            self,
            team_id: str,
            kind: str,
            resource_id: str,
        ) -> None:
            calls.append((team_id, kind, resource_id))

    async def cascade(_app, *, team_id, kind, resource_id, **_kwargs):
        calls.append((team_id, kind, resource_id))

    from bowhead_gateway.routes import access_requests

    monkeypatch.setattr(access_requests, "cascade_requests_for_resource", cascade)
    app_ctx.state.session_lifecycle_coordinator = Lifecycle()
    app_ctx.state.access_control_store = Access()

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.delete("/api/v1/canvas/cv_deleted?delete_session=1")

    assert response.status_code == 200
    assert calls == [
        ("team_1", "report", "s_deleted"),
        ("team_1", "report", "s_deleted"),
    ]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("internal_code", "status_code", "public_code"),
    [
        ("session_lifecycle_conflict", 409, "session_transitioning"),
        ("terminal_protocol_future_version", 409, "terminal_protocol_future_version"),
        ("terminal_protocol_malformed", 500, "terminal_protocol_malformed"),
    ],
)
async def test_canvas_lifecycle_failure_uses_finite_gateway_projection(
    app_ctx,
    internal_code: str,
    status_code: int,
    public_code: str,
) -> None:
    uid = app_ctx.state.settings.dev_default_user
    session, canvas = await _seed(
        app_ctx,
        session_id=f"s_{public_code}",
        canvas_user=uid,
    )

    class Lifecycle:
        async def canvas_delete_replay(self, **_kwargs):
            return None

        async def soft_delete(self, **_kwargs):
            raise SessionLifecycleError(internal_code)

    app_ctx.state.session_lifecycle_coordinator = Lifecycle()
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
    ) as client:
        response = await client.delete(f"/api/v1/canvas/{canvas.id}?delete_session=1")

    assert response.status_code == status_code
    assert response.json()["detail"] == {"error": {"code": public_code}}
    assert await app_ctx.state.canvas_store.get_canvas(canvas.id) is not None
    assert (await app_ctx.state.session_store.get_session(session.id)).deleted_at is None
