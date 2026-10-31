"""Context staging reuses the exact original Terminal v1 live capability."""

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.terminal_protocol import TerminalProtocolMalformedError
from bowhead_core.terminal_protocol_store import require_live_execution_in_connection
from test_context_maintenance import context_case as context_case
from test_terminal_runtime import _NOW, _create_db, _runtime

pytestmark = pytest.mark.integration


async def test_context_staging_guard_is_read_only_and_keeps_original_writer_epochs(tmp_path):
    path = _create_db(tmp_path)
    runtime = await _runtime(path)
    execution = runtime.execution
    async with open_shared_ctx(path) as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("SELECT row_version,claim_epoch,lease_expires_at FROM turn_generations WHERE turn_id='t_1'") as cursor:
            before = await cursor.fetchone()
        await require_live_execution_in_connection(db, context=execution, now=_NOW)
        async with db.execute("SELECT row_version,claim_epoch,lease_expires_at FROM turn_generations WHERE turn_id='t_1'") as cursor:
            assert await cursor.fetchone() == before
        with pytest.raises(TerminalProtocolMalformedError, match="ownership changed"):
            await require_live_execution_in_connection(db, context=replace(execution, claim_epoch=execution.claim_epoch + 1), now=_NOW)
        await db.execute("UPDATE terminal_protocol_writer_lease SET admission_state='draining'")
        await require_live_execution_in_connection(db, context=execution, now=_NOW)
        await db.execute("UPDATE terminal_protocol_writer_lease SET epoch=epoch+1")
        with pytest.raises(TerminalProtocolMalformedError, match="writer lease"):
            await require_live_execution_in_connection(db, context=execution, now=_NOW)


async def test_context_staging_cannot_reuse_a_running_capability_after_finalization_begins(tmp_path):
    path = _create_db(tmp_path)
    runtime = await _runtime(path)
    await runtime.store.begin_finalization(runtime.execution, finalization_owner_instance_id="gateway_1", closing_reason="context-test")
    async with open_shared_ctx(path) as db:
        await db.execute("BEGIN")
        with pytest.raises(TerminalProtocolMalformedError, match="ownership changed"):
            await require_live_execution_in_connection(db, context=runtime.execution, now=_NOW)


async def terminal_context_case(tmp_path, monkeypatch, context_case):
    import test_terminal_runtime as fixtures
    from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
    from bowhead_core.chat_reference_storage import ReferenceCipher
    from bowhead_core.context_execution import ExecutionContextView
    from bowhead_core.context_ownership import TerminalContextOwner
    from bowhead_core.subagent import SubagentProfile, _system_profile_snapshot
    from cryptography.fernet import Fernet

    monkeypatch.setattr(fixtures, "_NOW", datetime.now(UTC))
    path = _create_db(tmp_path)
    async with open_shared_ctx(path) as db:
        await db.execute("INSERT INTO users(id,team_id,email,status,created_at) VALUES ('u_1','team_1','context@example.test','active','t')")
        await db.execute(
            "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES ('tm_context','u_1','team_1','team_admin','active','t')"
        )
        await db.execute(
            "INSERT INTO workspace_members(workspace_id,user_id,role,status,joined_at,last_active_at) VALUES ('ws_1','u_1','admin','active','t','t')"
        )
        await db.commit()
    runtime = await _runtime(path)
    bindings = AgentExecutionBindingStore(path)
    binding = await bindings.admit_system_execution(
        team_id="team_1",
        user_id="u_1",
        turn_id="t_1",
        profile_name="lead",
        dispatch_key="context-root",
        definition=_system_profile_snapshot(
            SubagentProfile(name="lead", llm_agent_type="lead", tool_whitelist=(), system_prompt="Follow the request.")
        ),
        manifest_fingerprint="sha256:" + "1" * 64,
        override_version=None,
        system_required=True,
        enforce_current_override=False,
    )
    await bindings.mark_running(binding.execution_id)
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", key)
    view = ExecutionContextView(
        db_path=path,
        worker=TerminalContextOwner(runtime.execution, "u_1", "ws_1"),
        execution_id=binding.execution_id,
        lane="ordinary",
        cipher=ReferenceCipher(key),
        validate_sources=AsyncMock(),
        activity=AsyncMock(),
    )
    from test_context_execution import prepare

    await prepare(view, context_case)
    runtime.bus._context_publication = await view.conversation_publication(context_case.request.messages)
    return runtime, view


