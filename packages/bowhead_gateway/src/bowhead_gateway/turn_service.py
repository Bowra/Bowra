"""Channel-neutral turn-start / steer choreography (P0).

The web route (``routes/chat.py``) and the Feishu bridge run a structurally-parallel
turn-start sequence. ``TurnService`` extracts
the channel-neutral choreography so every caller delegates instead of re-implementing.

Two pieces of the choreography are channel-neutral and extracted here:

* ``steer_or_proceed`` — the self-contained block at the top of ``chat.py:send`` that, when a turn
  is already in-flight for a session, buffers the new message + acks 🤝 or falls through to a
  fresh turn.
* ``spawn_turn`` — the **spawn tail** every channel runs identically once it has built a
  ``TurnRequest`` + resolved its effective orchestrator: require the admitted producer,
  claim or persist the durable dispatch state, then ``create_task`` the worker with the
  ``_inflight_tasks`` bookkeeping. ``_run_turn_and_cleanup`` stays in ``routes.chat``
  (single source); we call it lazily to avoid a cycle.

Faithfulness matters: each step preserves the original's wrapped-exception + best-effort
semantics and the race-safe ordering: steer appends use a compare-and-swap fence against the
observed active turn, and spawn publishes the explicit Stop fence before scheduling the worker.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

if TYPE_CHECKING:
    from bowhead_core.access_control import AccessControlStore
    from bowhead_core.reference_mcp_metadata import ReferenceMcpMetadataIndex
    from bowhead_core.skill_read_resolution import SkillReadSourceResolver

from bowhead_core.clarification_store import (
    RESUME_CONTEXT_SCHEMA_VERSION,
    resume_context_is_readable,
    resume_context_version,
)
from bowhead_core.events import (
    OrchestratorEvent,
    TerminalDurabilityAbort,
    get_event_registry,
)
from bowhead_core.types import (
    LEGACY_TURN_WORKER_SETTLED_STATUSES,
    normalize_legacy_turn_status,
)

_log = logging.getLogger("bowhead_gateway.turn_service")
_CHAT_DISPATCH_LEASE_SECONDS = 30.0
LEGACY_CLARIFICATION_SETTLEMENT_WAIT_SECONDS = 7.0
LEGACY_CLARIFICATION_SETTLEMENT_POLL_SECONDS = 0.05

SteerKind = Literal[
    "attachments_unsupported",
    "buffered",
    "completed",
    "lookup_failed",
    "proceed",
    "protocol_active",
]
TeamModelEntrypoint = Literal[
    "catalog",
    "chat",
    "clarification",
    "plan",
    "steer",
    "tool_continuation",
]
TeamModelResolutionResult = Literal[
    "team_safe_success",
    "team_unavailable",
    "unsafe_deployment_fallback",
    "resolver_error",
]
TeamModelResolutionReason = Literal[
    "resolved",
    "team_scope_missing",
    "runtime_dependency_unavailable",
    "config_missing",
    "config_invalid",
    "config_load_failed",
    "router_unavailable",
    "router_build_failed",
    "deployment_catalog_ignored",
]


@dataclass(frozen=True)
class SteerOutcome:
    """Result of the steer/proceed decision.

    * ``buffered``  — the message was appended to the active turn's steer buffer (🤝 acked);
      caller returns the active turn's stream.
    * ``completed`` — the UI targeted a turn that is no longer the active turn; caller should
      keep the draft instead of promoting the steer into a fresh task.
    * ``lookup_failed`` — protocol-aware active identity could not be read; caller must reject
      without starting a fresh worker.
    * ``attachments_unsupported`` — the active Turn's current steer path cannot preserve typed
      attachment identity; caller must reject before the legacy text-only steer buffer.
    * ``proceed``   — no active turn, or fall-through (empty text / buffer append lost the
      race); caller proceeds to start a fresh turn.
    """

    kind: SteerKind
    active_turn_id: str | None = None
    active_protocol_version: int | None = None


@dataclass(frozen=True)
class ReferenceSteerMaterialization:
    """One exact reference child prepared outside and committed inside the Store lock."""

    texts: tuple[str, ...]
    scope: Any
    skill_sources: Any | None
    mcp_index: Any | None = None
    mcp_access: Any | None = None
    mcp_prompt: bool = False


class ResumeTurnConflictError(RuntimeError):
    """The same-Turn clarification resume no longer owns a resumable row."""


class TurnDispatchAlreadyOwnedError(RuntimeError):
    """Another durable dispatch owner won; this caller must not terminalize it."""


def clarification_requires_legacy_resume_fence(clarification: Any) -> bool:
    """Whether a legacy decision continues the producing Turn in-place."""

    resume_context = clarification.resume_context if isinstance(clarification.resume_context, dict) else None
    return bool(
        clarification.kind in {"ambiguous_turn", "options", "tool_context_select"}
        and resume_context is not None
        and not resume_context.get("plan_reapprove_plan_id")
        and not resume_context.get("plan_midrun_plan_id")
    )


async def wait_for_legacy_turn_resume_ready(
    session_store: Any,
    turn_id: str,
    *,
    timeout_seconds: float | None = None,
    poll_seconds: float | None = None,
) -> bool:
    """Wait without consuming a decision until its producer fence is settled."""

    if session_store is None:
        return False
    timeout = LEGACY_CLARIFICATION_SETTLEMENT_WAIT_SECONDS if timeout_seconds is None else max(0.0, timeout_seconds)
    poll = LEGACY_CLARIFICATION_SETTLEMENT_POLL_SECONDS if poll_seconds is None else max(0.001, poll_seconds)
    ready_probe = getattr(session_store, "is_turn_resume_ready", None)
    if not callable(ready_probe):
        return False

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            ready = bool(await ready_probe(turn_id))
        except Exception as exc:
            _log.warning(
                "clarification.resume_fence_probe_failed turn=%s err=%s",
                turn_id,
                exc,
            )
            return False
        if ready:
            return True
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(poll, remaining))


class TurnDispatchCancelledError(TurnDispatchAlreadyOwnedError):
    """A durable Stop won after dispatch claim but before worker start."""


_LEGACY_SPAWN_HANDOFF_ATTEMPTED = "_bowhead_legacy_spawn_handoff_attempted"


def _mark_legacy_spawn_failure_handoff_attempted(error: BaseException) -> None:
    setattr(error, _LEGACY_SPAWN_HANDOFF_ATTEMPTED, True)


def legacy_spawn_failure_handoff_attempted(error: BaseException) -> bool:
    """Return whether the spawn boundary already owned recovery handoff.

    The web route remains the fallback owner for failures that happen before a
    dispatch claim. Once ``spawn_turn`` has claimed the row, this marker keeps
    the route from running the same protocol a second time while preserving the
    original exception type and traceback.
    """

    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if bool(getattr(current, _LEGACY_SPAWN_HANDOFF_ATTEMPTED, False)):
            return True
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return False


def _contains_terminal_durability_abort(error: BaseException | None) -> bool:
    """Recognize a fail-stop even when a worker boundary wrapped its cause."""

    pending = [error] if error is not None else []
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, TerminalDurabilityAbort):
            return True
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return False


@dataclass(frozen=True)
class ResumeTurnSpawn:
    """One shared resume worker plus its durable execution generation."""

    task: asyncio.Task[None] | None
    execution_generation: int
    attached_remote: bool = False


@dataclass(frozen=True)
class TeamModelRuntimeResolution:
    """Finite, non-secret result of resolving one authenticated team's runtime."""

    router: Any = None
    kb_retriever: Any = None
    result: TeamModelResolutionResult = "team_unavailable"
    reason: TeamModelResolutionReason = "runtime_dependency_unavailable"

    @property
    def available(self) -> bool:
        return self.result == "team_safe_success" and self.router is not None


def record_team_model_resolution(
    *,
    entrypoint: TeamModelEntrypoint,
    result: TeamModelResolutionResult,
    reason: TeamModelResolutionReason,
) -> None:
    """Record only finite-cardinality, content-free team model outcomes."""
    try:
        from bowhead_core import metrics

        labels = {
            "entrypoint": entrypoint,
            "result": result,
            "reason": reason,
        }
        metrics.inc(
            "bowhead_team_model_resolution_total",
            labels=labels,
            help="Finite team model resolution outcomes by authenticated entrypoint.",
        )
        if result == "team_safe_success":
            metrics.inc(
                "bowhead_team_model_resolution_success_total",
                labels={"entrypoint": entrypoint},
                help="Successful team-safe model resolutions by authenticated entrypoint.",
            )
    except Exception:  # telemetry must never change the resolution decision  # noqa: S110
        pass


@dataclass(frozen=True)
class ClarificationResumeSemantics:
    """Canonical user-visible inputs for live and recovery resume."""

    user_text: str
    resume_glossary: dict[str, str]
    skipped: bool


def build_clarification_resume_semantics(
    clarification: Any,
) -> ClarificationResumeSemantics:
    """Build the exact semantic continuation shared by every Web execution.

    A durable decision may be consumed by the request that recorded it or by
    restart recovery after an ack-loss/process exit.  The two consumers must
    never reinterpret skip tokens, multi-question answers, or a workspace
    picker differently.
    """

    from bowhead_gateway.routes.clarifications import (
        ClarificationAnswerDTO,
        _answer_to_resume_text,
        _answers_from_context,
        _is_skip_value,
        _merge_clarified_user_text,
        _render_legacy_values_for_resume,
        _skip_resume_answer,
    )

    context = getattr(clarification, "resume_context", None)
    context = context if isinstance(context, dict) else {}
    user_text = str(context.get("user_text") or "")
    token = str(getattr(clarification, "token", "") or "")
    kind = str(getattr(clarification, "kind", "") or "")
    chosen_value = getattr(clarification, "chosen_value", None)
    skipped = _is_skip_value(chosen_value)
    glossary: dict[str, str] = {}
    semantic_answers = [
        ClarificationAnswerDTO.model_validate(answer) for answer in (getattr(clarification, "answers", None) or [])
    ] or _answers_from_context(context)

    if "question_round" in context:
        # The shared spawn reader restores canonical text and every persisted
        # answer. Never construct evidence from an accumulated display string.
        pass
    elif skipped and kind == "tool_context_select":
        catalog_items: list[str] = []
        for candidate in getattr(clarification, "candidates", ()) or ():
            label = str(getattr(candidate, "label", "") or "")
            if not label:
                continue
            description = str(getattr(candidate, "desc", "") or "")
            catalog_items.append(f"{label}（{description}）" if description else label)
        catalog = "、".join(catalog_items[:40])
        route_instruction = (
            "（我没有指定要用哪个北极九章工作区。请从下面可访问的工作区里，挑一个跟我的"
            "问题最相关的，先用 tool_switch_context 切换过去、再查数据；如果没有明显相关的，"
            f"就直接告诉我、让我自己选，不要瞎猜。可选工作区：{catalog}）"
        )
        user_text = f"{user_text}\n{route_instruction}" if user_text else route_instruction
    elif skipped:
        skip_answer = _skip_resume_answer(clarification)
        user_text = f"{user_text} ({skip_answer})" if user_text else skip_answer
    elif kind == "tool_context_select":
        # The selected workspace is execution metadata, never NL2SQL text.
        pass
    elif token and isinstance(chosen_value, str) and chosen_value:
        glossary[token] = chosen_value
    elif not token and semantic_answers:
        user_text = _merge_clarified_user_text(
            user_text,
            [_answer_to_resume_text(answer) for answer in semantic_answers],
        )
    elif not token and (getattr(clarification, "chosen_values", None) or chosen_value):
        chosen_values = getattr(clarification, "chosen_values", None)
        answers = list(chosen_values) if chosen_values else [str(chosen_value)]
        user_text = _merge_clarified_user_text(
            user_text,
            _render_legacy_values_for_resume(answers),
        )

    return ClarificationResumeSemantics(
        user_text=user_text,
        resume_glossary=glossary,
        skipped=skipped,
    )


async def is_session_deleted(orchestrator: Any, session_id: str) -> bool:
    """feedback-0713 F2: 软删会话是否该拒收新 turn。

    R1 软删只收敛列表面——store 层 get/ensure 刻意**穿透**软删（resume/IM 线程/
    定时任务深链/审计靠它，语义锁 test_canvas_delete_session_r1）。但穿透面曾让
    软删会话仍可继续发 turn 烧 credit（列表看不见=「隐形计费」）。边界拍板
    （2026-07-14）=**410 拒绝、不复活**：删除是用户明确决定，静默复活会让 R1
    「联动删除」变得不可靠。所有 turn 准入点统一调这里（web send / clarification
    resume / feishu / teams / scheduler / steer-drain follow-up）；store 层语义不动。

    fail-open：准入检查自身出错不拦 turn（可用性优先；窗口可忽略）。
    """
    if not session_id:
        return False
    store = getattr(orchestrator, "session_store", None)
    if store is None:
        return False
    try:
        sess = await store.get_session(session_id)
    except Exception:  # noqa: BLE001
        return False
    return bool(sess is not None and getattr(sess, "deleted_at", None))


