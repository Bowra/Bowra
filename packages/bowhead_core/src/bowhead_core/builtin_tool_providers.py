"""Registry of image-bundled third-party tool providers that expose agent tools.

One place answers, per provider: which configured connections are active this turn,
which operations each exposes, how to build the specs, how to label them for the
Lead's ``<team_tools>`` block, and how the authoring catalog describes them.
Consumers (``capability_resolver`` overlay, ``subagent_authoring_catalog``) iterate
this registry instead of branching on provider names.

Tavily is absent on purpose: it backs native tools (``web_search``, and ``web_extract`` / ``web_map`` in
``web_tavily_tools``) and exposes no per-connection tool.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CatalogOperation:
    display_name: str
    description: str
    capability_classes: frozenset[str]
    # English is the default; explicit translations are presentation metadata.
    display_name_zh: str = ""
    description_zh: str = ""
    display_name_en: str = ""
    description_en: str = ""
    display_name_de: str = ""
    description_de: str = ""
    display_name_fr: str = ""
    description_fr: str = ""
    risk: str = "read"


@dataclass(frozen=True)
class BuiltinToolProvider:
    name: str
    # Operations this connection exposes (empty = inactive) — config, not per-member state.
    operations: Callable[[Any], tuple[str, ...]]
    tool_ref: Callable[[str, str], str]
    source_label: Callable[[str], str]
    build_specs: Callable[..., list[Any]]
    catalog: dict[str, CatalogOperation]
    dependency_revision: str
    # Whether this turn may carry the provider's tools at all (e.g. attended runs only).
    turn_allows: Callable[[Any], bool]


def _datarc() -> BuiltinToolProvider:
    from bowhead_core.builtin_datarc_scope import supported_datarc_operations
    from bowhead_core.builtin_datarc_tool import build_builtin_datarc_specs, datarc_source_label, datarc_tool_ref

    order = ("datarc_workspaces", "datarc_schema", "datarc_query")

    def operations(tool: Any) -> tuple[str, ...]:
        if tool.provider != "datarc" or tool.status != "enabled":
            return ()
        supported = supported_datarc_operations(tool)
        return tuple(op for op in order if op in supported)

    return BuiltinToolProvider(
        name="datarc",
        operations=operations,
        tool_ref=datarc_tool_ref,
        source_label=datarc_source_label,
        build_specs=build_builtin_datarc_specs,
        catalog={
            "datarc_workspaces": CatalogOperation(
                "List Datarc workspaces",
                "List workspaces accessible to the current user.",
                frozenset({"data_discovery"}),
                display_name_zh="北极九章工作区查看",
                description_zh="查看当前用户可访问的工作区。",
            ),
            "datarc_schema": CatalogOperation(
                "Inspect Datarc schema",
                "Inspect queryable metrics and dimensions in a workspace.",
                frozenset({"data_discovery"}),
                display_name_zh="北极九章数据结构查看",
                description_zh="查看工作区内可查询的指标和维度。",
            ),
            "datarc_query": CatalogOperation(
                "Query Datarc data",
                "Query business data with the current user's Datarc membership permissions.",
                frozenset({"data_query"}),
                display_name_zh="北极九章数据查询",
                description_zh="以当前用户的北极九章成员权限查询业务数据。",
            ),
        },
        dependency_revision="datarc-scope-v1",
        turn_allows=lambda _ctx: True,
    )


def _google() -> BuiltinToolProvider:
    from bowhead_core.builtin_google_tool import build_builtin_google_specs, google_source_label, google_tool_ref, runtime_operations

    return BuiltinToolProvider(
        name="google_workspace",
        operations=runtime_operations,
        tool_ref=google_tool_ref,
        source_label=google_source_label,
        build_specs=build_builtin_google_specs,
        catalog={
            "gworkspace_read": CatalogOperation(
                "Read Google Docs, Sheets and Slides",
                "Reads files linked in the conversation with the current user's own Google access.",
                frozenset({"data_discovery"}),
                display_name_zh="读取 Google 文档、表格与演示",
                description_zh="以当前用户本人的 Google 权限读取对话中给出链接的文件。",
                display_name_en="Read Google Docs, Sheets and Slides",
                description_en="Reads files linked in the conversation with the current user's own Google access.",
                display_name_de="Google Docs, Tabellen und Präsentationen lesen",
                description_de="Liest im Gespräch verlinkte Dateien mit dem eigenen Google-Zugriff des aktuellen Nutzers.",
                display_name_fr="Lire Google Docs, Sheets et Slides",
                description_fr="Lit les fichiers liés dans la conversation avec l'accès Google propre à l'utilisateur actuel.",
            ),
            "gcalendar_events": CatalogOperation(
                "View Google Calendar",
                "Reads the current user's own calendar events.",
                frozenset({"data_discovery"}),
                display_name_zh="查看 Google 日历",
                description_zh="读取当前用户本人的日历事件。",
                display_name_en="View Google Calendar",
                description_en="Reads the current user's own calendar events.",
                display_name_de="Google Kalender ansehen",
                description_de="Liest die eigenen Kalendertermine des aktuellen Nutzers.",
                display_name_fr="Consulter Google Agenda",
                description_fr="Lit les événements d'agenda de l'utilisateur actuel.",
            ),
        },
        dependency_revision="google-workspace-v1",
        # Same single criterion send_email uses: only web chat and web clarification
        # resume set it; scheduled, plans, workflow runs and IM channels never do.
        turn_allows=lambda ctx: bool(getattr(ctx, "config_lite_supported", False)),
    )


def _powerbi() -> BuiltinToolProvider:
    from bowhead_core.builtin_powerbi_tool import (
        OPERATIONS,
        build_builtin_powerbi_specs,
        powerbi_source_label,
        powerbi_tool_ref,
    )

    labels = {
        "pbi_execute_dax": (
            "Query Power BI",
            "Run a DAX query against the selected connection's semantic model.",
            "查询 Power BI",
            "对指定连接的语义模型执行 DAX 查询。",
            "data_query",
        ),
        "pbi_fetch_rows": (
            "Read Power BI rows",
            "Read detail or aggregate rows from the selected connection.",
            "读取 Power BI 明细",
            "从指定连接读取明细或聚合行。",
            "data_query",
        ),
        "pbi_list_measures": (
            "List Power BI measures",
            "Inspect the selected connection's configured measure catalog.",
            "查看 Power BI 指标",
            "查看指定连接已配置的度量目录。",
            "data_discovery",
        ),
        "pbi_list_templates": (
            "List Power BI templates",
            "List DAX templates for the selected connection.",
            "查看 Power BI 模板",
            "列出指定连接的 DAX 模板。",
            "data_discovery",
        ),
        "pbi_execute_template": (
            "Run a Power BI template",
            "Fill parameters and run a DAX template for the selected connection.",
            "执行 Power BI 模板",
            "填写参数并执行指定连接的 DAX 模板。",
            "data_query",
        ),
    }
    return BuiltinToolProvider(
        name="powerbi",
        operations=lambda tool: OPERATIONS if tool.provider == "powerbi" and tool.status == "enabled" else (),
        tool_ref=powerbi_tool_ref,
        source_label=powerbi_source_label,
        build_specs=build_builtin_powerbi_specs,
        catalog={
            key: CatalogOperation(label, description, frozenset({capability}), display_name_zh=label_zh, description_zh=description_zh)
            for key, (label, description, label_zh, description_zh, capability) in labels.items()
        },
        dependency_revision="powerbi-connections-v1",
        turn_allows=lambda _ctx: True,
    )


def _shopify() -> BuiltinToolProvider:
    from bowhead_core.builtin_shopify_tool import (
        OPERATIONS,
        build_builtin_shopify_specs,
        shopify_source_label,
        shopify_tool_ref,
        supported_operations,
    )

    return BuiltinToolProvider(
        name="shopify",
        operations=supported_operations,
        tool_ref=shopify_tool_ref,
        source_label=shopify_source_label,
        build_specs=build_builtin_shopify_specs,
        catalog={
            name: CatalogOperation(
                name.replace("shopify_", "").replace("_", " ").title(),
                description,
                frozenset(
                    {
                        "team_domain_write"
                        if name == "shopify_apply_content_update"
                        else "data_discovery"
                        if name == "shopify_shop_context"
                        else "data_query"
                    }
                ),
                risk="external_write" if name == "shopify_apply_content_update" else "read",
            )
            for name, (_model, description) in OPERATIONS.items()
        },
        dependency_revision="shopify-content-v1",
        turn_allows=lambda _ctx: True,
    )


def providers() -> tuple[BuiltinToolProvider, ...]:
    from bowhead_core.builtin_shopify_tool import feature_enabled

    return (_datarc(), _google(), _powerbi()) + ((_shopify(),) if feature_enabled() else ())


def active_by_provider(rows: list[Any], ctx: Any | None) -> list[tuple[BuiltinToolProvider, list[Any]]]:
    """Connections that expose at least one operation, grouped by provider.

    ``ctx=None`` means authoring (no turn): the per-turn gate is not applied.
    """
    out: list[tuple[BuiltinToolProvider, list[Any]]] = []
    for provider in providers():
        if ctx is not None and not provider.turn_allows(ctx):
            continue
        tools = [tool for tool in rows if tool.provider == provider.name and provider.operations(tool)]
        if tools:
            out.append((provider, tools))
    return out
