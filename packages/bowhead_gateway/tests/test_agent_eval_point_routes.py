"""HTTP boundary of point calibration: Judged wrongly, Mark as good answer, point status, unmet points."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.user_store import UserStore
from bowhead_gateway.agent_eval_runtime import AgentEvalRuntime
from bowhead_gateway.routes import agents
from bowhead_gateway.routes.auth import _current_user, _store
from fastapi import FastAPI
from test_agent_eval_run_insights_routes import _USERS, _seed

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "test-agent-eval-point-routes-jwt-secret")
    db_path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    _seed(db_path)
    scheduled: list[tuple[str, str]] = []

    def _schedule(self, *, team_id: str, agent_id: str) -> None:
        scheduled.append((team_id, agent_id))

    monkeypatch.setattr(AgentEvalRuntime, "schedule_point_calibration", _schedule)
    current = {"user": _USERS["owner"]}
    app = FastAPI()
    app.state.agent_eval_runtime = AgentEvalRuntime(db_path=str(db_path), state=SimpleNamespace())
    app.include_router(agents.router, prefix="/api/v1/agents")
    app.dependency_overrides[_current_user] = lambda: current["user"]
    app.dependency_overrides[_store] = lambda: UserStore(db_path)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as http:
        yield http, current, scheduled


_FEEDBACK = "/api/v1/agents/osa_1/eval-points/p_0a1b/feedback"
_EXAMPLES = "/api/v1/agents/osa_1/eval-points/p_0a1b/examples"
_RESULT = {"case_result": {"run_id": "aer_1", "case_revision_id": "aec_1", "attempt": 1}}


@pytest.mark.anyio
@pytest.mark.parametrize("who", ["reviewer", "team_admin", "other_team"])
async def test_only_the_agent_owner_can_dispute_a_verdict_or_mark_a_good_answer(client, who) -> None:
    http, current, scheduled = client
    current["user"] = _USERS[who]

    feedback = await http.post(_FEEDBACK, json=_RESULT)
    example = await http.post(_EXAMPLES, json={"kind": "good", "case_revision_id": "aec_1", "answer_ref": "attempt:aer_1:aec_1:1"})

    assert feedback.status_code == example.status_code == 404
    assert feedback.json()["detail"]["error"]["code"] == "agent_eval_action_denied"
    assert scheduled == []


@pytest.mark.anyio
async def test_a_verdict_outside_the_owners_evaluations_is_an_actionable_error(client) -> None:
    http, _current, scheduled = client

    response = await http.post(_FEEDBACK, json=_RESULT)

    assert response.status_code == 422
    assert response.json()["detail"]["error"]["code"] == "agent_eval_case_not_found"
    assert scheduled == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        {"kind": "bad", "case_revision_id": "aec_1", "answer_ref": "attempt:aer_1:aec_1:1"},
        {"kind": "good", "case_revision_id": "aec_1", "answer_ref": "attempt:aer_1:aec_1:1", "met": True},
    ],
)
async def test_only_a_good_answer_can_be_marked_from_outside(client, body) -> None:
    http, _current, _scheduled = client

    assert (await http.post(_EXAMPLES, json=body)).status_code == 422


@pytest.mark.anyio
async def test_unmet_points_are_owner_only(client) -> None:
    http, current, _scheduled = client
    path = "/api/v1/agents/osa_1/release-candidates/arc_1/comparisons/aex_1/unmet-points"
    current["user"] = _USERS["reviewer"]

    denied = await http.get(path)
    current["user"] = _USERS["owner"]
    missing = await http.get(path)

    assert denied.status_code in {403, 404}
    assert missing.status_code == 404


@pytest.mark.anyio
async def test_point_status_is_attached_per_suite_and_calibration_starts_when_needed(monkeypatch) -> None:
    scheduled: list[str] = []
    seen: dict[str, Any] = {}

    class _Runtime:
        async def judge_model_hash(self, *, team_id: str) -> str | None:
            return "sha256:" + "a" * 64

        def schedule_point_calibration(self, *, team_id: str, agent_id: str) -> None:
            scheduled.append(agent_id)

    class _Store:
        def __init__(self, _db_path: str) -> None:
            pass

        async def suite_point_status(self, *, team_id, agent_id, suite_revision_ids, model_fingerprint):
            seen.update(suite_revision_ids=suite_revision_ids, model_fingerprint=model_fingerprint)
            return {"aes_1": {"p_0a1b": {"counts": False, "reason": "calibrating", "rewording": None}}}

    monkeypatch.setattr(agents, "AgentEvalPointStore", _Store)
    snapshot = {
        "suites": [
            {"suite_id": "aes_1", "revision": {"suite_revision_id": "aev_1"}},
            {"suite_id": "aes_2", "revision": None},
        ]
    }

    await agents._attach_point_status(_Runtime(), "unused", team_id="team_1", agent_id="osa_1", snapshot=snapshot)  # type: ignore[arg-type]

    assert seen == {"suite_revision_ids": {"aes_1": "aev_1"}, "model_fingerprint": "sha256:" + "a" * 64}
    assert snapshot["suites"][0]["point_status"] == {"p_0a1b": {"counts": False, "reason": "calibrating", "rewording": None}}
    assert snapshot["suites"][1]["point_status"] == {}
    assert scheduled == ["osa_1"]


@pytest.mark.anyio
async def test_background_calibration_uses_the_team_judge_and_coach_routers(monkeypatch) -> None:
    from bowhead_gateway import agent_eval_runtime

    calls: list[Any] = []
    task = SimpleNamespace(owner_user_id="owner_1", suite_id="aes_1", point={"id": "p_0a1b"})

    class _Store:
        def __init__(self, _db_path: str) -> None:
            pass

        async def calibration_tasks(self, *, team_id, agent_id, model_fingerprint):
            return [task]

    class _Calibrator:
        def __init__(self, *, store, judge, writer) -> None:
            calls.append((judge.router, writer.router))

        async def calibrate(self, item, *, model_fingerprint):
            calls.append((item, model_fingerprint))
            return SimpleNamespace(state="calibrated", failure_reason=None)

    async def _resolve(_state, team_id, *, agent_type, site):
        assert site == "agent_eval_calibration"
        return f"{agent_type}-router", None

    async def _hash(self, *, team_id: str) -> str:
        return "sha256:" + "a" * 64

    monkeypatch.setattr(agent_eval_runtime, "AgentEvalPointStore", _Store)
    monkeypatch.setattr(agent_eval_runtime, "PointCalibrator", _Calibrator)
    monkeypatch.setattr(agent_eval_runtime, "resolve_team_router", _resolve)
    monkeypatch.setattr(AgentEvalRuntime, "judge_model_hash", _hash)

    await AgentEvalRuntime(db_path="unused", state=SimpleNamespace())._calibrate_points(team_id="team_1", agent_id="osa_1")

    assert calls == [("judge-router", "echo-router"), (task, "sha256:" + "a" * 64)]


@pytest.mark.anyio
async def test_point_status_failure_leaves_the_eval_manager_read_intact() -> None:
    class _Runtime:
        async def judge_model_hash(self, *, team_id: str) -> str | None:
            raise RuntimeError("router registry unavailable")

        def schedule_point_calibration(self, *, team_id: str, agent_id: str) -> None:
            raise AssertionError("nothing to schedule without a status")

    snapshot = {"suites": [{"suite_id": "aes_1", "revision": {"suite_revision_id": "aev_1"}}]}

    await agents._attach_point_status(_Runtime(), "unused", team_id="team_1", agent_id="osa_1", snapshot=snapshot)  # type: ignore[arg-type]

    assert "point_status" not in snapshot["suites"][0]
