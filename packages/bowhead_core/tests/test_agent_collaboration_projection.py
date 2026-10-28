"""Authorized trace projections preserve facts without executing work."""

from __future__ import annotations

import asyncio
import json

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_collaboration import CollaborationUnavailable, EvidenceReference, authorize_result_exchange
from bowhead_core.agent_collaboration_runtime import CollaborationRuntime
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.events import OrchestratorEvent, TurnEventBus
from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore
from test_agent_collaboration import collab as collaboration_fixture
from test_subagent_step_emit_contract import _make_ctx, _make_runner

from bowhead_router import ToolCall

pytestmark = pytest.mark.integration
collab = collaboration_fixture


async def test_trace_distinguishes_reply_resolution_and_execution_completion(collab):
    question = await collab.ask()
    before = await collab.store.projection(collab.worker, authorize=collab.authorize)
    assert before["topics"][0]["response_status"] == "pending"
    assert before["topics"][0]["dependency_status"] == "open"
    reply = await collab.reply(question)
    answered = await collab.store.projection(collab.worker, authorize=collab.authorize)
    topic = answered["topics"][0]
    assert topic["response_status"] == "answered"
    assert topic["dependency_status"] == "open"
    assert topic["messages"][0]["body"] == "Which refund definition applies?"
    assert topic["messages"][1]["body"] == "Refunds exclude shipping."
    await collab.store.resolve(
        collab.worker,
        identity=collab.identity(step="verification"),
        question_id=question["operation_id"],
        reply_id=reply["operation_id"],
        disposition="resolved",
        reason="The definition matches the supplied evidence.",
        authorize=collab.authorize,
    )
    resolved = await collab.store.projection(collab.worker, authorize=collab.authorize)
    assert resolved["topics"][0]["dependency_status"] == "resolved"
    assert all(item["status"] == "running" for item in resolved["participants"])
    assert before["revision"] < answered["revision"] < resolved["revision"]
    assert await collab.store.projection(collab.worker, authorize=collab.authorize) == resolved


async def test_revoked_explicit_topic_evidence_hides_derived_reply_and_inbox(collab):
    question = await collab.ask(references=(EvidenceReference(identity="refund-policy", version="v1"),))
    reply = await collab.reply(question, body="The restricted policy says shipping is excluded.")
    collab.readable.clear()
    projection = await collab.store.projection(collab.worker, authorize=collab.authorize)
    messages = projection["topics"][0]["messages"]
    assert len(messages) == 2
    assert all(message["unavailable"] == "collaboration_material_unavailable" for message in messages)
    encoded = json.dumps(projection)
    assert "refund-policy" not in encoded
    assert "shipping" not in encoded
    assert "Which refund" not in encoded
    with pytest.raises(CollaborationUnavailable):
        await collab.read(reply)
    inbox = await collab.store.inbox(collab.worker, execution_id=collab.requester, after=0, authorize=collab.authorize)
    assert inbox["items"][0]["unavailable"] == "collaboration_material_unavailable"
    assert "body" not in inbox["items"][0]


async def test_projection_does_not_expire_acknowledge_or_wake_waiting_work(collab):
    question = await collab.ask()
    wait = await collab.store.begin_wait(
        collab.worker,
        identity=collab.identity(step="wait"),
        question_ids=(question["operation_id"],),
        after=0,
        authorize=collab.authorize,
    )
    collab.clock[0] += 1000
    before = await collab.count()
    projection = await collab.store.projection(collab.worker, authorize=collab.authorize)
    assert projection["topics"][0]["response_status"] == "pending"
    assert next(p for p in projection["participants"] if p["execution_id"] == collab.requester)["waiting"] is True
    async with (
        open_shared_ctx(collab.path) as db,
        db.execute("SELECT wait_operation_id FROM agent_collaboration_participant WHERE execution_id=?", (collab.requester,)) as cursor,
    ):
        assert (await cursor.fetchone())[0] == wait["operation_id"]
    assert await collab.count() == before


async def test_terminal_trace_preserves_unresolved_evidence_without_restarting(collab):
    await collab.ask()
    await collab.bindings.mark_terminal(collab.recipient, status="cancelled")
    await collab.bindings.mark_terminal(collab.requester, status="cancelled")
    projection = await collab.store.projection(collab.worker, authorize=collab.authorize)
    assert projection["topics"][0]["response_status"] == "unavailable"
    assert projection["topics"][0]["dependency_status"] == "open"
    assert all(not participant["waiting"] for participant in projection["participants"])
    assert projection["topics"][0]["messages"][0]["body"] == "Which refund definition applies?"


