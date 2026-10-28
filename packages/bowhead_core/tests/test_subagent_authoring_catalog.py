from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.openapi_tool_source import TeamToolEntry
from bowhead_core.subagent_authoring_catalog import (
    CatalogAccessDenied,
    SubagentAuthoringCatalogService,
    classify_mcp_risk,
    classify_openapi_risk,
)


def test_openapi_risk_comes_from_http_method() -> None:
    assert classify_openapi_risk("GET") == ("read", "openapi_method")
    assert classify_openapi_risk("post") == ("external_write", "openapi_method")
    assert classify_openapi_risk("DELETE") == ("destructive", "openapi_method")
    assert classify_openapi_risk("TRACE") == ("unknown", "unknown_default")


def test_mcp_risk_requires_non_conflicting_explicit_annotations() -> None:
    assert classify_mcp_risk(read_only=True, destructive=False) == (
        "read",
        "mcp_annotation",
    )
    assert classify_mcp_risk(read_only=False, destructive=True) == (
        "destructive",
        "mcp_annotation",
    )
    assert classify_mcp_risk(read_only=True, destructive=True) == (
        "unknown",
        "unknown_default",
    )
    assert classify_mcp_risk(read_only=None, destructive=False) == (
        "unknown",
        "unknown_default",
    )


@pytest.mark.asyncio
async def test_catalog_is_actor_scoped_locked_without_mcp_entitlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = SimpleNamespace(
        id="src_1",
        name="CRM\nsecret",
        updated_at="2026-07-31T00:00:00+00:00",
    )
    entries = [
        TeamToolEntry(
            source_id="src_1",
            advertised_name="teammcp.src_1.lookup",
            dispatch_name="lookup",
            description="Lookup customer",
            input_schema={"type": "object"},
            destructive=False,
            kind="mcp",
            read_only_hint=True,
            destructive_hint=False,
        ),
    ]

    async def _load(team_id: str, store: object):
        assert team_id == "team_a"
        return entries, {"src_1": source}

    monkeypatch.setattr(
        "bowhead_core.subagent_authoring_catalog.load_team_tool_entries",
        _load,
    )
    snapshot = await SubagentAuthoringCatalogService().build(
        store=object(),
        team_id="team_a",
        actor_user_id="user_a",
        actor_active=True,
        mcp_entitled=False,
    )

    tool = next(t for t in snapshot.team_tools if t.stable_ref.endswith(".lookup"))
    assert tool.risk == "read"
    assert tool.discoverable is True
    assert tool.selectable is False
    assert tool.locked_reason == "mcp_feature_unlicensed"
    assert tool.source_display_name == "CRM secret"
    assert "https://" not in (tool.confirmation_summary or "")
    assert snapshot.fingerprint.startswith("sha256:")


