"""Inline model-view ownership inside an already admitted Agent execution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import replace
from inspect import isawaitable
from pathlib import Path
from typing import Any

from bowhead_router.context_policy import TargetContextBudget
from bowhead_router.runtime_notes import OUTPUT_RECOVERY_STATE_NAME
from bowhead_router.types import LLMRequest, LLMResponse, Message

from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_storage import ReferenceCipher
from bowhead_core.context_capacity import capacity_message_groups
from bowhead_core.context_compaction import CompactionPreparationError, balanced_boundaries, message_digest
from bowhead_core.context_dependencies import adoption_source_validator
from bowhead_core.context_maintenance import ContextCandidate, ContextMaintenance, DecisionController, MaintenanceResult, Trigger
from bowhead_core.context_ownership import ContextOwner, require_context_owner_in_tx
from bowhead_core.context_store import (
    ContextCompletion,
    ContextLane,
    ContextOrigin,
    ContextStoreError,
    SourceValidator,
    prepare_context_messages,
    read_context_graph_in_tx,
    write_context_in_tx,
)
from bowhead_core.context_view import ContextReplacement, ContextView, eligible_context_range

Activity = Callable[[dict[str, Any]], Awaitable[None]]


def _is_runtime_note(message: Message) -> bool:
    return message.role == "user" and message.name == OUTPUT_RECOVERY_STATE_NAME


class ExecutionContextView:
    """Per-call state; never attach this mutable object to a shared Runner.

    Original worker checks, Tool dispatch, source authorization, usage and Turn
    finalization remain with their existing owners. This class only publishes
    a model projection after its encrypted generation has committed.
    """

    def __init__(
        self,
        *,
        db_path: str | Path,
        worker: ContextOwner,
        execution_id: str,
        lane: ContextLane,
        cipher: ReferenceCipher,
        validate_sources: SourceValidator,
        activity: Activity,
        subagent_id: str | None = None,
        profile: str = "lead",
        projection_key: str | None = None,
        source_dependencies: Callable[[], list[Message] | Awaitable[list[Message]]] | None = None,
        inherited_generation: str | None = None,
    ) -> None:
        self.db_path = db_path
        self.worker = worker
        self.execution_id = execution_id
        self.lane = lane
        self.cipher = cipher
        self.validate_sources = validate_sources
        self.activity = activity
        self.subagent_id = subagent_id
        self.profile = profile
        self.projection_key = projection_key
        self.source_dependencies = source_dependencies
        self.inherited_generation = inherited_generation
        self.last_budget: TargetContextBudget | None = None
        self.last_request: LLMRequest | None = None
        self.view = ContextView()
        self.maintenance = ContextMaintenance()
        self.generation: str | None = None
        self._loaded = False

    async def _dependencies(self) -> list[Message]:
        values = self.source_dependencies() if self.source_dependencies is not None else []
        return await values if isawaitable(values) else values

    async def _prepare_messages(self, messages):
        return await prepare_context_messages(messages, worker=self.worker, execution_id=self.execution_id, lane=self.lane, cipher=self.cipher)

    async def _prepare_source_validation(self, *batches: list[Message], for_publication: bool = False) -> SourceValidator:
        from bowhead_core.context_dependencies import ContextSourceValidator, PreparedContextSourceValidator

        if not isinstance(self.validate_sources, ContextSourceValidator):
            return self.validate_sources
        prepared = PreparedContextSourceValidator(self.validate_sources)
        # Reading under WAL does not hold the shared writer lock. The write
        # transaction rereads this ancestry and rechecks current authority.
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("BEGIN")
            for identity in dict.fromkeys(value for value in (self.generation, self.inherited_generation) if value):
                await read_context_graph_in_tx(
                    db,
                    worker=self.worker,
                    execution_id=self.execution_id,
                    lane=self.lane,
                    generation_id=identity,
                    cipher=self.cipher,
                    validate_sources=prepared.collect,
                    projection_only=True,
                    for_publication=for_publication,
                )
            await db.rollback()
        for messages in batches:
            await prepared.collect(messages)
        return prepared

    async def restore(self) -> None:
        if self._loaded:
            return
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("BEGIN")
            await require_context_owner_in_tx(db, worker=self.worker)
            async with db.execute(
                "SELECT generation_id FROM model_context_heads WHERE session_id=? AND execution_id=? AND lane=?",
                (self.worker.session_id, self.execution_id, self.lane),
            ) as cursor:
                row = await cursor.fetchone()
            if row is not None:
                generation = row[0]
                graph = await read_context_graph_in_tx(
                    db,
                    worker=self.worker,
                    execution_id=self.execution_id,
                    lane=self.lane,
                    generation_id=generation,
                    cipher=self.cipher,
                    validate_sources=self.validate_sources,
                    projection_only=True,
                )
                chain = []
                identity = generation
                seen = set()
                while identity is not None:
                    if identity in seen or identity not in graph:
                        raise ContextStoreError("context_projection_ancestry_invalid")
                    seen.add(identity)
                    record = graph[identity]
                    if record.execution_id != self.execution_id or record.lane != self.lane:
                        raise ContextStoreError("context_projection_owner_invalid")
                    if record.completion is not None and record.completion.projection_key == self.projection_key:
                        if record.completion.coverage is None or record.parent_id is None:
                            raise ContextStoreError("context_projection_coverage_unavailable")
                        chain.append(ContextReplacement.committed(graph[record.parent_id].messages, record.completion.coverage, record.messages))
                    identity = record.parent_id
                restored = ContextView()
                for replacement in reversed(chain):
                    restored.adopt(replacement)
                self.view = restored
                self.generation = generation
            await db.rollback()
        self._loaded = True

    async def prepare(
        self,
        request: LLMRequest,
        *,
        router: Any,
        agent_type: str,
        profile: str,
        user_pref: Any,
        controller: DecisionController,
        current_request: int,
        canonical_count: int,
        timeout: float,
        max_growth_tokens: int,
        stream: bool,
        record_usage: Callable[[LLMResponse], None],
        target: tuple[str, str] | None = None,
        trigger: Trigger = "threshold",
        prefix_count: int = 0,
        rejection: BaseException | None = None,
    ) -> MaintenanceResult:
        await self.restore()
        # Recovery counters belong to this execution. Their moving checkpoint
        # marker must never become historical state or a covered summary prefix.
        notes = [message for message in request.messages if _is_runtime_note(message)]
        if notes:
            current_request -= sum(_is_runtime_note(message) for message in request.messages[:current_request])
            canonical_count -= sum(_is_runtime_note(message) for message in request.messages[:canonical_count])
            request = replace(request, messages=[*(message for message in request.messages if not _is_runtime_note(message)), *notes])
        # Coordination keeps an immutable accepted business request outside its
        # own append-only lane. That prefix may grow between suspended batches;
        # it contributes to capacity but must never enter this lane's summary.
        if not 0 <= prefix_count <= current_request < canonical_count <= len(request.messages):
            raise CompactionPreparationError("context_lane_boundary_invalid")
        prefix = request.messages[:prefix_count]
        lane_messages = self.view.project(request.messages[prefix_count:])
        projected = replace(request, messages=[*prefix, *lane_messages], context_budget=None, output_budget=None)
        start, end = eligible_context_range(
            lane_messages,
            current_request=self.view.position(current_request - prefix_count),
            canonical_count=self.view.position(canonical_count - prefix_count),
        )
        start += prefix_count
        end += prefix_count
        source_generation = None
        replacement = None
        operation_id = None

        async def authorize() -> None:
            await self.validate_sources(request.messages)
            async with open_shared_ctx(self.db_path) as db:
                await db.execute("BEGIN")
                await require_context_owner_in_tx(db, worker=self.worker)
                await db.rollback()

        async def started(identity: str, reason: Trigger, budget: TargetContextBudget) -> None:
            nonlocal source_generation, operation_id
            origin = ContextOrigin(provider=budget.provider, model=budget.model, configuration_version=budget.configuration_version)
            dependencies = await self._dependencies()
            validation = await self._prepare_source_validation(dependencies, lane_messages)
            prepared_dependencies = await self._prepare_messages(dependencies) if dependencies else None
            prepared_lane = await self._prepare_messages(lane_messages)
            async with open_shared_ctx(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                validation = adoption_source_validator(validation, db)
                parent = self.generation
                if dependencies:
                    # Source hints can be delivered outside native Tool bodies.
                    # Retain their exact dependencies in encrypted ancestry,
                    # never as model-visible instructions or source authority.
                    parent = await write_context_in_tx(
                        db,
                        worker=self.worker,
                        execution_id=self.execution_id,
                        lane=self.lane,
                        expected_generation=parent,
                        messages=dependencies,
                        prepared_messages=prepared_dependencies,
                        policy=budget.policy,
                        origin=origin,
                        cipher=self.cipher,
                        validate_sources=validation,
                        source_refs=(self.inherited_generation,) if self.inherited_generation else (),
                    )
                source_generation = await write_context_in_tx(
                    db,
                    worker=self.worker,
                    execution_id=self.execution_id,
                    lane=self.lane,
                    expected_generation=parent,
                    messages=lane_messages,
                    prepared_messages=prepared_lane,
                    policy=budget.policy,
                    origin=origin,
                    cipher=self.cipher,
                    validate_sources=validation,
                    source_refs=(self.inherited_generation,) if self.inherited_generation else (),
                )
                await db.commit()
            self.generation = source_generation
            operation_id = identity
            await self.activity({"operation_id": identity, "phase": "running", "trigger": reason, "source_generation": source_generation})

        async def commit(candidate: ContextCandidate) -> None:
            nonlocal replacement
            if source_generation is None or candidate.operation_id != operation_id:
                raise ContextStoreError("context_source_checkpoint_required")
            ContextReplacement.committed(projected.messages, candidate.span, candidate.messages)
            if candidate.span.start < prefix_count or candidate.messages[:prefix_count] != prefix:
                raise CompactionPreparationError("context_protected_prefix_replaced")
            lane_candidate = candidate.messages[prefix_count:]
            lane_span = replace(
                candidate.span,
                start=candidate.span.start - prefix_count,
                end=candidate.span.end - prefix_count,
                source_count=len(lane_messages),
                source_digest=message_digest(lane_messages),
            )
            prepared = ContextReplacement.committed(lane_messages, lane_span, lane_candidate)
            origin = ContextOrigin(
                provider=candidate.after.provider, model=candidate.after.model, configuration_version=candidate.after.configuration_version
            )
            validation = await self._prepare_source_validation(lane_candidate)
            prepared_candidate = await self._prepare_messages(lane_candidate)
            async with open_shared_ctx(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                identity = await write_context_in_tx(
                    db,
                    worker=self.worker,
                    execution_id=self.execution_id,
                    lane=self.lane,
                    expected_generation=source_generation,
                    messages=lane_candidate,
                    prepared_messages=prepared_candidate,
                    policy=candidate.after.policy,
                    origin=origin,
                    cipher=self.cipher,
                    validate_sources=validation,
                    completion=ContextCompletion(
                        operation_id=candidate.operation_id,
                        trigger=candidate.trigger,
                        input_before=candidate.before.budget.input_estimate,
                        input_after=candidate.after.budget.input_estimate,
                        coverage=lane_span,
                        subagent_id=self.subagent_id,
                        profile=self.profile,
                        projection_key=self.projection_key,
                    ),
                )
                await db.commit()
            # Nothing before this point changes the view used by an ordinary call.
            self.generation = identity
            self.view.adopt(prepared)
            replacement = prepared

        rejected_input_estimate = None
        if trigger == "capacity" and self.last_request is not None:
            rejected_budget = self.last_budget
            if rejected_budget is None or target != (rejected_budget.provider, rejected_budget.model):
                # A fallback received the prior outgoing projection, not the
                # full native transcript. Recompile that projection for its
                # actual protocol before choosing a further reduction.
                rejected_budget = await router.request_context_budget(
                    agent_type,
                    self.last_request,
                    profile=profile,
                    user_pref=user_pref,
                    target=target,
                    stream=stream,
                    reject_over_capacity=False,
                )
            rejected_input_estimate = rejected_budget.budget.input_estimate

        result = await self.maintenance.prepare(
            projected,
            router=router,
            agent_type=agent_type,
            profile=profile,
            user_pref=user_pref,
            controller=controller,
            start=start,
            protected_from=end,
            fixed_messages=[*projected.messages[:start], *projected.messages[end:]],
            max_growth_tokens=max_growth_tokens,
            timeout=timeout,
            stream=stream,
            authorize=authorize,
            commit=commit,
            started=started,
            target=target,
            trigger=trigger,
            record_usage=record_usage,
            rejection=rejection,
            capacity_groups=tuple(
                (first + prefix_count, last + prefix_count)
                for first, last in capacity_message_groups(
                    lane_messages,
                    current_request=self.view.position(current_request - prefix_count),
                    canonical_count=self.view.position(canonical_count - prefix_count),
                )
            ),
            rejected_input_estimate=rejected_input_estimate,
        )
        if operation_id is not None:
            await self.activity(
                {
                    "operation_id": operation_id,
                    "phase": "committed" if replacement is not None else "skipped",
                    "trigger": trigger,
                    "source_generation": source_generation,
                    "generation_id": self.generation if replacement is not None else None,
                    "reason": result.skipped,
                }
            )
        self.last_budget = result.budget
        self.last_request = result.request
        return result

    async def conversation_publication(self, messages: list[Message]):
        """Prepare an exact snapshot for the original successful Turn finalizer.

        This closure performs no terminal write itself. The caller must invoke
        it before the original status CAS and commit both in the same transaction.
        """
        if self.lane != "ordinary" or self.last_budget is None:
            raise ContextStoreError("context_conversation_candidate_unavailable")
        projected = deepcopy(self.view.project([message for message in messages if not _is_runtime_note(message)]))
        if balanced_boundaries(projected)[-1] != len(projected):
            raise ContextStoreError("context_conversation_has_pending_tools")
        dependencies = deepcopy(await self._dependencies())
        from bowhead_core.context_publication import ContextPublication

        validation = await self._prepare_source_validation(projected, dependencies, for_publication=True)
        return ContextPublication(
            db_path=self.db_path,
            worker=self.worker,
            execution_id=self.execution_id,
            cipher=self.cipher,
            validate_sources=validation,
            messages=projected,
            dependencies=dependencies,
            budget=self.last_budget,
            expected_generation=self.generation,
            inherited_generation=self.inherited_generation,
        )
