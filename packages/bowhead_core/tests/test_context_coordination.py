"""Compaction inside the original bounded coordination reply frame."""

from copy import deepcopy

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_collaboration_runtime import CollaborationExecution, CollaborationRuntime, collaboration_tool_spec
from bowhead_core.agent_run_limits import AgentLoopBudget
from bowhead_core.events import TurnEventBus
from bowhead_core.session_store import SessionStore
from bowhead_core.subagent import SubagentProfile, SubagentRunner, _RuntimeExecution
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry
from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore
from bowhead_router.errors import ProviderCallError, ProviderFailureKind
from bowhead_router.invocation import begin_physical_invocation
from bowhead_router.types import LLMRequest, LLMResponse, Message, TokenUsage, ToolCall
from cryptography.fernet import Fernet
from test_agent_collaboration import collab as collaboration_fixture
from test_context_maintenance import context_case as maintenance_fixture

collab = collaboration_fixture
context_case = maintenance_fixture
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("mode", ["enabled", "disabled", "disabled_capacity", "disabled_overflow", "overflow", "commit_failure"])
async def test_coordination_uses_its_lane_original_decisions_and_native_reply(collab, context_case, monkeypatch, mode):
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    case = context_case
    policy = case.registry.definition_for("provider")["models"][0]["context_policy"]
    policy["enabled"] = not mode.startswith("disabled")
    if mode.endswith("overflow"):
        policy["trigger_input_tokens"] = 30000
    question = await collab.ask(recipient_id=collab.lead)
    runtime = CollaborationRuntime(collab.store, collab.worker, authorize=collab.authorize)
    registry = ToolRegistry()
    registry.register(collaboration_tool_spec())
    runner = SubagentRunner(router=case.router, tool_registry=registry, backend="legacy")
    log = TurnLogStore(collab.path)
    sink = OrderedTurnLogSink(log)
    bus = TurnEventBus(log_sink=sink)
    worker = collab.worker
    ctx = ToolExecutionContext(
        workspace_id=worker.workspace_id,
        member_uid=worker.user_id,
        user_id=worker.user_id,
        turn_id=worker.turn_id,
        datarc=None,
        team_id=worker.team_id,
        session_id=worker.session_id,
        execution_owner_id=worker.owner_id,
        execution_generation=worker.generation,
        session_store=SessionStore(collab.path),
        role="lead",
        subagent_runner=runner,
        parent_bus=bus,
        collaboration_runtime=runtime,
        collaboration_execution=CollaborationExecution(runtime, collab.lead),
    )
    adapter = case.registry.get("provider")
    sent = []

    async def invoke(request):
        body = adapter._build_body(request, stream=False)
        attempt = await begin_physical_invocation(
            provider="provider", model="model", adapter_contract_key="context-test.v1", mode="invoke", request=body, input_request=request
        )
        assert attempt is not None
        sent.append(deepcopy(request))
        if mode.endswith("overflow") and len(sent) == 1:
            await attempt.confirmed_not_applied(evidence_code="context_overflow", evidence_digest="rejected-before-execution")
            raise ProviderCallError(kind=ProviderFailureKind.CONTEXT_OVERFLOW, status_code=400)
        response = LLMResponse(
            content="Earlier coordination established the original scope." if request.summary_only else "",
            tool_calls=[]
            if request.summary_only
            else [
                ToolCall(
                    id="reply",
                    name="agent_message",
                    arguments={"action": "reply", "question_id": question["operation_id"], "body": "Use the original quarter."},
                )
            ],
            finish_reason="stop" if request.summary_only else "tool_calls",
            usage=TokenUsage(input_tokens=10, output_tokens=4, cached_tokens=2),
        )
        await attempt.complete(usage=response.usage, response_digest="response")
        return response

    monkeypatch.setattr(adapter, "invoke", invoke)
    if mode == "commit_failure":
        import bowhead_core.context_execution as module

        original_write = module.write_context_in_tx

        async def fail_commit(*args, **kwargs):
            if kwargs.get("completion") is not None:
                raise OSError("injected_commit_failure")
            return await original_write(*args, **kwargs)

        monkeypatch.setattr(module, "write_context_in_tx", fail_commit)
    controller = _RuntimeExecution(AgentLoopBudget(contract="agent-runtime-budget.v1", step_limit=5))
    observed_usage = []
    prefix = [Message(role="user", content="Original business task is waiting for its delegated result.")]
    history = deepcopy(case.request.messages[:8])
    if mode == "disabled_capacity":
        for message in history:
            message.content *= 5
    original_history = deepcopy(history)
    args = {
        "context": await runtime.context(collab.lead),
        "accepted_request": LLMRequest(model="", messages=prefix, invocation_execution_id=collab.lead),
        "profile": SubagentProfile(name="lead", llm_agent_type="lead", tool_whitelist=(), system_prompt="Complete the original assignment."),
        "ctx": ctx,
        "bus": bus,
        "subagent_id": "sub_owner",
        "user_pref": None,
        "timeout": 10,
        "window_guard": lambda *args, **kwargs: pytest.fail("The admitted context path must use the rendered budget"),
        "prior_coordination": history,
        "runtime_control": controller,
        "record_usage": observed_usage.append,
    }
    if mode == "commit_failure":
        with pytest.raises(OSError, match="injected_commit_failure"):
            await runner._coordinate_collaboration(**args)
        assert len(sent) == len(observed_usage) == controller.used == 1
        assert sent[0].summary_only
    else:
        usage, invocations, retained = await runner._coordinate_collaboration(**args)
        expected_calls = 1 if mode.startswith("disabled") else 2
        assert len(observed_usage) == expected_calls
        assert controller.used == expected_calls + int(mode.endswith("overflow"))
        assert usage.input_tokens == 10 * expected_calls
        assert len(invocations) == len(retained) == 1
        assert invocations[0].result["ok"]
        assert (await collab.read(question))["response_status"] == "answered"
        assert sent[-1].messages[:1] == prefix
        if mode in {"disabled_capacity", "disabled_overflow"}:
            assert len(sent[-1].messages) < len(prefix) + len(history) + 1
        assert any(message.name == "context_summary" for message in sent[-1].messages) == (not mode.startswith("disabled"))
    assert history == original_history
    await sink.drain_turn(worker.turn_id)
    events = await log.list_events(worker.turn_id, event_types=("agent.context",))
    assert len(events) == (0 if mode.startswith("disabled") else 1 if mode == "commit_failure" else 2)
    async with open_shared_ctx(collab.path) as db:
        async with db.execute("SELECT coordination_attempts FROM agent_collaboration_participant WHERE execution_id=?", (collab.lead,)) as cursor:
            assert (await cursor.fetchone())[0] == controller.used
        async with db.execute("SELECT lane FROM model_context_heads WHERE execution_id=?", (collab.lead,)) as cursor:
            assert [row[0] for row in await cursor.fetchall()] == ([] if mode.startswith("disabled") else ["coordination"])
