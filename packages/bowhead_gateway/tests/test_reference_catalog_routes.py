"""Production route registration, bounded decoding and real login/space gates."""

from __future__ import annotations

import json
import sqlite3

import pytest
from bowhead_gateway.routes import chat
from fastapi.testclient import TestClient
from test_team_chart_styles_routes import _as
from test_team_chart_styles_routes import app_ctx as _app_ctx

app_ctx = _app_ctx
pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
URL = "/api/v1/chat/references/catalog"


@pytest.fixture
async def client_ctx(app_ctx):
    space = await app_ctx.users.create_local_workspace(team_id=app_ctx.team.id, name="Catalog Space", creator_user_id=app_ctx.member.id)
    app_ctx.app.include_router(chat.router, prefix="/api/v1/chat")
    return TestClient(app_ctx.app), {"workspace_id": space, "trigger": "@"}


async def test_real_auth_and_workspace_scope_then_safe_read_only_projection(client_ctx, app_ctx):
    client, body = client_ctx
    assert client.post(URL, json=body).status_code == 401
    valid = client.post(URL, json=body, cookies=_as(app_ctx.member))
    assert valid.status_code == 200, valid.text
    result = valid.json()
    assert result["input_schema_versions"] == [1]
    assert len(result["entry_support"]) == 7
    assert all(cell["supported"] is False for cell in result["entry_support"])
    assert valid.headers["cache-control"] == "private, no-store"
    for actor in (app_ctx.owner, app_ctx.other_owner):
        denied = client.post(URL, json=body, cookies=_as(actor))
        assert denied.status_code == 404
        assert denied.json() == {"detail": {"error": {"code": "reference_scope_unavailable"}}}
    with sqlite3.connect(app_ctx.users.db_path) as db:
        db.execute("UPDATE workspace_members SET status='removed' WHERE user_id=?", (app_ctx.member.id,))
    assert client.post(URL, json={**body, "trigger": "/"}, cookies=_as(app_ctx.member)).status_code == 404


@pytest.mark.parametrize(
    "patch",
    [
        {"team_id": "private-scope"},
        {"user_id": "private-person"},
        {"query": "x" * 129},
        {"trigger": "?"},
        {"selected_refs": [{}]},
        {"entry_mode": "unknown"},
        {"submission_protocol": "chat-submit.v1"},
        {"cursor": ""},
    ],
)
async def test_validation_never_echoes_input_or_pydantic_details(client_ctx, app_ctx, patch):
    client, body = client_ctx
    response = client.post(URL, json={**body, **patch}, cookies=_as(app_ctx.member))
    assert response.status_code == 422
    assert response.json() == {"detail": {"error": {"code": "reference_input_invalid"}}}


@pytest.mark.parametrize(
    "raw,status",
    [
        (b'{"trigger":"@","trigger":"/"}', 422),
        (b'{"private":"' + b"x" * (32 * 1024) + b'"}', 413),
        (b'{"private":' + b"[" * 20 + b"0" + b"]" * 20 + b"}", 422),
        (b'{"private":"\xff"}', 422),
    ],
)
async def test_stream_body_is_bounded_and_duplicate_keys_rejected(client_ctx, app_ctx, raw, status):
    client, _body = client_ctx
    response = client.post(URL, content=raw, headers={"Content-Type": "application/json; charset=utf-8"}, cookies=_as(app_ctx.member))
    assert response.status_code == status
    assert set(response.json()["detail"]["error"]) == {"code"}


async def test_cursor_and_store_failure_are_finite_errors(client_ctx, app_ctx):
    client, body = client_ctx
    invalid = client.post(URL, json={**body, "cursor": "private cursor"}, cookies=_as(app_ctx.member))
    assert invalid.status_code == 409 and invalid.json()["detail"]["error"]["code"] == "reference_catalog_cursor_invalid"
    with sqlite3.connect(app_ctx.users.db_path) as db:
        db.execute("ALTER TABLE access_policy_state RENAME TO test_unavailable_policy")
    failed = client.post(URL, json=body, cookies=_as(app_ctx.member))
    assert failed.status_code == 503 and failed.json()["detail"]["error"]["code"] == "reference_catalog_unavailable"


