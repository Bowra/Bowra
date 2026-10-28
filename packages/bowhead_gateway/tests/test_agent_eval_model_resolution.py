"""Agent Eval's one model resolver: the role and a safe reason when an evaluation can't get a model."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.agent_eval_service import AgentEvalServiceError
from bowhead_gateway.routes._deps import RouterMiss
from bowhead_router.router import NoBindingError

from bowhead_gateway import agent_eval_runtime


class _Router:
    def __init__(self, bound: set[str]) -> None:
        self.bound = bound

    def resolve(self, agent_type: str):
        if agent_type not in self.bound:
            raise NoBindingError(agent_type)
        return SimpleNamespace(agent_type=agent_type, primary=("dashscope", "qwen3.6-plus"), fallback=[], profile=None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resolved", "reason"),
    [
        ((None, RouterMiss.UNCONFIGURED), "unconfigured"),
        ((None, RouterMiss.BUILD_FAILED), "build_failed"),
        ((None, RouterMiss.LEGACY_CONFIG), "legacy_config"),
        ((_Router({"judge"}), None), "role_unassigned"),
    ],
)
async def test_a_missing_model_names_the_role_and_why(monkeypatch, resolved, reason) -> None:
    async def resolve(*_args, **_kwargs):
        return resolved

    monkeypatch.setattr(agent_eval_runtime, "resolve_team_router", resolve)
    monkeypatch.setattr(agent_eval_runtime, "model_route_hash", lambda **_kwargs: "sha256:" + "1" * 64)

    with pytest.raises(AgentEvalServiceError) as refused:
        await agent_eval_runtime.resolve_eval_model(object(), team_id="team_1", role="analyst", site="test")

    assert refused.value.code == "agent_eval_model_unavailable"
    assert refused.value.details == {"model_role": "analyst", "model_reason": reason}


@pytest.mark.asyncio
async def test_an_assigned_role_resolves_to_its_route_hash(monkeypatch) -> None:
    router = _Router({"analyst"})

    async def resolve(*_args, **_kwargs):
        return router, None

    monkeypatch.setattr(agent_eval_runtime, "resolve_team_router", resolve)
    monkeypatch.setattr(agent_eval_runtime, "model_route_hash", lambda **kwargs: f"hash:{kwargs['role']}")

    assert await agent_eval_runtime.resolve_eval_model(object(), team_id="team_1", role="analyst", site="test") == (router, "hash:analyst")


@pytest.mark.asyncio
async def test_the_gate_checks_the_role_the_runtime_runs_a_team_agent_as() -> None:
    from bowhead_core.agent_model_policy import team_agent_model_role

    # A stored value outside the allowlist runs as the default role, so that is the role the gate must check.
    assert team_agent_model_role("lead") == "analyst"
    assert team_agent_model_role(None) == "analyst"
    assert team_agent_model_role("analyst") == "analyst"


@pytest.mark.asyncio
async def test_a_run_whose_model_went_away_reaches_its_gate_instead_of_retrying(monkeypatch) -> None:
    router = agent_eval_runtime._NoModelRouter("analyst")

    with pytest.raises(NoBindingError) as missing:
        router.resolve("analyst")
    assert missing.value.agent_type == "analyst"
    with pytest.raises(NoBindingError):
        await router.invoke("analyst")
