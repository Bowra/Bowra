"""Atomic successful-Turn publication and cursor-based conversation loading."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.context_history import load_conversation_context
from bowhead_core.context_store import ContextStoreError
from bowhead_core.session_store import SessionStore
from cryptography.fernet import Fernet
from test_agent_collaboration import collab as collaboration_fixture
from test_context_execution import owner, prepare
from test_context_maintenance import context_case as maintenance_fixture

collab = collaboration_fixture
context_case = maintenance_fixture
pytestmark = pytest.mark.integration


def completion(case, publication):
    return {
        "worker": case.worker,
        "turn_id": case.worker.turn_id,
        "intent": "chat",
        "final_answer": "Verified final answer.",
        "tokens_input": 10,
        "tokens_output": 4,
        "cost_usd": 0.01,
        "context_publication": publication,
    }


async def next_context(case, *, turn_id="t_context_next"):
    store = SessionStore(case.path)
    await store.record_turn(turn_id=turn_id, session_id=case.worker.session_id, user_text="Continue with the original constraints.")
    assert await store.claim_chat_spawn_dispatch_for_worker(turn_id=turn_id, owner_id="context-next", lease_seconds=600) == "claimed"
    return SimpleNamespace(
        session_store=store,
        execution_owner_id="context-next",
        execution_generation=1,
        turn_id=turn_id,
        team_id=case.worker.team_id,
        user_id=case.worker.user_id,
        workspace_id=case.worker.workspace_id,
        session_id=case.worker.session_id,
        knowledge_scope=None,
        knowledge_source_uses={},
        knowledge_source_ancestors=(),
        ordinary_lead_state=None,
    )


async def test_completed_turn_atomically_publishes_the_exact_compacted_native_view(collab, context_case, monkeypatch):
    from bowhead_core.chat_reference_storage import ReferenceCipher

    key = Fernet.generate_key().decode()
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", key)
    collab.store.cipher = ReferenceCipher(key)
    execution = owner(collab, execution_id=collab.lead)
    result = await prepare(execution, context_case)
    publish = await execution.conversation_publication(context_case.request.messages)
    store = SessionStore(collab.path)
    assert await store.mark_completed(**completion(collab, publish))
    assert not await store.mark_completed(**completion(collab, publish))
    await store.finalize_turn(session_id=collab.worker.session_id, current_turn_id=collab.worker.turn_id, next_turn_id=None)
    ctx = await next_context(collab)
    restored = await load_conversation_context(ctx)
    assert restored.messages[:-1] == result.request.messages
    assert restored.messages[-1].content == "Verified final answer."
    assert restored.generation_id and restored.through_seq == 1
    assert any(message.role == "tool" and message.tool_call_id == "c1" for message in restored.messages)


async def test_terminal_failure_rolls_back_conversation_head_and_keeps_previous_execution_view(collab, context_case, monkeypatch):
    execution = owner(collab, execution_id=collab.lead)
    await prepare(execution, context_case)
    store = SessionStore(collab.path)
    monkeypatch.setattr(store, "_rollup_session_usage", AsyncMock(side_effect=OSError("injected_terminal_failure")))
    with pytest.raises(OSError, match="injected_terminal_failure"):
        await store.mark_completed(**completion(collab, await execution.conversation_publication(context_case.request.messages)))
    async with open_shared_ctx(collab.path) as db:
        async with db.execute("SELECT COUNT(*) FROM model_context_conversation_heads") as cursor:
            assert (await cursor.fetchone())[0] == 0
        async with db.execute("SELECT generation_id FROM model_context_heads WHERE execution_id=?", (collab.lead,)) as cursor:
            assert (await cursor.fetchone())[0] == execution.generation
        async with db.execute("SELECT status FROM turns WHERE id=?", (collab.worker.turn_id,)) as cursor:
            assert (await cursor.fetchone())[0] == "running"


async def test_helper_cannot_publish_even_with_the_original_turn_finalizer(collab, context_case):
    execution = owner(collab)
    await prepare(execution, context_case)
    with pytest.raises(ContextStoreError, match="publisher_unavailable"):
        await SessionStore(collab.path).mark_completed(**completion(collab, await execution.conversation_publication(context_case.request.messages)))


async def test_cursor_reads_more_than_two_hundred_business_turns_without_dropping_early_constraints(collab):
    # Historical completed records predate the currently owned Turn. They are
    # canonical history, not execution authority or a synthesized Tool receipt.
    async with open_shared_ctx(collab.path) as db:
        await db.execute("UPDATE turns SET seq=206 WHERE id=?", (collab.worker.turn_id,))
        await db.executemany(
            "INSERT INTO turns(id,session_id,seq,user_text,status,final_answer,started_at) VALUES (?,?,?,?,?,?,?)",
            [
                (f"t_history_{i}", collab.worker.session_id, i, f"Keep constraint {i}.", "completed", f"Confirmed {i}.", "2026-10-01")
                for i in range(1, 206)
            ],
        )
        await db.executemany(
            "INSERT INTO turn_events(id,turn_id,seq,event_type,payload_json,occurred_at) VALUES (?,?,1,'lead.subagent_summary',?,'2026-10-01')",
            [
                (
                    f"evt_history_{index}",
                    f"t_history_{index}",
                    json.dumps(
                        {
                            "call_id": f"call_{index}",
                            "profile": "compute",
                            "task": f"Check {index}",
                            "status": "completed",
                            "outcome_message": f"Verified {index}.",
                        }
                    ),
                )
                for index in range(1, 206)
            ],
        )
        await db.commit()
    worker = collab.worker
    ctx = SimpleNamespace(
        session_store=SessionStore(collab.path),
        execution_owner_id=worker.owner_id,
        execution_generation=worker.generation,
        turn_id=worker.turn_id,
        team_id=worker.team_id,
        user_id=worker.user_id,
        workspace_id=worker.workspace_id,
        session_id=worker.session_id,
        knowledge_scope=None,
        knowledge_source_uses={},
        knowledge_source_ancestors=(),
        ordinary_lead_state=None,
    )
    history = await load_conversation_context(ctx)
    assert len(history.messages) == 820
    assert history.messages[0].content == "Keep constraint 1."
    assert history.messages[-1].content == "Confirmed 205."
    assert sum(message.role == "tool" for message in history.messages) == 205
    assert history.messages[1].tool_calls[0].arguments["task"] == "Check 1"
    assert history.through_seq == 205 and history.generation_id is None
