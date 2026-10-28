"""Persistent original-card boundaries; full runner coverage lives in HTTP tests."""

import json
from dataclasses import asdict, replace
from types import SimpleNamespace

import aiosqlite
import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.clarification_store import ClarificationStore
from bowhead_core.document_chat_continuation import DocumentTurnOwner
from bowhead_core.ordinary_lead_checkpoint import OrdinaryLeadCapture, OrdinaryLeadCheckpoint, capture_message_prefix
from bowhead_core.ordinary_lead_continuation import (
    OrdinaryContinuationUnavailable,
    canonical_json,
    consume_ordinary_resume_in_tx,
    read_ordinary_card_in_tx,
    reopen_ordinary_checkpoint,
    validate_ordinary_document_admission_in_tx,
)
from bowhead_core.ordinary_lead_runtime import initialize_ordinary_lead
from bowhead_core.turn_log_store import TurnLogStore
from bowhead_core.types import ToolInvocation
from bowhead_router.types import Message, ToolCall
from cryptography.fernet import Fernet
from test_document_store import SCOPE, _sql
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db

pytestmark = pytest.mark.integration
QUESTION = {"question": "Which title?", "type": "free_text", "options": []}
ANSWER = {"question": QUESTION["question"], "source": "custom", "answer": "Quarterly review", "skipped": False, "option_index": None}


def multi_checkpoint(checkpoint, helper_count=2):
    """Controlled codec material, never evidence of actual helper execution."""
    messages = [checkpoint.messages()[0]]
    invocations, helpers = [], []
    for index in range(helper_count):
        native_id, child_id = f"helper-{index}", f"sub-helper-{index}"
        invocation = replace(
            checkpoint.invocations()[0],
            call_id=f"trace-{index}",
            arguments={"task": f"Inspect source {index}"},
            result={"ok": True, "subagent_id": child_id},
        )
        invocations.append(asdict(invocation))
        messages.extend(
            [
                Message("assistant", "", tool_calls=[ToolCall(native_id, "subagent_spawn", invocation.arguments)]),
                Message("tool", canonical_json(invocation.result), name="subagent_spawn", tool_call_id=native_id),
            ]
        )
        helpers.append(
            {
                "execution_id": f"execution-{index}",
                "dispatch_key": "call:" + native_id,
                "subagent_id": child_id,
                "binding_hash": "a" * 64,
                "tools": [],
            }
        )
    messages.append(checkpoint.messages()[-1])
    return checkpoint.model_copy(
        update={
            "contract": "ordinary-lead-loop.v5",
            "messages_json": canonical_json([asdict(message) for message in messages]),
            "invocations_json": canonical_json(invocations),
            "next_iteration": helper_count + 1,
            "material_json": canonical_json({"helpers": helpers, "attachments": None, "canvas": None}),
        }
    )


