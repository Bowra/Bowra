"""Exercise the native Tool adapter and wait races against real durable state."""

from __future__ import annotations

import asyncio

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_collaboration import CollaborationPolicy, CollaborationStore, CollaborationUnavailable
from bowhead_core.agent_collaboration_runtime import (
    CollaborationAttemptGuard,
    CollaborationCall,
    CollaborationRuntime,
    RunnablePermit,
    collaboration_tool_spec,
)
from bowhead_core.agent_execution_binding import AgentExecutionBindingError
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry
from bowhead_router.invocation import begin_physical_invocation, enclosing_physical_guard_scope
from test_agent_collaboration import collab as collaboration_fixture

pytestmark = pytest.mark.integration
collab = collaboration_fixture


def runtime(collab):
    return CollaborationRuntime(collab.store, collab.worker, authorize=collab.authorize)


def call(collab, execution=None, step="one", *, permit=None):
    return CollaborationCall(collab.identity(execution, step=step), permit)


async def resolve(collab, question, reply, *, step="resolve"):
    return await collab.store.resolve(
        collab.worker,
        identity=collab.identity(step=step),
        question_id=question["operation_id"],
        reply_id=reply["operation_id"],
        disposition="resolved",
        reason="Checked the evidence against this assignment.",
        authorize=collab.authorize,
    )


async def test_native_registry_tool_uses_server_identity_and_rejects_model_sender(collab):
    adapter = runtime(collab)
    registry = ToolRegistry()
    registry.register(collaboration_tool_spec())
    ctx = ToolExecutionContext(
        workspace_id=collab.worker.workspace_id,
        member_uid=collab.worker.user_id,
        user_id=collab.worker.user_id,
        turn_id=collab.worker.turn_id,
        datarc=None,
        team_id=collab.worker.team_id,
        session_id=collab.worker.session_id,
        execution_owner_id=collab.worker.owner_id,
        collaboration_runtime=adapter,
        collaboration_call=call(collab),
    )
    args = {"action": "ask", "to": collab.recipient, "body": "Verify the refund policy."}
    refused = await registry.execute("agent_message", {**args, "sender_id": collab.lead}, ctx)
    assert refused.result["error"] == "collaboration_arguments_invalid"
    assert "sender_id" not in refused.result["required_fields"] + refused.result["optional_fields"]
    assert await collab.count() == 0
    accepted = await registry.execute("agent_message", args, ctx)
    assert accepted.result["sender_id"] == collab.requester
    assert accepted.result["recipient_id"] == collab.recipient
    ctx.execution_generation = 2
    stale = await registry.execute("agent_message", args, ctx)
    assert stale.result == {"ok": False, "error": "collaboration_worker_unavailable"}
    ctx.execution_generation = 1
    ctx.session_id = "different-session"
    wrong_scope = await registry.execute("agent_message", args, ctx)
    assert wrong_scope.result == {"ok": False, "error": "collaboration_worker_unavailable"}
    assert await collab.count() == 1


async def test_rejected_reply_has_action_specific_repair_without_accepting_or_echoing_extra_fields(collab):
    adapter = runtime(collab)
    question = await collab.ask()
    arguments = {"action": "reply", "to": collab.requester, "question_id": question["operation_id"], "body": "Shipping is excluded."}
    refused = await adapter.dispatch(call(collab, collab.recipient), {**arguments, "private-extra-name": "PRIVATE_VALUE"})
    assert refused["error"] == "collaboration_arguments_invalid"
    assert refused["required_fields"] == ["action", "question_id", "body"]
    assert refused["optional_fields"] == ["references", "unavailable"]
    assert "PRIVATE_VALUE" not in str(refused) and "private-extra-name" not in str(refused)
    assert await collab.count() == 1, "Invalid reply must not consume the original question"
    arguments.pop("to")
    reply = await adapter.dispatch(call(collab, collab.recipient, step="corrected"), arguments)
    assert reply["accepted"]
    missing_reply = await adapter.dispatch(
        call(collab, step="verify"),
        {"action": "resolve", "question_id": question["operation_id"], "disposition": "resolved", "reason": "The definition matches."},
    )
    assert missing_reply["error"] == "collaboration_evidence_unavailable"
    assert "reply_id" in missing_reply["guidance"]
    assert (await collab.read(question))["dependency_status"] == "open"
    await resolve(collab, question, reply)
    assert (await collab.read(question))["dependency_status"] == "resolved"


