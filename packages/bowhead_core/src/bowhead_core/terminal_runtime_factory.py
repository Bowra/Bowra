"""Admission/claim factory for Terminal Protocol v1 Web runtimes."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace

from bowhead_core.chat_turn_source import ChatTurnUserSource
from bowhead_core.language_context import LanguageContext
from bowhead_core.model_clock import known_time_zone_name, request_time_zone_name
from bowhead_core.terminal_effect_contract import EffectIntentDisposition
from bowhead_core.terminal_generation_bus import GenerationEventBusRegistry
from bowhead_core.terminal_protocol import (
    AdmissionContext,
    AuthFenceResolveContext,
    AuthFenceScope,
    AuthFenceScopeKind,
    CommittedReceipt,
    ControlGenerationRequest,
    DecisionAction,
    DecisionContext,
    DecisionOpenDisposition,
    DecisionPreflightSnapshot,
    DecisionPrincipalSet,
    DecisionRecordRequest,
    DecisionSideEffectPlan,
    DecisionSourceKind,
    GenerationClaimContext,
    RecordedDecision,
    ResumeGenerationRequest,
    TerminalAdmissionRequest,
    TerminalFollowupCapability,
    TerminalProtocolFutureVersionError,
    TerminalProtocolMalformedError,
    TerminalScope,
    TerminalStatus,
    canonical_decision_payload_digest_for,
    canonical_digest_v1,
    canonical_json_v1,
    stable_decision_action_id,
    stable_decision_id,
)
from bowhead_core.terminal_protocol_store import (
    AdmissionDisposition,
    DecisionConflictError,
    GenerationClaimDisposition,
    TerminalAdmissionResult,
    TerminalProtocolFaultPoint,
    TerminalProtocolStore,
)
from bowhead_core.terminal_runtime import TerminalRuntimeIdentity, TerminalTurnRuntime
from bowhead_core.types import TurnRequest


class TerminalRuntimeAdmissionError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        decision_id: str | None = None,
        generation: int | None = None,
        admission_may_have_committed: bool = True,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.decision_id = decision_id
        self.generation = generation
        self.admission_may_have_committed = admission_may_have_committed


class TerminalRuntimePreAdmissionCancelled(asyncio.CancelledError):
    """Cancellation proven to precede every Terminal Store mutation."""

    admission_may_have_committed = False


def web_start_execution_intent(
    request: TurnRequest,
    user_source: ChatTurnUserSource,
) -> dict[str, object]:
    """Build the one typed Web-start envelope consumed by admission and replay."""

    intent: dict[str, object] = {
        "active_datarc_project": request.active_datarc_project,
        "attachment_refs": list(user_source.attachment_refs),
        "attachments": list(request.attachments),
        "language_context": (request.language_context.to_dict() if request.language_context is not None else None),
        "member_uid": request.member_uid,
        "parts": (list(user_source.parts) if user_source.parts is not None else None),
        "planning_mode": request.planning_mode,
        "preferred_model": request.preferred_model,
        "preferred_provider": request.preferred_provider,
        "paradigm_v2": request.paradigm_v2,
        "text": request.text,
        "workspace_id": request.workspace_id,
    }
    if request.agent_draft_target is not None:
        intent["agent_draft_target"] = request.agent_draft_target.model_dump()
    if request.plan_execution_id is not None:
        intent["plan_execution_id"] = request.plan_execution_id
    if request.expected_plan_revision is not None:
        intent["expected_plan_revision"] = request.expected_plan_revision
    if request.language_context is None and (zone := known_time_zone_name(request.viewer_time_zone)):
        # No language snapshot (i18n disabled) to carry the viewer zone: keep it beside it.
        intent["viewer_time_zone"] = zone
    return intent


def _add_followup_time_zone(intent: dict[str, object], request: TurnRequest) -> dict[str, object]:
    """Carry the parent Turn's viewer zone into a queued follow-up (omitted when none)."""
    if zone := request_time_zone_name(request):
        intent["time_zone"] = zone
    return intent


@dataclass(frozen=True, slots=True)
class TerminalWriterSnapshot:
    instance_id: str
    lease_epoch: int
    admission_epoch: int
    rollout_config_generation: str
    recovery_only: bool = False

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value for value in (self.instance_id, self.rollout_config_generation)):
            raise ValueError("terminal writer snapshot identity is invalid")
        for value in (self.lease_epoch, self.admission_epoch):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError("terminal writer snapshot epoch is invalid")
        if not isinstance(self.recovery_only, bool):
            raise TypeError("terminal writer capability kind is invalid")


@dataclass(frozen=True, slots=True)
class TerminalPreparedDecision:
    decision: RecordedDecision
    preflight: DecisionPreflightSnapshot

    def __post_init__(self) -> None:
        if not isinstance(self.decision, RecordedDecision):
            raise TypeError("prepared terminal decision is invalid")
        if not isinstance(self.preflight, DecisionPreflightSnapshot):
            raise TypeError("prepared terminal decision preflight is invalid")


def _consume_task_result(task: asyncio.Task[object]) -> None:
    """Keep detached single-flight failures observable without loop warnings."""

    def consume(done: asyncio.Task[object]) -> None:
        if not done.cancelled():
            done.exception()

    task.add_done_callback(consume)


