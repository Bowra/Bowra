"""Run an admitted context-only Turn through the original ownership and finalizer."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from uuid import uuid4

from bowhead_router.router import UserModelPref
from bowhead_router.types import LLMRequest

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
from bowhead_core.context_action import validate_context_action_in_tx
from bowhead_core.context_activity import ContextActivity
from bowhead_core.context_history import context_history_available, load_conversation_context
from bowhead_core.context_store import ContextStoreError
from bowhead_core.events import OrchestratorEvent
from bowhead_core.subagent import SubagentProfile, _RuntimeExecution
from bowhead_core.types import IntentType, TurnResult, TurnStatus


async def run_manual_context(runner, request, *, ctx, bus, agent_runner) -> TurnResult:
    """Never enter Lead business dispatch, Tool execution or a paused continuation."""
    from bowhead_core.orchestrator import TurnCancelledError

    action = request.context_maintenance
    if action is None or not context_history_available(ctx, agent_runner.router):
        raise ContextStoreError("context_maintenance_unsupported")
    source = await ctx.session_store.get_chat_turn_user_source(
        turn_id=ctx.turn_id,
        user_id=ctx.user_id,
        workspace_id=ctx.workspace_id,
    )
    if source is None or source.context_maintenance != action:
        raise ContextStoreError("context_maintenance_source_changed")
    async with open_shared_ctx(ctx.session_store.db_path) as db:
        await db.execute("BEGIN")
        await validate_context_action_in_tx(db, source=source)
    history = await load_conversation_context(ctx)
    if history.generation_id != action.expected_generation:
        raise ContextStoreError("context_conversation_head_changed")
    ctx._model_context_history = history
    binding = await agent_runner.admit_execution_binding(
        profile_name="lead",
        parent_turn_id=ctx.turn_id,
        parent_ctx=ctx,
        dispatch_key=f"context-maintenance:{ctx.turn_id}:{ctx.execution_generation}",
    )
    if binding is None:
        raise ContextStoreError("context_maintenance_binding_unavailable")
    bindings = AgentExecutionBindingStore(ctx.session_store.db_path)
    await bindings.mark_running(binding.execution_id)
    settled = "failed"
    try:
        controller = _RuntimeExecution(ctx.agent_runtime_budget)
        view = agent_runner._execution_context_view(
            ctx=ctx,
            bus=bus,
            profile=SubagentProfile(name="lead", llm_agent_type="lead", tool_whitelist=(), system_prompt=""),
            subagent_id=binding.execution_id,
            execution_id=binding.execution_id,
            lane="ordinary",
            runtime_control=controller,
        )
        if view is None:
            raise ContextStoreError("context_maintenance_unsupported")
        completed = []
        emit = view.activity

        async def activity(payload):
            # The execution checkpoint is durable here, but manual success means
            # adoption by the original conversation finalizer, not this callback.
            if payload["phase"] == "skipped" and payload.get("reason") == "summary_reduction_insufficient":
                payload = {**payload, "reason": "no_change"}
            if payload["phase"] == "committed":
                completed.append(
                    ContextActivity(
                        **payload,
                        execution_id=binding.execution_id,
                        lane="ordinary",
                        profile="lead",
                    )
                )
            else:
                await emit(payload)

        view.activity = activity
        usage = [0, 0]

        def record_usage(response):
            usage[0] += response.usage.input_tokens
            usage[1] += response.usage.output_tokens
            runner._tokens_in, runner._tokens_out = usage

        messages = history.messages
        current = max((i for i, message in enumerate(messages) if message.role == "user"), default=-1)
        if current < 0:
            raise ContextStoreError("context_maintenance_history_unavailable")
        result = await view.prepare(
            LLMRequest(model="", messages=messages),
            router=agent_runner.router,
            agent_type="lead",
            profile="lead",
            user_pref=UserModelPref.from_request_fields(ctx.preferred_provider, ctx.preferred_model),
            controller=controller,
            current_request=current,
            canonical_count=len(messages),
            timeout=min(
                ctx.agent_run_limits.turn_time_limit_s, ctx.agent_run_limits.model_call_time_limit_s or ctx.agent_run_limits.turn_time_limit_s
            ),
            max_growth_tokens=0,
            stream=False,
            trigger="manual",
            record_usage=record_usage,
        )
        runner._check_cancel()
        if result.candidate is not None:
            publication = await view.conversation_publication(messages)
            bus._context_publication = replace(publication, append_answer=False)
            bus._context_maintenance_completions = tuple(completed)
        elif not result.attempted:
            if result.skipped not in {"no_eligible_messages", "protected_recent_messages"}:
                raise ContextStoreError(f"context_maintenance_{result.skipped}")
            activity = ContextActivity(
                operation_id="mco_" + uuid4().hex,
                execution_id=binding.execution_id,
                lane="ordinary",
                profile="lead",
                phase="skipped",
                trigger="manual",
                source_generation=history.generation_id,
                reason="no_change",
            )
            await bus.emit(
                OrchestratorEvent(
                    event_type="agent.context",
                    turn_id=ctx.turn_id,
                    payload=activity.payload(ctx.turn_id),
                    durable_event_id=activity.event_id(),
                    persist_before_delivery=True,
                )
            )
        elif result.skipped != "summary_reduction_insufficient":
            raise ContextStoreError(f"context_maintenance_{result.skipped}")
        duration = int((time.perf_counter() - runner._t_start) * 1000)
        bus._turn_wall_duration_ms = duration
        from bowhead_core.turn_runner import _estimate_cost_usd

        cost = _estimate_cost_usd(*usage)
        if request.terminal_runtime is None:
            accepted = await runner._persist_turn_state(
                turn_id=ctx.turn_id,
                status_value="completed",
                intent=None,
                final_answer="",
                tokens_input=usage[0],
                tokens_output=usage[1],
                cost_usd=cost,
                error=None,
                error_kind=None,
            )
            if not accepted:
                raise ContextStoreError("context_maintenance_finalization_rejected")
            for completion in completed:
                await bus.emit(
                    OrchestratorEvent(
                        event_type="agent.context",
                        turn_id=ctx.turn_id,
                        payload=completion.payload(ctx.turn_id),
                        durable_event_id=completion.event_id(),
                        persist_before_delivery=True,
                        deliver_existing_durable=True,
                    )
                )
        await bus.emit(
            OrchestratorEvent(
                event_type="done",
                turn_id=ctx.turn_id,
                payload={
                    "status": "completed",
                    "final_answer": "",
                    "tokens_input": usage[0],
                    "tokens_output": usage[1],
                    "cost_usd": cost,
                    "duration_ms": duration,
                    "context_maintenance": action.model_dump(),
                },
            )
        )
        settled = "completed"
        return TurnResult(
            turn_id=ctx.turn_id,
            status=TurnStatus.COMPLETED,
            intent=IntentType.AMBIGUOUS,
            final_answer="",
            tokens_input=usage[0],
            tokens_output=usage[1],
            cost_usd=cost,
            duration_ms=duration,
        )
    except (asyncio.CancelledError, TurnCancelledError):
        settled = "cancelled"
        raise
    finally:
        try:
            await bindings.mark_terminal(
                binding.execution_id, status=settled, failure_code="context_maintenance_failed" if settled == "failed" else None
            )
        except Exception:
            logging.getLogger(__name__).exception("context.maintenance_settlement_failed execution=%s", binding.execution_id)