async def prepare(ctx, monkeypatch, *, contract="ordinary-lead-loop.v1", document_routing=None, helper_count=1):
    monkeypatch.setenv("FEATURE_CHAT_MULTI_HELPER_QUESTION", "1")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    worker = DocumentTurnOwner(**SCOPE.model_dump(), session_id="s_doc", turn_id="t_doc", generation=1, owner_id="ordinary-producer")
    assert await ctx.sessions.claim_chat_spawn_dispatch_for_worker(turn_id=worker.turn_id, owner_id=worker.owner_id, lease_seconds=600) == "claimed"
    # Controlled provider-neutral loop material tests the real card writer and
    # transaction fences. This is not evidence of actual helper execution.
    args = {"questions": [QUESTION]}
    helper = ToolInvocation("subagent_spawn", {"task": "Inspect the source"}, call_id="helper", result={"ok": True}, ended_at="2026-10-05")
    messages = [
        Message("user", ctx.source.user_text),
        Message("assistant", "", tool_calls=[ToolCall("helper", "subagent_spawn", helper.arguments)]),
        Message("tool", canonical_json(helper.result), name="subagent_spawn", tool_call_id="helper"),
        Message("assistant", "", tool_calls=[ToolCall("question", "clarification_ask", args)]),
        Message("tool", "pause", name="clarification_ask", tool_call_id="question"),
    ]
    checkpoint = OrdinaryLeadCheckpoint(
        contract=contract,
        document_routing=document_routing,
        subagent_id="sub_lead",
        system="Original system",
        turn_tail="Original tail",
        language_tail="",
        attachment_context=None,
        max_iterations=10,
        next_iteration=2,
        tool_schema_hash="a" * 64,
        preferred_provider=None,
        preferred_model=None,
        model_config_version=0,
        language_json="{}",
        messages_json=capture_message_prefix(messages, question_call_id="question"),
        invocations_json=canonical_json([asdict(helper)]),
        context_json="{}",
        question_call_id="question",
        question_arguments_json=canonical_json(args),
        consumed_tokens=15,
        remaining_wallclock_s=120.0,
        remaining_action_deadline_s=60.0,
        final_bounce_count=0,
        source_bounce_used=False,
        overflow_retried=False,
        deadline_retried=False,
        history_sizes=[],
        material_json="{}" if contract == "ordinary-lead-loop.v1" else canonical_json({"tools": [], "canvas": None}),
    )
    if helper_count > 1:
        checkpoint = multi_checkpoint(checkpoint, helper_count)
    store = ClarificationStore(ctx.path)
    card = await store.put(
        turn_id=worker.turn_id,
        user_id=worker.user_id,
        workspace_id=worker.workspace_id,
        session_id=worker.session_id,
        token="",
        rationale=QUESTION["question"],
        candidates=[],
        kind="ambiguous_turn",
        ordinary_question=OrdinaryLeadCapture(worker, checkpoint, [QUESTION]),
    )
    await _sql(ctx, "UPDATE turns SET status='clarification_pending' WHERE id=?", (worker.turn_id,))
    await TurnLogStore(ctx.path).record(
        turn_id=worker.turn_id, event_type="done", payload={"status": "clarification_pending", "execution_generation": 1}
    )
    assert await ctx.sessions.mark_chat_spawn_dispatch_terminal(turn_id=worker.turn_id, owner_id=worker.owner_id)
    assert await ctx.sessions.is_turn_resume_ready(worker.turn_id)
    return store, card, worker, checkpoint


async def test_ordinary_lead_does_not_engage_without_a_team(monkeypatch, tmp_path):
    # Dev teamless sessions have no Team scope; the optional continuation must stay off, not fail the Turn.
    lead = SimpleNamespace(
        team_id=None,
        user_id="u_dev",
        workspace_id="ws_dev",
        session_id="s_dev",
        turn_id="t_dev",
        execution_generation=1,
        execution_owner_id="ordinary-producer",
        session_store=SimpleNamespace(db_path=tmp_path / "missing" / "bowhead.db"),
    )
    await initialize_ordinary_lead(lead, request_context={})
    assert getattr(lead, "ordinary_lead_state", None) is None


@pytest.mark.parametrize("member", [True, False])
async def test_ordinary_lead_engages_only_for_a_current_member(ctx, monkeypatch, member):
    # A missing membership row leaves the optional continuation off instead of failing the Turn.
    assert await ctx.sessions.claim_chat_spawn_dispatch_for_worker(turn_id="t_doc", owner_id="ordinary-producer", lease_seconds=600) == "claimed"
    if not member:
        await _sql(ctx, "DELETE FROM team_members WHERE user_id=?", (SCOPE.user_id,))
    lead = SimpleNamespace(
        **SCOPE.model_dump(),
        session_id="s_doc",
        turn_id="t_doc",
        execution_generation=1,
        execution_owner_id="ordinary-producer",
        session_store=ctx.sessions,
    )
    await initialize_ordinary_lead(lead, request_context={})
    assert (getattr(lead, "ordinary_lead_state", None) is not None) is member


async def resume(ctx, card, worker):
    generation = await ctx.sessions.advance_turn_execution_generation(turn_id=worker.turn_id, clarification_id=card.request_id)
    assert generation is not None
    worker = worker.model_copy(update={"generation": generation, "owner_id": f"resumed-{generation}"})
    assert await ctx.sessions.claim_chat_spawn_dispatch_for_worker(turn_id=worker.turn_id, owner_id=worker.owner_id, lease_seconds=600) == "claimed"
    return worker


