"""Native document calls borrow actual released Team-Agent executions."""

import json
from copy import copy
from types import SimpleNamespace

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_assignment_store import AgentAssignmentStore
from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
from bowhead_core.document_chat_runtime import worker_from_context
from bowhead_core.documents.agent_caller import DocumentAgentOutcome, validate_document_agent_outcome
from bowhead_core.documents.continuation import DocumentPlanningFailure, DocumentPlanningPause
from bowhead_core.documents.request import DocumentRequestError
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import SubagentRunner
from bowhead_core.user_store import TeamSubagent, UserStore
from test_agent_execution_binding import _insert_release
from test_document_authoring_turn import authored as authored
from test_document_authoring_turn import edits
from test_document_store import SCOPE
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db
from test_document_turn_runner import claim_resume, observe
from test_document_turn_runner import native as native
from test_subagent import _router

from bowhead_router import LLMResponse, ToolCall

pytestmark = pytest.mark.integration


@pytest.fixture(params=["tool", "plugin"])
async def caller(ctx, native, request):
    users = UserStore(ctx.path)
    await users.create_team_subagent(
        TeamSubagent(
            id="osa_exact",
            team_id=SCOPE.team_id,
            name="exact-agent",
            display_name="Document Agent",
            description="Use the native private draft workflow.",
            system_prompt="Use the native document tool.",
            tool_refs=["template_document_submit"],
            visibility="team_wide",
            created_by=SCOPE.user_id,
            created_at="2026-09-04T00:00:00+00:00",
            updated_at="2026-09-04T00:00:00+00:00",
        )
    )
    # Historical frozen release input; these tests exercise caller authority,
    # not publication admission. Production publication has separate tests.
    fixture = SimpleNamespace(db_path=ctx.path, team_id=SCOPE.team_id, owner_id=SCOPE.user_id)
    definition = {"tool_refs": ["template_document_submit"]}
    if request.param == "plugin":
        definition = {"tool_refs": [], "builtin_plugin_ids": ["builtin:document-authoring"]}
    await _insert_release(fixture, sequence=1, prompt="Use the native document tool.", definition_overrides=definition)
    await AgentAssignmentStore(ctx.path).hire(
        team_id=SCOPE.team_id,
        user_id=SCOPE.user_id,
        agent_id="osa_exact",
        agent_source_kind="team",
        live_access=True,
    )
    bindings = AgentExecutionBindingStore(ctx.path)

    async def admit(key="call:document"):
        binding = await bindings.admit_team_execution(
            team_id=SCOPE.team_id,
            user_id=SCOPE.user_id,
            turn_id="t_doc",
            agent_id="osa_exact",
            profile_name="exact-agent",
            dispatch_key=key,
        )
        await bindings.mark_running(binding.execution_id)
        return binding

    binding = await admit()
    context = copy(native.context)
    context.role = "analyst"
    context.document_caller_execution_id = binding.execution_id

    async def exact():
        return binding, False

    context.document_admission_binding = exact
    return SimpleNamespace(binding=binding, bindings=bindings, context=context, users=users, admit=admit, mode=request.param)


async def test_plugin_disable_preserves_independent_tool_selection(ctx, native, caller):
    async with open_shared_ctx(ctx.path) as db:
        await db.execute(
            "INSERT INTO team_settings(team_id,key,value,updated_at) VALUES (?,'builtin_plugins_disabled',?,'now')",
            (SCOPE.team_id, '["document-authoring"]'),
        )
        await db.commit()
    if caller.mode == "plugin":
        with pytest.raises(DocumentRequestError, match="document_capability_unavailable"):
            await native.service.submit(caller.context, ctx.proposal)
    else:
        receipt = await native.service.submit(caller.context, ctx.proposal)
        assert receipt.job_id
    assert not native.calls


async def test_custom_caller_requires_the_interactive_document_runtime(ctx, native, caller, monkeypatch):
    monkeypatch.setenv("FEATURE_DOCUMENT_CHAT_CONTINUATION", "0")
    with pytest.raises(DocumentRequestError, match="document_capability_unavailable"):
        await native.service.submit(caller.context, ctx.proposal)
    async with open_shared_ctx(ctx.path) as db:
        assert (await (await db.execute("SELECT count(*) FROM document_jobs")).fetchone())[0] == 0


