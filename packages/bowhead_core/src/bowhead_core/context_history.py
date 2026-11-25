"""Read a committed Lead view and its ordered canonical conversation suffix."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass

from bowhead_router.reasoning import message_record
from bowhead_router.router import UserModelPref
from bowhead_router.types import Message, ToolCall

from bowhead_core._db import open_pooled_read_ctx, open_shared_ctx
from bowhead_core.chat_reference_scope import ReferenceRequestScope
from bowhead_core.chat_reference_storage import ReferenceCipher
from bowhead_core.context_assembly import INCOMPLETE_TURN_MARKERS, build_turn_history, has_incomplete_turn_marker
from bowhead_core.context_dependencies import ContextSourceValidator, validate_context_dependencies
from bowhead_core.context_ownership import TerminalContextOwner, context_owner, require_context_owner_in_tx
from bowhead_core.context_segments import MAX_CONTEXT_BYTES
from bowhead_core.context_store import ContextStoreError, read_context_graph_in_tx
from bowhead_core.cross_turn_digest import _strip_markers
from bowhead_core.knowledge_source_runtime import validate_source_messages


@dataclass(frozen=True, slots=True)
class ConversationContext:
    generation_id: str | None
    through_seq: int
    messages: list[Message]
    incomplete_turns: int
    # A published head this Turn replaces without inheriting, because a source
    # it depends on is deleted, changed or no longer authorized.
    replaced_generation_id: str | None = None


# Context keys of a Turn whose running work is bound to something the user selected or
# approved (an Agent, a Plan, a settings or document target). A Turn with none of them
# is an ordinary Lead Turn.
BOUND_TURN_CONTEXT_KEYS = (
    "_reference_runtime",
    "_agent_binding",
    "plan_execution_id",
    "configuration_settings_target",
    "coach_canvas_target",
    "document_revision_target",
    "_bound_workflow",
)


def is_ordinary_lead_context(ctx) -> bool:
    return not any(getattr(ctx, key, None) for key in BOUND_TURN_CONTEXT_KEYS)


def context_history_available(ctx, router) -> bool:
    return bool(
        getattr(ctx, "session_store", None) is not None
        and is_ordinary_lead_context(ctx)
        and context_owner(ctx) is not None
        and getattr(ctx, "agent_runtime_budget", None) is not None
        and callable(getattr(ctx.session_store, "list_context_history_page", None))
        and callable(getattr(router, "supports_context_maintenance", None))
        and router.supports_context_maintenance("lead", "lead", UserModelPref.from_request_fields(ctx.preferred_provider, ctx.preferred_model))
        is True
    )


async def prepare_context_lead(ctx, *, runner, bus, task):
    """Reuse original Agent admission without enabling a different collaboration protocol."""
    owner = context_owner(ctx)
    if not isinstance(owner, TerminalContextOwner):
        from bowhead_core.agent_collaboration_runtime import prepare_lead_collaboration

        return await prepare_lead_collaboration(ctx, runner=runner, bus=bus, task=task)
    retained = getattr(ctx, "_model_context_lead_binding", None)
    if retained is not None:
        return retained
    async with open_shared_ctx(ctx.session_store.db_path) as db:
        await db.execute("BEGIN")
        await require_context_owner_in_tx(db, worker=owner)
    binding = await runner.admit_execution_binding(
        profile_name="lead",
        parent_turn_id=ctx.turn_id,
        parent_ctx=ctx,
        dispatch_key=f"context-lead:{ctx.turn_id}:{owner.generation}",
    )
    if binding is not None:
        ctx._model_context_lead_binding = binding
    return binding


async def run_context_lead(ctx, binding, operation):
    """Settle only the exact root binding admitted for this Terminal execution."""
    if binding is None or binding is not getattr(ctx, "_model_context_lead_binding", None):
        return await operation
    from bowhead_core.agent_execution_binding import AgentExecutionBindingStore

    store = AgentExecutionBindingStore(ctx.session_store.db_path)
    try:
        await store.mark_running(binding.execution_id)
    except BaseException:
        operation.close()
        raise
    status = "failed"
    try:
        result = await operation
        status = "completed" if result.ok else "failed"
        return result
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    finally:
        try:
            await store.mark_terminal(binding.execution_id, status=status, failure_code="agent_execution_failed" if status == "failed" else None)
        except Exception:
            logging.getLogger(__name__).exception("context.lead_execution_settlement_failed execution=%s", binding.execution_id)


def _snapshot_retained_proofs(ctx) -> list[tuple[dict, dict]]:
    """Copy every map that history validation can add retained source proofs to."""
    maps = [
        getattr(ctx, "knowledge_source_uses", None),
        getattr(ctx, "knowledge_navigation_uses", None),
        *(getattr(ctx, "knowledge_source_ancestors", None) or ()),
        *(getattr(ctx, "knowledge_navigation_ancestors", None) or ()),
        getattr(getattr(ctx, "ordinary_lead_state", None), "knowledge_sources", None),
    ]
    return [(values, dict(values)) for values in maps if isinstance(values, dict)]


def _restore_retained_proofs(snapshot: list[tuple[dict, dict]]) -> None:
    # In place: ancestors and helper contexts hold references to the same maps.
    for values, saved in snapshot:
        values.clear()
        values.update(saved)


async def load_conversation_context(ctx) -> ConversationContext:
    """Use the original worker and current source readers; no Agent is admitted here."""
    worker = context_owner(ctx)
    if worker is None:
        raise ContextStoreError("context_owner_unavailable")
    store = ctx.session_store
    messages: list[Message] = []
    generation = None
    replaced = None
    through = 0
    source_validator = (
        ContextSourceValidator(ctx) if callable(getattr(getattr(ctx, "kb_file_authorizer", None), "history_validation", None)) else None
    )

    # Every generation of the chain is checked inside this one read. Exact
    # material records and stored file bytes repeat across generations, so they
    # are verified once per load; nothing here outlives the load.
    validated_materials: set[str] = set()

    async def authorize(source):
        if source_validator is not None:
            await source_validator.validate(source, read_bytes=True, validated_materials=validated_materials, connection=db)
            return
        await validate_source_messages(ctx, source)
        await validate_context_dependencies(ctx, source, validated_materials=validated_materials)

    async with open_pooled_read_ctx(store.db_path) as db:
        await db.execute("BEGIN")
        await require_context_owner_in_tx(db, worker=worker)
        async with db.execute("SELECT seq FROM turns WHERE id=? AND session_id=?", (worker.turn_id, worker.session_id)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise ContextStoreError("context_current_turn_unavailable")
        before = row[0]
        async with db.execute(
            "SELECT generation_id,published_turn_seq FROM model_context_conversation_heads WHERE session_id=?", (worker.session_id,)
        ) as cursor:
            head = await cursor.fetchone()
        if head is not None:
            generation, through = head
            if through >= before:
                raise ContextStoreError("context_history_cursor_changed")
            from bowhead_core.agent_knowledge_policy import KnowledgeAccessError
            from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable

            retained_before = _snapshot_retained_proofs(ctx)
            try:
                graph = await read_context_graph_in_tx(
                    db,
                    worker=worker,
                    execution_id=None,
                    lane="ordinary",
                    generation_id=generation,
                    cipher=ReferenceCipher(os.environ.get("BOWHEAD_FERNET_KEY", "")),
                    validate_sources=authorize,
                    projection_only=True,
                )
            except (OrdinaryContinuationUnavailable, KnowledgeAccessError) as exc:
                # An authorization outage or malformed provenance proves nothing about
                # the source; it fails this Turn and leaves the head for a retry.
                if isinstance(exc, KnowledgeAccessError) and exc.code != "knowledge_source_changed":
                    raise
                # A source an earlier Turn used is deleted, changed or no longer
                # authorized. Its content must not reach the model, but the
                # conversation continues: rebuild history from Turn summaries and
                # let this Turn's publication replace the head without inheriting it.
                # Proofs that earlier generations of the discarded head recorded on
                # this Turn's context are dropped with it.
                from bowhead_core.ordinary_lead_runtime import capture_failure_site

                _restore_retained_proofs(retained_before)
                logging.getLogger(__name__).info(
                    "context_history.head_unavailable session=%s turn=%s condition=%s failure_site=%s",
                    worker.session_id,
                    worker.turn_id,
                    exc.code if isinstance(exc, KnowledgeAccessError) else exc.condition,
                    capture_failure_site(exc),
                )
                replaced, generation, through = generation, None, 0
            else:
                messages.extend(graph[generation].messages)
        await db.rollback()
    incomplete = sum("[clarification_pending:" in message.content or has_incomplete_turn_marker(message.content) for message in messages)
    byte_count = len(json.dumps([message_record(message) for message in messages], ensure_ascii=False).encode())
    while through < before - 1:
        turns = await store.list_context_history_page(session_id=worker.session_id, after_seq=through, before_seq=before)
        if not turns:
            break
        spawns = await store.list_turn_spawns(
            [turn.id for turn in turns], incomplete_status={turn.id: turn.status for turn in turns if turn.status in INCOMPLETE_TURN_MARKERS}
        )
        clarifications = {}
        if any(turn.status == "clarification_pending" for turn in turns):
            clarifications = await store.load_clarification_history(
                ReferenceRequestScope(team_id=worker.team_id, user_id=worker.user_id, workspace_id=worker.workspace_id, session_id=worker.session_id),
                worker.turn_id,
                turn_ids=tuple(turn.id for turn in turns),
            )
        from bowhead_core.result_material_history import load_material_history

        materials = await load_material_history(ctx, [turn.id for turn in turns])
        applied = getattr(store, "list_turn_applied_instructions", None)
        replies = getattr(store, "list_unfinished_replies", None)
        unfinished = [turn.id for turn in turns if turn.status in INCOMPLETE_TURN_MARKERS]
        history = build_turn_history(
            turns,
            exclude_turn_id=worker.turn_id,
            instructions=await applied([turn.id for turn in turns]) if callable(applied) else None,
            unfinished_replies=await replies(unfinished) if callable(replies) and unfinished else None,
            budget_tokens=0,
            make_message=lambda role, content: Message(role=role, content=content),
            strip=_strip_markers,
            spawns=spawns,
            clarifications=clarifications,
            material_refs=materials,
            make_tool_calls_message=lambda calls: Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id=identity, name=name, arguments=arguments) for identity, name, arguments in calls],
            ),
            make_tool_result_message=lambda identity, name, content: Message(role="tool", content=content, tool_call_id=identity, name=name),
        )
        messages.extend(history.messages)
        incomplete += history.incomplete_turns
        byte_count += len(json.dumps([message_record(message) for message in history.messages], ensure_ascii=False).encode())
        if byte_count > MAX_CONTEXT_BYTES:
            raise ContextStoreError("context_history_storage_limit")
        through = turns[-1].seq
    return ConversationContext(generation, through, messages, incomplete, replaced)
