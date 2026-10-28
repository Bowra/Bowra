"""Background evaluation work never inherits the scheduling chat Turn's usage scope.

Coach starts Agent CI from inside a chat Turn, which binds a Turn-scoped physical
usage observer and usage attribution in contextvars. ``asyncio.create_task`` copies
the caller's context, so evaluation tasks scheduled there must start from the same
empty scope the worker and Owner paths use; otherwise every evaluation model call is
refused by the Coach Turn's observer and its usage is attributed to the Coach Turn.
"""

import asyncio
from types import SimpleNamespace

import pytest
from bowhead_core.chat_reference_storage import ReferenceCipher
from bowhead_core.physical_usage import PhysicalUsageObserver
from bowhead_gateway.agent_eval_runtime import AgentEvalRuntime
from bowhead_router.invocation import begin_physical_invocation, physical_observation_active, physical_observation_scope
from bowhead_router.usage import current_usage_context, record_usage, usage_context
from cryptography.fernet import Fernet

pytestmark = pytest.mark.unit


def _coach_turn_observer() -> PhysicalUsageObserver:
    return PhysicalUsageObserver(
        "unused",
        cipher=ReferenceCipher(Fernet.generate_key().decode()),
        turn_id="t_coach",
        generation=1,
    )


async def _evaluation_model_call(observed: dict) -> None:
    observed["observer_active"] = physical_observation_active()
    observed["inherited_turn_id"] = current_usage_context().turn_id
    # The evaluation service binds its own scratch Turn around each attempt.
    with usage_context(team_id="team_test", user_id="u_owner", workspace_id="ws_test", turn_id="t_eval_attempt", reason="agent_eval"):
        observed["attempt"] = await begin_physical_invocation(
            provider="openai",
            model="gpt-test",
            adapter_contract_key="test",
            mode="invoke",
            request={"messages": []},
        )


async def test_evaluation_scheduled_inside_a_chat_turn_runs_outside_its_usage_scope(monkeypatch) -> None:
    observed: dict = {}
    coach_usage: list = []

    async def fake_execute(self, run_id: str) -> None:
        await _evaluation_model_call(observed)
        record_usage(agent_type="eval", provider="openai", model="gpt-test", tokens_input=1, tokens_output=1)

    monkeypatch.setattr(AgentEvalRuntime, "_execute", fake_execute)
    runtime = AgentEvalRuntime(db_path="unused", state=SimpleNamespace())
    with (
        physical_observation_scope(_coach_turn_observer()),
        usage_context(
            team_id="team_test",
            user_id="u_owner",
            workspace_id="ws_test",
            turn_id="t_coach",
            reason="turn",
            turn_usage_sink=coach_usage.append,
        ),
    ):
        runtime.schedule_execution(run_id="aer_test")
    await asyncio.gather(*runtime._tasks.values())

    assert observed == {"observer_active": False, "inherited_turn_id": None, "attempt": None}
    assert coach_usage == []


async def test_point_calibration_scheduled_inside_a_chat_turn_is_not_billed_to_it(monkeypatch) -> None:
    observed: dict = {}

    async def fake_calibrate(self, *, team_id: str, agent_id: str) -> None:
        observed["observer_active"] = physical_observation_active()
        observed["inherited_turn_id"] = current_usage_context().turn_id

    monkeypatch.setattr(AgentEvalRuntime, "_calibrate_points", fake_calibrate)
    runtime = AgentEvalRuntime(db_path="unused", state=SimpleNamespace())
    with (
        physical_observation_scope(_coach_turn_observer()),
        usage_context(team_id="team_test", user_id="u_owner", workspace_id="ws_test", turn_id="t_coach", reason="turn"),
    ):
        runtime.schedule_point_calibration(team_id="team_test", agent_id="sa_test")
    await asyncio.gather(*runtime._tasks.values())

    assert observed == {"observer_active": False, "inherited_turn_id": None}