@pytest.mark.parametrize("fault", [None, "before_commit", "after_commit"])
async def test_terminal_head_and_receipt_commit_together_with_ack_recovery(tmp_path, monkeypatch, context_case, fault):
    from bowhead_core.events import OrchestratorEvent, TerminalDurabilityAbort
    from bowhead_core.terminal_protocol_store import TerminalProtocolFaultPoint

    runtime, view = await terminal_context_case(tmp_path, monkeypatch, context_case)
    fired = False

    def inject(point):
        nonlocal fired
        wanted = (
            TerminalProtocolFaultPoint.TERMINAL_BEFORE_COMMIT
            if fault == "before_commit"
            else TerminalProtocolFaultPoint.TERMINAL_AFTER_COMMIT_BEFORE_ACK
        )
        if fault and point is wanted and not fired:
            fired = True
            raise OSError("injected_context_terminal_commit")

    runtime.store._fault_injector = inject
    await runtime.bus.emit(OrchestratorEvent("final_answer", "t_1", {"text": "Final verified answer."}))
    done = OrchestratorEvent("done", "t_1", {"status": "completed", "tokens_input": 0, "tokens_output": 0})
    if fault == "before_commit":
        with pytest.raises(TerminalDurabilityAbort):
            await runtime.bus.emit(done)
    else:
        await runtime.bus.emit(done)
    async with open_shared_ctx(view.db_path) as db:
        async with db.execute("SELECT generation_id FROM model_context_conversation_heads WHERE session_id='s_1'") as cursor:
            head = await cursor.fetchone()
        async with db.execute("SELECT COUNT(*) FROM turn_terminal_receipts WHERE turn_id='t_1'") as cursor:
            receipts = (await cursor.fetchone())[0]
    assert bool(head) == (fault != "before_commit")
    assert receipts == int(fault != "before_commit")
    if head:
        assert head[0] == runtime.bus._staged_context_publication.projection.generation_id


async def test_prepared_terminal_recovery_interrupt_does_not_adopt_context(tmp_path, monkeypatch, context_case):
    from datetime import timedelta

    from bowhead_core.events import OrchestratorEvent, TerminalDurabilityAbort
    from bowhead_core.terminal_protocol import TerminalStatus
    from bowhead_core.terminal_protocol_store import TerminalProtocolFaultPoint
    from bowhead_core.terminal_reconciler import TerminalReconciler

    runtime, view = await terminal_context_case(tmp_path, monkeypatch, context_case)

    def inject(point):
        if point is TerminalProtocolFaultPoint.CANDIDATE_AFTER_PREPARE:
            raise OSError("injected_context_candidate_crash")

    runtime.store._fault_injector = inject
    await runtime.bus.emit(OrchestratorEvent("final_answer", "t_1", {"text": "Final verified answer."}))
    with pytest.raises(TerminalDurabilityAbort):
        await runtime.bus.emit(OrchestratorEvent("done", "t_1", {"status": "completed", "tokens_input": 0, "tokens_output": 0}))
    runtime.store._fault_injector = None
    async with open_shared_ctx(view.db_path) as db:
        await db.execute("UPDATE turn_generations SET finalization_lease_expires_at=?", ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(),))
        await db.commit()
    reconciler = TerminalReconciler(runtime.store)
    discoveries = await reconciler.scan(scanner_id="context-recovery")
    assert len(discoveries) == 1
    processing = await reconciler.claim(discoveries[0], owner_instance_id="context-recovery")
    plan = await runtime.store.load_candidate_for_recovery(processing)
    recovery = next(item for item in plan.variants if item.variant_key.startswith("recovery"))
    receipt = await reconciler.converge(
        processing,
        candidate_builder=lambda _: pytest.fail("The original prepared candidate must be reused"),
        selected_variant_key=recovery.variant_key,
        authz_snapshot_digest="auth_recovery",
        writer_admission_epoch=3,
    )
    assert receipt.status is TerminalStatus.ABORTED
    async with open_shared_ctx(view.db_path) as db, db.execute("SELECT COUNT(*) FROM model_context_conversation_heads") as cursor:
        assert (await cursor.fetchone())[0] == 0