async def test_wait_releases_permit_and_cannot_lose_reply_between_read_and_sleep(collab, monkeypatch):
    adapter = runtime(collab)
    question = await collab.ask()
    sem = asyncio.Semaphore(1)
    checked = asyncio.Event()
    release_read = asyncio.Event()
    original = collab.store.wait_state

    async def paused_read(*args, **kwargs):
        state = await original(*args, **kwargs)
        if not state["ready"]:
            checked.set()
            await release_read.wait()
        return state

    monkeypatch.setattr(collab.store, "wait_state", paused_read)

    async def requester():
        async with RunnablePermit(sem) as permit:
            result = await adapter.dispatch(call(collab, step="wait", permit=permit), {"action": "wait", "question_ids": [question["operation_id"]]})
            assert sem.locked(), "A resumed execution reacquires its original runnable capacity"
            return result

    waiting = asyncio.create_task(requester())
    try:
        await asyncio.wait_for(checked.wait(), 2)
        async with sem:
            reply = await adapter.dispatch(
                call(collab, collab.recipient), {"action": "reply", "question_id": question["operation_id"], "body": "Shipping is excluded."}
            )
        release_read.set()
        result = await asyncio.wait_for(waiting, 2)
        assert result["ready"] is True
        assert result["questions"][0]["reply_id"] == reply["operation_id"]
        assert result["questions"][0]["dependency_status"] == "open"
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
    assert not sem.locked()


async def test_single_specialist_can_reach_owner_while_its_original_work_is_pending(collab):
    adapter = runtime(collab)
    child_finished = asyncio.Event()
    question = await adapter.dispatch(
        call(collab), {"action": "ask", "to": collab.lead, "body": "The evidence conflicts. Which scope is authorized?"}
    )
    frames = []

    async def original_work():
        await child_finished.wait()
        return "original child result"

    async def coordinate(context):
        frames.append(context)
        assert not child_finished.is_set()
        assert context["items"][0]["operation_id"] == question["operation_id"]
        reply = await adapter.dispatch(
            call(collab, collab.lead, "coordination"),
            {"action": "reply", "question_id": question["operation_id"], "body": "Keep the original scope and report the missing evidence."},
        )
        assert reply["ok"] is True
        adapter.acknowledge_context(collab.lead, revision=context["revision"])
        child_finished.set()

    result = await asyncio.wait_for(adapter.while_suspended(original_work(), execution_id=collab.lead, coordinate=coordinate), 2)
    assert result == "original child result"
    assert len(frames) == 1
    assert (await collab.read(question))["dependency_status"] == "open"


async def test_correction_invalidates_completion_and_old_reply_cannot_resolve_again(collab):
    question = await collab.ask()
    reply = await collab.reply(question)
    await resolve(collab, question, reply)
    update = await collab.store.update(
        collab.worker,
        identity=collab.identity(collab.recipient, step="correction"),
        recipient_id=collab.requester,
        body="The new policy includes shipping.",
        supersedes=reply["operation_id"],
        authorize=collab.authorize,
    )
    with pytest.raises(AgentExecutionBindingError, match="agent_collaboration_unresolved"):
        await collab.bindings.mark_terminal(collab.lead, status="completed")
    with pytest.raises(CollaborationUnavailable, match="collaboration_evidence_unavailable"):
        await resolve(collab, question, reply, step="stale")
    await resolve(collab, question, update, step="corrected")
    assert (await collab.read(question))["dependency_status"] == "resolved"
    assert (await collab.read(reply))["body"] == "Refunds exclude shipping."


async def test_correction_for_terminal_consumer_routes_to_owner_without_reopening_execution(collab):
    question = await collab.ask()
    reply = await collab.reply(question)
    await resolve(collab, question, reply)
    await collab.bindings.mark_terminal(collab.requester, status="completed")
    update = await collab.store.update(
        collab.worker,
        identity=collab.identity(collab.recipient, step="correction"),
        recipient_id=collab.requester,
        body="The prior evidence was superseded.",
        supersedes=reply["operation_id"],
        authorize=collab.authorize,
    )
    assert update["recipient_id"] == collab.lead
    assert update["affected_recipient_id"] == collab.requester
    async with open_shared_ctx(collab.path) as db:
        assert (await (await db.execute("SELECT status FROM turn_agent_execution WHERE id=?", (collab.requester,))).fetchone())[0] == "completed"
    with pytest.raises(AgentExecutionBindingError, match="agent_collaboration_unresolved"):
        await collab.bindings.mark_terminal(collab.lead, status="completed")