class TerminalRuntimeRegistry:
    """Process-local live runtime lookup; persisted state remains authoritative."""

    def __init__(self) -> None:
        self._runtimes: dict[str, TerminalTurnRuntime] = {}
        self._workers: dict[tuple[str, int], asyncio.Task[object]] = {}
        self._worker_start_failures: set[tuple[str, int]] = set()
        self._attach_reservations: dict[
            TerminalScope,
            asyncio.Task[TerminalTurnRuntime],
        ] = {}
        self._lock = asyncio.Lock()

    async def register(self, runtime: TerminalTurnRuntime) -> None:
        async with self._lock:
            existing = self._runtimes.get(runtime.scope.turn_id)
            if existing is not None and existing is not runtime:
                if existing.scope != runtime.scope:
                    # A paused generation has already committed its immutable
                    # receipt, but the compatibility live bus may stay around
                    # briefly for late subscribers.  N+1 resume must be able to
                    # replace that closed generation immediately; the old
                    # runtime's later retire callback is identity-checked and
                    # therefore cannot remove the replacement.
                    if (
                        existing.scope.team_id == runtime.scope.team_id
                        and existing.scope.session_id == runtime.scope.session_id
                        and existing.scope.generation < runtime.scope.generation
                        and existing.bus.receipt is not None
                    ):
                        self._runtimes[runtime.scope.turn_id] = runtime
                        return
                    raise RuntimeError("terminal runtime registry identity conflict")
                raise RuntimeError("terminal runtime already registered")
            self._runtimes[runtime.scope.turn_id] = runtime

    async def get(self, turn_id: str) -> TerminalTurnRuntime | None:
        async with self._lock:
            runtime = self._runtimes.get(turn_id)
            return runtime if runtime is not None and not runtime.retired else None

    async def get_exact(
        self,
        scope: TerminalScope,
    ) -> TerminalTurnRuntime | None:
        """Return only the runtime for the complete generation identity."""

        if not isinstance(scope, TerminalScope):
            raise TypeError("terminal runtime scope is invalid")
        async with self._lock:
            runtime = self._runtimes.get(scope.turn_id)
            if runtime is None or runtime.retired or runtime.scope != scope:
                return None
            return runtime

    async def retire(self, runtime: TerminalTurnRuntime) -> None:
        async with self._lock:
            if self._runtimes.get(runtime.scope.turn_id) is runtime:
                self._runtimes.pop(runtime.scope.turn_id, None)
            self._workers.pop(
                (runtime.scope.turn_id, runtime.scope.generation),
                None,
            )
            self._worker_start_failures.discard(
                (runtime.scope.turn_id, runtime.scope.generation),
            )

    async def attach_runtime_once(
        self,
        scope: TerminalScope,
        attach: Callable[[], Awaitable[TerminalTurnRuntime]],
    ) -> TerminalTurnRuntime:
        """Single-flight one complete claim-and-register operation per scope."""

        if not isinstance(scope, TerminalScope):
            raise TypeError("terminal runtime scope is invalid")
        async with self._lock:
            existing = self._runtimes.get(scope.turn_id)
            if existing is not None and not existing.retired and existing.scope == scope:
                return existing
            reservation = self._attach_reservations.get(scope)
            if reservation is None:
                reservation = asyncio.create_task(
                    self._complete_runtime_attach(scope, attach),
                    name=f"terminal-runtime-attach:{scope.turn_id}:{scope.generation}",
                )
                self._attach_reservations[scope] = reservation
                _consume_task_result(reservation)
        return await asyncio.shield(reservation)

    async def _complete_runtime_attach(
        self,
        scope: TerminalScope,
        attach: Callable[[], Awaitable[TerminalTurnRuntime]],
    ) -> TerminalTurnRuntime:
        reservation = asyncio.current_task()
        assert reservation is not None
        try:
            runtime = await attach()
            if not isinstance(runtime, TerminalTurnRuntime) or runtime.scope != scope:
                raise RuntimeError("terminal runtime attach identity changed")
            async with self._lock:
                if runtime.retired or self._runtimes.get(scope.turn_id) is not runtime:
                    raise RuntimeError("terminal runtime attach was not registered")
            return runtime
        finally:
            async with self._lock:
                if self._attach_reservations.get(scope) is reservation:
                    self._attach_reservations.pop(scope, None)

    async def spawn_worker_once(
        self,
        scope: TerminalScope,
        spawn: Callable[[], asyncio.Task[object]],
        *,
        expected_runtime: TerminalTurnRuntime | None = None,
    ) -> tuple[asyncio.Task[object], bool]:
        """Validate, create, and bind one worker without an await-sized race."""

        key = (scope.turn_id, scope.generation)
        async with self._lock:
            if expected_runtime is not None and (
                expected_runtime.scope != scope or expected_runtime.retired or self._runtimes.get(scope.turn_id) is not expected_runtime
            ):
                raise RuntimeError("terminal runtime is no longer active")
            if key in self._worker_start_failures:
                raise RuntimeError("terminal worker start is fenced after creation failure")
            existing = self._workers.get(key)
            if existing is not None:
                return existing, False
            try:
                task = spawn()
                if not isinstance(task, asyncio.Task):
                    raise TypeError("terminal worker spawner did not return a Task")
            except BaseException:
                self._worker_start_failures.add(key)
                raise
            self._workers[key] = task
            return task, True

    @property
    def size(self) -> int:
        return len(self._runtimes)


