"""Actor-scoped, non-secret catalog for Subagent authoring.

The catalog is advisory UX state, never an authorization token. Final create
must rebuild it and re-run the canonical validator against live state.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal

from bowhead_core.agents._generated.role_bindings import _PARADIGM_V2_ROLE_MAP
from bowhead_core.agents._generated.tool_display import TOOL_DISPLAY
from bowhead_core.agents._generated.tool_kinds import TOOL_RISKS
from bowhead_core.approval_gate import approval_executable
from bowhead_core.custom_subagent_store import (
    DESTRUCTIVE_TOOL_PREFIXES,
    REPORT_WRITE_TOOLS,
)
from bowhead_core.team_tool_loader import load_team_tool_entries
from bowhead_core.tool_registry import build_default_registry

Risk = Literal[
    "read",
    "compute",
    "artifact_write",
    "external_write",
    "destructive",
    "unknown",
]
RiskSource = Literal[
    "native_registry",
    "openapi_method",
    "mcp_annotation",
    "unknown_default",
]

CATALOG_SCHEMA_VERSION = 1
NATIVE_ALLOWLIST_VERSION = "native-authoring-v2"


def native_allowlist_version() -> str:
    """Catalog-level label: v3 includes extra Tools available to team Agents (§4.2).

    Per-Tool ``dependency_revision`` stays NATIVE_ALLOWLIST_VERSION so existing Agents' dependency
    fingerprints do not change.
    """
    from bowhead_core.resource_disable import team_agent_opened_tools

    return "native-authoring-v3" if team_agent_opened_tools() else NATIVE_ALLOWLIST_VERSION


RISK_REGISTRY_VERSION = "subagent-risk-v1"
APPROVAL_EXECUTOR_UNAVAILABLE = "approval_executor_unavailable"
# Skill wrapper Tools are not Owner choices (agent-skill-gates D1). Each Skill's
# grant decides them: the dependency compiler adds skill_read for any Skill and
# skill_invoke for a runnable one, and ignores stored refs to all three. They
# stay in the compile catalog (not discoverable) so the compiler can resolve the
# wrappers it derives; no picker lists them.
COMPILER_DERIVED_SKILL_TOOLS: frozenset[str] = frozenset({"skill_read", "skill_invoke", "skill_list"})

_DEFAULT_REGISTRY = build_default_registry()
_HIDDEN_NATIVE_NAMES: frozenset[str] = frozenset(
    _DEFAULT_REGISTRY.list_names(),
) - frozenset(schema.name for schema in _DEFAULT_REGISTRY.to_schemas())
_PSEUDO_ANALYST_NATIVES = frozenset({"python_sandbox"})
# Authoring exposes only python_sandbox as the Python execution entry.
#
# python_exec is a flat entry to the same sandbox (top-level code/inputs, internally still skill_invoke →
# python_sandbox). Listing both shows customers two identical entries ("运行代码 ·
# 在隔离沙箱里跑 Python…"), with no distinction or clear choice (photographed in 116 dogfood on 2026-09-07).
# This is the same decision already in tool_aliases.yaml to converge display on python_sandbox;
# it simply had not reached this list.
#
# ⚠ Affects only authoring checkboxes: python_exec remains registered, bound to analyst, and executable at runtime.
# Selecting python_sandbox is translated by subagent.py:_materialize_authoring_pseudo_tools into
# restricted skill_invoke (python_sandbox only), with equivalent capability.
# Existing agents on 116 then had zero python_exec tool_refs; later new agents acquired them (two Eddy agents on 2026-09-25).
# Reference-based catalog lookup must therefore map it to python_sandbox (AUTHORING_LOOKUP_ALIASES), or report it missing from the catalog.
# Internal Coach Tools require server-bound Coach admission, not ordinary Agent tool selection.
_AUTHORING_HIDDEN_NATIVES = frozenset(
    {
        "python_exec",
        "agent_authoring_inspect",
        "agent_authoring_tools",
        "agent_authoring_validate",
        "agent_authoring_propose",
        "agent_authoring_proposal_get",
        "agent_change_validate",
        "agent_skill_inspect",
        "agent_eval_evidence_get",
        "agent_ci_request",
        "agent_operation_open",
        "agent_operation_get",
        "agent_ci_get",
        "agent_ci_prepare",
        "agent_ci_start",
        "agent_ci_cancel",
        "agent_eval_suite_list",
        "agent_eval_suite_get",
        "agent_eval_case_propose",
        "agent_eval_case_proposal_get",
        "agent_eval_turns_list",
        "agent_eval_alignment_start",
        "agent_eval_alignment_ask",
        "agent_eval_alignment_get",
        "agent_eval_alignment_report",
        "agent_plugin_inspect",
        "agent_plugin_validate",
        "agent_plugin_save",
        "agent_plugin_operation_get",
        "agent_tool_requirement_create",
        "agent_tool_requirement_get",
        "agent_tool_requirement_open",
        "agent_tool_requirement_reconcile",
        "agent_skill_read",
        "agent_skill_validate",
        "agent_skill_save",
        "agent_skill_cancel",
        "agent_skill_operation_get",
        "agent_change_propose",
        "agent_change_get",
        "agent_draft_start",
        "agent_resource_search",
        "agent_resource_get",
        # Platform and Coach-only Tools: they open Bowhead's own editors, stage settings changes or save
        # Skills for Coach, so they are never a choice for a custom Agent (novice walkthrough 2026-10-21).
        "agent_resource_open",
        "agent_working_copy_open",
        "configuration_apply",
        "configuration_open",
        "configuration_propose",
        "configuration_list",
        "configuration_read",
        "configuration_result",
        "configuration_submit",
        "skill_save_generated",
        # Long-term memory tools are Lead built-ins. They joined tool_aliases.yaml for display
        # names only; keeping them out here leaves the authoring list and dependency fingerprints unchanged.
        "remember",
        "recall",
    }
)
# Built-in Skills that only Bowhead's own assistants use: the settings assistant's Skills, Coach's authoring
# method, Lead's "what can this Space do" answer and the analyst's internal return format. Owners never pick them;
# a stored grant still resolves through the compiler.
_PLATFORM_ONLY_SKILLS: frozenset[str] = frozenset({"agent-authoring", "workspace-capabilities", "analyst-sop-output-contract"})


def is_platform_only_skill(name: str) -> bool:
    """Whether a built-in Skill is kept out of the Skill pickers custom Agents are built with."""
    return name in _PLATFORM_ONLY_SKILLS or name.startswith("settings-")


# Tools combined into one authoring row are also combined during reference-based catalog lookup.
AUTHORING_LOOKUP_ALIASES: dict[str, str] = {"python_exec": "python_sandbox"}


def runtime_tool_ref(ref: str) -> str:
    """A stored or declared tool reference, named the way the authoring catalog lists it.

    Old names are renamed (``tool_name_canon``) and ``python_exec`` folds into ``python_sandbox``. Every check
    that asks "does this Agent have tool X" compares these, not the raw strings (116 dogfood 2026-09-26: the
    capability report called ``python_sandbox`` Agents short of ``python_exec``).
    """
    from bowhead_core.tool_name_canon import canonical_tool_name

    name = canonical_tool_name(ref)
    return AUTHORING_LOOKUP_ALIASES.get(name, name)


NATIVE_ALLOWLIST: frozenset[str] = (
    frozenset(
        name
        for name, roles in _PARADIGM_V2_ROLE_MAP.items()
        if "analyst" in roles
        and name != "subagent_return"
        and not any(name.startswith(prefix) for prefix in DESTRUCTIVE_TOOL_PREFIXES)
        and name not in REPORT_WRITE_TOOLS
        and name not in _HIDDEN_NATIVE_NAMES
    )
    | _PSEUDO_ANALYST_NATIVES
) - _AUTHORING_HIDDEN_NATIVES

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class CatalogAccessDenied(PermissionError):
    """The caller has not proven active membership in the requested team."""


@dataclass(frozen=True)
class AuthoringTool:
    stable_ref: str
    display_name: str
    description: str
    source_kind: Literal["native", "team_mcp"]
    source_display_name: str | None
    capability_classes: frozenset[str]
    risk: Risk
    discoverable: bool
    selectable: bool
    locked_reason: str | None
    risk_source: RiskSource
    approval_required: bool
    confirmation_summary: str | None
    dependency_revision: str
    # Built-in display translations travel together; external tools retain their authored labels.
    display_name_zh: str = ""
    description_zh: str = ""
    display_name_en: str = ""
    description_en: str = ""
    display_name_de: str = ""
    description_de: str = ""
    display_name_fr: str = ""
    description_fr: str = ""
    source_id: str | None = None
    operation_id: str | None = None
    method: str | None = None
    path: str | None = None

    def for_selection(self) -> AuthoringTool:
        """Project current picking availability without changing dependency facts.

        The dependency compiler also consumes this catalog. An executor outage
        must not invalidate an existing Agent's otherwise valid read capability.
        Existing license/risk locks take precedence and are never grandfathered.
        """
        if self.selectable and self.approval_required and not approval_executable(self.stable_ref):
            return replace(self, selectable=False, locked_reason=APPROVAL_EXECUTOR_UNAVAILABLE)
        return self

    def to_public_dict(self) -> dict[str, Any]:
        value = asdict(self.for_selection())
        value["capability_classes"] = sorted(self.capability_classes)
        value["connection_id"] = value.pop("source_id", None)
        # The legacy picker keeps method/path for compatibility. The new
        # authoring DTO deliberately does not expose endpoint paths.
        value.pop("path", None)
        return value


@dataclass(frozen=True)
class SubagentAuthoringCatalogSnapshot:
    team_id: str
    actor_user_id: str
    native_tools: tuple[AuthoringTool, ...]
    team_tools: tuple[AuthoringTool, ...]
    fingerprint: str
    dependency_guard: tuple[tuple[str, str], ...]
    built_at: datetime
    risk_registry_version: str = RISK_REGISTRY_VERSION
    schema_version: int = CATALOG_SCHEMA_VERSION

    @property
    def tools_by_ref(self) -> dict[str, AuthoringTool]:
        return {tool.stable_ref: tool for tool in (*self.native_tools, *self.team_tools)}


def _safe_label(value: object, *, fallback: str, limit: int = 120) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = _CONTROL_RE.sub(" ", normalized)
    normalized = " ".join(normalized.split())
    return (normalized or fallback)[:limit]


def classify_openapi_risk(method: str) -> tuple[Risk, RiskSource]:
    normalized = method.strip().upper()
    if normalized in {"GET", "HEAD", "OPTIONS"}:
        return "read", "openapi_method"
    if normalized in {"POST", "PUT", "PATCH"}:
        return "external_write", "openapi_method"
    if normalized == "DELETE":
        return "destructive", "openapi_method"
    return "unknown", "unknown_default"


def classify_mcp_risk(
    *,
    read_only: bool | None,
    destructive: bool | None,
) -> tuple[Risk, RiskSource]:
    # Conflicting or incomplete annotations fail closed. In particular, legacy
    # ``destructive=False`` never gets silently promoted to a read capability.
    if read_only is True and destructive is False:
        return "read", "mcp_annotation"
    if read_only is False and destructive is True:
        return "destructive", "mcp_annotation"
    return "unknown", "unknown_default"


def classify_native_risk(tool_ref: str) -> tuple[Risk, RiskSource]:
    """Native risk from the `risk` field in agents/manifests/tool_aliases.yaml.

    A newly allowlisted native without a declared risk is intentionally ``unknown`` and therefore
    cannot be selected. write_report is artifact_write like add_chart; its real gate is per-section
    ownership (REPORT_WRITE_TOOLS).
    """
    risk = TOOL_RISKS.get(tool_ref)
    if risk is None:
        return "unknown", "unknown_default"
    return risk, "native_registry"


def _capability_classes(tool_ref: str, risk: Risk) -> frozenset[str]:
    if tool_ref.startswith("bi_"):
        return frozenset(
            {"data_discovery" if tool_ref.endswith(("schema", "measures")) else "data_query"},
        )
    if tool_ref.startswith("kb_"):
        return frozenset({"knowledge_search"})
    if tool_ref.startswith("web_"):
        return frozenset({"public_web_research"})
    if tool_ref == "python_sandbox":
        return frozenset({"isolated_compute"})
    if risk == "artifact_write":
        return frozenset({"artifact_chart" if "chart" in tool_ref else "artifact_report"})
    if tool_ref.startswith("teammcp."):
        operation = "team_domain_read" if risk == "read" else "team_domain_write"
        return frozenset({operation})
    return frozenset()


def _native_descriptions() -> dict[str, str]:
    return {
        schema.name: _safe_label(
            getattr(schema, "description", ""),
            fallback=schema.name,
            limit=500,
        )
        for schema in _DEFAULT_REGISTRY.to_schemas()
    }


def _native_display(tool_ref: str) -> tuple[str, str, str, str]:
    """勾选列表里这个内置工具叫什么、干什么用（中英）。

    ⚠ **不要**回去用 `ToolSchema.description`。capability-surface-audit 2026-09-06 实测：
    28 项里 `display_name` 100% 是工具代号本身，`description` 19 项落回代号
    （`_DEFAULT_REGISTRY` 只装 BI/datarc 那一个 registrar，canvas / skill / web /
    python 由别的 registrar 注册），剩下 9 项显示的是喂给模型的 prompt 原文
    （`NL → datarc rows. 批量传 queries: List[str], ...`）。三种都不是给客户看的。

    名字和说明的唯一来源是 tool_aliases.yaml —— 与步骤卡、chip 同一份。
    registry description 只在 yaml 没登记这个工具时兜底（正常不该发生）。
    """
    entry = TOOL_DISPLAY.get(tool_ref)
    if entry is None:
        fallback = _native_descriptions().get(tool_ref, tool_ref)
        return tool_ref, tool_ref, fallback, ""
    return (
        entry["display_zh"],
        entry["display_en"],
        entry["blurb_zh"],
        entry["blurb_en"],
    )


def native_authoring_tools() -> tuple[AuthoringTool, ...]:
    """Return the one code-owned exact catalog for selectable native Tools."""
    tools: list[AuthoringTool] = []
    from bowhead_core.resource_disable import team_agent_opened_tools

    for tool_ref in sorted(NATIVE_ALLOWLIST | team_agent_opened_tools()):
        name_zh, name_en, blurb_zh, blurb_en = _native_display(tool_ref)
        translations = TOOL_DISPLAY.get(tool_ref, {})
        risk, risk_source = classify_native_risk(tool_ref)
        tools.append(
            AuthoringTool(
                stable_ref=tool_ref,
                display_name=name_en,
                description=blurb_en,
                display_name_zh=name_zh,
                description_zh=blurb_zh,
                display_name_en=name_en,
                description_en=blurb_en,
                display_name_de=translations.get("display_de", name_en),
                description_de=translations.get("blurb_de", blurb_en),
                display_name_fr=translations.get("display_fr", name_en),
                description_fr=translations.get("blurb_fr", blurb_en),
                source_kind="native",
                source_display_name="Built-in tools",
                capability_classes=_capability_classes(tool_ref, risk),
                risk=risk,
                discoverable=tool_ref not in COMPILER_DERIVED_SKILL_TOOLS,
                selectable=risk != "unknown",
                locked_reason="risk_metadata_unknown" if risk == "unknown" else None,
                risk_source=risk_source,
                approval_required=False,
                confirmation_summary=None,
                dependency_revision=NATIVE_ALLOWLIST_VERSION,
            ),
        )
    return tuple(tools)


def _fingerprint_payload(
    native_tools: tuple[AuthoringTool, ...],
    team_tools: tuple[AuthoringTool, ...],
    *,
    mcp_entitled: bool,
) -> str:
    rows = [
        {
            "ref": tool.stable_ref,
            "revision": tool.dependency_revision,
            "risk": tool.risk,
            "selectable": tool.selectable,
            "locked_reason": tool.locked_reason,
        }
        for tool in sorted(
            (item.for_selection() for item in (*native_tools, *team_tools)),
            key=lambda item: item.stable_ref,
        )
    ]
    payload = {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "native_allowlist_version": native_allowlist_version(),
        "risk_registry_version": RISK_REGISTRY_VERSION,
        "mcp_entitled": mcp_entitled,
        "tools": rows,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
    ).hexdigest()
    return f"sha256:{digest}"


class SubagentAuthoringCatalogService:
    async def build(
        self,
        *,
        store: object,
        team_id: str,
        actor_user_id: str,
        actor_active: bool,
        mcp_entitled: bool,
    ) -> SubagentAuthoringCatalogSnapshot:
        if not actor_active or not team_id or not actor_user_id:
            raise CatalogAccessDenied("active team membership required")

        native_tools = native_authoring_tools()

        entries, sources = await load_team_tool_entries(team_id, store)
        team_tools_list: list[AuthoringTool] = []
        dependency_guard: list[tuple[str, str]] = [
            ("native", NATIVE_ALLOWLIST_VERSION),
            ("risk", RISK_REGISTRY_VERSION),
        ]
        for source_id, source in sorted(sources.items()):
            dependency_guard.append(
                (source_id, str(getattr(source, "updated_at", "") or "")),
            )
        for entry in sorted(entries, key=lambda item: item.advertised_name):
            source = sources.get(entry.source_id)
            source_name = _safe_label(
                getattr(source, "name", ""),
                fallback="Team tool",
                limit=80,
            )
            if entry.op is not None:
                risk, risk_source = classify_openapi_risk(entry.op.method)
                method = entry.op.method.upper()
                path = entry.op.path
            else:
                risk, risk_source = classify_mcp_risk(
                    read_only=entry.read_only_hint,
                    destructive=entry.destructive_hint,
                )
                method = None
                path = None
            locked_reason = None
            selectable = risk != "unknown"
            if not mcp_entitled:
                selectable = False
                locked_reason = "mcp_feature_unlicensed"
            elif risk == "unknown":
                locked_reason = "risk_metadata_unknown"
            approval_required = risk in {"external_write", "destructive"}
            confirmation = (
                f"{source_name} · {_safe_label(entry.dispatch_name, fallback='operation', limit=80)} · {risk}" if approval_required else None
            )
            revision = str(getattr(source, "updated_at", "") or "")
            team_tools_list.append(
                AuthoringTool(
                    stable_ref=entry.advertised_name,
                    display_name=_safe_label(
                        entry.dispatch_name,
                        fallback=entry.advertised_name,
                        limit=120,
                    ),
                    description=_safe_label(
                        entry.description,
                        fallback=entry.dispatch_name,
                        limit=500,
                    ),
                    source_kind="team_mcp",
                    source_display_name=source_name,
                    capability_classes=_capability_classes(
                        entry.advertised_name,
                        risk,
                    ),
                    risk=risk,
                    discoverable=True,
                    selectable=selectable,
                    locked_reason=locked_reason,
                    risk_source=risk_source,
                    approval_required=approval_required,
                    confirmation_summary=confirmation,
                    dependency_revision=revision,
                    source_id=entry.source_id,
                    operation_id=entry.dispatch_name,
                    method=method,
                    path=path,
                ),
            )
        from bowhead_core.builtin_tool_providers import active_by_provider

        list_builtin_tools = getattr(store, "list_team_builtin_tools", None)
        builtin_tools = await list_builtin_tools(team_id) if callable(list_builtin_tools) else []
        for provider, provider_tools in active_by_provider(builtin_tools, None):
            for tool in provider_tools:
                operations = provider.operations(tool)
                dependency_guard.append((tool.id, ",".join(sorted(operations))))
                for operation in operations:
                    meta = provider.catalog[operation]
                    team_tools_list.append(
                        AuthoringTool(
                            stable_ref=provider.tool_ref(tool.id, operation),
                            display_name=meta.display_name,
                            description=meta.description,
                            display_name_zh=meta.display_name_zh,
                            description_zh=meta.description_zh,
                            display_name_en=meta.display_name_en or meta.display_name,
                            description_en=meta.description_en or meta.description,
                            display_name_de=meta.display_name_de,
                            description_de=meta.description_de,
                            display_name_fr=meta.display_name_fr,
                            description_fr=meta.description_fr,
                            source_kind="team_mcp",
                            source_display_name=provider.source_label(tool.display_name),
                            capability_classes=meta.capability_classes,
                            risk=meta.risk,
                            discoverable=True,
                            selectable=True,
                            locked_reason=None,
                            risk_source="native_registry",
                            approval_required=False,
                            confirmation_summary="The requester reviews and confirms the exact content before each Shopify update."
                            if meta.risk == "external_write"
                            else None,
                            dependency_revision=f"{provider.dependency_revision}:{operation}",
                            source_id=tool.id,
                            operation_id=operation,
                        )
                    )
        team_tools = tuple(team_tools_list)
        return SubagentAuthoringCatalogSnapshot(
            team_id=team_id,
            actor_user_id=actor_user_id,
            native_tools=native_tools,
            team_tools=team_tools,
            fingerprint=_fingerprint_payload(
                native_tools,
                team_tools,
                mcp_entitled=mcp_entitled,
            ),
            dependency_guard=tuple(dependency_guard),
            built_at=datetime.now(UTC),
        )


__all__ = [
    "AuthoringTool",
    "CATALOG_SCHEMA_VERSION",
    "COMPILER_DERIVED_SKILL_TOOLS",
    "CatalogAccessDenied",
    "NATIVE_ALLOWLIST",
    "is_platform_only_skill",
    "RISK_REGISTRY_VERSION",
    "SubagentAuthoringCatalogService",
    "SubagentAuthoringCatalogSnapshot",
    "classify_mcp_risk",
    "classify_native_risk",
    "classify_openapi_risk",
    "native_authoring_tools",
]