async def test_wrong_media_type_and_nonexistent_session_do_not_fall_back(client_ctx, app_ctx):
    client, body = client_ctx
    assert client.post(URL, content="{}", cookies=_as(app_ctx.member)).status_code == 415
    denied = client.post(URL, json={**body, "session_id": "s_missing"}, cookies=_as(app_ctx.member))
    assert denied.status_code == 404 and denied.json()["detail"]["error"]["code"] == "reference_scope_unavailable"


async def test_prefixed_route_keeps_auth_and_strict_decoder(client_ctx, app_ctx):
    client, body = client_ctx
    app_ctx.app.include_router(chat.router, prefix="/bowhead/api/v1/chat")
    url = "/bowhead" + URL
    assert client.post(url, json=body, cookies=_as(app_ctx.member)).status_code == 200
    assert client.post(url, json=body, cookies=_as(app_ctx.other_owner)).status_code == 404
    raw = json.dumps({**body, "query": "first"}).replace('"query": "first"', '"query": "first", "query": "private"')
    malformed = client.post(url, content=raw, headers={"Content-Type": "application/json"}, cookies=_as(app_ctx.member))
    assert malformed.status_code == 422 and "private" not in malformed.text


async def test_duplicate_key_in_an_otherwise_valid_request_is_rejected(client_ctx, app_ctx):
    client, body = client_ctx
    raw = json.dumps(body)
    assert client.post(URL, content=raw, headers={"Content-Type": "application/json"}, cookies=_as(app_ctx.member)).status_code == 200
    duplicated = raw.replace('"trigger": "@"', '"trigger": "@", "trigger": "/"')
    assert client.post(URL, content=duplicated, headers={"Content-Type": "application/json"}, cookies=_as(app_ctx.member)).status_code == 422


@pytest.fixture
async def builtin_ctx(app_ctx, client_ctx, tmp_path, monkeypatch):
    from bowhead_core.access_control import AccessControlStore
    from bowhead_core.skill_instance_store import SkillInstanceStore
    from bowhead_core.skill_reference_metadata import build_builtin_skill_metadata

    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    with sqlite3.connect(app_ctx.users.db_path) as db:
        db.execute("PRAGMA journal_mode=WAL")
    root = tmp_path / "installed"
    for name in ["dupont", "python_sandbox", *[f"method_{i:02}" for i in range(23)]]:
        path = root / name
        (path / "scripts").mkdir(parents=True)
        declaration = {
            "schema_version": 1,
            "entry_file": "SKILL.md",
            "required_files": [],
            "parameter_schema": {
                "type": "object",
                "properties": {"company": {"type": "string", "default": "Example"}},
                "additionalProperties": False,
            },
            "required_skill_refs": [],
            "requires_script": False,
            "requires_new_tools": False,
            "requires_new_connections": False,
        }
        frontmatter = {"name": name, "description": "Profitability method", "read_only_entry": declaration}
        (path / "SKILL.md").write_text("---\n" + json.dumps(frontmatter) + "\n---\nPRIVATE_BODY_NOT_IN_METADATA\n")
        (path / "scripts/unused.py").write_text("raise RuntimeError('MUST_NOT_EXECUTE')")
    index = build_builtin_skill_metadata(builtin_root=root)
    access = AccessControlStore(app_ctx.users.db_path)
    await access.initialize()
    # Existing policy permits resources without an allowlist. Owner-only lists
    # make member grants meaningful without changing platform defaults.
    for kind, name in [("skill", item.name) for item in index.items] + [("builtin_capability", "python_sandbox")]:
        await access.create_or_update_resource_grant(
            team_id=app_ctx.team.id, resource_kind=kind, resource_id=name, principal_type="user", principal_id=app_ctx.owner.id, permission="use"
        )
    instances = SkillInstanceStore(app_ctx.users.db_path)
    await instances.initialize()
    app_ctx.app.state.access_control_store = access
    app_ctx.app.state.builtin_skill_reference_metadata = index

    async def grant(name, *, kind="skill", effect="allow"):
        return await access.create_or_update_resource_grant(
            team_id=app_ctx.team.id,
            resource_kind=kind,
            resource_id=name,
            principal_type="user",
            principal_id=app_ctx.member.id,
            permission="use",
            effect=effect,
        )

    return root, index, access, instances, grant


