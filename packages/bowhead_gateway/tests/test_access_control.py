from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from bowhead_core.access_control import AccessContext, AccessControlStore, permission_satisfies
from bowhead_core.kb.permission import ScopeContext
from bowhead_core.kb.schemas import KbFileDTO, KbSearchResult, KbSearchResultChunk
from bowhead_core.resource_access import canonical_permission
from bowhead_core.user_store import TeamSkill, UserStore
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from bowhead_skill.catalog import builtin_catalog_dir

SECRET = "test-secret-long-enough-for-hs256-32-bytes"


def _jwt(user_id: str) -> str:
    import jwt as _jwt

    return _jwt.encode(
        {"sub": user_id, "iat": int(time.time()), "exp": int(time.time()) + 3600, "pwd_v": 0},
        SECRET,
        algorithm="HS256",
    )


@pytest.fixture
async def stores(tmp_path):
    db = tmp_path / "access.db"
    users = UserStore(db)
    await users.initialize()
    access = AccessControlStore(db)
    await access.initialize()
    return users, access


async def _seed_team_ws(users: UserStore):
    team = await users.create_team(name="Acme", max_seats=10)
    creator = await users.create_user(
        team_id=team.id,
        email="creator@example.com",
        password="Passw0rd!",
    )
    await users.set_team_creator(team.id, creator.id)
    member = await users.create_user(
        team_id=team.id,
        email="member@example.com",
        password="Passw0rd!",
    )
    ws_id = await users.create_local_workspace(
        team_id=team.id,
        name="Sales",
        creator_user_id=creator.id,
    )
    await users.add_member(
        workspace_id=ws_id,
        user_id=member.id,
        role="member",
        invited_by=creator.id,
    )
    return team, creator, member, ws_id


@pytest.mark.asyncio
async def test_access_store_deny_wins_and_allowlist(stores) -> None:
    users, access = stores
    team, creator, member, ws_id = await _seed_team_ws(users)
    group = await access.create_group(
        team_id=team.id,
        workspace_id=ws_id,
        name="Sales",
        created_by=creator.id,
    )
    await access.add_group_member(
        team_id=team.id,
        group_id=group.id,
        user_id=member.id,
        created_by=creator.id,
    )
    ctx = AccessContext(
        user_id=member.id,
        team_id=team.id,
        workspace_id=ws_id,
        workspace_role="member",
        group_ids=(group.id,),
        policy_epoch=await access.get_policy_epoch(team.id),
    )
    await access.create_or_update_resource_grant(
        team_id=team.id,
        resource_kind="skill",
        resource_id="forecast",
        principal_type="group",
        principal_id=group.id,
        permission="use",
        effect="allow",
        created_by=creator.id,
    )
    deny = await access.create_or_update_resource_grant(
        team_id=team.id,
        resource_kind="skill",
        resource_id="forecast",
        principal_type="user",
        principal_id=member.id,
        permission="use",
        effect="deny",
        created_by=creator.id,
    )

    decision = await access.explain_decision(ctx, "skill", "forecast", "use")
    assert decision["allowed"] is False
    assert decision["reason"] == "denied_by_grant"

    await access.revoke_resource_grant(team.id, deny.id)
    decision = await access.explain_decision(ctx, "skill", "forecast", "use")
    assert decision["allowed"] is True
    assert decision["reason"] == "allowed_by_grant"

    await access.create_or_update_resource_grant(
        team_id=team.id,
        resource_kind="skill",
        resource_id="restricted",
        principal_type="group",
        principal_id="grp_other",
        permission="use",
        effect="allow",
        created_by=creator.id,
    )
    decision = await access.explain_decision(ctx, "skill", "restricted", "use")
    assert decision["allowed"] is False
    assert decision["reason"] == "not_in_allowlist"
    assert await access.is_allowed(ctx, "skill", "restricted", "use", enforce=False) is True
    assert await access.is_allowed(ctx, "skill", "restricted", "use", enforce=True) is False


