"""workspace_settings authz regression tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiosqlite
import httpx2
import pytest
from bowhead_core.user_store import UserStore
from bowhead_core.workspace_settings_store import WorkspaceSettingsStore
from bowhead_gateway.routes.workspace_settings import router
from fastapi import FastAPI

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")
    app = FastAPI()
    app.include_router(router)
    db = tmp_path / "app.db"
    store = WorkspaceSettingsStore(db)
    await store.init()
    users = UserStore(db)
    await users.initialize()
    team = await users.create_team(name="Acme", max_seats=10)
    admin = await users.create_user(
        team_id=team.id,
        email="admin@example.com",
        password="Passw0rd!",
    )
    other_admin = await users.create_user(
        team_id=team.id,
        email="other-admin@example.com",
        password="Passw0rd!",
    )
    random = await users.create_user(
        team_id=team.id,
        email="random@example.com",
        password="Passw0rd!",
    )
    team_admin = await users.create_user(
        team_id=team.id,
        email="team-admin@example.com",
        password="Passw0rd!",
    )
    async with aiosqlite.connect(users.db_path) as db:
        await db.execute(
            "UPDATE team_members SET role = 'team_admin' WHERE team_id = ? AND user_id = ?",
            (team.id, team_admin.id),
        )
        await db.commit()
    ws_id = await users.create_local_workspace(
        team_id=team.id,
        name="Pharma",
        creator_user_id=admin.id,
    )
    await users.add_member(workspace_id=ws_id, user_id=other_admin.id, role="admin")

    class _Settings:
        dev_default_user = "u_admin"
        dev_workspace_admins = {"ws_pharma": ["u_admin", "u_other_admin"]}
        auth_mode = "dev"  # dev-mode test: X-Bowhead-User-Id header honored (non-dev would 401)

    app.state.settings = _Settings()
    app.state.workspace_settings_store = store
    app.state.user_store = users
    app.state.ws_id = ws_id
    app.state.admin_id = admin.id
    app.state.other_admin_id = other_admin.id
    app.state.random_id = random.id
    app.state.team_admin_id = team_admin.id
    return app


class TestPermissionCheck:
    @pytest.mark.asyncio
    async def test_admin_can_set(self, app: FastAPI) -> None:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
            r = await c.put(
                f"/api/v1/workspace-settings/{app.state.ws_id}/week_start_day",
                json={"value": "sunday"},
                headers={"x-bowhead-user-id": app.state.admin_id},
            )
            assert r.status_code == 200
            assert r.json()["value"] == "sunday"

    @pytest.mark.asyncio
    async def test_other_admin_can_set(self, app: FastAPI) -> None:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
            r = await c.put(
                f"/api/v1/workspace-settings/{app.state.ws_id}/week_start_day",
                json={"value": "sunday"},
                headers={"x-bowhead-user-id": app.state.other_admin_id},
            )
            assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_non_admin_rejected(self, app: FastAPI) -> None:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
            r = await c.put(
                f"/api/v1/workspace-settings/{app.state.ws_id}/week_start_day",
                json={"value": "sunday"},
                headers={"x-bowhead-user-id": app.state.random_id},
            )
            assert r.status_code == 403
            body = r.json()
            assert body["detail"]["error"]["code"] == "E_PERMISSION_DENIED"

    @pytest.mark.asyncio
    async def test_unknown_workspace_rejected(
        self,
        app: FastAPI,
    ) -> None:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
            r = await c.put(
                "/api/v1/workspace-settings/ws_undefined/week_start_day",
                json={"value": "monday"},
                headers={"x-bowhead-user-id": app.state.admin_id},
            )
            assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_non_member_unknown_workspace_rejected(
        self,
        app: FastAPI,
    ) -> None:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
            r = await c.put(
                "/api/v1/workspace-settings/ws_undefined/week_start_day",
                json={"value": "monday"},
                headers={"x-bowhead-user-id": app.state.random_id},
            )
            assert r.status_code == 404


class TestDelete:
    @pytest.mark.asyncio
    async def test_admin_can_delete(self, app: FastAPI) -> None:
        store: WorkspaceSettingsStore = app.state.workspace_settings_store
        await store.set(app.state.ws_id, "week_start_day", "sunday")

        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
            r = await c.delete(
                f"/api/v1/workspace-settings/{app.state.ws_id}/week_start_day",
                headers={"x-bowhead-user-id": app.state.admin_id},
            )
            assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_non_admin_delete_rejected(self, app: FastAPI) -> None:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
            r = await c.delete(
                f"/api/v1/workspace-settings/{app.state.ws_id}/week_start_day",
                headers={"x-bowhead-user-id": app.state.random_id},
            )
            assert r.status_code == 403


class TestGetRequiresMembership:
    """GET requires an explicit workspace membership."""

    @pytest.mark.asyncio
    async def test_member_can_read(self, app: FastAPI) -> None:
        store: WorkspaceSettingsStore = app.state.workspace_settings_store
        await store.set(app.state.ws_id, "week_start_day", "sunday")

        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
            r = await c.get(
                f"/api/v1/workspace-settings/{app.state.ws_id}",
                headers={"x-bowhead-user-id": app.state.admin_id},
            )
            assert r.status_code == 200
            assert r.json()["settings"]["week_start_day"] == "sunday"

    @pytest.mark.asyncio
    async def test_non_member_read_rejected(self, app: FastAPI) -> None:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
            r = await c.get(
                f"/api/v1/workspace-settings/{app.state.ws_id}",
                headers={"x-bowhead-user-id": app.state.random_id},
            )
            assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_team_admin_without_space_membership_is_rejected(self, app: FastAPI) -> None:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
            r = await c.put(
                f"/api/v1/workspace-settings/{app.state.ws_id}/week_start_day",
                json={"value": "sunday"},
                headers={"x-bowhead-user-id": app.state.team_admin_id},
            )
            assert r.status_code == 403


class TestMetadataRequiresSpaceAdmin:
    @pytest.mark.asyncio
    async def test_space_admin_can_update_metadata(self, app: FastAPI) -> None:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
            current = await c.get(
                f"/api/v1/workspace-settings/{app.state.ws_id}/metadata",
                headers={"x-bowhead-user-id": app.state.admin_id},
            )
            assert current.status_code == 200
            updated = await c.patch(
                f"/api/v1/workspace-settings/{app.state.ws_id}/metadata",
                json={"name": "Renamed", "expected_revision": current.json()["revision"]},
                headers={"x-bowhead-user-id": app.state.admin_id},
            )
            assert updated.status_code == 200
            assert updated.json()["name"] == "Renamed"

    @pytest.mark.asyncio
    async def test_metadata_save_that_never_gets_the_lease_answers_busy(self, app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
        from bowhead_core.error_catalog import default_error_catalog_dir, load_error_catalog
        from bowhead_gateway.error_handler import register_error_handlers

        from bowhead_core import _db, sqlite_domain_runtime

        register_error_handlers(app)
        app.state.error_catalog = load_error_catalog(default_error_catalog_dir())
        monkeypatch.setattr(_db, "AUTHORITY_BUSY_WAIT_SECONDS", 0.05)

        async def lose_lease(self: UserStore, **kwargs: object) -> None:
            raise sqlite_domain_runtime._busy()

        monkeypatch.setattr(UserStore, "write_admin_audit_log", lose_lease)
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
            headers = {"x-bowhead-user-id": app.state.admin_id}
            current = await c.get(f"/api/v1/workspace-settings/{app.state.ws_id}/metadata", headers=headers)
            busy = await c.patch(
                f"/api/v1/workspace-settings/{app.state.ws_id}/metadata",
                json={"description": "Retail data", "expected_revision": current.json()["revision"]},
                headers={**headers, "Accept-Language": "zh-CN"},
            )
            assert busy.status_code == 503
            assert busy.headers["Retry-After"] == "2"
            assert busy.json()["detail"]["code"] == "store_busy"
            assert "没有保存" in busy.json()["detail"]["message"]
            after = await c.get(f"/api/v1/workspace-settings/{app.state.ws_id}/metadata", headers=headers)
            assert after.json()["revision"] == current.json()["revision"]
            assert after.json()["description"] == current.json()["description"]

    @pytest.mark.asyncio
    async def test_team_admin_cannot_update_metadata_without_space_membership(self, app: FastAPI) -> None:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
            r = await c.get(
                f"/api/v1/workspace-settings/{app.state.ws_id}/metadata",
                headers={"x-bowhead-user-id": app.state.team_admin_id},
            )
            assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_archived_space_rejects_internal_writes(self, app: FastAPI) -> None:
        users: UserStore = app.state.user_store
        async with aiosqlite.connect(users.db_path) as db:
            await db.execute("UPDATE workspaces SET status = 'archived' WHERE id = ?", (app.state.ws_id,))
            await db.commit()
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
            r = await c.put(
                f"/api/v1/workspace-settings/{app.state.ws_id}/week_start_day",
                json={"value": "sunday"},
                headers={"x-bowhead-user-id": app.state.admin_id},
            )
            assert r.status_code == 409
