"""Cases from past conversations: the question is the task, Coach only drafts suggested points (R14)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bowhead_core.agent_eval_conversation_cases import AgentEvalConversationError
from bowhead_core.agent_eval_points import ExpectedPoint
from bowhead_core.agent_eval_store import EvalCaseDraft
from bowhead_gateway.routes import agents
from fastapi import HTTPException

pytestmark = pytest.mark.unit

USER = SimpleNamespace(id="usr_owner", team_id="team_1")
STORE = SimpleNamespace(db_path="unused.db")


def _turns(count: int) -> list[dict[str, str]]:
    return [{"turn_id": f"t_{index}", "question": f"Question {index}?", "answer": f"Answer {index}."} for index in range(count)]


def _model(*responses):
    return SimpleNamespace(invoke=AsyncMock(side_effect=list(responses)))


def _content(payload) -> SimpleNamespace:
    return SimpleNamespace(content=json.dumps(payload))


async def _suggest(monkeypatch, turns, model):
    monkeypatch.setattr(agents, "read_conversation_turns", AsyncMock(return_value=turns))
    monkeypatch.setattr(agents, "resolve_team_router", AsyncMock(return_value=(model, None)))
    return await agents._suggest_eval_cases_from_turns(
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
        user=USER,
        store=STORE,
        agent_id="osa_1",
        turn_ids=[item["turn_id"] for item in turns],
    )


async def test_each_turn_becomes_a_case_with_its_question_and_suggested_points(monkeypatch) -> None:
    payload = {
        "cases": [
            {
                "turn_id": "t_0",
                "points": [
                    {"kind": "judgment", "text": "Compares with the same month last year"},
                    {"kind": "data_fact", "text": "States last quarter's revenue for East China"},
                    # Dropped: only model-graded kinds are drafted from conversations.
                    {"kind": "required_action", "text": "Queries sales data"},
                    {"kind": "judgment", "text": "   "},
                ],
            },
            # Dropped: not a selected turn.
            {"turn_id": "t_other", "points": [{"kind": "judgment", "text": "Anything"}]},
        ]
    }
    cases = await _suggest(monkeypatch, _turns(2), _model(_content(payload)))

    assert [case["task"] for case in cases] == ["Question 0?", "Question 1?"]
    points = cases[0]["expected_contract"]["points"]
    assert [(point["kind"], point["text"]) for point in points] == [
        ("judgment", "Compares with the same month last year"),
        ("data_fact", "States last quarter's revenue for East China"),
    ]
    assert {(point["source"], point["state"]) for point in points} == {("coach_conversation", "suggested")}
    for point in points:
        ExpectedPoint.model_validate(point)
    # A turn the model said nothing about keeps its case, without points.
    assert "points" not in cases[1]["expected_contract"]
    # Every drafted case saves through the normal case contract.
    for case in cases:
        EvalCaseDraft(input={"task": case["task"]}, **{key: value for key, value in case.items() if key != "task"})


async def test_a_failed_chunk_keeps_its_cases_and_other_chunks_still_get_points(monkeypatch) -> None:
    turns = _turns(10)  # two chunks of up to eight
    good = _content({"cases": [{"turn_id": "t_8", "points": [{"kind": "judgment", "text": "Names the source"}]}]})

    async def invoke(_agent_type, request):
        if "t_0" in request.messages[0].content:
            raise TimeoutError
        return good

    cases = await _suggest(monkeypatch, turns, SimpleNamespace(invoke=invoke))
    assert len(cases) == 10
    assert all("points" not in case["expected_contract"] for case in cases[:8])
    assert cases[8]["expected_contract"]["points"][0]["text"] == "Names the source"


async def test_points_per_case_stop_at_six(monkeypatch) -> None:
    payload = {"cases": [{"turn_id": "t_0", "points": [{"kind": "judgment", "text": f"Point {index}"} for index in range(9)]}]}
    cases = await _suggest(monkeypatch, _turns(1), _model(_content(payload)))
    assert len(cases[0]["expected_contract"]["points"]) == 6


async def test_without_a_team_model_nothing_is_drafted(monkeypatch) -> None:
    monkeypatch.setattr(agents, "read_conversation_turns", AsyncMock(return_value=_turns(1)))
    monkeypatch.setattr(agents, "resolve_team_router", AsyncMock(return_value=(None, "miss")))
    with pytest.raises(agents.AgentEvalStoreError) as error:
        await agents._suggest_eval_cases_from_turns(
            SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())), user=USER, store=STORE, agent_id="osa_1", turn_ids=["t_0"]
        )
    assert error.value.code == "team_model_unconfigured"


def test_an_unreadable_conversation_is_a_not_found() -> None:
    with pytest.raises(HTTPException) as error:
        agents._raise_eval_http(AgentEvalConversationError("agent_eval_conversation_unavailable"))
    assert error.value.status_code == 404
    with pytest.raises(HTTPException) as error:
        agents._raise_eval_http(AgentEvalConversationError("agent_eval_conversation_selection_invalid"))
    assert error.value.status_code == 422


def test_the_request_accepts_at_most_thirty_two_turns() -> None:
    agents.AgentEvalSuggestBody(from_turns=[f"t_{index}" for index in range(32)])
    with pytest.raises(ValueError):
        agents.AgentEvalSuggestBody(from_turns=[f"t_{index}" for index in range(33)])
    with pytest.raises(ValueError):
        agents.AgentEvalSuggestBody(from_turns=[])