async def test_custom_caller_admits_original_job_without_a_synthetic_lead(ctx, native, caller):
    with observe(ctx, native):
        receipt = await native.service.submit(caller.context, ctx.proposal)
        outcome = await native.service.run_turn(caller.context, receipt)
    assert isinstance(outcome, DocumentPlanningPause)
    handoff = DocumentAgentOutcome(caller.binding.execution_id, outcome)
    assert (
        await validate_document_agent_outcome(
            ctx.path,
            worker=worker_from_context(caller.context),
            result=handoff,
            execution_id=caller.binding.execution_id,
        )
        == outcome
    )
    async with open_shared_ctx(ctx.path) as db:
        assert (await (await db.execute("SELECT count(*) FROM turn_agent_execution")).fetchone())[0] == 1
        assert (await (await db.execute("SELECT status FROM turn_agent_execution")).fetchone())[0] == "running"
        row = await (
            await db.execute(
                "SELECT c.agent_execution_id FROM document_dispatches d JOIN turn_capability_execution c ON c.id=d.capability_execution_id WHERE d.job_id=?",
                (receipt.job_id,),
            )
        ).fetchone()
        assert row[0] == caller.binding.execution_id
    with pytest.raises(DocumentRequestError, match="document_checkpoint_unavailable"):
        await validate_document_agent_outcome(ctx.path, worker=worker_from_context(caller.context), result=handoff, execution_id="another-execution")


@pytest.mark.parametrize("mutation", ["assignment", "agent", "tool", "scope", "evaluation"])
async def test_custom_caller_rechecks_authority_before_admission(ctx, native, caller, mutation):
    async with open_shared_ctx(ctx.path) as db:
        if mutation == "assignment":
            await db.execute("UPDATE agent_assignment SET personal_state='disabled'")
        elif mutation == "agent":
            await db.execute("UPDATE team_subagent SET status='disabled'")
        elif mutation == "tool":
            await db.execute(
                "INSERT INTO team_settings(team_id,key,value,updated_at) VALUES (?,'builtin_tool_states',?,'now')",
                (SCOPE.team_id, '{"template_document_submit":{"enabled":false}}'),
            )
        elif mutation == "evaluation":
            await db.execute("UPDATE sessions SET origin='agent_eval' WHERE id='s_doc'")
        else:
            caller.context.workspace_id = "ws_other"
        await db.commit()
    with pytest.raises(DocumentRequestError):
        await native.service.submit(caller.context, ctx.proposal)
    async with open_shared_ctx(ctx.path) as db:
        assert (await (await db.execute("SELECT count(*) FROM document_jobs")).fetchone())[0] == 0
    assert not native.calls


async def test_sibling_cannot_adopt_another_execution_receipt(ctx, native, caller):
    first = await native.service.submit(caller.context, ctx.proposal)
    other = await caller.admit("call:sibling")
    sibling = copy(caller.context)
    sibling.document_caller_execution_id = other.execution_id

    async def exact():
        return other, False

    sibling.document_admission_binding = exact
    with pytest.raises(DocumentRequestError, match="document_command_conflict"):
        await native.service.submit(sibling, ctx.proposal)
    assert await native.service.submit(caller.context, ctx.proposal) == first


async def test_concurrent_siblings_cannot_adopt_the_winning_admission(ctx, native, caller):
    import asyncio

    other = await caller.admit("call:concurrent-sibling")
    sibling = copy(caller.context)
    sibling.document_caller_execution_id = other.execution_id
    arrived = 0
    ready = asyncio.Event()

    def callback(binding):
        async def exact():
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                ready.set()
            await ready.wait()
            return binding, False

        return exact

    caller.context.document_admission_binding = callback(caller.binding)
    sibling.document_admission_binding = callback(other)
    results = await asyncio.gather(
        native.service.submit(caller.context, ctx.proposal), native.service.submit(sibling, ctx.proposal), return_exceptions=True
    )
    assert sum(isinstance(result, DocumentRequestError) and result.code == "document_command_conflict" for result in results) == 1, results
    async with open_shared_ctx(ctx.path) as db:
        assert (await (await db.execute("SELECT count(*) FROM document_jobs")).fetchone())[0] == 1
    assert not native.calls


