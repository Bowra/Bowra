"""Context maintenance through the original shared Runner decision boundary."""

from copy import deepcopy

import pytest
from bowhead_core.agent_collaboration_runtime import CollaborationCall, CollaborationRuntime
from bowhead_core.context_history import ConversationContext, load_conversation_context
from bowhead_core.events import TurnEventBus
from bowhead_core.session_store import SessionStore
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry
from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore
from bowhead_router.errors import ProviderCallError, ProviderFailureKind
from bowhead_router.types import LLMResponse, TokenUsage
from cryptography.fernet import Fernet
from test_agent_collaboration import collab as collaboration_fixture
from test_context_maintenance import context_case as maintenance_fixture

from bowhead_router import AgentBinding

collab = collaboration_fixture
context_case = maintenance_fixture
pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("enabled", "overflow", "large_history", "unusable_summary"),
    [
        (True, False, False, False),
        (False, False, False, False),
        (True, True, False, False),
        (True, False, True, False),
        (False, False, True, False),
        (False, True, False, False),
        (False, True, True, False),
        (False, "repeated", True, False),
        (True, False, True, True),
    ],
)
async def test_original_runner_compacts_only_the_model_view_and_accounts_both_decisions(
    collab, context_case, monkeypatch, enabled, overflow, large_history, unusable_summary
):
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    case = context_case
    case.registry.definition_for("provider")["models"][0]["context_policy"]["enabled"] = enabled
    if overflow:
        case.registry.definition_for("provider")["models"][0]["context_policy"]["trigger_input_tokens"] = 30000
    case.router.bind(AgentBinding(agent_type="analyst", primary=("provider", "model")))
    adapter = case.registry.get("provider")
    sent = []

    async def invoke(request):
        adapter._build_body(request, stream=False)
        sent.append(deepcopy(request))
        if overflow and (len(sent) == 1 or overflow == "repeated"):
            raise ProviderCallError(kind=ProviderFailureKind.CONTEXT_OVERFLOW, status_code=400)
        return LLMResponse(
            content="Earlier evidence establishes the refund question. Continue from the current request." if request.summary_only else "Hello.",
            finish_reason="length" if request.summary_only and unusable_summary else "stop",
            usage=TokenUsage(input_tokens=101, output_tokens=17, cached_tokens=30),
        )

    monkeypatch.setattr(adapter, "invoke", invoke)
    targets = []
    route_invoke = case.router.invoke

    async def route(*args, **kwargs):
        targets.append(kwargs.get("target"))
        return await route_invoke(*args, **kwargs)

    monkeypatch.setattr(case.router, "invoke", route)
    log = TurnLogStore(collab.path)
    sink = OrderedTurnLogSink(log)
    bus = TurnEventBus(log_sink=sink)
    runner = SubagentRunner(router=case.router, tool_registry=ToolRegistry(), backend="legacy")
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
        collaboration_runtime=CollaborationRuntime(collab.store, worker, authorize=collab.authorize),
        collaboration_call=CollaborationCall(collab.identity(collab.lead, step="spawn"), None),
    )
    history = deepcopy(case.request.messages[:8])
    if large_history:
        for message in history:
            message.content *= 5
    original = deepcopy(history)
    ctx._model_context_history = ConversationContext(None, 0, history, 0)
    result = await runner.run(
        profile_name="lead",
        task="Say hello.",
        parent_turn_id=worker.turn_id,
        parent_ctx=ctx,
        parent_bus=bus,
        exact_binding=collab.executions[collab.lead],
        history=history,
    )
    await sink.drain_turn(worker.turn_id)
    if overflow == "repeated":
        assert not result.ok and result.error == "llm_context_overflow"
        assert len(sent) == 2 and not any(request.summary_only for request in sent)
        assert len(sent[1].messages) < len(sent[0].messages)
        assert history == original
        assert not await log.list_events(worker.turn_id, event_types=("agent.context",))
        return
    assert result.ok, result.error
    assert history == original
    if large_history and enabled and not unusable_summary:
        assert sum(request.summary_only for request in sent) > 1
    else:
        assert sum(request.summary_only for request in sent) == int(enabled)
    assert result.tokens_input == 101 * (len(sent) - int(overflow))
    assert result.tokens_output == 17 * (len(sent) - int(overflow))
    assert result.tokens_cached == 30 * (len(sent) - int(overflow))
    ordinary = [request for request in sent if not request.summary_only]
    assert len(ordinary) == 1 + int(overflow)
    if enabled and not unusable_summary:
        assert any(message.name == "context_summary" for message in ordinary[-1].messages)
        events = await log.list_events(worker.turn_id, event_types=("agent.context",))
        if large_history:
            assert sum(event.payload["phase"] == "committed" for event in events) >= 2
            assert len(events) == 2 * sum(request.summary_only for request in sent)
        else:
            assert [event.payload["phase"] for event in events] == ["running", "committed"]
            assert events[0].payload["operation_id"] == events[1].payload["operation_id"]
    elif large_history or overflow:
        assert len(ordinary[-1].messages) < len(original) + 1
        assert not any(message.name == "context_summary" for message in ordinary[-1].messages)
        events = await log.list_events(worker.turn_id, event_types=("agent.context",))
        if unusable_summary:
            assert [event.payload["phase"] for event in events] == ["running", "skipped"]
            assert events[-1].payload["reason"] == "summary_result_invalid"
        else:
            assert not events
    else:
        assert ordinary[0].messages[:8] == original
    if overflow and not enabled:
        assert len(ordinary[-1].messages) < len(ordinary[0].messages)
    if overflow:
        assert targets == ([None, ("provider", "model"), ("provider", "model")] if enabled else [None, ("provider", "model")])
    if not overflow:
        from test_context_history import next_context

        store = ctx.session_store
        assert await store.mark_completed(
            turn_id=worker.turn_id,
            worker=worker,
            intent="chat",
            final_answer=result.final_answer,
            tokens_input=result.tokens_input,
            tokens_output=result.tokens_output,
            cost_usd=0,
            context_publication=bus._context_publication,
        )
        await store.finalize_turn(session_id=worker.session_id, current_turn_id=worker.turn_id, next_turn_id=None)
        restored = await load_conversation_context(await next_context(collab))
        assert restored.messages[-1].content == result.final_answer
        assert any(message.name == "context_summary" for message in restored.messages) is (enabled and not unusable_summary)
        if not enabled or unusable_summary:
            assert restored.messages[:8] == original