async def test_builtin_metadata_uses_real_grants_capability_and_preferences(client_ctx, app_ctx, builtin_ctx, monkeypatch):
    from bowhead_core import skill_reference_metadata as metadata

    root, index, access, instances, grant = builtin_ctx
    client, body = client_ctx
    body = {**body, "trigger": "/"}

    def get():
        response = client.post(URL, json=body, cookies=_as(app_ctx.member), headers={"X-Space-Id": "ws_wrong"})
        assert response.status_code == 200, response.text
        assert all(not cell["supported"] for cell in response.json()["entry_support"])
        assert str(root) not in response.text and "PRIVATE_BODY_NOT_IN_METADATA" not in response.text
        return response.json()

    def forbidden(*args, **kwargs):
        raise AssertionError("request-time package scan")

    monkeypatch.setattr(metadata, "load_skill", forbidden)
    monkeypatch.setattr(metadata, "bundle_sha256", forbidden)
    assert get()["groups"][0]["items"] == []
    await grant("dupont")
    await grant("python_sandbox")
    first = get()
    assert [item["id"] for item in first["groups"][0]["items"]] == ["builtin:dupont"]
    item = first["groups"][0]["items"][0]
    assert item["resource"]["version"] == index.items[0].revision
    assert item["read_only_parameter_schema"]["properties"]["company"]["default"] == "Example"
    await grant("python_sandbox", kind="builtin_capability")
    assert len(get()["groups"][0]["items"]) == 2
    await instances.upsert(user_id=app_ctx.member.id, name="dupont", source_type="user_upload", source_path="private-shadow", enabled=False)
    assert get()["groups"][0]["items"][0]["reason_code"] == "unsupported_entry"
    await instances.upsert(user_id=app_ctx.member.id, name="dupont", source_type="builtin", source_path="", enabled=False)
    disabled = get()
    assert disabled["groups"][0]["items"][0]["reason_code"] == "resource_unavailable"
    assert disabled["catalog_generation"] != first["catalog_generation"]
    await grant("dupont", effect="deny")
    assert [item["id"] for item in get()["groups"][0]["items"]] == ["builtin:python_sandbox"]


async def test_builtin_paging_rechecks_current_policy_and_index_generation(client_ctx, app_ctx, builtin_ctx):
    from bowhead_core.skill_reference_metadata import build_builtin_skill_metadata

    root, index, access, instances, grant = builtin_ctx
    client, body = client_ctx
    body = {**body, "trigger": "/", "query": "method"}
    for i in range(23):
        await grant(f"method_{i:02}")

    def get(**patch):
        return client.post(URL, json={**body, **patch}, cookies=_as(app_ctx.member))

    first = get().json()
    assert len(first["groups"][0]["items"]) == 20 and first["next_cursor"]
    second = get(cursor=first["next_cursor"]).json()
    assert [item["id"] for item in second["groups"][0]["items"]] == [f"builtin:method_{i:02}" for i in range(20, 23)]
    assert second["next_cursor"] is None
    (root / "method_00/scripts/unused.py").write_text("changed unused file")
    app_ctx.app.state.builtin_skill_reference_metadata = build_builtin_skill_metadata(builtin_root=root)
    assert get(cursor=first["next_cursor"]).status_code == 409
    fresh = get().json()
    await grant("method_22", effect="deny")
    assert get(cursor=fresh["next_cursor"]).status_code == 409
    assert get(query="method_22").json()["groups"][0]["items"] == []