@pytest.mark.parametrize("helper_count", [1, 2])
async def test_first_answer_and_checkpoint_survive_safe_retry_but_started_work_cannot_restart(ctx, monkeypatch, helper_count):
    store, card, worker, checkpoint = await prepare(ctx, monkeypatch, helper_count=helper_count)
    assert await store.mark_injected(card.request_id) is False
    original = card.resume_context
    assert "Original system" not in canonical_json(original)
    answered, changed = await store.resolve_once(card.request_id, answers=[ANSWER])
    assert changed and answered.answers == [ANSWER]
    retry, changed = await store.resolve_once(card.request_id, answers=[{**ANSWER, "answer": "Different"}])
    assert not changed and retry.answers == [ANSWER]
    monkeypatch.setenv("FEATURE_CHAT_MULTI_HELPER_QUESTION", "0")
    for _ in range(2):
        worker = await resume(ctx, card, worker)
        assert await ctx.sessions.rollback_turn_resume_generation(
            turn_id=worker.turn_id, expected_execution_generation=worker.generation, owner_id=worker.owner_id
        )
    worker = await resume(ctx, card, worker)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        record = await consume_ordinary_resume_in_tx(db, worker=worker, card_id=card.request_id)
        assert reopen_ordinary_checkpoint(record) == checkpoint
        answer = await validate_ordinary_document_admission_in_tx(db, worker=worker, source_hash=record.snapshot.continuation.source_hash)
        assert answer.answers == (ANSWER,)
        await db.commit()
    assert not await ctx.sessions.rollback_turn_resume_generation(
        turn_id=worker.turn_id, expected_execution_generation=worker.generation, owner_id=worker.owner_id
    )
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        with pytest.raises(OrdinaryContinuationUnavailable):
            await consume_ordinary_resume_in_tx(db, worker=worker, card_id=card.request_id)
        with pytest.raises(aiosqlite.IntegrityError, match="immutable|consumption unavailable"):
            await db.execute(
                "UPDATE clarification_requests SET injected_at=NULL,continuation_generation=NULL,continuation_owner_id=NULL WHERE request_id=?",
                (card.request_id,),
            )
    assert (await store.get(card.request_id)).resume_context == original


@pytest.mark.parametrize(
    "table,condition",
    [
        ("users", "id='u_doc'"),
        ("teams", "id='team_doc'"),
        ("team_members", "user_id='u_doc'"),
        ("workspace_members", "user_id='u_doc'"),
        ("workspaces", "id='ws_doc'"),
    ],
)
@pytest.mark.parametrize("helper_count", [1, 2])
async def test_revoked_principal_cannot_consume_an_answer(ctx, monkeypatch, table, condition, helper_count):
    store, card, worker, _ = await prepare(ctx, monkeypatch, helper_count=helper_count)
    await store.resolve_once(card.request_id, answers=[ANSWER])
    worker = await resume(ctx, card, worker)
    status = "removed" if table in {"team_members", "workspace_members"} else "archived" if table == "workspaces" else "disabled"
    await _sql(ctx, f"UPDATE {table} SET status=? WHERE {condition}", (status,))
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        with pytest.raises(OrdinaryContinuationUnavailable):
            await consume_ordinary_resume_in_tx(db, worker=worker, card_id=card.request_id)
    assert (await store.get(card.request_id)).injected_at is None


async def test_old_cards_cannot_be_promoted_and_original_card_cannot_be_replaced(ctx, monkeypatch):
    store, card, worker, _ = await prepare(ctx, monkeypatch)
    with pytest.raises(ValueError, match="server_owned"):
        await store.put(
            turn_id=worker.turn_id,
            user_id=worker.user_id,
            workspace_id=worker.workspace_id,
            token="",
            rationale="Q",
            candidates=[],
            resume_context=card.resume_context,
        )
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        with pytest.raises(aiosqlite.IntegrityError, match="retained"):
            await db.execute("DELETE FROM clarification_requests WHERE request_id=?", (card.request_id,))
        with pytest.raises(aiosqlite.IntegrityError, match="immutable"):
            await db.execute("UPDATE clarification_requests SET resume_context_json='{}' WHERE request_id=?", (card.request_id,))
    with pytest.raises(aiosqlite.IntegrityError, match="cannot be replaced"):
        await store.put(
            turn_id=worker.turn_id, user_id=worker.user_id, workspace_id=worker.workspace_id, token="", rationale="Another question", candidates=[]
        )


