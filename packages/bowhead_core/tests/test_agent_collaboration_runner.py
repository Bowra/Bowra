"""Run directed collaboration through the real runner, registry and Router.

Only the provider adapter is scripted. Native dispatch, context assembly,
physical-attempt admission and durable question state execute normally.
"""

from __future__ import annotations

import asyncio
import copy
import json

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_collaboration import CollaborationPolicy, CollaborationUnavailable
from bowhead_core.agent_collaboration_runtime import (
    CollaborationCall,
    CollaborationExecution,
    CollaborationRuntime,
    RunnablePermit,
    collaboration_tool_spec,
)
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import PROFILES, SubagentRunner, _materialize_system_profile_snapshot, register_subagent_tool
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec
from bowhead_core.user_store import UserStore
from bowhead_router.invocation import begin_physical_invocation
from bowhead_router.types import LLMChunk, ToolSchema
from test_agent_collaboration import collab as collaboration_fixture

from bowhead_core import _chi_runtime
from bowhead_router import AgentBinding, LLMResponse, MockAdapter, ProviderConfig, ProviderRegistry, Router, TokenUsage, ToolCall

pytestmark = pytest.mark.integration
collab = collaboration_fixture


def tool(action, **arguments):
    # Reuse the provider's native ID across responses deliberately. The runner
    # must scope it by exact execution, frame and accepted model step.
    return LLMResponse(content="", tool_calls=[ToolCall(id="call", name="agent_message", arguments={"action": action, **arguments})])


def receipts(request):
    return [json.loads(message.content) for message in request.messages if message.role == "tool"]


def inbox(request):
    items = []
    for message in request.messages:
        if message.content.startswith("Directed collaboration evidence follows."):
            items.extend(json.loads(message.content.split("\n", 1)[1]))
    return items


def assert_settled_transcript(request):
    pending = set()
    for message in request.messages:
        if message.role == "tool":
            assert message.tool_call_id in pending, "A native Tool result must match an original call"
            pending.remove(message.tool_call_id)
        else:
            assert not pending, "Model context must never cross an unsettled native Tool batch"
            pending.update(call.id for call in message.tool_calls or ())
    assert not pending


class ScriptedProvider(MockAdapter):
    def __init__(self, handler):
        super().__init__(provider_name="collaboration-test")
        self.handler = handler
        self.requests = []
        self.failures = []

    async def invoke(self, request):
        assert_settled_transcript(request)
        attempt = await begin_physical_invocation(
            provider=self.provider_name,
            model=request.model,
            adapter_contract_key="collaboration-test.v1",
            mode="invoke",
            request={"messages": [message.content for message in request.messages]},
            input_request=request,
        )
        self.requests.append(copy.deepcopy(request))
        try:
            response = await self.handler(request)
        except Exception as exc:
            self.failures.append((type(exc).__name__, str(exc)))
            raise
        response.provider = self.provider_name
        response.model = request.model
        response.usage = TokenUsage(input_tokens=3, output_tokens=2, cached_tokens=1)
        if attempt is not None:
            await attempt.complete(usage=response.usage, response_digest="test-response")
        return response

    async def stream(self, request):
        response = await self.invoke(request)
        if response.content:
            yield LLMChunk(delta=response.content)
        for call in response.tool_calls:
            yield LLMChunk(tool_call_delta=call)
        yield LLMChunk(finish_reason="tool_use" if response.tool_calls else "stop", usage=response.usage)


