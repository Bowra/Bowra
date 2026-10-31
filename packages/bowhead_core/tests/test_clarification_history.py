"""Historical questions retain context without crossing access or execution boundaries."""

import json
import sqlite3
from types import SimpleNamespace

import pytest
from bowhead_core.clarification_rounds import CONTEXT_KEY, QUESTIONS_KEY, question_hashes, validate_answer_questions
from bowhead_core.clarification_store import ClarificationStore
from bowhead_core.context_assembly import build_turn_history
from test_coach_operation_history import history_db as history_db

QUESTIONS = [
    {"question": "Which city?", "type": "free_text", "options": []},
    {"question": "Which sections?", "type": "multi_select", "options": ["Forecast", "Travel"]},
]
ANSWERS = [
    {"question": "Which sections?", "source": "candidate", "answer": "Travel", "option_index": 1, "skipped": False},
    {"question": "Which city?", "source": "custom", "answer": "Singapore", "skipped": False},
    {"question": "Which sections?", "source": "candidate", "answer": "Forecast", "option_index": 0, "skipped": False},
]


@pytest.mark.unit
def test_multiple_selections_preserve_question_identity_independent_of_order():
    context = {CONTEXT_KEY: {"question_hashes": question_hashes(QUESTIONS, None)}, QUESTIONS_KEY: QUESTIONS}
    validate_answer_questions(ANSWERS, context)
    with pytest.raises(ValueError, match="clarification_question_repeated"):
        question_hashes([QUESTIONS[0], QUESTIONS[0]], None)


@pytest.mark.unit
@pytest.mark.parametrize("change", ["missing", "foreign", "single", "skip_mix", "duplicate"])
def test_answer_grouping_retains_original_question_constraints(change):
    questions = [dict(question) for question in QUESTIONS]
    answers = [dict(answer) for answer in ANSWERS]
    if change == "missing":
        answers.pop(1)
    elif change == "foreign":
        answers[1]["question"] = "Unrelated question"
    elif change == "single":
        questions[1]["type"] = "single_select"
    elif change == "skip_mix":
        answers.append({"question": "Which sections?", "source": "skip", "skipped": True})
    else:
        answers.append(answers[0])
    context = {CONTEXT_KEY: {"question_hashes": question_hashes(questions, None)}, QUESTIONS_KEY: questions}
    with pytest.raises(ValueError, match="clarification_answer_changed"):
        validate_answer_questions(answers, context)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_history_reads_only_prior_owned_questions_and_never_changes_cards(history_db):
    fixture, store, _log, scope = history_db
    cards = ClarificationStore(fixture.db_path)
    await cards.initialize()
    card = await cards.put(
        turn_id=fixture.turn_id,
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        session_id=scope.session_id,
        token="",
        rationale="Weather request",
        candidates=[],
        kind="ambiguous_turn",
        resume_context={"__schema_version": 2, "card_questions": QUESTIONS, "secret": "must-not-enter-history"},
    )
    await cards.resolve_once(card.request_id, chosen_value="Singapore", answers=ANSWERS)
    with sqlite3.connect(fixture.db_path) as db:
        db.execute("UPDATE turns SET status='clarification_pending' WHERE id=?", (fixture.turn_id,))
    await store.finalize_turn(session_id=scope.session_id, current_turn_id=fixture.turn_id, next_turn_id=None)
    await store.record_turn(turn_id="t_followup", session_id=scope.session_id, user_text="Make it a report instead.")
    original = await cards.get(card.request_id)
    projected = await store.load_clarification_history(scope, "t_followup")
    assert list(projected) == [fixture.turn_id]
    assert projected[fixture.turn_id][0]["questions"] == QUESTIONS
    assert projected[fixture.turn_id][0]["answers"] == ANSWERS
    assert projected[fixture.turn_id][0]["status"] == "resolved"
    assert "must-not-enter-history" not in json.dumps(projected)
    assert await cards.get(card.request_id) == original
    assert (await store.get_turn(fixture.turn_id)).status == "clarification_pending"
    assert await store.load_clarification_history(scope, fixture.turn_id) == {}
    for field in ("user_id", "team_id", "workspace_id", "session_id"):
        with pytest.raises(ValueError, match="reference_scope_unavailable"):
            await store.load_clarification_history(scope.model_copy(update={field: "unrelated"}), "t_followup")
    with sqlite3.connect(fixture.db_path) as db:
        db.execute("UPDATE workspace_members SET status='revoked' WHERE workspace_id=? AND user_id=?", (scope.workspace_id, scope.user_id))
    with pytest.raises(ValueError, match="reference_scope_unavailable"):
        await store.load_clarification_history(scope, "t_followup")


@pytest.mark.unit
def test_pending_context_uses_whole_turn_budget_and_never_replays_tools():
    turn = SimpleNamespace(id="t_weather", seq=1, status="clarification_pending", user_text="Tomorrow's weather presentation", final_answer="")
    projected = {turn.id: [{"status": "resolved", "questions": QUESTIONS, "answers": ANSWERS}]}
    kwargs = {
        "exclude_turn_id": "t_followup",
        "clarifications": projected,
        "estimate": len,
        "spawns": {turn.id: [SimpleNamespace(call_id="previous-tool")]},
    }
    history = build_turn_history([turn], budget_tokens=0, **kwargs)
    assert history.turn_sizes == [2] and history.incomplete_turns == 1
    assert history.messages[0]["content"] == turn.user_text
    assert "does not resume" in history.messages[1]["content"]
    assert "Singapore" in history.messages[1]["content"]
    assert all(message["role"] != "tool" and "tool_calls" not in message for message in history.messages)
    limited = build_turn_history([turn], budget_tokens=1, **kwargs)
    assert limited.messages == [] and limited.turns_dropped == 1 and limited.incomplete_turns == 0
