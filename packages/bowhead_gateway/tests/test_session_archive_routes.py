"""Gateway contracts for the Option A session archive lifecycle."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import httpx2
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.migrations.baseline_schema import initial_schema
from bowhead_core.session_lifecycle import SessionLifecycleCoordinator
from bowhead_core.terminal_protocol_store import TerminalProtocolStore
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings

from bowhead_core import SessionStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def app_ctx(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions.db")
    await store.initialize()
    app = create_app()
    app.state.settings = Settings()
    app.state.session_store = store
    app.state.legacy_session_lifecycle_allowed = True
    return app


@pytest.fixture
async def migrated_app_ctx(tmp_path: Path):
    db_path = tmp_path / "bowhead.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    store = SessionStore(db_path)
    await store.initialize()
    terminal = TerminalProtocolStore(str(db_path))
    await terminal.initialize()
    app = create_app()
    app.state.settings = Settings()
    app.state.session_store = store
    app.state.session_lifecycle_coordinator = SessionLifecycleCoordinator(store, terminal)
    app.state.legacy_session_lifecycle_allowed = False
    return app


def _error_code(response: httpx2.Response) -> str:
    return str(response.json()["detail"]["error"]["code"])


@pytest.mark.integration
async def test_archive_list_restore_and_count_contract(
    app_ctx,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store: SessionStore = app_ctx.state.session_store
    uid = app_ctx.state.settings.dev_default_user
    archived = await store.ensure_session(
        session_id="s_archived",
        workspace_id="w1",
        user_id=uid,
    )
    other = await store.ensure_session(
        session_id="s_other",
        workspace_id="w1",
        user_id=uid,
    )
    await store.set_title(session_id=archived.id, title="Quarterly revenue")
    await store.set_title(session_id=other.id, title="Other work")
    await store.pin_session(user_id=uid, session_id=archived.id)

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post(f"/api/v1/sessions/{archived.id}/archive")
        assert response.status_code == 204
        assert (await client.post(f"/api/v1/sessions/{archived.id}/archive")).status_code == 204

        active = (await client.get("/api/v1/sessions")).json()
        active_ids = {item["id"] for group in active["groups"] for item in group["items"]}
        assert active_ids == {other.id}
        assert active["archived_count"] == 1

        archived_view = (
            await client.get(
                "/api/v1/sessions",
                params={"view": "archived", "q": "Quarterly", "limit": 1},
            )
        ).json()
        assert archived_view["archived_count"] == 1
        assert [group["id"] for group in archived_view["groups"]] == ["archived"]
        assert [item["id"] for item in archived_view["groups"][0]["items"]] == [
            archived.id,
        ]
        item = archived_view["groups"][0]["items"][0]
        assert item["archived"] is True
        assert item["pinned"] is True

        response = await client.delete(f"/api/v1/sessions/{archived.id}/archive")
        assert response.status_code == 204
        assert (await client.delete(f"/api/v1/sessions/{archived.id}/archive")).status_code == 204
        restored = (await client.get("/api/v1/sessions")).json()
        restored_items = [item for group in restored["groups"] for item in group["items"] if item["id"] == archived.id]
        assert restored_items[0]["pinned"] is True
        assert restored_items[0]["archived"] is False
        assert restored["archived_count"] == 0


@pytest.mark.integration
async def test_archive_rejects_running_and_conceals_inaccessible_sessions(
    app_ctx,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store: SessionStore = app_ctx.state.session_store
    uid = app_ctx.state.settings.dev_default_user
    running = await store.ensure_session(
        session_id="s_running",
        workspace_id="w1",
        user_id=uid,
    )
    foreign = await store.ensure_session(
        session_id="s_foreign",
        workspace_id="w1",
        user_id="u_foreign",
    )
    deleted = await store.ensure_session(
        session_id="s_deleted",
        workspace_id="w1",
        user_id=uid,
    )
    await store.record_turn(
        turn_id="t_running",
        session_id=running.id,
        user_text="still running",
    )
    await store.soft_delete_session(deleted.id)

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post(f"/api/v1/sessions/{running.id}/archive")
        assert response.status_code == 409
        assert _error_code(response) == "session_archive_active_turn"

        for session_id in (foreign.id, deleted.id, "s_missing"):
            response = await client.post(f"/api/v1/sessions/{session_id}/archive")
            assert response.status_code == 404
            assert _error_code(response) == "session_not_found"
            response = await client.delete(f"/api/v1/sessions/{session_id}/archive")
            assert response.status_code == 404
            assert _error_code(response) == "session_not_found"


# ── feedback-triage-2026-08-24: session soft-deletion route contract ──────────
#
# Customer wants deletable conversations. Store already supported soft deletion (originally to soft-delete source sessions when deleting reports),
# but user entry was missing. Pin three route-layer behaviors:
# · Deleted sessions leave lists, but details remain auditable;
# · Undo restores them;
# · Active turns return 409 with dedicated session_delete_active_turn code
# (frontend needs it for the specific '这项工作仍在运行' message, distinct from generic failures).


@pytest.mark.integration
async def test_delete_hides_session_and_undelete_brings_it_back(app_ctx) -> None:
    store: SessionStore = app_ctx.state.session_store
    uid = app_ctx.state.settings.dev_default_user
    await store.ensure_session(session_id="s_del", workspace_id="w1", user_id=uid)
    await store.ensure_session(session_id="s_keep", workspace_id="w1", user_id=uid)

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        assert (await client.delete("/api/v1/sessions/s_del")).status_code == 204

        listed = (await client.get("/api/v1/sessions")).json()
        ids = [item["id"] for group in listed["groups"] for item in group["items"]]
        assert "s_del" not in ids
        assert "s_keep" in ids

        # Soft deletion is not hard deletion: detail remains available.
        assert (await client.get("/api/v1/sessions/s_del")).status_code == 200

        assert (await client.post("/api/v1/sessions/s_del/undelete")).status_code == 204
        listed = (await client.get("/api/v1/sessions")).json()
        ids = [item["id"] for group in listed["groups"] for item in group["items"]]
        assert "s_del" in ids


@pytest.mark.integration
async def test_migrated_database_delete_and_undo_use_lifecycle_coordinator(migrated_app_ctx) -> None:
    store = migrated_app_ctx.state.session_store
    uid = migrated_app_ctx.state.settings.dev_default_user
    await store.ensure_session(session_id="s_migrated", workspace_id="w1", user_id=uid)
    transport = httpx2.ASGITransport(app=migrated_app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        deleted = await client.delete("/api/v1/sessions/s_migrated")
        assert deleted.status_code == 204
        assert (await store.get_session("s_migrated")).deleted_at is not None
        assert await store.list_sessions(user_id=uid) == []
        restored = await client.post("/api/v1/sessions/s_migrated/undelete")
        assert restored.status_code == 204
        assert (await store.get_session("s_migrated")).deleted_at is None
        assert [session.id for session in await store.list_sessions(user_id=uid)] == ["s_migrated"]


@pytest.mark.integration
async def test_migrated_database_still_rejects_deleting_running_work(migrated_app_ctx) -> None:
    store = migrated_app_ctx.state.session_store
    uid = migrated_app_ctx.state.settings.dev_default_user
    await store.ensure_session(session_id="s_migrated_busy", workspace_id="w1", user_id=uid)
    await store.record_turn(turn_id="t_migrated_busy", session_id="s_migrated_busy", user_text="Run")
    transport = httpx2.ASGITransport(app=migrated_app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        blocked = await client.delete("/api/v1/sessions/s_migrated_busy")
    assert blocked.status_code == 409
    assert _error_code(blocked) == "session_delete_active_turn"
    assert (await store.get_session("s_migrated_busy")).deleted_at is None


@pytest.mark.integration
async def test_migrated_database_without_coordinator_reports_readiness_failure(migrated_app_ctx) -> None:
    store = migrated_app_ctx.state.session_store
    uid = migrated_app_ctx.state.settings.dev_default_user
    await store.ensure_session(session_id="s_unready", workspace_id="w1", user_id=uid)
    migrated_app_ctx.state.session_lifecycle_coordinator = None
    transport = httpx2.ASGITransport(app=migrated_app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        blocked = await client.delete("/api/v1/sessions/s_unready")
    assert blocked.status_code == 503
    assert _error_code(blocked) == "terminal_protocol_writer_not_ready"
    assert (await store.get_session("s_unready")).deleted_at is None


@pytest.mark.integration
async def test_delete_is_idempotent_and_conceals_unknown_sessions(app_ctx) -> None:
    store: SessionStore = app_ctx.state.session_store
    uid = app_ctx.state.settings.dev_default_user
    await store.ensure_session(session_id="s_once", workspace_id="w1", user_id=uid)

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        assert (await client.delete("/api/v1/sessions/s_once")).status_code == 204
        repeat = await client.delete("/api/v1/sessions/s_once")
        assert repeat.status_code == 404
        assert _error_code(repeat) == "session_not_found"

        missing = await client.delete("/api/v1/sessions/s_never_existed")
        assert missing.status_code == 404
        assert _error_code(missing) == "session_not_found"


@pytest.mark.integration
async def test_delete_refuses_a_running_session_with_its_own_code(app_ctx) -> None:
    """★ 码必须是 session_delete_active_turn ——前端靠它给专门话术。"""
    store: SessionStore = app_ctx.state.session_store
    uid = app_ctx.state.settings.dev_default_user
    await store.ensure_session(session_id="s_busy", workspace_id="w1", user_id=uid)
    await store.record_turn(turn_id="t_busy", session_id="s_busy", user_text="run")

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        blocked = await client.delete("/api/v1/sessions/s_busy")
        assert blocked.status_code == 409
        assert _error_code(blocked) == "session_delete_active_turn"

        listed = (await client.get("/api/v1/sessions")).json()
        ids = [item["id"] for group in listed["groups"] for item in group["items"]]
        assert "s_busy" in ids