class RunnerFixture:
    def __init__(self, collab, handler):
        self.collab = collab
        self.runtime = CollaborationRuntime(collab.store, collab.worker, authorize=collab.authorize)
        self.provider = ScriptedProvider(handler)
        providers = ProviderRegistry()
        providers.load([ProviderConfig(name=self.provider.provider_name, type="mock")])
        providers._adapters[self.provider.provider_name] = self.provider
        self.router = Router(providers)
        self.router.bind(AgentBinding(agent_type="analyst", primary=(self.provider.provider_name, "mock")))
        self.registry = ToolRegistry()
        self.runner = SubagentRunner(router=self.router, tool_registry=self.registry, backend="legacy")
        self.bus = TurnEventBus()

    def register(self, name, handler):
        self.registry.register(
            ToolSpec(schema=ToolSchema(name=name, description="Controlled business operation", input_schema={"type": "object"}), handler=handler)
        )

    async def run(self, execution_id, *, permit=None, exact=True, context_changes=None):
        worker = self.collab.worker
        context = ToolExecutionContext(
            workspace_id=worker.workspace_id,
            member_uid=worker.user_id,
            user_id=worker.user_id,
            turn_id=worker.turn_id,
            datarc=None,
            team_id=worker.team_id,
            session_id=worker.session_id,
            execution_owner_id=worker.owner_id,
            role="lead",
            collaboration_runtime=self.runtime,
            collaboration_call=CollaborationCall(self.collab.identity(execution_id, step="spawn"), permit),
            subagent_runner=self.runner,
            parent_bus=self.bus,
        )
        for name, value in (context_changes or {}).items():
            setattr(context, name, value)
        binding = self.collab.executions[execution_id]
        return await self.runner.run(
            profile_name=binding.runtime_profile_name,
            task="Verify assigned evidence.",
            parent_turn_id=worker.turn_id,
            parent_bus=self.bus,
            parent_ctx=context,
            exact_binding=binding if exact else None,
        )


@pytest.mark.parametrize("pending_question", [False, True])
async def test_lead_clarification_preserves_execution_and_obligations_until_existing_stop(collab, pending_question):
    from bowhead_core.agents.clarification_tool import is_clarification_sentinel, make_clarification_sentinel

    question = await collab.ask(identity=collab.identity(collab.lead)) if pending_question else None

    async def provider(request):
        assert request.invocation_execution_id == collab.lead
        return LLMResponse(
            content="",
            tool_calls=[ToolCall(id="clarify", name="clarification_ask", arguments={"question": "Which reporting period should apply?"})],
        )

    harness = RunnerFixture(collab, provider)

    async def ask_user(_arguments, context):
        return make_clarification_sentinel(
            candidates=["This month", "Last month"],
            fallback_text="Which reporting period should apply?",
            aborted_turn_id=context.turn_id,
        )

    harness.register("clarification_ask", ask_user)
    result = await harness.run(collab.lead)
    assert result.ok, result.error
    assert is_clarification_sentinel(result.tool_invocations[-1].result)
    assert len(harness.provider.requests) == 1
    assert not any(event.event_type == "agent_spawned" and event.payload.get("status") in {"completed", "failed"} for event in harness.bus.snapshot())
    async with open_shared_ctx(collab.path) as db:
        async with db.execute("SELECT status FROM turn_agent_execution WHERE id=?", (collab.lead,)) as cursor:
            assert (await cursor.fetchone())[0] == "running"
        # The original finalizer and Stop transitions own the paused Turn.
        await db.execute("UPDATE turns SET status='clarification_pending' WHERE id=?", (collab.worker.turn_id,))
        await db.commit()
    if question is not None:
        state = await collab.read(question)
        assert (state["response_status"], state["dependency_status"]) == ("pending", "open")
    async with open_shared_ctx(collab.path) as db:
        await db.execute("UPDATE turns SET status='cancelled',ended_at='2026-10-10T00:00:00Z' WHERE id=?", (collab.worker.turn_id,))
        await db.commit()
    if question is not None:
        state = await collab.read(question)
        assert (state["response_status"], state["dependency_status"]) == ("cancelled", "cancelled")
    assert len(harness.provider.requests) == 1


