"""Bounded questions retain original identity and every answer without effects."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_lead_continuation import read_pause
from bowhead_core.chat_reference_projection import read_reference_projection
from bowhead_core.chat_reference_storage import ChatReferenceStore
from bowhead_core.clarification_rounds import available
from bowhead_core.clarification_store import ClarificationStore
from test_reference_lead_delegation import _normal_lead
from test_reference_lead_delegation import lead_case as lead_case
from test_reference_lead_delegation import start_case as start_case
from test_reference_primary_runtime import _ctx, _rows, _runtime

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize("recovery", [None, "rollback", "expired_lease"])
async def test_native_repeated_questions_keep_all_answers_and_one_primary(lead_case, monkeypatch, recovery):
    case = lead_case
    case.runtime = await _runtime(case)
    store = ClarificationStore(case.fixture.db_path)
    answers = ["period-2026", "currency-CNY", "basis-consolidated"]
    requests = []
    cards = []
    resumed_generations = []
    for index, question in enumerate(["Which period?", "Which currency?", "Which consolidation basis?"]):
        await _normal_lead(
            case,
            monkeypatch,
            clarification_tool=True,
            lead_free_text_question=True,
            lead_question_text=question,
            expected_question_rounds=index + 1,
            expected_answer=answers[index - 1],
            captured_requests=requests,
        )
        assert not _rows(case, "chat_reference_evidence")
        card = sorted(_rows(case, "clarification_requests"), key=lambda row: row["created_at"])[-1]
        context = json.loads(card["resume_context_json"])
        cards.append(card)
        assert context["question_round"]["round"] == index + 1
        assert context["question_round"]["generation"] == case.runtime.generation
        assert context["__schema_version"] == 2
        assert [q["question"] for q in context["card_questions"]] == [question]
        if index:
            # This generation answered the previous card and produced this one.
            async with open_shared_ctx(case.fixture.db_path) as db:
                await db.execute("BEGIN")
                previous = await read_pause(db, "t_reference", resume_generation=case.runtime.generation)
                assert previous["request_id"] == cards[-2]["request_id"]
        await store.resolve(card["request_id"], chosen_value=answers[index])
        assert await case.store.mark_chat_spawn_dispatch_terminal(turn_id="t_reference", owner_id=case.runtime.owner_id)
        expected_generation = case.runtime.generation + 1
        for _ in range(2 if recovery else 0):
            assert (
                await case.store.advance_turn_execution_generation(
                    turn_id="t_reference",
                    reference_clarification_id=card["request_id"],
                    lease_seconds=-1 if recovery == "expired_lease" else 30,
                )
                == expected_generation
            )
            if recovery == "expired_lease":
                await case.store.recover_expired_chat_spawn_dispatches(reference_store=ChatReferenceStore(case.runtime.cipher))
                assert (await case.store.get_turn("t_reference")).status == "clarification_pending"
            else:
                assert await case.store.rollback_turn_resume_generation(
                    turn_id="t_reference", expected_execution_generation=expected_generation, owner_id=None
                )
            expected_generation += 1
        generation = await case.store.advance_turn_execution_generation(turn_id="t_reference", reference_clarification_id=card["request_id"])
        assert generation == expected_generation
        resumed_generations.append(generation)
        if recovery:
            assert not await case.store.rollback_turn_resume_generation(
                turn_id="t_reference", expected_execution_generation=generation - 1, owner_id=None
            )
            stale = replace(case.runtime, generation=generation - 1, owner_id="stale-worker")
            with pytest.raises(ValueError, match="reference_continuation_unavailable"):
                await stale.claim_bi_resume(clarification_id=card["request_id"])
        case.runtime = replace(case.runtime, generation=generation, owner_id=f"worker-round-{generation}")
        await case.runtime.claim_bi_resume(clarification_id=card["request_id"])
        assert await case.store.advance_turn_execution_generation(turn_id="t_reference", reference_clarification_id=card["request_id"]) is None
        # Closing new admission must preserve the accepted bounded policy.
    ctx = _ctx(case)
    ctx._reference_runtime = case.runtime
    ctx.clarification_store = store
    assert not await available(ctx)
    final_requests = []
    await _normal_lead(case, monkeypatch, expected_answer=answers[-1], captured_requests=final_requests, expected_pause_count=3)
    wire = json.dumps(final_requests[0], ensure_ascii=False)
    assert all(value in wire for value in answers)
    assert len(requests) == 3
    assert len(_rows(case, "turn_agent_execution")) == 1
    assert [row["kind"] for row in _rows(case, "chat_reference_evidence")] == ["delegate_started", "output_received"]
    assert [row["resume_context_json"] for row in _rows(case, "clarification_requests")] == [card["resume_context_json"] for card in cards]
    projection = await read_reference_projection(
        case.fixture.db_path,
        scope=case.scope,
        turn_id="t_reference",
        visible_resource_identities=frozenset(),
        visibility_complete=True,
        cipher=case.runtime.cipher,
    )
    associations = projection["continuations"]
    assert [row["clarification_id"] for row in associations] == [card["request_id"] for card in cards]
    assert [row["resume_generation"] for row in associations] == resumed_generations
    assert {row["created_generation"] for row in _rows(case, "chat_reference_evidence")} == {case.runtime.generation}
    assert not await case.store.rollback_turn_resume_generation(
        turn_id="t_reference", expected_execution_generation=case.runtime.generation, owner_id=case.runtime.owner_id
    )


async def test_retry_projection_uses_latest_matching_notice_and_exact_invocation(lead_case, monkeypatch):
    import aiosqlite
    from bowhead_core.chat_reference_projection import _continuation_associations
    from bowhead_core.turn_log_store import TurnLogStore

    case = lead_case
    case.runtime = await _runtime(case)
    await _normal_lead(case, monkeypatch, clarification_tool=True)
    card = _rows(case, "clarification_requests")[0]
    await ClarificationStore(case.fixture.db_path).resolve(card["request_id"], "2026")
    assert await case.store.mark_chat_spawn_dispatch_terminal(turn_id="t_reference", owner_id=case.runtime.owner_id)
    execution_id = _rows(case, "turn_agent_execution")[0]["id"]
    # Projection inputs represent older, current and unrelated model receipts.
    invocations = [
        {"execution_id": execution_id, "created_generation": 2, "invocation_id": "inv_old"},
        {"execution_id": execution_id, "created_generation": 3, "invocation_id": "inv_current"},
        {"execution_id": "exec_other", "created_generation": 3, "invocation_id": "inv_other"},
    ]

    async def association():
        async with open_shared_ctx(case.fixture.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            values, invalid = await _continuation_associations(db, turn_id="t_reference", invocations=invocations)
        assert not invalid and len(values) == 1
        return values[0]

    assert (await association())["resume_generation"] is None
    log = TurnLogStore(case.fixture.db_path)
    for generation in (2, 3):
        assert await case.store.advance_turn_execution_generation(turn_id="t_reference", reference_clarification_id=card["request_id"]) == generation
        assert await log.record(
            turn_id="t_reference", event_type="turn.resumed", payload={"execution_generation": generation, "request_id": "cr_other"}
        )
        assert (await association())["resume_generation"] == (None if generation == 2 else 2)
        assert await log.record(
            turn_id="t_reference", event_type="turn.resumed", payload={"execution_generation": generation, "request_id": card["request_id"]}
        )
        actual = await association()
        assert actual["resume_generation"] == generation
        assert actual["model_invocation_ids"] == ["inv_old" if generation == 2 else "inv_current"]
        assert await case.store.rollback_turn_resume_generation(turn_id="t_reference", expected_execution_generation=generation, owner_id=None)


async def test_shared_store_serializes_budget_and_rejects_stale_or_repeated_questions(lead_case, monkeypatch):
    case = lead_case
    runtime = await _runtime(case)
    store = ClarificationStore(case.fixture.db_path)
    fields = {
        "turn_id": "t_reference",
        "user_id": case.scope.user_id,
        "workspace_id": case.scope.workspace_id,
        "session_id": case.scope.session_id,
        "kind": "ambiguous_turn",
        "token": "",
        "rationale": "Which period?",
        "candidates": [],
        "resume_context": {"user_text": "Analyze profit"},
        "bounded_question": {"generation": 1, "owner_id": runtime.owner_id, "questions": [], "fallback": "Which period?"},
    }
    results = await asyncio.gather(store.put(**fields), store.put(**fields), return_exceptions=True)
    cards = [value for value in results if not isinstance(value, Exception)]
    assert len(cards) == 1
    assert sum(isinstance(value, ValueError) for value in results) == 1
    from bowhead_core.agents.clarification_tool import _clarification_ask, is_clarification_sentinel

    ctx = _ctx(case)
    ctx._reference_runtime = runtime
    ctx.clarification_store = store
    assert not is_clarification_sentinel(await _clarification_ask({"fallback_text": "Which currency?"}, ctx))
    assert len(_rows(case, "clarification_requests")) == 1
    with sqlite3.connect(case.fixture.db_path) as db, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("UPDATE clarification_requests SET resume_context_json='{}' WHERE request_id=?", (cards[0].request_id,))
    await store.resolve(cards[0].request_id, chosen_value="2026")
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("UPDATE turns SET execution_generation=2 WHERE id='t_reference'")
        db.execute("UPDATE chat_turn_spawn_dispatch SET lease_execution_generation=2,lease_kind='resume' WHERE turn_id='t_reference'")
    with pytest.raises(ValueError, match="owner_unavailable"):
        await store.put(**fields)
    fields["bounded_question"]["generation"] = 2
    fields["bounded_question"]["fallback"] = "  WHICH   period?  "
    with pytest.raises(ValueError, match="question_repeated"):
        await store.put(**fields)
    fields["bounded_question"]["fallback"] = "Which currency?"
    fields["bounded_question"]["questions"] = [{"question": "Which currency?", "options": ["CNY", "USD"], "type": "single_select"}]
    second = await store.put(**fields)
    assert second.resume_context["question_round"]["prior_request_id"] == cards[0].request_id
    # The card's questions sit beside the hash-only marker for HTTP readers.
    assert second.resume_context["card_questions"] == fields["bounded_question"]["questions"]
    assert cards[0].resume_context["card_questions"] == []
