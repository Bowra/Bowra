"""Retain settled ordinary helpers and continue the original Lead loop.

The checkpoint carries model data, never permission to execute. Every resume
reauthorizes current principals and retained material before consuming the
original card under the active worker lease.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from typing import TYPE_CHECKING

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.ordinary_lead_checkpoint import (
    OrdinaryHelperOutcome,
    OrdinaryLeadCheckpoint,
    OrdinaryLeadRunState,
    capture_message_prefix,
    current_turn_messages,
)
from bowhead_core.ordinary_lead_continuation import (
    OrdinaryContinuationUnavailable,
    OrdinaryQuestionUnavailable,
    canonical_json,
    consume_ordinary_resume_in_tx,
    content_hash,
    ordinary_multi_helper_question_enabled,
    read_ordinary_card_in_tx,
    reopen_ordinary_checkpoint,
    require_live_worker_in_tx,
    retained_lead_call,
    typed_answer,
)

if TYPE_CHECKING:
    from bowhead_router.types import Message, ToolSchema

    from bowhead_core._ref_allowlist import RefAllowlist
    from bowhead_core.chat_turn_source import ChatTurnUserSource
    from bowhead_core.document_chat_continuation import DocumentTurnOwner
    from bowhead_core.documents.routing import DocumentRoutingSnapshot
    from bowhead_core.tool_registry import ToolExecutionContext
    from bowhead_core.types import ToolInvocation, TurnEventBus

logger = logging.getLogger(__name__)


def worker_identity(ctx: ToolExecutionContext) -> DocumentTurnOwner:
    from bowhead_core.document_chat_continuation import DocumentTurnOwner

    return DocumentTurnOwner(
        team_id=ctx.team_id,
        user_id=ctx.user_id,
        workspace_id=ctx.workspace_id,
        session_id=ctx.session_id,
        turn_id=ctx.turn_id,
        generation=ctx.execution_generation,
        owner_id=ctx.execution_owner_id,
    )


def _ordinary_lead_may_engage(ctx: ToolExecutionContext) -> bool:
    """Static admission for the optional continuation; anything else runs as plain ordinary Lead."""
    return bool(
        ctx.execution_generation == 1
        and ctx.execution_owner_id
        and ctx.session_store is not None
        # A teamless dev session has no Team scope to retain a worker under.
        and ctx.team_id
        and not any(
            getattr(ctx, field, None)
            for field in (
                "_reference_runtime",
                "terminal_execution_context",
                "plan_execution_id",
                "configuration_settings_target",
                "coach_canvas_target",
                "document_revision_target",
                "_bound_workflow",
            )
        )
    )


async def initialize_ordinary_lead(ctx: ToolExecutionContext, *, request_context: dict) -> None:
    """Engage the optional ordinary continuation only for a live, fully scoped worker.

    The continuation is an optional capability of ordinary Lead. When its
    preconditions do not hold (no Team, no current membership, or no live
    worker lease), it stays off and the Turn proceeds without it; it never
    fails the Turn on its own.
    """
    if getattr(ctx, "ordinary_lead_state", None) is not None or not _ordinary_lead_may_engage(ctx):
        return
    async with open_shared_ctx(ctx.session_store.db_path) as db:
        await db.execute("BEGIN")
        try:
            await require_live_worker_in_tx(db, worker=worker_identity(ctx))
        except OrdinaryContinuationUnavailable:
            logger.info("ordinary_lead.continuation_not_engaged turn=%s reason=worker_scope_unavailable", ctx.turn_id)
            return
        async with db.execute("SELECT llm_config_version FROM teams WHERE id=?", (ctx.team_id,)) as cursor:
            row = await cursor.fetchone()
    if row is None:
        logger.info("ordinary_lead.continuation_not_engaged turn=%s reason=team_unavailable", ctx.turn_id)
        return
    ctx.ordinary_lead_state = OrdinaryLeadRunState(model_config_version=row[0], request_context=request_context)


async def _source(ctx: ToolExecutionContext, db: aiosqlite.Connection) -> ChatTurnUserSource:
    from bowhead_core.session_store import _read_chat_turn_user_source_in_tx

    source = await _read_chat_turn_user_source_in_tx(db, turn_id=ctx.turn_id, expected_user_id=ctx.user_id, expected_workspace_id=ctx.workspace_id)
    if source is None or source.session_id != ctx.session_id or source.input_document is not None or source.document_revision_target is not None:
        raise OrdinaryContinuationUnavailable()
    return source


async def _uploads(ctx: ToolExecutionContext, source: ChatTurnUserSource) -> None:
    """Retain the existing ready-upload fence, including exact metadata."""
    from bowhead_core.chat_reference_attachment_snapshot import supports_lead_attachment_refs

    if not source.attachment_ids:
        return
    if not supports_lead_attachment_refs(source.attachment_refs, source.attachment_ids):
        raise OrdinaryContinuationUnavailable()
    if any(ref.get("referenceKind", ref.get("reference_kind", "upload")) != "upload" for ref in source.attachment_refs):
        raise OrdinaryContinuationUnavailable()
    ready = await ctx.artifact_store.get_ready_uploads_for_chat(list(source.attachment_ids), user_id=ctx.user_id, workspace_id=ctx.workspace_id)
    for artifact_id, ref in zip(source.attachment_ids, source.attachment_refs, strict=True):
        art = ready.get(artifact_id)
        if art is None or (art.filename, art.mime, art.size) != (ref["filename"], ref["mime"], ref["size"]):
            raise OrdinaryContinuationUnavailable()


async def _helper(ctx: ToolExecutionContext, db: aiosqlite.Connection, material: dict) -> str:
    from bowhead_core.agent_execution_binding import AgentExecutionBindingError, AgentExecutionBindingStore
    from bowhead_core.chat_reference_scope import ReferenceRequestScope

    try:
        store = AgentExecutionBindingStore(ctx.session_store.db_path)
        reader = store.read_completed_helper_in_connection
        outcome_args = {}
        if "outcome" in material:
            outcome = OrdinaryHelperOutcome.read(material["outcome"])
            outcome.require_result(outcome.status == "completed")
            reader = store.read_settled_helper_in_connection
            outcome_args = {
                "status": outcome.status,
                "failure_code": outcome.failure_code,
                "system_profile_resolver": getattr(getattr(ctx, "subagent_runner", None), "_resolve_system_profile", None),
            }
        binding = await reader(
            db,
            scope=ReferenceRequestScope(team_id=ctx.team_id, user_id=ctx.user_id, workspace_id=ctx.workspace_id, session_id=ctx.session_id),
            turn_id=ctx.turn_id,
            execution_id=material["execution_id"],
            dispatch_key=material["dispatch_key"],
            **outcome_args,
        )
    except AgentExecutionBindingError as exc:
        raise OrdinaryContinuationUnavailable() from exc
    projection = asdict(binding)
    projection["behavior_descriptor"] = binding.behavior_descriptor.model_dump(mode="json") if binding.behavior_descriptor is not None else None
    digest = content_hash(projection)
    if material.get("binding_hash", digest) != digest:
        raise OrdinaryContinuationUnavailable()
    return digest


def note_ordinary_return_rejection(ctx: ToolExecutionContext, result: dict) -> dict:
    """Observe native validation or the pre-handler document inspection guard.

    The model-visible error is not proof. Bind the trusted code location's
    observation to this call's exact child identity and unchanged result.
    """
    state = getattr(ctx, "ordinary_lead_state", None)
    identity = getattr(ctx, "ordinary_call_identity", None)
    if state is not None and state.checkpoint is None and identity is not None:
        state.rejected_returns[identity] = content_hash(result)
    return result


def _require_settled_returns(helper: ToolInvocation, state: OrdinaryLeadRunState) -> None:
    seen: set[str] = set()
    for index, child in enumerate(helper.nested):
        if not child.call_id or child.call_id in seen or not child.ended_at or child.nested:
            raise OrdinaryContinuationUnavailable()
        seen.add(child.call_id)
        if child.tool_name != "subagent_return":
            continue  # Native material evidence is checked below for every call.
        if child.error or not isinstance(child.result, dict):
            raise OrdinaryContinuationUnavailable()
        if child.result.get("ok") is True:
            if index != len(helper.nested) - 1:
                raise OrdinaryContinuationUnavailable()
        elif child.result.get("ok") is not False or state.rejected_returns.get((helper.result["subagent_id"], child.call_id)) != content_hash(
            child.result
        ):
            raise OrdinaryContinuationUnavailable()


async def capture_ordinary_lead_loop(
    *,
    ctx: ToolExecutionContext,
    bus: TurnEventBus,
    messages: list[Message],
    invocations: list[ToolInvocation],
    subagent_id: str,
    system: str,
    turn_tail: str,
    language_tail: str,
    attachment_context: str | None,
    schemas: list[ToolSchema],
    next_iteration: int,
    max_iterations: int,
    consumed_tokens: int,
    history_sizes: list[int],
    source_bounce_used: bool,
    overflow_retried: bool,
    deadline_retried: bool,
    reference_allowlist: RefAllowlist | None = None,
) -> OrdinaryLeadCheckpoint | None:
    state = getattr(ctx, "ordinary_lead_state", None)
    # Effect-free questions still use the existing bounded contract. An
    # unsupported post-helper pause must not pretend it can restart safely.
    if not any(inv.tool_name == "subagent_spawn" for inv in invocations):
        return None
    if state is None or state.checkpoint is not None:
        raise OrdinaryContinuationUnavailable()
    current_messages = current_turn_messages(messages, history_sizes)
    collaboration = getattr(ctx, "collaboration_execution", None)
    if collaboration is not None:
        # Coordination receipts live in the exchange ledger and retained user
        # evidence. They are not native calls in the original Lead transcript.
        native_ids = {call.id for message in current_messages for call in message.tool_calls or []}
        invocations = [inv for inv in invocations if inv.tool_name != "agent_message" or inv.call_id in native_ids]
    prefix, question = invocations[:-1], invocations[-1]
    from bowhead_core.documents.turn_policy import empty_lookup_before_question

    helpers = prefix
    if prefix and empty_lookup_before_question([prefix[0], question], getattr(ctx, "document_turn_state", None)):
        helpers = prefix[1:]
    helpers = [
        inv
        for inv in helpers
        if state.blocked_calls.get(inv.call_id) != content_hash({"tool": inv.tool_name, "arguments": inv.arguments, "result": inv.result})
    ]
    if collaboration is not None:
        helpers = [inv for inv in helpers if inv.tool_name != "agent_message"]
    # Lead's own settled calls stay in its transcript and are never rerun.
    lead_calls = [inv for inv in helpers if retained_lead_call(inv)]
    helpers = [inv for inv in helpers if not retained_lead_call(inv)]
    if any(helper.tool_name != "subagent_spawn" for helper in helpers):
        raise OrdinaryQuestionUnavailable(
            "No question was created because the current Tool sequence cannot be retained for a durable pause. "
            "Continue with the existing results or explain which user input is still needed."
        )
    if helpers and len(state.helper_bindings) == len(helpers) and any(not binding for binding in state.helper_bindings):
        # A dispatch without a retainable binding (for example a primary that
        # delivered the whole answer) cannot resume, but the settled results
        # stay valid. Ask in prose instead of failing the Turn.
        raise OrdinaryQuestionUnavailable(
            "No question was created because a dispatched Agent result cannot be retained for a durable pause. "
            "Continue with the existing results, or ask the user in your answer which input is still needed."
        )
    if (
        not helpers
        or len(state.helper_bindings) != len(helpers)
        or question.tool_name != "clarification_ask"
        or question.error
        or (len(helpers) > 1 and not ordinary_multi_helper_question_enabled())
    ):
        raise OrdinaryContinuationUnavailable()
    parallel = any(len(message.tool_calls or []) > 1 for message in current_messages)
    if any(call.name != "subagent_spawn" for message in current_messages if len(message.tool_calls or []) > 1 for call in message.tool_calls):
        raise OrdinaryQuestionUnavailable(
            "No question was created because the current Tool sequence cannot be retained for a durable pause. "
            "Continue with the existing results or explain which user input is still needed."
        )
    native_spawns = [
        call
        for message in current_messages
        for call in message.tool_calls or []
        if call.name == "subagent_spawn" and call.id not in state.blocked_calls
    ]
    bindings = state.helper_bindings
    if parallel:
        # Execution finishes independently; model messages retain dispatch order.
        # Only original native IDs can join these two existing projections.
        by_dispatch = {binding.get("dispatch_key"): binding for binding in bindings}
        dispatch_keys = ["call:" + call.id for call in native_spawns]
        if len(by_dispatch) != len(bindings) or len(set(dispatch_keys)) != len(dispatch_keys) or set(by_dispatch) != set(dispatch_keys):
            raise OrdinaryContinuationUnavailable()
        bindings = [by_dispatch[key] for key in dispatch_keys]
    from bowhead_core.ordinary_lead_material import retained_read_tool, retained_tool_materials

    # Read-only observations use the same extended envelope as attachment reads.
    has_attachment_reads = bool(lead_calls) or any(
        child.tool_name == "attachment_read" or retained_read_tool(child.tool_name) for helper in helpers for child in helper.nested
    )
    extended_outcomes = has_attachment_reads or any(
        binding.get("source_kind") == "system" or helper.result.get("ok") is False
        for helper, binding in zip(helpers, bindings, strict=True)
        if isinstance(helper.result, dict)
    )
    if extended_outcomes and not ordinary_multi_helper_question_enabled():
        raise OrdinaryContinuationUnavailable()
    async with open_shared_ctx(ctx.session_store.db_path) as db:
        source = await _source(ctx, db)
    parsed = list(getattr(ctx, "_psi_parsed_results", []) or [])
    pinned = getattr(ctx, "_psi_pinned_snapshot", []) or []
    input_context = {"parsed": [{**asdict(item), "local_path": None} for item in parsed], "pinned": pinned}
    from bowhead_core.ordinary_lead_input_context import retained_ids

    has_retained_context = bool(retained_ids(source, input_context))
    if has_retained_context and not ordinary_multi_helper_question_enabled():
        raise OrdinaryContinuationUnavailable()
    shared_outcomes = collaboration is not None or extended_outcomes or has_retained_context or parallel
    helper_materials = []
    for helper, binding in zip(helpers, bindings, strict=True):
        if (
            helper.error
            or not helper.ended_at
            or not isinstance(helper.result, dict)
            or (not shared_outcomes and helper.result.get("ok") is not True)
            or not helper.result.get("subagent_id")
        ):
            raise OrdinaryContinuationUnavailable()
        _require_settled_returns(helper, state)
        entry = {key: binding[key] for key in ("execution_id", "dispatch_key", "subagent_id")}
        if shared_outcomes:
            outcome = OrdinaryHelperOutcome.read(binding.get("outcome"))
            outcome.require_result(helper.result.get("ok"))
            entry["outcome"] = outcome.model_dump()
        helper_materials.append({**entry, "tools": retained_tool_materials(helper, state, collaboration=collaboration is not None)})
    # No undocumented pinned input or prior-turn extraction may be silently
    # carried as the original message's authorized attachment.
    async with open_shared_ctx(ctx.session_store.db_path) as db:
        await db.execute("BEGIN")
        await require_live_worker_in_tx(db, worker=worker_identity(ctx))
        source = await _source(ctx, db)
        if len(native_spawns) != len(helpers):
            raise OrdinaryContinuationUnavailable()
        for call, helper, entry in zip(native_spawns, helpers, helper_materials, strict=True):
            if (
                "call:" + call.id != entry["dispatch_key"]
                or entry["subagent_id"] != helper.result.get("subagent_id")
                or call.arguments != helper.arguments
            ):
                raise OrdinaryContinuationUnavailable()
            entry["binding_hash"] = await _helper(ctx, db, entry)
    if len(helpers) > 1 or shared_outcomes:
        from bowhead_core.subagent import _serialize

        # Compare the actual native model projection while its writer is live.
        # Readers retain these immutable bytes, not a newly projected result.
        results = [message for message in current_messages[:-1] if message.role == "tool"]
        calls = [call for message in current_messages[:-1] for call in message.tool_calls or []][:-1]
        if (
            len(results) != len(prefix)
            or len(calls) != len(prefix)
            or any(
                message.tool_call_id != call.id or message.content != _serialize(inv)
                for message, inv, call in zip(results, prefix, calls, strict=True)
            )
        ):
            raise OrdinaryContinuationUnavailable()
    material = {"helpers": helper_materials} if len(helpers) > 1 or shared_outcomes else helper_materials[0]
    await _uploads(ctx, source)
    from bowhead_core.ordinary_lead_material import revalidate_tool_materials

    for entry in helper_materials:
        await revalidate_tool_materials(ctx, entry["tools"])
    if parallel:
        material["input_context"] = None
    if has_retained_context:
        from bowhead_core.ordinary_lead_input_context import capture_context

        async with open_shared_ctx(ctx.session_store.db_path) as db:
            await db.execute("BEGIN")
            material["input_context"] = await capture_context(ctx, db, source, input_context)
    from bowhead_core.chat_reference_attachment_snapshot import seal_source_attachment_snapshot
    from bowhead_core.language_context import language_context_snapshot

    material["attachments"] = await seal_source_attachment_snapshot(source, ctx, attachment_context or "") if source.attachment_ids else None
    from bowhead_core.ordinary_lead_material import canvas_evidence

    material["canvas"] = await canvas_evidence(ctx)
    context = {
        "parsed": [{**asdict(item), "local_path": None} for item in parsed],
        "payloads": getattr(ctx, "_m137_subagent_payloads", []) or [],
        "spawn_order": getattr(bus, "_spawn_order", []) or [],
        "spawn_count": getattr(ctx, "_chat_direct_spawn_count", 0),
        # Retained for checkpoint compatibility; no per-turn cost accumulator exists.
        "cost_usd": 0.0,
        "pinned": pinned,
        "pinned_total": getattr(ctx, "_psi_pinned_total", 0),
    }
    if has_retained_context or parallel:
        context["reference_inventory"] = reference_allowlist.snapshot() if reference_allowlist is not None else None
    if (len(helpers) > 1 or shared_outcomes) and context["spawn_count"] != len(helpers) + sum(
        inv.tool_name == "subagent_spawn" and inv.call_id in state.blocked_calls for inv in prefix
    ):
        raise OrdinaryContinuationUnavailable()
    remaining_wallclock_s = ctx._ordinary_lead_deadline - time.monotonic()
    if remaining_wallclock_s <= 0:
        raise OrdinaryContinuationUnavailable()
    checkpoint = OrdinaryLeadCheckpoint(
        collaboration=await collaboration.runtime.checkpoint(collaboration) if collaboration is not None else None,
        contract="ordinary-lead-loop.v11"
        if parallel
        else "ordinary-lead-loop.v10"
        if has_retained_context
        else "ordinary-lead-loop.v9"
        if collaboration is not None
        else "ordinary-lead-loop.v8"
        if has_attachment_reads
        else "ordinary-lead-loop.v7"
        if any(entry.get("outcome", {}).get("reason") in {"model_failure", "model_unknown", "tool_unknown"} for entry in helper_materials)
        else "ordinary-lead-loop.v6"
        if shared_outcomes
        else "ordinary-lead-loop.v5"
        if len(helpers) > 1
        else "ordinary-lead-loop.v3",
        subagent_id=subagent_id,
        system=system,
        turn_tail=turn_tail,
        language_tail=language_tail,
        attachment_context=attachment_context,
        max_iterations=max_iterations,
        next_iteration=next_iteration,
        tool_schema_hash=content_hash([asdict(schema) for schema in schemas]),
        preferred_provider=getattr(ctx, "preferred_provider", None),
        preferred_model=getattr(ctx, "preferred_model", None),
        model_config_version=state.model_config_version,
        language_json=canonical_json(language_context_snapshot(ctx)),
        messages_json=capture_message_prefix(messages, question_call_id=question.call_id),
        invocations_json=canonical_json([asdict(inv) for inv in prefix]),
        context_json=canonical_json(context),
        request_json=canonical_json(state.request_context),
        question_call_id=question.call_id,
        question_arguments_json=canonical_json(question.arguments),
        consumed_tokens=consumed_tokens,
        remaining_wallclock_s=remaining_wallclock_s,
        # Retained for checkpoint compatibility; resume uses remaining_wallclock_s.
        remaining_action_deadline_s=remaining_wallclock_s,
        final_bounce_count=state.final_bounce_count["n"],
        source_bounce_used=source_bounce_used,
        overflow_retried=overflow_retried,
        deadline_retried=deadline_retried,
        history_sizes=history_sizes,
        material_json=canonical_json(material),
    )
    try:
        checkpoint.encode()
    except OrdinaryContinuationUnavailable as exc:
        failure = exc.__traceback__
        while failure is not None and failure.tb_next is not None:
            failure = failure.tb_next
        logger.info(
            "ordinary_lead.question_capture_unavailable turn=%s next_iteration=%d max_iterations=%d failure_site=%s.%s:%d",
            ctx.turn_id,
            checkpoint.next_iteration,
            checkpoint.max_iterations,
            failure.tb_frame.f_globals.get("__name__", "unknown") if failure is not None else "unknown",
            failure.tb_frame.f_code.co_name if failure is not None else "unknown",
            failure.tb_lineno if failure is not None else 0,
        )
        # Resource and worker checks above remain authoritative. An existing
        # envelope's representation limits do not invalidate live execution.
        raise OrdinaryQuestionUnavailable(
            "No question was created because the current execution history cannot be saved in a supported pause format. "
            "Continue with the existing results or explain which user input is still needed."
        ) from exc
    return checkpoint


def _validate_document_routing_source(routing: DocumentRoutingSnapshot, source: ChatTurnUserSource) -> None:
    # Historical card provenance remains authoritative, not its retired routing policy.
    try:
        routing.verify_source(source)
    except ValueError as error:
        raise OrdinaryContinuationUnavailable() from error


async def _validate_input_context(
    ctx: ToolExecutionContext, db: aiosqlite.Connection, source: ChatTurnUserSource, checkpoint: OrdinaryLeadCheckpoint, *, read_bytes: bool
) -> None:
    from bowhead_core.ordinary_lead_input_context import retained_ids, validate_context

    material = json.loads(checkpoint.material_json)
    context = json.loads(checkpoint.context_json)
    if checkpoint.contract == "ordinary-lead-loop.v11" and material["input_context"] is None:
        if retained_ids(source, context):
            raise OrdinaryContinuationUnavailable()
    else:
        await validate_context(ctx, db, source, material["input_context"], context, read_bytes=read_bytes)


async def validate_ordinary_retained_materials(ctx: ToolExecutionContext, checkpoint: OrdinaryLeadCheckpoint) -> None:
    """Read-only admission preflight, also repeated by the actual worker."""
    from bowhead_core.agent_collaboration_runtime import validate_collaboration_checkpoint

    await validate_collaboration_checkpoint(ctx, checkpoint)
    material = json.loads(checkpoint.material_json)
    async with open_shared_ctx(ctx.session_store.db_path) as db:
        await db.execute("BEGIN")
        source = await _source(ctx, db)
        if checkpoint.document_routing is not None:
            _validate_document_routing_source(checkpoint.document_routing, source)
        for entry in checkpoint.helper_materials():
            await _helper(ctx, db, entry)
        async with db.execute("SELECT llm_config_version FROM teams WHERE id=?", (ctx.team_id,)) as cursor:
            row = await cursor.fetchone()
            if row is None or row[0] != checkpoint.model_config_version:
                raise OrdinaryContinuationUnavailable()
    if checkpoint.contract in {"ordinary-lead-loop.v10", "ordinary-lead-loop.v11"}:
        async with open_shared_ctx(ctx.session_store.db_path) as db:
            await db.execute("BEGIN")
            await _validate_input_context(ctx, db, source, checkpoint, read_bytes=True)
    await _uploads(ctx, source)
    from bowhead_core.ordinary_lead_material import revalidate_tool_materials

    for entry in checkpoint.helper_materials():
        await revalidate_tool_materials(ctx, entry.get("tools", []), contract=checkpoint.contract)
    from bowhead_core.ordinary_lead_material import canvas_evidence

    if "canvas" in material and await canvas_evidence(ctx, contract=checkpoint.contract) != material["canvas"]:
        raise OrdinaryContinuationUnavailable()
    if source.attachment_ids:
        from bowhead_core.chat_reference_attachment_snapshot import reopen_source_attachment_snapshot

        await reopen_source_attachment_snapshot(source, ctx, material["attachments"])


async def restore_ordinary_lead(ctx: ToolExecutionContext, bus: TurnEventBus) -> None:
    state = getattr(ctx, "ordinary_lead_state", None)
    if state is None or state.checkpoint is None:
        return
    if state.consumed or not state.card_id:
        raise OrdinaryContinuationUnavailable()
    checkpoint = state.checkpoint
    from bowhead_core.language_context import language_context_snapshot

    if (
        canonical_json(language_context_snapshot(ctx)) != checkpoint.language_json
        or getattr(ctx, "preferred_provider", None) != checkpoint.preferred_provider
        or getattr(ctx, "preferred_model", None) != checkpoint.preferred_model
    ):
        raise OrdinaryContinuationUnavailable()
    material = json.loads(checkpoint.material_json)
    async with open_shared_ctx(ctx.session_store.db_path) as db:
        await db.execute("BEGIN")
        record = await read_ordinary_card_in_tx(db, card_id=state.card_id)
        if reopen_ordinary_checkpoint(record) != checkpoint:
            raise OrdinaryContinuationUnavailable()
        await require_live_worker_in_tx(db, worker=worker_identity(ctx))
    await validate_ordinary_retained_materials(ctx, checkpoint)
    from bowhead_core.attachments_injector import PreparedAttachment

    context = json.loads(checkpoint.context_json)
    # Validate/construct data before the irreversible consumption marker.
    parsed = [PreparedAttachment(**item) for item in context["parsed"]]
    from bowhead_core.ordinary_lead_material import revalidate_material_authority_in_connection

    async with open_shared_ctx(ctx.session_store.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        from bowhead_core.agent_collaboration_runtime import validate_collaboration_checkpoint

        await validate_collaboration_checkpoint(ctx, checkpoint, db=db)
        for entry in checkpoint.helper_materials():
            await _helper(ctx, db, entry)
            await revalidate_material_authority_in_connection(ctx, db, entry, contract=checkpoint.contract)
        source = await _source(ctx, db)
        await _uploads(ctx, source)
        if checkpoint.contract in {"ordinary-lead-loop.v10", "ordinary-lead-loop.v11"}:
            await _validate_input_context(ctx, db, source, checkpoint, read_bytes=False)
        if checkpoint.contract in {
            "ordinary-lead-loop.v5",
            "ordinary-lead-loop.v6",
            "ordinary-lead-loop.v7",
            "ordinary-lead-loop.v8",
            "ordinary-lead-loop.v9",
            "ordinary-lead-loop.v10",
            "ordinary-lead-loop.v11",
        }:
            await revalidate_material_authority_in_connection(ctx, db, material, contract=checkpoint.contract)
        record = await consume_ordinary_resume_in_tx(db, worker=worker_identity(ctx), card_id=state.card_id)
        state.answers = typed_answer(record).answers
        await db.commit()
    state.consumed = True
    state.final_bounce_count["n"] = checkpoint.final_bounce_count
    ctx._psi_parsed_results = parsed
    ctx._psi_pinned_snapshot = context["pinned"]
    ctx._psi_pinned_total = context["pinned_total"]
    ctx._m137_subagent_payloads = context["payloads"]
    ctx._chat_direct_spawn_count = context["spawn_count"]
    bus._spawn_order = context["spawn_order"]