async def test_peer_question_independent_work_wait_and_verified_completion_with_one_slot(collab):
    turns = {collab.requester: 0, collab.recipient: 0}
    started = asyncio.Event()
    semaphore = asyncio.Semaphore(1)
    question_id = None
    reply_id = None
    independent_work = []

    async def provider(request):
        nonlocal question_id, reply_id
        execution_id = request.invocation_execution_id
        step = turns[execution_id]
        turns[execution_id] += 1
        assert any(schema.name == "agent_message" for schema in request.tools)
        if execution_id == collab.requester:
            if step == 0:
                assert collab.recipient in "\n".join(message.content for message in request.messages)
                started.set()
                return tool("ask", to=collab.recipient, body="Which refund definition applies?")
            if step == 1:
                question_id = receipts(request)[-1]["operation_id"]
                return LLMResponse(content="", tool_calls=[ToolCall(id="probe", name="probe", arguments={})])
            if step == 2:
                assert independent_work == ["done"]
                return tool("wait", question_ids=[question_id])
            if step == 3:
                replies = [item for item in inbox(request) if item.get("action") == "reply"]
                assert len(replies) == 1
                assert replies[0]["sender_id"] == collab.recipient
                assert replies[0]["body"] == "Shipping is excluded."
                reply_id = replies[0]["operation_id"]
                return LLMResponse(content="Premature completion before verification.")
            if step == 4:
                assert "Completion is blocked" in request.messages[-1].content or any(
                    "Completion is blocked" in message.content for message in request.messages
                )
                return tool(
                    "resolve",
                    question_id=question_id,
                    reply_id=reply_id,
                    disposition="resolved",
                    reason="The definition matches the assigned period.",
                )
            assert step == 5
            assert len([item for item in inbox(request) if item.get("action") == "reply"]) == 1
            return LLMResponse(content="Verified refund analysis.")
        if step == 0:
            assert independent_work == ["done"], "The responder runs only after the requester releases the sole permit"
            question = next(item for item in inbox(request) if item.get("action") == "ask")
            return tool("reply", question_id=question["operation_id"], body="Shipping is excluded.")
        assert step == 1
        return LLMResponse(content="Refund definition verified.")

    harness = RunnerFixture(collab, provider)

    async def probe(_arguments, _context):
        assert semaphore.locked(), "Independent business work retains its runnable permit"
        independent_work.append("done")
        return {"ok": True}

    harness.register("probe", probe)
    requester = asyncio.create_task(harness.run(collab.requester, permit=RunnablePermit(semaphore)))
    try:
        await asyncio.wait_for(started.wait(), 5)
        recipient = asyncio.create_task(harness.run(collab.recipient, permit=RunnablePermit(semaphore)))
        results = await asyncio.wait_for(asyncio.gather(requester, recipient), 10)
    finally:
        requester.cancel()
        await asyncio.gather(requester, return_exceptions=True)
    assert all(result.ok for result in results), [result.error for result in results]
    assert results[0].final_answer == "Verified refund analysis."
    assert not any("Premature completion" in str(event.payload) for event in harness.bus.snapshot())
    assert not semaphore.locked()
    assert await collab.store.obligations(collab.worker, execution_id=collab.lead) == []
    assert not harness.registry.has("agent_message"), "Per-execution assembly must not modify the process registry"
    async with open_shared_ctx(collab.path) as db, db.execute("SELECT model_attempts,tool_attempts FROM agent_collaboration_turn") as cursor:
        assert tuple(await cursor.fetchone()) == (8, 1)


