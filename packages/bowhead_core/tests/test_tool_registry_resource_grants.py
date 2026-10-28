from __future__ import annotations

import aiosqlite
import pytest
from bowhead_core.access_control import AccessContext, AccessControlStore
from bowhead_core.tool_registry import (
    ToolExecutionContext,
    ToolRegistry,
    ToolSpec,
    _kb_search_scope_filter_for_grants,
)
from bowhead_core.user_store import UserStore
from bowhead_router.types import ToolSchema


async def _ok_handler(args, ctx):
    return {"ok": True, "args": args}


class _FailingAccessStore:
    async def explain_decision(self, *args, **kwargs):
        raise RuntimeError("grant store unavailable")


def _ctx(access: AccessControlStore) -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="ws_sales",
        member_uid="u_alice",
        user_id="u_alice",
        turn_id="t1",
        datarc=object(),
        access_context=AccessContext(
            user_id="u_alice",
            team_id="team_acme",
            workspace_id="ws_sales",
            workspace_role="member",
            team_role=None,
            group_ids=(),
            policy_epoch=1,
            is_team_admin=False,
        ).__dict__,
        access_control_store=access,
    )


def _ctx_without_access_context(access: AccessControlStore) -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="ws_sales",
        member_uid="u_alice",
        user_id="u_alice",
        turn_id="t1",
        datarc=object(),
        access_context=None,
        access_control_store=access,
    )


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="skill_read",
                description="Read skill",
                input_schema={"type": "object", "properties": {}},
            ),
            handler=_ok_handler,
        ),
    )
    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="skill_invoke",
                description="Run skill",
                input_schema={"type": "object", "properties": {}},
            ),
            handler=_ok_handler,
        ),
    )
    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="skill_save_generated",
                description="Save generated skill",
                input_schema={"type": "object", "properties": {}},
            ),
            handler=_ok_handler,
        ),
    )
    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="mcp.salesforce.query",
                description="Run MCP tool",
                input_schema={"type": "object", "properties": {}},
            ),
            handler=_ok_handler,
        ),
    )
    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="kb_search",
                description="Search KB",
                input_schema={"type": "object", "properties": {}},
            ),
            handler=_ok_handler,
        ),
    )
    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="web_search",
                description="Search web",
                input_schema={"type": "object", "properties": {}},
            ),
            handler=_ok_handler,
        ),
    )
    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="canvas_add_section",
                description="Add section",
                input_schema={"type": "object", "properties": {}},
            ),
            handler=_ok_handler,
        ),
    )
    return registry


@pytest.mark.asyncio
async def test_resource_grants_shadow_does_not_block_skill(tmp_path, monkeypatch):
    monkeypatch.delenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", raising=False)
    access = AccessControlStore(tmp_path / "access.db")
    await access.initialize()
    await access.create_or_update_resource_grant(
        team_id="team_acme",
        resource_kind="skill",
        resource_id="forecast",
        principal_type="user",
        principal_id="u_alice",
        permission="use",
        effect="deny",
        created_by="u_admin",
    )

    invocation = await _registry().execute(
        "skill_invoke",
        {"name": "forecast"},
        _ctx(access),
    )

    assert invocation.error is None
    assert invocation.result == {"ok": True, "args": {"name": "forecast"}}


