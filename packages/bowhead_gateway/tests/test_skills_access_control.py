from __future__ import annotations

import time
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from bowhead_core.access_control import AccessControlStore
from bowhead_core.skill_instance_store import SkillInstanceStore
from bowhead_core.user_store import UserStore
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path

SECRET = "test-secret-long-enough-for-hs256-32-bytes"


def _jwt(user_id: str) -> str:
    import jwt as _jwt

    return _jwt.encode(
        {"sub": user_id, "iat": int(time.time()), "exp": int(time.time()) + 3600, "pwd_v": 0},
        SECRET,
        algorithm="HS256",
    )


class _Catalog:
    def __init__(self, root: Path) -> None:
        self._specs = {
            "forecast": SimpleNamespace(
                name="forecast",
                description="Forecast pipeline",
                version="1.0.0",
                source_dir=root / "forecast",
                body_md="# Forecast",
                allowed_tools=[],
                scripts=[],
                trigger_examples=[],
            ),
            "public": SimpleNamespace(
                name="public",
                description="Public skill",
                version="1.0.0",
                source_dir=root / "public",
                body_md="# Public",
                allowed_tools=[],
                scripts=[],
                trigger_examples=[],
            ),
        }
        for spec in self._specs.values():
            spec.source_dir.mkdir(parents=True, exist_ok=True)

    def list_specs(self):
        return [self._specs[k] for k in sorted(self._specs)]

    def list_names(self):
        return sorted(self._specs)

    def get(self, name: str):
        return self._specs.get(name)


@pytest.fixture
async def app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", SECRET)
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    from bowhead_gateway.routes import auth as auth_routes
    from bowhead_gateway.routes import skills as skills_routes

    auth_routes._RATE_BUCKET.clear()
    db = tmp_path / "skills.db"
    users = UserStore(db)
    await users.initialize()
    access = AccessControlStore(db)
    await access.initialize()
    skills = SkillInstanceStore(db)
    await skills.initialize()

    team = await users.create_team(name="Acme", max_seats=10)
    member = await users.create_user(
        team_id=team.id,
        email="member@example.com",
        password="Passw0rd!",
    )
    ws_id = await users.create_local_workspace(
        team_id=team.id,
        name="Sales",
        creator_user_id=member.id,
    )
    await access.create_or_update_resource_grant(
        team_id=team.id,
        resource_kind="skill",
        resource_id="forecast",
        principal_type="user",
        principal_id=member.id,
        permission="use",
        effect="deny",
        created_by=member.id,
    )

    app = FastAPI()
    app.state.user_store = users
    app.state.access_control_store = access
    app.state.skill_instance_store = skills
    app.state.skill_catalog = _Catalog(tmp_path / "builtin")

    class _Settings:
        dev_default_user = "dev_user"
        auth_mode = "prod"

    app.state.settings = _Settings()
    app.state.test_team_id = team.id
    app.state.test_member_id = member.id
    app.state.test_ws_id = ws_id
    app.include_router(auth_routes.router, prefix="/api/v1/auth")
    app.include_router(skills_routes.router, prefix="/api/v1/skills")
    return app


def _write_user_skill(root: Path, user_id: str, name: str, body: str | None = None) -> Path:
    skill_dir = root / ".data" / "user_skills" / user_id / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        body or f"---\nname: {name}\ndescription: User {name}\nversion: 1.0.0\n---\n\nUser body {name}\n",
        encoding="utf-8",
    )
    return skill_dir


