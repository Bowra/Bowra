"""Exact ``SubagentRunner`` adapter for M3-10 Agent Eval scratch Turns."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import time
from dataclasses import dataclass
from typing import Any, Protocol

from bowhead_core.agent_eval_service import EvalAttemptOutput, EvalAttemptRequest
from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
from bowhead_core.events import OrchestratorEvent, TurnEventBus
from bowhead_core.tool_registry import ToolExecutionContext


class EvalToolContextFactory(Protocol):
    async def build(self, request: EvalAttemptRequest) -> ToolExecutionContext: ...


@dataclass(frozen=True, slots=True)
class _EvalAccessControlStore:
    """Expose only permission decisions; Eval denials must not write production audit."""

    delegate: object

    async def explain_decision(self, *args: Any, **kwargs: Any) -> Any:
        return await self.delegate.explain_decision(*args, **kwargs)  # type: ignore[attr-defined]

    async def record_runtime_denied(self, *args: Any, **kwargs: Any) -> None:
        return None


class _EvalConnectorSentinel:
    """Non-production handle used only to pass Registry connection-presence gates."""


_EVAL_CONNECTOR = _EvalConnectorSentinel()

# SubagentRunner result codes that mean the model call itself failed or its outcome is
# unknown (admission refusal, provider error, platform call deadline). They say nothing
# about the Agent's quality, so the attempt settles as an infrastructure failure instead
# of being scored as a failed answer.
_PLATFORM_FAILURE_CODES = frozenset(
    {
        "subagent_internal_error",
        "peer_llm_deadline_exceeded",
        "subagent_execution_uncertain",
    }
)


class AgentEvalPlatformFailure(RuntimeError):
    """The pinned Agent could not run because of a platform failure."""

    def __init__(self, code: str, *, llm_reason_code: str | None = None, model_role: str | None = None) -> None:
        super().__init__(f"agent_eval_platform_failure:{code}")
        self.code = code
        # The typed model failure (rate limit, credentials, timeout, no model assigned...).
        self.llm_reason_code = llm_reason_code
        # The model role the Agent ran as, to name a missing assignment.
        self.model_role = model_role


@dataclass(slots=True)
class ExactSubagentEvalExecutor:
    """Run the pinned Agent revision directly, without a second Lead selection."""

    binding_store: AgentExecutionBindingStore
    subagent_runner: object
    context_factory: EvalToolContextFactory
    ordered_turn_log_sink: object
    # Team settings for agent run limits. Evaluation runs a
    # subagent outside a Lead turn, so it opens its own limits scope per case run.
    team_settings_store: object | None = None

    async def execute(self, request: EvalAttemptRequest) -> EvalAttemptOutput:
        if request.execution_mode != "eval":  # pragma: no cover - Literal boundary
            raise ValueError("Agent Eval executor requires eval mode")
        source_ctx = await self.context_factory.build(request)
        ctx = _isolated_context(source_ctx, request)
        bus = TurnEventBus(
            log_sink=self.ordered_turn_log_sink,
            producer_registered=True,
            persist_before_live=True,
        )
        binding = await self.binding_store.admit_eval_execution(
            eval_run_id=request.run.run_id,
            user_id=request.run.created_by,
            turn_id=request.turn_id,
            dispatch_key=(f"eval:{request.run.run_id}:{request.case.case_revision_id}:{request.attempt}:{request.turn_id}"),
        )
        await self.binding_store.mark_running(binding.execution_id)
        from bowhead_core.agent_run_limits import (
            TurnToolCallBudget,
            load_team_limits,
            reset_run_limits,
            reset_tool_call_budget,
            set_run_limits,
            set_tool_call_budget,
        )

        limits = await load_team_limits(self.team_settings_store, getattr(ctx, "team_id", None))
        limits_token = set_run_limits(limits)
        budget_token = set_tool_call_budget(TurnToolCallBudget(limits.tool_call_limit))
        started = time.perf_counter()
        try:
            try:
                from bowhead_core.execution_limits import execution_limits_scope
                from bowhead_core.execution_limits import load_team_limits as load_execution_limits

                execution_limits = await load_execution_limits(self.team_settings_store, getattr(ctx, "team_id", None))
                with execution_limits_scope(execution_limits):
                    result = await self.subagent_runner.run(
                        profile_name=binding.runtime_profile_name,
                        task=_task(request),
                        parent_turn_id=request.turn_id,
                        parent_bus=bus,
                        parent_ctx=ctx,
                        pre_minted_subagent_id=f"sub_eval_{request.turn_id.removeprefix('t_')[:32]}",
                        exact_binding=binding,
                    )
            finally:
                reset_tool_call_budget(budget_token)
                reset_run_limits(limits_token)
            if not result.ok and result.error in _PLATFORM_FAILURE_CODES:
                raise AgentEvalPlatformFailure(
                    result.error,
                    llm_reason_code=getattr(result, "llm_reason_code", None),
                    model_role=_binding_model_role(binding),
                )
            output = result.payload if result.payload is not None else result.final_answer
            terminal_status = "completed" if result.ok else "failed"
            from bowhead_core.turn_runner import _estimate_cost_usd

            cost_microusd = round(_estimate_cost_usd(result.tokens_input, result.tokens_output) * 1_000_000)
            if result.final_answer:
                await bus.emit(
                    OrchestratorEvent(
                        event_type="final_answer",
                        turn_id=request.turn_id,
                        payload={"text": result.final_answer},
                    )
                )
            await bus.emit(
                OrchestratorEvent(
                    event_type="done",
                    turn_id=request.turn_id,
                    payload={"status": terminal_status},
                )
            )
            await bus.close()
            await self.binding_store.mark_terminal(
                binding.execution_id,
                status="completed" if result.ok else "failed",
                failure_code=None if result.ok else "agent_eval_agent_failed",
            )
            return EvalAttemptOutput(
                output=output,
                terminal_status=terminal_status,
                # Some stops (the failure breaker, for one) return no duration of their own; the attempt
                # still ran, so it keeps the time it took instead of reading "0 s".
                duration_ms=max(0, result.duration_ms) or round((time.perf_counter() - started) * 1000),
                cost_microusd=max(0, cost_microusd),
                tokens_input=max(0, result.tokens_input),
                tokens_output=max(0, result.tokens_output),
                trace={"agent_execution_id": binding.execution_id},
            )
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await bus.close()
            await self.binding_store.mark_terminal(
                binding.execution_id,
                status="cancelled",
            )
            raise
        except AgentEvalPlatformFailure:
            with contextlib.suppress(Exception):
                await bus.close()
            await self.binding_store.mark_terminal(
                binding.execution_id,
                status="failed",
                failure_code="agent_eval_platform_failed",
            )
            raise
        except BaseException:
            with contextlib.suppress(Exception):
                await bus.close()
            await self.binding_store.mark_terminal(
                binding.execution_id,
                status="failed",
                failure_code="agent_eval_agent_failed",
            )
            raise


def _binding_model_role(binding: object) -> str | None:
    """The role the pinned Agent runs as: the Team Agent policy role, or a system Agent's own."""
    from bowhead_core.agent_model_policy import team_agent_model_role

    definition = getattr(binding, "definition", None)
    if not isinstance(definition, dict):
        return None
    if getattr(binding, "agent_source_kind", None) == "system":
        role = definition.get("llm_agent_type")
        return role if isinstance(role, str) and role else None
    return team_agent_model_role(definition.get("model"))


