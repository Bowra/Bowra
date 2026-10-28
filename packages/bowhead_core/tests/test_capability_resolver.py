from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import Any, cast

import pytest
from bowhead_core.capability_resolver import (
    CapabilityPrincipal,
    CapabilityResolver,
    advertised_tool_schemas,
)
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec
from bowhead_core.types import ToolAnnotations
from bowhead_router.types import ToolSchema


async def _noop(_args: dict[str, Any], _ctx: ToolExecutionContext) -> dict[str, bool]:
    return {"ok": True}


def _spec(
    name: str,
    *,
    roles: frozenset[str] = frozenset({"lead", "analyst"}),
    visible: bool = True,
    needs_approval: bool = False,
    risk_level: str = "low",
    annotations: ToolAnnotations | None = None,
) -> ToolSpec:
    return ToolSpec(
        schema=ToolSchema(
            name=name,
            description=f"Description for {name}",
            input_schema={"type": "object", "properties": {}},
        ),
        handler=_noop,
        needs_approval=needs_approval,
        risk_level=risk_level,
        allowed_for_roles=roles,
        visible_to_llm=visible,
        annotations=annotations,
    )


def _context() -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="ws_1",
        member_uid="member_1",
        user_id="user_1",
        turn_id="t_1",
        datarc=cast("Any", None),
        role="lead",
    )


class _MinimalRegistry:
    """Turn tests use a light registry without production-only grant filtering."""


@pytest.mark.asyncio
async def test_discovery_omits_tools_without_team_credentials() -> None:
    from types import SimpleNamespace

    from bowhead_core.capability_resolver import resolve_unconfigured_capability_tools

    async def missing_connection(_team_id: str):
        return None

    registry = ToolRegistry()
    registry.register(_spec("web_search"))
    registry.register(_spec("lead_read"))
    ctx = _context()
    ctx.unconfigured_capability_tools = await resolve_unconfigured_capability_tools(
        SimpleNamespace(team_search_resolver=missing_connection),
        "team_1",
    )
    async with contextlib.AsyncExitStack() as stack:
        result = await CapabilityResolver(base_registry=registry).resolve(
            stack,
            CapabilityPrincipal(user_id="user_1", team_id="team_1", workspace_id="ws_1", role="lead"),
            ctx,
            profile_whitelist=("web_search", "lead_read"),
        )
    assert [ref.id for ref in result.refs] == ["tool:lead_read"]


def test_advertised_tool_schemas_matches_lead_runtime_rules() -> None:
    base = ToolRegistry()
    base.register(_spec("lead_read"))
    base.register(_spec("analyst_only", roles=frozenset({"analyst"})))
    base.register(_spec("hidden", visible=False))
    view = base.clone_with_overlay(
        [
            _spec("teammcp.allowed"),
            _spec("teammcp.analyst_only", roles=frozenset({"analyst"})),
        ]
    )

    schemas = advertised_tool_schemas(
        view,
        ("lead_read", "analyst_only", "hidden"),
        profile_name="lead",
        role="lead",
    )

    assert [schema.name for schema in schemas] == ["lead_read", "teammcp.allowed"]


@pytest.mark.asyncio
async def test_resolver_returns_refs_from_the_same_advertised_schema_set() -> None:
    base = ToolRegistry()
    base.register(_spec("lead_read"))
    base.register(_spec("not_in_profile"))
    resolver = CapabilityResolver(base_registry=base)
    ctx = _context()

    async with contextlib.AsyncExitStack() as stack:
        resolved = await resolver.resolve(
            stack,
            CapabilityPrincipal(
                user_id="user_1",
                team_id="team_1",
                workspace_id="ws_1",
                role="lead",
            ),
            ctx,
            profile_whitelist=("lead_read",),
        )

    assert resolved.tool_view is base
    assert ctx.tool_view is None
    assert [ref.id for ref in resolved.refs] == ["tool:lead_read"]
    assert resolved.refs[0].description == "Description for lead_read"
    assert len(resolved.fingerprint) == 64


