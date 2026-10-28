"""Exact Plan execution joins collaboration before the native runner starts."""

from types import SimpleNamespace

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agents.agent_runner_adapter import make_legacy_agent_runner_fn
from bowhead_core.agents.registry import AgentRegistry
from bowhead_core.agents.task_spec import TaskSpec
from bowhead_core.tool_registry import ToolExecutionContext
from test_agent_collaboration import collab as collab
from test_agent_collaboration_runner import RunnerFixture

from bowhead_router import LLMResponse

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize("exact", [True, False], ids=["exact-choice", "legacy-nonparticipant"])
async def test_exact_plan_binding_is_joined_before_model_input(collab, monkeypatch, exact):
    monkeypatch.setenv("FEATURE_AGENT_BEHAVIOR_M3", "1")

    async def provider(request):
        assert ("agent_message" in {schema.name for schema in request.tools}) == exact
        assert (request.invocation_execution_id is not None) == exact
        return LLMResponse(content="The supplied values total 1080 USD.")

    fixture = RunnerFixture(collab, provider)
    binding = collab.executions[collab.requester]
    fixture.runner.team_subagent_store = SimpleNamespace(db_path=str(collab.path))

    async def admitted(**kwargs):
        return binding if exact else None

    monkeypatch.setattr(fixture.runner, "admit_execution_binding", admitted)
    async with open_shared_ctx(collab.path) as db:
        await db.execute("DELETE FROM agent_collaboration_participant WHERE execution_id=?", (binding.execution_id,))
        await db.commit()
    worker = collab.worker
    context = ToolExecutionContext(
        workspace_id=worker.workspace_id,
        member_uid=worker.user_id,
        user_id=worker.user_id,
        turn_id=worker.turn_id,
        datarc=None,
        team_id=worker.team_id,
        session_id=worker.session_id,
        execution_owner_id=worker.owner_id,
        collaboration_runtime=fixture.runtime,
    )
    choice = SimpleNamespace(
        runtime_profile_name=binding.runtime_profile_name,
        agent_id=binding.agent_id,
        assignment_id=None,
        revision_ref=binding.revision_ref,
        release_id=None,
        behavior_fingerprint="sha256:" + "1" * 64,
    )
    adapter = make_legacy_agent_runner_fn(
        subagent_runner=fixture.runner, parent_turn_id=worker.turn_id, parent_bus=fixture.bus, parent_ctx=context, team_sourced_names=frozenset()
    )
    spec = TaskSpec(task_id="ps_exact", agent_name="compute", user_question="Calculate supplied values.", agent_choice=choice if exact else None)
    payload, _, _ = await adapter(
        AgentRegistry.bootstrap().get("compute"),
        spec,
        SimpleNamespace(serializable=SimpleNamespace(workspace_id=worker.workspace_id, user_id=worker.user_id)),
    )
    assert payload["fallback_markdown"] == "The supplied values total 1080 USD."
    async with (
        open_shared_ctx(collab.path) as db,
        db.execute(
            "SELECT e.status FROM turn_agent_execution e JOIN agent_collaboration_participant p ON p.execution_id=e.id WHERE e.id=?",
            (binding.execution_id,),
        ) as cursor,
    ):
        row = await cursor.fetchone()
        assert row[0] == "completed" if exact else row is None


async def test_plan_coordination_preserves_hard_tool_denial(collab):
    from bowhead_core.agent_collaboration import CollaborationUnavailable

    async def provider(request):
        raise AssertionError("A denied coordinator cannot call the model")

    fixture = RunnerFixture(collab, provider)
    fixture.runner.tool_deny = frozenset({"agent_message"})
    worker = collab.worker
    context = ToolExecutionContext(
        workspace_id=worker.workspace_id,
        member_uid=worker.user_id,
        user_id=worker.user_id,
        turn_id=worker.turn_id,
        datarc=None,
        team_id=worker.team_id,
        session_id=worker.session_id,
        execution_owner_id=worker.owner_id,
        collaboration_runtime=fixture.runtime,
    )

    async def work():
        raise AssertionError("A denied Plan must not dispatch steps")

    with pytest.raises(CollaborationUnavailable, match="collaboration_tool_denied"):
        await fixture.runner.run_plan_collaboration(
            work, ctx=context, bus=fixture.bus, binding=collab.executions[collab.lead], task="Verify revenue."
        )
