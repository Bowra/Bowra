"""Router 主类。

按 agent × profile 选 provider，处理 fallback / 熔断 / cost cap。
Sprint 0 实现最小可用版本；M1 补全：
- 真正的 circuit breaker（见 TD §5.8）
- Cost cap 检查
- Prompt cache 观测
- Fallback 链路审计事件

上层 Agent 只调 ``router.invoke(agent_type, req, profile=None)``，
不感知 provider 细节。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from time import monotonic
from typing import TYPE_CHECKING, Any

from bowhead_router.agent_tiers import PRIMARY_TIER_AGENTS
from bowhead_router.errors import (
    InvocationOutcomeUnknownError,
    InvocationTargetsExhaustedError,
    LLMTargetFailure,
    ProviderFailureKind,
    RetryPolicy,
    StreamingInterruptedError,
    StreamingTargetsExhaustedError,
    target_failure,
)
from bowhead_router.inline_reasoning import InlineReasoningStream, split_inline_reasoning
from bowhead_router.input_admission import LLMInputAdmissionError, admit_request_input
from bowhead_router.invocation import physical_invocation_required, physical_observation_active
from bowhead_router.model_clock import model_now
from bowhead_router.retry import DEFAULT_MAX_RETRIES, RetryNotice, adapter_timeout, admit_retry, attempt_fits
from bowhead_router.types import LLMChunk, LLMRequest, LLMResponse
from bowhead_router.usage import record_usage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from bowhead_router.context_policy import TargetContextBudget
    from bowhead_router.registry import ProviderRegistry


def _get_logger() -> logging.Logger:
    return logging.getLogger("bowhead_router.router")


def _unregistered_target_failure(
    *,
    provider: str,
    model: str,
    mode: str,
    target_index: int,
) -> LLMTargetFailure:
    """Represent a configured-but-unregistered target without a fake HTTP attempt."""

    return LLMTargetFailure(
        provider=provider,
        model=model,
        provider_family="custom",
        mode=mode,
        target_index=target_index,
        attempt=0,
        kind=ProviderFailureKind.CONFIGURATION,
        retryable=False,
    )


def _skip_undeclared_admission_fallback(req: LLMRequest, *, target_index: int, context_window: int | None) -> bool:
    """A required-input fallback without a window cannot be admitted or called."""
    return target_index > 1 and context_window is None and getattr(req.input_admission, "requires_declared_context_window", False) is True


async def _invoke_with_retry(
    adapter: Any,
    req: LLMRequest,
    *,
    provider_name: str,
    model: str,
    max_attempts: int = 2,
    base_delay_s: float = 2.0,
    failures: list[LLMTargetFailure] | None = None,
    target_index: int = 1,
    context_window_reader: Callable[[], int | None] | None = None,
) -> LLMResponse:
    """Invoke adapter with retry on retryable errors (0.13.2).

    Why: 8686 dogfood (2026-05-12) showed 18/31 turns hit `429 HTTP` transient
    rate limits. Without retry, single 429 burst → immediate fall-over to next
    provider (often also 429 from same burst). Short retry (1 attempt, 2s
    backoff) catches transient bursts without delaying real provider failures.

    Only retries when `e.retryable=True` (adapter classifies 408/429/5xx as
    retryable). Account-quota-exhausted is technically retryable but second
    attempt will also 429 — falls over to next provider after exhausted.
    """
    last: Exception | None = None
    for attempt in range(max_attempts):
        await admit_request_input(
            req,
            provider=provider_name,
            model=model,
            context_window=context_window_reader() if context_window_reader else None,
            adapter_supported=getattr(adapter, "supports_input_admission", False) is True,
        )
        try:
            return await adapter.invoke(req)
        except (InvocationOutcomeUnknownError, LLMInputAdmissionError):
            raise
        except Exception as e:
            last = e
            failure = target_failure(
                e,
                adapter=adapter,
                provider=provider_name,
                model=model,
                mode="invoke",
                target_index=target_index,
                attempt=attempt + 1,
            )
            if failures is not None:
                failures.append(failure)
            retryable = failure.retryable
            if not retryable or attempt == max_attempts - 1:
                raise
            delay = base_delay_s * (2**attempt)
            notice = RetryNotice(
                retry=attempt + 1,
                max_retries=max_attempts - 1,
                kind=failure.kind,
                delay_s=delay,
                mode="invoke",
                provider=provider_name,
                model=model,
                attempt_timeout_s=adapter_timeout(adapter),
            )
            if not await admit_retry(notice):
                raise
            _get_logger().warning(
                "router.retry_attempt provider=%s model=%s attempt=%d/%d delay=%.1fs kind=%s status=%s",
                provider_name,
                model,
                attempt + 1,
                max_attempts,
                delay,
                failure.kind.value,
                failure.status_code,
            )
            await asyncio.sleep(delay)
    assert last is not None  # unreachable: loop either returns or raises
    raise last


class NoBindingError(KeyError):
    """No model is assigned to this agent type (and profile) in the Router.

    A ``KeyError`` for existing callers; callers that explain the failure (Agent Eval) read
    ``agent_type`` to name the missing assignment.
    """

    def __init__(self, agent_type: str, profile: str | None = None) -> None:
        super().__init__(f"No binding for agent_type={agent_type!r} profile={profile!r}")
        self.agent_type = agent_type
        self.profile = profile


@dataclass
class AgentBinding:
    """Agent × Model 绑定（TD §4.3.5）。"""

    agent_type: str  # lead | modeler | analyst | echo | judge
    profile: str | None = None  # analyst profile
    primary: tuple[str, str] = ("mock", "mock-model")  # (provider_name, model)
    fallback: list[tuple[str, str]] = field(default_factory=list)
    extended_thinking: bool = False
    thinking_budget_tokens: int = 0
    max_output_tokens: int | None = None
    routing_policy: str = "quality_first"  # quality_first | cost_optimized | latency_optimized


@dataclass
class UserModelPref:
    """M2.3: 用户在 Composer model picker 选的偏好.

    设计文档: docs/M157-routing-user-pref-design.md
    优先级: admin override > user_pref (仅 lead/composer) > workspace.default > yaml binding.
    """

    provider: str
    model: str

    @classmethod
    def from_request_fields(
        cls,
        preferred_provider: str | None,
        preferred_model: str | None,
    ) -> UserModelPref | None:
        """从 TurnRequest.preferred_provider+model 字段构造; 任一缺失返 None."""
        if not preferred_provider or not preferred_model:
            return None
        return cls(provider=preferred_provider, model=preferred_model)


# Decision C (model tiers): user_pref covers the entire primary tier (lead/modeler/narrative/analyst).
# This is the router's second filter (which agent_type values may honor user_pref); the caller (subagent.py)
# decides whether to **construct** user_pref for the broader tier and passes it to primary-tier executors.
# (The phantom composer was removed: no subagent uses it as llm_agent_type, and _LEGACY_AGENT_ALIASES also maps it →narrative.)
USER_PREF_APPLICABLE_AGENTS = PRIMARY_TIER_AGENTS


class Router:
    """上层入口。"""

    def __init__(self, registry: ProviderRegistry, *, owner_team_id: str | None = None) -> None:
        self._registry = registry
        self.configuration_version: int | None = None
        self._bindings: dict[tuple[str, str | None], AgentBinding] = {}
        # credit gap23 step 1: **attribution follows the router, not the call site**. The sole factory
        # (core TeamRouterRegistry) builds team routers by team_id; team identity is established at construction.
        # Fix it here so `record_usage` can read it directly without requiring any declarations at call sites.
        # Default None = process-level assembly routers (RefusalAdapter in prod, refusing invocation) and test doubles.
        self.owner_team_id = owner_team_id
        # Context engineering sprint batch 4-a: (provider, model) → declared context window (tokens).
        # Populated from the catalog by the router factory (team_llm_config.build_router_from_v2_config);
        # undeclared models are absent, and the window guard does nothing for None.
        self._context_windows: dict[tuple[str, str], int] = {}
        self.model_credit_rates: dict[tuple[str, str], dict[str, str]] = {}
        self.model_provider_types: dict[str, str] = {}
        # The model configuration's single retry setting (retries after the first attempt).
        self.max_retries = DEFAULT_MAX_RETRIES

    def bind(self, binding: AgentBinding) -> None:
        """注册 agent × profile → binding 映射。"""
        self._bindings[(binding.agent_type, binding.profile)] = binding

    def set_context_windows(self, windows: Mapping[tuple[str, str], int]) -> None:
        """登记 (provider, model) → 上下文窗口。只收正整数，其余忽略。"""
        self._context_windows = {k: int(v) for k, v in windows.items() if isinstance(v, int) and not isinstance(v, bool) and v > 0}

    def output_reservation_for(self, agent_type: str, profile: str, user_pref: UserModelPref | None, caller: int | None, *, capability: str) -> int:
        """Share the primary target's required reservation with input preparation."""
        from bowhead_router.output_budget import output_reservation
        from bowhead_router.runtime_parameters import model_spec

        binding = self.resolve(agent_type, profile, user_pref=user_pref)
        provider_name, model = binding.primary
        provider = self._registry.definition_for(provider_name)
        family = (
            "anthropic"
            if provider.get("type") == "anthropic_compatible"
            else "openai_responses"
            if model_spec(provider, model).get("api_surface") == "responses"
            else "openai"
        )
        return output_reservation(
            provider,
            model,
            caller if caller is not None else binding.max_output_tokens,
            capability=capability,
            family=family,
            thinking_budget=binding.thinking_budget_tokens if binding.extended_thinking else 0,
        )

    def context_window_for(
        self,
        agent_type: str,
        profile: str | None = None,
        user_pref: UserModelPref | None = None,
    ) -> int | None:
        """该 agent 本次会先落到的目标（primary，已套 user_pref）的窗口；未登记时用模型目录值或默认 131,072。

        review 修（2026-09-05）：第一版取 primary + fallback 的**最小值**，结果 1M 主模型配一个 32k 备胎时，
        每一轮正常流量都被按 28.8k 裁、前缀缓存每圈作废——把「1M 模型永远到不了」变成了「每轮都到」。
        备胎窗口小是备胎自己的事：主模型失败切过去时若超窗，上游 400 → 批 4-c 的超窗重试再收一次。
        没有 binding 也返回 ``None``，不抛——守卫是兜底，不能反过来把正常路径打断。
        """
        try:
            binding = self.resolve(agent_type, profile, user_pref=user_pref)
        except KeyError:
            return None
        registered = self._context_windows.get(binding.primary)
        if registered is not None:
            return registered
        # A bound model always has a window (design agent-release-beginner-experience §8): the
        # reviewed catalog value, else the 131,072 default an administrator can override.
        from bowhead_router.model_capabilities import DEFAULT_CONTEXT_WINDOW, catalog_context_window

        return catalog_context_window(binding.primary[1]) or DEFAULT_CONTEXT_WINDOW

    def supports_context_maintenance(self, agent_type: str, profile: str | None = None, user_pref: UserModelPref | None = None) -> bool:
        """Whether the admitted primary exposes the shared final-body compiler.

        Mock and custom-template adapters retain their existing input contract;
        importing the shared Router does not admit a new summarization surface.
        """
        provider, _ = self.resolve(agent_type, profile, user_pref=user_pref).primary
        return callable(getattr(self._registry.get(provider), "_build_body", None))

    async def request_context_budget(
        self,
        agent_type: str,
        req: LLMRequest,
        profile: str | None = None,
        user_pref: UserModelPref | None = None,
        *,
        target: tuple[str, str] | None = None,
        stream: bool = True,
        reject_over_capacity: bool = True,
    ) -> TargetContextBudget:
        """Compile one configured target without dispatch, usage or caller mutation.

        Input authority and the adapter's final rendering are the same as send.
        A smaller fallback is measured only when the owner selects that target.
        This is not a physical invocation ticket or permission to send later.
        """
        from bowhead_router.context_policy import TargetContextBudget, fallback_context_policy, model_context_policy
        from bowhead_router.output_budget import ContextCapacityError, output_reservation
        from bowhead_router.runtime_parameters import family_for

        binding = self.resolve(agent_type, profile, user_pref=user_pref)
        selected = target or binding.primary
        if selected not in (binding.primary, *binding.fallback):
            raise LLMInputAdmissionError("llm_context_target_not_configured")
        provider_name, model = selected
        adapter = self._registry.get(provider_name)
        builder = getattr(adapter, "_build_body", None)
        if not callable(builder):
            raise LLMInputAdmissionError("llm_context_accounting_adapter_unavailable")
        definition = self._registry.definition_for(provider_name)
        policy = model_context_policy(definition, model)
        if selected != binding.primary:
            primary_provider, primary_model = binding.primary
            policy = fallback_context_policy(model_context_policy(self._registry.definition_for(primary_provider), primary_model), policy)
        prepared = _clone_with_model(replace(_inject_runtime_context(req), invocation_agent_type=agent_type), model, binding)
        prepared.configuration_version = self.configuration_version
        prepared.output_reservation = output_reservation(
            definition,
            model,
            prepared.max_output_tokens,
            capability="tool" if prepared.tools else "vision" if any(message.images for message in prepared.messages) else "chat",
            family=family_for(definition, model),
            thinking_budget=prepared.thinking_budget_tokens if prepared.extended_thinking else 0,
        )
        window = self._context_windows.get(selected)
        if selected != binding.primary and _skip_undeclared_admission_fallback(prepared, target_index=2, context_window=window):
            raise LLMInputAdmissionError("llm_context_fallback_window_unavailable")
        await admit_request_input(
            prepared,
            provider=provider_name,
            model=model,
            context_window=window,
            adapter_supported=getattr(adapter, "supports_input_admission", False) is True,
        )
        try:
            builder(prepared, stream=stream)
        except ContextCapacityError as exc:
            exc.provider = provider_name
            exc.configuration_version = self.configuration_version
            if reject_over_capacity:
                raise
            return TargetContextBudget(provider_name, model, self.configuration_version, policy, exc.budget)
        if prepared.context_budget is None:
            raise LLMInputAdmissionError("llm_context_accounting_adapter_unavailable")
        return TargetContextBudget(provider_name, model, self.configuration_version, policy, prepared.context_budget)

    def _resolve_key(self, agent_type: str, profile: str | None) -> tuple[str, str | None]:
        if (agent_type, profile) in self._bindings:
            return (agent_type, profile)
        if (agent_type, None) in self._bindings:
            if profile is not None:
                # An explicitly requested profile without a binding gracefully falls back to default, but this must be observable:
                # for example, a missing structured profile makes concept-resolve fall back to a reasoning model
                # (60s/token); without this log, only profiling a slow turn again would reveal it.
                _get_logger().debug(
                    "router.profile_fallback agent=%s profile=%s -> default",
                    agent_type,
                    profile,
                )
            return (agent_type, None)
        raise NoBindingError(agent_type, profile)

    def resolve(
        self,
        agent_type: str,
        profile: str | None = None,
        user_pref: UserModelPref | None = None,
    ) -> AgentBinding:
        """查找 binding, 按优先级 user_pref > yaml. 见 M2.3 docs.

        优先级链 (高 → 低):
          1. user_pref (仅 lead/composer agent_type 受影响; sub-agents 不受)
          2. yaml binding.primary (出厂)

        user_pref 命中时, primary 替换成 (user_pref.provider, user_pref.model);
        binding 的 fallback 链保留 (用户选的 provider 失败仍回 yaml fallback).

        (进程内 admin runtime override 已随 /admin/routing 一并移除 —— 组织级模型选择
        现由 team LLM config 的 agent binding 承载, 见「模型」设置页 WS0.)
        """
        import dataclasses

        key = self._resolve_key(agent_type, profile)
        binding = self._bindings[key]
        # 1. user_pref applies only to lead/composer.
        if user_pref is not None and agent_type in USER_PREF_APPLICABLE_AGENTS:
            return dataclasses.replace(
                binding,
                primary=(user_pref.provider, user_pref.model),
            )
        # 2. yaml default
        return binding

    def list_bindings(self) -> list[tuple[str, str | None, tuple[str, str], tuple[str, str] | None]]:
        """返回 (agent_type, profile, primary, None).

        4-tuple 保留历史形状; 第 4 位（原 admin override）恒 None —— 进程内 override
        已移除, primary 即 yaml/team-config 出厂值.
        """
        out: list[tuple[str, str | None, tuple[str, str], tuple[str, str] | None]] = []
        for key, binding in self._bindings.items():
            out.append((key[0], key[1], binding.primary, None))
        return out

    async def invoke(
        self,
        agent_type: str,
        req: LLMRequest,
        profile: str | None = None,
        user_pref: UserModelPref | None = None,
        retry_policy: RetryPolicy | None = None,
        *,
        target: tuple[str, str] | None = None,
    ) -> LLMResponse:
        """按 binding 调用；failure 时按 fallback 链重试。

        user_pref: M2.3 用户在 Composer 选的 model 偏好; 仅作用 lead/composer.
        """
        binding = self.resolve(agent_type, profile, user_pref=user_pref)
        targets = [binding.primary, *binding.fallback]
        if target is not None and target not in targets:
            raise LLMInputAdmissionError("llm_context_target_not_configured")
        failures: list[LLMTargetFailure] = []
        policy = retry_policy or RetryPolicy(max_attempts_per_target=self.max_retries + 1)
        last_error: Exception | None = None

        # M2.4: log effective (provider, model) to diagnose "the user selected X, but Y actually ran".
        if user_pref is not None and agent_type in USER_PREF_APPLICABLE_AGENTS:
            _log = _get_logger()
            _log.info(
                "router.user_pref_applied agent=%s provider=%s model=%s",
                agent_type,
                user_pref.provider,
                user_pref.model,
            )

        # Inject {today} / {current_year} / {last_year} at the start of the system prompt so every LLM
        # call gets the same date context without assembling it separately in each prompt template.
        req = replace(_inject_runtime_context(req), invocation_agent_type=agent_type)

        for target_index, (provider_name, model) in enumerate(targets, start=1):
            if target is not None and target != (provider_name, model):
                continue
            if _skip_undeclared_admission_fallback(req, target_index=target_index, context_window=self._context_windows.get((provider_name, model))):
                _get_logger().warning(
                    "router.input_admission_fallback_skipped agent=%s provider=%s model=%s mode=invoke reason=context_window_missing",
                    agent_type,
                    provider_name,
                    model,
                )
                continue
            if not self._registry.has(provider_name):
                failures.append(
                    _unregistered_target_failure(
                        provider=provider_name,
                        model=model,
                        mode="invoke",
                        target_index=target_index,
                    )
                )
                _get_logger().error(
                    "router.target_unregistered agent=%s provider=%s model=%s",
                    agent_type,
                    provider_name,
                    model,
                )
                continue
            adapter = self._registry.get(provider_name)
            # A fallback target is one more attempt: it must fit the same time budget as a retry.
            if target_index > 1 and failures and not attempt_fits(attempt_timeout_s=adapter_timeout(adapter)):
                break
            # Copy req and inject model.
            req_for_attempt = _clone_with_model(req, model, binding)
            req_for_attempt.credit_rates = self.model_credit_rates.get((provider_name, model))
            req_for_attempt.configuration_version = self.configuration_version
            from bowhead_router.output_budget import output_reservation
            from bowhead_router.runtime_parameters import family_for

            definition_reader = getattr(self._registry, "definition_for", None)
            if callable(definition_reader):
                definition = definition_reader(provider_name)
            else:
                adapter_definition = getattr(adapter, "_provider_definition", None)
                definition = adapter_definition() if callable(adapter_definition) else getattr(adapter, "provider_options", {})
            req_for_attempt.output_reservation = output_reservation(
                definition,
                model,
                req_for_attempt.max_output_tokens,
                capability="tool" if req_for_attempt.tools else "vision" if any(message.images for message in req_for_attempt.messages) else "chat",
                family=family_for(definition, model),
                thinking_budget=req_for_attempt.thinking_budget_tokens if req_for_attempt.extended_thinking else 0,
            )
            try:
                resp = await _invoke_with_retry(
                    adapter,
                    req_for_attempt,
                    provider_name=provider_name,
                    model=model,
                    max_attempts=policy.max_attempts_per_target,
                    base_delay_s=policy.base_delay_s,
                    failures=failures,
                    target_index=target_index,
                    context_window_reader=lambda target=(provider_name, model): self._context_windows.get(target),
                )
                resp.provider = provider_name
                resp.model = model
                if req_for_attempt.output_budget is not None:
                    from bowhead_router.types import LLMResponseDiagnostics

                    observed = req_for_attempt.output_budget
                    resp.diagnostics = replace(
                        resp.diagnostics or LLMResponseDiagnostics(transport_mode="invoke"),
                        output_token_limit=observed.get("wire_allowance"),
                        output_budget=observed,
                    )
                # Move a leading inline <think> reasoning block into thinking so all plain-text callers (session titles, etc.)
                # benefit here without stripping it independently.
                _move_inline_reasoning_to_thinking(resp)
                # Usage chokepoint (credit-billing gap 7): every LLM call passes here, so record usage once here
                # to prevent new call sites from missing accounting. No registered sink = no-op; record_usage swallows sink exceptions.
                # gap23 step 1: provide complete **attribution** here too. `owner_team_id` is this router's construction
                # identity, so call sites need no declaration (previously team_id relied on contextvar, omitted by 20+ entry points).
                _usage = getattr(resp, "usage", None)
                record_usage(
                    agent_type=agent_type,
                    provider=provider_name,
                    model=model,
                    tokens_input=getattr(_usage, "input_tokens", 0) or 0,
                    tokens_output=getattr(_usage, "output_tokens", 0) or 0,
                    tokens_cached=getattr(_usage, "cached_tokens", 0) or 0,
                    owner_team_id=self.owner_team_id,
                    physical_recorded=req_for_attempt.physical_receipt_recorded,
                )
                # M2.4: record effective_model so traces / logs show which model was ultimately used.
                if target_index > 1:
                    _get_logger().info(
                        "router.fallback_used agent=%s primary=%s fallback=%s/%s",
                        agent_type,
                        binding.primary,
                        provider_name,
                        model,
                    )
                return resp
            except (InvocationOutcomeUnknownError, LLMInputAdmissionError) as exc:
                from bowhead_router.output_budget import ContextCapacityError

                if isinstance(exc, ContextCapacityError):
                    exc.provider = provider_name
                    exc.configuration_version = self.configuration_version
                raise
            except Exception as exc:
                last_error = exc
                # M1 emits the llm.provider_switched event here.
                last_failure = failures[-1]
                _get_logger().info(
                    "router.target_failed agent=%s provider=%s model=%s kind=%s status=%s",
                    agent_type,
                    provider_name,
                    model,
                    last_failure.kind.value,
                    last_failure.status_code,
                )
                continue

        # All fallbacks failed.
        if last_error is not None:
            raise InvocationTargetsExhaustedError(failures) from last_error
        raise InvocationTargetsExhaustedError(failures)

    async def stream(
        self,
        agent_type: str,
        req: LLMRequest,
        profile: str | None = None,
        user_pref: UserModelPref | None = None,
        retry_policy: RetryPolicy | None = None,
        idle_timeout_s: float | None = None,
        progress_idle: bool = False,
        *,
        target: tuple[str, str] | None = None,
    ) -> AsyncIterator[LLMChunk]:
        """流式调用. 见 invoke() 注释 user_pref 语义.

        Fallback 在流式场景更复杂（已 yield 出的 chunk 不能撤回），MVP 策略：
        "第一 token 前 fallback；之后失败就报错".
        """
        binding = self.resolve(agent_type, profile, user_pref=user_pref)
        targets = [binding.primary, *binding.fallback]
        if target is not None and target not in targets:
            raise LLMInputAdmissionError("llm_context_target_not_configured")
        req = replace(_inject_runtime_context(req), invocation_agent_type=agent_type)
        failures: list[LLMTargetFailure] = []
        policy = retry_policy or RetryPolicy(max_attempts_per_target=self.max_retries + 1)
        last_error: Exception | None = None

        for target_index, (provider_name, model) in enumerate(targets, start=1):
            if target is not None and target != (provider_name, model):
                continue
            if _skip_undeclared_admission_fallback(req, target_index=target_index, context_window=self._context_windows.get((provider_name, model))):
                _get_logger().warning(
                    "router.input_admission_fallback_skipped agent=%s provider=%s model=%s mode=stream reason=context_window_missing",
                    agent_type,
                    provider_name,
                    model,
                )
                continue
            if not self._registry.has(provider_name):
                failures.append(
                    _unregistered_target_failure(
                        provider=provider_name,
                        model=model,
                        mode="stream",
                        target_index=target_index,
                    )
                )
                _get_logger().error(
                    "router.target_unregistered agent=%s provider=%s model=%s",
                    agent_type,
                    provider_name,
                    model,
                )
                continue
            adapter = self._registry.get(provider_name)
            # A fallback target is one more attempt: it must fit the same time budget as a retry.
            if target_index > 1 and failures and not attempt_fits(attempt_timeout_s=adapter_timeout(adapter)):
                break
            req_for_attempt = _clone_with_model(req, model, binding)
            req_for_attempt.credit_rates = self.model_credit_rates.get((provider_name, model))
            req_for_attempt.configuration_version = self.configuration_version
            from bowhead_router.output_budget import output_reservation
            from bowhead_router.runtime_parameters import family_for

            definition_reader = getattr(self._registry, "definition_for", None)
            if callable(definition_reader):
                definition = definition_reader(provider_name)
            else:
                adapter_definition = getattr(adapter, "_provider_definition", None)
                definition = adapter_definition() if callable(adapter_definition) else getattr(adapter, "provider_options", {})
            req_for_attempt.output_reservation = output_reservation(
                definition,
                model,
                req_for_attempt.max_output_tokens,
                capability="tool" if req_for_attempt.tools else "vision" if any(message.images for message in req_for_attempt.messages) else "chat",
                family=family_for(definition, model),
                thinking_budget=req_for_attempt.thinking_budget_tokens if req_for_attempt.extended_thinking else 0,
            )
            # 0.13.2 stream retry: only retry BEFORE first chunk emits (can't
            # replay yielded chunks). max 2 attempts (1 retry) with 2s backoff
            # for retryable errors (429 transient bursts).
            max_attempts = policy.max_attempts_per_target
            for stream_attempt in range(max_attempts):
                await admit_request_input(
                    req_for_attempt,
                    provider=provider_name,
                    model=model,
                    context_window=self._context_windows.get((provider_name, model)),
                    adapter_supported=getattr(adapter, "supports_input_admission", False) is True,
                )
                first_visible_chunk_emitted = False
                inline_reasoning = InlineReasoningStream()
                # Usage accounting (credit-billing gap 7): record only the **last observed** usage, once when this attempt finishes.
                # Never record per chunk: some providers return **cumulative** usage on every chunk, which would inflate usage
                # by O(number of chunks). Use finally so callers breaking early (async generator aclose) do not escape accounting.
                _last_usage: object | None = None
                iterator: AsyncIterator[LLMChunk] | None = None
                try:
                    try:
                        iterator = adapter.stream(req_for_attempt).__aiter__()
                        progress_at = monotonic()
                        while True:
                            try:
                                if idle_timeout_s is None:
                                    chunk = await iterator.__anext__()
                                else:
                                    chunk = await asyncio.wait_for(
                                        iterator.__anext__(),
                                        timeout=max(0.0, idle_timeout_s - (monotonic() - progress_at)) if progress_idle else idle_timeout_s,
                                    )
                            except StopAsyncIteration:
                                break
                            if chunk.delta or chunk.thinking_delta or chunk.tool_call_delta or chunk.tool_call_partial:
                                progress_at = monotonic()
                            content, reasoning = inline_reasoning.feed(chunk.delta)
                            if chunk.finish_reason is not None:
                                tail, tail_reasoning = inline_reasoning.finish()
                                content += tail
                                reasoning = "\n".join(part for part in (reasoning, tail_reasoning) if part)
                            if content != chunk.delta or reasoning:
                                chunk = replace(
                                    chunk,
                                    delta=content,
                                    thinking_delta="\n".join(part for part in (chunk.thinking_delta, reasoning) if part),
                                )
                            if _is_visible_chunk(chunk):
                                first_visible_chunk_emitted = True
                            if getattr(chunk, "usage", None) is not None:
                                _last_usage = chunk.usage
                            if chunk.finish_reason is not None and req_for_attempt.output_budget is not None:
                                from bowhead_router.types import LLMResponseDiagnostics

                                chunk = replace(
                                    chunk,
                                    diagnostics=replace(
                                        chunk.diagnostics or LLMResponseDiagnostics(transport_mode="stream"),
                                        output_token_limit=req_for_attempt.output_budget.get("wire_allowance"),
                                        output_budget=req_for_attempt.output_budget,
                                    ),
                                )
                            yield chunk
                        tail, tail_reasoning = inline_reasoning.finish()
                        if tail or tail_reasoning:
                            yield LLMChunk(delta=tail, thinking_delta=tail_reasoning)
                        return  # stream completed successfully
                    finally:
                        if iterator is not None and (physical_invocation_required() or physical_observation_active()):
                            close_iterator = getattr(iterator, "aclose", None)
                            if close_iterator is not None:
                                await close_iterator()
                        if _last_usage is not None:
                            record_usage(
                                agent_type=agent_type,
                                provider=provider_name,
                                model=model,
                                tokens_input=getattr(_last_usage, "input_tokens", 0) or 0,
                                tokens_output=getattr(_last_usage, "output_tokens", 0) or 0,
                                tokens_cached=getattr(_last_usage, "cached_tokens", 0) or 0,
                                streamed=True,
                                owner_team_id=self.owner_team_id,
                                physical_recorded=req_for_attempt.physical_receipt_recorded,
                            )
                except (InvocationOutcomeUnknownError, LLMInputAdmissionError) as exc:
                    from bowhead_router.output_budget import ContextCapacityError

                    if isinstance(exc, ContextCapacityError):
                        exc.provider = provider_name
                        exc.configuration_version = self.configuration_version
                    raise
                except Exception as e:
                    if isinstance(e, TimeoutError) and (physical_invocation_required() or physical_observation_active()):
                        raise InvocationOutcomeUnknownError(
                            "durable_stream_idle_timeout",
                        ) from e
                    last_error = e
                    failure = target_failure(
                        e,
                        adapter=adapter,
                        provider=provider_name,
                        model=model,
                        mode="stream",
                        target_index=target_index,
                        attempt=stream_attempt + 1,
                        first_visible_chunk_emitted=first_visible_chunk_emitted,
                    )
                    failures.append(failure)
                    _get_logger().info(
                        "router.stream_target_failed agent=%s provider=%s model=%s kind=%s status=%s",
                        agent_type,
                        provider_name,
                        model,
                        failure.kind.value,
                        failure.status_code,
                    )
                    if first_visible_chunk_emitted:
                        raise StreamingInterruptedError(failures) from e
                    retryable = failure.retryable
                    delay = policy.base_delay_s * (2**stream_attempt)
                    if (
                        retryable
                        and stream_attempt < max_attempts - 1
                        and await admit_retry(
                            RetryNotice(
                                retry=stream_attempt + 1,
                                max_retries=max_attempts - 1,
                                kind=failure.kind,
                                delay_s=delay,
                                mode="stream",
                                provider=provider_name,
                                model=model,
                                attempt_timeout_s=adapter_timeout(adapter),
                            )
                        )
                    ):
                        _get_logger().warning(
                            "router.stream_retry provider=%s model=%s attempt=%d/%d delay=%.1fs kind=%s status=%s",
                            provider_name,
                            model,
                            stream_attempt + 1,
                            max_attempts,
                            delay,
                            failure.kind.value,
                            failure.status_code,
                        )
                        await asyncio.sleep(delay)
                        continue  # retry same provider
                    break  # exhausted retries OR not retryable → next provider

        if last_error is not None:
            raise StreamingTargetsExhaustedError(failures) from last_error
        raise StreamingTargetsExhaustedError(failures)