@pytest.mark.parametrize("first_response", ["ask", "prose"])
async def test_owner_question_left_unanswered_closes_as_a_limitation_and_the_child_completes(collab, first_response):
    """116 t_649dfe4abb48: Lead asked/waited (reply-only rejection) or answered in prose, the
    consultant's required question stayed open, and every return was refused until the cap."""
    child_returned = asyncio.Event()
    child_round = 0
    coordination_round = 0
    seen_by_child = []

    async def provider(request):
        nonlocal child_round, coordination_round
        if request.invocation_execution_id == collab.requester:
            child_round += 1
            if child_round == 1:
                return tool("ask", to=collab.lead, body="Can you grant the renderer authority I lack?")
            if child_round == 2:
                question = [value for value in receipts(request) if value.get("action") == "ask"][-1]
                return tool("wait", question_ids=[question["operation_id"]])
            seen_by_child.extend(item for item in inbox(request) if item.get("action") in {"reply", "resolve"})
            return LLMResponse(content="Slides drafted as text; the file could not be produced.")
        if request.trace_id.endswith(":coordination"):
            coordination_round += 1
            if coordination_round == 1:
                # Speaks as the requester (rejected by the reply-only round) or answers in prose;
                # either way one correction follows.
                if first_response == "ask":
                    return tool("ask", to=collab.lead, body="Please render the deck for me.")
                return LLMResponse(content="I will render the deck myself later.")
            if first_response == "ask":
                assert receipts(request)[-1]["error"] == "collaboration_coordination_reply_only"
            assert "the task owner of this turn" in request.messages[-1].content
            return LLMResponse(content="I cannot grant that authority.")
        if not child_returned.is_set():
            return LLMResponse(content="", tool_calls=[ToolCall(id="spawn-original", name="subagent_spawn", arguments={})])
        return LLMResponse(content="Delivered the text with the stated limitation.")

    harness = RunnerFixture(collab, provider)

    async def spawn(_arguments, _context):
        result = await harness.run(collab.requester)
        assert result.ok, result.error
        child_returned.set()
        return {"ok": True, "final_answer": result.final_answer}

    harness.register("subagent_spawn", spawn)
    result = await asyncio.wait_for(harness.run(collab.lead), 10)
    assert result.ok, result.error
    assert coordination_round == 2
    assert {item.get("outcome") or item.get("disposition") for item in seen_by_child} == {"unavailable", "blocked"}
    async with (
        open_shared_ctx(collab.path) as db,
        db.execute("SELECT response_status,dependency_status FROM agent_collaboration_operation WHERE action='ask' AND required=1") as cursor,
    ):
        assert [tuple(row) for row in await cursor.fetchall()] == [("unavailable", "blocked")]


async def test_collaboration_barrier_never_trips_the_cross_tool_failure_cap(collab, monkeypatch):
    """Refused returns are guidance, not Tool faults: 116 counted five of them toward the cap of 8."""
    from bowhead_core.subagent_return_tool import register_subagent_return_tool

    monkeypatch.setenv("BOWHEAD_PEER_TOTAL_FAIL_LIMIT", "2")
    rounds = 0
    refusals = []
    payload = {"payload": {"delivery_surface": "chat", "exec_summary": "Refund total is 1080 USD; the ledger period is unconfirmed."}}

    async def provider(request):
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            return tool("ask", to=collab.recipient, body="Which ledger period applies?")
        refusals.extend(value for value in receipts(request)[-1:] if value.get("error") == "collaboration_unresolved")
        if rounds == 5:
            collab.clock[0] += 61  # The peer never answers; expiry records the limitation.
        return LLMResponse(content="", tool_calls=[ToolCall(id=f"return-{rounds}", name="subagent_return", arguments=payload)])

    harness = RunnerFixture(collab, provider)
    register_subagent_return_tool(harness.registry)
    result = await asyncio.wait_for(harness.run(collab.requester), 10)
    assert result.ok, result.error
    assert len(refusals) == 3 and rounds == 5


