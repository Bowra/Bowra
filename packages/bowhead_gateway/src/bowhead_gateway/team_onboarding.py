"""Read-only Team onboarding readiness projection."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

import anyio
from bowhead_core.email_config import (
    resolve_smtp_config,
    team_email_config_enabled,
)
from bowhead_core.team_kb_runtime import diagnose_kb_runtime_config
from bowhead_core.team_llm_config import (
    TeamLlmConfigError,
    is_v2_config,
    migrate_legacy_config_to_v2,
    validate_v2_config,
)
from bowhead_router.agent_tiers import is_tier_config, resolve_tiers
from bowhead_router.model_capabilities import (
    diagnose_model_ref,
    provider_has_team_secret,
)
from pydantic import BaseModel, Field

from bowhead_core import metrics
from bowhead_gateway.public_entry_context import current_public_access_context

logger = logging.getLogger(__name__)

ReadinessState = Literal[
    "not_applicable",
    "unknown",
    "missing",
    "ready",
    "warning",
    "blocked",
]
ReadinessGroup = Literal["core", "conditional", "recommended"]
RequiredScope = Literal["core", "capability"]
CapabilityState = Literal["enabled", "disabled", "invalid", "unknown"]

_CORE_IDS = (
    "license_active",
    "public_entry_ready",
    "llm_primary_configured",
    "space_ready",
    "first_turn_succeeded",
)
_ITEM_ORDER = (
    *_CORE_IDS,
    "knowledge_base_ready",
    "datarc_connection_configured",
    "llm_fallback_configured",
    "email_delivery_configured",
    "secondary_admin_present",
)
_ITEM_GROUP: dict[str, ReadinessGroup] = {
    **dict.fromkeys(_CORE_IDS, "core"),
    "knowledge_base_ready": "conditional",
    "datarc_connection_configured": "recommended",
    "llm_fallback_configured": "recommended",
    "email_delivery_configured": "recommended",
    "secondary_admin_present": "recommended",
}
_ITEM_UNAVAILABLE_REASON = {
    "license_active": "license_state_unavailable",
    "public_entry_ready": "public_entry_unavailable",
    "llm_primary_configured": "model_state_unavailable",
    "space_ready": "space_state_unavailable",
    "first_turn_succeeded": "turn_state_unavailable",
    "knowledge_base_ready": "kb_state_unavailable",
    "datarc_connection_configured": "datarc_state_unavailable",
    "llm_fallback_configured": "model_state_unavailable",
    "email_delivery_configured": "email_state_unavailable",
    "secondary_admin_present": "admin_state_unavailable",
}
_ACTIONS = {
    "public_entry_ready": "/team/settings/public-entries/?from=setup",
    "llm_primary_configured": "/team/settings/llm/?from=setup",
    "space_ready": "/team/settings/spaces/?from=setup",
    "first_turn_succeeded": "/",
    "knowledge_base_ready": "/team/settings/llm/?from=setup",
    "datarc_connection_configured": "/team/settings/datasource/?from=setup",
    "llm_fallback_configured": "/team/settings/llm/?from=setup",
    "email_delivery_configured": "/team/settings/integrations/?from=setup",
    "secondary_admin_present": "/team/settings/people/?from=setup",
}
_ACTION_ROLES = {
    "license_active": ("owner", "team_admin"),
    "public_entry_ready": ("owner",),
    "llm_primary_configured": ("owner", "team_admin"),
    "space_ready": ("owner", "team_admin"),
    "first_turn_succeeded": ("owner", "team_admin"),
    "knowledge_base_ready": ("owner", "team_admin"),
    "datarc_connection_configured": ("owner", "team_admin"),
    "llm_fallback_configured": ("owner", "team_admin"),
    "email_delivery_configured": ("owner", "team_admin"),
    "secondary_admin_present": ("owner",),
}


class ReadinessCheck(BaseModel):
    id: str
    state: ReadinessState
    required: bool
    satisfied: bool
    reason_code: str


class ReadinessAction(BaseModel):
    kind: Literal["internal_link"] = "internal_link"
    href: str


class ReadinessItem(BaseModel):
    id: str
    group: ReadinessGroup
    state: ReadinessState
    required: bool
    required_scope: RequiredScope | None = None
    satisfied: bool
    blocking: bool
    reason_code: str
    required_roles: list[str]
    can_act: bool
    observed_at: str
    action: ReadinessAction | None = None
    checks: list[ReadinessCheck] = Field(default_factory=list)


class CoreReadiness(BaseModel):
    satisfied: int
    total: Literal[5] = 5


class RuntimeSignal(BaseModel):
    """credential-readiness R8b：「配置了但运行时不可达」观测信号（PRD §6.2 B 档）。

    数据源 = 进程内 `bowhead_llm_refusal_total`（D1 的 RefusalAdapter 被真正调用
    即 +1）。非零 = 有路径没拿到团队模型配置——**系统缺陷警示，不是配置问题**，
    呈现不分角色、不给「去配置」CTA。进程重启归零（observed_since 标窗口起点）。

    按团队过滤（§19 follow-up 收口）：只计本团队 + "unattributed"（归属不明的
    进程级缺陷对每个团队都展示；他队的 refusal 不再串进本队信号）。单机单团队
    行为不变。多 worker 部署仍只见本 worker 分片——扩容 checklist 项。
    """

    llm_refusals_total: int = 0
    llm_refusals_by_reason: dict[str, int] = Field(default_factory=dict)
    observed_since: str | None = None


class OnboardingContext(BaseModel):
    team_name: str
    current_role: str
    active_spaces: int | None = None
    active_seats: int | None = None
    max_seats: int | None = None
    license_health: str | None = None
    license_expires_at: str | None = None
    grace_until: str | None = None
    current_public_entry: str | None = None
    account_creation_policy: str | None = None
    auth_methods: list[str] = Field(default_factory=list)
    email_source: Literal["team", "env", "none", "unknown"] = "unknown"
    kb_feature_licensed: bool | None = None
    optional_capabilities: dict[str, CapabilityState] = Field(default_factory=dict)
    runtime: RuntimeSignal = Field(default_factory=RuntimeSignal)


class TeamOnboardingStatusResponse(BaseModel):
    schema_version: Literal[1] = 1
    team_id: str
    milestone: Literal["core_configuring", "core_ready"]
    partial: bool
    generated_at: str
    core: CoreReadiness
    items: list[ReadinessItem]
    context: OnboardingContext


class TeamOnboardingReadinessService:
    """Aggregate local Team facts without external probes or business writes."""

    def __init__(self, request: Any, *, user_id: str, team_id: str, role: str) -> None:
        self.request = request
        self.app_state = request.app.state
        self.user_id = user_id
        self.team_id = team_id
        self.role = role
        self.partial = False
        self.results: dict[str, Any] = {}
        self.context_patch: dict[str, Any] = {}

    async def collect(self) -> TeamOnboardingStatusResponse:
        store = self.app_state.user_store
        team = await store.get_team(self.team_id)
        if team is None:
            raise RuntimeError("current Team is unavailable")

        entitlement_snapshot = None
        kb_licensed: bool | None = None
        try:
            entitlement_snapshot = await self.app_state.entitlements.get(self.team_id)
            kb_licensed = self.app_state.entitlements.is_feature_licensed_from_snapshot(
                entitlement_snapshot,
                "kb",
            )
            self.results["license"] = self._license_item(entitlement_snapshot)
        except Exception as exc:  # noqa: BLE001
            self.partial = True
            self._log_adapter_failure("license", exc)
            self.results["license"] = self._unknown_item("license_active")

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(self._run_adapter, "public_entry", self._public_entry)
            task_group.start_soon(self._run_adapter, "model", lambda: self._model_bundle(kb_licensed))
            task_group.start_soon(self._run_adapter, "space", self._space)
            task_group.start_soon(self._run_adapter, "turn", self._first_turn)
            task_group.start_soon(self._run_adapter, "datarc", self._datarc)
            task_group.start_soon(self._run_adapter, "email", self._email)
            task_group.start_soon(
                self._run_adapter,
                "active_seats",
                lambda: store.count_active_seats(self.team_id),
            )
            task_group.start_soon(
                self._run_adapter,
                "admin",
                lambda: self._secondary_admin(entitlement_snapshot),
            )

        items_by_id: dict[str, ReadinessItem] = {
            self.results["license"].id: self.results["license"],
        }
        for result_key in ("public_entry", "space", "turn", "datarc", "email", "admin"):
            result = self.results.get(result_key)
            if isinstance(result, ReadinessItem):
                items_by_id[result.id] = result
        model_result = self.results.get("model")
        if isinstance(model_result, dict):
            for item in model_result["items"]:
                items_by_id[item.id] = item

        for item_id in _ITEM_ORDER:
            if item_id not in items_by_id:
                items_by_id[item_id] = self._unknown_item(
                    item_id,
                    kb_entitlement_unknown=item_id == "knowledge_base_ready" and kb_licensed is None,
                )
        items = [items_by_id[item_id] for item_id in _ITEM_ORDER]
        core_satisfied = sum(1 for item in items if item.id in _CORE_IDS and item.satisfied)

        model_optional = (
            model_result.get("optional_capabilities", {})
            if isinstance(model_result, dict)
            else {
                "image_generation": "unknown",
                "web_search": "unknown",
                "primary_vision": "unknown",
                "primary_think": "unknown",
            }
        )
        active_spaces = self.context_patch.get("active_spaces")
        current_public_entry = self.context_patch.get("current_public_entry")
        email_source = self.context_patch.get("email_source", "unknown")
        active_seats_result = self.results.get("active_seats")
        active_seats = active_seats_result if isinstance(active_seats_result, int) else None

        policy = getattr(self.app_state, "auth_policy", None)
        context = OnboardingContext(
            team_name=team.name,
            current_role=self.role,
            active_spaces=active_spaces,
            active_seats=active_seats,
            max_seats=getattr(entitlement_snapshot, "max_seats", None),
            license_health=getattr(entitlement_snapshot, "health", None),
            license_expires_at=getattr(entitlement_snapshot, "expires_at", None),
            grace_until=getattr(entitlement_snapshot, "grace_until", None),
            current_public_entry=current_public_entry,
            account_creation_policy=getattr(
                getattr(policy, "account_creation", None),
                "value",
                None,
            ),
            auth_methods=list(getattr(policy, "auth_methods", ()) or ()),
            email_source=email_source,
            kb_feature_licensed=kb_licensed,
            optional_capabilities=model_optional,
            runtime=_runtime_signal(self.team_id),
        )
        return TeamOnboardingStatusResponse(
            team_id=self.team_id,
            milestone="core_ready" if core_satisfied == 5 else "core_configuring",
            partial=self.partial,
            generated_at=_now(),
            core=CoreReadiness(satisfied=core_satisfied),
            items=items,
            context=context,
        )

    async def _run_adapter(
        self,
        name: str,
        call: Callable[[], Awaitable[Any]],
    ) -> None:
        started = time.monotonic()
        try:
            with anyio.fail_after(1.5):
                self.results[name] = await call()
        except BaseException as exc:
            if isinstance(exc, anyio.get_cancelled_exc_class()):
                raise
            if not isinstance(exc, Exception):
                raise
            self.partial = True
            metrics.inc(
                "team_onboarding_adapter_failures_total",
                labels={
                    "adapter": name,
                    "kind": "timeout" if isinstance(exc, TimeoutError) else "error",
                },
            )
            self._log_adapter_failure(name, exc)
        finally:
            metrics.observe(
                "team_onboarding_adapter_duration_seconds",
                time.monotonic() - started,
                labels={"adapter": name},
            )

    def _license_item(self, snapshot: Any) -> ReadinessItem:
        health = str(snapshot.health)
        if health == "active":
            return self._item("license_active", "ready", True, "license_active")
        if health == "grace":
            return self._item("license_active", "warning", True, "license_in_grace")
        reason = (
            health
            if health
            in {
                "over_grace",
                "suspended",
                "tamper",
                "clock_tamper",
                "needs_license",
            }
            else "license_state_unavailable"
        )
        return self._item("license_active", "blocked", False, reason)

    async def _public_entry(self) -> ReadinessItem:
        entries = await self.app_state.public_entry_store.list_for_team(self.team_id)
        active = {entry.id: entry for entry in entries if entry.status == "active"}
        if not active:
            return self._item(
                "public_entry_ready",
                "missing",
                False,
                "no_active_public_entry",
            )
        context = current_public_access_context()
        matched = active.get(context.entry_id) if context is not None and context.team_id == self.team_id and context.entry_id else None
        if matched is None:
            return self._item(
                "public_entry_ready",
                "blocked",
                False,
                "current_entry_unmatched",
            )
        item = self._item(
            "public_entry_ready",
            "ready",
            True,
            "public_entry_ready",
        )
        self.context_patch["current_public_entry"] = matched.app_root
        return item

    async def _space(self) -> ReadinessItem:
        store = self.app_state.user_store
        active_count = await store.count_active_workspaces(self.team_id)
        memberships = await store.list_workspaces_for_user(
            self.user_id,
            team_id=self.team_id,
        )
        if active_count == 0:
            item = self._item("space_ready", "missing", False, "no_active_space")
        elif not memberships:
            item = self._item("space_ready", "blocked", False, "admin_has_no_space")
        else:
            item = self._item("space_ready", "ready", True, "space_ready")
        self.context_patch["active_spaces"] = active_count
        return item

    async def _first_turn(self) -> ReadinessItem:
        facts = await self.app_state.session_store.first_turn_readiness_facts_for_team(
            self.team_id,
        )
        if facts.has_successful_turn:
            return self._item(
                "first_turn_succeeded",
                "ready",
                True,
                "successful_turn_exists",
            )
        if facts.has_any_normal_turn and not facts.has_completed_turn:
            return self._item(
                "first_turn_succeeded",
                "missing",
                False,
                "no_completed_turn",
            )
        return self._item(
            "first_turn_succeeded",
            "missing",
            False,
            "no_successful_turn",
        )

    async def _datarc(self) -> ReadinessItem:
        connections = await self.app_state.datarc_connection_store.list_connections(
            self.team_id,
        )
        if connections:
            return self._item(
                "datarc_connection_configured",
                "ready",
                True,
                "datarc_connection_configured",
            )
        return self._item(
            "datarc_connection_configured",
            "missing",
            False,
            "datarc_connection_missing",
        )

    async def _email(self) -> ReadinessItem:
        config = await resolve_smtp_config(
            self.team_id,
            self.app_state.user_store,
            strict_team=True,
        )
        source = config.source if config is not None else "none"
        if source == "team":
            item = self._item(
                "email_delivery_configured",
                "ready",
                True,
                "team_email_configured",
            )
        elif source == "env":
            item = self._item(
                "email_delivery_configured",
                "ready",
                True,
                "env_email_configured",
            )
        else:
            item = self._item(
                "email_delivery_configured",
                "missing",
                False,
                "email_not_configured",
                can_act=team_email_config_enabled(),
            )
        self.context_patch["email_source"] = source
        return item

    async def _secondary_admin(self, snapshot: Any | None) -> ReadinessItem:
        if snapshot is None:
            return self._unknown_item("secondary_admin_present")
        if snapshot.max_seats is not None and int(snapshot.max_seats) <= 1:
            return self._item(
                "secondary_admin_present",
                "not_applicable",
                True,
                "single_seat_license",
            )
        admin_ids = await self.app_state.user_store.list_team_users_by_roles(
            self.team_id,
            {"owner", "team_admin"},
            include_creator=True,
        )
        if len(admin_ids) >= 2:
            return self._item(
                "secondary_admin_present",
                "ready",
                True,
                "secondary_admin_present",
            )
        return self._item(
            "secondary_admin_present",
            "missing",
            False,
            "secondary_admin_missing",
        )

    async def _model_bundle(self, kb_licensed: bool | None) -> dict[str, Any]:
        store = self.app_state.user_store
        raw_config, _version = await store.get_team_llm_config_with_version(self.team_id)
        if not raw_config:
            model = self._item(
                "llm_primary_configured",
                "missing",
                False,
                "model_provider_missing",
                checks=[
                    _check("llm_provider_present", "missing", True, False, "model_provider_missing"),
                ],
            )
            fallback = self._item(
                "llm_fallback_configured",
                "missing",
                False,
                "fallback_model_missing",
            )
            kb = await self._safe_kb_item({}, kb_licensed)
            return {
                "items": [model, kb, fallback],
                "optional_capabilities": _disabled_optional_capabilities(),
            }
        config = raw_config if is_v2_config(raw_config) else migrate_legacy_config_to_v2(raw_config)
        providers = config.get("providers") or {}
        primary_spec = _primary_spec(config)
        if not providers:
            model = self._item(
                "llm_primary_configured",
                "missing",
                False,
                "model_provider_missing",
            )
            fallback = self._item(
                "llm_fallback_configured",
                "missing",
                False,
                "fallback_model_missing",
            )
            return {
                "items": [model, await self._safe_kb_item(config, kb_licensed), fallback],
                "optional_capabilities": _optional_capabilities(config, None),
            }
        if primary_spec is None:
            model = self._item(
                "llm_primary_configured",
                "missing",
                False,
                "primary_model_missing",
            )
            fallback = self._fallback_item(config, config_valid=False)
            return {
                "items": [model, await self._safe_kb_item(config, kb_licensed), fallback],
                "optional_capabilities": _optional_capabilities(config, None),
            }

        try:
            validate_v2_config(
                _model_routing_config(config),
                ssrf_resolver=_public_ssrf_resolver,
                allow_legacy_duplicate_fallbacks=True,
            )
            config_valid = True
        except TeamLlmConfigError:
            config_valid = False
        primary_provider = str(primary_spec.get("provider") or "")
        primary_model = str(primary_spec.get("model") or "")
        diagnostics = diagnose_model_ref(
            providers,
            primary_provider,
            primary_model,
        )
        checks = [
            _check(
                "llm_provider_present",
                "ready" if providers else "missing",
                True,
                bool(providers),
                "model_provider_present" if providers else "model_provider_missing",
            ),
            _check(
                "llm_provider_credential_present",
                "ready" if diagnostics.provider_has_secret else "blocked",
                True,
                diagnostics.provider_has_secret,
                "primary_model_configured" if diagnostics.provider_has_secret else "primary_provider_credential_missing",
            ),
            _check(
                "llm_primary_selected",
                "ready" if diagnostics.model_declared else "blocked",
                True,
                diagnostics.model_declared,
                "primary_model_configured" if diagnostics.model_declared else "primary_model_reference_invalid",
            ),
            _check(
                "llm_primary_chat_capable",
                "ready" if "chat" in diagnostics.capabilities else "blocked",
                True,
                "chat" in diagnostics.capabilities,
                "primary_model_configured" if "chat" in diagnostics.capabilities else "primary_model_capability_invalid",
            ),
            _check(
                "llm_primary_tool_capable",
                "ready" if "tool" in diagnostics.capabilities else "blocked",
                True,
                "tool" in diagnostics.capabilities,
                "primary_model_configured" if "tool" in diagnostics.capabilities else "primary_model_capability_invalid",
            ),
        ]
        if not config_valid:
            model = self._item(
                "llm_primary_configured",
                "blocked",
                False,
                "team_model_config_invalid",
                checks=checks,
            )
        elif not diagnostics.provider_exists or not diagnostics.model_declared:
            model = self._item(
                "llm_primary_configured",
                "blocked",
                False,
                "primary_model_reference_invalid",
                checks=checks,
            )
        elif not diagnostics.provider_has_secret:
            model = self._item(
                "llm_primary_configured",
                "blocked",
                False,
                "primary_provider_credential_missing",
                checks=checks,
            )
        elif not {"chat", "tool"}.issubset(diagnostics.capabilities):
            model = self._item(
                "llm_primary_configured",
                "blocked",
                False,
                "primary_model_capability_invalid",
                checks=checks,
            )
        else:
            auxiliary_degraded, judge_uses_primary = _auxiliary_tier_state(config)
            if auxiliary_degraded:
                model = self._item(
                    "llm_primary_configured",
                    "warning",
                    True,
                    "auxiliary_tier_degraded",
                    checks=checks,
                )
            elif judge_uses_primary:
                model = self._item(
                    "llm_primary_configured",
                    "warning",
                    True,
                    "judge_uses_primary",
                    checks=checks,
                )
            else:
                model = self._item(
                    "llm_primary_configured",
                    "ready",
                    True,
                    "primary_model_configured",
                    checks=checks,
                )
        fallback = self._fallback_item(config, config_valid=config_valid)
        kb = await self._safe_kb_item(config, kb_licensed)
        return {
            "items": [model, kb, fallback],
            "optional_capabilities": _optional_capabilities(config, diagnostics),
        }

    def _fallback_item(
        self,
        config: dict[str, Any],
        *,
        config_valid: bool,
    ) -> ReadinessItem:
        refs = _fallback_refs(config)
        if not refs:
            return self._item(
                "llm_fallback_configured",
                "missing",
                False,
                "fallback_model_missing",
            )
        if not config_valid:
            return self._item(
                "llm_fallback_configured",
                "warning",
                False,
                "fallback_model_invalid",
            )
        if is_tier_config(config):
            refs = [{"provider": ref.provider, "model": ref.model} for ref in resolve_tiers(config).primary_fallbacks]
            if not refs:
                return self._item(
                    "llm_fallback_configured",
                    "missing",
                    False,
                    "fallback_model_missing",
                )
        providers = config.get("providers") or {}
        for ref in refs:
            diagnostics = diagnose_model_ref(
                providers,
                str(ref.get("provider") or ""),
                str(ref.get("model") or ""),
            )
            if (
                not diagnostics.provider_exists
                or not diagnostics.provider_has_secret
                or not diagnostics.model_declared
                or not {"chat", "tool"}.issubset(diagnostics.capabilities)
            ):
                return self._item(
                    "llm_fallback_configured",
                    "warning",
                    False,
                    "fallback_model_unusable",
                )
        return self._item(
            "llm_fallback_configured",
            "ready",
            True,
            "fallback_model_configured",
        )

    async def _kb_item(
        self,
        config: dict[str, Any],
        kb_licensed: bool | None,
    ) -> ReadinessItem:
        if kb_licensed is None:
            return self._unknown_item(
                "knowledge_base_ready",
                kb_entitlement_unknown=True,
            )
        if not kb_licensed:
            return self._item(
                "knowledge_base_ready",
                "not_applicable",
                True,
                "kb_not_licensed",
                required=False,
                required_scope=None,
                checks=[
                    _check(
                        "kb_feature_licensed",
                        "not_applicable",
                        False,
                        True,
                        "kb_not_licensed",
                    ),
                ],
            )
        diagnostics = diagnose_kb_runtime_config(config)
        embedding = diagnostics.capabilities["embed"]
        pending_embedding = diagnostics.pending_embedding
        checks = [
            _check(
                "kb_feature_licensed",
                "ready",
                True,
                True,
                "kb_embedding_configured",
            ),
        ]
        has_old_index = bool(diagnostics.index_version and diagnostics.has_active_embedding)
        if diagnostics.pending_state:
            pending_valid = bool(pending_embedding is not None and pending_embedding.valid)
            embedding_available = has_old_index or pending_valid
            checks.extend(
                [
                    _check(
                        "kb_embedding_configured",
                        "ready" if embedding_available else "blocked",
                        True,
                        embedding_available,
                        "kb_embedding_configured" if embedding_available else "kb_embedding_config_invalid",
                    ),
                    _check(
                        "kb_embedding_index_ready",
                        "warning" if has_old_index else "missing",
                        True,
                        has_old_index,
                        "kb_reindex_in_progress" if has_old_index else "kb_initial_reindex_pending",
                    ),
                ],
            )
            if not pending_valid and diagnostics.pending_state != "failed":
                return self._item(
                    "knowledge_base_ready",
                    "warning" if has_old_index else "blocked",
                    has_old_index,
                    "kb_embedding_config_invalid",
                    required=True,
                    required_scope="capability",
                    checks=checks,
                )
            if diagnostics.pending_state == "failed":
                return self._item(
                    "knowledge_base_ready",
                    "warning" if has_old_index else "blocked",
                    has_old_index,
                    "kb_reindex_failed",
                    required=True,
                    required_scope="capability",
                    checks=checks,
                )
            return self._item(
                "knowledge_base_ready",
                "warning",
                has_old_index,
                "kb_reindex_in_progress" if has_old_index else "kb_initial_reindex_pending",
                required=True,
                required_scope="capability",
                checks=checks,
            )
        if not embedding.configured:
            checks.append(
                _check(
                    "kb_embedding_configured",
                    "missing",
                    True,
                    False,
                    "kb_embedding_missing",
                ),
            )
            return self._item(
                "knowledge_base_ready",
                "missing",
                False,
                "kb_embedding_missing",
                required=True,
                required_scope="capability",
                checks=checks,
            )
        if not embedding.valid:
            checks.append(
                _check(
                    "kb_embedding_configured",
                    "blocked",
                    True,
                    False,
                    "kb_embedding_config_invalid",
                ),
            )
            return self._item(
                "knowledge_base_ready",
                "blocked",
                False,
                "kb_embedding_config_invalid",
                required=True,
                required_scope="capability",
                checks=checks,
            )

        stats = await self.app_state.kb_files_store.readiness_stats_for_team(
            self.team_id,
        )
        workspace_ids = await self.app_state.user_store.list_active_workspace_ids(
            self.team_id,
        )
        row_extractor_needed = await self.app_state.kb_doc_type_store.has_many_auto_extract_for_workspaces(
            workspace_ids,
        )
        content_present = stats.active_file_count > 0
        checks.extend(
            [
                _check(
                    "kb_embedding_configured",
                    "ready",
                    True,
                    True,
                    "kb_embedding_configured",
                ),
                _check(
                    "kb_content_present",
                    "ready" if content_present else "missing",
                    False,
                    content_present,
                    "kb_embedding_configured" if content_present else "kb_content_missing",
                ),
            ],
        )

        if content_present and not has_old_index:
            checks.append(
                _check(
                    "kb_embedding_index_ready",
                    "blocked",
                    True,
                    False,
                    "kb_embedding_config_invalid",
                ),
            )
            return self._item(
                "knowledge_base_ready",
                "blocked",
                False,
                "kb_embedding_config_invalid",
                required=True,
                required_scope="capability",
                checks=checks,
            )
        if content_present:
            checks.append(
                _check(
                    "kb_embedding_index_ready",
                    "ready",
                    True,
                    True,
                    "kb_embedding_configured",
                ),
            )

        vision_needed = stats.image_file_count > 0 or stats.scanned_pdf_evidence_count > 0
        vision = diagnostics.capabilities["vision"]
        row_extractor = diagnostics.capabilities["row_extractor"]
        if vision_needed and not vision.valid:
            reason = "kb_conditional_model_invalid" if vision.configured else "kb_conditional_model_missing"
            return self._item(
                "knowledge_base_ready",
                "blocked" if vision.configured else "warning",
                False,
                reason,
                required=True,
                required_scope="capability",
                checks=checks
                + [
                    _check(
                        "kb_vision_configured",
                        "blocked" if vision.configured else "missing",
                        True,
                        False,
                        reason,
                    ),
                ],
            )
        if vision_needed:
            checks.append(
                _check(
                    "kb_vision_configured",
                    "ready",
                    True,
                    True,
                    "kb_embedding_configured",
                ),
            )
        if row_extractor_needed and not row_extractor.valid:
            reason = "kb_conditional_model_invalid" if row_extractor.configured else "kb_conditional_model_missing"
            return self._item(
                "knowledge_base_ready",
                "blocked" if row_extractor.configured else "warning",
                False,
                reason,
                required=True,
                required_scope="capability",
                checks=checks
                + [
                    _check(
                        "kb_row_extractor_configured",
                        "blocked" if row_extractor.configured else "missing",
                        True,
                        False,
                        reason,
                    ),
                ],
            )
        if row_extractor_needed:
            checks.append(
                _check(
                    "kb_row_extractor_configured",
                    "ready",
                    True,
                    True,
                    "kb_embedding_configured",
                ),
            )
        reranker = diagnostics.capabilities["rerank"]
        if reranker.configured and not reranker.valid:
            return self._item(
                "knowledge_base_ready",
                "warning",
                True,
                "kb_conditional_model_invalid",
                required=True,
                required_scope="capability",
                checks=checks
                + [
                    _check(
                        "kb_rerank_configured",
                        "warning",
                        False,
                        False,
                        "kb_conditional_model_invalid",
                    ),
                ],
            )
        if reranker.configured:
            checks.append(
                _check(
                    "kb_rerank_configured",
                    "ready",
                    False,
                    True,
                    "kb_embedding_configured",
                ),
            )
        return self._item(
            "knowledge_base_ready",
            "ready",
            True,
            "kb_embedding_configured",
            required=True,
            required_scope="capability",
            checks=checks,
            href="/knowledge?from=setup",
        )

    async def _safe_kb_item(
        self,
        config: dict[str, Any],
        kb_licensed: bool | None,
    ) -> ReadinessItem:
        try:
            return await self._kb_item(config, kb_licensed)
        except Exception as exc:  # noqa: BLE001
            self.partial = True
            self._log_adapter_failure("knowledge_base", exc)
            return self._unknown_item(
                "knowledge_base_ready",
                kb_entitlement_unknown=kb_licensed is None,
            )

    def _item(
        self,
        item_id: str,
        state: ReadinessState,
        satisfied: bool,
        reason_code: str,
        *,
        required: bool | None = None,
        required_scope: RequiredScope | None = None,
        checks: list[ReadinessCheck] | None = None,
        href: str | None = None,
        can_act: bool | None = None,
    ) -> ReadinessItem:
        group = _ITEM_GROUP[item_id]
        if required is None:
            required = group == "core"
        if required_scope is None and group == "core":
            required_scope = "core"
        target = href if href is not None else _ACTIONS.get(item_id)
        if item_id == "license_active" and getattr(self.app_state, "edition", "business") == "community":
            target = "/setup/"
        roles = list(_ACTION_ROLES[item_id])
        actionable = bool(target and self.role in roles)
        if can_act is not None:
            actionable = actionable and can_act
        return ReadinessItem(
            id=item_id,
            group=group,
            state=state,
            required=required,
            required_scope=required_scope,
            satisfied=satisfied,
            blocking=group == "core" and required and not satisfied,
            reason_code=reason_code,
            required_roles=roles,
            can_act=actionable,
            observed_at=_now(),
            action=ReadinessAction(href=target) if target else None,
            checks=checks or [],
        )

    def _unknown_item(
        self,
        item_id: str,
        *,
        kb_entitlement_unknown: bool = False,
    ) -> ReadinessItem:
        return self._item(
            item_id,
            "unknown",
            False,
            "kb_entitlement_unavailable" if kb_entitlement_unknown else _ITEM_UNAVAILABLE_REASON[item_id],
            required=False if item_id == "knowledge_base_ready" and kb_entitlement_unknown else None,
            required_scope=None,
        )

    @staticmethod
    def _log_adapter_failure(name: str, exc: BaseException) -> None:
        logger.warning(
            "team_onboarding.adapter_failed adapter=%s exception=%s",
            name,
            type(exc).__name__,
        )


def _check(
    check_id: str,
    state: ReadinessState,
    required: bool,
    satisfied: bool,
    reason_code: str,
) -> ReadinessCheck:
    return ReadinessCheck(
        id=check_id,
        state=state,
        required=required,
        satisfied=satisfied,
        reason_code=reason_code,
    )


def _primary_spec(config: dict[str, Any]) -> dict[str, Any] | None:
    if is_tier_config(config):
        spec = (config.get("tiers") or {}).get("primary")
    else:
        spec = ((config.get("agents") or {}).get("lead") or {}).get("primary")
    return spec if isinstance(spec, dict) else None


def _model_routing_config(config: dict[str, Any]) -> dict[str, Any]:
    """Exclude independently diagnosed optional capability sections."""
    routing = {
        "schema_version": config.get("schema_version"),
        "providers": config.get("providers"),
    }
    if is_tier_config(config):
        routing["tiers"] = config.get("tiers")
    else:
        routing["agents"] = config.get("agents")
    return routing


def _fallback_refs(config: dict[str, Any]) -> list[dict[str, Any]]:
    if is_tier_config(config):
        refs = (config.get("tiers") or {}).get("primary_fallback") or []
    else:
        refs = ((config.get("agents") or {}).get("lead") or {}).get("fallback") or []
    return [ref for ref in refs if isinstance(ref, dict)]


def _auxiliary_tier_state(config: dict[str, Any]) -> tuple[bool, bool]:
    if not is_tier_config(config):
        return False, False
    providers = config.get("providers") or {}
    tiers = config.get("tiers") or {}
    for tier_name in ("fast", "judge"):
        spec = tiers.get(tier_name)
        if not isinstance(spec, dict) or spec.get("follow") or spec.get("auto"):
            continue
        diagnostics = diagnose_model_ref(
            providers,
            str(spec.get("provider") or ""),
            str(spec.get("model") or ""),
        )
        if (
            not diagnostics.provider_exists
            or not diagnostics.provider_has_secret
            or not diagnostics.model_declared
            or "chat" not in diagnostics.capabilities
        ):
            return True, False
    return False, resolve_tiers(config).judge_used_primary_fallback


def _optional_capabilities(
    config: dict[str, Any],
    primary: Any | None,
) -> dict[str, CapabilityState]:
    return {
        "image_generation": _section_capability_state(
            config,
            section="media",
            capability="image_gen",
        ),
        "web_search": _section_capability_state(
            config,
            section="search",
            capability="web_search",
        ),
        "primary_vision": ("enabled" if primary is not None and "vision" in primary.capabilities else "disabled"),
        "primary_think": ("enabled" if primary is not None and "think" in primary.capabilities else "disabled"),
        # credential-readiness R8 (PRD §6.6 B8): include three KB capabilities in the **full lens**,
        # computed unconditionally (what is still missing), distinct from _kb_item's conditional readiness criterion
        # (whether it is usable; check vision only when stored images exist). Neither substitutes for the other.
        **_kb_capability_states(config),
    }


def _kb_capability_states(config: dict[str, Any]) -> dict[str, CapabilityState]:
    """kb rerank/vision/row_extractor 的四态映射（diagnose 的 configured/valid 折算）。"""
    try:
        diagnostics = diagnose_kb_runtime_config(config)
    except Exception:  # Structural anomalies → all three invalid (same direction as _section_capability_state).
        return {"kb_rerank": "invalid", "kb_vision": "invalid", "kb_row_extractor": "invalid"}
    out: dict[str, CapabilityState] = {}
    for key, cap in (("kb_rerank", "rerank"), ("kb_vision", "vision"), ("kb_row_extractor", "row_extractor")):
        d = diagnostics.capabilities.get(cap)
        if d is None or not d.configured:
            out[key] = "disabled"
        elif d.valid:
            out[key] = "enabled"
        else:
            out[key] = "invalid"
    return out


def _runtime_signal(team_id: str) -> RuntimeSignal:
    """R8b：进程内 refusal counter → RuntimeSignal 快照；观测面失败绝不拖垮就绪投影。"""
    try:
        by_reason: dict[str, int] = {}
        total = 0
        for labels, value in metrics.counter_items("bowhead_llm_refusal_total"):
            # This team + unattributed (legacy rows without team labels also fall back to unattributed).
            team = labels.get("team") or "unattributed"
            if team not in (team_id, "unattributed"):
                continue
            n = int(value)
            total += n
            reason = labels.get("reason") or "unknown"
            by_reason[reason] = by_reason.get(reason, 0) + n
        since = datetime.fromtimestamp(metrics.get_registry().started_at, tz=UTC).isoformat()
        return RuntimeSignal(
            llm_refusals_total=total,
            llm_refusals_by_reason=by_reason,
            observed_since=since,
        )
    except Exception:  # noqa: BLE001
        return RuntimeSignal()


def _disabled_optional_capabilities() -> dict[str, CapabilityState]:
    return {
        "image_generation": "disabled",
        "web_search": "disabled",
        "primary_vision": "disabled",
        "primary_think": "disabled",
        # R8 (review V10): unconfigured teams most need disabled-capability lists; all three keys must exist.
        "kb_rerank": "disabled",
        "kb_vision": "disabled",
        "kb_row_extractor": "disabled",
    }


def _section_capability_state(
    config: dict[str, Any],
    *,
    section: str,
    capability: str,
) -> CapabilityState:
    body = config.get(section) or {}
    if not isinstance(body, dict):
        return "invalid"
    active = body.get("active") or {}
    models = body.get("models") or {}
    if not isinstance(active, dict) or not isinstance(models, dict):
        return "invalid"
    model_name = active.get(capability)
    if not model_name:
        return "disabled"
    spec = models.get(model_name)
    providers = config.get("providers") or {}
    if not isinstance(spec, dict) or spec.get("capability") != capability:
        return "invalid"
    provider_name = spec.get("provider")
    provider = providers.get(provider_name) if isinstance(providers, dict) else None
    protocols = {
        "media": {"dashscope_async", "ark_sync"},
        "search": {"dashscope_compat", "dashscope_native"},
    }
    if (
        not isinstance(provider_name, str)
        or not isinstance(provider, dict)
        or not isinstance(provider.get("base_url"), str)
        or not provider["base_url"].strip()
        or not provider_has_team_secret(providers, provider_name)
        or not str(spec.get("real_id") or spec.get("model") or "").strip()
        or spec.get("protocol") not in protocols[section]
    ):
        return "invalid"
    return "enabled"


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _public_ssrf_resolver(_host: str) -> tuple[str, ...]:
    """Keep readiness validation local while preserving SSRF structure checks."""
    return ("93.184.216.34",)