class TerminalRuntimeFactory:
    def __init__(
        self,
        store: TerminalProtocolStore,
        generation_buses: GenerationEventBusRegistry,
        runtimes: TerminalRuntimeRegistry,
        writer: TerminalWriterSnapshot,
        recovery_trigger: Callable[[TerminalScope, str], Awaitable[None] | None] | None = None,
        writer_snapshot_provider: Callable[[], TerminalWriterSnapshot] | None = None,
        worker_task_observer: Callable[[asyncio.Task[object]], None] | None = None,
        writer_ready: Callable[[], Awaitable[bool]] | None = None,
        fresh_admission_ready: Callable[[], bool] | None = None,
        writer_idle_release: Callable[[], Awaitable[bool]] | None = None,
        followup_capability: TerminalFollowupCapability | None = None,
        followup_worker_spawner: Callable[[TurnRequest], Awaitable[object]] | None = None,
        followup_request_resolver: Callable[
            [Mapping[str, object], TerminalScope],
            Awaitable[TurnRequest],
        ]
        | None = None,
        fault_injector: Callable[[TerminalProtocolFaultPoint], None] | None = None,
    ) -> None:
        self._store = store
        self._generation_buses = generation_buses
        self._runtimes = runtimes
        self._writer = writer
        self._writer_snapshot_provider = writer_snapshot_provider
        self._worker_task_observer = worker_task_observer
        self._writer_ready = writer_ready
        self._fresh_admission_ready = fresh_admission_ready
        self._writer_idle_release = writer_idle_release
        self._recovery_trigger = recovery_trigger
        if (
            followup_capability is not None
            and followup_capability.certified
            and (followup_worker_spawner is None or followup_request_resolver is None)
        ):
            raise ValueError("certified follow-up requires current-context adapters")
        self._followup_capability = followup_capability
        self._followup_worker_spawner = followup_worker_spawner
        self._followup_request_resolver = followup_request_resolver
        self._fault_injector = fault_injector

    def _inject(self, point: TerminalProtocolFaultPoint) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)

    def _current_writer(self) -> TerminalWriterSnapshot:
        if self._writer_snapshot_provider is None:
            return self._writer
        return self._writer_snapshot_provider()

    async def _ensure_writer_ready(self) -> TerminalWriterSnapshot:
        try:
            if self._writer_ready is not None and not await self._writer_ready():
                raise RuntimeError("terminal writer is not ready")
            return self._current_writer()
        except asyncio.CancelledError as exc:
            raise TerminalRuntimePreAdmissionCancelled(
                "terminal writer readiness cancelled",
            ) from exc
        except Exception as exc:
            raise TerminalRuntimeAdmissionError(
                "terminal_protocol_writer_not_ready",
                admission_may_have_committed=False,
            ) from exc

    def _fresh_writer(self) -> TerminalWriterSnapshot:
        try:
            if self._fresh_admission_ready is not None and not self._fresh_admission_ready():
                raise RuntimeError("terminal fresh admission is not ready")
            writer = self._current_writer()
        except Exception as exc:
            raise TerminalRuntimeAdmissionError(
                "terminal_protocol_writer_not_ready",
                admission_may_have_committed=False,
            ) from exc
        if writer.recovery_only:
            raise TerminalRuntimeAdmissionError(
                "terminal_protocol_writer_not_ready",
                admission_may_have_committed=False,
            )
        return writer

    async def prepare_web_auth_fence(
        self,
        *,
        team_id: str,
        session_id: str,
        user_id: str,
    ) -> str:
        """Capture the vector before Gateway performs its full ACL preflight."""

        context = terminal_auth_resolve_context_values(
            team_id=team_id,
            session_id=session_id,
            user_id=user_id,
            resource_ref=session_id,
        )
        vector = await self._store.ensure_auth_scope_epochs(context)
        return vector.auth_fence_vector_digest

    async def authorize_active_web_control(
        self,
        *,
        team_id: str,
        session_id: str,
        user_id: str,
        expected_turn_id: str,
        auth_fence_vector_digest: str,
    ) -> TerminalScope:
        """Revalidate an in-process Web control against the exact active v1 tuple."""

        try:
            writer = self._current_writer()
        except RuntimeError as exc:
            raise TerminalRuntimeAdmissionError("terminal_protocol_writer_not_ready") from exc
        context = AdmissionContext(
            team_id=team_id,
            session_id=session_id,
            request_id=f"control:{expected_turn_id}",
            surface="web",
            cohort=f"all;config={writer.rollout_config_generation}",
            actor=user_id,
            writer_instance_id=writer.instance_id,
            writer_lease_epoch=writer.lease_epoch,
            writer_admission_epoch=writer.admission_epoch,
            rollout_config_generation=writer.rollout_config_generation,
            auth_fence_vector_digest=auth_fence_vector_digest,
        )
        authorized = await self._store.authorize_active_v1_control(
            context=context,
            expected_turn_id=expected_turn_id,
        )
        if authorized.scope is None:
            assert authorized.code is not None
            raise TerminalRuntimeAdmissionError(authorized.code.value)
        return authorized.scope

    async def commit_active_web_cancel(
        self,
        scope: TerminalScope,
    ) -> CommittedReceipt:
        """Durably cancel the exact in-process generation authorized by Web."""

        if not isinstance(scope, TerminalScope):
            raise TypeError("terminal runtime scope is invalid")
        runtime = await self._runtimes.get_exact(scope)
        if runtime is None:
            raise TerminalRuntimeAdmissionError(
                "terminal_dependency_recovering",
                generation=scope.generation,
            )
        try:
            receipt = await runtime.bus.commit_cancelled()
        except asyncio.CancelledError as exc:
            # Preserve task cancellation, but first hand an unconfirmed scope
            # to recovery.  Shielding prevents a disconnect from cancelling
            # the recovery notification itself.
            recovery_task = asyncio.create_task(
                self._trigger_recovery(
                    scope,
                    "web_cancel_commit_cancelled",
                    exc,
                )
            )
            # A repeated cancellation still leaves the shielded recovery task
            # running; the original cancellation remains authoritative.
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(recovery_task)
            raise
        except BaseException as exc:
            await self._trigger_recovery(scope, "web_cancel_commit_failed", exc)
            raise TerminalRuntimeAdmissionError(
                "terminal_dependency_recovering",
                generation=scope.generation,
            ) from exc
        if runtime.scope != scope or receipt.status not in {
            TerminalStatus.COMPLETED,
            TerminalStatus.FAILED,
            TerminalStatus.CANCELLED,
            TerminalStatus.ABORTED,
            TerminalStatus.CLARIFICATION_PENDING,
            TerminalStatus.APPROVAL_PENDING,
        }:
            error = RuntimeError("terminal web cancel receipt identity is invalid")
            await self._trigger_recovery(scope, "web_cancel_receipt_invalid", error)
            raise error
        return receipt

    async def admit_web_turn(
        self,
        request: TurnRequest,
        *,
        request_id: str,
        presentation_locale: str,
        user_source: ChatTurnUserSource | None = None,
        create_session_if_missing: bool = False,
        require_existing_v1_history: bool = False,
    ) -> TerminalTurnRuntime:
        if not isinstance(require_existing_v1_history, bool):
            raise TypeError("terminal admission protocol history fence is invalid")
        writer = await self._ensure_writer_ready() if require_existing_v1_history else self._fresh_writer()
        team_id = request.team_id or ""
        if not team_id or not request.session_id or not request.user_id:
            raise TerminalRuntimeAdmissionError(
                "terminal_admission_identity_missing",
                admission_may_have_committed=False,
            )
        expected_auth_digest = request.terminal_auth_fence_vector_digest
        if not expected_auth_digest:
            raise TerminalRuntimeAdmissionError(
                "terminal_auth_fence_missing",
                admission_may_have_committed=False,
            )
        typed_start = user_source is not None
        if request.language_context is not None and not isinstance(
            request.language_context,
            LanguageContext,
        ):
            raise TerminalRuntimeAdmissionError(
                "terminal_execution_envelope_malformed",
                admission_may_have_committed=False,
            )
        execution_intent = (
            web_start_execution_intent(request, user_source)
            if user_source is not None
            else {
                "attachments": list(request.attachments),
                "planning_mode": request.planning_mode,
                "text": request.text,
                "workspace_id": request.workspace_id,
            }
        )
        admission_request = TerminalAdmissionRequest(
            context=AdmissionContext(
                team_id=team_id,
                session_id=request.session_id,
                request_id=request_id,
                surface="web",
                cohort=(
                    f"sticky;config={writer.rollout_config_generation}"
                    if require_existing_v1_history
                    else f"all;config={writer.rollout_config_generation}"
                ),
                actor=request.user_id,
                writer_instance_id=writer.instance_id,
                writer_lease_epoch=writer.lease_epoch,
                writer_admission_epoch=writer.admission_epoch,
                rollout_config_generation=writer.rollout_config_generation,
                auth_fence_vector_digest=expected_auth_digest,
            ),
            turn_id=request.turn_id,
            user_text=request.text,
            execution_intent=execution_intent,
            presentation_locale=presentation_locale,
            message_catalog_version=1,
            allow_steer=False,
            user_source=user_source,
            create_session_if_missing=create_session_if_missing,
            require_existing_v1_history=require_existing_v1_history,
        )
        try:
            admitted = await self._store.admit_or_steer_v1(admission_request)
        except asyncio.CancelledError as exc:
            if typed_start:
                scope = TerminalScope(
                    team_id=team_id,
                    session_id=request.session_id,
                    turn_id=request.turn_id,
                    generation=1,
                )
                recovery_task = asyncio.create_task(
                    self._trigger_recovery(
                        scope,
                        "web_start_admission_cancelled",
                        exc,
                    )
                )
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(recovery_task)
            raise
        except BaseException as exc:
            if not typed_start:
                raise
            scope = TerminalScope(
                team_id=team_id,
                session_id=request.session_id,
                turn_id=request.turn_id,
                generation=1,
            )
            await self._trigger_recovery(
                scope,
                "web_start_admission_unconfirmed",
                exc,
            )
            raise
        if admitted.disposition not in {
            AdmissionDisposition.ADMITTED,
            AdmissionDisposition.ADMITTED_REPLAY,
        }:
            raise TerminalRuntimeAdmissionError(
                admitted.code.value if admitted.code is not None else "terminal_admission_rejected",
                admission_may_have_committed=False,
            )
        if not typed_start and admitted.disposition is AdmissionDisposition.ADMITTED_REPLAY:
            existing = await self._runtimes.get(request.turn_id)
            if existing is not None:
                return existing
            raise TerminalRuntimeAdmissionError(
                "terminal_admission_replay_pending",
                generation=(admitted.scope.generation if admitted.scope else None),
            )
        assert admitted.scope is not None
        assert admitted.effective_user_text is not None
        assert admitted.effective_execution_intent is not None
        effective = admitted.effective_execution_intent
        expected_target = request.agent_draft_target.model_dump() if request.agent_draft_target is not None else None
        if effective.get("agent_draft_target") != expected_target:
            raise TerminalRuntimeAdmissionError("terminal_execution_envelope_malformed")
        active_datarc_project = effective.get("active_datarc_project")
        attachments = effective.get("attachments")
        attachment_refs = effective.get("attachment_refs", [])
        language_context = effective.get("language_context")
        member_uid = effective.get("member_uid")
        parts = effective.get("parts")
        has_paradigm_v2 = "paradigm_v2" in effective
        paradigm_v2 = effective.get("paradigm_v2", False)
        has_plan_execution_id = "plan_execution_id" in effective
        plan_execution_id = effective.get("plan_execution_id")
        expected_plan_revision = effective.get("expected_plan_revision")
        planning_mode = effective.get("planning_mode")
        preferred_model = effective.get("preferred_model")
        preferred_provider = effective.get("preferred_provider")
        workspace_id = effective.get("workspace_id")
        effective_language_context: LanguageContext | None = None
        if language_context is not None:
            if not isinstance(language_context, dict):
                raise TerminalRuntimeAdmissionError("terminal_execution_envelope_malformed")
            try:
                effective_language_context = LanguageContext.from_dict(
                    language_context,
                )
            except ValueError as exc:
                raise TerminalRuntimeAdmissionError("terminal_execution_envelope_malformed") from exc
            if effective_language_context.to_dict() != language_context:
                raise TerminalRuntimeAdmissionError("terminal_execution_envelope_malformed")
        if (
            not isinstance(attachments, list)
            or any(not isinstance(value, str) for value in attachments)
            or (
                typed_start
                and (
                    (active_datarc_project is not None and not isinstance(active_datarc_project, str))
                    or not isinstance(attachment_refs, list)
                    or any(not isinstance(value, dict) for value in attachment_refs)
                    or (parts is not None and (not isinstance(parts, list) or any(not isinstance(value, dict) for value in parts)))
                    or not isinstance(member_uid, str)
                    or (has_paradigm_v2 and not isinstance(paradigm_v2, bool))
                    or (
                        has_plan_execution_id
                        and (
                            not isinstance(plan_execution_id, str)
                            or not plan_execution_id
                            or plan_execution_id.strip() != plan_execution_id
                            or len(plan_execution_id) > 128
                        )
                    )
                    or active_datarc_project != request.active_datarc_project
                    or member_uid != request.member_uid
                    or (has_paradigm_v2 and paradigm_v2 != request.paradigm_v2)
                    or (has_plan_execution_id and plan_execution_id != request.plan_execution_id)
                    or (request.plan_execution_id is not None and not has_plan_execution_id)
                    or expected_plan_revision != request.expected_plan_revision
                )
            )
            or (preferred_model is not None and not isinstance(preferred_model, str))
            or (preferred_provider is not None and not isinstance(preferred_provider, str))
            or workspace_id != request.workspace_id
        ):
            raise TerminalRuntimeAdmissionError("terminal_execution_envelope_malformed")
        request.text = admitted.effective_user_text
        request.attachments = list(attachments)
        if typed_start:
            request.active_datarc_project = active_datarc_project
            assert isinstance(member_uid, str)
            request.member_uid = member_uid
            if has_paradigm_v2:
                request.paradigm_v2 = paradigm_v2
            if has_plan_execution_id:
                assert isinstance(plan_execution_id, str)
                request.plan_execution_id = plan_execution_id
        request.planning_mode = (
            "planning" if planning_mode is True else "off" if planning_mode is False else planning_mode if isinstance(planning_mode, str) else None
        )
        if typed_start:
            request.preferred_model = preferred_model
            request.preferred_provider = preferred_provider
        assert admitted.effective_presentation_locale is not None
        if effective_language_context is not None and effective_language_context.ui_language != admitted.effective_presentation_locale:
            raise TerminalRuntimeAdmissionError("terminal_execution_envelope_malformed")
        if typed_start:
            request.language_context = effective_language_context
            request.viewer_time_zone = known_time_zone_name(effective.get("viewer_time_zone"))
        followup_execution_intent: dict[str, object] = {
            "active_datarc_project": request.active_datarc_project,
            "attachments": list(attachments),
            "member_uid": request.member_uid,
            "planning_mode": request.planning_mode,
            "presentation_locale": admitted.effective_presentation_locale,
            "user_id": request.user_id,
            "workspace_id": request.workspace_id,
        }
        _add_followup_time_zone(followup_execution_intent, request)
        if has_paradigm_v2:
            followup_execution_intent["paradigm_v2"] = request.paradigm_v2
        if admitted.disposition is AdmissionDisposition.ADMITTED_REPLAY:
            existing = await self._runtimes.get_exact(admitted.scope)
            if existing is not None:
                return existing
        try:
            return await self._attach_opened_scope(
                admitted.scope,
                authz_snapshot_digest=expected_auth_digest,
                claim_auth_principal_ref=request.user_id,
                followup_execution_intent=followup_execution_intent,
                require_open_writer=(not require_existing_v1_history),
            )
        except TerminalRuntimeAdmissionError as exc:
            if admitted.disposition is not AdmissionDisposition.ADMITTED_REPLAY:
                raise
            existing = await self._runtimes.get_exact(admitted.scope)
            if existing is not None:
                return existing
            if exc.code == "terminal_auth_fence_changed":
                raise
            raise TerminalRuntimeAdmissionError(
                "terminal_admission_recovery_pending",
                generation=admitted.scope.generation,
            ) from exc

    async def record_source_decision(
        self,
        *,
        team_id: str,
        session_id: str,
        turn_id: str,
        source_kind: DecisionSourceKind,
        source_id: str,
        action: DecisionAction,
        principals: DecisionPrincipalSet,
        source_values: Mapping[str, object] | None = None,
        side_effects: tuple[DecisionSideEffectPlan, ...] = (),
        side_effect_specs: tuple[tuple[str, str, Mapping[str, object]], ...] = (),
    ) -> TerminalPreparedDecision:
        """Bind a surface decision to the current durable pause tuple."""

        if source_kind is DecisionSourceKind.SYSTEM_CONTROL:
            expected_source_state, target_source_state = "pending", None
        elif source_kind is DecisionSourceKind.CLARIFICATION:
            expected_source_state = "pending"
            target_source_state = {
                DecisionAction.RESUME: "resolved",
                DecisionAction.RESOLVE_WITHOUT_RESUME: "resolved",
                DecisionAction.DISMISS: "dismissed",
                DecisionAction.EXPIRE: "expired",
            }.get(action)
        else:
            expected_source_state = "pending"
            target_source_state = {
                DecisionAction.APPROVE: "approved",
                DecisionAction.REJECT: "rejected",
                DecisionAction.EXPIRE: "expired",
            }.get(action)
        if source_kind is not DecisionSourceKind.SYSTEM_CONTROL and target_source_state is None:
            raise TerminalRuntimeAdmissionError("terminal_decision_action_invalid")
        await self._ensure_decision_auth_scopes(
            team_id=team_id,
            session_id=session_id,
            principals=principals,
        )
        try:
            preflight = await self._store.read_decision_preflight(
                team_id=team_id,
                session_id=session_id,
                turn_id=turn_id,
                source_kind=source_kind,
                source_id=source_id,
                action=action,
                principals=principals,
            )
        except DecisionConflictError:
            replay = await self._store.read_source_decision(
                team_id=team_id,
                session_id=session_id,
                turn_id=turn_id,
                source_kind=source_kind,
                source_id=source_id,
                action=action,
                principals=principals,
            )
            if replay is None:
                raise
            # Rebuild the caller's complete canonical request against the
            # durable tuple below. ``record_decision`` validates source values
            # and side-effect plans against the winner, so a conflicting retry
            # cannot be reported as a successful replay after N+1 opened.
            preflight = replay[1]
        if source_kind is DecisionSourceKind.SYSTEM_CONTROL and source_id == "__auto_control__":
            source_id = f"{turn_id}:{preflight.scope.generation}:control"
        source_values_json = canonical_json_v1(dict(source_values)) if source_values is not None else None
        context = DecisionContext(
            scope=preflight.scope,
            decision_id=stable_decision_id(
                scope=preflight.scope,
                source_kind=source_kind,
                source_id=source_id,
                action=action,
            ),
            source_kind=source_kind,
            source_id=source_id,
            action=action,
            principals=principals,
            authz_snapshot_digest=preflight.authz_snapshot_digest,
            prior_receipt_id=preflight.prior_receipt_id,
        )
        if side_effects and side_effect_specs:
            raise ValueError("decision side effects must use one construction form")
        if side_effect_specs:
            side_effects = tuple(
                DecisionSideEffectPlan.from_payload(
                    decision_id=context.decision_id,
                    effect_kind=effect_kind,
                    target_ref=target_ref,
                    payload=payload,
                )
                for effect_kind, target_ref, payload in side_effect_specs
            )
        request = DecisionRecordRequest(
            context=context,
            decision_payload_digest=canonical_decision_payload_digest_for(
                source_kind=source_kind,
                action=action,
                expected_source_state=expected_source_state,
                target_source_state=target_source_state,
                source_values_json=source_values_json,
            ),
            expected_source_state=expected_source_state,
            target_source_state=target_source_state,
            source_values_json=source_values_json,
            side_effects=side_effects,
        )
        try:
            recorded = await self._store.record_decision(request)
        except BaseException as exc:
            # The store has an explicit post-commit-before-ack fault window.
            # Triggering recovery only after a successful await would strand a
            # committed decision when the HTTP task is cancelled or the DB ack
            # is lost.  A pre-commit failure is harmless: recovery finds no
            # durable inventory.
            await self._trigger_recovery(
                preflight.scope,
                "decision_commit_uncertain",
                exc,
            )
            raise
        prepared = TerminalPreparedDecision(
            decision=recorded,
            preflight=preflight,
        )
        marker = RuntimeError("terminal_decision_recorded")
        await self._trigger_recovery(
            preflight.scope,
            "decision_recorded",
            marker,
        )
        return prepared

    async def rehydrate_source_decision(
        self,
        prepared: TerminalPreparedDecision,
    ) -> TerminalPreparedDecision:
        """Replace a caller snapshot with the authoritative durable state."""

        context = prepared.decision.context
        rehydrated = await self._store.read_source_decision(
            team_id=context.scope.team_id,
            session_id=context.scope.session_id,
            turn_id=context.scope.turn_id,
            source_kind=context.source_kind,
            source_id=context.source_id,
            action=context.action,
            principals=context.principals,
        )
        if rehydrated is None:
            raise TerminalRuntimeAdmissionError("terminal_decision_source_missing")
        decision, preflight = rehydrated
        return TerminalPreparedDecision(decision, preflight)

    async def read_existing_source_decision(
        self,
        *,
        team_id: str,
        session_id: str,
        turn_id: str,
        source_kind: DecisionSourceKind,
        source_id: str,
        action: DecisionAction,
        principals: DecisionPrincipalSet,
    ) -> TerminalPreparedDecision | None:
        """Read a durable winner before deciding whether first-use auth applies."""

        rehydrated = await self._store.read_source_decision(
            team_id=team_id,
            session_id=session_id,
            turn_id=turn_id,
            source_kind=source_kind,
            source_id=source_id,
            action=action,
            principals=principals,
        )
        if rehydrated is None:
            return None
        decision, preflight = rehydrated
        return TerminalPreparedDecision(decision, preflight)

    async def open_decision_runtime(
        self,
        prepared: TerminalPreparedDecision,
        request: TurnRequest,
    ) -> TerminalTurnRuntime:
        """Open, claim and attach the sole execution generation for a decision."""

        if request.turn_id != prepared.preflight.scope.turn_id:
            raise TerminalRuntimeAdmissionError("terminal_decision_turn_mismatch")
        execution_intent = {
            "attachments": list(request.attachments),
            "decision_id": prepared.decision.context.decision_id,
            "planning_mode": request.planning_mode,
            "resume_turn_id": request.resume_turn_id,
            "source_id": prepared.decision.context.source_id,
            "text": request.text,
            "workspace_id": request.workspace_id,
        }
        durable_presentation_locale = await self._store.read_persisted_turn_presentation_locale(
            team_id=prepared.decision.context.scope.team_id,
            turn_id=prepared.decision.context.scope.turn_id,
        )
        writer = await self._ensure_writer_ready()
        try:
            opened = await self._store.open_resume_generation(
                ResumeGenerationRequest(
                    decision=prepared.decision,
                    execution_intent_id=stable_decision_action_id(
                        prepared.decision.context.decision_id,
                        "resume_execution",
                    ),
                    execution_intent_digest=canonical_digest_v1(execution_intent),
                    execution_intent=execution_intent,
                    expected_generation_transition_epoch=(prepared.preflight.generation_transition_epoch),
                    expected_session_claim_epoch=0,
                    writer_instance_id=writer.instance_id,
                    writer_lease_epoch=writer.lease_epoch,
                    current_authz_snapshot_digest=(prepared.preflight.authz_snapshot_digest),
                )
            )
        except BaseException as exc:
            # Same commit-ack ambiguity as record_decision.  Recovery must be
            # scheduled before the exception escapes; an opened N+1 is never
            # automatically re-run, only durably interrupted by the reconciler.
            await self._trigger_recovery(
                prepared.preflight.scope,
                "resume_open_commit_uncertain",
                exc,
            )
            raise
        if opened.disposition is DecisionOpenDisposition.REPLAY:
            existing = await self._runtimes.get(request.turn_id)
            if existing is not None and existing.scope == opened.scope:
                return existing
            raise TerminalRuntimeAdmissionError(
                "terminal_admission_recovery_pending",
                decision_id=prepared.decision.context.decision_id,
                generation=(opened.scope.generation if opened.scope else None),
            )
        if opened.disposition is not DecisionOpenDisposition.OPENED or opened.scope is None:
            raise TerminalRuntimeAdmissionError(f"terminal_decision_{opened.disposition.value}")
        return await self._attach_opened_scope(
            opened.scope,
            authz_snapshot_digest=prepared.preflight.authz_snapshot_digest,
            worker_fault_point=(TerminalProtocolFaultPoint.RESUME_BEFORE_TASK_CREATE),
            followup_execution_intent=_add_followup_time_zone(
                {
                    "active_datarc_project": request.active_datarc_project,
                    "attachments": list(request.attachments),
                    "member_uid": request.member_uid,
                    "planning_mode": request.planning_mode,
                    "presentation_locale": durable_presentation_locale,
                    "user_id": request.user_id,
                    "workspace_id": request.workspace_id,
                },
                request,
            ),
        )

    async def spawn_decision_worker(
        self,
        runtime: TerminalTurnRuntime,
        spawn: Callable[[], asyncio.Task[object]],
    ) -> tuple[asyncio.Task[object], bool]:
        """Bind the sole local worker to a claimed decision generation."""

        registered = await self._runtimes.get(runtime.scope.turn_id)
        if registered is not runtime:
            raise TerminalRuntimeAdmissionError("terminal_generation_attach_required")
        return await runtime.spawn_worker_once(spawn)

    async def commit_decision_control(
        self,
        prepared: TerminalPreparedDecision,
        *,
        reason_code: str,
        message_key: str,
    ) -> TerminalPreparedDecision:
        status = (
            TerminalStatus.FAILED
            if reason_code == "terminal_dependency_missing"
            else TerminalStatus.ABORTED
            if prepared.decision.context.action in {DecisionAction.EXPIRE, DecisionAction.REVOKE}
            else TerminalStatus.CANCELLED
        )
        writer = await self._ensure_writer_ready()
        try:
            await self._store.commit_control_generation(
                ControlGenerationRequest(
                    decision=prepared.decision,
                    status=status,
                    reason_code=reason_code,
                    message_key=message_key,
                    writer_instance_id=writer.instance_id,
                    writer_lease_epoch=writer.lease_epoch,
                    writer_admission_epoch=writer.admission_epoch,
                    current_authz_snapshot_digest=(prepared.preflight.authz_snapshot_digest),
                )
            )
        finally:
            if self._writer_idle_release is not None:
                await self._writer_idle_release()
        refreshed = await self._store.read_source_decision(
            team_id=prepared.decision.context.scope.team_id,
            session_id=prepared.decision.context.scope.session_id,
            turn_id=prepared.decision.context.scope.turn_id,
            source_kind=prepared.decision.context.source_kind,
            source_id=prepared.decision.context.source_id,
            action=prepared.decision.context.action,
            principals=prepared.decision.context.principals,
        )
        if refreshed is None:
            raise TerminalRuntimeAdmissionError("terminal_control_decision_missing_after_commit")
        decision, preflight = refreshed
        return TerminalPreparedDecision(decision, preflight)

    async def commit_decision_authorization_denial(
        self,
        prepared: TerminalPreparedDecision,
        *,
        denial_reason_code: str,
    ) -> None:
        """Persist a live-path permanent denial before any N+1 work starts."""

        decision, preflight = await self._store.supersede_pending_decision_with_revoke(
            prepared.decision,
            denial_reason_code=denial_reason_code,
        )
        dependency_failure = denial_reason_code in {
            "dependency_missing",
            "entitlement_denied",
        }
        await self.commit_decision_control(
            TerminalPreparedDecision(decision, preflight),
            reason_code=("terminal_dependency_missing" if dependency_failure else "access_revoked"),
            message_key=("turn.failed" if dependency_failure else "turn.access_revoked"),
        )

    async def steer_web_turn(
        self,
        *,
        team_id: str,
        session_id: str,
        user_id: str,
        expected_turn_id: str,
        expected_execution_generation: int,
        request_id: str,
        user_msg_id: str,
        user_text: str,
        workspace_id: str,
        auth_fence_vector_digest: str,
        presentation_locale: str,
        attachments: tuple[str, ...] = (),
        planning_mode: str | None = None,
    ) -> TerminalAdmissionResult:
        """Atomically accept one exact active-v1 steer and wake its writer."""

        scope = TerminalScope(
            team_id=team_id,
            session_id=session_id,
            turn_id=expected_turn_id,
            generation=expected_execution_generation,
        )
        writer = await self._ensure_writer_ready()
        admission_request = TerminalAdmissionRequest(
            context=AdmissionContext(
                team_id=team_id,
                session_id=session_id,
                request_id=request_id,
                surface="web",
                cohort=f"all;config={writer.rollout_config_generation}",
                actor=user_id,
                writer_instance_id=writer.instance_id,
                writer_lease_epoch=writer.lease_epoch,
                writer_admission_epoch=writer.admission_epoch,
                rollout_config_generation=writer.rollout_config_generation,
                auth_fence_vector_digest=auth_fence_vector_digest,
            ),
            turn_id=expected_turn_id,
            user_text=user_text,
            execution_intent={
                "attachments": list(attachments),
                "planning_mode": planning_mode,
                "text": user_text,
                "workspace_id": workspace_id,
            },
            presentation_locale=presentation_locale,
            message_catalog_version=1,
            allow_steer=True,
            expected_execution_generation=expected_execution_generation,
            user_message_id=user_msg_id,
        )
        try:
            admitted = await self._store.admit_or_steer_v1(
                admission_request,
            )
        except asyncio.CancelledError as exc:
            await self._trigger_recovery_preserving_cancellation(
                scope,
                "steer_admission_cancelled",
                exc,
            )
            raise
        except BaseException as exc:
            await self._trigger_recovery_preserving_cancellation(
                scope,
                "steer_admission_unconfirmed",
                exc,
            )
            raise
        if admitted.disposition not in {
            AdmissionDisposition.STEERED,
            AdmissionDisposition.STEER_REPLAY,
        }:
            raise TerminalRuntimeAdmissionError(admitted.code.value if admitted.code is not None else "terminal_steer_rejected")
        if (
            admitted.scope != scope
            or admitted.active_turn_id != expected_turn_id
            or admitted.active_generation != expected_execution_generation
            or admitted.steer_ack_proposal is None
            or admitted.execution_started is None
        ):
            raise TerminalRuntimeAdmissionError("terminal_steer_identity_missing")
        if not admitted.execution_started:
            return admitted
        try:
            runtime = await self._runtimes.get_exact(admitted.scope)
            if runtime is not None:
                await runtime.bus.materialize_accepted_prefix(
                    admitted.steer_ack_proposal.accept_ordinal,
                )
        except asyncio.CancelledError as exc:
            await self._trigger_recovery_preserving_cancellation(
                admitted.scope,
                "steer_materialize_cancelled",
                exc,
            )
            raise
        except Exception as exc:
            await self._trigger_recovery_preserving_cancellation(
                admitted.scope,
                "steer_materialize_failed",
                exc,
            )
            return admitted
        if runtime is None:
            await self._trigger_recovery_preserving_cancellation(
                admitted.scope,
                "steer_materialize_runtime_missing",
                RuntimeError("terminal steer runtime is unavailable"),
            )
        return admitted

    async def _retire_runtime(self, runtime: TerminalTurnRuntime) -> None:
        retire_error: BaseException | None = None
        try:
            await self._generation_buses.retire(
                runtime.scope,
                claim_epoch=runtime.execution.claim_epoch,
            )
        except BaseException as exc:
            retire_error = exc
        finally:
            await self._runtimes.retire(runtime)
        if retire_error is not None:
            raise retire_error

    async def _attach_opened_scope(
        self,
        scope: TerminalScope,
        *,
        authz_snapshot_digest: str,
        claim_auth_principal_ref: str | None = None,
        followup_execution_intent: Mapping[str, object] | None = None,
        worker_fault_point: TerminalProtocolFaultPoint | None = None,
        require_open_writer: bool = False,
    ) -> TerminalTurnRuntime:
        return await self._runtimes.attach_runtime_once(
            scope,
            lambda: self._attach_opened_scope_owned(
                scope,
                authz_snapshot_digest=authz_snapshot_digest,
                claim_auth_principal_ref=claim_auth_principal_ref,
                followup_execution_intent=followup_execution_intent,
                worker_fault_point=worker_fault_point,
                require_open_writer=require_open_writer,
            ),
        )

    async def _attach_opened_scope_owned(
        self,
        scope: TerminalScope,
        *,
        authz_snapshot_digest: str,
        claim_auth_principal_ref: str | None = None,
        followup_execution_intent: Mapping[str, object] | None = None,
        worker_fault_point: TerminalProtocolFaultPoint | None = None,
        require_open_writer: bool = False,
    ) -> TerminalTurnRuntime:
        try:
            effect_contract = await self._store.read_effect_execution_contract(scope)
        except (
            TerminalProtocolFutureVersionError,
            TerminalProtocolMalformedError,
        ) as exc:
            raise TerminalRuntimeAdmissionError("terminal_effect_contract_unreadable") from exc
        if effect_contract.disposition is EffectIntentDisposition.RECOVERY_ONLY:
            raise TerminalRuntimeAdmissionError("terminal_effect_contract_writer_not_ready")
        writer = self._current_writer()
        generation_bus = await self._generation_buses.get_or_create(
            scope,
            claim_epoch=1,
        )
        try:
            claim = await self._store.claim_generation(
                GenerationClaimContext(
                    scope=scope,
                    expected_generation_row_version=1,
                    expected_session_claim_epoch=0,
                    writer_lease_epoch=writer.lease_epoch,
                    owner_instance_id=writer.instance_id,
                    auth_principal_ref=claim_auth_principal_ref,
                    current_authz_snapshot_digest=(authz_snapshot_digest if claim_auth_principal_ref is not None else None),
                    require_open_writer=require_open_writer,
                )
            )
        except BaseException as exc:
            await self._generation_buses.retire(scope, claim_epoch=1)
            await self._trigger_recovery(scope, "generation_claim_failed", exc)
            raise
        if claim.disposition is not GenerationClaimDisposition.CLAIMED:
            await self._generation_buses.retire(scope, claim_epoch=1)
            error = TerminalRuntimeAdmissionError(claim.code.value if claim.code is not None else "terminal_generation_attach_required")
            await self._trigger_recovery(scope, "generation_claim_rejected", error)
            raise error
        assert claim.context is not None

        async def spawn_worker_once(
            spawn: Callable[[], asyncio.Task[object]],
        ) -> tuple[asyncio.Task[object], bool]:
            def guarded_spawn() -> asyncio.Task[object]:
                if worker_fault_point is not None:
                    self._inject(worker_fault_point)
                task = spawn()
                if self._worker_task_observer is not None:
                    with contextlib.suppress(BaseException):
                        self._worker_task_observer(task)
                return task

            task, started_here = await self._runtimes.spawn_worker_once(
                scope,
                guarded_spawn,
                expected_runtime=runtime,
            )
            return task, started_here

        runtime = TerminalTurnRuntime(
            store=self._store,
            execution=claim.context,
            generation_bus=generation_bus,
            identity=TerminalRuntimeIdentity(
                authz_snapshot_digest=authz_snapshot_digest,
                writer_admission_epoch=writer.admission_epoch,
                owner_instance_id=writer.instance_id,
                rollout_config_generation=(writer.rollout_config_generation),
            ),
            effect_contract=effect_contract,
            followup_capability=self._followup_capability,
            followup_execution_intent=dict(followup_execution_intent or {}),
            followup_committed_callback=(self._spawn_committed_followup if self._followup_worker_spawner is not None else None),
            retire_callback=self._retire_runtime,
            worker_spawn_callback=spawn_worker_once,
            handoff_callback=self._runtime_handoff_completed,
        )
        try:
            await self._runtimes.register(runtime)
        except BaseException as exc:
            try:
                await runtime.handoff_to_recovery(reason_code="runtime_attach_failed")
            except BaseException as handoff_error:
                exc.add_note(f"terminal runtime recovery handoff failed: {handoff_error!r}")
            await self._trigger_recovery(scope, "runtime_attach_failed", exc)
            raise
        return runtime

    async def _spawn_committed_followup(
        self,
        parent: TerminalTurnRuntime,
        receipt: object,
    ) -> None:
        """Claim and spawn a transactionally-created certified child once."""

        spawner = self._followup_worker_spawner
        resolver = self._followup_request_resolver
        if not isinstance(receipt, CommittedReceipt) or spawner is None or resolver is None:
            raise RuntimeError("terminal_followup_adapter_missing")
        self._inject(TerminalProtocolFaultPoint.FOLLOWUP_AFTER_PARENT_RECEIPT_BEFORE_CLAIM)
        template = await self._store.read_committed_followup_template(receipt)
        if template is None:
            raise RuntimeError("terminal_followup_ledger_missing")
        intent = template.get("execution_intent")
        if not isinstance(intent, Mapping):
            raise RuntimeError("terminal_followup_envelope_malformed")
        turn_id = template.get("turn_id")
        user_text = template.get("user_text")
        workspace_id = intent.get("workspace_id")
        user_id = intent.get("user_id")
        member_uid = intent.get("member_uid")
        presentation_locale = intent.get("presentation_locale")
        if (
            any(
                not isinstance(value, str) or not value
                for value in (
                    turn_id,
                    workspace_id,
                    user_id,
                    presentation_locale,
                )
            )
            or not isinstance(user_text, str)
            or not isinstance(member_uid, str)
        ):
            raise RuntimeError("terminal_followup_envelope_malformed")
        child_scope = TerminalScope(
            team_id=parent.scope.team_id,
            session_id=parent.scope.session_id,
            turn_id=turn_id,
            generation=1,
        )
        try:
            self._fresh_writer()
        except TerminalRuntimeAdmissionError as exc:
            await self._trigger_recovery(
                child_scope,
                "followup_writer_not_ready",
                exc,
            )
            raise
        try:
            request = await resolver(template, parent.scope)
            if (
                not isinstance(request, TurnRequest)
                or request.session_id != child_scope.session_id
                or request.team_id != child_scope.team_id
                or request.turn_id != child_scope.turn_id
                or request.workspace_id != workspace_id
                or request.user_id != user_id
                or request.member_uid != member_uid
                or request.text != user_text
                or request.language_context is None
                or request.language_context.ui_language != presentation_locale
                or request.resume_turn_id != parent.scope.turn_id
                or request.terminal_runtime is not None
                or not request.terminal_auth_fence_vector_digest
                or request.access_context is None
                or request.team_router is None
                or request.team_kb_retriever is None
            ):
                raise RuntimeError("terminal_followup_current_context_malformed")
        except BaseException as exc:
            await self._trigger_recovery(
                child_scope,
                "followup_context_resolution_failed",
                exc,
            )
            raise
        child = await self._attach_opened_scope(
            child_scope,
            authz_snapshot_digest=(request.terminal_auth_fence_vector_digest),
            claim_auth_principal_ref=request.user_id,
            followup_execution_intent=intent,
            require_open_writer=True,
        )
        request = replace(request, terminal_runtime=child)
        try:
            self._inject(TerminalProtocolFaultPoint.FOLLOWUP_AFTER_CLAIM_BEFORE_TASK)
            await spawner(request)
        except BaseException as exc:
            try:
                await child.handoff_to_recovery(reason_code="worker_start_failed")
            except BaseException as handoff_error:
                exc.add_note(f"terminal follow-up handoff failed: {handoff_error!r}")
            await self._trigger_recovery(
                child_scope,
                "followup_worker_start_failed",
                exc,
            )
            raise

    async def _trigger_recovery(
        self,
        scope: TerminalScope,
        reason: str,
        prior_error: BaseException,
    ) -> None:
        if self._recovery_trigger is None:
            return
        try:
            result = self._recovery_trigger(scope, reason)
            if inspect.isawaitable(result):
                await result
        except BaseException as trigger_error:
            prior_error.add_note(f"terminal recovery trigger failed: {trigger_error!r}")

    async def _trigger_recovery_preserving_cancellation(
        self,
        scope: TerminalScope,
        reason: str,
        prior_error: BaseException,
    ) -> None:
        task = asyncio.create_task(self._trigger_recovery(scope, reason, prior_error))
        cancellation: asyncio.CancelledError | None = None
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError as exc:
                if task.cancelled():
                    raise
                if cancellation is None:
                    cancellation = exc
        if cancellation is not None:
            raise cancellation

    async def _runtime_handoff_completed(
        self,
        runtime: TerminalTurnRuntime,
        reason: str,
        error: BaseException | None,
    ) -> None:
        marker = error or RuntimeError("terminal_runtime_handoff_completed")
        await self._trigger_recovery(
            runtime.scope,
            (f"{reason}:handoff_failed" if error is not None else f"{reason}:handoff_completed"),
            marker,
        )

    async def trigger_recovery(
        self,
        scope: TerminalScope,
        *,
        reason: str,
        prior_error: BaseException,
    ) -> None:
        """Public supervised-task seam for synchronous spawn failures."""

        await self._trigger_recovery(scope, reason, prior_error)

    async def wake_admitted_web_turn(self, scope: TerminalScope) -> bool:
        """Wake recovery for a replayed admission whose exact runtime is absent."""

        if not isinstance(scope, TerminalScope):
            raise TypeError("terminal runtime scope is invalid")
        try:
            runtime = await self._runtimes.get_exact(scope)
        except asyncio.CancelledError as exc:
            await self._trigger_recovery_preserving_cancellation(
                scope,
                "web_action_start_replay_lookup_cancelled",
                exc,
            )
            raise
        except Exception as exc:
            await self._trigger_recovery_preserving_cancellation(
                scope,
                "web_action_start_replay_lookup_failed",
                exc,
            )
            return True
        if runtime is not None:
            return False
        await self._trigger_recovery_preserving_cancellation(
            scope,
            "web_action_start_replay_runtime_missing",
            RuntimeError("terminal action runtime is unavailable"),
        )
        return True

    async def _ensure_decision_auth_scopes(
        self,
        *,
        team_id: str,
        session_id: str,
        principals: DecisionPrincipalSet,
    ) -> None:
        scopes = {
            AuthFenceScope(AuthFenceScopeKind.TEAM_MEMBERSHIP, team_id, team_id),
            AuthFenceScope(AuthFenceScopeKind.RESOURCE_GRANT, team_id, team_id),
            AuthFenceScope(AuthFenceScopeKind.SESSION_VISIBILITY, team_id, session_id),
        }
        for principal in {
            principals.requester_ref,
            principals.actor_ref,
            principals.effect_principal_ref,
        }:
            if not principal.startswith("system:"):
                scopes.add(
                    AuthFenceScope(
                        AuthFenceScopeKind.USER_ACCOUNT,
                        "__global__",
                        principal,
                    )
                )
        await self._store.ensure_auth_scope_epochs(
            AuthFenceResolveContext(
                team_id=team_id,
                viewer=(principals.requester_ref if principals.actor_ref.startswith("system:") else principals.actor_ref),
                resource_ref=session_id,
                session_id=session_id,
                required_scopes=tuple(sorted(scopes, key=AuthFenceScope.sort_key)),
            )
        )