@pytest.mark.parametrize("invalid_first_reply", [False, True])
async def test_lead_answers_child_before_original_spawn_completes(collab, invalid_first_reply):
    child_returned = asyncio.Event()
    base_round = 0
    child_round = 0
    coordinated = []
    rejected_reply = False

    async def provider(request):
        nonlocal base_round, child_round, rejected_reply
        if request.invocation_execution_id == collab.requester:
            child_round += 1
            if child_round == 1:
                return tool("ask", to=collab.lead, body="Which reporting period is in scope?")
            if child_round == 4:
                return tool("ask", to=collab.lead, body="Retain partial records with an explicit limitation?")
            question = [value for value in receipts(request) if value.get("action") == "ask"][-1]
            replies = [item for item in inbox(request) if item.get("action") == "reply" and item.get("question_id") == question["operation_id"]]
            if child_round in {2, 5}:
                return tool("wait", question_ids=[question["operation_id"]])
            if child_round in {3, 6}:
                assert len(replies) == 1
                return tool(
                    "resolve",
                    question_id=question["operation_id"],
                    reply_id=replies[0]["operation_id"],
                    disposition="resolved",
                    reason="The owner confirmed the original quarter.",
                )
            return LLMResponse(content="Analysis for the accepted quarter.")
        if request.trace_id.endswith(":coordination"):
            assert not child_returned.is_set()
            assert not any(message.role == "tool" and message.name == "subagent_spawn" for message in request.messages)
            brief, data = next(message.content for message in request.messages if message.content.startswith("You are ")).rsplit("\n", 1)
            payload = json.loads(data)
            question = next(item for item in payload["items"] if item["action"] == "ask")
            # The round states, from server data, who answers and which question awaits it.
            assert f"(execution_id {collab.lead}), the task owner of this turn." in brief
            assert f"- {question['operation_id']} from " in brief and f"({collab.requester}), required" in brief
            if invalid_first_reply and not rejected_reply:
                rejected_reply = True
                return tool("reply", to=collab.requester, question_id=question["operation_id"], body="Use the original quarter.")
            if invalid_first_reply and not coordinated:
                assert receipts(request)[-1]["error"] == "collaboration_arguments_invalid"
                assert receipts(request)[-1]["required_fields"] == ["action", "question_id", "body"]
            if coordinated:
                assert any("coordination_input" in message.content for message in request.messages[:-1]), (
                    "Later help retains the owner's prior decisions"
                )
            coordinated.append(question["operation_id"])
            return tool("reply", question_id=question["operation_id"], body="Use the original quarter; report evidence gaps.")
        base_round += 1
        if base_round == 1:
            return LLMResponse(content="", tool_calls=[ToolCall(id="spawn-original", name="subagent_spawn", arguments={})])
        assert child_returned.is_set()
        assert any(message.role == "tool" and message.tool_call_id == "spawn-original" for message in request.messages)
        assert any("coordination_input" in message.content for message in request.messages)
        return LLMResponse(content="Completed within the confirmed scope.")

    harness = RunnerFixture(collab, provider)

    async def spawn(_arguments, _context):
        result = await harness.run(collab.requester)
        assert result.ok, result.error
        child_returned.set()
        return {
            "ok": True,
            "final_answer": result.final_answer,
            "tokens_input": result.tokens_input,
            "tokens_output": result.tokens_output,
            "tokens_cached": result.tokens_cached,
        }

    harness.register("subagent_spawn", spawn)
    result = await asyncio.wait_for(harness.run(collab.lead), 10)
    assert result.ok, result.error
    assert len(coordinated) == 2
    assert result.tokens_input == (36 if invalid_first_reply else 33), "Root usage includes coordination corrections and the original child"
    assert result.tokens_output == (24 if invalid_first_reply else 22)


async def test_wait_in_a_business_batch_is_refused_before_yielding(collab):
    question = await collab.ask()
    semaphore = asyncio.Semaphore(1)
    calls = 0
    replied = None

    async def provider(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="wait", name="agent_message", arguments={"action": "wait", "question_ids": [question["operation_id"]]}),
                    ToolCall(id="business", name="probe", arguments={}),
                ],
            )
        if calls == 2:
            assert receipts(request)[0] == {"ok": False, "error": "collaboration_wait_requires_settled_batch"}
            return tool(
                "resolve",
                question_id=question["operation_id"],
                reply_id=replied["operation_id"],
                disposition="resolved",
                reason="Verified against independent work.",
            )
        return LLMResponse(content="Work and reply reconciled.")

    harness = RunnerFixture(collab, provider)

    async def probe(_arguments, _context):
        nonlocal replied
        assert semaphore.locked(), "A sibling business operation must not lose its permit to a wait"
        replied = await collab.reply(question)
        await harness.runtime.notify()
        return {"ok": True}

    harness.register("probe", probe)
    result = await asyncio.wait_for(harness.run(collab.requester, permit=RunnablePermit(semaphore)), 5)
    assert result.ok, result.error
    async with open_shared_ctx(collab.path) as db, db.execute("SELECT COUNT(*) FROM agent_collaboration_operation WHERE action='wait'") as cursor:
        assert (await cursor.fetchone())[0] == 0
    assert not semaphore.locked()


