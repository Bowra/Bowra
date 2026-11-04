"""Context preparation releases the writer while final adoption retains authority."""

import asyncio
import sqlite3
import time
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_knowledge_policy import KnowledgeAccessError
from bowhead_core.context_dependencies import ContextSourceValidator
from bowhead_core.context_execution import ExecutionContextView
from bowhead_core.context_store import ContextStoreError
from bowhead_core.events import OrchestratorEvent
from bowhead_core.session_store import SessionStore
from test_agent_collaboration import collab as collaboration_fixture
from test_context_execution import prepare
from test_context_history import completion
from test_context_maintenance import context_case as maintenance_fixture
from test_context_terminal import terminal_context_case

collab = collaboration_fixture
context_case = maintenance_fixture
pytestmark = pytest.mark.integration


def source_context():
    return SimpleNamespace(
        knowledge_scope=None,
        knowledge_source_uses={},
        knowledge_source_ancestors=(),
        ordinary_lead_state=None,
    )


def legacy_view(collab, *, lead=True, ctx=None):
    return ExecutionContextView(
        db_path=collab.path,
        worker=collab.worker,
        execution_id=collab.lead if lead else collab.requester,
        lane="ordinary",
        cipher=collab.store.cipher,
        validate_sources=ContextSourceValidator(ctx or source_context()),
        activity=AsyncMock(),
    )


def independent_write(path):
    with closing(sqlite3.connect(path, timeout=0.025)) as db:
        db.execute("INSERT INTO context_independent_writes DEFAULT VALUES")
        db.commit()


