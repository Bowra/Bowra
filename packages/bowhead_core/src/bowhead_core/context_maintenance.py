"""One execution lane's inline context maintenance at a model-decision boundary.

The existing Runner supplies its decision controller, source authorization and
transactional adoption. This module owns no worker, Tool dispatcher or publisher.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol
from uuid import uuid4

from bowhead_router.context_policy import TargetContextBudget, resolve_context_policy, summary_output_target
from bowhead_router.errors import ProviderFailureKind, RouteTargetsExhaustedError
from bowhead_router.input_admission import SummaryTransportUnsupported
from bowhead_router.output_budget import ContextCapacityError
from bowhead_router.types import LLMRequest, LLMResponse, Message

from bowhead_core.context_compaction import (
    CompactionPreparationError,
    CompactionSpan,
    balanced_boundaries,
    bounded_summary_request,
    message_digest,
    select_compaction_span,
    summarized_messages,
    summary_request,
)

Trigger = Literal["threshold", "capacity", "model_change", "manual"]


def recoverable_context_target(error: BaseException) -> tuple[str, str] | None:
    """Only a definite capacity rejection before visible output can be retried."""
    if isinstance(error, ContextCapacityError):
        return (error.provider, error.model) if error.provider and error.model else None
    from bowhead_router.errors import ProviderFailureKind

    from bowhead_core.llm_failure import target_failures

    failures = list(target_failures(error))
    if not failures or any(failure.first_visible_chunk_emitted for failure in failures):
        return None
    failure = failures[-1]
    if failure.kind is ProviderFailureKind.CONTEXT_OVERFLOW and failure.provider and failure.model:
        return failure.provider, failure.model
    return None


class DecisionController(Protocol):
    used: int
    budget: Any

    def admit(self) -> bool: ...

    def owned(self) -> Any: ...


@dataclass(frozen=True, slots=True)
class ContextCandidate:
    operation_id: str
    trigger: Trigger
    span: CompactionSpan
    messages: list[Message]
    before: TargetContextBudget
    after: TargetContextBudget
    response: LLMResponse


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    request: LLMRequest
    candidate: ContextCandidate | None = None
    skipped: str | None = None
    response: LLMResponse | None = None
    attempted: bool = False
    budget: TargetContextBudget | None = None
    needs_more: bool = False
    trimmed_messages: int = 0


class ContextMaintenance:
    """Instantiate per execution/lane, never on a shared Runner singleton."""

    def __init__(self) -> None:
        self._active = False
        self._attempted: tuple[str, tuple[str, str], Trigger] | None = None

    async def prepare(
        self,
        request: LLMRequest,
        *,
        router: Any,
        agent_type: str,
        profile: str,
        user_pref: Any,
        controller: DecisionController,
        start: int,
        protected_from: int,
        fixed_messages: list[Message],
        max_growth_tokens: int,
        timeout: float,
        authorize: Callable[[], Awaitable[None]],
        commit: Callable[[ContextCandidate], Awaitable[None]],
        started: Callable[[str, Trigger, TargetContextBudget], Awaitable[None]],
        target: tuple[str, str] | None = None,
        trigger: Trigger = "threshold",
        stream: bool = True,
        record_usage: Callable[[LLMResponse], None] | None = None,
        capacity_groups: tuple[tuple[int, int], ...] | None = None,
        rejected_input_estimate: int | None = None,
        rejection: BaseException | None = None,
    ) -> MaintenanceResult:
        if self._active:
            raise CompactionPreparationError("context_operation_already_running")
        self._active = True
        try:
            return await self._prepare(
                request,
                router=router,
                agent_type=agent_type,
                profile=profile,
                user_pref=user_pref,
                controller=controller,
                start=start,
                protected_from=protected_from,
                fixed_messages=fixed_messages,
                max_growth_tokens=max_growth_tokens,
                timeout=timeout,
                authorize=authorize,
                commit=commit,
                started=started,
                target=target,
                trigger=trigger,
                stream=stream,
                record_usage=record_usage,
                capacity_groups=capacity_groups,
                rejected_input_estimate=rejected_input_estimate,
                rejection=rejection,
            )
        finally:
            self._active = False

    async def _prepare(self, request: LLMRequest, **args) -> MaintenanceResult:
        router = args["router"]
        route = {"profile": args["profile"], "user_pref": args["user_pref"]}

        async def measure(value, *, target=args["target"], stream=args["stream"]):
            try:
                return await router.request_context_budget(
                    args["agent_type"], value, **route, target=target, stream=stream, reject_over_capacity=False
                )
            except SummaryTransportUnsupported:
                if not value.summary_only:
                    raise
                return None

        await args["authorize"]()
        before = await measure(request)
        summary_response = None
        attempted = False

        manual = args["trigger"] == "manual"

        async def continue_without_summary(reason: str) -> MaintenanceResult:
            if not manual and (not before.budget.fits or args["trigger"] == "capacity"):
                from bowhead_router.request_record import note_request_change

                from bowhead_core.context_capacity import capacity_projection

                groups = args["capacity_groups"]
                if groups is None:
                    cuts = [cut for cut in balanced_boundaries(request.messages) if args["start"] <= cut <= args["protected_from"]]
                    groups = tuple(zip(cuts, cuts[1:]))
                rejected = args["rejected_input_estimate"]
                if args["trigger"] == "capacity" and rejected is None and before.budget.fits:
                    rejected = before.budget.input_estimate
                projection = await capacity_projection(
                    request, before=before, groups=groups, measure=measure, rejected_input_estimate=rejected, rejection=args["rejection"]
                )
                if projection is not None:
                    prepared, after, removed = projection
                    await args["authorize"]()
                    note_request_change(
                        "history_turns_dropped",
                        reason="context_capacity_fallback",
                        messages_dropped=removed,
                        input_before=before.budget.input_estimate,
                        input_after=after.budget.input_estimate,
                        provider=after.provider,
                        model=after.model,
                    )
                    return MaintenanceResult(
                        prepared,
                        skipped=reason,
                        response=summary_response,
                        attempted=attempted,
                        budget=after,
                        trimmed_messages=removed,
                    )
                if not before.policy.enabled:
                    reason = "no_removable_capacity_history"
            if not before.budget.fits:
                error = ContextCapacityError(
                    "The request cannot fit the configured model context. Compact the conversation or select a larger model.",
                    model=before.model,
                    budget=before.budget,
                )
                error.provider = before.provider
                error.configuration_version = before.configuration_version
                raise error
            return MaintenanceResult(request, skipped=reason, response=summary_response, attempted=attempted, budget=before)

        if not manual and not before.policy.enabled:
            return await continue_without_summary("automatic_compaction_disabled")
        controller = args["controller"]
        decisions = 1 if manual else 2
        if controller.budget.step_limit is not None and controller.used + decisions > controller.budget.step_limit:
            return await continue_without_summary("insufficient_remaining_decisions")
        if args["timeout"] <= 0:
            return await continue_without_summary("insufficient_remaining_time")
        span = select_compaction_span(request.messages, start=args["start"], protected_from=args["protected_from"], keep_recent_tokens=0)
        if span is None:
            return await continue_without_summary("no_eligible_messages")
        key = (span.source_digest, (before.provider, before.model), args["trigger"])
        if self._attempted == key:
            return await continue_without_summary("unchanged_source_already_attempted")
        output = summary_output_target(before.budget.context_window)
        draft = summary_request(request, span, output_tokens=output)
        summary = await measure(draft, stream=False)
        if summary is None:
            return await continue_without_summary("summary_transport_unavailable")
        # Fixed accounting is read-only. Full-request source admission has already
        # succeeded; this synthetic projection can never be sent to a provider.
        fixed = await measure(replace(request, messages=args["fixed_messages"], input_admission=None))
        resolved = resolve_context_policy(
            before.policy,
            ordinary=before.budget,
            summary=summary.budget,
            fixed_input_tokens=fixed.budget.input_estimate,
            max_growth_tokens=args["max_growth_tokens"],
        )
        if args["trigger"] == "threshold" and before.budget.fits and before.budget.input_estimate <= resolved.trigger_input_tokens:
            return await continue_without_summary("below_trigger")
        span = select_compaction_span(
            request.messages, start=args["start"], protected_from=args["protected_from"], keep_recent_tokens=resolved.keep_recent_tokens
        )
        if span is None:
            return await continue_without_summary("protected_recent_messages")
        draft = summary_request(request, span, output_tokens=output)
        summary = await measure(draft, stream=False)
        if summary is None:
            return await continue_without_summary("summary_transport_unavailable")
        if not summary.budget.fits:
            # Only an actual selected fallback may use the already admitted
            # primary to summarize. Never restart the ordinary routing chain.
            primary = router.resolve(args["agent_type"], **route).primary
            if args["target"] is not None and args["target"] != primary:
                summary = await measure(draft, target=primary, stream=False)
                if summary is None:
                    return await continue_without_summary("summary_transport_unavailable")
            if not summary.budget.fits and request.input_admission is None:
                # An imported history or smaller model can already exceed the
                # summary window. Read bounded complete partitions; never drop
                # the untouched history or a protected current transaction.
                destination = (summary.provider, summary.model)
                cuts = [cut for cut in balanced_boundaries(request.messages) if span.start < cut <= span.end]
                low, high, selected = 0, len(cuts) - 1, None
                while low <= high:
                    middle = (low + high) // 2
                    partition = replace(span, end=cuts[middle], covered_digest=message_digest(request.messages[span.start : cuts[middle]]))
                    proposed = bounded_summary_request(request, partition, protected_from=args["protected_from"], output_tokens=output)
                    measured = await measure(proposed, target=destination, stream=False)
                    if measured is None:
                        return await continue_without_summary("summary_transport_unavailable")
                    if measured.budget.fits:
                        selected = partition, proposed, measured
                        low = middle + 1
                    else:
                        high = middle - 1
                if selected is not None:
                    span, draft, summary = selected
            if not summary.budget.fits:
                return await continue_without_summary("summary_input_cannot_fit")
        await args["authorize"]()
        if not controller.admit():
            return await continue_without_summary("insufficient_remaining_decisions")
        attempted = True
        self._attempted = key
        operation_id = "mco_" + uuid4().hex
        with controller.owned():
            await args["started"](operation_id, args["trigger"], before)
            try:
                response = await asyncio.wait_for(
                    router.invoke(args["agent_type"], draft, **route, target=(summary.provider, summary.model)), timeout=args["timeout"]
                )
            except (RouteTargetsExhaustedError, TimeoutError, SummaryTransportUnsupported) as error:
                can_continue = isinstance(error, (TimeoutError, SummaryTransportUnsupported)) or (
                    bool(error.failures)
                    and all(
                        not failure.first_visible_chunk_emitted
                        and failure.kind
                        in {
                            ProviderFailureKind.TIMEOUT,
                            ProviderFailureKind.NETWORK,
                            ProviderFailureKind.RATE_LIMITED,
                            ProviderFailureKind.UPSTREAM,
                            ProviderFailureKind.REQUEST_REJECTED,
                            ProviderFailureKind.RESPONSE_INVALID,
                            ProviderFailureKind.CONTEXT_OVERFLOW,
                        }
                        for failure in error.failures
                    )
                )
                if manual or not can_continue:
                    raise
                await args["authorize"]()
                return await continue_without_summary("summary_call_failed")
            summary_response = response
            if args["record_usage"] is not None:
                args["record_usage"](response)
            try:
                candidate_messages = summarized_messages(request.messages, span, response, max_summary_tokens=output)
            except CompactionPreparationError:
                return await continue_without_summary("summary_result_invalid")
            prepared = replace(request, messages=candidate_messages, context_budget=None, output_budget=None)
            after = await measure(prepared)
            if after.budget.input_estimate >= before.budget.input_estimate:
                return await continue_without_summary("summary_reduction_insufficient")
            await args["authorize"]()
            if message_digest(request.messages) != span.source_digest:
                raise CompactionPreparationError("context_source_changed")
            candidate = ContextCandidate(operation_id, args["trigger"], span, candidate_messages, before, after, response)
            await args["commit"](candidate)
        return MaintenanceResult(
            prepared,
            candidate=candidate,
            response=response,
            attempted=True,
            budget=after,
            needs_more=not after.budget.fits or after.budget.input_estimate > resolved.trigger_input_tokens,
        )