@pytest.mark.parametrize("failure", ["access", "preferences", "scope", "partial"])
async def test_builtin_group_failure_never_falls_back_to_unfiltered_packages(client_ctx, app_ctx, builtin_ctx, monkeypatch, failure):
    from dataclasses import replace

    root, index, access, instances, grant = builtin_ctx
    client, body = client_ctx
    await grant("dupont")
    if failure == "access":
        original = access.explain_decision

        async def unavailable(*args, **kwargs):
            if args[1:3] == ("skill", "dupont"):
                raise RuntimeError("private-account-path")
            return await original(*args, **kwargs)

        monkeypatch.setattr(access, "explain_decision", unavailable)
    elif failure == "preferences":
        with sqlite3.connect(app_ctx.users.db_path) as db:
            db.execute("ALTER TABLE skill_instances RENAME TO unavailable_instances")
    elif failure == "scope":
        body["workspace_id"] = "ws_wrong"
    else:
        app_ctx.app.state.builtin_skill_reference_metadata = replace(index, incomplete=True)
    response = client.post(URL, json={**body, "trigger": "/"}, cookies=_as(app_ctx.member))
    assert "private-account-path" not in response.text
    if failure == "scope":
        assert response.status_code == 404
    else:
        assert response.status_code == 200, response.text
        groups = response.json()["groups"]
        assert groups[0]["error"] == "catalog_metadata_unavailable"
        assert bool(groups[0]["items"]) is (failure == "partial")
        assert groups[1]["id"] == "capabilities" and groups[1]["error"] is None
        assert groups[2]["id"] == "mcp" and groups[2]["error"] == "catalog_adapter_unavailable"


async def test_builtin_grant_change_during_projection_does_not_issue_mixed_generation(client_ctx, app_ctx, builtin_ctx, monkeypatch):
    root, index, access, instances, grant = builtin_ctx
    client, body = client_ctx
    await grant("dupont")
    original = access.explain_decision
    changed = False

    async def changing(*args, **kwargs):
        nonlocal changed
        allowed = await original(*args, **kwargs)
        if not changed and args[1:3] == ("skill", "dupont"):
            await grant("dupont", effect="deny")
            changed = True
        return allowed

    monkeypatch.setattr(access, "explain_decision", changing)
    response = client.post(URL, json={**body, "trigger": "/"}, cookies=_as(app_ctx.member))
    assert response.status_code == 200
    assert changed, "concurrent policy update must commit successfully"
    assert response.json()["groups"][0] == {"id": "skills", "items": [], "error": "catalog_metadata_unavailable"}