@pytest.mark.parametrize("entry", ["helper", "legacy", "reference"])
async def test_history_decoding_allows_independent_writes_during_compaction_and_publication(collab, context_case, tmp_path, monkeypatch, entry):
    from bowhead_core import context_store, knowledge_source_runtime

    if entry == "reference":
        runtime, view = await terminal_context_case(tmp_path, monkeypatch, context_case)
        view.validate_sources = ContextSourceValidator(source_context())
    else:
        runtime = None
        view = legacy_view(collab, lead=entry == "legacy")
    async with open_shared_ctx(view.db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("CREATE TABLE context_independent_writes(id INTEGER PRIMARY KEY)")
        await db.commit()
    decode = context_store.open_context_messages
    decoded = []

    def checked_decode(*args, **kwargs):
        independent_write(view.db_path)
        decoded.append(True)
        return decode(*args, **kwargs)

    monkeypatch.setattr(context_store, "open_context_messages", checked_decode)
    validate = knowledge_source_runtime.validate_source_messages

    async def checked_source_parsing(ctx, messages):
        if any(message.role == "tool" for message in messages):
            await asyncio.to_thread(independent_write, view.db_path)
        await validate(ctx, messages)

    monkeypatch.setattr(knowledge_source_runtime, "validate_source_messages", checked_source_parsing)
    if runtime is None:
        await prepare(view, context_case)
    if entry == "legacy":
        publication = await view.conversation_publication(context_case.request.messages)
        assert await SessionStore(view.db_path).mark_completed(**completion(collab, publication))
    elif entry == "reference":
        runtime.bus._context_publication = await view.conversation_publication(context_case.request.messages)
        await runtime.bus.emit(OrchestratorEvent("final_answer", "t_1", {"text": "Final verified answer."}))
        await runtime.bus.emit(OrchestratorEvent("done", "t_1", {"status": "completed", "tokens_input": 0, "tokens_output": 0}))
    assert decoded


async def test_prepared_publication_rechecks_current_source_authority_and_can_recover_without_model_replay(collab, context_case, monkeypatch):
    from bowhead_core import knowledge_source_runtime

    ctx = source_context()
    ctx.knowledge_source_uses = {"file_original": "generation_original"}
    allowed = True

    async def evidence(ctx, proof):
        if proof["sources"] and not allowed:
            raise KnowledgeAccessError("knowledge_source_changed")

    monkeypatch.setattr(knowledge_source_runtime, "require_evidence", evidence)
    view = legacy_view(collab, ctx=ctx)
    await prepare(view, context_case)
    publication = await view.conversation_publication(context_case.request.messages)
    ctx.knowledge_source_uses.clear()
    allowed = False
    with pytest.raises(KnowledgeAccessError, match="knowledge_source_changed"):
        await SessionStore(view.db_path).mark_completed(**completion(collab, publication))
    allowed = True
    assert await SessionStore(view.db_path).mark_completed(**completion(collab, publication))
    assert len(context_case.sent) == 1


async def test_changed_tool_provenance_uses_the_original_authority_reader(collab, context_case, monkeypatch):
    from bowhead_core import knowledge_source_runtime

    view = legacy_view(collab)
    await prepare(view, context_case)
    publication = await view.conversation_publication(context_case.request.messages)
    tool = next(message for message in publication.messages if message.role == "tool")
    tool.content = '{"knowledge_provenance":{"schema_version":1,"sources":{"file_revoked":"generation"}}}'

    async def evidence(ctx, proof):
        if "file_revoked" in proof["sources"]:
            raise KnowledgeAccessError("knowledge_source_changed")

    monkeypatch.setattr(knowledge_source_runtime, "require_evidence", evidence)
    with pytest.raises(KnowledgeAccessError, match="knowledge_source_changed"):
        await SessionStore(view.db_path).mark_completed(**completion(collab, publication))


async def test_slow_preparation_observes_renewed_owner_in_the_next_root_snapshot(collab, context_case, monkeypatch):
    from bowhead_core import context_store

    view = legacy_view(collab)
    await prepare(view, context_case)
    async with open_shared_ctx(view.db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        async with db.execute("SELECT parent_id FROM model_context_generations WHERE id=?", (view.generation,)) as cursor:
            view.inherited_generation = (await cursor.fetchone())[0]
        await db.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=strftime('%Y-%m-%dT%H:%M:%f+00:00','now','+1 seconds') WHERE turn_id=?",
            (collab.worker.turn_id,),
        )
        await db.commit()
    decode = context_store.open_context_messages
    renewed = False

    def slow_decode(*args, **kwargs):
        nonlocal renewed
        if not renewed:
            renewed = True
            with closing(sqlite3.connect(view.db_path)) as db:
                db.execute(
                    "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=strftime('%Y-%m-%dT%H:%M:%f+00:00','now','+600 seconds') WHERE turn_id=?",
                    (collab.worker.turn_id,),
                )
                db.commit()
            time.sleep(1.1)
        return decode(*args, **kwargs)

    monkeypatch.setattr(context_store, "open_context_messages", slow_decode)
    assert await view.conversation_publication(context_case.request.messages)
    assert renewed


async def test_prepared_publication_does_not_adopt_deleted_source_segments(collab, context_case):
    view = legacy_view(collab)
    await prepare(view, context_case)
    publication = await view.conversation_publication(context_case.request.messages)
    async with open_shared_ctx(view.db_path) as db:
        await db.execute("DELETE FROM model_context_segments")
        await db.commit()
    with pytest.raises(ContextStoreError, match="context_segment_unavailable"):
        await SessionStore(view.db_path).mark_completed(**completion(collab, publication))


async def test_cancellation_during_authority_recheck_releases_publication_writer(collab, context_case, monkeypatch):
    from bowhead_core import knowledge_source_runtime

    view = legacy_view(collab)
    await prepare(view, context_case)
    publication = await view.conversation_publication(context_case.request.messages)
    async with open_shared_ctx(view.db_path) as db:
        await db.execute("CREATE TABLE context_independent_writes(id INTEGER PRIMARY KEY)")
        await db.commit()
    entered = asyncio.Event()

    async def paused_authority(ctx, proof):
        try:
            await asyncio.to_thread(independent_write, view.db_path)
        except sqlite3.OperationalError as error:
            assert error.sqlite_errorcode == sqlite3.SQLITE_BUSY
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(knowledge_source_runtime, "require_evidence", paused_authority)
    task = asyncio.create_task(SessionStore(view.db_path).mark_completed(**completion(collab, publication)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.to_thread(independent_write, view.db_path)
    async with open_shared_ctx(view.db_path) as db:
        async with db.execute("SELECT status FROM turns WHERE id=?", (collab.worker.turn_id,)) as cursor:
            assert (await cursor.fetchone())[0] == "running"
        async with db.execute("SELECT COUNT(*) FROM model_context_conversation_heads") as cursor:
            assert (await cursor.fetchone())[0] == 0
    assert len(context_case.sent) == 1


@pytest.mark.parametrize("entry", ["legacy", "reference"])
async def test_changed_final_tool_provenance_is_prepared_without_blocking_other_writers(collab, context_case, tmp_path, monkeypatch, entry):
    from bowhead_core import knowledge_source_runtime

    if entry == "reference":
        runtime, view = await terminal_context_case(tmp_path, monkeypatch, context_case)
        view.validate_sources = ContextSourceValidator(source_context())
    else:
        runtime, view = None, legacy_view(collab)
        await prepare(view, context_case)
    publication = await view.conversation_publication(context_case.request.messages)
    async with open_shared_ctx(view.db_path) as db:
        await db.execute("CREATE TABLE context_independent_writes(id INTEGER PRIMARY KEY)")
        await db.commit()
    tool = next(message for message in publication.messages if message.role == "tool")
    tool.content = '{"knowledge_provenance":{"schema_version":1,"sources":{"file_current":"generation_current"}}}'
    parse = knowledge_source_runtime.validate_source_messages
    parsed = []

    async def parse_with_other_writer(ctx, messages):
        if any(message.role == "tool" for message in messages):
            await asyncio.to_thread(independent_write, view.db_path)
            parsed.append(True)
        await parse(ctx, messages)

    monkeypatch.setattr(knowledge_source_runtime, "validate_source_messages", parse_with_other_writer)
    monkeypatch.setattr(knowledge_source_runtime, "require_evidence", AsyncMock())
    if runtime is None:
        assert await SessionStore(view.db_path).mark_completed(**completion(collab, publication))
    else:
        runtime.bus._context_publication = publication
        await runtime.bus.emit(OrchestratorEvent("final_answer", "t_1", {"text": "Final verified answer."}))
        await runtime.bus.emit(OrchestratorEvent("done", "t_1", {"status": "completed", "tokens_input": 0, "tokens_output": 0}))
    assert parsed


@pytest.mark.parametrize("entry", ["legacy", "reference"])
async def test_reused_segments_are_decoded_before_terminal_writer_acquisition(collab, context_case, tmp_path, monkeypatch, entry):
    from bowhead_router.types import Message

    from bowhead_core import context_store

    if entry == "reference":
        runtime, view = await terminal_context_case(tmp_path, monkeypatch, context_case)
        view.validate_sources = ContextSourceValidator(source_context())
    else:
        runtime, view = None, legacy_view(collab)
        await prepare(view, context_case)
    publication = await view.conversation_publication(context_case.request.messages)
    final_answer = "Final verified answer."
    sealed = await context_store.prepare_context_messages(
        [*publication.messages, Message(role="assistant", content=final_answer)],
        worker=view.worker,
        execution_id=view.execution_id,
        lane="ordinary",
        cipher=view.cipher,
    )
    async with open_shared_ctx(view.db_path) as db:
        await db.execute("CREATE TABLE context_independent_writes(id INTEGER PRIMARY KEY)")
        for segment in sealed.segments:
            await db.execute(
                "INSERT OR IGNORE INTO model_context_segments(id,session_id,execution_id,lane,digest,byte_count,ciphertext,created_at) "
                "VALUES (?,?,?,'ordinary',?,?,?,?)",
                (
                    "mcs_" + uuid4().hex,
                    view.worker.session_id,
                    view.execution_id,
                    segment.reference.digest,
                    segment.reference.byte_count,
                    segment.ciphertext,
                    "2026-11-04T00:00:00Z",
                ),
            )
        await db.commit()
    decode = context_store.open_context_messages
    decoded = []

    def decode_with_other_writer(*args, **kwargs):
        independent_write(view.db_path)
        decoded.append(True)
        return decode(*args, **kwargs)

    monkeypatch.setattr(context_store, "open_context_messages", decode_with_other_writer)
    if runtime is None:
        assert await SessionStore(view.db_path).mark_completed(**{**completion(collab, publication), "final_answer": final_answer})
    else:
        runtime.bus._context_publication = publication
        await runtime.bus.emit(OrchestratorEvent("final_answer", "t_1", {"text": final_answer}))
        await runtime.bus.emit(OrchestratorEvent("done", "t_1", {"status": "completed", "tokens_input": 0, "tokens_output": 0}))
    assert decoded