@pytest.mark.asyncio
async def test_catalog_includes_each_enabled_datarc_instance_without_mcp_license(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _load(team_id: str, store: object):
        assert team_id == "team_a"
        return [], {}

    class Store:
        async def list_team_builtin_tools(self, team_id: str):
            assert team_id == "team_a"
            return [
                SimpleNamespace(
                    id="ibt_sales",
                    provider="datarc",
                    display_name="销售数据",
                    status="enabled",
                    updated_at="2026-09-23T00:00:00+00:00",
                    scope_state="verified",
                    scope={"permissions": ["account", "workspace", "data", "task"], "workspaces": []},
                ),
                SimpleNamespace(
                    id="ibt_disabled",
                    provider="datarc",
                    display_name="停用连接",
                    status="disabled",
                    updated_at="2026-09-23T00:00:00+00:00",
                    scope_state="unknown",
                    scope={},
                ),
            ]

    monkeypatch.setattr(
        "bowhead_core.subagent_authoring_catalog.load_team_tool_entries",
        _load,
    )
    snapshot = await SubagentAuthoringCatalogService().build(
        store=Store(),
        team_id="team_a",
        actor_user_id="user_a",
        actor_active=True,
        mcp_entitled=False,
    )

    assert [(tool.stable_ref, tool.source_display_name) for tool in snapshot.team_tools] == [
        ("teammcp.ibt_sales.datarc_workspaces", "Datarc - 销售数据"),
        ("teammcp.ibt_sales.datarc_schema", "Datarc - 销售数据"),
        ("teammcp.ibt_sales.datarc_query", "Datarc - 销售数据"),
    ]
    assert snapshot.team_tools[0].selectable is True
    assert snapshot.team_tools[0].locked_reason is None
    assert snapshot.team_tools[0].to_public_dict()["connection_id"] == "ibt_sales"
    assert "source_id" not in snapshot.team_tools[0].to_public_dict()


@pytest.mark.asyncio
async def test_native_tools_never_show_the_raw_tool_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """勾选列表里的内置工具必须有人话名字和说明，中英都要。

    capability-surface-audit 2026-09-06 实测：28 项里 `display_name` 100% 是工具代号
    （`canvas_add_spec_block`），`description` 19 项也是代号 —— 因为描述只从 BI
    registrar 的 schema 取，canvas / skill / web / python 由别的 registrar 注册。
    剩下 9 项显示的是喂给模型的 prompt 原文。客户在唯一能看到内置能力的地方，
    读到的就是这些。

    突变验证：把 `_native_display` 改回 `return tool_ref, tool_ref, ...` → 本测试红。
    """

    async def _load(team_id: str, store: object):
        return [], {}

    monkeypatch.setattr(
        "bowhead_core.subagent_authoring_catalog.load_team_tool_entries",
        _load,
    )
    snapshot = await SubagentAuthoringCatalogService().build(
        store=object(),
        team_id="team_a",
        actor_user_id="user_a",
        actor_active=True,
        mcp_entitled=False,
    )

    assert snapshot.native_tools, "内置工具目录不能是空的，否则本测试变成空转"
    bare = [t.stable_ref for t in snapshot.native_tools if t.display_name == t.stable_ref]
    assert bare == [], f"这些工具的名字还是代号: {bare}"
    bare_desc = [t.stable_ref for t in snapshot.native_tools if t.description in ("", t.stable_ref)]
    assert bare_desc == [], f"这些工具没有说明: {bare_desc}"
    missing_en = [t.stable_ref for t in snapshot.native_tools if not t.display_name_en or not t.description_en]
    assert missing_en == [], f"这些工具缺英文: {missing_en}"
    query = next(tool for tool in snapshot.native_tools if tool.stable_ref == "bi_query")
    public_query = query.to_public_dict()
    assert public_query["display_name_de"] == "Daten abfragen"
    assert public_query["display_name_fr"] == "Interroger les données"
    assert public_query["description_de"].startswith("Daten aus der verbundenen Datenquelle")
    assert public_query["description_fr"].startswith("Récupérer les données de la source connectée")

    # Customer-facing copy, not model-facing: no internal protocol terms.
    leaked = [
        t.stable_ref
        for t in snapshot.native_tools
        if any(token in t.description for token in ("List[str]", "artifact_id", "_ref_", "schema=", "json"))
    ]
    assert leaked == [], f"这些说明里漏了内部协议词: {leaked}"

    # Identical names and descriptions leave customers unable to choose. Photographed in 116 dogfood on 2026-09-07:
    # python_exec and python_sandbox were such a pair (two invocation shapes for the same sandbox).
    by_name: dict[str, list[str]] = {}
    for t in snapshot.native_tools:
        by_name.setdefault((t.display_name, t.description), []).append(t.stable_ref)  # type: ignore[arg-type]
    dupes = {k: v for k, v in by_name.items() if len(v) > 1}
    assert dupes == {}, f"勾选列表里有同名同说明的重复条目，客户分不出: {dupes}"


@pytest.mark.asyncio
async def test_catalog_rejects_unverified_actor() -> None:
    with pytest.raises(CatalogAccessDenied):
        await SubagentAuthoringCatalogService().build(
            store=object(),
            team_id="team_a",
            actor_user_id="user_a",
            actor_active=False,
            mcp_entitled=True,
        )


@pytest.mark.asyncio
async def test_approval_picker_projection_does_not_block_existing_dependency_compilation(monkeypatch):
    from bowhead_core.agent_dependency_compiler import AgentDependencyCompiler, AgentDependencyContext

    from bowhead_core import approval_gate

    ref = "teammcp.crm.update"
    entry = TeamToolEntry(
        source_id="crm",
        advertised_name=ref,
        dispatch_name="update",
        description="Update customer",
        input_schema={"type": "object"},
        destructive=True,
        kind="mcp",
        read_only_hint=False,
        destructive_hint=True,
    )

    async def load(_team_id, _store):
        return [entry], {"crm": SimpleNamespace(name="CRM", updated_at="v1")}

    monkeypatch.setattr("bowhead_core.subagent_authoring_catalog.load_team_tool_entries", load)
    monkeypatch.setattr(approval_gate, "SUPPORTED_NATIVE_WRITE_TOOLS", frozenset({ref}))
    context = AgentDependencyContext(agent_id="agent", team_id="team_a", owner_user_id="user_a", audience_mode="private")
    compiled = []
    fingerprints = []
    for enabled in ("1", "0", "1"):
        monkeypatch.setenv("BOWHEAD_APPROVAL_WRITE_ENABLED", enabled)
        catalog = await SubagentAuthoringCatalogService().build(
            store=object(),
            team_id="team_a",
            actor_user_id="user_a",
            actor_active=True,
            mcp_entitled=True,
        )
        public = catalog.team_tools[0].to_public_dict()
        assert public["selectable"] is (enabled == "1")
        assert public["locked_reason"] == (None if enabled == "1" else "approval_executor_unavailable")
        compiled.append(await AgentDependencyCompiler().compile({"tool_refs": [ref]}, context=context, tool_catalog=catalog))
        fingerprints.append(catalog.fingerprint)
    assert [item.status for item in compiled] == ["ready"] * 3
    assert len({item.dependency_fingerprint for item in compiled}) == 1
    assert len({item.capability_fingerprint for item in compiled}) == 1
    assert fingerprints[0] == fingerprints[2] != fingerprints[1]
    monkeypatch.setattr(approval_gate, "SUPPORTED_NATIVE_WRITE_TOOLS", frozenset())
    assert catalog.team_tools[0].to_public_dict()["selectable"] is False
    unlicensed = await SubagentAuthoringCatalogService().build(
        store=object(),
        team_id="team_a",
        actor_user_id="user_a",
        actor_active=True,
        mcp_entitled=False,
    )
    assert unlicensed.team_tools[0].to_public_dict()["locked_reason"] == "mcp_feature_unlicensed"


def test_report_tools_are_offerable_but_legacy_canvas_writes_are_not(monkeypatch) -> None:
    """两支报告工具都可授权，四个 legacy canvas 写工具仍不可——正反一起断言。

    时间线（都在 2026-09-09）：本测试最初写成「读可授权 / 写仍被挡」，因为当时
    `write_report` 还在 `REPORT_WRITE_TOOLS` 围栏内；同日用户拍板放开写（tech-design:
    docs/sprints/custom-agent-report-pen-2026-09-09/），本测试**如设计那样当场红**，
    这里按新属性重写。仍守的边界是留栏的四个——它们没有 per-section owner 模型。
    """
    from bowhead_core.custom_subagent_store import REPORT_WRITE_TOOLS, filter_destructive
    from bowhead_core.subagent_authoring_catalog import NATIVE_ALLOWLIST, classify_native_risk

    assert "read_report" in NATIVE_ALLOWLIST
    assert classify_native_risk("read_report") == ("read", "native_registry")
    assert "write_report" in NATIVE_ALLOWLIST
    assert classify_native_risk("write_report") == ("artifact_write", "native_registry")
    assert filter_destructive(["read_report", "write_report"]) == ["read_report", "write_report"]

    # Negative control: tools without ownership models (including batch B2-2 remove_chart) still cannot be
    # granted by the current catalog.
    legacy = ["canvas_add_section", "canvas_compose_final", "canvas_patch_section", "canvas_reorder_sections", "remove_chart"]
    assert set(legacy) == set(REPORT_WRITE_TOOLS)
    assert filter_destructive(legacy) == ["remove_chart"]
    for name in legacy:
        assert name not in NATIVE_ALLOWLIST, name
    # remove_chart is offered and kept (user decision 2026-10-08); retired tools stay fenced.
    from bowhead_core.subagent_authoring_catalog import native_authoring_tools

    assert filter_destructive(legacy) == ["remove_chart"]
    offered = {tool.stable_ref: tool for tool in native_authoring_tools()}
    assert offered["remove_chart"].selectable and offered["remove_chart"].risk == "destructive"
    assert offered["tool_list_workspaces"].selectable and offered["tool_list_workspaces"].risk == "read"
    assert not ({"canvas_add_section", "canvas_patch_section"} & set(offered))


def test_builtin_catalog_defaults_and_explicit_chinese_names():
    from bowhead_core.builtin_tool_providers import providers
    from bowhead_core.subagent_authoring_catalog import native_authoring_tools

    tools = native_authoring_tools()
    assert tools
    for tool in tools:
        assert tool.display_name == tool.display_name_en
        assert not any("\u4e00" <= char <= "\u9fff" for char in tool.display_name)
        assert tool.display_name_zh
    for provider in providers():
        for operation in provider.catalog.values():
            assert operation.display_name
            assert not any("\u4e00" <= char <= "\u9fff" for char in operation.display_name)
            assert operation.display_name_zh


def test_draft_tool_names_keep_explicit_chinese_selection():
    from bowhead_core.subagent_authoring_catalog import native_authoring_tools
    from bowhead_core.subagent_draft_service import _tool_names

    tools = {tool.stable_ref: tool for tool in native_authoring_tools()}
    tool = tools["bi_query"]
    catalog = SimpleNamespace(tools_by_ref=tools)
    assert _tool_names(("bi_query",), catalog, is_zh=False) == tool.display_name
    assert _tool_names(("bi_query",), catalog, is_zh=True) == tool.display_name_zh


def test_catalog_version_label_follows_the_opened_tools(monkeypatch) -> None:
    from bowhead_core.subagent_authoring_catalog import NATIVE_ALLOWLIST_VERSION, native_allowlist_version

    assert NATIVE_ALLOWLIST_VERSION == "native-authoring-v2"
    assert native_allowlist_version() == "native-authoring-v3"


def test_memory_tools_are_not_offered_for_agent_authoring() -> None:
    """remember/recall are Lead built-ins listed in tool_aliases.yaml for display names only."""
    from bowhead_core.agents._generated.role_bindings import _PARADIGM_V2_ROLE_MAP
    from bowhead_core.subagent_authoring_catalog import NATIVE_ALLOWLIST

    for name in ("remember", "recall"):
        assert "analyst" in _PARADIGM_V2_ROLE_MAP[name]  # would otherwise qualify
        assert name not in NATIVE_ALLOWLIST


@pytest.mark.asyncio
async def test_shopify_content_write_is_cataloged_as_external_with_requester_review(monkeypatch):
    monkeypatch.setenv("FEATURE_SHOPIFY_PLUGIN", "1")
    monkeypatch.setenv("FEATURE_SHOPIFY_CONTENT_WRITES", "1")

    async def load(_team_id, _store):
        return [], {}

    class Store:
        async def list_team_builtin_tools(self, _team_id):
            return [
                SimpleNamespace(
                    id="ibt_shop",
                    provider="shopify",
                    display_name="Shop",
                    status="enabled",
                    config={"granted_scopes": ["read_products", "write_products"]},
                )
            ]

    monkeypatch.setattr("bowhead_core.subagent_authoring_catalog.load_team_tool_entries", load)
    snapshot = await SubagentAuthoringCatalogService().build(
        store=Store(), team_id="team_a", actor_user_id="user_a", actor_active=True, mcp_entitled=False
    )
    writes = [item for item in snapshot.team_tools if item.risk != "read"]
    assert len(writes) == 1
    assert writes[0].stable_ref == "teammcp.ibt_shop.shopify_apply_content_update"
    assert writes[0].risk == "external_write"
    assert writes[0].selectable and not writes[0].approval_required
    assert writes[0].confirmation_summary


def test_platform_only_skills_stay_out_of_owner_pickers() -> None:
    from bowhead_core.subagent_authoring_catalog import NATIVE_ALLOWLIST, is_platform_only_skill

    assert is_platform_only_skill("settings-space-metadata")
    assert is_platform_only_skill("agent-authoring") and is_platform_only_skill("analyst-sop-output-contract")
    assert not is_platform_only_skill("python_sandbox") and not is_platform_only_skill("bowra-docx")
    # Platform and Coach-only Tools are not Owner choices either.
    assert not NATIVE_ALLOWLIST & {"agent_working_copy_open", "configuration_propose", "skill_save_generated"}