def _task(request: EvalAttemptRequest) -> str:
    value = request.case.input.get("task")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Agent Eval case task is missing")
    return value.strip()


def _isolated_context(
    source: ToolExecutionContext,
    request: EvalAttemptRequest,
) -> ToolExecutionContext:
    """Build the Eval context from an explicit allowlist of inert values.

    Mutating a live-style context is unsafe: newly added production handles would
    silently cross the boundary.  A fresh dataclass makes future fields default to
    unavailable until this adapter deliberately admits them.
    """

    access_store = source.access_control_store
    return ToolExecutionContext(
        workspace_id=request.workspace_id,
        member_uid="",
        user_id=request.run.created_by,
        turn_id=request.turn_id,
        datarc=_EVAL_CONNECTOR,  # type: ignore[arg-type]
        pbi=_EVAL_CONNECTOR,
        session_id=request.session_id,
        user_text=_task(request),
        role="analyst",
        team_id=request.run.subject.team_id,
        access_context=copy.deepcopy(source.access_context),
        access_control_store=(_EvalAccessControlStore(access_store) if access_store is not None else None),
        granted_team_subagent_ids=(frozenset(source.granted_team_subagent_ids) if source.granted_team_subagent_ids is not None else None),
        granted_team_skill_ids=(frozenset(source.granted_team_skill_ids) if source.granted_team_skill_ids is not None else None),
        active_datarc_project="eval_fixture",
        tool_view=request.safe_tool_registry,
    )