@pytest.mark.parametrize("fail", [False, True])
async def test_message_tool_prose_never_leaks_through_live_or_replayed_tool_events(collab, fail):
    secret = "Restricted refund policy and private source identity"

    async def handler(args, ctx):
        if fail:
            raise ValueError(secret)
        return {"ok": True, "items": [{"body": secret, "references": [{"identity": "private-source"}]}]}

    runner = _make_runner({"agent_message": handler})
    ctx = _make_ctx(collab.worker.turn_id)
    ctx.pycell_counter_ref = [0]
    logs = TurnLogStore(collab.path)
    bus = TurnEventBus(log_sink=OrderedTurnLogSink(logs), persist_before_live=True)
    events = []

    async def consume():
        async for event in bus.subscribe():
            events.append(event)

    consumer = asyncio.create_task(consume())
    invocation = await runner._execute_tool(
        ToolCall(id="message-call", name="agent_message", arguments={"action": "reply", "body": secret}),
        ctx,
        bus,
        parent_turn_id=collab.worker.turn_id,
        subagent_id="peer",
        running_profile="compute",
    )
    await bus.emit(OrchestratorEvent(event_type="done", turn_id=collab.worker.turn_id, payload={}))
    await consumer
    assert invocation.arguments["body"] == secret
    if not fail:
        assert invocation.result["items"][0]["body"] == secret
    live = json.dumps([event.payload for event in events])
    async with (
        open_shared_ctx(collab.path) as db,
        db.execute("SELECT payload_json FROM turn_events WHERE turn_id=?", (collab.worker.turn_id,)) as cursor,
    ):
        persisted = "\n".join(row[0] for row in await cursor.fetchall())
    for rendered in (live, persisted):
        assert secret not in rendered
        assert "private-source" not in rendered
    assert not any(event.event_type.startswith("pycell.") for event in events)
    assert ctx.pycell_counter_ref == [0]
    assert next(event for event in events if event.event_type == "step_done").payload["state"] == ("failure" if fail else "success")


async def test_corrected_source_version_is_readable_without_revealing_old_version(collab):
    allowed_version = ["v1"]

    async def authorize(db, worker, sender, recipient, content):
        await collab.authorize(db, worker, sender, recipient, content)
        if any(reference["version"] != allowed_version[0] for reference in content["references"]):
            raise CollaborationUnavailable("collaboration_material_unavailable")

    question = await collab.ask()
    reply = await collab.reply(question, body="The old policy applied.", references=(EvidenceReference(identity="refund-policy", version="v1"),))
    allowed_version[0] = "v2"
    correction = await collab.store.update(
        collab.worker,
        identity=collab.identity(collab.recipient, step="correction"),
        recipient_id=collab.requester,
        supersedes=reply["operation_id"],
        body="The revised policy applies.",
        references=(EvidenceReference(identity="refund-policy", version="v2"),),
        authorize=authorize,
    )
    projection = await collab.store.projection(collab.worker, authorize=authorize)
    messages = projection["topics"][0]["messages"]
    assert messages[0]["body"] == "Which refund definition applies?"
    assert messages[1]["unavailable"] == "collaboration_material_unavailable"
    assert messages[2]["operation_id"] == correction["operation_id"]
    assert messages[2]["body"] == "The revised policy applies."
    assert messages[2]["references"] == [{"identity": "refund-policy", "version": "v2"}]
    assert "The old policy applied." not in json.dumps(projection)
    assert projection["topics"][0]["dependency_status"] == "open"


async def test_trace_rechecks_original_caller_membership(collab):
    await collab.ask()
    async with open_shared_ctx(collab.path) as db:
        await db.execute(
            "UPDATE workspace_members SET status='removed' WHERE workspace_id=? AND user_id=?", (collab.worker.workspace_id, collab.worker.user_id)
        )
        await db.commit()
    with pytest.raises(ValueError, match="reference_scope_unavailable"):
        await collab.store.projection(collab.worker, authorize=collab.authorize)


async def test_archived_space_history_does_not_reenable_live_reads(collab):
    await collab.ask()
    async with open_shared_ctx(collab.path) as db:
        await db.execute("UPDATE workspaces SET status='archived' WHERE id=?", (collab.worker.workspace_id,))
        await db.commit()
    projection = await collab.store.projection(collab.worker, authorize=collab.authorize)
    assert projection["topics"][0]["messages"][0]["body"] == "Which refund definition applies?"
    with pytest.raises(ValueError, match="reference_scope_unavailable"):
        await collab.store.inbox(collab.worker, execution_id=collab.requester, after=0, authorize=collab.authorize)


