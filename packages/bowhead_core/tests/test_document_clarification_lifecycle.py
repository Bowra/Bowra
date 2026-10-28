"""Real migrated card/Turn transactions around retained document checkpoints."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.clarification_store import ClarificationStore
from bowhead_core.document_chat_runtime import finish_document_planning
from bowhead_core.document_clarification_lifecycle import prepare_document_pause_in_tx
from bowhead_core.documents.continuation import DocumentPlanningPause
from bowhead_core.events import TurnEventBus
from bowhead_core.turn_log_store import TurnLogStore
from bowhead_core.turn_runner import TurnRunner
from test_document_planning_continuation import QUESTION, checkpoint
from test_document_store import SCOPE
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def rows(ctx, query, args=()):
    async with open_shared_ctx(ctx.path) as db, db.execute(query, args) as cursor:
        return await cursor.fetchall()


async def prepare(ctx, case):
    store = ClarificationStore(ctx.path)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        card = await prepare_document_pause_in_tx(
            db, store=store, worker=case.worker, checkpoint=case.ref, questions=[{"question": QUESTION, "type": "free_text", "options": []}]
        )
        await db.commit()
    return store, card


async def pause(ctx, case):
    store = ClarificationStore(ctx.path)
    logs = TurnLogStore(str(ctx.path))

    async def sink(event):
        return await logs.record(turn_id=event.turn_id, event_type=event.event_type, payload=event.payload)

    bus = TurnEventBus(log_sink=sink, persist_before_live=True)
    runner = TurnRunner(SimpleNamespace(session_store=ctx.sessions))
    bus._pi_persist_turn_state = runner._persist_turn_state
    bus._pi_turn_state = {}
    tool_ctx = SimpleNamespace(
        **case.worker.model_dump(exclude={"generation", "owner_id"}),
        execution_generation=case.worker.generation,
        execution_owner_id=case.worker.owner_id,
        clarification_store=store,
        language_context=case.snapshot.language.language_context(),
    )
    result = await finish_document_planning(
        tool_ctx, DocumentPlanningPause(case.job_id, case.ref, QUESTION), bus=bus, tokens_input=17, tokens_output=9
    )
    card = (await store.get_pending_for_turn(case.worker.turn_id))[0]
    assert result.status.value == "clarification_pending"
    assert (result.tokens_input, result.tokens_output) == (17, 9)
    assert await ctx.sessions.mark_chat_spawn_dispatch_terminal(turn_id=case.worker.turn_id, owner_id=case.worker.owner_id)
    return store, card


async def answer(store, card, text="Compare Q2 with Q1"):
    return await store.resolve_once(
        card.request_id,
        chosen_value=text,
        answers=[{"question": QUESTION, "source": "custom", "answer": text, "skipped": False, "option_index": None}],
        resolution_channel="web",
        resolution_owner="test-resolution-owner",
        resolution_work={"request_id": card.request_id},
    )


async def test_pause_preserves_exact_checkpoint_and_excludes_document_tokens(ctx):
    case = await checkpoint(ctx)
    store, card = await pause(ctx, case)
    assert await ctx.sessions.is_turn_resume_ready(case.worker.turn_id)
    assert card.resume_context["__schema_version"] == 3
    assert "question_round" not in card.resume_context
    assert card.resume_context["continuation"]["checkpoint"] == case.ref.model_dump()
    assert await rows(ctx, "SELECT tokens_input,tokens_output FROM turns WHERE id='t_doc'") == [(17, 9)]
    assert len(await rows(ctx, "SELECT id FROM credit_ledger WHERE reason='template_document'")) == 1
    assert [
        row[0] for row in await rows(ctx, "SELECT event_type FROM turn_events WHERE event_type IN ('clarification_required','done') ORDER BY seq")
    ] == ["clarification_required", "done"]
    payload = json.loads((await rows(ctx, "SELECT payload_json FROM turn_events WHERE event_type='clarification_required'"))[0][0])
    assert payload["purpose"] == "document_draft"


async def test_card_and_association_share_one_transaction(ctx):
    case = await checkpoint(ctx)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute(
            "CREATE TRIGGER fail_card_association BEFORE INSERT ON clarification_document_continuations BEGIN SELECT RAISE(ABORT,'injected association failure'); END"
        )
        await db.commit()
    with pytest.raises(aiosqlite.IntegrityError, match="injected association failure"):
        await prepare(ctx, case)
    assert await rows(ctx, "SELECT request_id FROM clarification_requests") == []
    assert await rows(ctx, "SELECT card_id FROM clarification_document_continuations") == []


@pytest.mark.parametrize("boundary", ["card_only", "paused_without_done", "done_without_dispatch"])
async def test_expired_producer_repairs_pause_without_replanning(ctx, boundary):
    case = await checkpoint(ctx)
    store, card = await prepare(ctx, case)
    assert not await ctx.sessions.is_turn_resume_ready(case.worker.turn_id)
    if boundary != "card_only":
        assert await ctx.sessions.mark_clarification_pending(turn_id=case.worker.turn_id, intent="ambiguous", error_kind="needs_clarification")
    if boundary == "done_without_dispatch":
        await TurnLogStore(str(ctx.path)).record(
            turn_id=case.worker.turn_id, event_type="done", payload={"turn_id": case.worker.turn_id, "status": "clarification_pending"}
        )
    before = await rows(ctx, "SELECT * FROM document_model_calls")
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("UPDATE chat_turn_spawn_dispatch SET lease_expires_at='2020-01-01' WHERE turn_id=?", (case.worker.turn_id,))
        await db.commit()
    await ctx.sessions.recover_expired_chat_spawn_dispatches()
    assert await ctx.sessions.is_turn_resume_ready(case.worker.turn_id)
    assert (await store.get(card.request_id)).status == "pending"
    assert await rows(ctx, "SELECT * FROM document_model_calls") == before
    assert len(await rows(ctx, "SELECT 1 FROM turn_events WHERE event_type='clarification_required'")) == 1
    if boundary != "done_without_dispatch":
        assert await rows(ctx, "SELECT json_extract(payload_json,'$.display_usage.tokens_input') FROM turn_events WHERE event_type='done'") == [
            (600,)
        ]


@pytest.mark.parametrize("recovery", ["rollback", "expired_lease"])
async def test_original_answer_survives_two_preworker_recoveries(ctx, recovery):
    case = await checkpoint(ctx)
    store, card = await pause(ctx, case)
    await answer(store, card)
    for generation in (2, 3):
        assert (
            await ctx.sessions.advance_turn_execution_generation(
                turn_id=case.worker.turn_id, document_clarification_id=card.request_id, lease_seconds=-1 if recovery == "expired_lease" else 30
            )
            == generation
        )
        assert not await ctx.sessions.is_turn_resume_ready(case.worker.turn_id)
        if recovery == "rollback":
            assert await ctx.sessions.rollback_turn_resume_generation(
                turn_id=case.worker.turn_id, expected_execution_generation=generation, owner_id=None
            )
        else:
            await ctx.sessions.recover_expired_chat_spawn_dispatches()
        assert await ctx.sessions.is_turn_resume_ready(case.worker.turn_id)
        assert (await store.get(card.request_id)).answers[0]["answer"] == "Compare Q2 with Q1"
    assert await ctx.sessions.advance_turn_execution_generation(turn_id=case.worker.turn_id, document_clarification_id="another-card") is None
    assert await ctx.sessions.advance_turn_execution_generation(turn_id=case.worker.turn_id) is None


@pytest.mark.parametrize("mutation", ["question", "snapshot", "answer", "replace", "delete", "rewind"])
async def test_original_card_and_answer_cannot_be_rebound(ctx, mutation):
    case = await checkpoint(ctx)
    store, card = await pause(ctx, case)
    await answer(store, card)
    sql = {
        "question": "UPDATE clarification_requests SET rationale='Another question' WHERE request_id=?",
        "snapshot": "UPDATE clarification_requests SET resume_context_json='{}' WHERE request_id=?",
        "answer": "UPDATE clarification_requests SET answers_json='[]' WHERE request_id=?",
        "replace": "INSERT OR REPLACE INTO clarification_requests SELECT * FROM clarification_requests WHERE request_id=?",
        "delete": "DELETE FROM clarification_requests WHERE request_id=?",
        "rewind": "UPDATE clarification_requests SET status='pending' WHERE request_id=?",
    }[mutation]
    async with open_shared_ctx(ctx.path) as db:
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(sql, (card.request_id,))


@pytest.mark.parametrize("mutation", ["revocation", "model_change", "dismiss"])
async def test_unavailable_checkpoint_never_reserves_another_execution(ctx, mutation):
    case = await checkpoint(ctx)
    store, card = await pause(ctx, case)
    if mutation == "dismiss":
        await store.invalidate_one(card.request_id, target_status="dismissed", reason="user_rephrase", actor="user")
    else:
        await answer(store, card)
        async with open_shared_ctx(ctx.path) as db:
            if mutation == "revocation":
                await db.execute("UPDATE workspace_members SET role='viewer' WHERE workspace_id=? AND user_id=?", (SCOPE.workspace_id, SCOPE.user_id))
            elif mutation == "model_change":
                await db.execute("UPDATE teams SET llm_config_version=2 WHERE id=?", (SCOPE.team_id,))
            await db.commit()
    assert await ctx.sessions.advance_turn_execution_generation(turn_id=case.worker.turn_id, document_clarification_id=card.request_id) is None
    await ctx.sessions.recover_expired_chat_spawn_dispatches()
    assert (await ctx.sessions.get_turn(case.worker.turn_id)).status == ("cancelled" if mutation == "dismiss" else "failed")
    assert await rows(ctx, "SELECT state,error_code FROM document_executions") == [("cancelled", "clarification_resume_unavailable")]
    assert await rows(ctx, "SELECT reserved_generation,consumed_generation FROM clarification_document_continuations") == [(None, None)]


async def test_stop_after_checkpoint_preparation_closes_document_without_resurrecting_turn(ctx):
    case = await checkpoint(ctx)
    _, card = await prepare(ctx, case)
    calls = await rows(ctx, "SELECT * FROM document_model_calls")
    charges = await rows(ctx, "SELECT * FROM credit_ledger")
    assert (
        await ctx.sessions.request_cancel_turn(
            team_id=SCOPE.team_id,
            user_id=SCOPE.user_id,
            workspace_id=SCOPE.workspace_id,
            turn_id=case.worker.turn_id,
            expected_execution_generation=case.worker.generation,
            cancel_request_id="cancel-document-pause",
        )
        == "cancelled"
    )
    for _ in range(2):
        await ctx.sessions.recover_expired_chat_spawn_dispatches()
        assert (await ctx.sessions.get_turn(case.worker.turn_id)).status == "cancelled"
        assert await rows(ctx, "SELECT state,error_code FROM document_executions") == [("cancelled", "clarification_resume_unavailable")]
        assert await rows(ctx, "SELECT * FROM document_model_calls") == calls
        assert await rows(ctx, "SELECT * FROM credit_ledger") == charges
        assert await rows(ctx, "SELECT 1 FROM turn_events WHERE event_type='clarification_required'") == []
        assert await ctx.sessions.advance_turn_execution_generation(turn_id=case.worker.turn_id, document_clarification_id=card.request_id) is None


async def test_pause_invalidation_rolls_back_domain_and_turn_together(ctx):
    case = await checkpoint(ctx)
    store, card = await pause(ctx, case)
    await store.invalidate_one(card.request_id, target_status="dismissed", reason="user_rephrase", actor="user")
    async with open_shared_ctx(ctx.path) as db:
        await db.execute(
            "CREATE TRIGGER reject_pause_terminal BEFORE UPDATE OF status ON turns "
            "WHEN NEW.status='cancelled' BEGIN SELECT RAISE(ABORT,'injected terminal failure'); END"
        )
        await db.commit()
    with pytest.raises(aiosqlite.IntegrityError, match="injected terminal failure"):
        await ctx.sessions.recover_expired_chat_spawn_dispatches()
    assert (await ctx.sessions.get_turn(case.worker.turn_id)).status == "clarification_pending"
    assert await rows(ctx, "SELECT state,error_code FROM document_executions") == [("question", None)]
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("DROP TRIGGER reject_pause_terminal")
        await db.commit()
    await ctx.sessions.recover_expired_chat_spawn_dispatches()
    assert (await ctx.sessions.get_turn(case.worker.turn_id)).status == "cancelled"
    assert await rows(ctx, "SELECT state,error_code FROM document_executions") == [("cancelled", "clarification_resume_unavailable")]


async def test_bounded_recovery_rotates_healthy_cards_without_foreign_worker_starvation(monkeypatch):
    """Exercise the real recovery SQL batch while isolating per-card domain validation."""
    from bowhead_core.documents import continuation

    from bowhead_core import document_clarification_lifecycle as lifecycle
    from bowhead_core import session_store

    read = AsyncMock(return_value=SimpleNamespace(creation_worker=lambda: SimpleNamespace(generation=1, owner_id="creator")))
    monkeypatch.setattr(lifecycle, "read_document_card_in_tx", read)
    monkeypatch.setattr(lifecycle, "require_document_pause_in_tx", AsyncMock())
    monkeypatch.setattr(session_store, "_legacy_generation_floor_in_tx", AsyncMock(return_value=0))
    monkeypatch.setattr(session_store, "_legacy_root_terminal_matches_in_tx", AsyncMock(return_value=True))
    monkeypatch.setattr(session_store, "_next_legacy_event_cursors_in_tx", AsyncMock(return_value=(1, 1)))
    invalidate = AsyncMock(side_effect=AssertionError("A foreign spawned worker must not enter pause cleanup"))
    monkeypatch.setattr(continuation, "invalidate_document_pause_in_tx", invalidate)
    async with aiosqlite.connect(":memory:") as db:
        await db.executescript("""
            CREATE TABLE clarification_document_continuations(card_id,turn_id,job_id,creation_generation,creation_owner_id,reserved_generation,consumed_generation);
            CREATE TABLE turns(id,status,execution_generation,tokens_input,tokens_output,ended_at,error_kind);
            CREATE TABLE chat_turn_spawn_dispatch(turn_id,state,owner_id,lease_kind,lease_execution_generation,lease_expires_at,cancel_requested_at,updated_at);
            CREATE TABLE document_executions(job_id,state,owner);
            CREATE TABLE turn_events(turn_id,event_type,payload_json);
        """)
        for index in range(66):
            key = f"card-{index:02d}"
            foreign = index == 65
            await db.execute("INSERT INTO clarification_document_continuations VALUES (?,?,?,1,'creator',NULL,NULL)", (key, key, key))
            await db.execute("INSERT INTO turns VALUES (?,?,1,0,0,'2020-01-01',NULL)", (key, "cancelled" if foreign else "clarification_pending"))
            await db.execute(
                "INSERT INTO chat_turn_spawn_dispatch VALUES (?,?,?,'initial',1,'2020-01-01',NULL,?)",
                (key, "spawned" if foreign else "terminal", "foreign" if foreign else "creator", "2000-01-01" if foreign else "2020-01-01"),
            )
            await db.execute("INSERT INTO document_executions VALUES (?,'question','creator')", (key,))
            await db.execute("INSERT INTO turn_events VALUES (?,'clarification_required',?)", (key, '{"clarification_id":"' + key + '"}'))
        await lifecycle.recover_document_pauses_in_tx(db, now="2026-10-04T01:00:00")
        assert len(read.await_args_list) == 64
        assert "card-64" not in {call.kwargs["card_id"] for call in read.await_args_list}
        read.reset_mock()
        await lifecycle.recover_document_pauses_in_tx(db, now="2026-10-04T02:00:00")
        assert "card-64" in {call.kwargs["card_id"] for call in read.await_args_list}
        invalidate.assert_not_awaited()


async def test_unknown_physical_observation_never_exposes_a_card(ctx):
    with pytest.raises(ValueError, match="document_usage_unresolved"):
        await checkpoint(ctx, physical_state="unknown")
    assert await rows(ctx, "SELECT request_id FROM clarification_requests") == []
    assert await rows(ctx, "SELECT card_id FROM clarification_document_continuations") == []


async def claim_answered(ctx):
    case = await checkpoint(ctx)
    store, card = await pause(ctx, case)
    await answer(store, card)
    generation = await ctx.sessions.advance_turn_execution_generation(turn_id=case.worker.turn_id, document_clarification_id=card.request_id)
    assert generation == 2
    worker = case.worker.model_copy(update={"generation": generation, "owner_id": "resumed-worker"})
    assert await ctx.sessions.claim_chat_spawn_dispatch_for_worker(turn_id=worker.turn_id, owner_id=worker.owner_id, lease_seconds=600) == "claimed"
    return case, card, worker


@pytest.mark.parametrize("fail_association", [False, True])
async def test_real_shared_resume_consumes_once_with_atomic_domain_attempt(ctx, fail_association):
    from bowhead_core.document_chat_continuation import DocumentResumeReservation
    from bowhead_core.document_clarification_lifecycle import consume_document_resume_in_tx
    from bowhead_core.documents.delivery import _fence

    case, card, worker = await claim_answered(ctx)
    reservation = DocumentResumeReservation(card_id=card.request_id, checkpoint=case.ref, generation=worker.generation)
    if fail_association:
        async with open_shared_ctx(ctx.path) as db:
            await db.execute(
                "CREATE TRIGGER fail_shared_consume BEFORE UPDATE OF consumed_generation ON clarification_document_continuations BEGIN SELECT RAISE(ABORT,'shared consume fault'); END"
            )
            await db.commit()
    try:
        async with open_shared_ctx(ctx.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            attempt = await consume_document_resume_in_tx(db, worker=worker, reservation=reservation)
            assert attempt.attempt_no == 2 and attempt.worker == worker
            assert await _fence(db, SCOPE, case.job_id, worker.owner_id)
            await db.commit()
    except aiosqlite.IntegrityError as error:
        assert fail_association and "shared consume fault" in str(error)
    assert await rows(ctx, "SELECT attempt_no FROM document_planning_attempts ORDER BY attempt_no") == ([(1,)] if fail_association else [(1,), (2,)])
    assert await rows(ctx, "SELECT reserved_generation,consumed_generation,consumed_owner_id FROM clarification_document_continuations") == [
        (2, None, None) if fail_association else (2, 2, worker.owner_id)
    ]
    if not fail_association:
        assert not await ctx.sessions.rollback_turn_resume_generation(
            turn_id=worker.turn_id, expected_execution_generation=2, owner_id=worker.owner_id
        )
        async with open_shared_ctx(ctx.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            with pytest.raises(ValueError):
                await consume_document_resume_in_tx(db, worker=worker, reservation=reservation)


async def test_prior_history_excludes_only_proven_original_document_card(ctx):
    from bowhead_core.document_clarification_lifecycle import prior_document_answers_in_tx

    case = await checkpoint(ctx)
    await pause(ctx, case)
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN")
        assert await prior_document_answers_in_tx(db, worker=case.worker) == []
        with pytest.raises(ValueError):
            await prior_document_answers_in_tx(db, worker=case.worker.model_copy(update={"owner_id": "another-owner"}))


async def test_queued_followup_waits_for_original_document_answer(ctx):
    case = await checkpoint(ctx)
    assert await ctx.sessions.append_steer_buffer(
        session_id=case.worker.session_id, text="Then summarize the changes", expected_turn_id=case.worker.turn_id
    )
    store, card = await pause(ctx, case)
    # Exercise the maintenance promotion of an old terminal-active buffer.
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("UPDATE turns SET ended_at='2000-01-01' WHERE id=?", (case.worker.turn_id,))
        await db.commit()
    await ctx.sessions.recover_expired_chat_spawn_dispatches()
    assert await ctx.sessions.is_turn_resume_ready(case.worker.turn_id)
    assert await rows(ctx, "SELECT count(*) FROM turns") == [(1,)]
    assert await rows(ctx, "SELECT json_extract(steer_buffer_json,'$[0].text') FROM sessions WHERE id='s_doc'") == [("Then summarize the changes",)]
    await answer(store, card)
    assert await ctx.sessions.advance_turn_execution_generation(turn_id=case.worker.turn_id, document_clarification_id=card.request_id) == 2


async def test_live_terminal_hook_cannot_drain_document_pause_into_another_turn(ctx):
    case = await checkpoint(ctx)
    await prepare(ctx, case)
    assert await ctx.sessions.append_steer_buffer(
        session_id=case.worker.session_id, text="Follow up after the document", expected_turn_id=case.worker.turn_id
    )
    assert await ctx.sessions.mark_clarification_pending(turn_id=case.worker.turn_id, intent="ambiguous", error_kind="needs_clarification")
    assert (
        await ctx.sessions.materialize_steer_follow_up(
            session_id=case.worker.session_id, current_turn_id=case.worker.turn_id, next_turn_id="t_must_wait"
        )
        == []
    )
    assert await rows(ctx, "SELECT count(*) FROM turns") == [(1,)]
    assert await ctx.sessions.get_active_turn(case.worker.session_id) == case.worker.turn_id


@pytest.mark.parametrize("delivery_fault", [False, True])
async def test_shared_review_emits_durable_original_canvas_pointer_before_done(ctx, delivery_fault):
    from bowhead_core.events import TerminalDurabilityAbort
    from test_document_canvas_open import ready

    case, review = await ready(ctx)
    logs = TurnLogStore(str(ctx.path))

    async def sink(event):
        if delivery_fault and event.event_type == "document.draft_open":
            raise OSError("injected durable event fault")
        return await logs.record(turn_id=event.turn_id, event_type=event.event_type, payload=event.payload)

    bus = TurnEventBus(log_sink=sink, persist_before_live=True)
    runner = TurnRunner(SimpleNamespace(session_store=ctx.sessions))
    bus._pi_persist_turn_state = runner._persist_turn_state
    bus._pi_turn_state = {}
    tool_ctx = SimpleNamespace(
        **case.worker.model_dump(exclude={"generation", "owner_id"}),
        execution_generation=case.worker.generation,
        execution_owner_id=case.worker.owner_id,
        clarification_store=ClarificationStore(ctx.path),
        language_context=case.snapshot.language.language_context(),
    )
    if delivery_fault:
        with pytest.raises(TerminalDurabilityAbort, match="durable event write"):
            await finish_document_planning(tool_ctx, review, bus=bus)
        assert await rows(ctx, "SELECT 1 FROM turn_events WHERE event_type='done'") == []
    else:
        result = await finish_document_planning(tool_ctx, review, bus=bus)
        assert result.status.value == "completed"
        events = await logs.list_events(case.worker.turn_id)
        assert [event.event_type for event in events if event.event_type in {"document.draft_open", "final_answer", "done"}] == [
            "document.draft_open",
            "final_answer",
            "done",
        ]
        assert events[0].payload["draft_digest"] == review.draft_digest
        assert events[0].payload["job_id"] == case.job_id
    assert (await case.delivery.get(scope=SCOPE, job_id=case.job_id))["state"] == "review"


@pytest.mark.parametrize("change", [None, "question", "unresolved", "wrong_scope", "old_untyped", "wrong_generation"])
async def test_prior_ordinary_answers_are_retained_only_from_canonical_round_history(ctx, change):
    from bowhead_core.document_chat_continuation import DocumentTurnOwner
    from bowhead_core.document_clarification_lifecycle import prior_document_answers_in_tx

    store = ClarificationStore(ctx.path)
    prior_question = "Which audience?"
    assert await ctx.sessions.claim_chat_spawn_dispatch_for_worker(turn_id="t_doc", owner_id="ordinary-worker", lease_seconds=600) == "claimed"
    card = await store.put(
        turn_id="t_doc",
        user_id=SCOPE.user_id,
        workspace_id=SCOPE.workspace_id,
        session_id="s_doc",
        token="",
        rationale=prior_question,
        candidates=[],
        kind="ambiguous_turn",
        resume_context={"user_text": ctx.source.user_text},
        bounded_question={"generation": 1, "owner_id": "ordinary-worker", "questions": [{"question": prior_question}]},
    )
    typed = [
        {
            "question": "Another question" if change == "question" else prior_question,
            "source": "custom",
            "answer": "Board members",
            "skipped": False,
            "option_index": None,
        }
    ]
    if change != "unresolved":
        await store.resolve_once(card.request_id, chosen_value="Board members", answers=None if change == "old_untyped" else typed)
    worker = DocumentTurnOwner(
        **SCOPE.model_dump(),
        session_id="another-session" if change == "wrong_scope" else "s_doc",
        turn_id="t_doc",
        generation=1 if change == "wrong_generation" else 2,
        owner_id="resumed-worker",
    )
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("BEGIN")
        if change:
            with pytest.raises(ValueError):
                await prior_document_answers_in_tx(db, worker=worker)
        else:
            assert await prior_document_answers_in_tx(db, worker=worker) == typed
