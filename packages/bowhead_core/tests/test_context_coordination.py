"""Compaction inside the original bounded coordination reply frame."""

import json
from copy import deepcopy

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_collaboration_runtime import CollaborationExecution, CollaborationRuntime, collaboration_tool_spec
from bowhead_core.agent_run_limits import AgentLoopBudget
from bowhead_core.events import TurnEventBus
from bowhead_core.output_recovery import OutputRecovery
from bowhead_core.session_store import SessionStore
from bowhead_core.subagent import SubagentProfile, SubagentRunner, _RuntimeExecution
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry
from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore
from bowhead_core.types import ToolInvocation
from bowhead_router.errors import ProviderCallError, ProviderFailureKind
from bowhead_router.invocation import begin_physical_invocation
from bowhead_router.runtime_notes import OUTPUT_RECOVERY_STATE_NAME
from bowhead_router.types import LLMRequest, LLMResponse, Message, TokenUsage, ToolCall
from cryptography.fernet import Fernet
from test_agent_collaboration import collab as collaboration_fixture
from test_context_maintenance import context_case as maintenance_fixture

collab = collaboration_fixture
context_case = maintenance_fixture
pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "mode",
    [
        "enabled",
        "disabled",
        "disabled_capacity",
        "disabled_overflow",
        "overflow",
        "commit_failure",
        "material_supplement",
        "material_tool_name",
        "output_recovery",
    ],
)
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
    from bowhead_core.result_material_history import SUPPLEMENT_NAME

    if mode == "material_tool_name":
        prefix.extend(
            [
                Message(role="assistant", content="", tool_calls=[ToolCall("original", SUPPLEMENT_NAME, {})]),
                Message(role="tool", name=SUPPLEMENT_NAME, tool_call_id="original", content="Original Tool observation."),
            ]
        )
    accepted = (
        [*prefix, Message(role="user", name=SUPPLEMENT_NAME, content="Earlier authorized pointer metadata.")]
        if mode == "material_supplement"
        else list(prefix)
    )
    if mode == "output_recovery":
        # After any successful business Tool, the waiting request ends with the
        # base loop's output-recovery checkpoint (an interrupted task).
        recovery = OutputRecovery()
        recovery.observe([ToolInvocation(tool_name="bi_schema", arguments={}, result={"ok": True})])
        recovery.sync(accepted)
        assert accepted[-1].name == OUTPUT_RECOVERY_STATE_NAME
    # The first coordination round of a suspended batch has no earlier coordination.
    history = [] if mode == "output_recovery" else deepcopy(case.request.messages[:8])
    compacts = not mode.startswith("disabled") and mode != "output_recovery"
    if mode == "disabled_capacity":
        for message in history:
            message.content *= 5
    original_history = deepcopy(history)
    args = {
        "context": await runtime.context(collab.lead),
        "accepted_request": LLMRequest(model="", messages=accepted, invocation_execution_id=collab.lead),
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
        expected_calls = 2 if compacts else 1
        assert len(observed_usage) == expected_calls
        assert controller.used == expected_calls + int(mode.endswith("overflow"))
        assert usage.input_tokens == 10 * expected_calls
        assert len(invocations) == len(retained) == 1
        assert invocations[0].result["ok"]
        # The record kept for the base loop says what this execution replied.
        (record,) = json.loads(retained[0].content.split("\n", 1)[1])
        assert record["receipts"] == [
            {
                "tool": "agent_message",
                "arguments": {"action": "reply", "question_id": question["operation_id"], "body": "Use the original quarter."},
                "result": invocations[0].result,
                "error": None,
            }
        ]
        assert [item["question_id"] for item in record["coordination_input"]] == [question["operation_id"]]
        assert (await collab.read(question))["response_status"] == "answered"
        assert sent[-1].messages[: len(prefix)] == prefix
        if mode in {"disabled_capacity", "disabled_overflow"}:
            assert len(sent[-1].messages) < len(prefix) + len(history) + 1
        assert any(message.name == "context_summary" for message in sent[-1].messages) == compacts
    assert history == original_history
    assert args["accepted_request"].messages == accepted
    assert all(message.role != "user" or message.name != SUPPLEMENT_NAME for request in sent for message in request.messages)
    assert all(
        message.name != OUTPUT_RECOVERY_STATE_NAME and "Continue from retained Tool results" not in str(message.content)
        for request in sent
        for message in request.messages
    )
    await sink.drain_turn(worker.turn_id)
    events = await log.list_events(worker.turn_id, event_types=("agent.context",))
    assert len(events) == (1 if mode == "commit_failure" else 2 if compacts else 0)
    async with open_shared_ctx(collab.path) as db:
        async with db.execute("SELECT coordination_attempts FROM agent_collaboration_participant WHERE execution_id=?", (collab.lead,)) as cursor:
            assert (await cursor.fetchone())[0] == controller.used
        async with db.execute("SELECT lane FROM model_context_heads WHERE execution_id=?", (collab.lead,)) as cursor:
            assert [row[0] for row in await cursor.fetchall()] == (["coordination"] if compacts else [])


def test_a_coordination_record_keeps_what_was_said_to_a_participant_and_only_the_refusal_of_other_calls():
    from bowhead_core.subagent import _coordination_record

    reply = ToolInvocation(
        tool_name="agent_message",
        arguments={"action": "reply", "question_id": "aco_1", "body": "Return what you have."},
        result={"ok": True, "accepted": True, "action": "reply", "outcome": "answered"},
    )
    refused = ToolInvocation(
        tool_name="write_report", arguments={"content": "x" * 20000}, result={"ok": False, "error": "collaboration_coordination_reply_only"}
    )
    record = _coordination_record([{"action": "ask", "question_id": "aco_1"}], "", [reply, refused])
    assert record == {
        "coordination_input": [{"action": "ask", "question_id": "aco_1"}],
        "coordination_response": "",
        "receipts": [
            {"tool": "agent_message", "arguments": reply.arguments, "result": reply.result, "error": None},
            {"tool": "write_report", "result": refused.result, "error": None},
        ],
    }
    assert len(json.dumps(record)) < 1000