def terminal_auth_resolve_context(request: TurnRequest) -> AuthFenceResolveContext:
    return terminal_auth_resolve_context_values(
        team_id=request.team_id or "",
        session_id=request.session_id,
        user_id=request.user_id,
        resource_ref=request.turn_id,
    )


def terminal_auth_resolve_context_values(
    *,
    team_id: str,
    session_id: str,
    user_id: str,
    resource_ref: str,
) -> AuthFenceResolveContext:
    scopes = (
        AuthFenceScope(AuthFenceScopeKind.TEAM_MEMBERSHIP, team_id, team_id),
        AuthFenceScope(AuthFenceScopeKind.RESOURCE_GRANT, team_id, team_id),
        AuthFenceScope(
            AuthFenceScopeKind.SESSION_VISIBILITY,
            team_id,
            session_id,
        ),
        AuthFenceScope(
            AuthFenceScopeKind.USER_ACCOUNT,
            "__global__",
            user_id,
        ),
    )
    return AuthFenceResolveContext(
        team_id=team_id,
        viewer=user_id,
        resource_ref=resource_ref,
        session_id=session_id,
        required_scopes=scopes,
    )


__all__ = [
    "TerminalRuntimeAdmissionError",
    "TerminalRuntimeFactory",
    "TerminalRuntimeRegistry",
    "TerminalPreparedDecision",
    "TerminalWriterSnapshot",
    "terminal_auth_resolve_context",
    "terminal_auth_resolve_context_values",
]