async def test_pending_selected_source_action_refuses_before_creating_a_job(ctx, native, caller):
    from bowhead_core.documents.agent_caller import DocumentAdmissionGuard

    async def pending():
        raise ValueError("reference_mcp_required_tool_incomplete")

    caller.context.document_admission_guard = DocumentAdmissionGuard(caller.binding.execution_id, pending)
    result = (await native.registry.execute("template_document_submit", native.arguments, caller.context)).result
    assert result["error"] == "document_source_action_pending" and result["document_terminal"] is True
    async with open_shared_ctx(ctx.path) as db:
        assert (await (await db.execute("SELECT count(*) FROM document_jobs")).fetchone())[0] == 0
    assert not native.calls


@pytest.mark.parametrize("disable", [False, True])
async def test_document_question_resumes_original_job_after_caller_settlement(ctx, native, caller, disable):
    with observe(ctx, native):
        receipt = await native.service.submit(caller.context, ctx.proposal)
        outcome = await native.service.run_turn(caller.context, receipt)
    assert isinstance(outcome, DocumentPlanningPause)
    await caller.bindings.mark_terminal(caller.binding.execution_id, status="completed")
    attempt = await claim_resume(ctx, native, outcome)
    if disable:
        async with open_shared_ctx(ctx.path) as db:
            await db.execute("UPDATE agent_assignment SET personal_state='disabled'")
            await db.commit()
    from test_document_turn_runner import ASK

    native.responses = [ASK]
    before = len(native.calls)
    with observe(ctx, native):
        resumed = await native.service.resume_turn(native.context, attempt)
    assert isinstance(resumed, DocumentPlanningFailure)
    assert resumed.job_id == receipt.job_id
    assert len(native.calls) == before + (0 if disable else 1)
    if disable:
        assert resumed.error_code == "document_capability_unavailable"


async def test_real_custom_runner_hands_native_question_back_without_paraphrase(ctx, native, caller):
    router = _router(
        LLMResponse(
            content="", tool_calls=[ToolCall(id="document", name="template_document_submit", arguments=native.arguments)], finish_reason="tool_calls"
        ),
        LLMResponse(content="This extra model call must not run."),
    )
    runner = SubagentRunner(router=router, tool_registry=native.registry, team_subagent_store=caller.users)
    # The parent latch belongs to Lead; the custom run must allocate its own.
    from bowhead_core.documents.turn_policy import DocumentTurnState

    root = copy(native.context)
    root.document_turn_state = DocumentTurnState()
    with observe(ctx, native):
        result = await runner.run(
            profile_name="exact-agent",
            task=ctx.source.user_text,
            parent_turn_id="t_doc",
            parent_bus=TurnEventBus(),
            parent_ctx=root,
            exact_binding=caller.binding,
        )
    assert result.ok and isinstance(result.document_outcome, DocumentAgentOutcome), (result.error, result.tool_invocations)
    assert isinstance(result.document_outcome.outcome, DocumentPlanningPause)
    assert result.document_outcome.execution_id == caller.binding.execution_id
    assert root.document_turn_state.planning_result is None
    assert result.final_answer == "" and result.payload["files_published"] is False
    assert router._registry._adapters["p1"]._script_index == 1


async def test_custom_review_uses_original_digest_confirmation_after_agent_disable(ctx, authored, caller):
    from dataclasses import replace

    from bowhead_core.documents.continuation import DocumentPlanningReview
    from bowhead_core.documents.delivery import DocumentDeliveryStore

    authored.responses = edits(ctx)
    with observe(ctx, authored):
        receipt = await authored.service.submit(caller.context, ctx.proposal)
        outcome = await authored.service.run_turn(caller.context, receipt)
    assert isinstance(outcome, DocumentPlanningReview)
    handoff = DocumentAgentOutcome(caller.binding.execution_id, outcome)
    await validate_document_agent_outcome(
        ctx.path, worker=worker_from_context(caller.context), result=handoff, execution_id=caller.binding.execution_id
    )
    with pytest.raises(DocumentRequestError, match="document_checkpoint_unavailable"):
        await validate_document_agent_outcome(
            ctx.path,
            worker=worker_from_context(caller.context),
            result=replace(handoff, outcome=replace(outcome, draft_digest="a" * 64)),
            execution_id=caller.binding.execution_id,
        )
    async with open_shared_ctx(ctx.path) as db:
        await db.execute("UPDATE agent_assignment SET personal_state='disabled'")
        await db.commit()
    delivery = DocumentDeliveryStore(ctx.artifacts)
    draft = await delivery.get(scope=SCOPE, job_id=outcome.job_id)
    assert draft["state"] == "review"
    with pytest.raises(DocumentRequestError, match="document_not_found"):
        await delivery.read_file(scope=SCOPE, job_id=outcome.job_id, name="candidate.pptx", preview=False)
    published = await delivery.confirm(scope=SCOPE, job_id=outcome.job_id, command_id="confirm-custom", draft_digest=outcome.draft_digest)
    assert published == await delivery.confirm(scope=SCOPE, job_id=outcome.job_id, command_id="confirm-custom", draft_digest=outcome.draft_digest)
    assert len(authored.calls) == 3