async def test_production_reader_allows_professional_replies_without_tool_grants(collab):
    question = await collab.ask(authorize=authorize_result_exchange)
    await collab.reply(question, authorize=authorize_result_exchange)
    projection = await collab.store.projection(collab.worker, authorize=authorize_result_exchange)
    assert projection["topics"][0]["messages"][1]["body"] == "Refunds exclude shipping."
    with pytest.raises(CollaborationUnavailable, match="collaboration_material_unavailable"):
        await collab.ask(
            identity=collab.identity(step="unsupported-evidence"),
            references=(EvidenceReference(identity="missing-source", version="v1"),),
            authorize=authorize_result_exchange,
        )


async def test_result_attachment_authority_is_rechecked_for_reply_and_history(collab, tmp_path):
    artifacts = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "artifacts"), db_path=collab.path)
    artifact = await artifacts.put_bytes(
        content=b"refund,shipping\n10,0",
        filename="refunds.csv",
        mime="text/csv",
        created_by=collab.worker.user_id,
        created_by_turn=collab.worker.turn_id,
        source="tool",
    )
    question = await collab.ask(authorize=authorize_result_exchange)
    await collab.reply(question, references=(EvidenceReference(identity=artifact.id, version=artifact.id),), authorize=authorize_result_exchange)
    visible = await collab.store.projection(collab.worker, authorize=authorize_result_exchange)
    assert visible["topics"][0]["messages"][1]["references"][0]["identity"] == artifact.id
    async with open_shared_ctx(collab.path) as db:
        await db.execute("UPDATE artifacts SET deleted_at='2026-10-09T00:00:00Z' WHERE id=?", (artifact.id,))
        await db.commit()
    revoked = await collab.store.projection(collab.worker, authorize=authorize_result_exchange)
    assert revoked["topics"][0]["messages"][1]["unavailable"] == "collaboration_material_unavailable"
    assert artifact.id not in json.dumps(revoked)
    assert "Refunds exclude" not in json.dumps(revoked)


async def test_committed_outbox_retries_without_republishing_or_resequencing(collab):
    log = TurnLogStore(str(collab.path))
    await log.initialize()
    sink = OrderedTurnLogSink(log)
    bus = TurnEventBus(log_sink=sink)
    for index in range(4):
        await bus.emit(OrchestratorEvent("thinking", collab.worker.turn_id, {"text": f"Step {index}"}))
    question = await collab.ask()
    intent = (await collab.store.pending_publications(collab.worker))[0]
    # Simulate a crash after durable publication but before outbox acknowledgement.
    await bus.emit(
        OrchestratorEvent(
            "agent.collaboration",
            collab.worker.turn_id,
            intent["payload"],
            occurred_at=intent["occurred_at"],
            durable_event_id=intent["event_id"],
            persist_before_delivery=True,
        )
    )
    original = (await log.list_events(collab.worker.turn_id))[-1]
    assert original.payload["evt_seq"] == 5
    fresh = TurnEventBus(log_sink=OrderedTurnLogSink(log))
    runtime = CollaborationRuntime(collab.store, collab.worker, authorize=collab.authorize, bus=fresh)
    await runtime.publish_pending()
    assert fresh.snapshot() == []
    assert await collab.store.pending_publications(collab.worker) == []
    await collab.reply(question)
    await runtime.notify()
    events = fresh.snapshot()
    assert len(events) == 1 and events[0].seq == 6
    rows = await log.list_events(collab.worker.turn_id)
    assert rows[-2].payload == original.payload
    assert rows[-1].payload == events[0].payload
    assert "Refunds" not in json.dumps([row.payload for row in rows])


async def test_failed_publication_keeps_intent_and_terminal_drain_recovers(collab):
    question = await collab.ask()
    missing_sink = TurnEventBus()
    failed = CollaborationRuntime(collab.store, collab.worker, authorize=collab.authorize, bus=missing_sink)
    await failed.notify()
    assert missing_sink.snapshot() == []
    assert len(await collab.store.pending_publications(collab.worker)) == 1
    log = TurnLogStore(str(collab.path))
    await log.initialize()
    bus = TurnEventBus(log_sink=OrderedTurnLogSink(log))
    CollaborationRuntime(collab.store, collab.worker, authorize=collab.authorize, bus=bus)
    await bus.emit(OrchestratorEvent("done", collab.worker.turn_id, {"status": "failed"}))
    assert [event.event_type for event in bus.snapshot()] == ["agent.collaboration", "done"]
    assert bus.snapshot()[0].payload["operation_id"] == question["operation_id"]
    assert await collab.store.pending_publications(collab.worker) == []


