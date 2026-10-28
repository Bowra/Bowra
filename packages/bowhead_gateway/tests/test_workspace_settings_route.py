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
                f"/api/v1/workspace-settings/{app.state.ws_id}/i18n_v1",
                json={"value": "true"},
                headers={"x-bowhead-user-id": app.state.admin_id},
            )
            assert r.status_code == 200
            assert r.json()["value"] == "true"

    @pytest.mark.asyncio
    async def test_other_admin_can_set(self, app: FastAPI) -> None:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
            r = await c.put(
                f"/api/v1/workspace-settings/{app.state.ws_id}/i18n_v1",
                json={"value": "true"},
                headers={"x-bowhead-user-id": app.state.other_admin_id},
            )
            assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_non_admin_rejected(self, app: FastAPI) -> None:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
            r = await c.put(
                f"/api/v1/workspace-settings/{app.state.ws_id}/i18n_v1",
                json={"value": "true"},
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
        await store.set(app.state.ws_id, "i18n_v1", "true")

        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
            r = await c.delete(
                f"/api/v1/workspace-settings/{app.state.ws_id}/i18n_v1",
                headers={"x-bowhead-user-id": app.state.admin_id},
            )
            assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_non_admin_delete_rejected(self, app: FastAPI) -> None:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
            r = await c.delete(
                f"/api/v1/workspace-settings/{app.state.ws_id}/i18n_v1",
                headers={"x-bowhead-user-id": app.state.random_id},
            )
            assert r.status_code == 403


class TestGetRequiresMembership:
    """GET requires an explicit workspace membership."""

    @pytest.mark.asyncio
    async def test_member_can_read(self, app: FastAPI) -> None:
        store: WorkspaceSettingsStore = app.state.workspace_settings_store
        await store.set(app.state.ws_id, "i18n_v1", "true")

        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
            r = await c.get(
                f"/api/v1/workspace-settings/{app.state.ws_id}",
                headers={"x-bowhead-user-id": app.state.admin_id},
            )
            assert r.status_code == 200
            assert r.json()["settings"]["i18n_v1"] == "true"

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
                f"/api/v1/workspace-settings/{app.state.ws_id}/i18n_v1",
                json={"value": "true"},
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
                f"/api/v1/workspace-settings/{app.state.ws_id}/i18n_v1",
                json={"value": "true"},
                headers={"x-bowhead-user-id": app.state.admin_id},
            )
            assert r.status_code == 409
