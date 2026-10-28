"""The verdict preview grades unconfirmed points on the Live answer and stores nothing (PRD R21, R27)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from bowhead_core.agent_eval_service import AgentEvalServiceError
from bowhead_gateway.agent_eval_runtime import AgentEvalRuntime
from bowhead_gateway.routes import agents
from fastapi import HTTPException

_ANSWER = {
    "run_id": "aer_live",
    "case_revision_id": "aec_1",
    "attempt": 2,
    "answer_ref": "attempt:aer_live:aec_1:2",
    "answered_at": "2026-10-18T00:00:00Z",
    "answer": "Revenue grew 12% on the same month last year. East China reached 1.2 billion.",
    "answer_truncated": False,
}


class _Router:
    def __init__(self, content: dict) -> None:
        self.content = content
        self.calls: list[object] = []

    async def invoke(self, role: str, request) -> SimpleNamespace:
        self.calls.append((role, request))
        return SimpleNamespace(content=json.dumps(self.content), usage=None, provider="p", model="m")


class _Runtime(AgentEvalRuntime):
    judge_configured = True

    async def model_hash(self, *, team_id: str, role: str) -> str:
        if not self.judge_configured:
            raise AgentEvalServiceError("team_model_unconfigured")
        return "sha256:" + "1" * 64


def _runtime(*, judge_configured: bool = True) -> AgentEvalRuntime:
    runtime = _Runtime.__new__(_Runtime)
    runtime.judge_configured = judge_configured
    return runtime


def _request(runtime: AgentEvalRuntime) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(agent_eval_runtime=runtime)))


def _body(*points: dict) -> agents.AgentEvalPointPreviewBody:
    return agents.AgentEvalPointPreviewBody(task="How did revenue change?", points=list(points))


_JUDGMENT = {"id": "p_0001", "kind": "judgment", "text": "Compares with the same month last year"}
_FACT = {"id": "p_0002", "kind": "data_fact", "text": "States East China revenue"}


def _patch(monkeypatch, *, answer: dict | None, router: _Router | None) -> list[dict]:
    lookups: list[dict] = []

    class _Product:
        def __init__(self, _db_path: str) -> None:
            pass

        async def latest_live_answer(self, **kwargs):
            lookups.append(kwargs)
            return answer

    async def _resolve(_state, team_id, *, agent_type, site):
        assert agent_type == "judge" and site == "agent_eval_point_preview"
        return router, None

    monkeypatch.setattr(agents, "AgentEvalProductStore", _Product)
    monkeypatch.setattr(agents, "resolve_team_router", _resolve)
    return lookups


_USER = SimpleNamespace(id="user_owner", team_id="team_1")
_STORE = SimpleNamespace(db_path="unused")


@pytest.mark.anyio
async def test_one_judge_call_previews_every_point_with_its_quote(monkeypatch) -> None:
    router = _Router(
        {
            "points": [
                {"id": "p_0001", "met": True, "quote": "Revenue grew 12% on the same month last year."},
                {"id": "p_0002", "met": True, "quote": "East China reached 1.2 billion.", "value": "1.2 billion"},
            ]
        }
    )
    lookups = _patch(monkeypatch, answer=_ANSWER, router=router)

    result = await agents.preview_agent_eval_points("osa_1", _body(_JUDGMENT, _FACT), _request(_runtime()), user=_USER, store=_STORE)

    assert lookups == [{"team_id": "team_1", "agent_id": "osa_1", "actor_user_id": "user_owner", "task": "How did revenue change?"}]
    assert len(router.calls) == 1
    assert result["answer"] == _ANSWER
    judgment, fact = result["points"]
    assert judgment == {"id": "p_0001", "met": True, "quote": "Revenue grew 12% on the same month last year.", "value": None, "reason": None}
    # A data fact is decided against the run's Tool results, which a preview doesn't have.
    assert fact["met"] is None and fact["value"] == "1.2 billion" and fact["reason"] == "preview_value_only"


@pytest.mark.anyio
async def test_a_quote_missing_from_the_answer_is_not_graded(monkeypatch) -> None:
    router = _Router({"points": [{"id": "p_0001", "met": True, "quote": "A sentence the Agent never wrote."}]})
    _patch(monkeypatch, answer=_ANSWER, router=router)

    result = await agents.preview_agent_eval_points("osa_1", _body(_JUDGMENT), _request(_runtime()), user=_USER, store=_STORE)

    assert result["points"][0]["met"] is None


@pytest.mark.anyio
async def test_without_a_live_answer_nothing_is_graded(monkeypatch) -> None:
    router = _Router({"points": []})
    _patch(monkeypatch, answer=None, router=router)

    result = await agents.preview_agent_eval_points("osa_1", _body(_JUDGMENT), _request(_runtime()), user=_USER, store=_STORE)

    assert result == {"kind": "agent_eval_point_preview", "schema_version": 1, "answer": None, "points": []}
    assert router.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("router_configured", [False, True])
async def test_without_an_evaluation_model_points_read_not_graded(monkeypatch, router_configured) -> None:
    router = _Router({"points": []}) if router_configured else None
    _patch(monkeypatch, answer=_ANSWER, router=router)

    result = await agents.preview_agent_eval_points("osa_1", _body(_JUDGMENT), _request(_runtime(judge_configured=False)), user=_USER, store=_STORE)

    assert result["answer"] == _ANSWER
    assert result["points"] == [{"id": "p_0001", "met": None, "quote": None, "value": None, "reason": "agent_eval_judge_unavailable"}]
    assert router is None or router.calls == []


@pytest.mark.anyio
async def test_duplicate_point_ids_are_rejected(monkeypatch) -> None:
    _patch(monkeypatch, answer=_ANSWER, router=_Router({"points": []}))

    with pytest.raises(HTTPException) as caught:
        await agents.preview_agent_eval_points("osa_1", _body(_JUDGMENT, _JUDGMENT), _request(_runtime()), user=_USER, store=_STORE)
    assert caught.value.status_code == 422


def test_only_model_graded_points_can_be_previewed() -> None:
    with pytest.raises(ValueError):
        agents.AgentEvalPointPreviewBody(task="Task", points=[{"id": "p_0001", "kind": "required_action", "text": "Queries data"}])


def test_suggested_cases_carry_suggested_points_only_for_real_tools() -> None:
    [case] = agents._suggested_eval_cases(
        {
            "cases": [
                {
                    "task": "How did East China revenue change last quarter?",
                    "points": [
                        {"kind": "judgment", "text": "Compares with the same quarter last year"},
                        {"kind": "required_action", "text": "Queries sales data", "tool": "bi_query"},
                        {"kind": "required_action", "text": "Uses a made-up tool", "tool": "made_up"},
                    ],
                }
            ]
        },
        allowed_tools=["bi_query"],
    )

    points = case["expected_contract"]["points"]
    assert [point["kind"] for point in points] == ["judgment", "required_action"]
    assert {point["state"] for point in points} == {"suggested"} and {point["source"] for point in points} == {"coach_task"}
    agents.AgentEvalCaseBody.model_validate(case).domain()