def _is_visible_chunk(chunk: LLMChunk) -> bool:
    """Only chunks that can change rendered user content close the fallback window."""

    return bool(chunk.delta or chunk.thinking_delta or chunk.tool_call_delta is not None or chunk.tool_call_partial is not None)


def _today_banner(now: datetime | None = None) -> str:
    """统一的"今日"上下文注入 —— 所有 LLM 调用都带这个头.

    让模型对"今年/去年/本月/上月/近30天"有具体日期锚点；同时硬性指明：
    "去年" 是 last_year（整数），构 BI query 时直接用整年数字（"2024 年"），
    不要输出模糊词（"去年" / "近期"）到下游 datarc.

    The date comes from the model clock (``model_clock.model_now``): the turn's
    viewer zone when one was admitted, else the deployment clock. It must match
    the subagent ``<current_date>`` anchor and BI advice, never a separate UTC "today".
    """
    dt = now or model_now()
    wd = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][dt.weekday()]
    this_m_start = dt.replace(day=1)
    last_m_end = this_m_start.replace(day=1)  # noqa: F841 – placeholder if we want
    return (
        "<runtime_context>\n"
        f"today: {dt.strftime('%Y-%m-%d')} ({wd})\n"
        f"current_year: {dt.year}\n"
        f"last_year: {dt.year - 1}\n"
        f"two_years_ago: {dt.year - 2}\n"
        f"this_month: {dt.strftime('%Y-%m')}\n"
        f"this_quarter: Q{(dt.month - 1) // 3 + 1} of {dt.year}\n"
        "\n"
        "时间词必须**替换成具体年份/月份/季度**后再发给 BI（datarc.ask_question）:\n"
        f'  - 用户说 "去年" → 构造查询用 "{dt.year - 1}年"；若需缩到月份，拼 "{dt.year - 1}年1-12月".\n'
        f'  - 用户说 "今年" → "{dt.year}年"；"本月" → "{dt.strftime("%Y年%m月")}".\n'
        f'  - 用户说 "上月" → 上一自然月（跨年时要回到前一年 12 月）.\n'
        f'  - 用户说 "近30天" / "近7天" / "近一年" → 允许原样传 datarc（它认）.\n'
        '  - **禁止**在发给 datarc 的 text 里写模糊词 "最近" / "近期" / "现在".\n'
        "</runtime_context>"
    )