@pytest.mark.parametrize("collab", [CollaborationPolicy(max_model_attempts=2)], indirect=True)
async def test_each_physical_retry_spends_shared_budget_and_restart_does_not_reset_it(collab):
    adapter = runtime(collab)
    guard = CollaborationAttemptGuard(adapter, execution_id=collab.requester, frame_id="base", model_step_id="one")
    args = {"provider": "provider", "model": "model", "adapter_contract_key": "test/v1", "mode": "invoke", "request": {"same": "request"}}
    with enclosing_physical_guard_scope(guard):
        first = await begin_physical_invocation(**args)
        await first.confirmed_not_applied(evidence_code="retryable/v1", evidence_digest="a" * 64)
        await begin_physical_invocation(**args)
    restored = CollaborationStore(collab.path, cipher=collab.store.cipher, clock=collab.store.clock)
    restored_runtime = CollaborationRuntime(restored, collab.worker, authorize=collab.authorize)
    with (
        enclosing_physical_guard_scope(
            CollaborationAttemptGuard(restored_runtime, execution_id=collab.recipient, frame_id="base", model_step_id="new")
        ),
        pytest.raises(CollaborationUnavailable, match="collaboration_effort_exhausted"),
    ):
        await begin_physical_invocation(**args)
    # Holds do not become fake user messages or a second usage ledger.
    assert await collab.store.pending_publications(collab.worker) == []


async def test_wait_wakes_for_independent_inbound_work_without_resolving_own_question(collab):
    adapter = runtime(collab)
    question = await collab.ask()
    received = asyncio.Event()

    async def other_work():
        await received.wait()
        return await adapter.dispatch(
            call(collab, collab.recipient, "independent"),
            {"action": "ask", "to": collab.requester, "body": "Which period is your current task using?"},
        )

    other = asyncio.create_task(other_work())
    receipt = await collab.store.begin_wait(
        collab.worker, identity=collab.identity(step="wait"), question_ids=(question["operation_id"],), after=0, authorize=collab.authorize
    )
    received.set()
    await other
    result = await asyncio.wait_for(adapter._wait(collab.requester, receipt["operation_id"]), 2)
    assert result["ready"] is True
    assert result["questions"][0]["response_status"] == "pending"
    assert result["items"][0]["body"] == "Which period is your current task using?"


async def test_revoked_source_is_a_safe_blocker_in_inbox(collab):
    from bowhead_core.agent_collaboration import EvidenceReference

    question = await collab.ask(references=(EvidenceReference(identity="refund-policy", version="v1"),))
    collab.readable.clear()
    inbox = await collab.store.inbox(collab.worker, execution_id=collab.recipient, authorize=collab.authorize)
    assert inbox["items"][0]["operation_id"] == question["operation_id"]
    assert inbox["items"][0]["unavailable"] == "collaboration_material_unavailable"
    assert "body" not in inbox["items"][0]
    assert "references" not in inbox["items"][0]


async def test_wait_cancellation_does_not_leak_or_reacquire_permits(collab, monkeypatch):
    adapter = runtime(collab)
    question = await collab.ask()
    waiting = asyncio.Event()
    original = collab.store.wait_state

    async def observe_wait(*args, **kwargs):
        result = await original(*args, **kwargs)
        waiting.set()
        return result

    monkeypatch.setattr(collab.store, "wait_state", observe_wait)
    sem = asyncio.Semaphore(1)

    async def requester():
        async with RunnablePermit(sem) as permit:
            return await adapter.dispatch(call(collab, step="wait", permit=permit), {"action": "wait", "question_ids": [question["operation_id"]]})

    task = asyncio.create_task(requester())
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        await sem.acquire()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert sem.locked(), "Cancellation must not release another execution's permit"
    finally:
        sem.release()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_failed_recipient_releases_response_obligation_but_keeps_requester_dependency(collab):
    question = await collab.ask()
    wait = await collab.store.begin_wait(
        collab.worker, identity=collab.identity(step="wait"), question_ids=(question["operation_id"],), after=0, authorize=collab.authorize
    )
    await collab.bindings.mark_terminal(collab.recipient, status="failed")
    state = await collab.store.wait_state(collab.worker, execution_id=collab.requester, operation_id=wait["operation_id"], authorize=collab.authorize)
    assert state["ready"] is True
    assert state["questions"][0]["response_status"] == "unavailable"
    assert state["questions"][0]["dependency_status"] == "open"


