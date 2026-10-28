from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.agent_eval_judge import TeamRouterEvalJudge
from bowhead_gateway.agent_eval_runtime import AgentEvalRuntime

from bowhead_gateway import agent_eval_runtime


def _run(judge_role_ref: str | None) -> SimpleNamespace:
    return SimpleNamespace(subject=SimpleNamespace(team_id="team_1"), judge_role_ref=judge_role_ref)


@pytest.mark.parametrize(
    ("judge_role_ref", "judge_router", "wired"), [("judge", "judge-router", True), ("judge", None, False), (None, "judge-router", False)]
)
@pytest.mark.anyio
async def test_the_service_grades_points_with_the_team_evaluation_model(monkeypatch, judge_role_ref, judge_router, wired) -> None:
    # This isolated wiring test has no Datarc run identity or instance encryption root.
    monkeypatch.setenv("FEATURE_AGENT_EVAL_DATARC_READ_ONLY", "0")
    sites: list[tuple[str, str]] = []

    class _Store:
        def __init__(self, _db_path: str) -> None:
            pass

        async def read_run(self, _run_id: str) -> SimpleNamespace:
            return _run(judge_role_ref)

    async def _resolve(_state, team_id, *, agent_type, site):
        sites.append((agent_type, site))
        return (judge_router if agent_type == "judge" else "subject-router"), None

    async def _role(_db_path, _run):
        return "analyst"

    monkeypatch.setattr(agent_eval_runtime, "AgentEvalStore", _Store)
    monkeypatch.setattr(agent_eval_runtime, "resolve_team_router", _resolve)
    monkeypatch.setattr(agent_eval_runtime, "_model_role", _role)
    state = SimpleNamespace(tool_registry=None, user_store=None, access_control_store=None, session_store=None, ordered_turn_log_sink=None)

    service = await AgentEvalRuntime(db_path="unused", state=state)._service("aer_1")

    if wired:
        assert isinstance(service.judge, TeamRouterEvalJudge) and service.judge.router == "judge-router"
        assert ("judge", "agent_eval_judge") in sites
    else:
        assert service.judge is None
