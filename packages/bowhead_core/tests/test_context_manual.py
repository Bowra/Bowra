"""Manual maintenance uses original admission and publishes no synthetic exchange."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_run_limits import DEFAULT_LIMITS, get_runtime_controls
from bowhead_core.chat_turn_source import ChatTurnUserSource
from bowhead_core.context_action import ContextMaintenanceAction
from bowhead_core.context_history import load_conversation_context
from bowhead_core.context_manual import run_manual_context
from bowhead_core.events import OrchestratorEvent
from bowhead_core.session_store import SessionStore
from bowhead_core.subagent import SubagentRunner
from bowhead_core.terminal_generation_bus import GenerationEventBusRegistry
from bowhead_core.terminal_protocol import AdmissionContext, GenerationClaimContext, TerminalAdmissionRequest
from bowhead_core.terminal_runtime import TerminalRuntimeIdentity, TerminalTurnRuntime
from bowhead_core.terminal_runtime_factory import terminal_auth_resolve_context, web_start_execution_intent
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry
from bowhead_core.types import TurnRequest
from bowhead_core.user_store import UserStore
from test_context_maintenance import context_case as context_case
from test_context_terminal import terminal_context_case

pytestmark = pytest.mark.integration


async def admit(runtime, action, *, turn_id="t_manual"):
    request = TurnRequest(
        session_id="s_1", workspace_id="ws_1", user_id="u_1", member_uid="m_1", text="", turn_id=turn_id, team_id="team_1", context_maintenance=action
    )
    source = ChatTurnUserSource(
        turn_id=turn_id,
        session_id="s_1",
        user_id="u_1",
        workspace_id="ws_1",
        user_text="",
        attachment_ids=(),
        attachment_refs=(),
        parts=None,
        context_maintenance=action,
    )
    vector = await runtime.store.ensure_auth_scope_epochs(terminal_auth_resolve_context(request))
    admission = TerminalAdmissionRequest(
        context=AdmissionContext(
            team_id="team_1",
            session_id="s_1",
            request_id=f"req_{turn_id}",
            surface="web",
            cohort="all;config=rollout_1",
            actor="u_1",
            writer_instance_id="gateway_1",
            writer_lease_epoch=7,
            writer_admission_epoch=3,
            rollout_config_generation="rollout_1",
            auth_fence_vector_digest=vector.auth_fence_vector_digest,
        ),
        turn_id=turn_id,
        user_text="",
        execution_intent=web_start_execution_intent(request, source),
        user_source=source,
        presentation_locale="zh-CN",
        message_catalog_version=1,
        allow_steer=False,
    )
    accepted = await runtime.store.admit_or_steer_v1(admission)
    assert accepted.accepted, accepted.code
    claimed = await runtime.store.claim_generation(
        GenerationClaimContext(
            scope=accepted.scope,
            expected_generation_row_version=1,
            expected_session_claim_epoch=0,
            writer_lease_epoch=7,
            owner_instance_id="gateway_1",
        )
    )
    execution = await runtime.store.mark_execution_started(claimed.context)
    generation_bus = await GenerationEventBusRegistry().get_or_create(execution.scope, claim_epoch=execution.claim_epoch)
    active = TerminalTurnRuntime(
        store=runtime.store,
        execution=execution,
        generation_bus=generation_bus,
        identity=TerminalRuntimeIdentity(
            authz_snapshot_digest=vector.auth_fence_vector_digest, writer_admission_epoch=3, owner_instance_id="gateway_1"
        ),
        effect_contract=await runtime.store.read_effect_execution_contract(execution.scope),
    )
    request.terminal_runtime = active
    return request, active, admission


async def base_history(tmp_path, monkeypatch, context_case, *, compact=False):
    runtime, view = await terminal_context_case(tmp_path, monkeypatch, context_case)
    if not compact:
        runtime.bus._context_publication = replace(runtime.bus._context_publication, messages=context_case.request.messages)
    else:
        runtime.bus._context_publication = replace(runtime.bus._context_publication, messages=context_case.current)
    await runtime.bus.emit(OrchestratorEvent("final_answer", "t_1", {"text": "Verified final answer."}))
    await runtime.bus.emit(OrchestratorEvent("done", "t_1", {"status": "completed", "tokens_input": 0, "tokens_output": 0}))
    head = await SessionStore(view.db_path).context_conversation_generation(session_id="s_1")
    return runtime, view, ContextMaintenanceAction(expected_generation=head)


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("bootstrap", [False, True])
async def test_manual_operation_only_summarizes_and_publishes_before_success(tmp_path, monkeypatch, context_case, compact, bootstrap):
    import json
    import time

    runtime, view, action = await base_history(tmp_path, monkeypatch, context_case, compact=compact)
    business_ids = ["t_1"]
    if bootstrap:
        async with open_shared_ctx(view.db_path) as db:
            await db.execute("DELETE FROM model_context_conversation_heads WHERE session_id='s_1'")
            if not compact:
                await db.execute("UPDATE turns SET seq=5,user_text=? WHERE id='t_1'", (context_case.current[0].content,))
                business_ids = []
                for seq in range(1, 5):
                    identity = f"t_legacy_{seq}"
                    business_ids.append(identity)
                    question = context_case.request.messages[(seq - 1) * 2].content
                    answer = context_case.request.messages[(seq - 1) * 2 + 1].content
                    await db.execute(
                        "INSERT INTO turns(id,session_id,seq,user_text,status,started_at,final_answer) VALUES (?,'s_1',?,?,'completed','2026-10-30',?)",
                        (identity, seq, question, answer),
                    )
                business_ids.append("t_1")
            await db.commit()
        action = ContextMaintenanceAction(expected_generation=None)
    request, active, admission = await admit(runtime, action)
    context_case.registry.definition_for("provider")["models"][0]["context_policy"]["enabled"] = False
    store = SessionStore(view.db_path)
    agent_runner = SubagentRunner(router=context_case.router, tool_registry=ToolRegistry(), team_subagent_store=UserStore(view.db_path))
    ctx = ToolExecutionContext(
        workspace_id="ws_1",
        member_uid="m_1",
        user_id="u_1",
        turn_id=request.turn_id,
        datarc=None,
        team_id="team_1",
        session_id="s_1",
        terminal_execution_context=active.execution,
        session_store=store,
        parent_bus=active.bus,
        subagent_runner=agent_runner,
        agent_run_limits=DEFAULT_LIMITS,
        agent_runtime_budget=get_runtime_controls().budget(DEFAULT_LIMITS),
    )
    runner = SimpleNamespace(_t_start=time.perf_counter(), _check_cancel=lambda: None)
    before_calls = len(context_case.sent)
    result = await run_manual_context(runner, request, ctx=ctx, bus=active.bus, agent_runner=agent_runner)
    assert result.status.value == "completed" and result.final_answer == "" and result.tool_invocations == []
    assert context_case.registry.definition_for("provider")["models"][0]["context_policy"]["enabled"] is False
    assert len(context_case.sent) - before_calls == (0 if compact else 1)
    assert all(call.summary_only and not call.tools for call in context_case.sent[before_calls:])
    assert (await runtime.store.admit_or_steer_v1(admission)).scope == active.scope
    lookup = await runtime.store.read_web_chat_action(
        action_id=f"req_{request.turn_id}",
        action_kind="start",
        actor="u_1",
        team_id="team_1",
        turn_id=request.turn_id,
    )
    assert lookup.context_maintenance == action.model_dump()
    assert [turn.id for turn in await store.list_turns(session_id="s_1")] == business_ids
    assert [turn.operation_kind for turn in await store.list_turns(session_id="s_1", include_maintenance=True)] == [
        *[None] * len(business_ids),
        "context_maintenance",
    ]
    head = await store.context_conversation_generation(session_id="s_1")
    assert (head == action.expected_generation) is compact
    async with open_shared_ctx(view.db_path) as db:
        async with db.execute("SELECT event_type,payload_json FROM turn_events WHERE turn_id='t_manual' ORDER BY seq") as cursor:
            events = await cursor.fetchall()
        assert not any(kind == "final_answer" for kind, _ in events)
        phases = [json.loads(payload)["phase"] for kind, payload in events if kind == "agent.context"]
        assert phases == (["skipped"] if compact else ["running", "committed"])
    next_request, next_runtime, _ = await admit(runtime, ContextMaintenanceAction(expected_generation=head), turn_id="t_manual_next")
    next_ctx = replace(ctx, turn_id=next_request.turn_id, terminal_execution_context=next_runtime.execution)
    restored = await load_conversation_context(next_ctx)
    assert restored.generation_id == head
    assert restored.messages[-1].content == "Verified final answer."
    assert not any(message.role == "assistant" and message.content == "" and not message.tool_calls for message in restored.messages)


@pytest.mark.parametrize("expected", [None, "mcg_" + "0" * 32])
async def test_manual_admission_rejects_stale_head_without_creating_a_turn(tmp_path, monkeypatch, context_case, expected):
    runtime, view, _ = await base_history(tmp_path, monkeypatch, context_case)
    with pytest.raises(ValueError, match="context_conversation_head_changed"):
        await admit(runtime, ContextMaintenanceAction(expected_generation=expected))
    assert await SessionStore(view.db_path).get_turn("t_manual") is None


async def test_idle_helper_checkpoint_does_not_block_manual_admission(tmp_path, monkeypatch, context_case):
    from test_subagent_resume_store import _capture

    from bowhead_core import subagent_resume as sr

    runtime, view, action = await base_history(tmp_path, monkeypatch, context_case)
    checkpoint = await _capture(str(view.db_path))
    async with (
        open_shared_ctx(view.db_path) as db,
        db.execute("SELECT state,ciphertext FROM subagent_resume_checkpoints WHERE id=?", (checkpoint,)) as cursor,
    ):
        original = tuple(await cursor.fetchone())
    request, _, _ = await admit(runtime, action)
    assert request.context_maintenance == action
    async with (
        open_shared_ctx(view.db_path) as db,
        db.execute("SELECT state,ciphertext FROM subagent_resume_checkpoints WHERE id=?", (checkpoint,)) as cursor,
    ):
        assert tuple(await cursor.fetchone()) == original
    assert [item.id for item in await sr.list_resumable(str(view.db_path), session_id="s_1", user_id="u_1")] == [checkpoint]


@pytest.mark.parametrize("fault", ["cancel", "prepared", "ack"])
async def test_manual_terminal_failure_never_publishes_uncommitted_context(tmp_path, monkeypatch, context_case, fault):
    import json
    import time
    from datetime import UTC, datetime, timedelta

    from bowhead_core.events import TerminalDurabilityAbort
    from bowhead_core.orchestrator import TurnCancelledError
    from bowhead_core.terminal_protocol import TerminalStatus
    from bowhead_core.terminal_protocol_store import TerminalProtocolFaultPoint
    from bowhead_core.terminal_reconciler import TerminalReconciler

    runtime, view, action = await base_history(tmp_path, monkeypatch, context_case)
    request, active, _ = await admit(runtime, action)
    store = SessionStore(view.db_path)
    agent_runner = SubagentRunner(router=context_case.router, tool_registry=ToolRegistry(), team_subagent_store=UserStore(view.db_path))
    ctx = ToolExecutionContext(
        workspace_id="ws_1",
        member_uid="m_1",
        user_id="u_1",
        turn_id=request.turn_id,
        datarc=None,
        team_id="team_1",
        session_id="s_1",
        terminal_execution_context=active.execution,
        session_store=store,
        parent_bus=active.bus,
        subagent_runner=agent_runner,
        agent_run_limits=DEFAULT_LIMITS,
        agent_runtime_budget=get_runtime_controls().budget(DEFAULT_LIMITS),
    )

    def cancelled():
        if fault == "cancel":
            raise TurnCancelledError()

    fired = False

    def inject(point):
        nonlocal fired
        wanted = (
            TerminalProtocolFaultPoint.CANDIDATE_AFTER_PREPARE if fault == "prepared" else TerminalProtocolFaultPoint.TERMINAL_AFTER_COMMIT_BEFORE_ACK
        )
        if fault != "cancel" and point is wanted and not fired:
            fired = True
            raise OSError("injected maintenance terminal fault")

    runtime.store._fault_injector = inject
    runner = SimpleNamespace(_t_start=time.perf_counter(), _check_cancel=cancelled)
    call = run_manual_context(runner, request, ctx=ctx, bus=active.bus, agent_runner=agent_runner)
    if fault == "ack":
        await call
        assert active.bus.receipt.operation_kind == "context_maintenance"
    else:
        with pytest.raises(TurnCancelledError if fault == "cancel" else TerminalDurabilityAbort):
            await call
    runtime.store._fault_injector = None
    if fault == "cancel":
        receipt = await active.bus.commit_cancelled()
        assert receipt.status is TerminalStatus.CANCELLED and receipt.operation_kind == "context_maintenance"
    elif fault == "prepared":
        async with open_shared_ctx(view.db_path) as db:
            await db.execute(
                "UPDATE turn_generations SET finalization_lease_expires_at=? WHERE turn_id='t_manual'",
                ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(),),
            )
            await db.commit()
        reconciler = TerminalReconciler(runtime.store)
        discovery = (await reconciler.scan(scanner_id="manual-recovery"))[0]
        processing = await reconciler.claim(discovery, owner_instance_id="manual-recovery")
        plan = await runtime.store.load_candidate_for_recovery(processing)
        recovery = next(variant for variant in plan.variants if variant.variant_key.startswith("recovery"))
        receipt = await reconciler.converge(
            processing,
            candidate_builder=lambda _: pytest.fail("Reuse the original candidate"),
            selected_variant_key=recovery.variant_key,
            authz_snapshot_digest="auth_1",
            writer_admission_epoch=3,
        )
        assert receipt.status is TerminalStatus.ABORTED and receipt.operation_kind == "context_maintenance"
    head = await store.context_conversation_generation(session_id="s_1")
    assert (head != action.expected_generation) is (fault == "ack")
    async with open_shared_ctx(view.db_path) as db:
        async with db.execute("SELECT event_type,payload_json FROM turn_events WHERE turn_id='t_manual'") as cursor:
            events = await cursor.fetchall()
        assert not any(kind == "final_answer" for kind, _ in events)
        committed = [json.loads(encoded) for kind, encoded in events if kind == "agent.context" and json.loads(encoded)["phase"] == "committed"]
        assert len(committed) == int(fault == "ack")


async def test_manual_operation_never_accepts_a_business_steer(tmp_path, monkeypatch, context_case):
    runtime, view, action = await base_history(tmp_path, monkeypatch, context_case)
    request, active, admission = await admit(runtime, action)
    business_source = replace(admission.user_source, turn_id="t_business", user_text="Continue the analysis.", context_maintenance=None)
    intent = {**admission.execution_intent, "text": business_source.user_text}
    del intent["context_maintenance"]
    business = replace(
        admission,
        context=replace(admission.context, request_id="req_business"),
        turn_id="t_business",
        user_text=business_source.user_text,
        user_source=None,
        execution_intent=intent,
        allow_steer=True,
    )
    refused = await runtime.store.admit_or_steer_v1(business)
    assert not refused.accepted and refused.code.value == "session_turn_active"
    store = SessionStore(view.db_path)
    assert not await store.append_steer_buffer(session_id="s_1", text="Continue the analysis.", expected_turn_id=request.turn_id)
    assert await store.get_turn("t_business") is None
    async with open_shared_ctx(view.db_path) as db, db.execute("SELECT steer_buffer_json FROM sessions WHERE id='s_1'") as cursor:
        assert (await cursor.fetchone())[0] == "[]"


async def test_expired_manual_owner_recovers_without_a_candidate_or_an_answer(tmp_path, monkeypatch, context_case):
    import json
    from datetime import UTC, datetime, timedelta

    from bowhead_core.terminal_protocol import TerminalStatus
    from bowhead_core.terminal_reconciler import TerminalReconciler, _build_interrupt_candidate

    runtime, view, action = await base_history(tmp_path, monkeypatch, context_case)
    request, active, _ = await admit(runtime, action)
    async with open_shared_ctx(view.db_path) as db:
        await db.execute(
            "UPDATE turn_generations SET lease_expires_at=? WHERE turn_id=?",
            ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), request.turn_id),
        )
        await db.commit()
    recovery = TerminalReconciler(runtime.store)
    discovery = (await recovery.scan(scanner_id="manual-expired"))[0]
    processing = await recovery.claim(discovery, owner_instance_id="manual-expired")
    receipt = await recovery.converge(
        processing,
        candidate_builder=_build_interrupt_candidate,
        selected_variant_key="recovery_none",
        authz_snapshot_digest="auth_recovery",
        writer_admission_epoch=3,
    )
    assert receipt.status is TerminalStatus.ABORTED and receipt.operation_kind == "context_maintenance"
    assert await SessionStore(view.db_path).context_conversation_generation(session_id="s_1") == action.expected_generation
    async with (
        open_shared_ctx(view.db_path) as db,
        db.execute("SELECT event_type,payload_json FROM turn_events WHERE turn_id=?", (request.turn_id,)) as cursor,
    ):
        events = await cursor.fetchall()
    assert not any(kind in {"final_answer", "agent.context"} for kind, _ in events)
    assert [json.loads(payload)["operation_kind"] for kind, payload in events if kind == "done"] == ["context_maintenance"]