async def test_whole_task_stop_cancels_waits_and_obligations_atomically(collab):
    from bowhead_core.session_store import SessionStore

    question = await collab.ask()
    await collab.store.begin_wait(
        collab.worker, identity=collab.identity(step="wait"), question_ids=(question["operation_id"],), after=0, authorize=collab.authorize
    )
    assert (
        await SessionStore(collab.path).request_cancel_turn(
            team_id=collab.worker.team_id,
            user_id=collab.worker.user_id,
            workspace_id=collab.worker.workspace_id,
            turn_id=collab.worker.turn_id,
            cancel_request_id="cancel-collaboration",
            expected_execution_generation=collab.worker.generation,
        )
        == "cancelled"
    )
    async with open_shared_ctx(collab.path) as db:
        row = await (
            await db.execute("SELECT wait_operation_id FROM agent_collaboration_participant WHERE execution_id=?", (collab.requester,))
        ).fetchone()
        assert row[0] is None
    state = await collab.read(question)
    assert state["response_status"] == "cancelled"
    assert state["dependency_status"] == "cancelled"
    with pytest.raises(CollaborationUnavailable, match="collaboration_worker_unavailable"):
        await collab.reply(question)


async def test_context_keeps_exact_owner_handle_and_bounded_messages(collab):
    adapter = runtime(collab)
    for index in range(2):
        await collab.ask(identity=collab.identity(step=f"long-{index}"), body="Policy " + "p" * 8100)
    context = await adapter.context(collab.recipient)
    import json

    assert len(json.dumps(context, ensure_ascii=False).encode()) <= 16 * 1024
    assert len(context["items"]) == 1
    assert any(card["is_owner"] and card["execution_id"] == collab.lead for card in context["roster"])
    # Preparing a prompt is not consumption. Only the retained input advances.
    assert (await adapter.context(collab.recipient))["revision"] == context["revision"]
    adapter.acknowledge_context(collab.recipient, revision=context["revision"])
    later = await adapter.context(collab.recipient)
    assert later["items"][0]["operation_id"] != context["items"][0]["operation_id"]


async def test_native_wait_retry_retains_original_cursor_after_input_advanced(collab):
    question = await collab.ask()
    args = {"identity": collab.identity(step="wait"), "question_ids": (question["operation_id"],), "authorize": collab.authorize}
    original = await collab.store.begin_wait(collab.worker, **args, after=0)
    reply = await collab.reply(question)
    replay = await collab.store.begin_wait(collab.worker, **args, after=999)
    assert replay == original
    state = await collab.store.wait_state(
        collab.worker, execution_id=collab.requester, operation_id=replay["operation_id"], authorize=collab.authorize
    )
    assert state["items"][0]["operation_id"] == reply["operation_id"]


async def test_json_expansion_cannot_block_the_mailbox_head(collab):
    adapter = runtime(collab)
    question = await collab.ask(body='"' * 8192)
    context = await adapter.context(collab.recipient)
    assert context["items"][0]["operation_id"] == question["operation_id"]
    assert context["items"][0]["unavailable"] == "collaboration_context_limit"
    assert "body" not in context["items"][0]
    adapter.acknowledge_context(collab.recipient, revision=context["revision"])
    assert (await adapter.context(collab.recipient))["items"] == []