async def test_unreadable_key_and_changed_answer_mapping_are_refused(ctx, monkeypatch):
    store, card, worker, checkpoint = await prepare(ctx, monkeypatch)
    with pytest.raises(OrdinaryContinuationUnavailable):
        await store.resolve_once(card.request_id, answers=[{**ANSWER, "question": "Unrelated"}])
    assert (await store.get(card.request_id)).status == "pending"
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN")
        record = await read_ordinary_card_in_tx(db, card_id=card.request_id)
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    with pytest.raises(OrdinaryContinuationUnavailable):
        reopen_ordinary_checkpoint(record)
    resumed = checkpoint.resume_messages((ANSWER,))
    assert resumed[-1].tool_call_id == "question" and "Quarterly review" in resumed[-1].content
    assert len(resumed) == 5


@pytest.mark.parametrize(
    "corruption", ["missing_result", "duplicate_call", "foreign_result", "question_sibling", "exhausted", "oversize", "deep_context"]
)
async def test_retained_loop_rejects_incomplete_identity_and_budget(ctx, monkeypatch, corruption):
    _, _, _, checkpoint = await prepare(ctx, monkeypatch)
    messages = json.loads(checkpoint.messages_json)
    update = {}
    if corruption == "missing_result":
        del messages[2]
    elif corruption == "duplicate_call":
        messages[-1]["tool_calls"][0]["id"] = messages[1]["tool_calls"][0]["id"]
    elif corruption == "foreign_result":
        messages[2]["tool_call_id"] = "foreign"
    elif corruption == "question_sibling":
        messages[-1]["tool_calls"].append({"id": "sibling", "name": "skill_list", "arguments": {}})
    elif corruption == "exhausted":
        update["next_iteration"] = checkpoint.max_iterations
    elif corruption == "oversize":
        update["system"] = "x" * (256 * 1024)
    elif corruption == "deep_context":
        nested = {}
        for _ in range(30):
            nested = {"child": nested}
        update["context_json"] = canonical_json(nested)
    update["messages_json"] = canonical_json(messages)
    with pytest.raises(ValueError):
        OrdinaryLeadCheckpoint.decode(canonical_json({**checkpoint.model_dump(), **update}).encode())


@pytest.mark.parametrize("started", [False, True])
@pytest.mark.parametrize("helper_count", [1, 2])
async def test_lease_recovery_only_restores_unconsumed_original_question(ctx, monkeypatch, started, helper_count):
    store, card, original, _ = await prepare(ctx, monkeypatch, helper_count=helper_count)
    await store.resolve_once(card.request_id, answers=[ANSWER])
    generation = await ctx.sessions.advance_turn_execution_generation(turn_id=original.turn_id, clarification_id=card.request_id)
    assert generation == 2
    if started:
        owner = "consumed-resume-worker"
        assert await ctx.sessions.claim_chat_spawn_dispatch_for_worker(turn_id=original.turn_id, owner_id=owner, lease_seconds=600) == "claimed"
        worker = original.model_copy(update={"generation": generation, "owner_id": owner})
        async with open_shared_ctx(ctx.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await consume_ordinary_resume_in_tx(db, worker=worker, card_id=card.request_id)
            await db.commit()
    await _sql(ctx, "UPDATE chat_turn_spawn_dispatch SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE turn_id=?", (original.turn_id,))
    await ctx.sessions.recover_expired_chat_spawn_dispatches()
    saved = await store.get(card.request_id)
    assert saved.answers == [ANSWER]
    assert bool(saved.injected_at) is started
    turn_state = await _sql(ctx, "SELECT status FROM turns WHERE id=?", (original.turn_id,))
    assert turn_state[0][0] == ("failed" if started else "clarification_pending")
    assert await ctx.sessions.is_turn_resume_ready(original.turn_id) is (not started)
    next_generation = await ctx.sessions.advance_turn_execution_generation(turn_id=original.turn_id, clarification_id=card.request_id)
    assert next_generation is None if started else next_generation == 3