async def test_terminal_source_revocation_after_prepare_rolls_back_publication_and_receipt(tmp_path, monkeypatch, context_case):
    from bowhead_core.events import OrchestratorEvent, TerminalDurabilityAbort
    from bowhead_core.terminal_protocol_store import TerminalProtocolFaultPoint

    runtime, view = await terminal_context_case(tmp_path, monkeypatch, context_case)

    def revoke(point):
        if point is TerminalProtocolFaultPoint.CANDIDATE_AFTER_PREPARE:
            view.validate_sources.side_effect = PermissionError("The retained source was revoked")

    runtime.store._fault_injector = revoke
    await runtime.bus.emit(OrchestratorEvent("final_answer", "t_1", {"text": "Final verified answer."}))
    with pytest.raises(TerminalDurabilityAbort):
        await runtime.bus.emit(OrchestratorEvent("done", "t_1", {"status": "completed", "tokens_input": 0, "tokens_output": 0}))
    async with open_shared_ctx(view.db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM model_context_conversation_heads") as cursor:
            assert (await cursor.fetchone())[0] == 0
        async with db.execute("SELECT COUNT(*) FROM turn_terminal_receipts") as cursor:
            assert (await cursor.fetchone())[0] == 0


async def test_terminal_recovery_repairs_committed_activity_without_publishing_a_conversation(tmp_path, monkeypatch, context_case):
    import json
    from datetime import timedelta

    from bowhead_core.terminal_protocol import TerminalStatus
    from bowhead_core.terminal_reconciler import TerminalReconciler, _build_interrupt_candidate

    runtime, view = await terminal_context_case(tmp_path, monkeypatch, context_case)
    async with open_shared_ctx(view.db_path) as db:
        await db.execute("UPDATE turn_generations SET lease_expires_at=?", ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(),))
        await db.commit()
    reconciler = TerminalReconciler(runtime.store)
    discoveries = await reconciler.scan(scanner_id="context-recovery")
    assert len(discoveries) == 1
    processing = await reconciler.claim(discoveries[0], owner_instance_id="context-recovery")
    receipt = await reconciler.converge(
        processing,
        candidate_builder=_build_interrupt_candidate,
        selected_variant_key="recovery_none",
        authz_snapshot_digest="auth_recovery",
        writer_admission_epoch=3,
    )
    assert receipt.status is TerminalStatus.ABORTED
    assert await reconciler.scan(scanner_id="context-recovery") == ()
    async with open_shared_ctx(view.db_path) as db:
        async with db.execute("SELECT payload_json FROM turn_events WHERE event_type='agent.context'") as cursor:
            rows = await cursor.fetchall()
        assert len(rows) == 1
        payload = json.loads(rows[0][0])
        assert payload["phase"] == "committed" and payload["generation_id"] == view.generation
        async with db.execute("SELECT COUNT(*) FROM model_context_conversation_heads") as cursor:
            assert (await cursor.fetchone())[0] == 0


async def test_original_terminal_runner_compacts_with_its_actual_execution_capability(tmp_path, monkeypatch, context_case):
    from copy import deepcopy

    from bowhead_core.agent_run_limits import DEFAULT_LIMITS, get_runtime_controls
    from bowhead_core.context_history import ConversationContext, context_history_available, prepare_context_lead
    from bowhead_core.events import OrchestratorEvent
    from bowhead_core.session_store import SessionStore
    from bowhead_core.subagent import SubagentRunner
    from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry
    from bowhead_core.user_store import UserStore
    from bowhead_router.types import LLMResponse, TokenUsage

    runtime, view = await terminal_context_case(tmp_path, monkeypatch, context_case)
    case = context_case
    sent = []

    async def invoke(request):
        sent.append(deepcopy(request))
        return LLMResponse(
            content="Earlier evidence establishes the refund question. Continue with the latest request." if request.summary_only else "Hello.",
            finish_reason="stop",
            usage=TokenUsage(input_tokens=101, output_tokens=17),
        )

    monkeypatch.setattr(case.registry.get("provider"), "invoke", invoke)
    runner = SubagentRunner(router=case.router, tool_registry=ToolRegistry(), team_subagent_store=UserStore(view.db_path))
    ctx = ToolExecutionContext(
        workspace_id="ws_1",
        member_uid="u_1",
        user_id="u_1",
        turn_id="t_1",
        datarc=None,
        team_id="team_1",
        session_id="s_1",
        terminal_execution_context=runtime.execution,
        session_store=SessionStore(view.db_path),
        parent_bus=runtime.bus,
        subagent_runner=runner,
        agent_runtime_budget=get_runtime_controls().budget(DEFAULT_LIMITS),
    )
    assert context_history_available(ctx, case.router)
    assert ctx.execution_owner_id is None
    binding = await prepare_context_lead(ctx, runner=runner, bus=runtime.bus, task="Say hello.")
    assert binding is not None
    history = deepcopy(case.request.messages[:8])
    ctx._model_context_history = ConversationContext(None, 0, history, 0)
    result = await runner.run(
        profile_name="lead",
        task="Say hello.",
        parent_turn_id="t_1",
        parent_ctx=ctx,
        parent_bus=runtime.bus,
        exact_binding=binding,
        history=history,
    )
    assert result.ok, result.error
    assert any(item.summary_only for item in sent)
    await runtime.bus.emit(OrchestratorEvent("final_answer", "t_1", {"text": result.final_answer}))
    await runtime.bus.emit(
        OrchestratorEvent("done", "t_1", {"status": "completed", "tokens_input": result.tokens_input, "tokens_output": result.tokens_output})
    )
    async with open_shared_ctx(view.db_path) as db:
        async with db.execute("SELECT generation_id FROM model_context_conversation_heads WHERE session_id='s_1'") as cursor:
            assert (await cursor.fetchone())[0] == runtime.bus._staged_context_publication.projection.generation_id
        async with db.execute("SELECT payload_json FROM turn_events WHERE event_type='agent.context' ORDER BY seq") as cursor:
            import json

            assert [json.loads(row[0])["phase"] for row in await cursor.fetchall()] == ["running", "committed"]