@pytest.mark.parametrize("already_persisted", [False, True])
async def test_owner_loss_reuses_dispatch_recovery_before_terminal_publication(collab, already_persisted):
    from bowhead_core.session_store import SessionStore

    question = await collab.ask()
    await collab.store.begin_wait(
        collab.worker,
        identity=collab.identity(step="waiting"),
        question_ids=(question["operation_id"],),
        after=0,
        authorize=collab.authorize,
    )
    original = None
    if already_persisted:
        logs = TurnLogStore(collab.path)
        await logs.initialize()
        bus = TurnEventBus(log_sink=OrderedTurnLogSink(logs))
        intent = (await collab.store.pending_publications(collab.worker))[0]
        await bus.emit(
            OrchestratorEvent(
                "agent.collaboration",
                collab.worker.turn_id,
                intent["payload"],
                occurred_at=intent["occurred_at"],
                durable_event_id=intent["event_id"],
                persist_before_delivery=True,
            )
        )
        original = (await logs.list_events(collab.worker.turn_id))[0]
    async with open_shared_ctx(collab.path) as db:
        await db.execute("UPDATE chat_turn_spawn_dispatch SET lease_expires_at='2000-01-01T00:00:00Z' WHERE turn_id=?", (collab.worker.turn_id,))
        await db.commit()
    sessions = SessionStore(collab.path)
    assert await sessions.recover_expired_chat_spawn_dispatches() == [collab.worker.turn_id]
    logs = TurnLogStore(collab.path)
    first = await logs.list_events(collab.worker.turn_id)
    collaboration = [event for event in first if event.event_type == "agent.collaboration"]
    assert len(collaboration) == 2
    if original is not None:
        assert collaboration[0] == original
    assert first[-1].event_type == "done" and first[-1].payload["status"] == "failed"
    assert all(event.seq < first[-1].seq for event in collaboration)
    assert all(event.payload["seq"] == event.seq == event.payload["evt_seq"] for event in collaboration)
    assert "Which refund" not in json.dumps([event.payload for event in first])
    state = await collab.read(question)
    assert (state["response_status"], state["dependency_status"]) == ("cancelled", "cancelled")
    assert await collab.store.pending_publications(collab.worker) == []
    assert await sessions.recover_expired_chat_spawn_dispatches() == []
    assert await logs.list_events(collab.worker.turn_id) == first


@pytest.mark.parametrize("within_consumption", [False, True])
async def test_collaboration_checkpoint_rechecks_current_result_authority(collab, tmp_path, within_consumption):
    from types import SimpleNamespace

    from bowhead_core.agent_collaboration_runtime import CollaborationExecution, validate_collaboration_checkpoint
    from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable

    artifacts = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "artifacts"), db_path=collab.path)
    artifact = await artifacts.put_bytes(
        content=b"Verified refund definition",
        filename="definition.txt",
        mime="text/plain",
        created_by=collab.worker.user_id,
        created_by_turn=collab.worker.turn_id,
        source="tool",
    )
    await collab.ask(references=(EvidenceReference(identity=artifact.id, version=artifact.id),), authorize=authorize_result_exchange)
    runtime = CollaborationRuntime(collab.store, collab.worker, authorize=authorize_result_exchange)
    checkpoint = SimpleNamespace(collaboration=await runtime.checkpoint(CollaborationExecution(runtime, collab.lead)))
    ctx = SimpleNamespace(**collab.worker.model_dump(exclude={"generation", "owner_id"}), collaboration_store=collab.store)
    await validate_collaboration_checkpoint(ctx, checkpoint)
    async with open_shared_ctx(collab.path) as db:
        await db.execute("UPDATE artifacts SET deleted_at='2026-10-10T00:00:00Z' WHERE id=?", (artifact.id,))
        await db.commit()
    if within_consumption:
        async with open_shared_ctx(collab.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            with pytest.raises(OrdinaryContinuationUnavailable):
                await validate_collaboration_checkpoint(ctx, checkpoint, db=db)
    else:
        with pytest.raises(OrdinaryContinuationUnavailable):
            await validate_collaboration_checkpoint(ctx, checkpoint)
    assert await collab.count() == 1