async def test_helper_protected_input_overflow_keeps_a_capacity_reason(collab, context_case, monkeypatch):
    from bowhead_core.published_diagnostics import normalize_chat_peer_failure_code, publish_chat_peer_failure

    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    case = context_case
    case.registry.definition_for("provider")["models"][0]["context_policy"]["enabled"] = False
    case.router.bind(AgentBinding(agent_type="analyst", primary=("provider", "model")))
    bus = TurnEventBus()
    runner = SubagentRunner(router=case.router, tool_registry=ToolRegistry(), backend="legacy")
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
        collaboration_runtime=CollaborationRuntime(collab.store, worker, authorize=collab.authorize),
        collaboration_call=CollaborationCall(collab.identity(collab.lead, step="spawn"), None),
    )
    result = await runner.run(
        profile_name="compute",
        task="Required input " * 100000,
        parent_turn_id=worker.turn_id,
        parent_ctx=ctx,
        parent_bus=bus,
        exact_binding=collab.executions[collab.requester],
    )
    assert not result.ok and result.error == "llm_context_overflow"
    assert result.llm_reason_code == "llm_context_overflow"
    assert not case.sent
    public = publish_chat_peer_failure(normalize_chat_peer_failure_code(result.error))
    assert public is not None and public.retryable is False