async def test_cancelled_wait_does_not_reacquire_an_occupied_permit(collab, monkeypatch):
    question = await collab.ask()
    sleeping = asyncio.Event()
    semaphore = asyncio.Semaphore(1)
    original = collab.store.wait_state

    async def observe_wait(*args, **kwargs):
        result = await original(*args, **kwargs)
        sleeping.set()
        return result

    monkeypatch.setattr(collab.store, "wait_state", observe_wait)

    async def provider(_request):
        return tool("wait", question_ids=[question["operation_id"]])

    harness = RunnerFixture(collab, provider)
    task = asyncio.create_task(harness.run(collab.requester, permit=RunnablePermit(semaphore)))
    try:
        await asyncio.wait_for(sleeping.wait(), 5)
        async with semaphore:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        assert len(harness.provider.requests) == 1, "Waiting and cancellation must not schedule polling model calls"
        assert not semaphore.locked()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("collab", [CollaborationPolicy(max_model_attempts=1)], indirect=True)
@pytest.mark.parametrize("first_action", ["ask", "probe"])
async def test_shared_model_limit_stops_provider_before_another_request(collab, first_action):
    async def provider(_request):
        if first_action == "probe":
            return LLMResponse(content="", tool_calls=[ToolCall(id="work", name="probe", arguments={})])
        return tool("ask", to=collab.recipient, body="Verify this definition.")

    harness = RunnerFixture(collab, provider)

    async def probe(_arguments, _context):
        return {"ok": True}

    harness.register("probe", probe)
    result = await asyncio.wait_for(harness.run(collab.requester), 5)
    assert not result.ok
    assert len(harness.provider.requests) == 1
    assert result.error == "collaboration_effort_exhausted"
    assert not any(event.payload.get("reason") == "stream_to_invoke_fallback" for event in harness.bus.snapshot())
    obligations = await collab.store.obligations(collab.worker, execution_id=collab.requester)
    assert len(obligations) == (1 if first_action == "ask" else 0)
    if obligations:
        assert obligations[0]["dependency_status"] == "open", "Budget exhaustion cannot fabricate dependency satisfaction"


async def test_question_winning_final_completion_race_never_publishes_success(collab, monkeypatch):
    async def provider(_request):
        return LLMResponse(content="The assignment appears complete.")

    harness = RunnerFixture(collab, provider)
    complete = collab.store.complete

    async def concurrent_question(worker, *, execution_id):
        await collab.ask(identity=collab.identity(collab.recipient), recipient_id=execution_id)
        await complete(worker, execution_id=execution_id)

    monkeypatch.setattr(collab.store, "complete", concurrent_question)
    result = await harness.run(collab.requester)
    assert not result.ok
    assert result.error == "collaboration_unresolved"
    assert not any(event.payload.get("status") == "completed" for event in harness.bus.snapshot())