@pytest.mark.parametrize("prefix", ["", "/bowhead"])
async def test_mcp_directory_uses_background_snapshot_and_current_grants(client_ctx, app_ctx, builtin_ctx, monkeypatch, prefix):
    from bowhead_core.openapi_tool_source import op_tool_name
    from bowhead_core.reference_mcp_metadata import ReferenceMcpMetadataCache
    from bowhead_core.user_store import TeamToolSource

    from bowhead_core import reference_mcp_metadata as metadata

    client, body = client_ctx
    _root, _index, _access, _instances, grant = builtin_ctx
    source_id = "ots_123456789abc"
    operation_id = "getMargin"
    specification = {
        "openapi": "3.0.0",
        "info": {"title": "Finance", "version": "1"},
        "paths": {
            "/margin": {
                "get": {
                    "operationId": operation_id,
                    "summary": "Read margin",
                    "parameters": [{"name": "year", "in": "query", "schema": {"type": "integer"}}],
                }
            }
        },
    }
    await app_ctx.users.create_team_tool_source(
        TeamToolSource(
            id=source_id,
            team_id=app_ctx.team.id,
            name="Finance API",
            base_url="https://finance.example/api",
            openapi_spec_json=json.dumps(specification),
            selected_op_ids=[operation_id],
            visibility="team_wide",
            created_by=app_ctx.member.id,
            created_at="2026-09-16T00:00:00+00:00",
            updated_at="2026-09-16T00:00:00+00:00",
        )
    )
    await grant(source_id, kind="team_tool_source")
    await grant(op_tool_name(source_id, operation_id), kind="team_tool_op")
    cache = ReferenceMcpMetadataCache(app_ctx.users.db_path)
    await cache.refresh()
    app_ctx.app.state.reference_mcp_metadata = cache
    if prefix:
        app_ctx.app.include_router(chat.router, prefix=prefix + "/api/v1/chat")

    def forbidden(*args, **kwargs):
        raise AssertionError("catalog request parsed the stored Tool contract")

    monkeypatch.setattr(metadata, "_entries", forbidden)
    response = client.post(prefix + URL, json={**body, "trigger": "/"}, cookies=_as(app_ctx.member))
    assert response.status_code == 200, response.text
    mcp = response.json()["groups"][2]
    assert mcp["error"] is None
    assert [item["kind"] for item in mcp["items"]] == ["mcp_source", "mcp_tool"]
    assert all(item["availability"] == "unavailable" for item in mcp["items"])
    assert mcp["items"][1]["resource"]["operation_name"] == op_tool_name(source_id, operation_id)
    assert mcp["items"][1]["remote_version"] == {"status": "unknown", "value": None, "evidence": None}
    assert "auth_ref" not in response.text and "finance.example" not in response.text

    tool = mcp["items"][1]

    def exact(ref):
        response = client.post(
            prefix + URL, json={**body, "trigger": "/", "exact_resource": ref, "selected_refs": [ref]}, cookies=_as(app_ctx.member)
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["next_cursor"] is None
        return [item for group in result["groups"] for item in group["items"]]

    assert exact(tool["resource"]) == [tool]
    for field in ("local_contract_revision", "connection_subject_fingerprint", "operation_contract_revision"):
        assert exact({**tool["resource"], field: "sha256:" + "0" * 64}) == []
    assert exact({**tool["resource"], "connection_binding_id": "ots_other"}) == []

    await grant(op_tool_name(source_id, operation_id), kind="team_tool_op", effect="deny")
    assert exact(tool["resource"]) == []
    operation_denied = client.post(prefix + URL, json={**body, "trigger": "/"}, cookies=_as(app_ctx.member))
    assert operation_denied.status_code == 200
    assert [item["kind"] for item in operation_denied.json()["groups"][2]["items"]] == ["mcp_source"]
    await grant(source_id, kind="team_tool_source", effect="deny")
    source_denied = client.post(prefix + URL, json={**body, "trigger": "/"}, cookies=_as(app_ctx.member))
    assert source_denied.status_code == 200
    assert source_denied.json()["groups"][2]["items"] == []

    await app_ctx.users.update_team_tool_source_meta(app_ctx.team.id, source_id, status="disabled")
    disabled = client.post(prefix + URL, json={**body, "trigger": "/"}, cookies=_as(app_ctx.member)).json()["groups"][2]
    assert disabled == {"id": "mcp", "items": [], "error": "catalog_metadata_unavailable"}


@pytest.mark.parametrize("prefix", ["", "/bowhead"])
async def test_uploaded_metadata_route_uses_current_cache_and_permissions(client_ctx, app_ctx, builtin_ctx, tmp_path, monkeypatch, prefix):
    from shutil import copytree

    from bowhead_core.skill_upload import bundle_sha256
    from bowhead_core.uploaded_skill_reference_metadata import UploadedSkillMetadataCache
    from bowhead_core.user_store import TeamSkill

    from bowhead_core import uploaded_skill_reference_metadata as metadata

    client, body = client_ctx
    root, _, access, instances, grant = builtin_ctx
    await grant("dupont")
    personal_path = copytree(root / "dupont", tmp_path / "personal-route")
    personal = await instances.upsert(user_id=app_ctx.member.id, name="dupont", source_type="user_upload", source_path=str(personal_path))
    team_path = copytree(root / "dupont", tmp_path / "team-route")
    team = await app_ctx.users.create_team_skill(
        TeamSkill(
            id="oskill_route",
            team_id=app_ctx.team.id,
            name="dupont",
            source_path=str(team_path),
            created_by=app_ctx.owner.id,
            approved_sha256=bundle_sha256(team_path),
        )
    )
    clock = [100.0]
    monkeypatch.setattr(metadata, "monotonic", lambda: clock[0])
    cache = UploadedSkillMetadataCache(app_ctx.users.db_path)
    await cache.refresh()
    app_ctx.app.state.uploaded_skill_reference_metadata = cache
    if prefix:
        app_ctx.app.include_router(chat.router, prefix=prefix + "/api/v1/chat")

    def forbidden(*args, **kwargs):
        raise AssertionError("catalog request opened uploaded package")

    monkeypatch.setattr(metadata, "_capture", forbidden)

    def get():
        response = client.post(prefix + URL, json={**body, "trigger": "/"}, cookies=_as(app_ctx.member))
        assert response.status_code == 200, response.text
        assert str(tmp_path) not in response.text and "PRIVATE_BODY_NOT_IN_METADATA" not in response.text
        assert all(not cell["supported"] for cell in response.json()["entry_support"])
        return response.json()["groups"][0]["items"]

    items = get()
    assert {(item["source_kind"], item["id"]) for item in items} == {("builtin", "builtin:dupont"), ("user", personal.id), ("team", team.id)}
    exact = next(item for item in items if item["id"] == personal.id)["resource"]
    assert exact is not None and exact["version"] == "sha256:" + bundle_sha256(personal_path)
    await access.create_or_update_resource_grant(
        team_id=app_ctx.team.id,
        resource_kind="team_skill",
        resource_id=team.id,
        principal_type="user",
        principal_id=app_ctx.member.id,
        permission="use",
        effect="deny",
    )
    assert all(item["id"] != team.id for item in get())
    clock[0] = 130.0
    stale = next(item for item in get() if item["id"] == personal.id)
    assert stale["resource"] is None and stale["read_only_parameter_schema"] is None and stale["reason_code"] == "catalog_metadata_unavailable"


@pytest.mark.parametrize("prefix", ["", "/bowhead"])
async def test_system_metadata_route_uses_real_startup_profiles_and_cache(client_ctx, app_ctx, monkeypatch, prefix):
    from bowhead_core.agents.registry import AgentRegistry
    from bowhead_core.agents.turn_runner_v2_entry import bootstrap_multi_agent
    from bowhead_core.builtin_subagent_override_store import BuiltinSubagentOverride, BuiltinSubagentOverrideStore
    from bowhead_core.subagent import PROFILES, SubagentRunner
    from bowhead_core.system_agent_reference_metadata import SystemAgentReferenceMetadataCache
    from bowhead_core.tool_registry import build_default_registry

    from bowhead_core import system_agent_reference_metadata as metadata

    monkeypatch.setenv("BOWHEAD_SUBAGENT_SPEC_RESOLVE_ENABLED", "0")
    for name in AgentRegistry.bootstrap().list_all():
        monkeypatch.setitem(PROFILES, name, PROFILES.get(name))
    bootstrap_multi_agent(build_default_registry())
    client, body = client_ctx
    runner = SubagentRunner(router=None, tool_registry=None, team_subagent_store=app_ctx.users)
    clock = [100.0]
    monkeypatch.setattr(metadata, "monotonic", lambda: clock[0])
    cache = SystemAgentReferenceMetadataCache(app_ctx.users.db_path, runner)
    app_ctx.app.state.system_agent_reference_metadata = cache
    if prefix:
        app_ctx.app.include_router(chat.router, prefix=prefix + "/api/v1/chat")

    def get():
        response = client.post(prefix + URL, json=body, cookies=_as(app_ctx.member))
        assert response.status_code == 200, response.text
        assert all(not cell["supported"] for cell in response.json()["entry_support"])
        return response.json()["groups"][1]

    assert get() == {"id": "system_agents", "items": [], "error": "catalog_metadata_unavailable"}
    await cache.refresh()

    def forbidden(*args, **kwargs):
        raise AssertionError("request-time system profile capture")

    monkeypatch.setattr(metadata, "_build_scope", forbidden)
    item = next(item for item in get()["items"] if item["id"] == "system:compute")
    assert item["resource"] is not None and item["resource"]["source_kind"] == "system"
    assert item["resource"] == {"kind": "agent", "source_kind": "system", "id": "system:compute", "version": item["resource"]["version"]}
    assert item["reason_code"] == "unsupported_entry"
    await BuiltinSubagentOverrideStore(str(app_ctx.users.db_path)).upsert(
        BuiltinSubagentOverride(team_id=app_ctx.team.id, name="compute", enabled=False),
        expected_version=None,
        actor_user_id=app_ctx.owner.id,
        audit_action="test_override",
    )
    assert all(item["id"] != "system:compute" for item in get()["items"])
    clock[0] = 130.0
    assert get() == {"id": "system_agents", "items": [], "error": "catalog_metadata_unavailable"}


async def test_saved_skill_parameter_lookup_is_exact_current_and_not_paginated(client_ctx, app_ctx, builtin_ctx):
    root, index, access, instances, grant = builtin_ctx
    client, body = client_ctx
    for item in index.items:
        await grant(item.name)
    skill = next(item for item in index.items if item.name == "method_22")
    resource = {"kind": "skill", "source_kind": "builtin", "id": skill.id, "version": skill.revision}

    def lookup(ref=resource):
        result = client.post(URL, json={**body, "trigger": "/", "exact_resource": ref, "selected_refs": [ref]}, cookies=_as(app_ctx.member))
        assert result.status_code == 200, result.text
        return result.json()

    result = lookup()
    assert result["next_cursor"] is None
    assert [item["resource"] for group in result["groups"] for item in group["items"]] == [resource]
    assert result["groups"][0]["items"][0]["read_only_parameter_schema"]["properties"]["company"]["default"] == "Example"
    assert "PRIVATE_BODY_NOT_IN_METADATA" not in json.dumps(result)
    for ref in ({**resource, "version": "sha256:" + "0" * 64}, {**resource, "source_kind": "team"}, {**resource, "id": "builtin:missing"}):
        assert lookup(ref)["groups"][0]["items"] == []
    await grant(skill.name, effect="deny")
    assert lookup()["groups"][0]["items"] == []
    for patch in ({"trigger": "@"}, {"query": "method"}, {"cursor": "some-cursor"}):
        response = client.post(URL, json={**body, "trigger": "/", "exact_resource": resource, **patch}, cookies=_as(app_ctx.member))
        assert response.status_code == 422
    assert client.post(URL, json={**body, "trigger": "/", "exact_resource": resource}, cookies=_as(app_ctx.other_owner)).status_code == 404


async def test_saved_capability_parameter_lookup_preserves_exact_version(client_ctx, app_ctx):
    client, body = client_ctx
    resource = {"kind": "capability", "id": "cap_visualize", "version": 1}
    response = client.post(URL, json={**body, "trigger": "/", "exact_resource": resource, "selected_refs": [resource]}, cookies=_as(app_ctx.member))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["next_cursor"] is None
    (item,) = [item for group in result["groups"] for item in group["items"]]
    assert item["resource"] == resource
    assert item["parameter_schema"]["type"] == "object"
    changed = client.post(URL, json={**body, "trigger": "/", "exact_resource": {**resource, "version": 2}}, cookies=_as(app_ctx.member))
    assert changed.status_code == 200
    assert not any(group["items"] for group in changed.json()["groups"])