async def test_custom_unknown_model_outcome_recovers_without_a_second_job_or_call(ctx, authored, caller):
    import httpx2

    authored.responses = [httpx2.ReadTimeout("Synthetic unknown provider outcome")]
    with observe(ctx, authored):
        receipt = await authored.service.submit(caller.context, ctx.proposal)
        outcome = await authored.service.run_turn(caller.context, receipt)
        assert await authored.service.submit(caller.context, ctx.proposal) == receipt
        recovered = await authored.service.run_turn(caller.context, receipt)
    assert isinstance(outcome, DocumentPlanningFailure) and outcome.error_code == "document_usage_unresolved"
    assert isinstance(recovered, DocumentPlanningFailure) and recovered.job_id == outcome.job_id
    assert len(authored.calls) == 1 and not authored.authoring_worker.operations
    async with open_shared_ctx(ctx.path) as db:
        assert (await (await db.execute("SELECT count(*) FROM document_jobs")).fetchone())[0] == 1
        assert (await (await db.execute("SELECT count(*) FROM document_confirmations")).fetchone())[0] == 0


@pytest.mark.parametrize(
    ("invoke_required", "final_reject", "pause_fault", "shared_tail"),
    [
        (False, False, None, None),
        (True, False, None, None),
        (True, True, None, None),
        (True, False, "owner", None),
        (True, False, "source", None),
        (True, False, "card", None),
        (True, False, "reserved", None),
        (True, False, "rationale", None),
        (True, False, "runtime", None),
        (True, False, None, "terminal"),
        (True, False, None, "maintenance"),
        (True, False, None, "expired"),
        (True, False, None, "buffered_terminal"),
        (True, False, None, "buffered_expired"),
        (True, False, None, "invalid_source"),
        (True, False, None, "missing_card"),
        (True, False, None, "reserved_card"),
        (True, False, None, "consumed_card"),
    ],
)
async def test_exact_mcp_obligation_precedes_native_job_and_original_question_resume(
    tmp_path, monkeypatch, invoke_required, final_reject, pause_fault, shared_tail
):
    from bowhead_core.artifact_store import ArtifactStore, LocalBackend
    from bowhead_core.documents.request import DocumentProposal, DocumentScope, InputRoleProposal, UserQuote
    from bowhead_core.reference_mcp_runtime import ReferenceMcpHandlerScope
    from bowhead_core.session_store import SessionStore
    from test_document_turn_runner import ASK, make_native
    from test_reference_mcp_preparation import _claimed_runtime

    turn_id = "t_document_mcp"
    case, reference, parent, document, read, _write, client = await _claimed_runtime(tmp_path, turn_id=turn_id, document_tool=True)
    if final_reject:
        original_check = ReferenceMcpHandlerScope.require_explicit_tools_completed
        checks = 0

        async def reject_final(self, **kwargs):
            nonlocal checks
            checks += 1
            if checks > 1:
                raise ValueError("reference_mcp_required_tool_incomplete")
            await original_check(self, **kwargs)

        monkeypatch.setattr(ReferenceMcpHandlerScope, "require_explicit_tools_completed", reject_final)
    scope = DocumentScope(team_id=parent.team_id, user_id=parent.user_id, workspace_id=parent.workspace_id)
    artifacts = ArtifactStore(db_path=case.fixture.db_path, backend=LocalBackend(tmp_path / "document-blobs"))
    artifact = await artifacts.put_bytes(
        content=b"replaced by the native fixture",
        filename="template.pptx",
        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        created_by=scope.user_id,
    )
    sessions = SessionStore(case.fixture.db_path)
    await sessions.pin_session_attachments(session_id=parent.session_id, artifact_ids=[artifact.id])
    quote = UserQuote(start=0, text=document.display_text())
    proposal = DocumentProposal(
        evidence=quote, inputs=(InputRoleProposal(artifact_id=artifact.id, role="template", evidence=quote),), requested_outputs=("source", "pdf")
    )
    domain = SimpleNamespace(path=case.fixture.db_path, sessions=sessions, artifacts=artifacts, artifact=artifact, proposal=proposal)
    native = await make_native(
        domain, monkeypatch, tmp_path, scope=scope, turn_id=turn_id, session_id=parent.session_id, owner_id="mcp-worker", claim=False
    )
    native.context.artifact_store = artifacts
    native.context.tool_view = native.registry.clone_with_overlay([parent.tool_view.get(name) for name in parent.tool_view.overlay_names])
    responses = []
    if invoke_required:
        responses.append(
            LLMResponse(content="", tool_calls=[ToolCall(id="selected-source", name=read.id, arguments={"year": 2025})], finish_reason="tool_calls")
        )
    responses.append(
        LLMResponse(
            content="", tool_calls=[ToolCall(id="document", name="template_document_submit", arguments=native.arguments)], finish_reason="tool_calls"
        )
    )
    responses.append(LLMResponse(content="An extra model call must not run."))
    router = _router(*responses)
    requests = []
    adapter = router._registry._adapters["p1"]
    invoke = adapter.invoke

    async def capture(request):
        reply = await invoke(request)
        requests.append({"tools": [item.name for item in request.tools], "reply": [item.name for item in reply.tool_calls]})
        return reply

    monkeypatch.setattr(adapter, "invoke", capture)
    from bowhead_router.types import LLMChunk

    async def stream(request):
        reply = await capture(request)
        for tool_call in reply.tool_calls:
            yield LLMChunk(tool_call_delta=tool_call)
        if reply.content:
            yield LLMChunk(delta=reply.content)
        yield LLMChunk(finish_reason=reply.finish_reason, usage=reply.usage)

    monkeypatch.setattr(adapter, "stream", stream)
    runner = SubagentRunner(router=router, tool_registry=native.registry, team_subagent_store=native.service.state.user_store)
    with observe(domain, native):
        result = await reference.run(runner=runner, parent_ctx=native.context, parent_bus=TurnEventBus())
    async with open_shared_ctx(domain.path) as db:
        count = (await (await db.execute("SELECT count(*) FROM document_jobs")).fetchone())[0]
    if not invoke_required:
        assert not result.ok and result.error == "reference_mcp_required_tool_incomplete"
        assert count == 0 and not native.calls and not client.calls
        return
    if final_reject:
        assert not result.ok and result.error == "reference_mcp_required_tool_incomplete"
        assert result.document_outcome is None and native.context.document_turn_state is None
        assert count == 1 and len(client.calls) == 1
        return
    assert result.ok and result.document_outcome is not None, (result.error, result.tool_invocations, requests)
    assert isinstance(result.document_outcome.outcome, DocumentPlanningPause)
    assert count == 1 and len(client.calls) == 1
    outcome = result.document_outcome.outcome
    from functools import partial

    from bowhead_core.document_chat_runtime import finish_document_planning
    from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore

    if pause_fault is not None:
        from dataclasses import replace
        from unittest.mock import AsyncMock

        from bowhead_core.events import TerminalDurabilityAbort

        if pause_fault == "owner":
            reference = replace(reference, owner_id="another-worker")
        elif pause_fault == "source":
            monkeypatch.setattr("bowhead_core.documents.continuation._read_chat_turn_user_source_in_tx", AsyncMock(return_value=None))
        elif pause_fault == "card":
            monkeypatch.setattr("bowhead_core.document_clarification_lifecycle.document_card_id_in_tx", AsyncMock(return_value=None))
        else:
            from bowhead_core import document_clarification_lifecycle as lifecycle

            original_read = lifecycle.read_document_card_in_tx

            async def changed_card(db, *, card_id):
                record = await original_read(db, card_id=card_id)
                if pause_fault == "reserved":
                    return replace(record, reserved_generation=2)
                if pause_fault == "rationale":
                    return replace(record, card=replace(record.card, rationale="A different question"))
                continuation = record.snapshot.continuation.model_copy(update={"runtime_snapshot_digest": "f" * 64})
                return replace(record, snapshot=record.snapshot.model_copy(update={"continuation": continuation}))

            monkeypatch.setattr(lifecycle, "read_document_card_in_tx", changed_card)
        values = {
            "store": sessions,
            "turn_id": turn_id,
            "status_value": "clarification_pending",
            "intent": "ambiguous",
            "final_answer": "",
            "tokens_input": 0,
            "tokens_output": 0,
            "cost_usd": 0,
            "error": None,
            "error_kind": "needs_clarification",
        }
        if pause_fault == "owner":
            assert await reference.persist_turn_state(**values) is False
        else:
            with pytest.raises(TerminalDurabilityAbort):
                await reference.persist_turn_state(**values)
        assert (await sessions.get_turn(turn_id)).status == "running"
        return
    logs = TurnLogStore(str(domain.path))
    bus = TurnEventBus(log_sink=OrderedTurnLogSink(logs), persist_before_live=True)
    bus._pi_persist_turn_state = partial(reference.persist_turn_state, store=sessions)
    bus._pi_turn_state = {}
    if shared_tail and shared_tail.startswith("buffered_"):
        assert await sessions.append_steer_buffer(session_id=parent.session_id, text="Then summarize the synthetic changes", expected_turn_id=turn_id)
    finished = await finish_document_planning(native.context, outcome, bus=bus)
    assert finished.status.value == "clarification_pending"
    assert (await sessions.get_turn(turn_id)).status == "clarification_pending"
    if shared_tail:
        if shared_tail.endswith("expired"):
            async with open_shared_ctx(domain.path) as db:
                await db.execute("UPDATE chat_turn_spawn_dispatch SET lease_expires_at='2000-01-01' WHERE turn_id=?", (turn_id,))
                await db.commit()
        else:
            assert await sessions.mark_chat_spawn_dispatch_terminal(turn_id=turn_id, owner_id="mcp-worker")
            async with open_shared_ctx(domain.path) as db:
                assert (await (await db.execute("SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id=?", (turn_id,))).fetchone())[
                    0
                ] == "terminal"
        if shared_tail in {"invalid_source", "missing_card", "reserved_card", "consumed_card"}:
            from dataclasses import replace
            from unittest.mock import AsyncMock

            from bowhead_core import document_clarification_lifecycle as lifecycle

            if shared_tail == "invalid_source":
                monkeypatch.setattr("bowhead_core.documents.continuation._read_chat_turn_user_source_in_tx", AsyncMock(return_value=None))
            elif shared_tail == "missing_card":
                monkeypatch.setattr(lifecycle, "document_card_id_in_tx", AsyncMock(return_value=None))
            else:
                original_read = lifecycle.read_document_card_in_tx

                async def changed_card(db, *, card_id):
                    record = await original_read(db, card_id=card_id)
                    return replace(record, **{"reserved_generation" if shared_tail == "reserved_card" else "consumed_generation": 2})

                monkeypatch.setattr(lifecycle, "read_document_card_in_tx", changed_card)
            assert not await sessions.is_turn_resume_ready(turn_id)
            return
        if shared_tail != "terminal":
            await sessions.recover_expired_chat_spawn_dispatches()
        assert (await sessions.get_turn(turn_id)).status == "clarification_pending"
        assert await sessions.is_turn_resume_ready(turn_id)
        async with open_shared_ctx(domain.path) as db:
            assert (await (await db.execute("SELECT count(*) FROM turns WHERE session_id=?", (parent.session_id,))).fetchone())[0] == 1
            assert (await (await db.execute("SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id=?", (turn_id,))).fetchone())[0] == "terminal"
            if shared_tail.startswith("buffered_"):
                buffer = await (await db.execute("SELECT steer_buffer_json FROM sessions WHERE id=?", (parent.session_id,))).fetchone()
                assert [item["text"] for item in json.loads(buffer[0])] == ["Then summarize the synthetic changes"]
    events = await logs.list_events(turn_id)
    assert [event.event_type for event in events if event.event_type in {"clarification_required", "done", "turn_failed"}] == [
        "clarification_required",
        "done",
    ]
    attempt = await claim_resume(domain, native, outcome, already_paused=True, dispatch_settled=shared_tail is not None)
    assert await sessions.claim_chat_spawn_dispatch_for_worker(turn_id=turn_id, owner_id="duplicate-worker") == "rejected"
    native.responses = [ASK]
    with observe(domain, native):
        resumed = await native.service.resume_turn(native.context, attempt)
    assert isinstance(resumed, DocumentPlanningFailure) and resumed.job_id == outcome.job_id
    assert len(client.calls) == 1 and len(native.calls) == 2