@pytest.fixture
def app(stores, monkeypatch):
    users, access = stores
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", SECRET)
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    from bowhead_gateway.routes import access_control as access_routes
    from bowhead_gateway.routes import auth as auth_routes

    auth_routes._RATE_BUCKET.clear()
    app = FastAPI()
    app.state.user_store = users
    app.state.access_control_store = access

    class _Settings:
        dev_default_user = "dev_user"
        auth_mode = "prod"

    app.state.settings = _Settings()
    app.include_router(auth_routes.router, prefix="/api/v1/auth")
    app.include_router(access_routes.router, prefix="/api/v1")
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


class _DummyToolRegistry:
    def __init__(self, names: set[str]) -> None:
        self._names = names

    def has(self, name: str) -> bool:
        return name in self._names


@pytest.mark.asyncio
async def test_groups_grants_and_explain_routes(client, stores) -> None:
    users, _access = stores
    _team, creator, member, ws_id = await _seed_team_ws(users)
    jwt = _jwt(creator.id)

    r = client.post(
        "/api/v1/groups",
        json={"name": "Sales", "workspace_id": ws_id},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 201, r.text
    group_id = r.json()["id"]

    r = client.post(
        f"/api/v1/groups/{group_id}/members",
        json={"user_id": member.id},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 201, r.text

    r = client.post(
        "/api/v1/resource-grants",
        json={
            "resource_kind": "kb_scope",
            "resource_id": f"workspace:{ws_id}",
            "principal_type": "group",
            "principal_id": group_id,
            "permission": "read",
            "effect": "allow",
        },
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 201, r.text

    r = client.post(
        "/api/v1/permissions/explain",
        json={
            "workspace_id": ws_id,
            "resource_kind": "kb_scope",
            "resource_id": f"workspace:{ws_id}",
            "permission": "read",
        },
        cookies={"bowhead_jwt": _jwt(member.id)},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["allowed"] is True
    assert body["reason"] == "allowed"
    assert body["grant_id"] is None
    assert body["access_context"]["workspace_role"] == "member"
    assert "group_ids" not in body["access_context"]

    r = client.post(
        "/api/v1/resource-grants",
        json={
            "resource_kind": "kb_scope",
            "resource_id": f"workspace:{ws_id}",
            "principal_type": "user",
            "principal_id": creator.id,
            "permission": "read",
            "effect": "allow",
        },
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 201, r.text

    r = client.post(
        "/api/v1/permissions/explain",
        json={
            "workspace_id": ws_id,
            "resource_kind": "kb_scope",
            "resource_id": f"workspace:{ws_id}",
            "permission": "read",
        },
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["allowed"] is True
    assert body["reason"] == "allowed_by_grant"
    assert body["grant_id"]
    assert body["access_context"]["group_ids"] == []


@pytest.mark.asyncio
async def test_create_group_duplicate_returns_409(client, stores) -> None:
    users, _access = stores
    _team, creator, _member, ws_id = await _seed_team_ws(users)
    jwt = _jwt(creator.id)

    payload = {"name": "Sales", "workspace_id": ws_id}
    r = client.post("/api/v1/groups", json=payload, cookies={"bowhead_jwt": jwt})
    assert r.status_code == 201, r.text

    r = client.post("/api/v1/groups", json=payload, cookies={"bowhead_jwt": jwt})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_use_grant_does_not_satisfy_read(stores) -> None:
    users, access = stores
    team, creator, member, ws_id = await _seed_team_ws(users)
    assert permission_satisfies("use", "read") is False
    await access.create_or_update_resource_grant(
        team_id=team.id,
        resource_kind="kb_file",
        resource_id="kbf_secret",
        principal_type="user",
        principal_id=member.id,
        permission="use",
        effect="allow",
        created_by=creator.id,
    )
    ctx = AccessContext(
        user_id=member.id,
        team_id=team.id,
        workspace_id=ws_id,
        workspace_role="member",
        group_ids=(),
        policy_epoch=await access.get_policy_epoch(team.id),
    )

    decision = await access.explain_decision(ctx, "kb_file", "kbf_secret", "read")
    assert decision["allowed"] is False
    assert decision["reason"] == "not_in_allowlist"


@pytest.mark.asyncio
async def test_skill_invoke_permission_is_distinct_and_namespaced(stores) -> None:
    users, access = stores
    team, creator, member, ws_id = await _seed_team_ws(users)
    assert canonical_permission("invoke") == "use"
    assert canonical_permission("invoke", resource_kind="tool") == "use"
    assert canonical_permission("invoke", resource_kind="skill") == "invoke"
    assert canonical_permission("invoke", resource_kind="team_skill") == "invoke"
    assert permission_satisfies("use", "invoke") is False
    assert permission_satisfies("invoke", "use") is False
    assert permission_satisfies("write", "invoke") is True

    await access.create_or_update_resource_grant(
        team_id=team.id,
        resource_kind="skill",
        resource_id="forecast",
        principal_type="user",
        principal_id=member.id,
        permission="invoke",
        effect="allow",
        created_by=creator.id,
    )
    ctx = AccessContext(
        user_id=member.id,
        team_id=team.id,
        workspace_id=ws_id,
        workspace_role="member",
        group_ids=(),
        policy_epoch=await access.get_policy_epoch(team.id),
    )

    invoke = await access.explain_decision(ctx, "skill", "forecast", "invoke")
    read = await access.explain_decision(ctx, "skill", "forecast", "read")
    assert invoke["allowed"] is True
    assert invoke["reason"] == "allowed_by_grant"
    assert read["allowed"] is False
    assert read["reason"] == "not_in_allowlist"

    with pytest.raises(ValueError, match="only valid for Skill"):
        await access.create_or_update_resource_grant(
            team_id=team.id,
            resource_kind="tool",
            resource_id="skill_invoke",
            principal_type="user",
            principal_id=member.id,
            permission="invoke",
            effect="allow",
            created_by=creator.id,
        )


@pytest.mark.asyncio
async def test_legacy_skill_use_grant_is_read_only_and_deny_remains_narrowing(stores) -> None:
    users, access = stores
    team, creator, member, ws_id = await _seed_team_ws(users)
    ctx = AccessContext(
        user_id=member.id,
        team_id=team.id,
        workspace_id=ws_id,
        workspace_role="member",
        group_ids=(),
        policy_epoch=await access.get_policy_epoch(team.id),
    )
    allow = await access.create_or_update_resource_grant(
        team_id=team.id,
        resource_kind="skill",
        resource_id="forecast",
        principal_type="user",
        principal_id=member.id,
        permission="use",
        effect="allow",
        created_by=creator.id,
    )

    assert (await access.explain_decision(ctx, "skill", "forecast", "read"))["allowed"] is True
    invoke = await access.explain_decision(ctx, "skill", "forecast", "invoke")
    assert invoke["allowed"] is False and invoke["reason"] == "not_in_allowlist"

    await access.revoke_resource_grant(team.id, allow.id)
    await access.create_or_update_resource_grant(
        team_id=team.id,
        resource_kind="skill",
        resource_id="forecast",
        principal_type="user",
        principal_id=member.id,
        permission="use",
        effect="deny",
        created_by=creator.id,
    )

    assert (await access.explain_decision(ctx, "skill", "forecast", "read"))["reason"] == "denied_by_grant"
    assert (await access.explain_decision(ctx, "skill", "forecast", "invoke"))["reason"] == "denied_by_grant"


@pytest.mark.asyncio
async def test_access_api_keeps_invoke_distinct_only_for_skill_resources(client, stores) -> None:
    users, _access = stores
    _team, creator, _member, _ws_id = await _seed_team_ws(users)
    client.app.state.skill_catalog = SimpleNamespace(
        get=lambda name: SimpleNamespace(source_dir=builtin_catalog_dir()) if name == "forecast" else None
    )
    client.app.state.tool_registry = _DummyToolRegistry({"skill_invoke"})
    cookie = {"bowhead_jwt": _jwt(creator.id)}

    skill = client.post(
        "/api/v1/resource-grants",
        json={
            "resource_kind": "skill",
            "resource_id": "forecast",
            "principal_type": "user",
            "principal_id": creator.id,
            "permission": "invoke",
        },
        cookies=cookie,
    )
    tool = client.post(
        "/api/v1/resource-grants",
        json={
            "resource_kind": "tool",
            "resource_id": "skill_invoke",
            "principal_type": "user",
            "principal_id": creator.id,
            "permission": "invoke",
        },
        cookies=cookie,
    )

    assert skill.status_code == 201, skill.text
    assert skill.json()["permission"] == "invoke"
    assert tool.status_code == 201, tool.text
    assert tool.json()["permission"] == "use"


@pytest.mark.asyncio
async def test_team_skill_invoke_grant_does_not_publish_its_audience(client, stores) -> None:
    users, _access = stores
    team, creator, _member, _ws_id = await _seed_team_ws(users)
    skill = await users.create_team_skill(
        TeamSkill(
            id="oskill_forecast",
            team_id=team.id,
            name="forecast",
            source_path="/tmp/forecast",
            visibility="creator_only",
            created_by=creator.id,
            approval="approved",
        )
    )

    response = client.post(
        "/api/v1/resource-grants",
        json={
            "resource_kind": "team_skill",
            "resource_id": skill.id,
            "principal_type": "team",
            "principal_id": team.id,
            "permission": "invoke",
        },
        cookies={"bowhead_jwt": _jwt(creator.id)},
    )

    assert response.status_code == 201, response.text
    assert response.json()["permission"] == "invoke"
    assert (await users.get_team_skill(team.id, skill.id)).visibility == "creator_only"


@pytest.mark.asyncio
async def test_resource_grant_rejects_cross_team_user(client, stores) -> None:
    users, _access = stores
    team, creator, _member, ws_id = await _seed_team_ws(users)
    other_team = await users.create_team(name="Other", max_seats=10)
    other_user = await users.create_user(
        team_id=other_team.id,
        email="other@example.com",
        password="Passw0rd!",
    )

    r = client.post(
        "/api/v1/resource-grants",
        json={
            "resource_kind": "kb_scope",
            "resource_id": f"workspace:{ws_id}",
            "principal_type": "user",
            "principal_id": other_user.id,
            "permission": "read",
            "effect": "allow",
        },
        cookies={"bowhead_jwt": _jwt(creator.id)},
    )
    assert r.status_code == 404
    assert team.id != other_team.id


@pytest.mark.asyncio
async def test_resource_grant_canonicalizes_permission_alias_and_capability(client, stores) -> None:
    users, _access = stores
    _team, creator, _member, _ws_id = await _seed_team_ws(users)

    r = client.post(
        "/api/v1/resource-grants",
        json={
            "resource_kind": "builtin_capability",
            "resource_id": "kb",
            "principal_type": "team",
            "principal_id": creator.team_id,
            "permission": "view",
            "effect": "allow",
        },
        cookies={"bowhead_jwt": _jwt(creator.id)},
    )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["resource_kind"] == "builtin_capability"
    assert body["resource_id"] == "kb_search"
    assert body["permission"] == "read"


@pytest.mark.asyncio
async def test_resource_grant_rejects_unknown_capability_and_unregistered_tool(client, stores) -> None:
    users, _access = stores
    _team, creator, _member, _ws_id = await _seed_team_ws(users)
    client.app.state.tool_registry = _DummyToolRegistry({"web_search"})

    r = client.post(
        "/api/v1/resource-grants",
        json={
            "resource_kind": "builtin_capability",
            "resource_id": "unknown_capability",
            "principal_type": "team",
            "principal_id": creator.team_id,
            "permission": "use",
            "effect": "allow",
        },
        cookies={"bowhead_jwt": _jwt(creator.id)},
    )
    assert r.status_code == 404

    r = client.post(
        "/api/v1/resource-grants",
        json={
            "resource_kind": "tool",
            "resource_id": "not_registered",
            "principal_type": "team",
            "principal_id": creator.team_id,
            "permission": "use",
            "effect": "allow",
        },
        cookies={"bowhead_jwt": _jwt(creator.id)},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_kb_scope_grant_helper_enforces_deny(stores, monkeypatch) -> None:
    _users, access = stores
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    await access.create_or_update_resource_grant(
        team_id="team_acme",
        resource_kind="kb_scope",
        resource_id="workspace:ws_sales",
        principal_type="user",
        principal_id="u_alice",
        permission="read",
        effect="deny",
        created_by="u_admin",
    )

    app = FastAPI()
    app.state.access_control_store = access
    request = Request({"type": "http", "app": app, "headers": []})
    request.state.bowhead_access_context = AccessContext(
        user_id="u_alice",
        team_id="team_acme",
        workspace_id="ws_sales",
        workspace_role="member",
        group_ids=(),
    ).__dict__
    ctx = ScopeContext(
        user_id="u_alice",
        workspace_id="ws_sales",
        team_id="team_acme",
        role="user",
    )

    from bowhead_gateway.routes.kb import _require_kb_scope_grant

    with pytest.raises(HTTPException) as exc:
        await _require_kb_scope_grant(
            request,
            ctx,
            scope="workspace",
            scope_id="ws_sales",
            permission="read",
        )

    assert exc.value.status_code == 403
    assert exc.value.detail["error"]["code"] == "resource_grant_denied"


@pytest.mark.asyncio
async def test_kb_file_grant_helper_enforces_deny(stores, monkeypatch) -> None:
    _users, access = stores
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    await access.create_or_update_resource_grant(
        team_id="team_acme",
        resource_kind="kb_file",
        resource_id="kbf_secret",
        principal_type="user",
        principal_id="u_alice",
        permission="read",
        effect="deny",
        created_by="u_admin",
    )

    app = FastAPI()
    app.state.access_control_store = access
    request = Request({"type": "http", "app": app, "headers": []})
    request.state.bowhead_access_context = AccessContext(
        user_id="u_alice",
        team_id="team_acme",
        workspace_id="ws_sales",
        workspace_role="member",
        group_ids=(),
    ).__dict__
    ctx = ScopeContext(
        user_id="u_alice",
        workspace_id="ws_sales",
        team_id="team_acme",
        role="user",
    )
    file = KbFileDTO(
        file_id="kbf_secret",
        artifact_id="art_secret",
        filename="secret.pdf",
        mime_type="application/pdf",
        size_bytes=12,
        scope="workspace",
        scope_id="ws_sales",
        workspace_id="ws_sales",
        team_id="team_acme",
        uploaded_by="u_admin",
        uploaded_at="2026-06-05T00:00:00Z",
    )

    from bowhead_gateway.routes.kb import _require_kb_file_grant

    assert (
        await _require_kb_file_grant(
            request,
            ctx,
            file,
            permission="read",
            strict=False,
        )
        is False
    )
    with pytest.raises(HTTPException) as exc:
        await _require_kb_file_grant(
            request,
            ctx,
            file,
            permission="read",
        )

    assert exc.value.status_code == 403
    assert exc.value.detail["error"]["resource_kind"] == "kb_file"


@pytest.mark.asyncio
async def test_kb_search_result_filters_denied_file_chunks(stores, monkeypatch) -> None:
    _users, access = stores
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    await access.create_or_update_resource_grant(
        team_id="team_acme",
        resource_kind="kb_file",
        resource_id="kbf_secret",
        principal_type="user",
        principal_id="u_alice",
        permission="read",
        effect="deny",
        created_by="u_admin",
    )

    app = FastAPI()
    app.state.access_control_store = access
    request = Request({"type": "http", "app": app, "headers": []})
    request.state.bowhead_access_context = AccessContext(
        user_id="u_alice",
        team_id="team_acme",
        workspace_id="ws_sales",
        workspace_role="member",
        group_ids=(),
    ).__dict__
    ctx = ScopeContext(
        user_id="u_alice",
        workspace_id="ws_sales",
        team_id="team_acme",
        role="user",
    )
    result = KbSearchResult(
        chunks=[
            KbSearchResultChunk(
                chunk_id="c_secret",
                content="secret content",
                file_id="kbf_secret",
                file_name="secret.pdf",
                role="intro",
                score=0.9,
                scope="workspace",
            ),
            KbSearchResultChunk(
                chunk_id="c_public",
                content="public content",
                file_id="kbf_public",
                file_name="public.pdf",
                role="intro",
                score=0.8,
                scope="workspace",
            ),
        ],
        total_candidates=2,
        rerank_used=True,
        latency_ms=5,
    )

    from bowhead_gateway.routes.kb import _filter_kb_search_result_by_file_grants

    filtered = await _filter_kb_search_result_by_file_grants(request, ctx, result)

    assert [chunk.chunk_id for chunk in filtered.chunks] == ["c_public"]
