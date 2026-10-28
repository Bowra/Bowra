"""Shared effective-capability resolution for turns and read models."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from bowhead_router.types import ToolSchema

from bowhead_core.approval_gate import approval_executable
from bowhead_core.tool_registry import ToolRegistry, _effective_tool_whitelist, connector_for_tool

if TYPE_CHECKING:
    import contextlib

    from bowhead_core.tool_registry import ToolExecutionContext

logger = logging.getLogger(__name__)


async def resolve_unconfigured_capability_tools(runtime: Any, team_id: str) -> frozenset[str]:
    """Use the same team credential readiness for Turn and discovery projections.

    Runtime is the orchestrator dependency container. Missing resolvers or a
    transient lookup failure keep the existing execution-time fallback.
    Google delivery tools count as unconfigured unless the team connection is enabled;
    the per-turn "is anyone present" rule is applied by the turn adapter.
    """
    if not team_id:
        # Without a team there is no Google connection: never advertise what would always be refused.
        from bowhead_core.google_delivery_tool import TOOL_NAMES as google_delivery_tools

        return frozenset(google_delivery_tools)
    unavailable: set[str] = set()
    for resolver_attr, capability, tool_name in (
        ("team_search_resolver", "web_search", "web_search"),
        ("team_media_resolver", "image_gen", "image_generate"),
    ):
        resolver = getattr(runtime, resolver_attr, None)
        if resolver is None:
            continue
        try:
            registry = await resolver(team_id)
            spec = registry.get_active(capability) if registry is not None else None
            if spec is None or not getattr(spec, "api_key", ""):
                unavailable.add(tool_name)
        except Exception:
            logger.warning("capability_visibility.resolve_failed cap=%s team=%s", capability, team_id, exc_info=True)
    # Tavily web tools differ from search/media registry shape: hide the entire group if the team's Tavily row is disabled or keyless.
    tavily_resolver = getattr(runtime, "team_tavily_resolver", None)
    if tavily_resolver is not None:
        from bowhead_core.web_tavily_tools import TAVILY_TOOL_NAMES

        try:
            if await tavily_resolver(team_id) is None:
                unavailable.update(TAVILY_TOOL_NAMES)
        except Exception:
            logger.warning("capability_visibility.resolve_failed cap=tavily team=%s", team_id, exc_info=True)
    from bowhead_core.google_delivery_tool import delivery_tools_unavailable

    unavailable |= await delivery_tools_unavailable(getattr(runtime, "user_store", None), team_id, attended=None)
    return frozenset(unavailable)


def bi_visibility_gate_enabled() -> bool:
    """bi-absent-turn R2 (`FEATURE_BI_VISIBILITY_GATE`，默认 0)：BI 缺席的 turn 是否
    直接从 schema 广告面剔除 bi_*（硬保证）。关 = 只靠 prompt 劝阻 + dispatch 闸兜底
    （Phase 2 行为）。每调用读 env——测试 monkeypatch 即生效，无进程级缓存。"""
    import os

    return os.environ.get("FEATURE_BI_VISIBILITY_GATE", "0") == "1"


CapabilityKind = Literal["skill", "subagent", "tool", "platform"]
CapabilityOperation = Literal["read", "create", "update", "delete", "execute"]


@dataclass(frozen=True)
class CapabilityPrincipal:
    user_id: str
    team_id: str
    workspace_id: str
    role: str | None = "lead"
    read_only: bool = False


@dataclass(frozen=True)
class CapabilityRef:
    id: str
    kind: CapabilityKind
    label: str
    description: str
    operations: frozenset[CapabilityOperation]
    output_formats: tuple[str, ...]
    version: str
    # home-i18n: display name for English UI. Empty = no English name (Chinese skill titles / tool names and other
    # proper names display verbatim). Excluded from capability_fingerprint (which uses only id/version/
    # operations/formats), so adding this field has no cache side effects.
    label_en: str = ""


@dataclass(frozen=True)
class ResolvedCapabilities:
    tool_view: ToolRegistry | None
    refs: tuple[CapabilityRef, ...]
    fingerprint: str


def advertised_tool_schemas(
    view: ToolRegistry,
    profile_whitelist: tuple[str, ...] | frozenset[str],
    *,
    profile_name: str,
    role: str | None,
    team_tool_bundle: tuple[str, ...] | frozenset[str] | None = None,
    drop_bi: bool = False,
    drop_unconfigured: frozenset[str] = frozenset(),
    drop_unapprovable: bool = False,
) -> list[ToolSchema]:
    """Return the exact schemas advertised by SubagentRunner for a profile.

    bi-absent-turn R2: ``drop_bi=True``（调用方在 flag 开启且
    ``ctx.bi_availability() == "absent"`` 时传）从广告面剔除 datarc connector 的
    全部工具——BI 缺席时 LLM 根本看不见 bi_*，硬保证；默认 False 使既有调用与全部
    测试逐字节不变。``needs_selection`` 恒不剔（sentinel 弹卡靠工具调用触发）。

    credential-readiness R6: ``drop_unconfigured``（= ``ctx.unconfigured_capability_tools``，
    团队未配置的可选能力工具名，如 web_search）同样从广告面剔除——未配置的能力对
    LLM 不可见，模型不再白绕一轮拿「未配置」错误串（PRD §6.3；与 drop_bi 同 seam，
    别写第三种剔除机制）。默认空集 = 字节不变。

    advertise-execute-gap（2026-09-07）：``drop_unapprovable=True`` 剔除「需要审批、
    而审批链路今天执行不了」的工具（判据取执行侧那一份 ``approval_executable``）。

    ⚠ **必须是调用方可选，不能在函数内部无条件做**——本函数有两个语义不同的调用方：
      1. ``subagent.py`` 的 LLM 广告面（要剔：模型看见了也调不动，白烧一轮）
      2. ``CapabilityResolver.resolve`` 算 capability refs → ``capability_fingerprint``
         （**不能剔**：那是「这个 agent 拥有什么能力」的读模型，剔了会改指纹，
         而指纹参与 agent revision/release 的一致性校验）
    设计初稿论证过「判据不依赖 ctx 所以内部无条件判」——**那个论证是错的**，
    决定因素不是判据依赖什么，而是**是不是所有调用方都想要这个过滤**。
    既有测试 ``test_resolver_does_not_treat_risk_level_as_write_semantics`` 逮到了它。"""
    effective_whitelist = effective_tool_names(
        view,
        profile_whitelist,
        team_tool_bundle=team_tool_bundle,
    )
    filter_for_lead = profile_name == "lead" and role is not None
    schemas: list[ToolSchema] = []
    for schema in view.to_schemas():
        if schema.name not in effective_whitelist:
            continue
        spec = view.get(schema.name)
        if filter_for_lead and role not in spec.allowed_for_roles:
            continue
        if drop_bi and connector_for_tool(schema.name) == "datarc":
            continue
        if schema.name in drop_unconfigured:
            continue
        # advertise-execute-gap (2026-09-07): tools needing approval whose approval path cannot currently execute
        # stay off the LLM advertising surface. Use **the execution-side criterion** (approval_gate.approval_executable),
        # not a separate duplicate here; once executable (tool added to supported set / team tool connected /
        # operator enables the flag), it automatically returns to the model next turn without changes here.
        # Applies only when caller explicitly requests advertising semantics; see docstring for the reason.
        if drop_unapprovable and spec.needs_approval and not approval_executable(schema.name):
            continue
        schemas.append(schema)
    # Context engineering sprint batch 2-c: **sort by name** (`BOWHEAD_CTX_PREFIX_ORDER`, default ON).
    #
    # `tools` occupies **position 0** in provider render order (tools → system → messages),
    # so instability here invalidates the entire cacheable prefix. Previously order came from `to_schemas()`'s
    # `list(self._specs)`, dict insertion order: built-ins follow stable code registration order, but team overlays
    # append through `clone_with_overlay(extra_specs)` in `build_team_specs(...)`'s **list order**,
    # whose stability across processes / repeated loads has never been verified.
    # Sorting changes stability from accidental to structural.
    #
    # ⚠ This changes relative tool positions in prompts once (position preferences really exist),
    # so include it in batch 2 A/B, rather than zero-behavior-change batch 0.
    from bowhead_core.context_prefix_flags import prefix_order_enabled

    if prefix_order_enabled():
        schemas.sort(key=lambda s: s.name)
    return schemas


def effective_tool_names(
    view: ToolRegistry,
    profile_whitelist: tuple[str, ...] | frozenset[str],
    *,
    team_tool_bundle: tuple[str, ...] | frozenset[str] | None = None,
) -> set[str]:
    return _effective_tool_whitelist(
        profile_whitelist,
        view,
        team_tool_bundle,
    )


class CapabilityResolver:
    """Build a per-principal ToolRegistry view using the turn runtime rules."""

    def __init__(
        self,
        *,
        base_registry: ToolRegistry | None,
        user_store: Any = None,
        tool_auth_registry: Any = None,
        access_control_store: Any = None,
    ) -> None:
        self._base = base_registry
        self._user_store = user_store
        self._tool_auth_registry = tool_auth_registry
        self._access_control_store = access_control_store

    @classmethod
    def from_runtime(cls, runtime: Any) -> CapabilityResolver:
        return cls(
            base_registry=getattr(runtime, "tool_registry", None),
            user_store=getattr(runtime, "user_store", None),
            tool_auth_registry=getattr(runtime, "tool_auth_registry", None),
            access_control_store=getattr(runtime, "access_control_store", None),
        )

    async def resolve(
        self,
        stack: contextlib.AsyncExitStack,
        principal: CapabilityPrincipal,
        execution_context: ToolExecutionContext,
        *,
        profile_whitelist: tuple[str, ...] | frozenset[str] | None = None,
        profile_name: str = "lead",
        team_tool_bundle: tuple[str, ...] | frozenset[str] | None = None,
    ) -> ResolvedCapabilities:
        view = await self._build_team_tool_view(
            stack,
            team_id=principal.team_id,
            execution_context=execution_context,
        )
        if view is not None and hasattr(view, "clone_filtered_by_resource_grants"):
            view = await view.clone_filtered_by_resource_grants(execution_context)
        execution_context.tool_view = view if view is not self._base else None

        refs: tuple[CapabilityRef, ...] = ()
        if view is not None and profile_whitelist is not None:
            # bi-absent-turn R2: same gate as SubagentRunner advertising (flag default off).
            _bi_avail_fn = getattr(execution_context, "bi_availability", None)
            _drop_bi = False
            if callable(_bi_avail_fn):
                try:
                    _drop_bi = bi_visibility_gate_enabled() and _bi_avail_fn() == "absent"
                except Exception:  # noqa: S110
                    _drop_bi = False
            schemas = advertised_tool_schemas(
                view,
                profile_whitelist,
                profile_name=profile_name,
                role=principal.role,
                team_tool_bundle=team_tool_bundle,
                drop_bi=_drop_bi,
                drop_unconfigured=frozenset(getattr(execution_context, "unconfigured_capability_tools", ()) or ()),
            )
            refs = tuple(self._to_ref(view, schema) for schema in schemas)
            if principal.read_only:
                refs = tuple(ref for ref in refs if not ref.operations.intersection({"create", "update", "delete"}))
        return ResolvedCapabilities(
            tool_view=view,
            refs=refs,
            fingerprint=capability_fingerprint(refs),
        )

    async def _build_team_tool_view(
        self,
        stack: contextlib.AsyncExitStack,
        *,
        team_id: str,
        execution_context: ToolExecutionContext,
    ) -> ToolRegistry | None:
        base = self._base
        store = self._user_store
        auth = self._tool_auth_registry
        if not team_id or base is None or store is None or auth is None:
            return base
        try:
            from bowhead_core.access_control import resource_grants_enforce_enabled
            from bowhead_core.openapi_tool_source import (
                RemoteMcpToolClient,
                build_team_mcp_server,
                connect_team_mcp,
                make_http_executor,
            )
            from bowhead_core.team_tool_loader import (
                load_team_tool_entries,
                make_team_source_resolver,
            )
            from bowhead_core.team_tool_registry_view import build_team_specs

            entries, by_id = await load_team_tool_entries(team_id, store)
            list_builtin_tools = getattr(store, "list_team_builtin_tools", None)
            try:
                builtin_rows = await list_builtin_tools(team_id) if callable(list_builtin_tools) else []
            except Exception:
                logger.warning("cca.builtin_tool_load_failed team=%s", team_id, exc_info=True)
                builtin_rows = []
            from bowhead_core.builtin_tool_providers import active_by_provider

            builtin_groups = active_by_provider(builtin_rows, execution_context)
            builtin_tools = [tool for _provider, tools in builtin_groups for tool in tools]
            if not entries and not builtin_tools:
                return base
            access_ctx = getattr(execution_context, "access_context", None)
            if self._access_control_store is not None and not access_ctx and resource_grants_enforce_enabled():
                logger.warning("cca.load_point_missing_access_context team=%s enforce=True", team_id)
                return base
            if self._access_control_store is not None and access_ctx:
                enforce_grants = resource_grants_enforce_enabled()
                filtered_entries: list[Any] = []
                shadow_denied = 0
                from bowhead_core.grant_evaluation import grant_verdicts

                for entry in entries:
                    tool_name = entry.advertised_name
                    verdicts = {
                        verdict.resource_kind: verdict
                        for verdict in await grant_verdicts(
                            execution_context,
                            self._access_control_store,
                            access_ctx,
                            [("team_tool_source", entry.source_id, "use"), ("team_tool_op", tool_name, "use")],
                        )
                    }
                    allowed = all(verdict.allowed for verdict in verdicts.values())
                    if not allowed:
                        shadow_denied += 1
                        source_verdict, op_verdict = verdicts.get("team_tool_source"), verdicts.get("team_tool_op")
                        logger.info(
                            "resource_grant.team_tool_would_deny team=%s tool=%s source_reason=%s op_reason=%s enforce=%s",
                            team_id,
                            tool_name,
                            source_verdict.reason if source_verdict else None,
                            op_verdict.reason if op_verdict else None,
                            enforce_grants,
                        )
                        if enforce_grants:
                            continue
                    filtered_entries.append(entry)
                entries = filtered_entries
                by_id = {entry.source_id: by_id[entry.source_id] for entry in entries if entry.source_id in by_id}
                if not entries and not builtin_tools:
                    logger.info(
                        "cca.load_point_all_denied team=%s shadow_denied=%d enforce=%s",
                        team_id,
                        shadow_denied,
                        enforce_grants,
                    )
                    return base

            specs: list[Any] = []
            turn_budget = [0]
            openapi_entries = [entry for entry in entries if entry.kind == "openapi"]
            if openapi_entries:
                executor = make_http_executor(
                    team_id,
                    make_team_source_resolver(team_id, by_id, auth),
                )
                server = build_team_mcp_server(openapi_entries, executor)
                session = await stack.enter_async_context(connect_team_mcp(server))
                specs.extend(
                    build_team_specs(
                        team_id,
                        openapi_entries,
                        session,
                        audit_store=store,
                        budget=turn_budget,
                    )
                )

            mcp_by_source: dict[str, list[Any]] = {}
            for entry in entries:
                if entry.kind == "mcp":
                    mcp_by_source.setdefault(entry.source_id, []).append(entry)
            for source_id, source_entries in mcp_by_source.items():
                source = by_id.get(source_id)
                if source is None or not source.base_url:
                    continue
                headers = await auth.get_headers(team_id, source_id)
                oauth_provider: Any = None
                if source.auth_type == "oauth":
                    from bowhead_core.mcp_oauth import (
                        FernetTokenStorage,
                        build_mcp_oauth_provider,
                    )

                    oauth_provider = build_mcp_oauth_provider(
                        source.base_url,
                        FernetTokenStorage(store, team_id, source_id),
                    )
                    headers = {}
                # mcp-connect-repair F1-b: connects per call in the dispatching task (no session on
                # the turn stack — that was an anyio cross-task cancel-scope violation). Still zero
                # network here: the advertised tools come from the enumerate cache.
                lazy = RemoteMcpToolClient(
                    source.base_url,
                    headers,
                    auth=oauth_provider,
                )
                specs.extend(
                    build_team_specs(
                        team_id,
                        source_entries,
                        lazy,
                        audit_store=store,
                        budget=turn_budget,
                    )
                )
            for provider, provider_tools in builtin_groups:
                specs.extend(
                    provider.build_specs(
                        team_id=team_id,
                        tools=provider_tools,
                        store=store,
                        base_registry=base,
                    )
                )
            if not specs:
                return base
            view = base.clone_with_overlay(specs)
            # team-tools source grouping (2026-07-22): this is the sole location with both by_id (source display names) and
            # entries (advertised_name↔source_id); attach label mapping to ctx for
            # Lead/planner <team_tools> group headers. Use ctx, not view (view is later replaced by clones such as
            # _widen_spawn_enum, while ctx is stable). Best effort: failures lose only labels,
            # degrading headers to prefix-only, without losing tools.
            try:
                labels: dict[str, str] = {}
                for entry in entries:
                    src_row = by_id.get(entry.source_id)
                    src_name = getattr(src_row, "name", "") if src_row is not None else ""
                    if src_name:
                        labels[entry.advertised_name] = src_name
                for provider, provider_tools in builtin_groups:
                    for tool in provider_tools:
                        for operation in provider.operations(tool):
                            labels[provider.tool_ref(tool.id, operation)] = provider.source_label(tool.display_name)
                execution_context.team_tool_source_labels = labels or None
            except Exception:
                logger.warning("cca.load_point source_labels_failed team=%s", team_id)
            logger.info(
                "cca.load_point team=%s sources=%d tools=%d",
                team_id,
                len(by_id) + len(builtin_tools),
                len(specs),
            )
            return view
        except Exception as exc:
            logger.warning("cca.load_point_failed team=%s err=%s", team_id, type(exc).__name__)
            return base

    @staticmethod
    def _to_ref(view: ToolRegistry, schema: ToolSchema) -> CapabilityRef:
        spec = view.get(schema.name)
        annotations = spec.annotations
        can_write = bool(spec.needs_approval or (annotations is not None and (annotations.read_only is False or annotations.destructive is True)))
        operations: frozenset[CapabilityOperation] = frozenset({"execute", "update"}) if can_write else frozenset({"read", "execute"})
        label = ((annotations.title if annotations else None) or schema.name).strip()[:80]
        description = (schema.description or "").strip()[:240]
        version_payload = json.dumps(
            {
                "name": schema.name,
                "description": description,
                "roles": sorted(spec.allowed_for_roles),
                "risk": spec.risk_level,
                "write": can_write,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
        version = hashlib.sha256(version_payload.encode()).hexdigest()[:16]
        return CapabilityRef(
            id=f"tool:{schema.name}",
            kind="tool",
            label=label,
            description=description,
            operations=operations,
            output_formats=(),
            version=version,
        )


def capability_fingerprint(refs: tuple[CapabilityRef, ...]) -> str:
    payload = [
        {
            "id": ref.id,
            "version": ref.version,
            "operations": sorted(ref.operations),
            "formats": list(ref.output_formats),
        }
        for ref in sorted(refs, key=lambda item: item.id)
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