async def test_native_parallel_spawn_admits_queued_recipient_before_waiting_for_capacity(collab, monkeypatch):
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EXEC", "1")
    monkeypatch.setenv("BOWHEAD_CHI_MAX_CONCURRENT", "1")
    monkeypatch.setenv("BOWHEAD_CHI_GLOBAL_MAX_CONCURRENT", "1")
    monkeypatch.setattr(_chi_runtime, "_global_sem", None)
    profile = _materialize_system_profile_snapshot(collab.executions[collab.requester].definition, expected_name="compute")
    monkeypatch.setitem(PROFILES, "compute", profile)
    joined = asyncio.Event()
    join = collab.store.join
    admitted = set()

    async def observe_join(worker, **kwargs):
        await join(worker, **kwargs)
        admitted.add(kwargs["execution_id"])
        if len(admitted) == 2:
            joined.set()

    monkeypatch.setattr(collab.store, "join", observe_join)
    rounds = {}
    child_ids = set()
    first_child = None

    async def provider(request):
        nonlocal first_child
        execution_id = request.invocation_execution_id
        step = rounds.get(execution_id, 0)
        rounds[execution_id] = step + 1
        if execution_id == collab.lead:
            if step == 0:
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="analyze",
                            name="subagent_spawn",
                            arguments={"profile": "compute", "task": "Analyze refunds.", "delivery": "chat", "canvas_mode": "none"},
                        ),
                        ToolCall(
                            id="verify",
                            name="subagent_spawn",
                            arguments={"profile": "compute", "task": "Verify refund rules.", "delivery": "chat", "canvas_mode": "none"},
                        ),
                    ],
                )
            results = receipts(request)
            assert len(results) == 2
            assert all(value.get("ok") for value in results), results
            return LLMResponse(content="Both assignments are complete.")
        child_ids.add(execution_id)
        if first_child is None:
            first_child = execution_id
        cards_message = next(
            message for message in reversed(request.messages) if message.content.startswith("Current collaboration responsibility cards")
        )
        cards = json.loads(cards_message.content.split("\n", 1)[1])["roster"]
        own = next(card for card in cards if card["execution_id"] == execution_id)
        if execution_id == first_child:
            if step == 0:
                return LLMResponse(content="", tool_calls=[ToolCall(id="work", name="probe", arguments={})])
            if step == 1:
                recipient = next(card for card in cards if card["execution_id"] in admitted and card["execution_id"] != execution_id)
                return tool("ask", to=recipient["execution_id"], body="Does the original refund definition exclude shipping?")
            question = next(value for value in receipts(request) if value.get("action") == "ask")
            if step == 2:
                return tool("wait", question_ids=[question["operation_id"]])
            if step == 3:
                reply = next(item for item in inbox(request) if item.get("action") == "reply")
                return tool(
                    "resolve",
                    question_id=question["operation_id"],
                    reply_id=reply["operation_id"],
                    disposition="resolved",
                    reason="Matches the assigned refund calculation.",
                )
            return LLMResponse(content="Refund analysis completed.")
        assert own["assignment"] in {"Analyze refunds.", "Verify refund rules."}
        if step == 0:
            question = next(item for item in inbox(request) if item.get("action") == "ask")
            return tool("reply", question_id=question["operation_id"], body="The definition excludes shipping.")
        return LLMResponse(content="Refund rules verified.")

    harness = RunnerFixture(collab, provider)
    harness.runner.team_subagent_store = UserStore(collab.path)
    register_subagent_tool(harness.registry)

    async def probe(_arguments, _context):
        await asyncio.wait_for(joined.wait(), 3)
        return {"ok": True}

    harness.register("probe", probe)
    result = await asyncio.wait_for(harness.run(collab.lead), 10)
    assert result.ok, (result.error, harness.provider.failures)
    assert len(child_ids) == 2
    assert not _chi_runtime.get_global_sem().locked()
    assert not any(request.trace_id.endswith(":coordination") for request in harness.provider.requests), "Directed peers do not require a Lead relay"


@pytest.mark.parametrize("changed", [{"user_id": "another-user"}, {"workspace_id": "another-space"}, {"execution_owner_id": "stale-worker"}])
async def test_runner_rejects_changed_caller_before_receiving_peer_context(collab, changed):
    async def provider(_request):
        pytest.fail("A mismatched caller must not receive any model input")

    harness = RunnerFixture(collab, provider)
    with pytest.raises(CollaborationUnavailable, match="collaboration_worker_unavailable"):
        await harness.run(collab.requester, context_changes=changed)
    assert not harness.provider.requests


async def test_legacy_child_cannot_inherit_parent_sender_identity(collab):
    calls = 0

    async def provider(request):
        nonlocal calls
        calls += 1
        assert not any(schema.name == "agent_message" for schema in request.tools)
        if calls == 1:
            return tool("ask", to=collab.recipient, body="Attempt to borrow the parent's identity.")
        return LLMResponse(content="Legacy assignment completed without collaboration.")

    harness = RunnerFixture(collab, provider)
    parent_frame = CollaborationExecution(harness.runtime, collab.lead)
    result = await harness.run(
        collab.requester,
        exact=False,
        context_changes={"collaboration_execution": parent_frame, "tool_view": harness.registry.clone_with_overlay([collaboration_tool_spec()])},
    )
    assert result.ok, (result.error, harness.provider.failures)
    assert await collab.count() == 0