@pytest.mark.asyncio
async def test_resolver_overlays_only_the_assigned_builtin_datarc_instance() -> None:
    class Store:
        async def list_team_tool_sources(self, team_id: str, *, only_enabled: bool = False):
            assert team_id == "team_1"
            return []

        async def list_team_builtin_tools(self, team_id: str):
            assert team_id == "team_1"
            return [
                SimpleNamespace(
                    id="ibt_sales",
                    provider="datarc",
                    display_name="销售数据",
                    endpoint="https://openapi.datarc.cn",
                    status="enabled",
                    scope_state="verified",
                    scope={"permissions": ["account", "workspace", "data", "task"], "workspaces": []},
                ),
                SimpleNamespace(
                    id="ibt_disabled",
                    provider="datarc",
                    display_name="停用连接",
                    endpoint="https://openapi.datarc.cn",
                    status="disabled",
                    scope_state="unknown",
                    scope={},
                ),
            ]

    base = ToolRegistry()
    base.register(_spec("bi_query", roles=frozenset({"analyst"})))
    base.register(_spec("bi_schema", roles=frozenset({"analyst"})))
    resolver = CapabilityResolver(
        base_registry=base,
        user_store=Store(),
        tool_auth_registry=object(),
    )
    ctx = _context()
    ctx.team_id = "team_1"

    async with contextlib.AsyncExitStack() as stack:
        resolved = await resolver.resolve(
            stack,
            CapabilityPrincipal(
                user_id="user_1",
                team_id="team_1",
                workspace_id="ws_1",
                role="analyst",
            ),
            ctx,
            profile_whitelist=(),
            profile_name="business-analyst",
            team_tool_bundle=("teammcp.ibt_sales.datarc_query",),
        )

    assert resolved.tool_view is not None
    assert resolved.tool_view.overlay_names == frozenset(
        {
            "teammcp.ibt_sales.datarc_workspaces",
            "teammcp.ibt_sales.datarc_schema",
            "teammcp.ibt_sales.datarc_query",
        }
    )
    assert [ref.id for ref in resolved.refs] == ["tool:teammcp.ibt_sales.datarc_query"]
    assert ctx.team_tool_source_labels == {
        "teammcp.ibt_sales.datarc_workspaces": "Datarc - 销售数据",
        "teammcp.ibt_sales.datarc_schema": "Datarc - 销售数据",
        "teammcp.ibt_sales.datarc_query": "Datarc - 销售数据",
    }


@pytest.mark.asyncio
async def test_resolver_accepts_lightweight_runtime_registry() -> None:
    base = _MinimalRegistry()
    resolver = CapabilityResolver(base_registry=cast("ToolRegistry", base))
    ctx = _context()

    async with contextlib.AsyncExitStack() as stack:
        resolved = await resolver.resolve(
            stack,
            CapabilityPrincipal(
                user_id="user_1",
                team_id="",
                workspace_id="ws_1",
                role="lead",
            ),
            ctx,
        )

    assert resolved.tool_view is base
    assert ctx.tool_view is None


@pytest.mark.asyncio
async def test_resolver_does_not_treat_risk_level_as_write_semantics() -> None:
    base = ToolRegistry()
    base.register(
        _spec(
            "medium_read",
            risk_level="medium",
            annotations=ToolAnnotations(read_only=True),
        )
    )
    base.register(_spec("approval_write", needs_approval=True))
    resolver = CapabilityResolver(base_registry=base)

    async with contextlib.AsyncExitStack() as stack:
        resolved = await resolver.resolve(
            stack,
            CapabilityPrincipal(
                user_id="user_1",
                team_id="team_1",
                workspace_id="ws_1",
                role="lead",
            ),
            _context(),
            profile_whitelist=("medium_read", "approval_write"),
        )

    refs = {ref.id: ref for ref in resolved.refs}
    assert refs["tool:medium_read"].operations == frozenset({"read", "execute"})
    assert refs["tool:approval_write"].operations == frozenset({"execute", "update"})


@pytest.mark.asyncio
async def test_read_only_principal_excludes_write_capability_refs() -> None:
    base = ToolRegistry()
    base.register(_spec("lead_read", annotations=ToolAnnotations(read_only=True)))
    base.register(_spec("approval_write", needs_approval=True))
    resolver = CapabilityResolver(base_registry=base)

    async with contextlib.AsyncExitStack() as stack:
        resolved = await resolver.resolve(
            stack,
            CapabilityPrincipal(
                user_id="user_1",
                team_id="team_1",
                workspace_id="ws_1",
                role="lead",
                read_only=True,
            ),
            _context(),
            profile_whitelist=("lead_read", "approval_write"),
        )

    assert [ref.id for ref in resolved.refs] == ["tool:lead_read"]


@pytest.mark.asyncio
async def test_resolver_resource_grants_fail_closed_without_access_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    base = ToolRegistry()
    base.register(_spec("lead_read"))
    access_store = object()
    resolver = CapabilityResolver(
        base_registry=base,
        access_control_store=access_store,
    )
    ctx = _context()
    ctx.access_control_store = access_store
    ctx.access_context = None

    async with contextlib.AsyncExitStack() as stack:
        resolved = await resolver.resolve(
            stack,
            CapabilityPrincipal(
                user_id="user_1",
                team_id="",
                workspace_id="ws_1",
                role="lead",
            ),
            ctx,
            profile_whitelist=("lead_read",),
        )

    assert resolved.tool_view is not None
    assert resolved.tool_view.list_names() == []
    assert resolved.refs == ()