@pytest.mark.parametrize("collab", [CollaborationPolicy(max_coordination_attempts=1)], indirect=True)
async def test_coordination_limit_is_per_execution_within_shared_parent_budget(collab):
    await collab.store.reserve_attempt(collab.worker, identity=collab.identity(collab.lead, frame="coordination"), kind="model", coordination=True)
    with pytest.raises(CollaborationUnavailable, match="collaboration_effort_exhausted"):
        await collab.store.reserve_attempt(
            collab.worker, identity=collab.identity(collab.lead, frame="coordination", step="two"), kind="model", coordination=True
        )
    # The owner's ordinary continuation does not receive a new budget; it may
    # still use the original parent's unspent base-frame allowance.
    await collab.store.reserve_attempt(collab.worker, identity=collab.identity(collab.lead, step="base"), kind="model")
    async with open_shared_ctx(collab.path) as db:
        assert (await (await db.execute("SELECT model_attempts FROM agent_collaboration_turn WHERE turn_id=?", (collab.worker.turn_id,))).fetchone())[
            0
        ] == 2


async def test_publication_retry_reuses_original_event_and_does_not_redeliver_live(collab):
    from bowhead_core.events import OrchestratorEvent, TurnEventBus
    from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore

    question = await collab.ask()
    descriptor = (await collab.store.pending_publications(collab.worker))[0]
    store = TurnLogStore(str(collab.path))
    sink = OrderedTurnLogSink(store)
    first = OrchestratorEvent(
        turn_id=collab.worker.turn_id,
        event_type=descriptor["event_type"],
        payload=dict(descriptor["payload"]),
        durable_event_id=descriptor["event_id"],
        persist_before_delivery=True,
    )
    bus = TurnEventBus(log_sink=sink, persist_before_live=True)
    assert await bus.emit(first)
    # Crash after event commit, before acknowledging its publication intent.
    retry = OrchestratorEvent(
        turn_id=collab.worker.turn_id,
        event_type=descriptor["event_type"],
        payload=dict(descriptor["payload"]),
        durable_event_id=descriptor["event_id"],
        persist_before_delivery=True,
    )
    recovered_bus = TurnEventBus(log_sink=sink, initial_seq=40, persist_before_live=True)
    assert await recovered_bus.emit(retry)
    assert retry.suppress_live_delivery is True
    await collab.store.acknowledge_publication(collab.worker, operation_id=question["operation_id"])
    assert await collab.store.pending_publications(collab.worker) == []
    async with open_shared_ctx(collab.path) as db:
        rows = await (await db.execute("SELECT payload_json FROM turn_events WHERE id=?", (descriptor["event_id"],))).fetchall()
        import json

        assert len(rows) == 1
        assert json.loads(rows[0][0]) == first.payload
        assert "body" not in first.payload


class _NoAdmissionStore:
    db_path = "unused.db"

    async def admission(self, worker):
        return None


class _NoBindingRunner:
    def __init__(self, team_subagent_store):
        self.team_subagent_store = team_subagent_store

    async def admit_execution_binding(self, **_kwargs):
        return None


def _lead_ctx():
    from types import SimpleNamespace

    return SimpleNamespace(
        collaboration_store=_NoAdmissionStore(),
        session_store=object(),
        team_id="team_1",
        user_id="user_1",
        workspace_id="ws_1",
        session_id="s_1",
        turn_id="t_1",
        execution_generation=1,
        execution_owner_id="owner_1",
    )


@pytest.mark.asyncio
async def test_runner_without_binding_store_keeps_the_compatibility_path():
    """A runner without the durable binding store cannot admit an owner; Lead runs without collaboration."""
    from bowhead_core.agent_collaboration_runtime import prepare_lead_collaboration

    ctx = _lead_ctx()
    binding = await prepare_lead_collaboration(ctx, runner=_NoBindingRunner(None), bus=None, task="task")
    assert binding is None
    assert getattr(ctx, "collaboration_runtime", None) is None


@pytest.mark.asyncio
async def test_missing_owner_binding_with_a_binding_store_still_fails_closed():
    """Any other missing Lead binding keeps the original refusal."""
    from types import SimpleNamespace

    from bowhead_core.agent_collaboration_runtime import prepare_lead_collaboration

    runner = _NoBindingRunner(SimpleNamespace(db_path="bowhead.db"))
    with pytest.raises(CollaborationUnavailable, match="collaboration_owner_unavailable"):
        await prepare_lead_collaboration(_lead_ctx(), runner=runner, bus=None, task="task")