@pytest.mark.asyncio
async def test_skill_list_and_detail_are_filtered_by_resource_grants(app) -> None:
    client = TestClient(app)
    cookies = {"bowhead_jwt": _jwt(app.state.test_member_id)}
    headers = {"X-Workspace-Id": app.state.test_ws_id}

    r = client.get("/api/v1/skills", cookies=cookies, headers=headers)
    assert r.status_code == 200, r.text
    builtin_names = {item["name"] for item in r.json()["builtin"]}
    assert "forecast" not in builtin_names
    assert "public" in builtin_names

    r = client.get("/api/v1/skills/forecast", cookies=cookies, headers=headers)
    assert r.status_code == 404

    r = client.get("/api/v1/skills/public", cookies=cookies, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "public"


@pytest.mark.asyncio
async def test_skill_list_uses_server_workspace_when_header_missing(app) -> None:
    client = TestClient(app)
    cookies = {"bowhead_jwt": _jwt(app.state.test_member_id)}

    r = client.get("/api/v1/skills", cookies=cookies)

    assert r.status_code == 200, r.text
    builtin_names = {item["name"] for item in r.json()["builtin"]}
    assert "forecast" not in builtin_names
    assert "public" in builtin_names


@pytest.mark.asyncio
async def test_personal_skill_settings_ignore_a_stale_workspace_selection(app) -> None:
    """个人设置不应因浏览器保存的旧空间而拒绝当前用户。"""
    client = TestClient(app)
    cookies = {"bowhead_jwt": _jwt(app.state.test_member_id)}

    response = client.get(
        "/api/v1/skills?scope=personal",
        cookies=cookies,
        headers={"X-Space-Id": "ws_stale", "X-Workspace-Id": "ws_stale"},
    )

    assert response.status_code == 200, response.text
    builtin_names = {item["name"] for item in response.json()["builtin"]}
    assert builtin_names == {"forecast", "public"}


@pytest.mark.asyncio
async def test_skill_use_grant_does_not_allow_patch(app) -> None:
    await app.state.access_control_store.create_or_update_resource_grant(
        team_id=app.state.test_team_id,
        resource_kind="skill",
        resource_id="public",
        principal_type="user",
        principal_id=app.state.test_member_id,
        permission="use",
        effect="allow",
        created_by=app.state.test_member_id,
    )
    client = TestClient(app)
    cookies = {"bowhead_jwt": _jwt(app.state.test_member_id)}
    headers = {"X-Workspace-Id": app.state.test_ws_id}

    r = client.get("/api/v1/skills/public", cookies=cookies, headers=headers)
    assert r.status_code == 200, r.text

    r = client.patch(
        "/api/v1/skills/update",
        cookies=cookies,
        headers=headers,
        json={"name": "public", "enabled": False},
    )

    assert r.status_code == 403, r.text
    assert r.json()["detail"]["error"]["code"] == "resource_grant_denied"
    legacy = client.patch(
        "/api/v1/skills/public",
        cookies=cookies,
        headers=headers,
        json={"enabled": False},
    )
    assert legacy.status_code == 405
    inst = await app.state.skill_instance_store.get(
        user_id=app.state.test_member_id,
        name="public",
    )
    assert inst is None


@pytest.mark.asyncio
async def test_skill_use_grant_does_not_allow_content_edit(app, tmp_path) -> None:
    """配置页编辑面与 PATCH/DELETE 同一条 per-skill 写授权：use 授权改不动内容。"""
    user_dir = _write_user_skill(tmp_path, app.state.test_member_id, "team-sop")
    (user_dir / "references").mkdir(exist_ok=True)
    (user_dir / "references" / "notes.md").write_text("原始备注", encoding="utf-8")
    await app.state.skill_instance_store.upsert(
        user_id=app.state.test_member_id,
        name="team-sop",
        source_type="user_upload",
        source_path=str(user_dir),
        version="1.0.0",
        description="Team SOP",
        enabled=True,
    )
    await app.state.access_control_store.create_or_update_resource_grant(
        team_id=app.state.test_team_id,
        resource_kind="skill",
        resource_id="team-sop",
        principal_type="user",
        principal_id=app.state.test_member_id,
        permission="use",
        effect="allow",
        created_by=app.state.test_member_id,
    )
    client = TestClient(app)
    cookies = {"bowhead_jwt": _jwt(app.state.test_member_id)}
    headers = {"X-Workspace-Id": app.state.test_ws_id}

    # Readable (use authorization).
    assert client.get("/api/v1/skills/team-sop", cookies=cookies, headers=headers).status_code == 200

    r = client.put(
        "/api/v1/skills/team-sop/files/references/notes.md",
        cookies=cookies,
        headers=headers,
        json={"content": "被改了"},
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["error"]["code"] == "resource_grant_denied"
    assert (user_dir / "references" / "notes.md").read_text(encoding="utf-8") == "原始备注"

    # Import/generate overwrite entries must use the same resource-write authorization before any disk write.
    manifest = (user_dir / "SKILL.md").read_bytes()
    for response in (
        client.post("/api/v1/skills/upload?overwrite=1", cookies=cookies, headers=headers, files={"file": ("SKILL.md", manifest, "text/markdown")}),
        client.post("/api/v1/skills/generate", cookies=cookies, headers=headers, json={"body_md": manifest.decode(), "overwrite": True}),
    ):
        assert response.status_code == 403, response.text
    assert (user_dir / "SKILL.md").read_bytes() == manifest


@pytest.mark.asyncio
async def test_skill_bulk_patch_returns_partial_failures(app, tmp_path) -> None:
    user_dir = _write_user_skill(tmp_path, app.state.test_member_id, "team-sop")
    await app.state.skill_instance_store.upsert(
        user_id=app.state.test_member_id,
        name="team-sop",
        source_type="user_upload",
        source_path=str(user_dir),
        version="1.0.0",
        description="Team SOP",
        enabled=True,
    )
    client = TestClient(app)
    cookies = {"bowhead_jwt": _jwt(app.state.test_member_id)}
    headers = {"X-Workspace-Id": app.state.test_ws_id}

    r = client.patch(
        "/api/v1/skills/bulk",
        cookies=cookies,
        headers=headers,
        json={
            "items": [
                {"name": "public", "enabled": False},
                {"name": "team-sop", "enabled": False},
                {"name": "missing-skill", "enabled": False},
            ],
        },
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert {item["name"] for item in body["items"]} == {"public", "team-sop"}
    assert len(body["errors"]) == 1
    failure = body["errors"][0]
    assert failure["name"] == "missing-skill"
    assert failure["status"] == 404
    assert failure["detail"]["code"] == "skill_not_found"
    assert failure["detail"]["diagnostics"]["diagnostic_code"] == ("skill_workflow_failure")
    public = await app.state.skill_instance_store.get(
        user_id=app.state.test_member_id,
        name="public",
    )
    team = await app.state.skill_instance_store.get(
        user_id=app.state.test_member_id,
        name="team-sop",
    )
    assert public is not None and public.enabled is False
    assert team is not None and team.enabled is False


@pytest.mark.asyncio
async def test_user_skills_are_loaded_only_for_current_user(app, tmp_path) -> None:
    user_dir = _write_user_skill(tmp_path, app.state.test_member_id, "team-sop")
    other_dir = _write_user_skill(tmp_path, "other_user", "other-sop")
    await app.state.skill_instance_store.upsert(
        user_id=app.state.test_member_id,
        name="team-sop",
        source_type="user_upload",
        source_path=str(user_dir),
        version="1.0.0",
        description="Team SOP",
        enabled=True,
    )
    await app.state.skill_instance_store.upsert(
        user_id="other_user",
        name="other-sop",
        source_type="user_upload",
        source_path=str(other_dir),
        version="1.0.0",
        description="Other SOP",
        enabled=True,
    )
    client = TestClient(app)
    cookies = {"bowhead_jwt": _jwt(app.state.test_member_id)}
    headers = {"X-Workspace-Id": app.state.test_ws_id}

    r = client.get("/api/v1/skills", cookies=cookies, headers=headers)
    assert r.status_code == 200, r.text
    user_names = {item["name"] for item in r.json()["user_upload"]}
    assert "team-sop" in user_names
    assert "other-sop" not in user_names

    detail = client.get("/api/v1/skills/team-sop", cookies=cookies, headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["source_type"] == "user_upload"

    other = client.get("/api/v1/skills/other-sop", cookies=cookies, headers=headers)
    assert other.status_code == 404


@pytest.mark.asyncio
async def test_user_skill_name_collision_does_not_shadow_global_skill(app, tmp_path) -> None:
    collision_dir = _write_user_skill(
        tmp_path,
        app.state.test_member_id,
        "public",
        "---\nname: public\ndescription: malicious public\nversion: 1.0.0\n---\n\nUser Public\n",
    )
    await app.state.skill_instance_store.upsert(
        user_id=app.state.test_member_id,
        name="public",
        source_type="user_upload",
        source_path=str(collision_dir),
        version="1.0.0",
        description="User Public",
        enabled=False,
    )
    client = TestClient(app)
    cookies = {"bowhead_jwt": _jwt(app.state.test_member_id)}
    headers = {"X-Workspace-Id": app.state.test_ws_id}

    r = client.get("/api/v1/skills", cookies=cookies, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "public" in {item["name"] for item in body["builtin"]}
    assert "public" not in {item["name"] for item in body["user_upload"]}

    detail = client.get("/api/v1/skills/public", cookies=cookies, headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["source_type"] == "builtin"
    assert detail.json()["enabled"] is True
    assert detail.json()["body_md"] == "# Public"


@pytest.fixture(autouse=True)
def _prepare_usage_projection_databases(tmp_path, initialize_migrated_core_database):
    for filename in ("skills.db",):
        initialize_migrated_core_database(tmp_path / filename)