@pytest.mark.asyncio
async def test_resolver_resource_grants_shadow_keeps_and_enforce_hides_only_denied_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = ToolRegistry()
    base.register(_spec("web_search"))
    base.register(_spec("lead_read"))

    class AccessStore:
        async def explain_decision(
            self,
            _access_context: object,
            _resource_kind: str,
            resource_id: str,
            _permission: str,
        ) -> dict[str, object]:
            return {"allowed": resource_id != "web_search"}

    access_store = AccessStore()
    resolver = CapabilityResolver(base_registry=base)

    async def resolve(enforce: bool) -> set[str]:
        monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1" if enforce else "0")
        ctx = _context()
        ctx.access_control_store = cast("Any", access_store)
        ctx.access_context = cast("Any", {"user_id": "user_1"})
        async with contextlib.AsyncExitStack() as stack:
            result = await resolver.resolve(
                stack,
                CapabilityPrincipal(
                    user_id="user_1",
                    team_id="",
                    workspace_id="ws_1",
                    role="lead",
                ),
                ctx,
                profile_whitelist=("web_search", "lead_read"),
            )
        return {ref.id for ref in result.refs}

    assert await resolve(enforce=False) == {"tool:web_search", "tool:lead_read"}
    assert await resolve(enforce=True) == {"tool:lead_read"}


def test_drop_unconfigured_removes_from_advertised(tool_registry_with_bi=None) -> None:
    """credential-readiness R6：drop_unconfigured 从广告面剔除未配置能力工具;
    默认空集 = 字节不变(既有全部调用)。"""
    from bowhead_core.capability_resolver import advertised_tool_schemas
    from bowhead_core.tool_registry import ToolRegistry, ToolSchema, ToolSpec

    reg = ToolRegistry()

    async def _noop(args, ctx):  # type: ignore[no-untyped-def]
        return {}

    for name in ("web_search", "kb_search"):
        reg.register(ToolSpec(schema=ToolSchema(name=name, description="d", input_schema={"type": "object", "properties": {}}), handler=_noop))

    base = advertised_tool_schemas(reg, ("web_search", "kb_search"), profile_name="compute", role=None)
    assert {s.name for s in base} == {"web_search", "kb_search"}

    dropped = advertised_tool_schemas(
        reg,
        ("web_search", "kb_search"),
        profile_name="compute",
        role=None,
        drop_unconfigured=frozenset({"web_search"}),
    )
    assert {s.name for s in dropped} == {"kb_search"}


class _SpecStub:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key


class _RegStub:
    def __init__(self, spec) -> None:  # type: ignore[no-untyped-def]
        self._spec = spec

    def get_active(self, capability: str):  # type: ignore[no-untyped-def]
        return self._spec


async def test_resolve_unconfigured_capability_tools_matrix() -> None:
    """credential-readiness R6：解析矩阵——配了→不进集合;无 key/无 spec→进;
    resolver 缺失/异常→fail-open 不进;无 team→空集。"""
    from types import SimpleNamespace

    from bowhead_core.turn_runner import _resolve_unconfigured_capability_tools

    async def _ok(team_id: str):  # type: ignore[no-untyped-def]
        return _RegStub(_SpecStub("sk-live"))

    async def _keyless(team_id: str):  # type: ignore[no-untyped-def]
        return _RegStub(_SpecStub(""))

    async def _none_spec(team_id: str):  # type: ignore[no-untyped-def]
        return _RegStub(None)

    async def _boom(team_id: str):  # type: ignore[no-untyped-def]
        raise RuntimeError("resolver down")

    from bowhead_core.google_delivery_tool import TOOL_NAMES as google_delivery

    # Google delivery tools also count as unconfigured when team connection is disabled (feature flag off here), and are not advertised.
    google = frozenset(google_delivery)
    req = SimpleNamespace(team_id="t1", config_lite_supported=True)

    # Configured → only disabled Google delivery remains.
    orch = SimpleNamespace(team_search_resolver=_ok, team_media_resolver=_ok)
    assert await _resolve_unconfigured_capability_tools(orch, req) == google

    # search keyless + media without spec → both included.
    orch = SimpleNamespace(team_search_resolver=_keyless, team_media_resolver=_none_spec)
    assert await _resolve_unconfigured_capability_tools(orch, req) == frozenset({"web_search", "image_generate"}) | google

    # Resolver exception → fail-open (do not hide).
    orch = SimpleNamespace(team_search_resolver=_boom, team_media_resolver=_ok)
    assert await _resolve_unconfigured_capability_tools(orch, req) == google

    # Missing/unmounted resolver → only Google delivery remains.
    orch = SimpleNamespace()
    assert await _resolve_unconfigured_capability_tools(orch, req) == google

    # No team_id → empty set.
    orch = SimpleNamespace(team_search_resolver=_keyless, team_media_resolver=_keyless)
    assert await _resolve_unconfigured_capability_tools(orch, SimpleNamespace(team_id="", config_lite_supported=True)) == google