@pytest.mark.asyncio
async def test_resource_grants_enforce_blocks_skill(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    access = AccessControlStore(tmp_path / "access.db")
    await access.initialize()
    await access.create_or_update_resource_grant(
        team_id="team_acme",
        resource_kind="skill",
        resource_id="forecast",
        principal_type="user",
        principal_id="u_alice",
        permission="use",
        effect="deny",
        created_by="u_admin",
    )

    invocation = await _registry().execute(
        "skill_invoke",
        {"name": "forecast"},
        _ctx(access),
    )

    assert invocation.result is None
    assert invocation.error is not None
    assert "resource grant denied" in invocation.error
    assert "skill:forecast" in invocation.error


@pytest.mark.asyncio
async def test_legacy_skill_use_allow_is_read_only_at_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    access = AccessControlStore(tmp_path / "access.db")
    await access.initialize()
    await access.create_or_update_resource_grant(
        team_id="team_acme",
        resource_kind="skill",
        resource_id="forecast",
        principal_type="user",
        principal_id="u_alice",
        permission="use",
        effect="allow",
        created_by="u_admin",
    )

    read = await _registry().execute("skill_read", {"name": "forecast"}, _ctx(access))
    invoke = await _registry().execute("skill_invoke", {"name": "forecast"}, _ctx(access))

    assert read.error is None
    assert invoke.error is not None
    assert "permission=invoke" in invoke.error


@pytest.mark.asyncio
async def test_skill_invoke_ticket_allows_runtime_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    access = AccessControlStore(tmp_path / "access.db")
    await access.initialize()
    await access.create_or_update_resource_grant(
        team_id="team_acme",
        resource_kind="skill",
        resource_id="forecast",
        principal_type="user",
        principal_id="u_alice",
        permission="invoke",
        effect="allow",
        created_by="u_admin",
    )

    invocation = await _registry().execute(
        "skill_invoke",
        {"name": "forecast"},
        _ctx(access),
    )

    assert invocation.error is None
    assert invocation.result == {"ok": True, "args": {"name": "forecast"}}


@pytest.mark.asyncio
async def test_legacy_skill_use_deny_still_blocks_read_and_invoke(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    access = AccessControlStore(tmp_path / "access.db")
    await access.initialize()
    await access.create_or_update_resource_grant(
        team_id="team_acme",
        resource_kind="skill",
        resource_id="forecast",
        principal_type="user",
        principal_id="u_alice",
        permission="use",
        effect="deny",
        created_by="u_admin",
    )

    read = await _registry().execute("skill_read", {"name": "forecast"}, _ctx(access))
    invoke = await _registry().execute("skill_invoke", {"name": "forecast"}, _ctx(access))

    assert read.error is not None and "permission=read" in read.error
    assert invoke.error is not None and "permission=invoke" in invoke.error


@pytest.mark.asyncio
async def test_resource_grants_enforce_filters_kb_search_to_allowed_scopes(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    access = AccessControlStore(tmp_path / "access.db")
    await access.initialize()
    await access.create_or_update_resource_grant(
        team_id="team_acme",
        resource_kind="kb_scope",
        resource_id="team:team_acme",
        principal_type="user",
        principal_id="u_alice",
        permission="read",
        effect="deny",
        created_by="u_admin",
    )

    scope_filter = await _kb_search_scope_filter_for_grants(_ctx(access))
    assert scope_filter == ["personal", "space"]  # Batch 3/c1 canonical

    invocation = await _registry().execute(
        "kb_search",
        {"query": "pipeline"},
        _ctx(access),
    )

    assert invocation.error is None
    assert invocation.result == {"ok": True, "args": {"query": "pipeline"}}


@pytest.mark.asyncio
async def test_resource_grants_enforce_kb_search_no_readable_scope_returns_empty_filter(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    access = AccessControlStore(tmp_path / "access.db")
    await access.initialize()
    for resource_id in ("workspace:ws_sales", "team:team_acme"):
        await access.create_or_update_resource_grant(
            team_id="team_acme",
            resource_kind="kb_scope",
            resource_id=resource_id,
            principal_type="user",
            principal_id="u_alice",
            permission="read",
            effect="deny",
            created_by="u_admin",
        )

    scope_filter = await _kb_search_scope_filter_for_grants(_ctx(access))

    assert scope_filter == []


@pytest.mark.asyncio
async def test_resource_grants_enforce_blocks_generic_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    access = AccessControlStore(tmp_path / "access.db")
    await access.initialize()
    await access.create_or_update_resource_grant(
        team_id="team_acme",
        resource_kind="tool",
        resource_id="canvas_add_section",
        principal_type="user",
        principal_id="u_alice",
        permission="use",
        effect="deny",
        created_by="u_admin",
    )

    invocation = await _registry().execute(
        "canvas_add_section",
        {"title": "QBR"},
        _ctx(access),
    )

    assert invocation.result is None
    assert invocation.error is not None
    assert "tool:canvas_add_section" in invocation.error


@pytest.mark.asyncio
async def test_resource_grants_enforce_blocks_builtin_capability_and_hides_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    access = AccessControlStore(tmp_path / "access.db")
    await access.initialize()
    await access.create_or_update_resource_grant(
        team_id="team_acme",
        resource_kind="builtin_capability",
        resource_id="web_search",
        principal_type="user",
        principal_id="u_alice",
        permission="use",
        effect="deny",
        created_by="u_admin",
    )
    registry = _registry()
    ctx = _ctx(access)

    filtered = await registry.clone_filtered_by_resource_grants(ctx)
    assert "web_search" not in [schema.name for schema in filtered.to_schemas()]

    invocation = await registry.execute("web_search", {"query": "latest"}, ctx)
    assert invocation.result is None
    assert invocation.error is not None
    assert "builtin_capability:web_search" in invocation.error


@pytest.mark.asyncio
async def test_skill_save_generated_requires_write_capability(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    access = AccessControlStore(tmp_path / "access.db")
    await access.initialize()
    await access.create_or_update_resource_grant(
        team_id="team_acme",
        resource_kind="builtin_capability",
        resource_id="skill_invoke",
        principal_type="user",
        principal_id="u_alice",
        permission="use",
        effect="allow",
        created_by="u_admin",
    )
    registry = _registry()
    ctx = _ctx(access)

    filtered = await registry.clone_filtered_by_resource_grants(ctx)
    schema_names = [schema.name for schema in filtered.to_schemas()]
    assert "skill_invoke" in schema_names
    assert "skill_save_generated" not in schema_names

    invocation = await registry.execute("skill_save_generated", {"body_md": "# Skill"}, ctx)
    assert invocation.result is None
    assert invocation.error is not None
    assert "builtin_capability:skill_invoke" in invocation.error
    assert "permission=write" in invocation.error

    await access.create_or_update_resource_grant(
        team_id="team_acme",
        resource_kind="builtin_capability",
        resource_id="skill_invoke",
        principal_type="user",
        principal_id="u_alice",
        permission="write",
        effect="allow",
        created_by="u_admin",
    )

    allowed = await registry.execute("skill_save_generated", {"body_md": "# Skill"}, ctx)
    assert allowed.error is None
    assert allowed.result == {"ok": True, "args": {"body_md": "# Skill"}}


@pytest.mark.asyncio
async def test_resource_grants_enforce_missing_access_context_blocks_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    access = AccessControlStore(tmp_path / "access.db")
    await access.initialize()
    registry = _registry()
    ctx = _ctx_without_access_context(access)

    filtered = await registry.clone_filtered_by_resource_grants(ctx)
    assert filtered.to_schemas() == []

    invocation = await registry.execute("web_search", {"query": "latest"}, ctx)
    assert invocation.result is None
    assert invocation.error is not None
    assert "missing_access_context" in invocation.error


@pytest.mark.asyncio
async def test_resource_grants_enforce_fails_closed_when_store_errors(monkeypatch):
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    registry = _registry()
    ctx = _ctx(_FailingAccessStore())  # type: ignore[arg-type]

    filtered = await registry.clone_filtered_by_resource_grants(ctx)
    assert filtered.to_schemas() == []

    invocation = await registry.execute("web_search", {"query": "latest"}, ctx)
    assert invocation.result is None
    assert invocation.error is not None
    assert "access_check_error" in invocation.error


@pytest.mark.asyncio
async def test_python_sandbox_capability_does_not_block_all_skill_invoke(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    access = AccessControlStore(tmp_path / "access.db")
    await access.initialize()
    await access.create_or_update_resource_grant(
        team_id="team_acme",
        resource_kind="builtin_capability",
        resource_id="python_sandbox",
        principal_type="user",
        principal_id="u_alice",
        permission="use",
        effect="deny",
        created_by="u_admin",
    )
    registry = _registry()
    ctx = _ctx(access)

    filtered = await registry.clone_filtered_by_resource_grants(ctx)
    assert "skill_invoke" in [schema.name for schema in filtered.to_schemas()]

    ok = await registry.execute("skill_invoke", {"name": "forecast"}, ctx)
    assert ok.error is None

    denied = await registry.execute("skill_invoke", {"name": "python_sandbox"}, ctx)
    assert denied.result is None
    assert denied.error is not None
    assert "builtin_capability:python_sandbox" in denied.error


@pytest.mark.asyncio
async def test_resource_grants_runtime_deny_records_admin_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    db = tmp_path / "access.db"
    users = UserStore(db)
    await users.initialize()
    access = AccessControlStore(db)
    await access.initialize()
    await access.create_or_update_resource_grant(
        team_id="team_acme",
        resource_kind="builtin_capability",
        resource_id="web_search",
        principal_type="user",
        principal_id="u_alice",
        permission="use",
        effect="deny",
        created_by="u_admin",
    )

    invocation = await _registry().execute("web_search", {"query": "latest"}, _ctx(access))
    assert invocation.error is not None

    async with (
        aiosqlite.connect(db) as conn,
        conn.execute(
            "SELECT action, target_kind, target_id, payload_json FROM admin_audit_log WHERE action='resource_access_denied'",
        ) as cur,
    ):
        row = await cur.fetchone()
    assert row is not None
    assert row[1] == "tool"
    assert row[2] == "web_search"
    assert "builtin_capability" in row[3]