async def steer_or_proceed(
    orchestrator: Any,
    *,
    session_id: str,
    text: str,
    user_msg_id: str,
    expected_turn_id: str | None = None,
    has_attachments: bool = False,
) -> SteerOutcome:
    """Decide steer/proceed for a possibly-in-flight session.

    Mirrors ``chat.py:send`` Phase 1.1 steer-mode intercept. ``user_msg_id`` is the id the
    🤝 ``emoji_ack`` targets (caller supplies a stable id or a synthesized fallback).
    Attachments fail closed only when an active Turn would otherwise consume the
    text-only legacy steer buffer; an idle Session still proceeds to a fresh Turn.
    """
    if not session_id:  # only existing sessions can have active turns
        return SteerOutcome("proceed")
    identity_reader = getattr(
        orchestrator.session_store,
        "get_active_turn_identity",
        None,
    )
    if callable(identity_reader):
        try:
            active_turn_id, active_protocol_version = await identity_reader(session_id)
        except Exception as exc:
            # Production SessionStore is protocol-aware.  A read/decode error
            # cannot be treated as "no active turn": doing so lets a legacy
            # worker start beside an active v1 generation.
            _log.warning(
                "steer.get_active_turn_identity_failed sid=%s err=%s",
                session_id,
                exc,
            )
            return SteerOutcome("lookup_failed")
    else:
        try:
            # Narrow compatibility for unit fakes/legacy embedders. Production
            # SessionStore always supplies the protocol-aware identity reader.
            active_turn_id = await orchestrator.session_store.get_active_turn(session_id)
            active_protocol_version = None
        except Exception as exc:
            _log.warning(
                "steer.legacy_get_active_turn_failed sid=%s err=%s",
                session_id,
                exc,
            )
            active_turn_id = None
            active_protocol_version = None
    if not active_turn_id:
        if expected_turn_id:
            return SteerOutcome("completed", expected_turn_id)
        return SteerOutcome("proceed")
    if has_attachments:
        return SteerOutcome("attachments_unsupported", active_turn_id)
    if active_protocol_version is not None:
        # S4 does not support durable steer/control decisions yet. Never let a
        # v1/future Turn touch the legacy buffer, finalize_turn or cancel path.
        return SteerOutcome(
            "protocol_active",
            active_turn_id,
            active_protocol_version,
        )
    if expected_turn_id and active_turn_id != expected_turn_id:
        return SteerOutcome("completed", active_turn_id)

    # Buffer steer message + emit 🤝 on the active turn's bus. Always CAS against the
    # identity observed above, even when the UI did not send expected_turn_id. Otherwise a
    # legacy turn can finish and a v1 turn can become active between the read and append, causing
    # this legacy-only buffer path to poison the new protocol turn.
    appended = False
    if text:  # empty msg = no steer to add (attachment-only fall-through)
        try:
            appended = await orchestrator.session_store.append_steer_buffer(
                session_id=session_id,
                text=text,
                expected_turn_id=active_turn_id,
            )
        except Exception as exc:
            _log.warning("steer.append_buffer_failed sid=%s err=%s", session_id, exc)
    if appended:
        try:
            await get_event_registry().emit_if_local_producer(
                active_turn_id,
                OrchestratorEvent(
                    event_type="emoji_ack",
                    turn_id=active_turn_id,
                    payload={"target_msg_id": user_msg_id, "emoji": "🤝", "text": text},
                ),
            )
        except Exception as exc:
            _log.warning("steer.emoji_ack_emit_failed turn=%s err=%s", active_turn_id, exc)
        return SteerOutcome("buffered", active_turn_id)

    if expected_turn_id:
        return SteerOutcome("completed", expected_turn_id)

    # Fall through to fresh-turn flow: empty text, or append lost the race.
    _log.info(
        "steer.fall_through_fresh_turn sid=%s text_len=%d active_was=%s",
        session_id,
        len(text),
        active_turn_id,
    )
    return SteerOutcome("proceed", active_turn_id)