def _inject_runtime_context(req: LLMRequest) -> LLMRequest:
    """在 LLM 请求的 system prompt 最前面注入 runtime context.

    通过 dataclasses.replace 保留所有其他字段（top_p / extended_thinking / metadata 等）。
    """
    import dataclasses

    banner = _today_banner()
    new_system = f"{banner}\n\n{req.system}" if req.system else banner
    return dataclasses.replace(req, system=new_system)


def _move_inline_reasoning_to_thinking(resp: LLMResponse) -> None:
    content, reasoning = split_inline_reasoning(resp.content or "")
    if not reasoning and content == resp.content:
        return
    resp.content = content
    resp.thinking = "\n".join(part for part in (resp.thinking, reasoning) if part)


def _clone_with_model(req: LLMRequest, model: str, binding: AgentBinding) -> LLMRequest:
    """生成 attempt-specific 的请求（替换 model + 套用 binding 参数）。"""
    return LLMRequest(
        model=model,
        messages=req.messages,
        system=req.system,
        tools=req.tools,
        temperature=req.temperature,
        top_p=req.top_p,
        max_output_tokens=req.max_output_tokens if req.max_output_tokens is not None else binding.max_output_tokens,
        stop=req.stop,
        extended_thinking=req.extended_thinking or binding.extended_thinking,
        thinking_budget_tokens=req.thinking_budget_tokens or binding.thinking_budget_tokens,
        stream=req.stream,
        trace_id=req.trace_id,
        metadata=req.metadata,
        response_format=req.response_format,
        tool_choice=req.tool_choice,
        input_admission=req.input_admission,
        input_context_id=req.input_context_id,
        invocation_agent_type=req.invocation_agent_type,
        invocation_execution_id=req.invocation_execution_id,
        invocation_subagent_id=req.invocation_subagent_id,
        summary_only=req.summary_only,
    )