async def materialize_reference_steer_head(
    orchestrator: Any,
    *,
    session_id: str,
    current_turn_id: str,
    next_turn_id: str,
    team_id: str,
    user_id: str,
    workspace_id: str,
) -> ReferenceSteerMaterialization | None:
    """Materialize an accepted Agent/Skill/Capability/MCP reference at the queue head.

    Source decryption and exact resource preparation happen before the writer
    lock. ``SessionStore.materialize_reference_steer_follow_up`` then repeats
    the authority checks and commits source, plan, execution, dispatch, queue
    settlement and suffix handoff atomically.
    """
    if os.environ.get("FEATURE_CHAT_REFERENCE_PRIMARY_RUNTIME", "1") != "1":
        return None
    import inspect
    from dataclasses import replace
    from pathlib import Path

    from bowhead_core._db import open_shared_ctx
    from bowhead_core.chat_reference_preparation import (
        prepare_reference_system_start_admission,
        prepare_reference_team_start_admission,
    )
    from bowhead_core.chat_reference_scope import (
        ReferenceRequestScope,
        require_reference_request_scope,
    )
    from bowhead_core.chat_reference_storage import (
        ChatReferenceStore,
        ReferenceCipher,
        ReferenceScope,
    )
    from bowhead_core.chat_references import (
        AgentResourceRef,
        CapabilityResourceRef,
        McpPromptResourceRef,
        McpSourceResourceRef,
        McpToolResourceRef,
        ReferenceNode,
        SkillResourceRef,
    )
    from bowhead_core.skill_read_resolution import SkillReadSourceResolver

    store = getattr(orchestrator, "session_store", None)
    source_factory = getattr(orchestrator, "reference_skill_sources_factory", None)
    db_path = getattr(store, "_db_path", None)
    if store is None or db_path is None or not callable(source_factory):
        raise ValueError("reference_submission_materializer_unavailable")
    request_scope = ReferenceRequestScope(
        team_id=team_id,
        user_id=user_id,
        workspace_id=workspace_id,
        session_id=session_id,
    )
    async with open_shared_ctx(db_path) as db:
        await db.execute("BEGIN")
        try:
            policy_epoch = await require_reference_request_scope(
                db,
                request_scope,
            )
        finally:
            await db.execute("ROLLBACK")
    reference_scope = ReferenceScope(
        **request_scope.model_dump(),
        policy_epoch=policy_epoch,
    )
    reference_store = ChatReferenceStore(
        ReferenceCipher(os.environ.get("BOWHEAD_FERNET_KEY", "")),
    )
    head = await store.read_reference_steer_head(
        session_id=session_id,
        current_turn_id=current_turn_id,
        reference_store=reference_store,
        reference_scope=reference_scope,
    )
    if head is None:
        return None
    submission_id, payload, queue_version = head
    if payload.attachment_ids or payload.attachment_refs:
        from bowhead_gateway.chat_attachment_access import (
            ChatAttachmentAccessError,
            validate_chat_attachment_access,
        )

        try:
            await validate_chat_attachment_access(
                artifact_store=getattr(orchestrator, "artifact_store", None),
                refs=payload.attachment_refs,
                runtime_ids=payload.attachment_ids,
                user_id=user_id,
                workspace_id=workspace_id,
                session_id=session_id,
            )
        except ChatAttachmentAccessError as exc:
            raise ValueError("reference_attachment_unavailable") from exc
    nodes = [node for node in payload.input_document.nodes if isinstance(node, ReferenceNode)]
    agents = [node.resource for node in nodes if isinstance(node.resource, AgentResourceRef)]
    skill_or_capability_nodes = [node for node in nodes if isinstance(node.resource, SkillResourceRef | CapabilityResourceRef)]
    mcp_nodes = [node for node in nodes if isinstance(node.resource, McpSourceResourceRef | McpToolResourceRef | McpPromptResourceRef)]
    receiver = payload.automatic_receiver
    if not ((len(agents) == 1 and receiver is None) or (not agents and receiver is not None)) or any(
        not isinstance(
            node.resource,
            AgentResourceRef | SkillResourceRef | CapabilityResourceRef | McpSourceResourceRef | McpToolResourceRef | McpPromptResourceRef,
        )
        for node in nodes
    ):
        raise ValueError("reference_steer_adapter_unavailable")
    if mcp_nodes and (receiver is not None or len(agents) != 1 or skill_or_capability_nodes):
        raise ValueError("reference_steer_adapter_unavailable")
    materialized_receiver = agents[0] if agents else receiver
    assert materialized_receiver is not None
    lead_peer_profile = None
    if receiver is not None:
        from bowhead_gateway.chat_reference_start import (
            current_reference_lead_peers_for_runtime,
        )

        lead_peers = await current_reference_lead_peers_for_runtime(
            orchestrator,
            access_store=getattr(orchestrator, "access_control_store", None),
            user_store=getattr(orchestrator, "user_store", None),
            override_store=getattr(
                orchestrator,
                "builtin_subagent_override_store",
                None,
            ),
            team_id=team_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        exact_roster_matches = [peer for peer in lead_peers if peer.source == receiver.source_kind and peer.agent_id == receiver.id]
        if len(exact_roster_matches) != 1:
            raise ValueError("reference_receiver_unavailable")
        lead_peer_profile = exact_roster_matches[0].name
    sources = source_factory(request_scope)
    if inspect.isawaitable(sources):
        sources = await sources
    if type(sources) is not SkillReadSourceResolver or Path(sources.db_path).resolve() != Path(db_path).resolve():
        raise ValueError("reference_skill_source_scope_changed")
    kwargs = {
        "input_document": payload.input_document,
        "scope": request_scope,
        "sources": sources,
        "automatic_receiver": receiver,
        "has_attachments": bool(payload.attachment_ids),
    }
    mcp_index = None
    mcp_access = None
    if mcp_nodes:
        from bowhead_core.access_control import AccessControlStore
        from bowhead_core.reference_mcp_metadata import ReferenceMcpMetadataIndex

        cache = getattr(orchestrator, "reference_mcp_metadata", None)
        mcp_index = cache.current() if cache is not None else None
        mcp_access = getattr(orchestrator, "access_control_store", None)
        if (
            type(mcp_index) is not ReferenceMcpMetadataIndex
            or not mcp_index.ready
            or type(mcp_access) is not AccessControlStore
            or Path(mcp_index.db_path).resolve() != Path(db_path).resolve()
            or Path(mcp_access.db_path).resolve() != Path(db_path).resolve()
        ):
            raise ValueError("reference_mcp_source_scope_changed")
        kwargs.update(mcp_index=mcp_index, mcp_access=mcp_access)
    prepared = (
        await prepare_reference_system_start_admission(
            db_path,
            runner=orchestrator.subagent_runner,
            **kwargs,
        )
        if materialized_receiver.source_kind == "system"
        else await prepare_reference_team_start_admission(db_path, **kwargs)
    )
    if lead_peer_profile is not None and lead_peer_profile != prepared.profile_name:
        raise ValueError("reference_receiver_unavailable")
    # The writer support decision was made before this accepted input entered
    # the queue. A later stop-write rollout must still consume it; current
    # resource authority is rechecked by the prepared admission below.
    prepared = replace(prepared, require_released_entry=False, orchestration_contract=payload.orchestration_contract)
    texts = await store.materialize_reference_steer_follow_up(
        session_id=session_id,
        current_turn_id=current_turn_id,
        next_turn_id=next_turn_id,
        submission_id=submission_id,
        expected_queue_version=queue_version,
        reference_store=reference_store,
        reference_scope=reference_scope,
        reference_start=prepared,
    )
    return ReferenceSteerMaterialization(
        texts=tuple(texts),
        scope=reference_scope,
        skill_sources=prepared.skill_sources,
        mcp_index=mcp_index,
        mcp_access=mcp_access,
        mcp_prompt=prepared.mcp_prompt_selection is not None,
    )


async def admit_legacy_chat_execution(
    orchestrator: Any,
    turn_req: Any,
    *,
    initial_seq: int = 0,
) -> Any:
    """Create the one legacy producer generation before a worker can run."""

    if getattr(turn_req, "terminal_runtime", None) is not None:
        raise RuntimeError("terminal_runtime_cannot_use_legacy_chat_adapter")
    registry = getattr(orchestrator, "event_registry", None)
    if registry is None:
        raise RuntimeError("legacy_chat_event_registry_unavailable")
    return await registry.get_or_create(
        turn_req.turn_id,
        initial_seq=initial_seq,
        exclusive_producer=True,
        persist_before_live=True,
    )


async def _retire_admitted_legacy_bus(
    registry: Any,
    *,
    turn_id: str,
    producer_bus: Any,
) -> None:
    """Retire one exact legacy producer even if its caller is cancelled."""
    if registry is None or producer_bus is None:
        return
    try:
        # EventBusRegistry.close owns the single cancellation-safe retire +
        # subscriber-wakeup primitive.  This boundary only suppresses the
        # cancellation it re-propagates so an already-selected Store recovery
        # or route error remains the caller-visible outcome.
        await registry.close(turn_id, expected=producer_bus)
    except asyncio.CancelledError:
        return
    except BaseException:
        _log.exception("turn.legacy_bus_retire_failed turn=%s", turn_id)


async def _await_task_through_repeated_cancellation(
    task: asyncio.Task[Any],
) -> Any:
    """Wait for a critical child without forwarding repeated caller cancels."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def _run_cleanup_through_repeated_cancellation(
    operation: Callable[[], Awaitable[Any]],
    *,
    task_name: str,
) -> Any:
    """Run a cleanup child to completion despite caller cancellation."""
    cleanup_coro = operation()
    try:
        cleanup_task = asyncio.create_task(
            cleanup_coro,
            name=task_name,
        )
    except BaseException:
        close_coro = getattr(cleanup_coro, "close", None)
        if callable(close_coro):
            close_coro()
        raise
    return await _await_task_through_repeated_cancellation(cleanup_task)


LegacySpawnFailureHandoff = Literal[
    "foreign_active",
    "recovered",
    "recovery_pending",
    "untracked",
]


async def handoff_legacy_spawn_failure_to_recovery(
    session_store: Any,
    *,
    turn_id: str,
    session_id: str,
    owner_id: str,
    error: str,
    error_kind: str,
) -> LegacySpawnFailureHandoff:
    """Fence one pre-worker failure and hand its terminal projection to Store recovery.

    The order is intentional: an exact recovery owner first excludes a foreign
    worker; the Turn failure then closes fresh steer admission; only afterwards
    does ``drain_pending`` expose the row to maintenance. Recovery owns the
    parent terminal pair, accepted-steer child and final dispatch fence in one
    writer transaction.
    """
    if not owner_id:
        raise RuntimeError("legacy_chat_dispatch_owner_unavailable")
    prepared = await session_store.prepare_chat_spawn_failure(
        turn_id=turn_id,
        session_id=session_id,
        expected_owner_id=owner_id,
    )
    if prepared == "foreign_active":
        return "foreign_active"
    if prepared == "untracked":
        return "untracked"
    if prepared == "ready":
        failure_marked = bool(
            await session_store.mark_failed(
                turn_id=turn_id,
                error=error,
                error_kind=error_kind,
            )
        )
        if not failure_marked:
            durable_turn = await session_store.get_turn(turn_id)
            if (
                durable_turn is None
                or str(getattr(durable_turn, "status", "")) != "failed"
                or str(getattr(durable_turn, "error_kind", "")) != error_kind
            ):
                return "untracked"
        handed_off = await session_store.mark_chat_spawn_dispatch_drain_pending(
            turn_id=turn_id,
            owner_id=owner_id,
            session_id=session_id,
        )
        if not handed_off:
            return "recovery_pending"

    recovered = await session_store.recover_expired_chat_spawn_dispatches()
    if turn_id in recovered:
        return "recovered"
    projection = await session_store.get_chat_spawn_dispatch_projection_state(
        turn_id=turn_id,
    )
    return "recovered" if projection == "settled" else "recovery_pending"


async def spawn_turn(
    orchestrator: Any,
    turn_req: Any,
    *,
    user_store: Any = None,
    team_id: str | None = None,
    metrics_log: Any = None,
    entitlements: Any = None,
    name_prefix: str = "turn_run",
    dispatch_owner_id: str | None = None,
    admitted_legacy_bus: Any | None = None,
    before_schedule: Callable[[], Awaitable[None]] | None = None,
    preworker_failure_recovery: Callable[[], Awaitable[None]] | None = None,
    cleanup_grace_s: float = 5.0,
    reference_skill_sources: SkillReadSourceResolver | None = None,
    reference_mcp_index: ReferenceMcpMetadataIndex | None = None,
    reference_mcp_access: AccessControlStore | None = None,
    reference_mcp_prompt: bool = False,
) -> asyncio.Task[Any]:
    """Claim a persisted legacy Turn and spawn its worker.

    The channel-neutral tail shared by web and Feishu once a ``TurnRequest`` is built and
    the effective orchestrator (BYO router/KB) is resolved. Steps, order-preserved from the live
    ``chat.py:send`` / ``feishu/bridge.py``:

    1. Claim the durable dispatch. Legacy direct-run entrypoints without a
       persisted Turn first create that Turn, then claim the materialized
       dispatch in a second Store transaction. Every claimed legacy owner gets
       one final exact heartbeat before scheduling; this is especially required
       after a pre-worker callback yields. Only the renewed, non-cancelled claim
       winner may schedule a worker.
    2. ``create_task(_run_turn_and_cleanup(...))`` named ``f"{name_prefix}:{turn_id}"`` so the
       worker is pending in the loop before the caller returns its response.
    3. Register the task in ``_inflight_tasks`` + drop it on done (GC anchor so the task isn't
       collected mid-flight).

    ``session_id`` is read from ``turn_req.session_id`` (every caller builds the request with it).
    ``metrics_log`` defaults to ``None`` — matches the Feishu call site which omits it.
    ``_run_turn_and_cleanup`` / ``_inflight_tasks`` are lazy-imported from ``routes.chat`` to keep
    the worker single-source and avoid an import cycle. Returns the spawned task.
    """
    from bowhead_gateway.routes.chat import (
        _inflight_tasks,
        _run_turn_and_cleanup,
    )

    # Publish the cancel fence before scheduling the coroutine. A user can click
    # Stop immediately after the accepted response; without this pre-registration
    # that request can arrive before TurnRunner's first instruction and be lost.
    terminal_runtime = getattr(turn_req, "terminal_runtime", None)
    cancel_registry = getattr(orchestrator, "_cancel_events", None) if terminal_runtime is None else None
    cancel_event: asyncio.Event | None = None
    cancel_event_created = False
    if isinstance(cancel_registry, dict):
        cancel_event_created = turn_req.turn_id not in cancel_registry
        cancel_event = cancel_registry.setdefault(turn_req.turn_id, asyncio.Event())

    def _drop_created_cancel_fence() -> None:
        if cancel_event_created and isinstance(cancel_registry, dict) and cancel_registry.get(turn_req.turn_id) is cancel_event:
            cancel_registry.pop(turn_req.turn_id, None)

    def _drop_exact_cancel_fence() -> None:
        if isinstance(cancel_registry, dict) and cancel_registry.get(turn_req.turn_id) is cancel_event:
            cancel_registry.pop(turn_req.turn_id, None)

    legacy_registry = None
    producer_bus = admitted_legacy_bus

    async def _retire_legacy_bus_on_failure() -> None:
        if legacy_registry is None or producer_bus is None:
            return
        await _retire_admitted_legacy_bus(
            legacy_registry,
            turn_id=turn_req.turn_id,
            producer_bus=producer_bus,
        )

    async def _cleanup_preworker_failure() -> None:
        if cancel_event_created:
            _drop_created_cancel_fence()
        await _retire_legacy_bus_on_failure()

    if terminal_runtime is None:
        legacy_registry = getattr(orchestrator, "event_registry", None)
        if legacy_registry is None or not hasattr(
            legacy_registry,
            "projection_checkpoint",
        ):
            await _cleanup_preworker_failure()
            raise RuntimeError("legacy_chat_event_registry_unavailable")
        if producer_bus is None:
            await _cleanup_preworker_failure()
            raise RuntimeError("legacy_chat_execution_not_admitted")
        try:
            current_bus, _stable_floor = await legacy_registry.projection_checkpoint(
                turn_req.turn_id,
            )
        except BaseException:
            await _cleanup_preworker_failure()
            raise
        if current_bus is not producer_bus:
            await _cleanup_preworker_failure()
            raise RuntimeError("legacy_chat_execution_generation_changed")
        if not bool(getattr(producer_bus, "_persist_before_live", False)):
            await _cleanup_preworker_failure()
            raise RuntimeError("legacy_chat_execution_not_durable")

    if reference_skill_sources is not None and (
        terminal_runtime is not None or getattr(turn_req, "reference_scope", None) is None or not getattr(turn_req, "session_id", None)
    ):
        await _cleanup_preworker_failure()
        raise ValueError("reference_skill_source_scope_changed")
    if reference_mcp_prompt and (reference_mcp_index is None or reference_skill_sources is None):
        await _cleanup_preworker_failure()
        raise ValueError("reference_mcp_prompt_source_changed")
    if (reference_mcp_index is None) != (reference_mcp_access is None) or (
        reference_mcp_index is not None
        and (terminal_runtime is not None or getattr(turn_req, "reference_scope", None) is None or not getattr(turn_req, "session_id", None))
    ):
        await _cleanup_preworker_failure()
        raise ValueError("reference_mcp_source_scope_changed")

    owner_id = dispatch_owner_id or getattr(orchestrator, "dispatch_owner_id", None)
    session_store = getattr(orchestrator, "session_store", None)
    dispatch_claim = "untracked"

    async def _run_critical_preworker_handoff(
        *,
        failure: BaseException,
        recovery: Callable[[], Awaitable[None]],
        task_name: str,
    ) -> None:
        """Start one protected Store handoff before marking route ownership."""

        async def marked_recovery() -> None:
            _mark_legacy_spawn_failure_handoff_attempted(failure)
            await recovery()

        await _run_cleanup_through_repeated_cancellation(
            marked_recovery,
            task_name=task_name,
        )

    async def _handoff_claimed_preworker_failure(
        *,
        failure: BaseException,
        error: str = "turn dispatch failed before worker start",
        error_kind: str = "dispatch_start_failed",
    ) -> None:
        if terminal_runtime is not None or dispatch_claim != "claimed":
            return
        assert session_store is not None
        assert owner_id is not None
        if getattr(turn_req, "reference_runtime", None) is not None:

            async def recovery() -> None:
                # No worker exists yet; surrender only this exact owner. Never
                # renew an expired reference lease through a legacy failure path.
                if await session_store.mark_chat_spawn_dispatch_drain_pending(
                    turn_id=turn_req.turn_id,
                    session_id=turn_req.session_id,
                    owner_id=owner_id,
                ):
                    await session_store.recover_expired_chat_spawn_dispatches()

        elif preworker_failure_recovery is None:

            async def recovery() -> None:
                await handoff_legacy_spawn_failure_to_recovery(
                    session_store,
                    turn_id=turn_req.turn_id,
                    session_id=turn_req.session_id,
                    owner_id=owner_id,
                    error=error,
                    error_kind=error_kind,
                )

        else:
            recovery = preworker_failure_recovery
        await _run_critical_preworker_handoff(
            failure=failure,
            recovery=recovery,
            task_name=(f"turn_preworker_recovery_handoff:{turn_req.turn_id}"),
        )

    async def _handoff_claimed_preworker_cancel(
        failure: BaseException,
    ) -> None:
        """Move an exact cancelled owner directly into Store recovery."""
        if terminal_runtime is not None or dispatch_claim != "claimed":
            return
        assert session_store is not None
        assert owner_id is not None

        async def recovery() -> None:
            handed_off = await session_store.mark_chat_spawn_dispatch_drain_pending(
                turn_id=turn_req.turn_id,
                owner_id=owner_id,
                session_id=turn_req.session_id,
                allow_terminal_session_idle=True,
            )
            if not handed_off:
                raise RuntimeError(
                    "cancelled dispatch recovery handoff was rejected",
                )
            await session_store.recover_expired_chat_spawn_dispatches()

        await _run_critical_preworker_handoff(
            failure=failure,
            recovery=recovery,
            task_name=f"turn_preworker_cancel_handoff:{turn_req.turn_id}",
        )

    async def _handoff_then_cleanup_preworker_failure(
        *,
        failure: BaseException,
        error: str = "turn dispatch failed before worker start",
        error_kind: str = "dispatch_start_failed",
    ) -> None:
        """Publish Store recovery ownership before retiring the exact bus."""
        try:
            await _handoff_claimed_preworker_failure(
                failure=failure,
                error=error,
                error_kind=error_kind,
            )
        except BaseException as handoff_error:
            failure.add_note(
                f"legacy recovery handoff also failed: {handoff_error!r}",
            )
            _log.exception(
                "turn.preworker_recovery_handoff_failed turn=%s",
                turn_req.turn_id,
            )
        finally:
            await _cleanup_preworker_failure()

    async def _handoff_unconfirmed_turn_then_cleanup(
        *,
        failure: BaseException,
        error: str,
    ) -> None:
        """Resolve a Store commit whose caller-side outcome is unknown."""
        if terminal_runtime is not None:
            await _cleanup_preworker_failure()
            return
        assert session_store is not None
        assert owner_id is not None

        async def recovery() -> None:
            await handoff_legacy_spawn_failure_to_recovery(
                session_store,
                turn_id=turn_req.turn_id,
                session_id=turn_req.session_id,
                owner_id=owner_id,
                error=error,
                error_kind="dispatch_start_failed",
            )

        try:
            await _run_critical_preworker_handoff(
                failure=failure,
                recovery=recovery,
                task_name=f"turn_unconfirmed_store_handoff:{turn_req.turn_id}",
            )
        except BaseException as handoff_error:
            failure.add_note(
                f"unconfirmed Store handoff also failed: {handoff_error!r}",
            )
            _log.exception(
                "turn.unconfirmed_store_handoff_failed turn=%s",
                turn_req.turn_id,
            )
        finally:
            await _cleanup_preworker_failure()

    async def _raise_if_cancelled_before_worker(
        *,
        durable_cancel_requested: bool = False,
    ) -> None:
        if durable_cancel_requested and cancel_event is not None:
            cancel_event.set()
        if not durable_cancel_requested and (cancel_event is None or not cancel_event.is_set()):
            return
        cancelled_error = TurnDispatchCancelledError(
            "turn_cancelled_before_dispatch",
        )
        try:
            await _handoff_claimed_preworker_cancel(cancelled_error)
        except BaseException as recovery_error:
            cancelled_error.add_note(
                f"cancelled dispatch recovery also failed: {recovery_error!r}",
            )
            _log.exception(
                "turn.cancelled_before_dispatch_recovery_failed turn=%s",
                turn_req.turn_id,
            )
        finally:
            await _cleanup_preworker_failure()
        raise cancelled_error

    if terminal_runtime is None:
        if (
            not owner_id
            or session_store is None
            or not hasattr(
                session_store,
                "claim_chat_spawn_dispatch_for_worker",
            )
        ):
            await _cleanup_preworker_failure()
            raise RuntimeError("legacy_chat_dispatch_store_unavailable")
        try:
            reference_scope = getattr(turn_req, "reference_scope", None)
            if reference_scope is not None:
                from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
                from bowhead_core.chat_reference_execution import claim_reference_primary_for_worker
                from bowhead_core.chat_reference_runtime import ReferencePrimaryRuntime
                from bowhead_core.chat_reference_storage import ReferenceCipher

                if os.environ.get("FEATURE_CHAT_REFERENCE_PRIMARY_RUNTIME", "1") != "1":
                    raise ValueError("reference_execution_adapter_unavailable")
                bindings = AgentExecutionBindingStore(session_store._db_path)
                cipher = ReferenceCipher(os.environ.get("BOWHEAD_FERNET_KEY", ""))
                generation = getattr(turn_req, "execution_generation", 1)
                if type(generation) is not int or generation < 1:
                    raise ValueError("reference_execution_fence_lost")
                prompt_fetch = None
                if reference_mcp_prompt:
                    from bowhead_core.mcp_prompt_expansion import fetch_mcp_prompt_expansion

                    from bowhead_gateway.chat_reference_start import resolve_reference_prompt_endpoint

                    async def prompt_fetch(resource, parameters, source_revision):
                        endpoint = await resolve_reference_prompt_endpoint(
                            db_path=bindings.db_path,
                            team_id=reference_scope.team_id,
                            resource=resource,
                            source_revision=source_revision,
                        )
                        # Registry lookup yielded: recheck the worker and all
                        # source/receiver permissions at the remote boundary.
                        await runtime.validate()
                        return await fetch_mcp_prompt_expansion(endpoint, resource=resource, parameters=parameters)

                runtime = ReferencePrimaryRuntime(
                    scope=reference_scope,
                    turn_id=turn_req.turn_id,
                    owner_id=owner_id,
                    bindings=bindings,
                    cipher=cipher,
                    generation=generation,
                    skill_sources=reference_skill_sources if reference_mcp_index is None or reference_mcp_prompt else None,
                    mcp_prompt_fetch=prompt_fetch,
                    mcp_index=reference_mcp_index,
                    mcp_access=reference_mcp_access,
                    allow_bi_workspace_continuation=(reference_mcp_index is None or reference_mcp_prompt)
                    and callable(getattr(orchestrator, "reference_skill_sources_factory", None)),
                )
                if generation > 1:
                    await runtime.claim_bi_resume(clarification_id=turn_req.reference_clarification_id)
                    dispatch_claim = "claimed"
                elif reference_mcp_index is not None:
                    from pathlib import Path

                    from bowhead_core.access_control import AccessControlStore
                    from bowhead_core.chat_reference_scope import ReferenceRequestScope
                    from bowhead_core.reference_mcp_execution import claim_reference_mcp_primary_for_worker
                    from bowhead_core.reference_mcp_metadata import ReferenceMcpMetadataIndex

                    if (
                        type(reference_mcp_index) is not ReferenceMcpMetadataIndex
                        or type(reference_mcp_access) is not AccessControlStore
                        or Path(reference_mcp_index.db_path).resolve() != Path(bindings.db_path).resolve()
                        or Path(reference_mcp_access.db_path).resolve() != Path(bindings.db_path).resolve()
                    ):
                        raise ValueError("reference_mcp_source_scope_changed")
                    claim_mcp = claim_reference_mcp_primary_for_worker
                    prompt_kwargs = {}
                    if reference_mcp_prompt:
                        from bowhead_core.reference_mcp_prompt_execution import claim_reference_mcp_prompt_primary_for_worker

                        # Child and ordinary tasks already passed admission.
                        # Closing a writer must not discard accepted work.
                        claim_mcp = claim_reference_mcp_prompt_primary_for_worker
                        prompt_kwargs = {"sources": reference_skill_sources, "cipher": cipher}
                    claim = await claim_mcp(
                        **prompt_kwargs,
                        index=reference_mcp_index,
                        access=reference_mcp_access,
                        scope=ReferenceRequestScope(**reference_scope.model_dump(exclude={"policy_epoch"})),
                        turn_id=turn_req.turn_id,
                        owner_id=owner_id,
                        expected_generation=1,
                    )
                    dispatch_claim = "claimed" if claim.outcome == "claimed" else "rejected"
                elif reference_skill_sources is None:
                    claim = await claim_reference_primary_for_worker(
                        scope=reference_scope,
                        turn_id=turn_req.turn_id,
                        owner_id=owner_id,
                        expected_generation=1,
                        bindings=bindings,
                    )
                    dispatch_claim = "claimed" if claim.outcome == "claimed" else "rejected"
                else:
                    from pathlib import Path

                    from bowhead_core.chat_reference_scope import ReferenceRequestScope
                    from bowhead_core.skill_read_resolution import SkillReadSourceResolver
                    from bowhead_core.skill_reference_execution import claim_reference_skill_primary_for_worker

                    if (
                        type(reference_skill_sources) is not SkillReadSourceResolver
                        or Path(reference_skill_sources.db_path).resolve() != Path(bindings.db_path).resolve()
                    ):
                        raise ValueError("reference_skill_source_scope_changed")
                    claim = await claim_reference_skill_primary_for_worker(
                        scope=ReferenceRequestScope(**reference_scope.model_dump(exclude={"policy_epoch"})),
                        turn_id=turn_req.turn_id,
                        owner_id=owner_id,
                        expected_generation=1,
                        sources=reference_skill_sources,
                        cipher=cipher,
                    )
                    dispatch_claim = "claimed" if claim.outcome == "claimed" else "rejected"
                if dispatch_claim == "claimed":
                    turn_req.reference_runtime = runtime
            else:
                dispatch_claim = await session_store.claim_chat_spawn_dispatch_for_worker(
                    turn_id=turn_req.turn_id,
                    owner_id=owner_id,
                )
        except BaseException as claim_error:
            if getattr(turn_req, "reference_scope", None) is not None:
                # A failed/read-only claim grants no legacy writer authority.
                # Lost commit acknowledgements are recovered from the durable
                # composition and expired lease, without another invocation.
                await _cleanup_preworker_failure()
                raise
            await _handoff_unconfirmed_turn_then_cleanup(
                failure=claim_error,
                error="turn dispatch claim failed before worker start",
            )
            raise
        if dispatch_claim == "rejected":
            await _cleanup_preworker_failure()
            raise TurnDispatchAlreadyOwnedError("turn_dispatch_not_claimable")

    if dispatch_claim == "untracked" and terminal_runtime is None:
        if session_store is None:
            await _cleanup_preworker_failure()
            raise RuntimeError("legacy_chat_dispatch_store_unavailable")
        try:
            await session_store.ensure_session(
                session_id=turn_req.session_id,
                workspace_id=turn_req.workspace_id or "",
                user_id=turn_req.user_id or "",
            )
            await session_store.record_turn(
                turn_id=turn_req.turn_id,
                session_id=turn_req.session_id,
                user_text=turn_req.text or "",
            )
        except BaseException as persist_error:
            await _handoff_unconfirmed_turn_then_cleanup(
                failure=persist_error,
                error="turn persistence failed before worker start",
            )
            raise

        async def _preserve_recorded_turn_for_recovery() -> None:
            """Ensure a failed/unknown second claim cannot orphan a running Turn."""
            assert owner_id is not None
            await handoff_legacy_spawn_failure_to_recovery(
                session_store,
                turn_id=turn_req.turn_id,
                session_id=turn_req.session_id,
                owner_id=owner_id,
                error="turn dispatch claim failed before worker start",
                error_kind="dispatch_start_failed",
            )

        try:
            dispatch_claim = await session_store.claim_chat_spawn_dispatch_for_worker(
                turn_id=turn_req.turn_id,
                owner_id=owner_id,
            )
        except BaseException as claim_error:
            try:
                await _run_critical_preworker_handoff(
                    failure=claim_error,
                    recovery=_preserve_recorded_turn_for_recovery,
                    task_name=(f"turn_second_claim_recovery:{turn_req.turn_id}"),
                )
            except BaseException as repair_error:
                claim_error.add_note(
                    f"second claim recovery preparation also failed: {repair_error!r}",
                )
            await _cleanup_preworker_failure()
            raise
        if dispatch_claim != "claimed":
            await _cleanup_preworker_failure()
            raise TurnDispatchAlreadyOwnedError("turn_dispatch_not_claimable")

    # A durable Stop can win at either side of the final pre-worker callback.
    # Never schedule after observing its already-persisted process signal.
    await _raise_if_cancelled_before_worker()

    try:
        if before_schedule is not None:
            await before_schedule()
    except TerminalDurabilityAbort as exc:
        if terminal_runtime is not None:
            await _cleanup_preworker_failure()
            raise
        await _handoff_then_cleanup_preworker_failure(failure=exc)
        raise RuntimeError("legacy_chat_durability_recovery_handoff") from exc
    except BaseException as preworker_error:
        await _handoff_then_cleanup_preworker_failure(
            failure=preworker_error,
        )
        raise

    await _raise_if_cancelled_before_worker()

    if terminal_runtime is None and dispatch_claim == "claimed":
        assert session_store is not None
        assert owner_id is not None
        try:
            preworker_heartbeat = await session_store.heartbeat_chat_spawn_dispatch(
                turn_id=turn_req.turn_id,
                owner_id=owner_id,
            )
        except BaseException as revalidation_error:
            await _handoff_then_cleanup_preworker_failure(
                failure=revalidation_error,
                error="turn dispatch revalidation failed before worker start",
                error_kind="dispatch_start_failed",
            )
            raise
        if preworker_heartbeat is True:
            preworker_heartbeat = "renewed"
        elif preworker_heartbeat is False:
            preworker_heartbeat = "lost"
        if preworker_heartbeat == "cancel_requested":
            await _raise_if_cancelled_before_worker(
                durable_cancel_requested=True,
            )
        if preworker_heartbeat != "renewed":
            ownership_error = TurnDispatchAlreadyOwnedError(
                "turn_dispatch_ownership_lost_before_worker",
            )
            await _handoff_then_cleanup_preworker_failure(
                failure=ownership_error,
                error="turn dispatch ownership was lost before worker start",
                error_kind="dispatch_owner_lost",
            )
            raise ownership_error
        turn_req.execution_owner_id = owner_id
        if getattr(turn_req, "reference_runtime", None) is not None:
            try:
                await turn_req.reference_runtime.validate(turn_req)
            except BaseException as reference_error:
                await _handoff_then_cleanup_preworker_failure(failure=reference_error)
                raise

    def create_worker() -> asyncio.Task[Any]:
        async def _run_worker() -> Any:
            try:
                return await _run_turn_and_cleanup(
                    orchestrator,
                    turn_req,
                    user_store=user_store,
                    team_id=team_id,
                    metrics_log=metrics_log,
                    entitlements=entitlements,
                    admitted_legacy_bus=producer_bus,
                    cleanup_grace_s=cleanup_grace_s,
                )
            except TerminalDurabilityAbort as exc:
                if terminal_runtime is not None:
                    raise
                raise RuntimeError("legacy_chat_durability_recovery_handoff") from exc

        worker = _run_worker()
        try:
            task = asyncio.create_task(
                worker,
                name=f"{name_prefix}:{turn_req.turn_id}",
            )
        except BaseException:
            worker.close()
            raise
        _inflight_tasks.add(task)

        async def _retire_worker_generation() -> None:
            # Normal completion retires the producer in the worker tail before
            # releasing a Plan fence. This callback is the abnormal/pre-first-
            # step fallback: wait for the monitor's settlement/recovery decision
            # before waking subscribers so reconnect can observe durable truth.
            if monitor is not None and monitor is not asyncio.current_task():
                with contextlib.suppress(BaseException):
                    await _await_task_through_repeated_cancellation(monitor)
            await _retire_legacy_bus_on_failure()

        def _observe_worker_done(done: asyncio.Task[Any]) -> None:
            _inflight_tasks.discard(done)
            if terminal_runtime is None:
                _drop_exact_cancel_fence()
                retire_task = asyncio.create_task(
                    _retire_worker_generation(),
                    name=f"legacy_worker_generation_retire:{turn_req.turn_id}",
                )
                _inflight_tasks.add(retire_task)
                retire_task.add_done_callback(_inflight_tasks.discard)
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                _log.error(
                    "turn.worker_failed turn=%s error=%r",
                    turn_req.turn_id,
                    error,
                )

        task.add_done_callback(_observe_worker_done)
        return task

    monitor: asyncio.Task[None] | None = None
    worker_ready: asyncio.Future[None] | None = None
    if dispatch_claim == "claimed":

        async def _monitor_dispatch_lease() -> None:
            owner_lost = False
            lease_ownership_lost = False
            worker_finished = task.done()
            loop = asyncio.get_running_loop()
            lease_deadline = loop.time() + _CHAT_DISPATCH_LEASE_SECONDS
            cancel_waiter = (
                asyncio.create_task(
                    cancel_event.wait(),
                    name=f"turn_cancel_wait:{turn_req.turn_id}",
                )
                if cancel_event is not None
                else None
            )
            try:
                while not task.done():
                    waiters: set[asyncio.Future[Any] | asyncio.Task[Any]] = {
                        task,
                    }
                    if cancel_waiter is not None:
                        waiters.add(cancel_waiter)
                    done, _ = await asyncio.wait(
                        waiters,
                        timeout=1,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if task in done:
                        worker_finished = True
                        break
                    if cancel_waiter is not None and cancel_waiter in done:
                        if task.cancelling() == 0:
                            task.cancel("durable_user_cancel")
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                        except Exception:
                            _log.exception(
                                "turn.local_cancel_worker_failed turn=%s",
                                turn_req.turn_id,
                            )
                        worker_finished = task.done()
                        break
                    try:
                        recover_expired_after_cancel = False
                        heartbeat = await session_store.heartbeat_chat_spawn_dispatch(
                            turn_id=turn_req.turn_id,
                            owner_id=owner_id,
                        )
                    except Exception as exc:
                        _log.error(
                            "turn.dispatch_heartbeat_failed turn=%s owner=%s err=%s",
                            turn_req.turn_id,
                            owner_id,
                            exc,
                        )
                        # A transient busy/I/O error is not proof of owner loss.
                        # Keep the worker inside the lease already acquired and
                        # retry until its known deadline instead of killing a
                        # healthy turn on the first failed heartbeat.
                        if loop.time() < lease_deadline:
                            continue
                        heartbeat = "lost"
                        recover_expired_after_cancel = True
                    if heartbeat is True:
                        heartbeat = "renewed"
                    elif heartbeat is False:
                        heartbeat = "lost"
                    if heartbeat == "renewed":
                        lease_deadline = loop.time() + _CHAT_DISPATCH_LEASE_SECONDS
                        continue
                    if heartbeat in {"cancel_requested", "lost"}:
                        # The heartbeat result is already the durable ownership
                        # fact.  Do not put another Store read or recovery await
                        # between that fact and stopping provider/tool work.
                        # In particular, a lost owner must never reinterpret a
                        # cancelled Turn as permission to run its local terminal
                        # writer.
                        durable_cancel = heartbeat == "cancel_requested"
                        owner_lost = heartbeat == "lost"
                        lease_ownership_lost = owner_lost
                        if durable_cancel and cancel_event is not None:
                            cancel_event.set()
                        task.cancel(
                            "durable_user_cancel" if durable_cancel else "dispatch_owner_lost",
                        )
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                        except Exception:
                            _log.exception(
                                "turn.dispatch_owner_lost_worker_failed turn=%s",
                                turn_req.turn_id,
                            )
                        worker_finished = task.done()
                        if recover_expired_after_cancel:
                            try:
                                await session_store.recover_expired_chat_spawn_dispatches()
                            except Exception:
                                _log.exception(
                                    "turn.dispatch_expiry_recovery_failed turn=%s",
                                    turn_req.turn_id,
                                )
                        break
                worker_finished = task.done()
            finally:
                if cancel_waiter is not None:
                    if not cancel_waiter.done():
                        cancel_waiter.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await cancel_waiter
                if not worker_finished:
                    # Gateway shutdown or another abnormal monitor exit is not
                    # evidence that the worker completed. Keep the durable row
                    # spawned; its existing lease can expire and recovery can
                    # fence any stale writes from this process.
                    _log.info(
                        "turn.dispatch_monitor_exited_before_worker turn=%s owner=%s",
                        turn_req.turn_id,
                        owner_id,
                    )
                else:
                    if lease_ownership_lost:
                        # Once the Store no longer confirms this exact owner,
                        # this process cannot settle even a terminal Turn row:
                        # the worker may have been cancelled between its CAS
                        # and wire terminal. Expiry recovery reconstructs and
                        # fences that tail atomically.
                        _log.warning(
                            "turn.dispatch_owner_loss_requires_recovery turn=%s owner=%s",
                            turn_req.turn_id,
                            owner_id,
                        )
                        return
                    try:
                        durable_turn = await session_store.get_turn(
                            turn_req.turn_id,
                        )
                    except Exception:
                        # Worker completion is not a durable terminal fact.
                        # Keep the spawned lease recoverable when Store truth
                        # cannot be read instead of publishing false success.
                        _log.exception(
                            "turn.dispatch_terminal_fact_read_failed turn=%s owner=%s",
                            turn_req.turn_id,
                            owner_id,
                        )
                        return
                    durable_status = str(
                        getattr(durable_turn, "status", "") or "",
                    )
                    if durable_status not in LEGACY_TURN_WORKER_SETTLED_STATUSES:
                        # ``task.done()`` can also mean a swallowed Store/event
                        # failure. Only canonical Turn state authorizes ordered
                        # tail settlement; expiry recovery owns every unknown or
                        # still-running case.
                        _log.error(
                            "turn.dispatch_terminal_fact_unconfirmed turn=%s owner=%s status=%s",
                            turn_req.turn_id,
                            owner_id,
                            durable_status or "missing",
                        )
                        if task.cancelled():
                            _log.warning(
                                "turn.dispatch_worker_cancelled_before_settlement turn=%s owner=%s",
                                turn_req.turn_id,
                                owner_id,
                            )
                        else:
                            worker_error = task.exception()
                            if worker_error is not None:
                                _log.error(
                                    "turn.dispatch_worker_failed_before_settlement turn=%s owner=%s error=%r",
                                    turn_req.turn_id,
                                    owner_id,
                                    worker_error,
                                )
                        return
                    worker_error = None if task.cancelled() else task.exception()
                    if _contains_terminal_durability_abort(worker_error):
                        # The fail-stop may originate below a wrapper that did
                        # not latch this exact bus (for example a durable Store
                        # boundary reached after the Turn CAS). A terminal Turn
                        # row cannot turn that failed transport tail into proof
                        # of settlement; expiry recovery must rebuild/fence it.
                        _log.error(
                            "turn.dispatch_worker_durability_abort turn=%s owner=%s error=%r",
                            turn_req.turn_id,
                            owner_id,
                            worker_error,
                        )
                        return
                    # Canonical Store truth wins over wrapper-task outcome. Stop
                    # durably stamps cancelled before signalling the coroutine,
                    # and a fail-stop can arrive after the worker committed a
                    # terminal Turn. Both still require ordered-tail settlement.
                    try:
                        if producer_bus is not None:
                            producer_bus.raise_if_failed()
                        if legacy_registry is not None and hasattr(
                            legacy_registry,
                            "drain_turn_persistence",
                        ):
                            drain_task = asyncio.create_task(
                                legacy_registry.drain_turn_persistence(
                                    turn_req.turn_id,
                                ),
                                name=f"turn_log_drain:{turn_req.turn_id}",
                            )
                            while not drain_task.done():
                                done, _ = await asyncio.wait({drain_task}, timeout=1)
                                if done:
                                    break
                                if owner_lost:
                                    continue
                                try:
                                    settling_heartbeat = await session_store.heartbeat_chat_spawn_dispatch(
                                        turn_id=turn_req.turn_id,
                                        owner_id=owner_id,
                                        settling=True,
                                    )
                                except Exception as exc:
                                    # The durable lease remains conservative: keep
                                    # retrying while the tail is live. If maintenance
                                    # wins after an outage, the event-writer fence
                                    # rejects stale owner writes and terminalization
                                    # below becomes a no-op.
                                    _log.error(
                                        "turn.dispatch_settlement_heartbeat_failed turn=%s owner=%s err=%s",
                                        turn_req.turn_id,
                                        owner_id,
                                        exc,
                                    )
                                    continue
                                if settling_heartbeat != "renewed":
                                    owner_lost = True
                            await drain_task
                        # Ordered sinks may observe their tail failure without
                        # re-raising it from ``drain_turn``. The admitted bus is
                        # the fail-stop latch shared with the worker, so check it
                        # again after the tail drains before publishing a false
                        # successful settlement.
                        if producer_bus is not None:
                            producer_bus.raise_if_failed()
                        settled = await session_store.mark_chat_spawn_dispatch_terminal(
                            turn_id=turn_req.turn_id,
                            owner_id=owner_id,
                            failed=owner_lost,
                        )
                        if not settled and not owner_lost and normalize_legacy_turn_status(durable_status) == "cancelled":
                            # Stop may cancel the wrapper before TurnRunner can
                            # publish its terminal pair. The exact owner has
                            # drained its event tail, so surrender directly to
                            # recovery instead of keeping a 30s spawned lease.
                            handed_off = await session_store.mark_chat_spawn_dispatch_drain_pending(
                                turn_id=turn_req.turn_id,
                                owner_id=owner_id,
                                session_id=turn_req.session_id,
                                allow_terminal_session_idle=True,
                            )
                            if handed_off:
                                await session_store.recover_expired_chat_spawn_dispatches()
                                settled = (
                                    await session_store.get_chat_spawn_dispatch_projection_state(
                                        turn_id=turn_req.turn_id,
                                    )
                                    == "settled"
                                )
                        if not settled:
                            _log.warning(
                                "turn.dispatch_terminal_wire_unconfirmed turn=%s owner=%s status=%s",
                                turn_req.turn_id,
                                owner_id,
                                durable_status,
                            )
                    except TerminalDurabilityAbort:
                        # Keep the exact spawned lease for expiry recovery. It
                        # will reconstruct the missing canonical wire terminal
                        # from Store truth after the persistence outage clears.
                        _log.error(
                            "turn.dispatch_terminal_tail_uncommitted turn=%s owner=%s",
                            turn_req.turn_id,
                            owner_id,
                            exc_info=True,
                        )
                    except Exception:
                        _log.exception(
                            "turn.dispatch_terminalize_failed turn=%s owner=%s",
                            turn_req.turn_id,
                            owner_id,
                        )

        # Start the lease monitor before the worker.  This removes the unsafe
        # state where the worker has been scheduled but a second create_task
        # failure prevents any durable owner heartbeat/finalizer from existing.
        worker_ready = asyncio.get_running_loop().create_future()

        async def _monitor_after_worker_start() -> None:
            assert worker_ready is not None
            await worker_ready
            await _monitor_dispatch_lease()

        monitor_worker = _monitor_after_worker_start()
        try:
            monitor = asyncio.create_task(
                monitor_worker,
                name=f"turn_dispatch_lease:{turn_req.turn_id}",
            )
        except BaseException as monitor_error:
            monitor_worker.close()
            await _handoff_then_cleanup_preworker_failure(
                failure=monitor_error,
            )
            raise

    if terminal_runtime is None:
        try:
            task = create_worker()
        except BaseException as worker_start_error:
            if monitor is not None:
                monitor.cancel()
                try:
                    await _await_task_through_repeated_cancellation(monitor)
                except BaseException as monitor_error:
                    if not isinstance(monitor_error, asyncio.CancelledError):
                        worker_start_error.add_note(
                            f"dispatch monitor cleanup also failed: {monitor_error!r}",
                        )
            await _handoff_then_cleanup_preworker_failure(
                failure=worker_start_error,
            )
            raise
    else:
        try:
            from bowhead_core.terminal_protocol import TerminalScope

            runtime_scope = getattr(terminal_runtime, "scope", None)
            if (
                not isinstance(runtime_scope, TerminalScope)
                or runtime_scope.team_id != getattr(turn_req, "team_id", None)
                or runtime_scope.session_id != turn_req.session_id
                or runtime_scope.turn_id != turn_req.turn_id
            ):
                raise RuntimeError("terminal_runtime_request_scope_mismatch")
            # Same-Turn clarification/recovery resumes open generation N+1.
            # The runtime's persisted scope is authoritative; normalize the
            # request before the sole worker captures it.
            turn_req.execution_generation = runtime_scope.generation
            task, _started_here = await terminal_runtime.spawn_worker_once(create_worker)
        except asyncio.CancelledError:
            raise
        except BaseException as spawn_error:
            if terminal_runtime.bus.receipt is None and not terminal_runtime.retired:
                try:
                    await terminal_runtime.abort_worker_start_failed()
                except BaseException as abort_error:
                    spawn_error.add_note(f"terminal worker-start finalization also failed: {abort_error!r}")
            raise

    if worker_ready is not None:
        worker_ready.set_result(None)
        assert monitor is not None
        _inflight_tasks.add(monitor)
        monitor.add_done_callback(_inflight_tasks.discard)
    return task


async def turn_seq_seed(turn_log_store: Any, turn_id: str) -> int:
    """out-of-band emit / resume bus 播种的 seq 下界 = max(DB max_seq, 内存高水位).

    dingsiyan #11: pause 后任何补发事件（clarification.resolved / turn.resumed /
    cancel-directive 回执）都必须以此播种, 否则新 bus seq 从 0 计数, 与 turn 早期
    事件撞号 → FE 重放去重误杀。

    review P1-B: 光读 DB `max_seq` 不够——log_sink 是 fire-and-forget，前一条补发
    事件（如 clarification.resolved）落库有延迟，resolve→resume 顺序流上 resume 读到
    的 max_seq 可能不含它 → turn.resumed 撞号。registry 的**同步内存高水位**补上这个
    缺失的因果屏障（emit_out_of_band 每次 emit 后同步更新）。取两者 max：DB 覆盖
    进程重启后的持久真相，高水位覆盖尚未落库的在途事件。max_seq 异常回退到高水位
    （不再退回 0 撞号——review 测试 #5 的老 bug 复现路径）。
    """
    from bowhead_core.events import get_event_registry

    floor = get_event_registry().seq_floor(turn_id)
    if turn_log_store is None:
        return floor
    try:
        return max(int(await turn_log_store.max_seq(turn_id)), floor)
    except Exception:
        _log.exception("turn_seq_seed.max_seq_failed turn=%s floor=%d", turn_id, floor)
        return floor


async def emit_clarification_resolution_once(
    app_state: Any,
    *,
    turn_id: str,
    request_id: str,
    payload: dict[str, Any],
) -> bool:
    """Persist an IM clarification receipt unless that request is already logged.

    Returns ``True`` when a new event was emitted and ``False`` for an
    idempotent replay. The stored choice remains the sole payload authority.
    """
    turn_log_store = getattr(app_state, "turn_log_store", None)
    seed = await turn_seq_seed(turn_log_store, turn_id)
    await get_event_registry().emit_out_of_band(
        turn_id,
        OrchestratorEvent(
            event_type="clarification.resolved",
            turn_id=turn_id,
            payload=payload,
            persist_before_delivery=True,
            durable_event_id=f"evt_clarification_resolution_{request_id}",
        ),
        fallback_initial_seq=seed,
    )
    return True


def resume_already_in_flight(turn_id: str) -> bool:
    """True if a ``turn_resume`` worker for ``turn_id`` is already running.

    Resume is idempotent: an FE EventSource auto-reconnect (or a double card tap) can fire the
    resume twice; a second worker against the same turn_id would double-charge tokens and race the
    bus. Callers check this BEFORE the bus close/recreate (re-closing the bus of a live resume
    would strand it) and short-circuit to the existing stream.
    """
    from bowhead_gateway.routes.chat import _inflight_tasks

    name = f"turn_resume:{turn_id}"
    return any(t.get_name() == name and not t.done() for t in _inflight_tasks)


# dingsiyan #11 (2026-07-07): per-turn spawn lock. Between resume_already_in_flight's
# early check and actual create_task are multiple entitlement/datarc awaits; concurrent
# double-clicks/EventSource reconnect duplicates can both pass early checks, spawning twice for one turn_id (double billing
# + bus interference). Locked recheck closes the window; retain early check as the uncontended fast path.
_resume_spawn_locks: dict[str, asyncio.Lock] = {}
_RESUME_LOCK_MAX = 256


class TerminalProtocolSurfaceUnavailable(RuntimeError):
    code = "terminal_protocol_surface_unavailable"


async def require_terminal_surface_capability(
    app: Any,
    clarification: Any,
    *,
    user_id: str,
    surface: str,
) -> None:
    """Reject uncertified non-Web mutation of a persisted v1 clarification."""

    protocol_store = getattr(app.state, "terminal_protocol_store", None)
    if protocol_store is None:
        raise TerminalProtocolSurfaceUnavailable("terminal_protocol_surface_unavailable")
    from bowhead_gateway.terminal_protocol_rollout import (
        PersistedChatExecutionProtocol,
        resolve_persisted_chat_execution_protocol,
    )

    try:
        resume_context = clarification.resume_context if isinstance(clarification.resume_context, dict) else {}
        source_session_id = str(clarification.session_id or resume_context.get("session_id") or "").strip()
        user_store = getattr(app.state, "user_store", None)
        if user_store is None:
            raise RuntimeError("terminal_surface_user_store_missing")
        team_id = await user_store.get_user_team_id(user_id)
        if not team_id:
            raise RuntimeError("terminal_surface_team_missing")
        session_id, version = await protocol_store.read_persisted_turn_protocol_binding(
            team_id=team_id,
            turn_id=clarification.turn_id,
        )
        protocol = resolve_persisted_chat_execution_protocol(version)
    except TerminalProtocolSurfaceUnavailable:
        raise
    except Exception as exc:
        raise TerminalProtocolSurfaceUnavailable("terminal_protocol_surface_unavailable") from exc
    if source_session_id and source_session_id != session_id:
        raise TerminalProtocolSurfaceUnavailable("terminal_protocol_surface_unavailable")
    if protocol is PersistedChatExecutionProtocol.TERMINAL_V1 and surface not in {"web"}:
        raise TerminalProtocolSurfaceUnavailable("terminal_protocol_surface_unavailable")


def _resume_spawn_lock(turn_id: str) -> asyncio.Lock:
    lock = _resume_spawn_locks.get(turn_id)
    if lock is None:
        if len(_resume_spawn_locks) >= _RESUME_LOCK_MAX:
            for k in [k for k, v in _resume_spawn_locks.items() if not v.locked()][: _RESUME_LOCK_MAX // 2]:
                _resume_spawn_locks.pop(k, None)
        lock = asyncio.Lock()
        _resume_spawn_locks[turn_id] = lock
    return lock


async def _prepare_reference_bi_resume(orchestrator: Any, turn_req: Any):
    """Restore canonical source and current sources before claiming a generation."""
    import json
    from pathlib import Path

    import aiosqlite
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.chat_reference_bi_continuation import decision, open_checkpoint, pause_scope, read_pause, require_question
    from bowhead_core.chat_reference_continuation import is_lead
    from bowhead_core.chat_reference_lead_continuation import pause_ready as lead_pause_ready
    from bowhead_core.chat_reference_lead_continuation import read_pause as read_lead_pause
    from bowhead_core.chat_reference_lead_postdispatch import pause_ready as post_pause_ready
    from bowhead_core.chat_reference_lead_postdispatch import read_pause as read_post_pause
    from bowhead_core.chat_reference_scope import ReferenceRequestScope
    from bowhead_core.chat_reference_storage import ReferenceCipher, ReferenceScope
    from bowhead_core.session_store import _read_chat_turn_user_source_in_tx
    from bowhead_core.skill_read_resolution import SkillReadSourceResolver

    clarification_id = getattr(turn_req, "reference_clarification_id", None)
    if clarification_id is None:
        return None
    factory = getattr(orchestrator, "reference_skill_sources_factory", None)
    if os.environ.get("FEATURE_CHAT_REFERENCE_PRIMARY_RUNTIME", "1") != "1":
        raise ValueError("reference_continuation_unavailable")
    db_path = str(orchestrator.session_store._db_path)
    async with open_shared_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN")
        if await is_lead(db, turn_req.turn_id):
            pre_ready = await lead_pause_ready(db, turn_id=turn_req.turn_id, clarification_id=clarification_id)
            post_ready = await post_pause_ready(db, turn_id=turn_req.turn_id, clarification_id=clarification_id)
            if pre_ready == post_ready:
                raise ValueError("reference_continuation_unavailable")
            pause = await (read_lead_pause(db, turn_req.turn_id) if pre_ready else read_post_pause(db, turn_req.turn_id))
            scope = ReferenceScope(
                team_id=pause["original_team_id"],
                user_id=pause["original_user_id"],
                workspace_id=pause["original_workspace_id"],
                session_id=pause["original_session_id"],
                policy_epoch=pause["created_policy_epoch"],
            )
            if post_ready:
                from bowhead_core.chat_reference_lead_postdispatch import open_read_question

                await open_read_question(db, pause, scope=scope, cipher=ReferenceCipher(os.environ.get("BOWHEAD_FERNET_KEY", "")))
        else:
            pause = await read_pause(db, turn_req.turn_id)
            if pause is None or pause["clarification_id"] != clarification_id:
                raise ValueError("reference_continuation_unavailable")
            scope = pause_scope(pause)
            open_checkpoint(pause, cipher=ReferenceCipher(os.environ.get("BOWHEAD_FERNET_KEY", "")))
        if any(getattr(turn_req, key, None) != value for key, value in scope.identity().items()):
            raise ValueError("reference_continuation_changed")
        if not await is_lead(db, turn_req.turn_id):
            question = await require_question(db, pause, resolved=True)
            _, selected, _answer = decision(question)
            if turn_req.active_datarc_project != selected or (
                selected is not None and selected not in {str(item.get("id")) for item in turn_req.accessible_datarc_workspaces or ()}
            ):
                raise ValueError("reference_continuation_changed")
        source = await _read_chat_turn_user_source_in_tx(
            db, turn_id=turn_req.turn_id, expected_user_id=scope.user_id, expected_workspace_id=scope.workspace_id
        )
        if source is None or source.input_document is None:
            raise ValueError("reference_source_unavailable")
        # Resume the original admitted adapter. An ordinary system Agent-only
        # composition never acquired a precise Skill material baseline.
        async with db.execute(
            "SELECT composition_json FROM chat_reference_resolutions WHERE turn_id=? AND resolution_seq=1", (turn_req.turn_id,)
        ) as cursor:
            composition = await cursor.fetchone()
        ordinary_agent_only = composition is not None and json.loads(composition[0]).get("runtime_policy") == "initial-primary.v1"

    from bowhead_core.chat_references import McpPromptResourceRef, McpSourceResourceRef, McpToolResourceRef, ReferenceNode

    references = [node.resource for node in source.input_document.nodes if isinstance(node, ReferenceNode)]
    mcp_read = any(isinstance(resource, McpSourceResourceRef | McpToolResourceRef) for resource in references)
    mcp_prompt = any(isinstance(resource, McpPromptResourceRef) for resource in references)
    inputs = {}
    if not mcp_read and not ordinary_agent_only:
        if not callable(factory):
            raise ValueError("reference_continuation_unavailable")
        sources = factory(ReferenceRequestScope(**scope.model_dump(exclude={"policy_epoch"})))
        if type(sources) is not SkillReadSourceResolver or Path(sources.db_path).resolve() != Path(db_path).resolve():
            raise ValueError("reference_skill_source_scope_changed")
        await sources.access_factory()
        inputs["reference_skill_sources"] = sources
    turn_req.reference_scope = scope
    turn_req.text = source.user_text
    turn_req.attachments = list(source.attachment_ids)
    if mcp_read or mcp_prompt:
        from bowhead_core.access_control import AccessControlStore
        from bowhead_core.reference_mcp_metadata import ReferenceMcpMetadataIndex

        cache = getattr(orchestrator, "reference_mcp_metadata", None)
        index = cache.current() if cache is not None else None
        access = getattr(orchestrator, "access_control_store", None)
        if (
            type(index) is not ReferenceMcpMetadataIndex
            or not index.ready
            or type(access) is not AccessControlStore
            or Path(index.db_path).resolve() != Path(db_path).resolve()
            or Path(access.db_path).resolve() != Path(db_path).resolve()
        ):
            raise ValueError("reference_mcp_source_scope_changed")
        # The original acceptance owns the writer decision. Resuming consumes
        # retained material under current access, never a new Prompt expansion.
        inputs.update(reference_mcp_index=index, reference_mcp_access=access, reference_mcp_prompt=mcp_prompt)
    return inputs


async def _restore_bounded_question_inputs(orchestrator: Any, turn_req: Any) -> None:
    """Restore original text and separate answer data before worker admission."""
    if (
        not getattr(turn_req, "resume_clarification_id", None)
        or getattr(turn_req, "reference_clarification_id", None)
        or getattr(turn_req, "ordinary_clarification_id", None)
    ):
        return
    import json

    from bowhead_core._db import open_shared_ctx
    from bowhead_core.clarification_rounds import history_contexts, read_history, validate_answer_questions
    from bowhead_core.session_store import _read_chat_turn_user_source_in_tx

    from bowhead_gateway.routes.clarifications import ClarificationAnswerDTO, _is_skip_value, _render_legacy_values_for_resume

    responses = []
    async with open_shared_ctx(orchestrator.session_store.db_path) as db:
        await db.execute("BEGIN")
        source = await _read_chat_turn_user_source_in_tx(
            db, turn_id=turn_req.turn_id, expected_user_id=turn_req.user_id, expected_workspace_id=turn_req.workspace_id
        )
        if source is None or source.session_id != turn_req.session_id:
            raise ValueError("clarification_source_changed")
        rows = await read_history(
            db, turn_id=turn_req.turn_id, user_id=turn_req.user_id, workspace_id=turn_req.workspace_id, session_id=turn_req.session_id
        )
        contexts = history_contexts(rows)
        if not rows or rows[-1]["request_id"] != turn_req.resume_clarification_id:
            raise ValueError("clarification_round_history_changed")
        for row, context in zip(rows, contexts, strict=True):
            if (
                row["status"] != "resolved"
                or row["kind"] != "ambiguous_turn"
                or row["token"]
                or row["tool"]
                or context.get("__schema_version") != 2
                or (context.get("user_id"), context.get("workspace_id"), context.get("session_id"), context.get("team_id"))
                != (turn_req.user_id, turn_req.workspace_id, turn_req.session_id, turn_req.team_id)
            ):
                raise ValueError("clarification_round_history_changed")
            raw_answers = json.loads(row["answers_json"] or "[]")
            if not isinstance(raw_answers, list) or any(not isinstance(answer, dict) for answer in raw_answers):
                raise ValueError("clarification_answer_changed")
            answers = [ClarificationAnswerDTO.model_validate(answer, strict=True).model_dump(exclude_none=True) for answer in raw_answers]
            if answers != [{key: value for key, value in answer.items() if value is not None} for answer in raw_answers]:
                raise ValueError("clarification_answer_changed")
            if answers:
                validate_answer_questions(answers, context)
            skipped = _is_skip_value(row["chosen_value"])
            values = json.loads(row["chosen_values_json"] or "[]") or ([row["chosen_value"]] if row["chosen_value"] else [])
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values) or not (answers or skipped or values):
                raise ValueError("clarification_answer_changed")
            responses.append(
                {
                    "clarification_id": row["request_id"],
                    "answers": answers,
                    "skipped": skipped,
                    # Old scalar clients have no typed question association.
                    # Keep their data without manufacturing answer authority.
                    "legacy_selected_values": _render_legacy_values_for_resume(values) if not answers and not skipped else [],
                }
            )
    if source.attachment_ids:
        from bowhead_gateway.chat_attachment_access import validate_chat_attachment_access

        artifact_store = getattr(orchestrator, "artifact_store", None)
        if source.attachment_refs:
            await validate_chat_attachment_access(
                artifact_store=artifact_store,
                refs=source.attachment_refs,
                runtime_ids=source.attachment_ids,
                user_id=turn_req.user_id,
                workspace_id=turn_req.workspace_id,
                session_id=turn_req.session_id,
            )
        else:
            reader = getattr(artifact_store, "get_ready_uploads_for_chat", None)
            ready = await reader(list(source.attachment_ids), user_id=turn_req.user_id, workspace_id=turn_req.workspace_id) if reader else {}
            if set(ready) != set(source.attachment_ids):
                raise ValueError("clarification_attachment_unavailable")
    turn_req.attachments = list(source.attachment_ids)
    turn_req.text = source.user_text
    turn_req.resume_answer_context = json.dumps({"clarification_responses": responses}, ensure_ascii=False, separators=(",", ":"))


async def _restore_ordinary_question_inputs(orchestrator: Any, turn_req: Any) -> None:
    from types import SimpleNamespace

    from bowhead_core._db import open_shared_ctx
    from bowhead_core.ordinary_lead_continuation import ordinary_resume_request_material, require_ordinary_pause_in_tx
    from bowhead_core.ordinary_lead_runtime import validate_ordinary_retained_materials
    from bowhead_core.session_store import _read_chat_turn_user_source_in_tx
    from bowhead_core.turn_runner import _make_subagent_runner_with_catalog

    from bowhead_gateway.chat_attachment_access import validate_chat_attachment_access

    async with open_shared_ctx(orchestrator.session_store.db_path) as db:
        await db.execute("BEGIN")
        record = await require_ordinary_pause_in_tx(db, card_id=turn_req.ordinary_clarification_id, answered=True)
        checkpoint, context = ordinary_resume_request_material(record.card, db_path=orchestrator.session_store.db_path)
        if (turn_req.team_id, turn_req.user_id, turn_req.workspace_id, turn_req.session_id, turn_req.turn_id) != (
            record.snapshot.team_id,
            record.snapshot.user_id,
            record.snapshot.workspace_id,
            record.snapshot.session_id,
            record.snapshot.turn_id,
        ):
            raise ValueError("clarification_resume_unavailable")
        source = await _read_chat_turn_user_source_in_tx(
            db, turn_id=turn_req.turn_id, expected_user_id=turn_req.user_id, expected_workspace_id=turn_req.workspace_id
        )
        if source is None:
            raise ValueError("clarification_resume_unavailable")
    if source.attachment_ids:
        await validate_chat_attachment_access(
            artifact_store=orchestrator.artifact_store,
            refs=source.attachment_refs,
            runtime_ids=source.attachment_ids,
            user_id=turn_req.user_id,
            workspace_id=turn_req.workspace_id,
            session_id=turn_req.session_id,
        )
    # This same preflight serves read-only history probes without a team Router.
    # Reuse dispatch's profile resolver with no model runtime or execution right.
    profile_runner = (
        _make_subagent_runner_with_catalog(
            router=None,
            tool_registry=getattr(orchestrator, "tool_registry", None),
            custom_store=None,
            product_terms_catalog=getattr(orchestrator, "product_terms_catalog", None),
        )
        if checkpoint.contract
        in {
            "ordinary-lead-loop.v6",
            "ordinary-lead-loop.v7",
            "ordinary-lead-loop.v8",
            "ordinary-lead-loop.v9",
            "ordinary-lead-loop.v10",
            "ordinary-lead-loop.v11",
            "ordinary-lead-loop.v12",
            "ordinary-lead-loop.v13",
        }
        else None
    )
    await validate_ordinary_retained_materials(
        SimpleNamespace(
            subagent_runner=profile_runner,
            session_store=orchestrator.session_store,
            artifact_store=orchestrator.artifact_store,
            skill_catalog=getattr(orchestrator, "skill_catalog", None),
            canvas_store=getattr(orchestrator, "canvas_store", None),
            team_id=turn_req.team_id,
            user_id=turn_req.user_id,
            workspace_id=turn_req.workspace_id,
            session_id=turn_req.session_id,
            turn_id=turn_req.turn_id,
        ),
        checkpoint,
    )
    turn_req.text = source.user_text
    turn_req.attachments = list(source.attachment_ids)
    turn_req.ordinary_lead_checkpoint = checkpoint
    turn_req.resume_answer_context = None
    if turn_req.active_datarc_project != context.get("active_datarc_project"):
        raise ValueError("clarification_resume_unavailable")


async def spawn_resume_turn(
    orchestrator: Any,
    turn_req: Any,
    *,
    user_store: Any = None,
    team_id: str | None = None,
    entitlements: Any = None,
    initial_seq: int,
    on_bus_ready: Callable[[], None] | None = None,
    resume_notice: dict[str, Any] | None = None,
) -> ResumeTurnSpawn:
    """Re-create the turn's event bus (the generation guard) and spawn the resume worker.

    The clarification-resume tail shared by web (``clarifications.py``) and Feishu (``bridge.py``).
    Keeps the SAME turn_id (Option A same-turn-resume). Steps:

    1. Atomically advance ``clarification_pending→running``, increment the durable execution
       generation, and publish a leased ``claimed`` resume dispatch. Startup and maintenance can
       therefore distinguish a healthy old clarification resume from an untracked orphan.
    2. ``registry.close(turn_id)`` — drop the closed bus the original turn left in the registry (its
       sentinel emit set ``_closed=True``; ``get_or_create`` would otherwise hand back the dead bus
       and new events would silently no-op).
    3. ``admit_legacy_chat_execution(initial_seq=...)`` — a fresh durable bus at the same
       turn_id. ``initial_seq`` seeds the seq counter past the persisted replay max so live resume
       events don't reuse low seqs the FE per-turn dedupe would drop as replay duplicates. Every
       channel passes the persisted plus in-memory event high-water mark.
    4. ``on_bus_ready()`` — channel hook run AFTER the fresh bus exists, BEFORE the worker (Feishu
       restarts its card mirror here so it subscribes before the worker emits its first event).
    5. ``spawn_turn`` claims that durable dispatch, emits the persist-before-live resume notice,
       and only then schedules ``_run_turn_and_cleanup`` plus its lease heartbeat/finalizer.

    Resume continues the existing active Turn rather than rewriting its lifecycle pointer, and
    both resume call sites still omit ``metrics_log``. Returns the worker and durable execution
    generation. Callers MAY use
    ``resume_already_in_flight(turn_id)`` as an early fast path; the lock remains authoritative.
    """
    from bowhead_gateway.routes.chat import (
        _inflight_tasks,
    )

    async with _resume_spawn_lock(turn_req.turn_id):
        # Locked idempotency recheck (see _resume_spawn_locks): reuse existing live workers;
        # never close/recreate the bus, stranding the running resume.
        name = f"turn_resume:{turn_req.turn_id}"
        session_store = getattr(orchestrator, "session_store", None)
        advance_generation = getattr(session_store, "advance_turn_execution_generation", None)
        get_turn = getattr(session_store, "get_turn", None)
        get_remote_generation = getattr(
            session_store,
            "get_active_resume_dispatch_generation",
            None,
        )
        rollback_generation = getattr(
            session_store,
            "rollback_turn_resume_generation",
            None,
        )
        if advance_generation is None or get_turn is None or get_remote_generation is None or rollback_generation is None:
            raise RuntimeError("resume requires a SessionStore generation lease")

        for t in _inflight_tasks:
            if t.get_name() == name and not t.done():
                current_turn = await get_turn(turn_req.turn_id)
                generation = max(
                    1,
                    int(getattr(current_turn, "execution_generation", 1) or 1),
                )
                return ResumeTurnSpawn(task=t, execution_generation=generation)

        registry = getattr(orchestrator, "event_registry", None)
        if registry is None:
            raise RuntimeError("legacy_chat_event_registry_unavailable")
        document_clarification_id = getattr(turn_req, "document_clarification_id", None)
        if document_clarification_id is not None:
            remote_generation = await get_remote_generation(turn_id=turn_req.turn_id)
            if remote_generation is not None:
                return ResumeTurnSpawn(task=None, execution_generation=remote_generation, attached_remote=True)
            from bowhead_core.document_clarification_lifecycle import hydrate_document_resume_request

            await hydrate_document_resume_request(session_store, turn_req)
        if getattr(turn_req, "ordinary_clarification_id", None):
            await _restore_ordinary_question_inputs(orchestrator, turn_req)
        reference_inputs = await _prepare_reference_bi_resume(orchestrator, turn_req)
        await _restore_bounded_question_inputs(orchestrator, turn_req)
        generation = await advance_generation(
            turn_id=turn_req.turn_id,
            **({"document_clarification_id": document_clarification_id} if document_clarification_id else {}),
            **({"reference_clarification_id": turn_req.reference_clarification_id} if reference_inputs is not None else {}),
            **({"clarification_id": turn_req.resume_clarification_id} if getattr(turn_req, "resume_clarification_id", None) else {}),
        )
        if generation is None:
            remote_generation = await get_remote_generation(
                turn_id=turn_req.turn_id,
            )
            if remote_generation is not None:
                # A live spawned lease is durable proof that another Gateway
                # owns the same resume worker. A bare running generation is not.
                return ResumeTurnSpawn(
                    task=None,
                    execution_generation=remote_generation,
                    attached_remote=True,
                )
            raise ResumeTurnConflictError(turn_req.turn_id)

        bus = None
        owner_id = f"resume_{uuid4().hex}"
        resume_retry_state_restored = False

        async def _restore_resume_retry_state() -> None:
            nonlocal resume_retry_state_restored
            if resume_retry_state_restored:
                return
            rolled_back = await rollback_generation(
                turn_id=turn_req.turn_id,
                expected_execution_generation=int(generation),
                owner_id=owner_id,
            )
            if not rolled_back:
                rolled_back = await rollback_generation(
                    turn_id=turn_req.turn_id,
                    expected_execution_generation=int(generation),
                    owner_id=None,
                )
            if not rolled_back:
                current_turn = await get_turn(turn_req.turn_id)
                current_status = str(
                    getattr(current_turn, "status", "") or "",
                )
                if current_status in LEGACY_TURN_WORKER_SETTLED_STATUSES:
                    await session_store.mark_chat_spawn_dispatch_terminal(
                        turn_id=turn_req.turn_id,
                        owner_id=owner_id,
                    )
                    await session_store.recover_expired_chat_spawn_dispatches()
                    projection = await session_store.get_chat_spawn_dispatch_projection_state(
                        turn_id=turn_req.turn_id,
                    )
                    if projection == "settled":
                        resume_retry_state_restored = True
                        return
                raise RuntimeError("resume retry state was not restored")
            resume_retry_state_restored = True

        try:
            previous_bus, _previous_floor = await registry.projection_checkpoint(
                turn_req.turn_id,
            )
            if previous_bus is not None:
                await registry.close(
                    turn_req.turn_id,
                    expected=previous_bus,
                )
            bus = await admit_legacy_chat_execution(
                orchestrator,
                turn_req,
                initial_seq=initial_seq,
            )
            if on_bus_ready is not None:
                on_bus_ready()

            turn_req.execution_generation = int(generation)

            async def _emit_resume_notice() -> None:
                if resume_notice is None:
                    return
                # User-visible acknowledgement is the final pre-worker step.
                # spawn_turn has already claimed the durable lease and upgraded
                # the bus to persist-before-live, but has not scheduled the
                # worker yet.
                notice_persisted = await bus.emit(
                    OrchestratorEvent(
                        event_type="turn.resumed",
                        turn_id=turn_req.turn_id,
                        payload={
                            "turn_id": turn_req.turn_id,
                            "execution_generation": int(generation),
                            **resume_notice,
                        },
                    )
                )
                if not notice_persisted:
                    raise RuntimeError("resume notice was not durably accepted")

            task = await spawn_turn(
                orchestrator,
                turn_req,
                user_store=user_store,
                team_id=team_id,
                entitlements=entitlements,
                name_prefix="turn_resume",
                dispatch_owner_id=owner_id,
                admitted_legacy_bus=bus,
                before_schedule=_emit_resume_notice,
                preworker_failure_recovery=_restore_resume_retry_state,
                **(reference_inputs or {}),
            )
            return ResumeTurnSpawn(task=task, execution_generation=int(generation))
        except BaseException as spawn_error:
            caught_spawn_error = spawn_error

            async def _cleanup_failed_resume_spawn() -> None:
                try:
                    await _restore_resume_retry_state()
                except BaseException as rollback_error:
                    caught_spawn_error.add_note(
                        f"resume retry-state rollback also failed: {rollback_error!r}",
                    )
                    _log.exception(
                        "resume.spawn_failure_rollback_failed turn=%s",
                        turn_req.turn_id,
                    )
                finally:
                    if bus is not None:
                        await _retire_admitted_legacy_bus(
                            registry,
                            turn_id=turn_req.turn_id,
                            producer_bus=bus,
                        )

            try:
                await _run_cleanup_through_repeated_cancellation(
                    _cleanup_failed_resume_spawn,
                    task_name=f"resume_spawn_cleanup:{turn_req.turn_id}",
                )
            except BaseException as cleanup_error:
                spawn_error.add_note(
                    f"resume spawn cleanup also failed: {cleanup_error!r}",
                )
                _log.exception(
                    "resume.spawn_failure_cleanup_failed turn=%s",
                    turn_req.turn_id,
                )
            raise


def build_resume_turn_request(
    req: Any,
    *,
    source_db_path: str | None = None,
    datarc_client: Any = None,
    datarc_binding: Any = None,
    datarc_unavailable: bool = False,
    vector_client: Any = None,
    team_router: Any = None,
    team_kb_retriever: Any = None,
) -> Any | None:
    """Reconstruct a same-turn-resume ``TurnRequest`` from a RESOLVED clarification's resume_context.

    Shared by every supported channel's clarification resume (web / Feishu) so the user_text merge +
    field reconstruction isn't copied a third time. The caller resolves the team's datarc/vector
    clients (§1.4 tenant isolation) and passes them in. Returns ``None`` when the clarification isn't
    resumable (no resume_context / missing user_text / missing session).

    Legacy empty-token cards append choices to their saved ask; bounded cards
    restore canonical text and separate answer data in spawn_resume_turn. Token
    cards carry a resume_glossary so Lead does not repeat the same question.
    """
    from bowhead_core.coach_canvas_target import unrestored_coach_canvas_target
    from bowhead_core.configuration_settings_target import unrestored_settings_target
    from bowhead_core.language_context import RESUME_LANGUAGE_KEY, resume_viewer_time_zone

    from bowhead_core import LanguageContext, TurnRequest

    rc = getattr(req, "resume_context", None)
    if not isinstance(rc, dict) or not rc:
        return None
    # Share the Web reader's version gate. Unsupported snapshots remain
    # untouched and cannot reconstruct an ordinary Lead request by fallback.
    if not resume_context_is_readable(rc):
        _log.warning(
            "resume_context.unsupported request_id=%s version=%s max_supported=%s",
            getattr(req, "request_id", None),
            resume_context_version(rc),
            RESUME_CONTEXT_SCHEMA_VERSION,
        )
        return None
    from bowhead_core.clarification_resume_context import DocumentResumeContext, OrdinaryLeadResumeContext, decode_clarification_context

    try:
        decoded = decode_clarification_context(req)
    except ValueError:
        return None
    if isinstance(decoded, DocumentResumeContext):
        if (req.turn_id, req.session_id, req.workspace_id, req.user_id) != (
            decoded.turn_id,
            decoded.session_id,
            decoded.workspace_id,
            decoded.user_id,
        ):
            return None
        return TurnRequest(
            session_id=decoded.session_id,
            workspace_id=decoded.workspace_id,
            user_id=decoded.user_id,
            member_uid="",
            text=decoded.user_text,
            turn_id=decoded.turn_id,
            team_id=decoded.team_id,
            resume_turn_id=decoded.turn_id,
            document_clarification_id=req.request_id,
            document_checkpoint=decoded.continuation.checkpoint,
            team_router=team_router,
            team_kb_retriever=team_kb_retriever,
        )
    ordinary_checkpoint = None
    if isinstance(decoded, OrdinaryLeadResumeContext):
        from bowhead_core.ordinary_lead_continuation import ordinary_resume_request_material

        try:
            ordinary_checkpoint, rc = ordinary_resume_request_material(req, db_path=source_db_path)
        except ValueError:
            return None
        user_text = decoded.user_text
        semantics = None
    else:
        semantics = build_clarification_resume_semantics(req)
        user_text = semantics.user_text
    session_id = str(rc.get("session_id") or "")
    if not (user_text and session_id):
        return None
    return TurnRequest(
        session_id=session_id,
        configuration_settings_target=unrestored_settings_target(rc),
        coach_canvas_target=unrestored_coach_canvas_target(rc),
        workspace_id=rc.get("workspace_id") or getattr(req, "workspace_id", ""),
        active_datarc_project=rc.get("active_datarc_project") or rc.get("datarc_workspace_id"),
        datarc_binding=datarc_binding,
        datarc_source_evidence=rc.get("datarc_selection"),
        datarc_client=datarc_client,
        datarc_unavailable=datarc_unavailable,
        vector_client=vector_client,
        # byo-no-clone: carry the typed team runtime so the resume turn (where the actual
        # analysis/subagent work runs) honors the team model, not the process default. An
        # authenticated caller must fail closed before constructing this request when resolution
        # is unavailable.
        team_router=team_router,
        team_kb_retriever=team_kb_retriever,
        user_id=rc.get("user_id") or getattr(req, "user_id", ""),
        member_uid=rc.get("member_uid") or "",
        text=user_text,
        turn_id=req.turn_id,
        resume_clarification_id=req.request_id if "question_round" in rc or ordinary_checkpoint is not None else None,
        ordinary_clarification_id=req.request_id if ordinary_checkpoint is not None else None,
        ordinary_lead_checkpoint=ordinary_checkpoint,
        reference_clarification_id=req.request_id
        if isinstance(rc.get("reference_continuation"), dict)
        and rc["reference_continuation"].get("contract")
        in {"reference-bi-workspace.v1", "reference-lead-predispatch.v1", "reference-lead-postdispatch.v1"}
        else None,
        team_id=rc.get("team_id"),
        # Batch 7-F: restoring the **same turn** must preserve that turn's chosen language.
        # Previously hardcoded default() switched English users to Chinese after one HITL clarification
        # (README §6 gap 1, registered but unfixed; Feishu and terminal approval execution both use this constructor).
        # Old snapshots omit this key; from_dict(None) falls back to default, preserving old behavior byte-for-byte.
        language_context=ordinary_checkpoint.language_context()
        if ordinary_checkpoint is not None
        else LanguageContext.from_dict(rc.get(RESUME_LANGUAGE_KEY)),
        # Same Turn ⇒ same model clock, also when no language snapshot was saved.
        viewer_time_zone=resume_viewer_time_zone(rc),
        kb_role=rc.get("kb_role") or "user",
        paradigm_v2=bool(rc.get("paradigm_v2", False)),
        preferred_provider=rc.get("preferred_provider"),
        preferred_model=rc.get("preferred_model"),
        resume_turn_id=req.turn_id,
        resume_glossary=semantics.resume_glossary or None if semantics is not None else None,
    )


async def resolve_team_model_runtime(
    app_state: Any,
    team_id: str | None,
    *,
    entrypoint: TeamModelEntrypoint,
) -> TeamModelRuntimeResolution:
    """Resolve an authenticated team's Router without any deployment fallback.

    The returned value is typed and content-free so callers must inspect the
    finite result instead of treating ``None`` as permission to use
    ``app.state.router``.  KB remains optional: its build can fail without
    weakening the model boundary, but the Router itself must resolve from the
    current team's saved config.
    """

    def finish(
        result: TeamModelResolutionResult,
        reason: TeamModelResolutionReason,
        *,
        router: Any = None,
        kb_retriever: Any = None,
    ) -> TeamModelRuntimeResolution:
        record_team_model_resolution(
            entrypoint=entrypoint,
            result=result,
            reason=reason,
        )
        return TeamModelRuntimeResolution(
            router=router,
            kb_retriever=kb_retriever,
            result=result,
            reason=reason,
        )

    if not team_id:
        return finish("team_unavailable", "team_scope_missing")

    team_registry = getattr(app_state, "team_router_registry", None)
    user_store = getattr(app_state, "user_store", None)
    if team_registry is None or user_store is None:
        return finish(
            "team_unavailable",
            "runtime_dependency_unavailable",
        )

    try:
        llm_cfg, llm_version = await user_store.get_team_llm_config_with_version(
            team_id,
        )
    except Exception as exc:
        _log.warning(
            "team_model_runtime.config_load_failed team=%s entrypoint=%s error_type=%s",
            team_id,
            entrypoint,
            type(exc).__name__,
        )
        return finish("resolver_error", "config_load_failed")
    if not llm_cfg:
        return finish("team_unavailable", "config_missing")

    try:
        from bowhead_core.team_llm_config import (
            TeamLlmConfigError,
            is_v2_config,
            migrate_legacy_config_to_v2,
            validate_v2_config,
        )

        llm_cfg = llm_cfg if is_v2_config(llm_cfg) else migrate_legacy_config_to_v2(llm_cfg)
        if not is_v2_config(llm_cfg):
            return finish("team_unavailable", "config_invalid")
        validate_v2_config(
            llm_cfg,
            allow_legacy_duplicate_fallbacks=True,
        )
        team_router = await team_registry.get(
            team_id,
            llm_cfg,
            llm_version,
        )
    except (TeamLlmConfigError, TypeError, ValueError):
        return finish("team_unavailable", "config_invalid")
    except Exception as exc:
        _log.warning(
            "team_model_runtime.router_build_failed team=%s entrypoint=%s error_type=%s",
            team_id,
            entrypoint,
            type(exc).__name__,
        )
        return finish("resolver_error", "router_build_failed")
    if team_router is None:
        return finish("team_unavailable", "router_unavailable")

    team_kb_retriever: Any = None
    team_kb_registry = getattr(app_state, "team_kb_runtime_registry", None)
    if team_kb_registry is not None:
        try:
            runtime = await team_kb_registry.get(
                team_id,
                llm_cfg,
                llm_version,
            )
            team_kb_retriever = getattr(runtime, "retriever", None)
        except Exception as exc:
            _log.warning(
                "team_model_runtime.kb_build_failed team=%s entrypoint=%s error_type=%s",
                team_id,
                entrypoint,
                type(exc).__name__,
            )

    return finish(
        "team_safe_success",
        "resolved",
        router=team_router,
        kb_retriever=team_kb_retriever,
    )


async def resolve_team_byo_overrides(
    app_state: Any,
    team_id: str | None,
    *,
    entrypoint: TeamModelEntrypoint = "tool_continuation",
) -> tuple[Any, Any]:
    """Compatibility tuple around the strict typed team runtime resolver.

    New authenticated continuation code must call
    :func:`resolve_team_model_runtime` directly and inspect ``available``.
    This wrapper exists for non-Composer callers while they migrate; it never
    returns a deployment Router on failure.
    """
    resolved = await resolve_team_model_runtime(
        app_state,
        team_id,
        entrypoint=entrypoint,
    )
    return resolved.router, resolved.kb_retriever
