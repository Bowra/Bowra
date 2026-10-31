from __future__ import annotations

import ast
import asyncio
import inspect
import json
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.access_control import AccessControlStore
from bowhead_core.agents.lead_orchestrator import _PUBLIC_TURN_FAILURE_CODES
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.chat_turn_source import ChatTurnUserSource
from bowhead_core.events import OrchestratorEvent
from bowhead_core.language_context import LanguageContext
from bowhead_core.migrations.baseline_schema import initial_schema
from bowhead_core.plan_store import PlanStatus, PlanStore
from bowhead_core.session_lifecycle import (
    SESSION_LIFECYCLE_ERROR_CODES,
    SessionLifecycleCoordinator,
    SessionLifecycleError,
)
from bowhead_core.session_store import SessionProtocolManagedError, SessionStore
from bowhead_core.terminal_effect_contract import (
    EFFECT_CONTRACT_VERSION,
    EffectExecutionContract,
    EffectIntentDisposition,
)
from bowhead_core.terminal_finalizer import FinalizationBuildSnapshot
from bowhead_core.terminal_generation_bus import (
    GenerationEventBusRegistry,
    ProducerAdmissionClosedError,
)
from bowhead_core.terminal_llm_invocation import TerminalLLMInvocationHook
from bowhead_core.terminal_protocol import (
    AcceptedEventProposal,
    ActorScope,
    AdmissionContext,
    CandidateOutcome,
    CommittedReceipt,
    EffectClass,
    EventPublishMode,
    ExecutionGenerationContext,
    GenerationClaimContext,
    PhaseScope,
    RootDurability,
    SessionVariant,
    TerminalAdmissionRequest,
    TerminalDeleteContext,
    TerminalFollowupCapability,
    TerminalProtocolMalformedError,
    TerminalScope,
    TerminalStatus,
    TerminalWriterLease,
    canonical_digest_v1,
)
from bowhead_core.terminal_protocol_store import (
    AdmissionCode,
    AdmissionDisposition,
    GenerationClaimDisposition,
    TerminalAdmissionResult,
    TerminalProtocolEventRow,
    TerminalProtocolFaultPoint,
    TerminalProtocolStore,
    TerminalReadContext,
)
from bowhead_core.terminal_reconciler import TerminalRecoveryProcessor
from bowhead_core.terminal_runtime import (
    _CHAT_FAILURE_REASON_COMPAT,
    TerminalDurabilityAbort,
    TerminalRuntimeIdentity,
    TerminalTurnRuntime,
    _stable_id,
    _terminal_row_ticket,
)
from bowhead_core.terminal_runtime_factory import (
    TerminalRuntimeAdmissionError,
    TerminalRuntimeFactory,
    TerminalRuntimePreAdmissionCancelled,
    TerminalRuntimeRegistry,
    TerminalWriterSnapshot,
    terminal_auth_resolve_context,
)
from bowhead_core.turn_log_store import TurnLogStore
from bowhead_core.turn_runner import _estimate_cost_usd
from bowhead_core.types import TurnRequest
from bowhead_core.user_store import UserStore
from bowhead_router.invocation import PhysicalInvocationSpec
from bowhead_router.types import TokenUsage

from bowhead_core import session_lifecycle as session_lifecycle_module

_NOW = datetime(2026, 8, 3, 4, 5, 6, tzinfo=UTC)


def test_terminal_batch_live_ticket_retains_receipt_identity() -> None:
    row = TerminalProtocolEventRow(
        event_id="evt_answer",
        turn_id="t_1",
        generation=2,
        event_seq=4,
        event_type="final_answer",
        actor_scope=ActorScope.ROOT.value,
        phase_scope=PhaseScope.IN_GENERATION.value,
        terminal_receipt_id="tr_1",
        payload={"text": "391"},
        payload_digest=canonical_digest_v1({"text": "391"}),
        occurred_at=_NOW.isoformat(),
    )
    receipt = CommittedReceipt(
        receipt_id="tr_1",
        candidate_id="tc_1",
        candidate_projection_digest="digest_candidate",
        selected_projection_digest="digest_selected",
        selected_variant_key="primary",
        selected_outcome_variant=CandidateOutcome.PRIMARY,
        selected_session_variant=SessionVariant.NONE,
        terminal_event_id="evt_done",
        first_event_seq=4,
        last_event_seq=5,
        status=TerminalStatus.COMPLETED,
    )

    ticket = _terminal_row_ticket(
        TerminalScope("team_1", "s_1", "t_1", 2),
        receipt,
        row,
    )

    assert ticket.terminal_receipt_id == "tr_1"


def _create_db(tmp_path: Path) -> str:
    path = tmp_path / "bowhead.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()
    upgrade(f"sqlite+aiosqlite:///{path}", target="core")
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO teams(id,name,status,created_at) VALUES ('team_1','Team','active','t')")
        connection.execute(
            """
            INSERT INTO workspaces(id,team_id,name,status,created_at,updated_at,revision)
            VALUES ('ws_1','team_1','Space','active','t','t',1)
            """
        )
        connection.execute(
            """
            INSERT INTO sessions(
              id,workspace_id,user_id,title,started_at,last_active_at,status,
              cost_usd,tokens_total,intents
            ) VALUES (
              's_1','ws_1','u_1','Session','2026-08-01T00:00:00Z',
              '2026-08-01T00:00:00Z','active',0,0,'[]'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO terminal_protocol_writer_lease(
              lease_name,owner_instance_id,epoch,admission_state,admission_epoch,
              rollout_config_generation,lease_expires_at,heartbeat_at
            ) VALUES ('v1_writer','gateway_1',7,'open',3,'rollout_1',?,?)
            """,
            (
                (_NOW + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                _NOW.isoformat(),
            ),
        )
        connection.commit()
    return str(path)


def _seed_billing_user(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO users(id,team_id,email,credit_balance,created_at)
            VALUES ('u_1','team_1','u1@example.test',100,'t')
            """,
        )
        connection.execute(
            "UPDATE teams SET team_credit_balance=100 WHERE id='team_1'",
        )
        connection.commit()


async def _runtime(
    db_path: str,
    *,
    execution_intent: dict[str, object] | None = None,
) -> TerminalTurnRuntime:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    request = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="hello",
        turn_id="t_1",
        team_id="team_1",
    )
    vector = await store.ensure_auth_scope_epochs(terminal_auth_resolve_context(request))
    admitted = await store.admit_or_steer_v1(
        TerminalAdmissionRequest(
            context=AdmissionContext(
                team_id="team_1",
                session_id="s_1",
                request_id="req_1",
                surface="web",
                cohort="all;config=rollout_1",
                actor="u_1",
                writer_instance_id="gateway_1",
                writer_lease_epoch=7,
                writer_admission_epoch=3,
                rollout_config_generation="rollout_1",
                auth_fence_vector_digest=vector.auth_fence_vector_digest,
            ),
            turn_id="t_1",
            user_text="hello",
            execution_intent=execution_intent or {"text": "hello"},
            presentation_locale="zh-CN",
            message_catalog_version=1,
            allow_steer=False,
        )
    )
    assert admitted.disposition is AdmissionDisposition.ADMITTED
    assert admitted.scope is not None
    claimed = await store.claim_generation(
        GenerationClaimContext(
            scope=admitted.scope,
            expected_generation_row_version=1,
            expected_session_claim_epoch=0,
            writer_lease_epoch=7,
            owner_instance_id="gateway_1",
        )
    )
    assert claimed.disposition is GenerationClaimDisposition.CLAIMED
    assert claimed.context is not None
    execution = await store.mark_execution_started(claimed.context)
    bus = await GenerationEventBusRegistry().get_or_create(
        execution.scope,
        claim_epoch=execution.claim_epoch,
    )
    return TerminalTurnRuntime(
        store=store,
        execution=execution,
        generation_bus=bus,
        identity=TerminalRuntimeIdentity(
            authz_snapshot_digest=vector.auth_fence_vector_digest,
            writer_admission_epoch=3,
            owner_instance_id="gateway_1",
        ),
        effect_contract=await store.read_effect_execution_contract(
            execution.scope,
        ),
    )


async def _claimed_runtime_plan(db_path: str) -> tuple[PlanStore, str]:
    plans = PlanStore(db_path)
    await plans.initialize()
    plan = await plans.create_plan(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        title="Terminal owner proof",
    )
    assert await plans.try_claim_running(plan.id, "t_1")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE plans SET updated_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (plan.id,),
        )
        connection.commit()
    return plans, plan.id


async def _runtime_plan_status(
    plans: PlanStore,
    plan_id: str,
) -> tuple[str, str | None]:
    plan = await plans.get_plan(
        plan_id,
        user_id="u_1",
        workspace_id="ws_1",
    )
    assert plan is not None
    return plan.status, plan.execution_turn_id


@pytest.mark.anyio
async def test_plan_orphan_recovery_preserves_open_terminal_generation(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    await _runtime(db_path)
    plans, plan_id = await _claimed_runtime_plan(db_path)

    assert await plans.recover_orphans() == (0, 0)
    assert await _runtime_plan_status(plans, plan_id) == (
        PlanStatus.RUNNING,
        "t_1",
    )


@pytest.mark.anyio
async def test_plan_orphan_recovery_accepts_canonical_terminal_receipt(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path)
    plans, plan_id = await _claimed_runtime_plan(db_path)
    await runtime.bus.emit(
        OrchestratorEvent("final_answer", "t_1", {"text": "done"}),
    )
    await runtime.bus.emit(
        OrchestratorEvent(
            "done",
            "t_1",
            {"status": "completed", "intent": "lookup"},
        ),
    )

    assert await plans.recover_orphans() == (1, 0)
    assert await _runtime_plan_status(plans, plan_id) == (
        PlanStatus.BLOCKED,
        None,
    )


@pytest.mark.anyio
async def test_plan_orphan_recovery_rolls_back_receipt_with_stray_delete(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path)
    plans, plan_id = await _claimed_runtime_plan(db_path)
    await runtime.bus.emit(
        OrchestratorEvent("final_answer", "t_1", {"text": "done"}),
    )
    await runtime.bus.emit(
        OrchestratorEvent(
            "done",
            "t_1",
            {"status": "completed", "intent": "lookup"},
        ),
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO turn_terminal_deletion_ledgers(
              deletion_id,turn_id,generation,prior_generation_state,
              prior_row_version,prior_execution_claim_epoch,
              prior_finalization_epoch,candidate_id,receipt_id,
              old_session_active_tuple,old_session_recovery_tuple,
              new_delivery_fence_epoch,unsettled_effect_count,
              effect_identity_digest,effect_bundle_digest,
              effect_audit_key_version,effect_key_required_until,
              resolution,actor_audit_ref,authz_snapshot_digest,
              policy_code,created_at,deletion_digest
            ) VALUES (
              'td_stray','t_1',1,'closed',1,1,1,NULL,NULL,
              '{}','{}',1,0,'effects','bundle','v1','2099-01-01T00:00:00Z',
              'fenced_deleted','u_1','auth_1','test','2026-08-03T04:05:06Z','digest'
            )
            """,
        )
        connection.commit()

    with pytest.raises(TerminalProtocolMalformedError):
        await plans.recover_orphans()
    assert await _runtime_plan_status(plans, plan_id) == (
        PlanStatus.RUNNING,
        "t_1",
    )


@pytest.mark.anyio
async def test_terminal_stop_cancels_bound_plan_before_commit_ack(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path)
    plans, plan_id = await _claimed_runtime_plan(db_path)
    injected = False

    def lose_commit_ack(point: TerminalProtocolFaultPoint) -> None:
        nonlocal injected
        if point is TerminalProtocolFaultPoint.TERMINAL_AFTER_COMMIT_BEFORE_ACK and not injected:
            injected = True
            raise asyncio.CancelledError("terminal commit acknowledgement lost")

    runtime.store._fault_injector = lose_commit_ack
    receipt = await runtime.bus.commit_cancelled()

    assert injected is True
    assert receipt.status is TerminalStatus.CANCELLED
    assert await _runtime_plan_status(plans, plan_id) == (
        PlanStatus.CANCELLED,
        "t_1",
    )


@pytest.mark.anyio
async def test_terminal_stop_rolls_back_plan_before_receipt_commit(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path)
    plans, plan_id = await _claimed_runtime_plan(db_path)

    def fail_before_commit(point: TerminalProtocolFaultPoint) -> None:
        if point is TerminalProtocolFaultPoint.TERMINAL_BEFORE_COMMIT:
            raise RuntimeError("terminal commit rolled back")

    runtime.store._fault_injector = fail_before_commit
    with pytest.raises(TerminalDurabilityAbort):
        await runtime.bus.commit_cancelled()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM turn_terminal_receipts WHERE turn_id='t_1'",
        ).fetchone() == (0,)
    assert await _runtime_plan_status(plans, plan_id) == (
        PlanStatus.RUNNING,
        "t_1",
    )

    recovery = await TerminalRecoveryProcessor(
        TerminalProtocolStore(
            db_path,
            _clock=lambda: _NOW + timedelta(minutes=6),
        ),
    ).run_bounded(
        owner_instance_id="gateway_stop_recovery",
        rollout_config_generation="recovery_stop",
    )
    assert recovery.errors == ()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT status FROM turn_terminal_receipts WHERE turn_id='t_1'",
        ).fetchone() == ("aborted",)
    assert await plans.recover_orphans() == (1, 0)
    assert await _runtime_plan_status(plans, plan_id) == (
        PlanStatus.BLOCKED,
        None,
    )


@pytest.mark.anyio
async def test_plan_orphan_recovery_accepts_exact_fenced_delete(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path)
    plans, plan_id = await _claimed_runtime_plan(db_path)
    lifecycle = SessionLifecycleCoordinator(SessionStore(db_path), runtime.store)
    await lifecycle.soft_delete(
        session_id="s_1",
        actor_audit_ref="u_1",
        request_id="delete-plan-owner",
        expected_user_id="u_1",
        authz_snapshot_digest="auth_1",
    )

    assert await plans.recover_orphans() == (1, 0)
    assert await _runtime_plan_status(plans, plan_id) == (
        PlanStatus.BLOCKED,
        None,
    )


@pytest.mark.anyio
async def test_fenced_delete_reader_and_plan_recovery_reject_stray_receipt(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path)
    plans, plan_id = await _claimed_runtime_plan(db_path)
    lifecycle = SessionLifecycleCoordinator(SessionStore(db_path), runtime.store)
    await lifecycle.soft_delete(
        session_id="s_1",
        actor_audit_ref="u_1",
        request_id="delete-plan-owner-stray-receipt",
        expected_user_id="u_1",
        authz_snapshot_digest="auth_1",
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        deletion = connection.execute(
            "SELECT * FROM turn_terminal_deletion_ledgers WHERE turn_id='t_1' AND generation=1",
        ).fetchone()
        assert deletion is not None
        connection.execute(
            """
            INSERT INTO turn_terminal_receipts(
              receipt_id,turn_id,generation,candidate_id,protocol_version,
              status,reason_code,message_key,final_answer_mode,
              final_answer_event_id,selected_variant_key,
              selected_outcome_variant,selected_session_variant,
              terminal_event_id,first_event_seq,last_event_seq,
              persisted_event_highwater,required_event_manifest_json,
              candidate_projection_digest,selected_projection_digest,
              selected_manifest_digest,selected_session_mutation_digest,
              projection_extensions_json,committed_at
            ) VALUES (
              'tr_stray','t_1',1,'tc_stray',1,
              'cancelled','user_cancelled','terminal.cancelled','none',
              NULL,'primary_none','primary','none',
              'evt_stray',1,1,0,'[]',
              'candidate','selected','manifest','mutation','{}','2026-08-03T04:05:06Z'
            )
            """,
        )
        connection.commit()

    replay_context = TerminalDeleteContext(
        scope=runtime.scope,
        deletion_id=deletion["deletion_id"],
        expected_generation_row_version=deletion["prior_row_version"],
        expected_candidate_id=deletion["candidate_id"],
        expected_delivery_fence_epoch=(deletion["new_delivery_fence_epoch"] - 1),
        actor_audit_ref=deletion["actor_audit_ref"],
        authz_snapshot_digest=deletion["authz_snapshot_digest"],
        policy_code=deletion["policy_code"],
        effect_identity_digest=deletion["effect_identity_digest"],
        effect_bundle_digest=deletion["effect_bundle_digest"],
        effect_audit_key_version=deletion["effect_audit_key_version"],
        effect_key_required_until=deletion["effect_key_required_until"],
    )
    with pytest.raises(TerminalProtocolMalformedError):
        await runtime.store.commit_protocol_delete(
            replay_context,
        )
    with pytest.raises(TerminalProtocolMalformedError):
        await plans.recover_orphans()
    assert await _runtime_plan_status(plans, plan_id) == (
        PlanStatus.RUNNING,
        "t_1",
    )


@pytest.mark.anyio
async def test_runtime_persists_before_live_and_commits_last_root_answer(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(_create_db(tmp_path))
    await runtime.bus.emit(OrchestratorEvent("turn_started", "t_1", {"session_id": "s_1"}))
    await runtime.bus.emit(OrchestratorEvent("final_answer", "t_1", {"text": "old"}))
    await runtime.bus.emit(OrchestratorEvent("final_answer", "t_1", {"text": "new"}))
    await runtime.bus.emit(
        OrchestratorEvent(
            "done",
            "t_1",
            {
                "status": "completed",
                "intent": "lookup",
                "tokens_input": 11,
                "tokens_output": 7,
                "cost_usd": 0.25,
            },
        )
    )

    assert runtime.bus.receipt is not None
    assert runtime.bus.receipt.status is TerminalStatus.COMPLETED
    read_context = TerminalReadContext(
        "team_1",
        "s_1",
        "t_1",
        "u_1",
        runtime.identity.authz_snapshot_digest,
    )
    summary = await runtime.store.read_protocol_summary(read_context)
    events = await runtime.store.list_protocol_events(read_context)
    answers = [event for event in events if event.event_type == "final_answer"]
    assert [event.payload["text"] for event in answers] == ["old", "new"]
    assert summary.current_receipt is not None
    assert summary.current_receipt.final_answer_event_id == answers[-1].event_id
    with sqlite3.connect(runtime.store.db_path) as connection:
        row = connection.execute("SELECT status,final_answer,tokens_input,tokens_output,cost_usd FROM turns WHERE id='t_1'").fetchone()
    assert row == ("completed", "new", 11, 7, 0.25)
    live = runtime.bus.snapshot()
    assert [event.event_type for event in live][-1] == "done"
    assert [event.seq for event in live] == list(range(1, len(live) + 1))


@pytest.mark.anyio
async def test_dormant_v2_runtime_binds_billing_to_primary_receipt(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    _seed_billing_user(db_path)
    runtime = await _runtime(
        db_path,
        execution_intent={
            "billing_rate_card_version": "v1",
            "effect_contract_version": EFFECT_CONTRACT_VERSION,
            "text": "hello",
        },
    )
    invocation = await TerminalLLMInvocationHook(
        runtime,
        cost_estimator=_estimate_cost_usd,
    ).begin(
        PhysicalInvocationSpec(
            provider="provider",
            model="model",
            adapter_contract_key="openai_compatible/openai_chat_completions",
            mode="invoke",
            request_digest="a" * 64,
        )
    )
    assert await invocation.complete(
        usage=TokenUsage(input_tokens=700, output_tokens=0),
        response_digest="b" * 64,
    )

    await runtime.bus.emit(
        OrchestratorEvent(
            "done",
            "t_1",
            {
                "status": "completed",
                "tokens_input": 700,
                "tokens_output": 0,
            },
        )
    )

    assert runtime.bus.receipt is not None
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM credit_ledger WHERE turn_id='t_1'",
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT event_type FROM turn_events WHERE event_type='effect_settlement'",
        ).fetchall() == [("effect_settlement",)]
        extensions = connection.execute(
            "SELECT projection_extensions_json FROM turn_terminal_receipts",
        ).fetchone()
        turn_cost = connection.execute(
            "SELECT cost_usd FROM turns WHERE id='t_1'",
        ).fetchone()[0]
    assert extensions is not None
    assert "effect_disclosure_v2" in extensions[0]
    assert turn_cost == _estimate_cost_usd(700, 0)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tokens_input", "recorded_input"),
    [
        (True, 1),
        (1.9, 1),
        ("1", 1),
        (None, 0),
    ],
    ids=["bool", "float", "string", "missing"],
)
async def test_dormant_v2_runtime_rejects_non_exact_done_usage(
    tmp_path: Path,
    tokens_input: object,
    recorded_input: int,
) -> None:
    db_path = _create_db(tmp_path)
    _seed_billing_user(db_path)
    runtime = await _runtime(
        db_path,
        execution_intent={
            "billing_rate_card_version": "v1",
            "effect_contract_version": EFFECT_CONTRACT_VERSION,
            "text": "hello",
        },
    )
    if recorded_input:
        invocation = await TerminalLLMInvocationHook(
            runtime,
            cost_estimator=_estimate_cost_usd,
        ).begin(
            PhysicalInvocationSpec(
                provider="provider",
                model="model",
                adapter_contract_key="openai_compatible/openai_chat_completions",
                mode="invoke",
                request_digest="c" * 64,
            )
        )
        assert await invocation.complete(
            usage=TokenUsage(
                input_tokens=recorded_input,
                output_tokens=0,
            ),
            response_digest="d" * 64,
        )

    payload: dict[str, object] = {
        "status": "completed",
        "tokens_output": 0,
    }
    if tokens_input is not None:
        payload["tokens_input"] = tokens_input
    with pytest.raises(TerminalDurabilityAbort) as failed:
        await runtime.bus.emit(OrchestratorEvent("done", "t_1", payload))
    assert isinstance(failed.value.__cause__, TerminalProtocolMalformedError)
    assert runtime.bus.receipt is None


@pytest.mark.anyio
async def test_dormant_v2_runtime_accepts_explicit_zero_done_usage(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    _seed_billing_user(db_path)
    runtime = await _runtime(
        db_path,
        execution_intent={
            "billing_rate_card_version": "v1",
            "effect_contract_version": EFFECT_CONTRACT_VERSION,
            "text": "hello",
        },
    )

    await runtime.bus.emit(
        OrchestratorEvent(
            "done",
            "t_1",
            {
                "status": "completed",
                "tokens_input": 0,
                "tokens_output": 0,
            },
        )
    )

    assert runtime.bus.receipt is not None
    assert runtime.bus.receipt.status is TerminalStatus.COMPLETED


@pytest.mark.anyio
async def test_dormant_v2_runtime_stop_freezes_unknown_disclosure(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    _seed_billing_user(db_path)
    runtime = await _runtime(
        db_path,
        execution_intent={
            "billing_rate_card_version": "v1",
            "effect_contract_version": EFFECT_CONTRACT_VERSION,
            "text": "hello",
        },
    )
    attempt = await runtime.store.begin_effect_attempt(
        runtime.execution,
        attempt_id="eff_runtime_stop",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="tool_external/v1",
        action_ref="tool:runtime-stop",
        idempotency_or_fence_token="runtime-stop-key",
        non_retriable=False,
    )
    await runtime.store.mark_effect_dispatched(runtime.execution, attempt)
    await runtime.store.record_execution_usage(
        runtime.execution,
        input_tokens=700,
        output_tokens=0,
        cost_amount=0.25,
    )

    receipt = await runtime.bus.commit_cancelled()

    assert receipt.status is TerminalStatus.CANCELLED
    snapshot = await runtime.store.read_effect_disclosure_snapshot(
        runtime.scope,
    )
    assert snapshot is not None
    assert snapshot.kind_outcome_counts["accounting"]["applied"] == 1
    assert snapshot.kind_outcome_counts["user_action"]["unsettled"] == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT event_type FROM turn_events WHERE event_type='effect_disclosure'",
        ).fetchall() == [("effect_disclosure",)]
        assert connection.execute(
            "SELECT input_tokens,output_tokens FROM turn_generations WHERE turn_id='t_1'",
        ).fetchone() == (700, 0)
        assert connection.execute(
            "SELECT COUNT(*) FROM credit_ledger WHERE turn_id='t_1'",
        ).fetchone() == (1,)


@pytest.mark.anyio
async def test_dormant_v2_worker_start_failure_freezes_zero_usage(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    _seed_billing_user(db_path)
    runtime = await _runtime(
        db_path,
        execution_intent={
            "billing_rate_card_version": "v1",
            "effect_contract_version": EFFECT_CONTRACT_VERSION,
            "text": "hello",
        },
    )

    await runtime.abort_worker_start_failed()

    assert runtime.bus.receipt is not None
    assert runtime.bus.receipt.status is TerminalStatus.ABORTED
    with sqlite3.connect(db_path) as connection:
        usage_digest, status = connection.execute(
            "SELECT usage_digest,state FROM turn_generations WHERE turn_id='t_1'",
        ).fetchone()
        assert isinstance(usage_digest, str) and usage_digest
        assert status == "closed"
        assert connection.execute(
            "SELECT COUNT(*) FROM credit_ledger WHERE turn_id='t_1'",
        ).fetchone() == (0,)


@pytest.mark.anyio
async def test_legacy_recovery_effect_event_keeps_stable_identity(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(_create_db(tmp_path))
    attempt = await runtime.store.begin_effect_attempt(
        runtime.execution,
        attempt_id="eff_legacy_identity",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="tool_external/v1",
        action_ref="tool:legacy-identity",
        idempotency_or_fence_token="legacy-identity-key",
        non_retriable=False,
    )
    dispatched = await runtime.store.mark_effect_dispatched(
        runtime.execution,
        attempt,
    )
    await runtime.store.mark_effect_unknown(dispatched)
    finalization = await runtime.store.begin_finalization(
        runtime.execution,
        finalization_owner_instance_id="gateway_1:finalizer",
        closing_reason="root_done:completed",
    )
    flush = await runtime.store.flush_finalization_through(finalization)
    disclosure = await runtime.store.read_effect_disclosure_snapshot(
        runtime.scope,
    )
    assert disclosure is not None and disclosure.protocol == 1
    runtime.bus._terminal_snapshot = {"status": "completed"}
    plan = await runtime.bus._build_candidate(
        FinalizationBuildSnapshot(
            finalization=finalization,
            flush=flush,
            effect_disclosure=disclosure,
        )
    )
    recovery = next(variant for variant in plan.variants if variant.outcome_variant is CandidateOutcome.RECOVERY_INTERRUPT)
    assert recovery.required_events[0].event_id == _stable_id(
        "evt",
        runtime.scope,
        finalization.finalization_epoch,
        "effect_disclosure",
    )


@pytest.mark.anyio
async def test_runtime_materialize_wake_preserves_durable_prefix_live_order(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(_create_db(tmp_path))
    ack = await runtime.store.accept_event(
        runtime.execution,
        event_type="emoji_ack",
        actor_scope=ActorScope.ROOT,
        phase_scope=PhaseScope.IN_GENERATION,
        payload={
            "emoji": "🤝",
            "parent_agent_id": "not-a-root-parent",
            "steer_id": "steer_1",
            "user_msg_id": "msg_1",
        },
        idempotency_key="steer_ack:steer_1",
    )

    await runtime.bus.emit(OrchestratorEvent("step_started", "t_1", {"step_id": "step_1"}))
    await runtime.bus.materialize_accepted_prefix(ack.accept_ordinal)

    live = runtime.bus.snapshot()
    assert [event.event_type for event in live] == [
        "emoji_ack",
        "step_started",
    ]
    assert [event.seq for event in live] == [1, 2]
    assert live[0].parent_agent_id is None


@pytest.mark.anyio
async def test_runtime_materialize_cancel_after_live_append_remains_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_SSE_FANOUT", "1")
    runtime = await _runtime(_create_db(tmp_path))
    ack = await runtime.store.accept_event(
        runtime.execution,
        event_type="emoji_ack",
        actor_scope=ActorScope.ROOT,
        phase_scope=PhaseScope.IN_GENERATION,
        payload={
            "emoji": "🤝",
            "steer_id": "steer_cancelled",
            "user_msg_id": "msg_cancelled",
        },
        idempotency_key="steer_ack:steer_cancelled",
    )
    condition = runtime.bus._live._cond
    await condition.acquire()
    materialize = asyncio.create_task(runtime.bus.materialize_accepted_prefix(ack.accept_ordinal))
    try:
        for _ in range(1_000):
            if runtime.bus._live.snapshot():
                break
            await asyncio.sleep(0.001)
        assert len(runtime.bus._live.snapshot()) == 1
        materialize.cancel("request disconnected")
    finally:
        condition.release()

    with pytest.raises(asyncio.CancelledError, match="request disconnected"):
        await materialize
    await runtime.bus.materialize_accepted_prefix(ack.accept_ordinal)

    live = runtime.bus._live.snapshot()
    assert len(live) == 1
    assert live[0].payload["event_id"] == ack.event_id


@pytest.mark.anyio
async def test_factory_web_cancel_commits_exact_active_generation_before_signal(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(_create_db(tmp_path))
    runtimes = TerminalRuntimeRegistry()
    await runtimes.register(runtime)
    factory = TerminalRuntimeFactory(
        runtime.store,
        GenerationEventBusRegistry(),
        runtimes,
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
    )
    await runtime.bus.emit(OrchestratorEvent("turn_started", "t_1", {"session_id": "s_1"}))
    runtime.bus._step_count = 2
    runtime.bus._pi_turn_state = {
        "intent": "lookup",
        "tokens_input": 7,
        "tokens_output": 3,
        "cost_usd": 0.25,
    }

    receipt = await factory.commit_active_web_cancel(runtime.scope)

    assert receipt.status is TerminalStatus.CANCELLED
    assert runtime.bus.receipt == receipt
    read_context = TerminalReadContext(
        "team_1",
        "s_1",
        "t_1",
        "u_1",
        runtime.identity.authz_snapshot_digest,
    )
    summary = await runtime.store.read_protocol_summary(read_context)
    events = await runtime.store.list_protocol_events(read_context)
    assert summary.current_receipt is not None
    assert summary.current_receipt.status is TerminalStatus.CANCELLED
    assert [event.event_type for event in events[-3:]] == [
        "turn_cancelled",
        "step_list_card_finalized",
        "done",
    ]
    assert events[-2].payload == {
        "card_id": "t_1",
        "state": "cancelled",
        "step_count": 2,
    }
    assert events[-1].payload["status"] == TerminalStatus.CANCELLED.value
    with sqlite3.connect(runtime.store.db_path) as connection:
        row = connection.execute("SELECT status,tokens_input,tokens_output FROM turns WHERE id='t_1'").fetchone()
    assert row == ("cancelled", 7, 3)
    session_store = SessionStore(runtime.store.db_path)
    cancel_request_id = "7e597ffc-b192-5a6f-801a-1fb3cc7d7053"
    receipt_outcome = await session_store.record_terminal_cancel_receipt(
        team_id="team_1",
        user_id="u_1",
        cancel_request_id=cancel_request_id,
        workspace_id="ws_1",
        turn_id="t_1",
        expected_execution_generation=1,
        outcome="cancelled",
    )
    replay_outcome = await session_store.record_terminal_cancel_receipt(
        team_id="team_1",
        user_id="u_1",
        cancel_request_id=cancel_request_id,
        workspace_id="ws_1",
        turn_id="t_1",
        expected_execution_generation=1,
        outcome="cancelled",
    )
    cancel_receipt = await session_store.get_cancel_turn_receipt(
        team_id="team_1",
        user_id="u_1",
        cancel_request_id=cancel_request_id,
        turn_id="t_1",
        expected_execution_generation=1,
    )
    assert receipt_outcome == replay_outcome == "cancelled"
    assert cancel_receipt is not None
    assert cancel_receipt.outcome == "cancelled"


@pytest.mark.anyio
async def test_factory_never_attaches_recovery_only_effect_contract() -> None:
    scope = TerminalScope("team_1", "s_1", "t_v2_seed", 1)

    class Store:
        claim_calls = 0

        async def read_effect_execution_contract(self, actual_scope):
            assert actual_scope == scope
            return EffectExecutionContract(
                disposition=EffectIntentDisposition.RECOVERY_ONLY,
                contract_version=EFFECT_CONTRACT_VERSION,
                billing_rate_card_version="v1",
            )

        async def claim_generation(self, _context):
            self.claim_calls += 1
            raise AssertionError("recovery-only contract must not claim a writer")

    store = Store()
    factory = TerminalRuntimeFactory(
        store,  # type: ignore[arg-type]
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
    )

    with pytest.raises(
        TerminalRuntimeAdmissionError,
        match="terminal_effect_contract_writer_not_ready",
    ):
        await factory._attach_opened_scope_owned(
            scope,
            authz_snapshot_digest="auth_1",
        )

    assert store.claim_calls == 0


@pytest.mark.anyio
async def test_factory_web_cancel_rejects_different_generation_identity(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(_create_db(tmp_path))
    runtimes = TerminalRuntimeRegistry()
    await runtimes.register(runtime)
    factory = TerminalRuntimeFactory(
        runtime.store,
        GenerationEventBusRegistry(),
        runtimes,
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
    )

    with pytest.raises(
        TerminalRuntimeAdmissionError,
        match="terminal_dependency_recovering",
    ):
        await factory.commit_active_web_cancel(TerminalScope("team_1", "s_1", "t_1", 2))

    assert runtime.bus.receipt is None


@pytest.mark.anyio
async def test_factory_web_cancel_normalizes_durability_abort_for_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await _runtime(_create_db(tmp_path))
    runtimes = TerminalRuntimeRegistry()
    await runtimes.register(runtime)
    recovery: list[tuple[TerminalScope, str]] = []

    async def trigger(scope: TerminalScope, reason: str) -> None:
        recovery.append((scope, reason))

    async def fail_finalize(_request):
        raise RuntimeError("sqlite unavailable")

    monkeypatch.setattr(
        runtime.bus._finalizer,
        "finalize_cancelled",
        fail_finalize,
    )
    factory = TerminalRuntimeFactory(
        runtime.store,
        GenerationEventBusRegistry(),
        runtimes,
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
        recovery_trigger=trigger,
    )

    with pytest.raises(
        TerminalRuntimeAdmissionError,
        match="terminal_dependency_recovering",
    ):
        await factory.commit_active_web_cancel(runtime.scope)

    assert recovery == [(runtime.scope, "web_cancel_commit_failed")]
    assert runtime.bus.receipt is None


@pytest.mark.anyio
async def test_factory_web_cancel_confirms_post_commit_cancellation(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(_create_db(tmp_path))
    runtimes = TerminalRuntimeRegistry()
    await runtimes.register(runtime)
    injected = False

    def cancel_after_commit(point: TerminalProtocolFaultPoint) -> None:
        nonlocal injected
        if point is TerminalProtocolFaultPoint.TERMINAL_AFTER_COMMIT_BEFORE_ACK and not injected:
            injected = True
            raise asyncio.CancelledError("terminal commit acknowledgement lost")

    runtime.store._fault_injector = cancel_after_commit
    recovery: list[tuple[TerminalScope, str]] = []

    async def trigger(scope: TerminalScope, reason: str) -> None:
        recovery.append((scope, reason))

    factory = TerminalRuntimeFactory(
        runtime.store,
        GenerationEventBusRegistry(),
        runtimes,
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
        recovery_trigger=trigger,
    )

    receipt = await factory.commit_active_web_cancel(runtime.scope)

    assert injected is True
    assert receipt.status is TerminalStatus.CANCELLED
    assert runtime.bus.receipt == receipt
    assert recovery == []


@pytest.mark.anyio
async def test_factory_web_cancel_recovers_precommit_task_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await _runtime(_create_db(tmp_path))
    runtimes = TerminalRuntimeRegistry()
    await runtimes.register(runtime)
    recovery: list[tuple[TerminalScope, str]] = []

    async def cancelled_commit():
        raise asyncio.CancelledError("request disconnected")

    async def trigger(scope: TerminalScope, reason: str) -> None:
        recovery.append((scope, reason))

    monkeypatch.setattr(runtime.bus, "commit_cancelled", cancelled_commit)
    factory = TerminalRuntimeFactory(
        runtime.store,
        GenerationEventBusRegistry(),
        runtimes,
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
        recovery_trigger=trigger,
    )

    with pytest.raises(asyncio.CancelledError, match="request disconnected"):
        await factory.commit_active_web_cancel(runtime.scope)

    assert recovery == [(runtime.scope, "web_cancel_commit_cancelled")]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "fanout_error",
    [
        asyncio.CancelledError("fanout task cancelled"),
        RuntimeError("fanout failed"),
    ],
)
async def test_factory_web_cancel_closes_live_after_committed_fanout_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fanout_error: BaseException,
) -> None:
    runtime = await _runtime(_create_db(tmp_path))
    runtimes = TerminalRuntimeRegistry()
    await runtimes.register(runtime)
    subscription = runtime.bus.subscribe().__aiter__()
    waiting = asyncio.create_task(anext(subscription))
    await asyncio.sleep(0)

    async def cancelled_fanout(_receipt):
        raise fanout_error

    monkeypatch.setattr(runtime.bus, "_publish_terminal_rows", cancelled_fanout)
    factory = TerminalRuntimeFactory(
        runtime.store,
        GenerationEventBusRegistry(),
        runtimes,
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
    )

    receipt = await factory.commit_active_web_cancel(runtime.scope)

    assert receipt.status is TerminalStatus.CANCELLED
    assert runtime.bus.receipt == receipt
    assert runtime.bus.closed is True
    assert runtime.bus._generation_bus.fanout_failed is True
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(waiting, timeout=0.5)


@pytest.mark.anyio
async def test_runtime_live_and_durable_replay_share_stable_canvas_cursor(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path)
    await runtime.bus.emit(
        OrchestratorEvent(
            "canvas.section_chunk",
            "t_1",
            {
                "delta": "first chunk",
                "section_id": "sec_1",
                # Canvas section order is not the transport event cursor.
                "seq": 7,
            },
        )
    )

    # report-streaming P1: before first this-turn canvas content event, bus injects
    # canvas.turn_streaming (sole frontend currentTurnId writer). Select **by type**,
    # not [0]: case asserts section-order seq differs from transport cursor,
    # independent of which is first.
    live_all = runtime.bus.snapshot()
    replay_all = await TurnLogStore(db_path).list_events("t_1")
    assert [e.event_type for e in live_all][:2] == ["canvas.turn_streaming", "canvas.section_chunk"]
    # Injected frame must be live and persisted; only one violates live == persist (chat invariant).
    assert [e.event_type for e in replay_all][:2] == ["canvas.turn_streaming", "canvas.section_chunk"]

    live = next(e for e in live_all if e.event_type == "canvas.section_chunk")
    replay = next(e for e in replay_all if e.event_type == "canvas.section_chunk")

    assert replay.payload == live.payload
    assert replay.payload["seq"] == 7
    assert replay.payload["evt_seq"] == 2
    assert replay.payload["event_seq"] == 2
    assert replay.payload["event_id"] == replay.id
    assert replay.payload["generation"] == 1


@pytest.mark.anyio
async def test_runtime_renews_execution_lease_before_thirty_second_expiry(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(_create_db(tmp_path))
    clock = [_NOW + timedelta(seconds=20)]
    runtime.store._clock = lambda: clock[0]

    await runtime.heartbeat_once()
    clock[0] += timedelta(seconds=20)
    await runtime.bus.emit(OrchestratorEvent("turn_started", "t_1", {}))

    with sqlite3.connect(runtime.store.db_path) as connection:
        lease, heartbeat = connection.execute("SELECT lease_expires_at,last_heartbeat_at FROM turn_generations WHERE turn_id='t_1'").fetchone()
    assert datetime.fromisoformat(lease.replace("Z", "+00:00")) == _NOW + timedelta(seconds=50)
    assert datetime.fromisoformat(heartbeat.replace("Z", "+00:00")) == _NOW + timedelta(seconds=20)


@pytest.mark.anyio
async def test_done_usage_uses_merged_runtime_snapshot(tmp_path: Path) -> None:
    runtime = await _runtime(_create_db(tmp_path))
    runtime.bus._pi_turn_state = {
        "tokens_input": 13,
        "tokens_output": 17,
        "cost_usd": 0.75,
    }

    await runtime.bus.emit(
        OrchestratorEvent(
            "done",
            "t_1",
            {"status": "completed", "final_answer": "done"},
        )
    )

    with sqlite3.connect(runtime.store.db_path) as connection:
        usage = connection.execute("SELECT tokens_input,tokens_output,cost_usd FROM turns WHERE id='t_1'").fetchone()
    assert usage == (13, 17, 0.75)


@pytest.mark.anyio
async def test_done_drains_admitted_business_event_producer_before_freeze(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(_create_db(tmp_path))
    registered = asyncio.Event()
    release = asyncio.Event()

    async def producer() -> None:
        lease = await runtime.bus.register_producer()
        registered.set()
        try:
            await release.wait()
            assert await runtime.bus.emit(
                OrchestratorEvent(
                    "lead.final.payload",
                    "t_1",
                    {"title": "persist me"},
                )
            )
        finally:
            await lease.release()

    producer_task = asyncio.create_task(producer())
    await registered.wait()
    done_task = asyncio.create_task(
        runtime.bus.emit(
            OrchestratorEvent(
                "done",
                "t_1",
                {"status": "completed", "final_answer": "done"},
            )
        )
    )
    await asyncio.sleep(0)
    assert not done_task.done()
    release.set()
    await producer_task
    await done_task

    events = await runtime.store.list_protocol_events(
        TerminalReadContext(
            "team_1",
            "s_1",
            "t_1",
            "u_1",
            runtime.identity.authz_snapshot_digest,
        )
    )
    assert [event.event_type for event in events] == [
        "lead.final.payload",
        "final_answer",
        "done",
    ]
    with pytest.raises(ProducerAdmissionClosedError):
        await runtime.bus.register_producer()


@pytest.mark.anyio
async def test_root_error_is_safe_nonterminal_until_failed_done(tmp_path: Path) -> None:
    runtime = await _runtime(_create_db(tmp_path))
    await runtime.bus.emit(
        OrchestratorEvent(
            "error",
            "t_1",
            {
                "reason": "lead_internal_error",
                "message": "secret exception",
                "details": {"token": "secret"},
                "occurrence_id": "occ_1",
            },
        )
    )
    assert runtime.bus.receipt is None
    await runtime.bus.emit(
        OrchestratorEvent(
            "done",
            "t_1",
            {"status": "failed", "error_kind": "internal"},
        )
    )
    events = await runtime.store.list_protocol_events(
        TerminalReadContext(
            "team_1",
            "s_1",
            "t_1",
            "u_1",
            runtime.identity.authz_snapshot_digest,
        )
    )
    failure = next(event for event in events if event.event_type == "turn_failed")
    assert failure.payload == {
        "correlation_id": "occ_1",
        "message_key": "turn.failed",
        "reason_code": "worker_start_failed",
    }
    assert runtime.bus.receipt is not None
    assert runtime.bus.receipt.status is TerminalStatus.FAILED


@pytest.mark.anyio
async def test_failed_done_freezes_lead_reason_across_event_receipt_and_done(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(_create_db(tmp_path))
    await runtime.bus.emit(
        OrchestratorEvent(
            "turn_failed",
            "t_1",
            {"reason_code": "wallclock_exceeded", "message": "internal detail"},
        )
    )
    await runtime.bus.emit(
        OrchestratorEvent(
            "done",
            "t_1",
            {"status": "failed", "error": "wallclock_exceeded"},
        )
    )

    read_context = TerminalReadContext(
        "team_1",
        "s_1",
        "t_1",
        "u_1",
        runtime.identity.authz_snapshot_digest,
    )
    events = await runtime.store.list_protocol_events(read_context)
    failed = next(item for item in events if item.event_type == "turn_failed")
    done = next(item for item in events if item.event_type == "done")
    with sqlite3.connect(runtime.store.db_path) as connection:
        receipt_reason = connection.execute("SELECT reason_code FROM turn_terminal_receipts WHERE turn_id='t_1'").fetchone()[0]
    assert failed.payload["reason_code"] == "terminal_producer_timeout"
    assert done.payload["reason"] == "terminal_producer_timeout"
    assert receipt_reason == "terminal_producer_timeout"


def test_terminal_failure_compatibility_map_pins_every_public_lead_code() -> None:
    assert _CHAT_FAILURE_REASON_COMPAT.keys() >= _PUBLIC_TURN_FAILURE_CODES


@pytest.mark.anyio
async def test_duplicate_done_reuses_one_receipt(tmp_path: Path) -> None:
    runtime = await _runtime(_create_db(tmp_path))
    done = OrchestratorEvent(
        "done",
        "t_1",
        {"status": "completed", "final_answer": "fallback"},
    )
    assert await runtime.bus.emit(done)
    first = runtime.bus.receipt
    assert first is not None
    assert await runtime.bus.emit(done)
    assert runtime.bus.receipt == first
    with sqlite3.connect(runtime.store.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_receipts WHERE turn_id='t_1'").fetchone()[0] == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status",
    [
        "completed",
        "failed",
        "cancelled",
        "aborted",
        "clarification_pending",
        "approval_pending",
    ],
)
async def test_all_root_terminal_statuses_commit_exactly_one_receipt(
    tmp_path: Path,
    status: str,
) -> None:
    runtime = await _runtime(_create_db(tmp_path))
    await runtime.bus.emit(
        OrchestratorEvent(
            "done",
            "t_1",
            {"status": status, "final_answer": "answer"},
        )
    )
    receipt = runtime.bus.receipt
    assert receipt is not None
    assert receipt.status.value == status
    with sqlite3.connect(runtime.store.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_receipts WHERE turn_id='t_1'").fetchone()[0] == 1


@pytest.mark.anyio
async def test_peer_failure_is_process_event_and_cannot_close_root(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(_create_db(tmp_path))
    await runtime.bus.emit(
        OrchestratorEvent(
            "error",
            "t_1",
            {"message": "peer failed"},
            parent_agent_id="peer_7",
        )
    )
    assert runtime.bus.receipt is None
    events = await runtime.store.list_protocol_events(
        TerminalReadContext(
            "team_1",
            "s_1",
            "t_1",
            "u_1",
            runtime.identity.authz_snapshot_digest,
        )
    )
    assert events[0].event_type == "error"
    assert events[0].payload["parent_agent_id"] == "peer_7"


@pytest.mark.anyio
async def test_post_commit_worker_error_cannot_overwrite_receipt(tmp_path: Path) -> None:
    runtime = await _runtime(_create_db(tmp_path))
    await runtime.bus.emit(
        OrchestratorEvent(
            "done",
            "t_1",
            {"status": "completed", "final_answer": "committed"},
        )
    )
    receipt = runtime.bus.receipt
    assert receipt is not None

    assert not await runtime.bus.emit(
        OrchestratorEvent(
            "error",
            "t_1",
            {"message": "late worker error"},
        )
    )
    assert runtime.bus.receipt == receipt


@pytest.mark.anyio
async def test_post_commit_fanout_failure_closes_live_stream_for_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await _runtime(_create_db(tmp_path))
    subscription = runtime.bus.subscribe().__aiter__()
    waiting = asyncio.create_task(anext(subscription))
    await asyncio.sleep(0)

    async def fail_terminal_replay(*_args, **_kwargs):
        raise RuntimeError("terminal fanout unavailable")

    monkeypatch.setattr(runtime.store, "list_protocol_events", fail_terminal_replay)
    with pytest.raises(TerminalDurabilityAbort) as failure:
        await runtime.bus.emit(
            OrchestratorEvent(
                "done",
                "t_1",
                {"status": "completed", "final_answer": "committed"},
            )
        )
    assert isinstance(failure.value.__cause__, RuntimeError)

    assert runtime.bus.receipt is not None
    with sqlite3.connect(runtime.store.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_receipts WHERE turn_id='t_1'").fetchone() == (1,)
    assert runtime.bus.closed is True
    assert runtime.bus._generation_bus.fanout_failed is True
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(waiting, timeout=0.5)
    await runtime.retire()


@pytest.mark.anyio
async def test_writer_close_failure_still_closes_stream_and_retires_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await _runtime(_create_db(tmp_path))
    runtimes = TerminalRuntimeRegistry()
    runtime.retire_callback = runtimes.retire
    await runtimes.register(runtime)
    subscription = runtime.bus.subscribe().__aiter__()
    waiting = asyncio.create_task(anext(subscription))
    await asyncio.sleep(0)

    async def fail_writer_close() -> None:
        raise RuntimeError("writer drain failed")

    monkeypatch.setattr(runtime.bus._writer, "close", fail_writer_close)
    with pytest.raises(RuntimeError, match="writer drain failed"):
        await runtime.retire()

    assert runtimes.size == 0
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(waiting, timeout=0.5)


@pytest.mark.anyio
async def test_generation_registry_retire_failure_still_drops_runtime(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(_create_db(tmp_path))
    runtimes = TerminalRuntimeRegistry()

    class FailingGenerationRegistry:
        async def retire(self, *_args, **_kwargs) -> None:
            raise RuntimeError("generation retire failed")

    factory = TerminalRuntimeFactory(
        runtime.store,
        FailingGenerationRegistry(),  # type: ignore[arg-type]
        runtimes,
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
    )
    runtime.retire_callback = factory._retire_runtime
    await runtimes.register(runtime)

    with pytest.raises(RuntimeError, match="generation retire failed"):
        await runtime.retire()

    assert runtimes.size == 0


@pytest.mark.anyio
async def test_legacy_startup_cleanup_preserves_v1_running_generation(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(_create_db(tmp_path))
    store = SessionStore(runtime.store.db_path)

    await store.initialize()

    with sqlite3.connect(runtime.store.db_path) as connection:
        turn = connection.execute("SELECT status,terminal_protocol_version FROM turns WHERE id='t_1'").fetchone()
        session = connection.execute("SELECT active_turn_id,active_turn_protocol_version FROM sessions WHERE id='s_1'").fetchone()
    assert turn == ("running", 1)
    assert session == ("t_1", 1)


@pytest.mark.anyio
async def test_factory_fresh_gate_rejects_start_before_store_mutation() -> None:
    class Store:
        calls = 0

        async def admit_or_steer_v1(self, _request):
            self.calls += 1
            raise AssertionError("fresh gate must run before Store mutation")

    store = Store()
    factory = TerminalRuntimeFactory(
        store,  # type: ignore[arg-type]
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
        fresh_admission_ready=lambda: False,
    )
    request = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="hello",
        turn_id="t_1",
        team_id="team_1",
    )

    with pytest.raises(
        TerminalRuntimeAdmissionError,
        match="terminal_protocol_writer_not_ready",
    ):
        await factory.admit_web_turn(
            request,
            request_id="req_1",
            presentation_locale="zh-CN",
        )
    assert store.calls == 0


@pytest.mark.anyio
async def test_factory_writer_readiness_error_is_pre_admission() -> None:
    class Store:
        calls = 0

        async def admit_or_steer_v1(self, _request):
            self.calls += 1
            raise AssertionError("writer readiness must run before Store mutation")

    async def writer_ready() -> bool:
        raise RuntimeError("writer lease read failed")

    store = Store()
    factory = TerminalRuntimeFactory(
        store,  # type: ignore[arg-type]
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
        writer_ready=writer_ready,
    )
    request = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="hello",
        turn_id="t_1",
        team_id="team_1",
    )

    with pytest.raises(
        TerminalRuntimeAdmissionError,
        match="terminal_protocol_writer_not_ready",
    ) as error:
        await factory.admit_web_turn(
            request,
            request_id="req_1",
            presentation_locale="zh-CN",
            require_existing_v1_history=True,
        )
    assert error.value.admission_may_have_committed is False
    assert store.calls == 0


@pytest.mark.anyio
async def test_factory_writer_readiness_cancellation_is_pre_admission() -> None:
    class Store:
        calls = 0

        async def admit_or_steer_v1(self, _request):
            self.calls += 1
            raise AssertionError("writer readiness must run before Store mutation")

    async def writer_ready() -> bool:
        raise asyncio.CancelledError("readiness task cancelled")

    store = Store()
    factory = TerminalRuntimeFactory(
        store,  # type: ignore[arg-type]
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
        writer_ready=writer_ready,
    )
    request = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="hello",
        turn_id="t_1",
        team_id="team_1",
    )

    with pytest.raises(TerminalRuntimePreAdmissionCancelled) as error:
        await factory.admit_web_turn(
            request,
            request_id="req_1",
            presentation_locale="zh-CN",
            require_existing_v1_history=True,
        )
    assert error.value.admission_may_have_committed is False
    assert store.calls == 0


@pytest.mark.anyio
async def test_factory_sticky_v1_start_uses_recovery_writer(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    first_request = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="first",
        turn_id="t_1",
        team_id="team_1",
    )
    vector = await store.ensure_auth_scope_epochs(
        terminal_auth_resolve_context(first_request),
    )
    first = await store.admit_or_steer_v1(
        TerminalAdmissionRequest(
            context=AdmissionContext(
                team_id="team_1",
                session_id="s_1",
                request_id="req_1",
                surface="web",
                cohort="all;config=rollout_1",
                actor="u_1",
                writer_instance_id="gateway_1",
                writer_lease_epoch=7,
                writer_admission_epoch=3,
                rollout_config_generation="rollout_1",
                auth_fence_vector_digest=(vector.auth_fence_vector_digest),
            ),
            turn_id="t_1",
            user_text="first",
            execution_intent={"text": "first"},
            presentation_locale="zh-CN",
            message_catalog_version=1,
            allow_steer=False,
        )
    )
    assert first.accepted is True
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE sessions
            SET active_turn_id=NULL,active_turn_protocol_version=NULL,
                active_turn_generation=NULL,active_turn_claim_epoch=NULL
            WHERE id='s_1'
            """,
        )
        connection.execute(
            """
            UPDATE terminal_protocol_writer_lease
            SET admission_state='draining',admission_epoch=4
            WHERE lease_name='v1_writer'
            """,
        )
        connection.commit()

    async def writer_ready() -> bool:
        return True

    factory = TerminalRuntimeFactory(
        store,
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        TerminalWriterSnapshot(
            "gateway_1",
            7,
            4,
            "rollout_1",
            recovery_only=True,
        ),
        writer_ready=writer_ready,
    )
    request = replace(
        first_request,
        text="second",
        turn_id="t_2",
    )
    request.terminal_auth_fence_vector_digest = vector.auth_fence_vector_digest
    source = ChatTurnUserSource(
        turn_id="t_2",
        session_id="s_1",
        user_id="u_1",
        workspace_id="ws_1",
        user_text="second",
        attachment_ids=(),
        attachment_refs=(),
        parts=None,
    )

    runtime = await factory.admit_web_turn(
        request,
        request_id="req_2",
        presentation_locale="zh-CN",
        user_source=source,
        require_existing_v1_history=True,
    )

    assert runtime.scope.turn_id == "t_2"
    await runtime.retire()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("require_open_writer", "expected"),
    [
        (True, GenerationClaimDisposition.REJECTED),
        (False, GenerationClaimDisposition.CLAIMED),
    ],
)
async def test_generation_claim_can_require_open_writer_atomically(
    tmp_path: Path,
    require_open_writer: bool,
    expected: GenerationClaimDisposition,
) -> None:
    db_path = _create_db(tmp_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    request = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="hello",
        turn_id="t_1",
        team_id="team_1",
    )
    vector = await store.ensure_auth_scope_epochs(terminal_auth_resolve_context(request))
    admitted = await store.admit_or_steer_v1(
        TerminalAdmissionRequest(
            context=AdmissionContext(
                team_id="team_1",
                session_id="s_1",
                request_id="req_1",
                surface="web",
                cohort="all;config=rollout_1",
                actor="u_1",
                writer_instance_id="gateway_1",
                writer_lease_epoch=7,
                writer_admission_epoch=3,
                rollout_config_generation="rollout_1",
                auth_fence_vector_digest=vector.auth_fence_vector_digest,
            ),
            turn_id="t_1",
            user_text="hello",
            execution_intent={"text": "hello"},
            presentation_locale="zh-CN",
            message_catalog_version=1,
            allow_steer=False,
        )
    )
    assert admitted.scope is not None
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE terminal_protocol_writer_lease SET admission_state='draining'")
        connection.commit()

    claimed = await store.claim_generation(
        GenerationClaimContext(
            scope=admitted.scope,
            expected_generation_row_version=1,
            expected_session_claim_epoch=0,
            writer_lease_epoch=7,
            owner_instance_id="gateway_1",
            require_open_writer=require_open_writer,
        )
    )

    assert claimed.disposition is expected


@pytest.mark.anyio
async def test_factory_admits_claims_and_registers_web_runtime(tmp_path: Path) -> None:
    db_path = _create_db(tmp_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    buses = GenerationEventBusRegistry()
    runtimes = TerminalRuntimeRegistry()
    factory = TerminalRuntimeFactory(
        store,
        buses,
        runtimes,
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
    )
    request = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="hello",
        turn_id="t_1",
        team_id="team_1",
    )
    auth_digest = await factory.prepare_web_auth_fence(
        team_id="team_1",
        session_id="s_1",
        user_id="u_1",
    )
    request.terminal_auth_fence_vector_digest = auth_digest

    runtime = await factory.admit_web_turn(
        request,
        request_id="req_1",
        presentation_locale="zh-CN",
    )

    assert await runtimes.get("t_1") is runtime
    assert runtime.scope.generation == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT terminal_protocol_version,status FROM turns WHERE id='t_1'").fetchone() == (1, "running")
        assert connection.execute("SELECT COUNT(*) FROM terminal_auth_scope_epochs").fetchone() == (4,)
        assert connection.execute("SELECT execution_started_at FROM turn_generations WHERE turn_id='t_1' AND generation=1").fetchone() == (None,)
    await runtime.start_execution()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT execution_started_at IS NOT NULL FROM turn_generations WHERE turn_id='t_1' AND generation=1"
        ).fetchone() == (1,)
    await runtime.retire()


@pytest.mark.anyio
async def test_factory_persists_plan_execution_identity_in_typed_web_start(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    factory = TerminalRuntimeFactory(
        store,
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
    )
    request = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="(执行已批准的计划)",
        turn_id="t_plan_1",
        team_id="team_1",
        planning_mode="planning",
        plan_execution_id="plan_1",
        expected_plan_revision=3,
    )
    request.terminal_auth_fence_vector_digest = await factory.prepare_web_auth_fence(
        team_id="team_1",
        session_id="s_1",
        user_id="u_1",
    )
    source = ChatTurnUserSource(
        turn_id=request.turn_id,
        session_id=request.session_id,
        user_id=request.user_id,
        workspace_id=request.workspace_id,
        user_text=request.text,
        attachment_ids=(),
        attachment_refs=(),
        parts=None,
    )

    runtime = await factory.admit_web_turn(
        request,
        request_id="act_start_11111111111111111111111111111111",
        presentation_locale="zh-CN",
        user_source=source,
    )

    assert request.plan_execution_id == "plan_1"
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT execution_intent_json FROM turn_generations WHERE turn_id=? AND generation=1",
            (request.turn_id,),
        ).fetchone()
    assert row is not None
    intent = json.loads(row[0])
    assert intent["planning_mode"] == "planning"
    assert intent["plan_execution_id"] == "plan_1"
    assert intent["expected_plan_revision"] == 3
    await runtime.handoff_to_recovery(reason_code="worker_start_failed")


@pytest.mark.anyio
async def test_factory_concurrent_typed_start_single_flights_runtime_and_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _create_db(tmp_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    buses = GenerationEventBusRegistry()
    runtimes = TerminalRuntimeRegistry()
    recovery: list[tuple[TerminalScope, str]] = []

    async def trigger_recovery(scope: TerminalScope, reason: str) -> None:
        recovery.append((scope, reason))

    factory = TerminalRuntimeFactory(
        store,
        buses,
        runtimes,
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
        recovery_trigger=trigger_recovery,
    )
    auth_digest = await factory.prepare_web_auth_fence(
        team_id="team_1",
        session_id="s_1",
        user_id="u_1",
    )

    def request() -> TurnRequest:
        value = TurnRequest(
            session_id="s_1",
            workspace_id="ws_1",
            user_id="u_1",
            member_uid="m_1",
            text="hello",
            turn_id="t_singleflight",
            team_id="team_1",
        )
        value.terminal_auth_fence_vector_digest = auth_digest
        return value

    def source() -> ChatTurnUserSource:
        return ChatTurnUserSource(
            turn_id="t_singleflight",
            session_id="s_1",
            user_id="u_1",
            workspace_id="ws_1",
            user_text="hello",
            attachment_ids=(),
            attachment_refs=(),
            parts=None,
        )

    original_admit = store.admit_or_steer_v1
    original_claim = store.claim_generation
    second_admitted = asyncio.Event()
    claim_entered = asyncio.Event()
    release_claim = asyncio.Event()
    admission_calls = 0
    claim_calls = 0

    async def observed_admit(value: TerminalAdmissionRequest) -> TerminalAdmissionResult:
        nonlocal admission_calls
        result = await original_admit(value)
        admission_calls += 1
        if admission_calls == 2:
            second_admitted.set()
        return result

    async def blocked_claim(value: GenerationClaimContext):
        nonlocal claim_calls
        claim_calls += 1
        claim_entered.set()
        await release_claim.wait()
        return await original_claim(value)

    monkeypatch.setattr(store, "admit_or_steer_v1", observed_admit)
    monkeypatch.setattr(store, "claim_generation", blocked_claim)

    first = asyncio.create_task(
        factory.admit_web_turn(
            request(),
            request_id="req_singleflight",
            presentation_locale="zh-CN",
            user_source=source(),
        )
    )
    await asyncio.wait_for(claim_entered.wait(), timeout=1)
    second = asyncio.create_task(
        factory.admit_web_turn(
            request(),
            request_id="req_singleflight",
            presentation_locale="zh-CN",
            user_source=source(),
        )
    )
    await asyncio.wait_for(second_admitted.wait(), timeout=1)
    assert claim_calls == 1
    release_claim.set()

    first_runtime, second_runtime = await asyncio.gather(first, second)
    assert first_runtime is second_runtime
    assert claim_calls == 1
    assert recovery == []
    assert runtimes.size == 1
    assert buses.size == 1
    generation_bus = await buses.get(first_runtime.scope)
    assert generation_bus is not None
    assert generation_bus.retired is False
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state,claim_epoch FROM turn_generations WHERE turn_id='t_singleflight' AND generation=1").fetchone() == (
            "running",
            1,
        )
        assert connection.execute("SELECT COUNT(*) FROM chat_turn_user_sources WHERE turn_id='t_singleflight'").fetchone() == (1,)

    worker_entered = asyncio.Event()
    release_worker = asyncio.Event()
    worker_calls = 0

    def create_worker() -> asyncio.Task[object]:
        nonlocal worker_calls
        worker_calls += 1

        async def run() -> object:
            worker_entered.set()
            await release_worker.wait()
            return None

        return asyncio.create_task(run())

    first_spawn = asyncio.create_task(first_runtime.spawn_worker_once(create_worker))
    await asyncio.wait_for(worker_entered.wait(), timeout=1)
    second_spawn = asyncio.create_task(second_runtime.spawn_worker_once(create_worker))
    await asyncio.sleep(0)
    assert worker_calls == 1
    release_worker.set()
    (first_task, first_started), (second_task, second_started) = await asyncio.gather(
        first_spawn,
        second_spawn,
    )
    assert first_task is second_task
    assert {first_started, second_started} == {False, True}
    await first_task
    await first_runtime.retire()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("fault", "persisted_count"),
    [
        (TerminalProtocolFaultPoint.ADMISSION_BEFORE_COMMIT, 0),
        (TerminalProtocolFaultPoint.ADMISSION_AFTER_COMMIT, 1),
    ],
)
async def test_factory_typed_home_admission_fault_triggers_recovery_with_atomic_state(
    tmp_path: Path,
    fault: TerminalProtocolFaultPoint,
    persisted_count: int,
) -> None:
    db_path = _create_db(tmp_path)

    def inject(point: TerminalProtocolFaultPoint) -> None:
        if point is fault:
            raise RuntimeError(f"injected:{point.value}")

    store = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=inject,
    )
    recovery: list[tuple[TerminalScope, str]] = []

    async def trigger(scope: TerminalScope, reason: str) -> None:
        recovery.append((scope, reason))

    factory = TerminalRuntimeFactory(
        store,
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
        recovery_trigger=trigger,
    )
    session_id = f"s_home_{fault.value.lower()}"
    turn_id = f"t_home_{fault.value.lower()}"
    request = TurnRequest(
        session_id=session_id,
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="hello",
        turn_id=turn_id,
        team_id="team_1",
    )
    request.terminal_auth_fence_vector_digest = await factory.prepare_web_auth_fence(
        team_id="team_1",
        session_id=session_id,
        user_id="u_1",
    )
    source = ChatTurnUserSource(
        turn_id=turn_id,
        session_id=session_id,
        user_id="u_1",
        workspace_id="ws_1",
        user_text="hello",
        attachment_ids=(),
        attachment_refs=(),
        parts=None,
    )

    with pytest.raises(RuntimeError, match=fault.value):
        await factory.admit_web_turn(
            request,
            request_id="act_start_0123456789abcdef0123456789abcdef",
            presentation_locale="zh-CN",
            user_source=source,
            create_session_if_missing=True,
        )

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone() == (persisted_count,)
        assert connection.execute(
            "SELECT COUNT(*) FROM turns WHERE id=?",
            (turn_id,),
        ).fetchone() == (persisted_count,)
        assert connection.execute(
            "SELECT COUNT(*) FROM turn_generations WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == (persisted_count,)
        assert connection.execute(
            "SELECT COUNT(*) FROM chat_turn_user_sources WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == (persisted_count,)
    assert recovery == [
        (
            TerminalScope("team_1", session_id, turn_id, 1),
            "web_start_admission_unconfirmed",
        )
    ]


@pytest.mark.anyio
async def test_factory_typed_admission_cancellation_preserves_cancel_and_shields_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _create_db(tmp_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    original_admit = store.admit_or_steer_v1

    async def cancel_after_commit(
        request: TerminalAdmissionRequest,
    ) -> TerminalAdmissionResult:
        await original_admit(request)
        raise asyncio.CancelledError

    monkeypatch.setattr(store, "admit_or_steer_v1", cancel_after_commit)
    recovery_completed = asyncio.Event()
    recovery: list[str] = []

    async def trigger(_scope: TerminalScope, reason: str) -> None:
        await asyncio.sleep(0)
        recovery.append(reason)
        recovery_completed.set()

    factory = TerminalRuntimeFactory(
        store,
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
        recovery_trigger=trigger,
    )
    request = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="hello",
        turn_id="t_cancelled_admission",
        team_id="team_1",
    )
    request.terminal_auth_fence_vector_digest = await factory.prepare_web_auth_fence(
        team_id="team_1",
        session_id="s_1",
        user_id="u_1",
    )
    source = ChatTurnUserSource(
        turn_id=request.turn_id,
        session_id=request.session_id,
        user_id=request.user_id,
        workspace_id=request.workspace_id,
        user_text=request.text,
        attachment_ids=(),
        attachment_refs=(),
        parts=None,
    )

    with pytest.raises(asyncio.CancelledError):
        await factory.admit_web_turn(
            request,
            request_id="act_start_11111111111111111111111111111111",
            presentation_locale="zh-CN",
            user_source=source,
        )
    assert recovery_completed.is_set()
    assert recovery == ["web_start_admission_cancelled"]


@pytest.mark.anyio
async def test_factory_cold_typed_replay_claims_and_attaches_from_persisted_opened(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)

    def inject(point: TerminalProtocolFaultPoint) -> None:
        if point is TerminalProtocolFaultPoint.ADMISSION_AFTER_COMMIT:
            raise RuntimeError("admission acknowledgement lost")

    faulty_store = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=inject,
    )
    writer = TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1")
    first_factory = TerminalRuntimeFactory(
        faulty_store,
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        writer,
    )
    request = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="hello",
        turn_id="t_cold_replay",
        team_id="team_1",
        active_datarc_project="project_1",
        language_context=LanguageContext(
            ui_language="en-US",
            output_language="en-US",
            data_language="en-US",
            week_start_day="sunday",
        ),
        preferred_provider="provider_1",
        preferred_model="model_1",
        paradigm_v2=True,
    )
    request.terminal_auth_fence_vector_digest = await first_factory.prepare_web_auth_fence(
        team_id="team_1",
        session_id="s_1",
        user_id="u_1",
    )
    source = ChatTurnUserSource(
        turn_id=request.turn_id,
        session_id=request.session_id,
        user_id=request.user_id,
        workspace_id=request.workspace_id,
        user_text=request.text,
        attachment_ids=(),
        attachment_refs=(),
        parts=None,
    )
    with pytest.raises(RuntimeError, match="acknowledgement lost"):
        await first_factory.admit_web_turn(
            request,
            request_id="act_start_22222222222222222222222222222222",
            presentation_locale="en-US",
            user_source=source,
        )
    with sqlite3.connect(db_path) as connection:
        persisted_intent = json.loads(
            connection.execute(
                "SELECT execution_intent_json FROM turn_generations WHERE turn_id=? AND generation=1",
                (request.turn_id,),
            ).fetchone()[0]
        )
    assert persisted_intent["paradigm_v2"] is True

    clean_store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    buses = GenerationEventBusRegistry()
    runtimes = TerminalRuntimeRegistry()
    fresh_factory = TerminalRuntimeFactory(
        clean_store,
        buses,
        runtimes,
        writer,
    )
    conflicting_request = replace(
        request,
        preferred_model="model_2",
    )
    with pytest.raises(
        TerminalRuntimeAdmissionError,
        match="terminal_admission_request_conflict",
    ):
        await fresh_factory.admit_web_turn(
            conflicting_request,
            request_id="act_start_22222222222222222222222222222222",
            presentation_locale="en-US",
            user_source=source,
        )
    for conflicting_request in (
        replace(request, active_datarc_project="project_2"),
        replace(request, member_uid="m_2"),
    ):
        with pytest.raises(
            TerminalRuntimeAdmissionError,
            match="terminal_admission_request_conflict",
        ):
            await fresh_factory.admit_web_turn(
                conflicting_request,
                request_id="act_start_22222222222222222222222222222222",
                presentation_locale="en-US",
                user_source=source,
            )
    replay_request = replace(request)
    runtime = await fresh_factory.admit_web_turn(
        replay_request,
        request_id="act_start_22222222222222222222222222222222",
        presentation_locale="zh-CN",
        user_source=source,
    )

    assert await runtimes.get_exact(runtime.scope) is runtime
    assert runtime.followup_execution_intent["presentation_locale"] == "en-US"
    assert runtime.followup_execution_intent["active_datarc_project"] == "project_1"
    assert runtime.followup_execution_intent["member_uid"] == "m_1"
    assert runtime.followup_execution_intent["paradigm_v2"] is True
    assert replay_request.active_datarc_project == "project_1"
    assert replay_request.member_uid == "m_1"
    assert replay_request.paradigm_v2 is True
    assert replay_request.preferred_provider == "provider_1"
    assert replay_request.preferred_model == "model_1"
    assert replay_request.language_context == LanguageContext(
        ui_language="en-US",
        output_language="en-US",
        data_language="en-US",
        week_start_day="sunday",
    )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state,claim_epoch FROM turn_generations WHERE turn_id=? AND generation=1",
            (request.turn_id,),
        ).fetchone() == ("running", 1)
    await runtime.retire()


@pytest.mark.anyio
async def test_factory_legacy_replay_keeps_pre_r02_execution_intent_shape(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    request = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="hello",
        turn_id="t_legacy_replay",
        team_id="team_1",
        language_context=LanguageContext.default(),
        preferred_provider="provider_1",
        preferred_model="model_1",
    )
    vector = await store.ensure_auth_scope_epochs(
        terminal_auth_resolve_context(request),
    )
    old_shape = TerminalAdmissionRequest(
        context=AdmissionContext(
            team_id="team_1",
            session_id="s_1",
            request_id="req_legacy_replay",
            surface="web",
            cohort="all;config=rollout_1",
            actor="u_1",
            writer_instance_id="gateway_1",
            writer_lease_epoch=7,
            writer_admission_epoch=3,
            rollout_config_generation="rollout_1",
            auth_fence_vector_digest=vector.auth_fence_vector_digest,
        ),
        turn_id=request.turn_id,
        user_text=request.text,
        execution_intent={
            "attachments": [],
            "planning_mode": None,
            "text": "hello",
            "workspace_id": "ws_1",
        },
        presentation_locale="zh-CN",
        message_catalog_version=1,
        allow_steer=False,
    )
    admitted = await store.admit_or_steer_v1(old_shape)
    assert admitted.disposition is AdmissionDisposition.ADMITTED

    factory = TerminalRuntimeFactory(
        store,
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
    )
    request.terminal_auth_fence_vector_digest = vector.auth_fence_vector_digest
    with pytest.raises(
        TerminalRuntimeAdmissionError,
        match="terminal_admission_replay_pending",
    ):
        await factory.admit_web_turn(
            request,
            request_id="req_legacy_replay",
            presentation_locale="zh-CN",
        )


@pytest.mark.anyio
async def test_fresh_opened_generation_waits_for_worker_start_grace_before_recovery(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    now = [_NOW]

    def inject(point: TerminalProtocolFaultPoint) -> None:
        if point is TerminalProtocolFaultPoint.ADMISSION_AFTER_COMMIT:
            raise RuntimeError("admission acknowledgement lost")

    store = TerminalProtocolStore(
        db_path,
        _clock=lambda: now[0],
        _fault_injector=inject,
    )
    factory = TerminalRuntimeFactory(
        store,
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
    )
    request = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="hello",
        turn_id="t_grace",
        team_id="team_1",
    )
    request.terminal_auth_fence_vector_digest = await factory.prepare_web_auth_fence(
        team_id="team_1",
        session_id="s_1",
        user_id="u_1",
    )
    source = ChatTurnUserSource(
        turn_id=request.turn_id,
        session_id=request.session_id,
        user_id=request.user_id,
        workspace_id=request.workspace_id,
        user_text=request.text,
        attachment_ids=(),
        attachment_refs=(),
        parts=None,
    )
    with pytest.raises(RuntimeError, match="acknowledgement lost"):
        await factory.admit_web_turn(
            request,
            request_id="act_start_33333333333333333333333333333333",
            presentation_locale="zh-CN",
            user_source=source,
        )

    writer = TerminalWriterLease(
        owner_instance_id="gateway_1",
        writer_lease_epoch=7,
        writer_admission_epoch=3,
        rollout_config_generation="rollout_1",
        lease_expires_at=(_NOW + timedelta(minutes=5)).isoformat(),
        admission_state="open",
    )
    first = await TerminalRecoveryProcessor(store).run_bounded(
        owner_instance_id="gateway_1",
        rollout_config_generation="rollout_1",
        writer_fence=writer,
    )

    assert first.processed_count == 0
    assert first.eligible_remaining == 0
    assert first.next_due_at is not None
    assert first.max_due_at is not None
    assert datetime.fromisoformat(first.next_due_at.replace("Z", "+00:00")) == _NOW + timedelta(seconds=30)
    assert first.max_due_at == first.next_due_at
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state,claim_epoch FROM turn_generations WHERE turn_id='t_grace'").fetchone() == ("opened", 0)

    now[0] = _NOW + timedelta(seconds=31)
    second = await TerminalRecoveryProcessor(store).run_bounded(
        owner_instance_id="gateway_1",
        rollout_config_generation="rollout_1",
        writer_fence=writer,
    )

    assert second.processed_count == 1
    assert second.eligible_remaining == 0
    assert second.next_due_at is None
    assert second.max_due_at is None
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state FROM turn_generations WHERE turn_id='t_grace'").fetchone() == ("closed",)


@pytest.mark.anyio
@pytest.mark.parametrize("cancel_before_register", [False, True])
async def test_runtime_attach_owner_cancel_does_not_cancel_waiter(
    tmp_path: Path,
    cancel_before_register: bool,
) -> None:
    runtime = await _runtime(_create_db(tmp_path))
    registry = TerminalRuntimeRegistry()
    attach_entered = asyncio.Event()
    allow_register = asyncio.Event()
    registered = asyncio.Event()
    allow_return = asyncio.Event()
    unexpected_attach_calls = 0

    async def attach_owner() -> TerminalTurnRuntime:
        attach_entered.set()
        await allow_register.wait()
        await registry.register(runtime)
        registered.set()
        await allow_return.wait()
        return runtime

    async def unexpected_attach() -> TerminalTurnRuntime:
        nonlocal unexpected_attach_calls
        unexpected_attach_calls += 1
        raise AssertionError("attach waiter became a second owner")

    owner = asyncio.create_task(registry.attach_runtime_once(runtime.scope, attach_owner))
    await asyncio.wait_for(attach_entered.wait(), timeout=1)
    waiter = asyncio.create_task(registry.attach_runtime_once(runtime.scope, unexpected_attach))
    await asyncio.sleep(0)
    if cancel_before_register:
        owner.cancel()
        allow_register.set()
        allow_return.set()
    else:
        allow_register.set()
        await asyncio.wait_for(registered.wait(), timeout=1)

        await registry._lock.acquire()
        try:
            allow_return.set()
            await asyncio.sleep(0)
            owner.cancel()
        finally:
            registry._lock.release()

    with pytest.raises(asyncio.CancelledError):
        await owner
    assert await asyncio.wait_for(waiter, timeout=1) is runtime
    assert unexpected_attach_calls == 0
    assert registry._attach_reservations == {}
    await runtime.retire()


@pytest.mark.anyio
async def test_worker_task_is_reused_until_runtime_retire_and_stale_runtime_cannot_spawn(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(_create_db(tmp_path))
    registry = TerminalRuntimeRegistry()
    await registry.register(runtime)
    runtime.retire_callback = registry.retire

    async def bind(
        spawn: Callable[[], asyncio.Task[object]],
    ) -> tuple[asyncio.Task[object], bool]:
        return await registry.spawn_worker_once(
            runtime.scope,
            spawn,
            expected_runtime=runtime,
        )

    runtime.worker_spawn_callback = bind
    spawn_calls = 0

    def spawn() -> asyncio.Task[object]:
        nonlocal spawn_calls
        spawn_calls += 1

        async def run() -> object:
            return None

        return asyncio.create_task(run())

    first_task, first_started = await runtime.spawn_worker_once(spawn)
    await first_task
    replay_task, replay_started = await runtime.spawn_worker_once(spawn)

    assert replay_task is first_task
    assert first_started is True
    assert replay_started is False
    assert spawn_calls == 1
    await runtime.retire()
    assert await registry.get_exact(runtime.scope) is None
    assert (runtime.scope.turn_id, runtime.scope.generation) not in registry._workers
    with pytest.raises(RuntimeError, match="retired"):
        await runtime.spawn_worker_once(spawn)


@pytest.mark.anyio
async def test_worker_creation_failure_fences_concurrent_waiter_until_retire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await _runtime(_create_db(tmp_path))
    registry = TerminalRuntimeRegistry()
    await registry.register(runtime)
    runtime.retire_callback = registry.retire

    async def bind(
        spawn: Callable[[], asyncio.Task[object]],
    ) -> tuple[asyncio.Task[object], bool]:
        return await registry.spawn_worker_once(
            runtime.scope,
            spawn,
            expected_runtime=runtime,
        )

    runtime.worker_spawn_callback = bind
    both_failures_observed = asyncio.Event()
    allow_retire = asyncio.Event()
    spawn_factory_calls = 0
    spawn_failures = 0
    handoff_calls = 0
    original_handoff = runtime.store.handoff_execution_to_recovery

    async def count_handoff(
        context: ExecutionGenerationContext,
        *,
        reason_code: str,
    ) -> bool:
        nonlocal handoff_calls
        handoff_calls += 1
        return await original_handoff(context, reason_code=reason_code)

    monkeypatch.setattr(
        runtime.store,
        "handoff_execution_to_recovery",
        count_handoff,
    )

    def fail_spawn() -> asyncio.Task[object]:
        nonlocal spawn_factory_calls
        spawn_factory_calls += 1
        raise RuntimeError("task creation failed")

    def unexpected_spawn() -> asyncio.Task[object]:
        nonlocal spawn_factory_calls
        spawn_factory_calls += 1

        async def run() -> object:
            return None

        return asyncio.create_task(run())

    async def spawn_then_abort(
        spawn: Callable[[], asyncio.Task[object]],
        error: str,
    ) -> None:
        nonlocal spawn_failures
        with pytest.raises(RuntimeError, match=error):
            await runtime.spawn_worker_once(spawn)
        spawn_failures += 1
        if spawn_failures == 2:
            both_failures_observed.set()
        await allow_retire.wait()
        await runtime.abort_worker_start_failed()

    owner = asyncio.create_task(
        spawn_then_abort(fail_spawn, "task creation failed"),
    )
    await asyncio.sleep(0)
    waiter = asyncio.create_task(
        spawn_then_abort(unexpected_spawn, "fenced after creation failure"),
    )
    await asyncio.wait_for(both_failures_observed.wait(), timeout=1)
    assert spawn_factory_calls == 1
    assert registry._workers == {}

    allow_retire.set()
    await asyncio.gather(owner, waiter)
    assert handoff_calls == 1
    assert runtime.retired
    assert registry._worker_start_failures == set()


@pytest.mark.anyio
async def test_worker_start_abort_failure_preserves_error_notifies_once_and_cleans_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await _runtime(_create_db(tmp_path))
    registry = TerminalRuntimeRegistry()
    await registry.register(runtime)
    runtime.retire_callback = registry.retire

    async def bind(
        spawn: Callable[[], asyncio.Task[object]],
    ) -> tuple[asyncio.Task[object], bool]:
        return await registry.spawn_worker_once(
            runtime.scope,
            spawn,
            expected_runtime=runtime,
        )

    runtime.worker_spawn_callback = bind

    def fail_spawn() -> asyncio.Task[object]:
        raise RuntimeError("task creation failed")

    with pytest.raises(RuntimeError, match="task creation failed"):
        await runtime.spawn_worker_once(fail_spawn)
    assert registry._worker_start_failures == {(runtime.scope.turn_id, runtime.scope.generation)}

    handoff_error = RuntimeError("handoff failed")

    async def fail_handoff(
        context: ExecutionGenerationContext,
        *,
        reason_code: str,
    ) -> bool:
        assert context == runtime.execution
        assert reason_code == "worker_start_failed"
        raise handoff_error

    monkeypatch.setattr(
        runtime.store,
        "handoff_execution_to_recovery",
        fail_handoff,
    )
    callbacks: list[tuple[str, BaseException | None]] = []

    async def observe_handoff(
        observed_runtime: TerminalTurnRuntime,
        reason_code: str,
        error: BaseException | None,
    ) -> None:
        assert observed_runtime is runtime
        callbacks.append((reason_code, error))

    runtime.handoff_callback = observe_handoff

    with pytest.raises(RuntimeError, match="handoff failed") as caught:
        await runtime.abort_worker_start_failed()

    assert caught.value is handoff_error
    assert callbacks == [("worker_start_failed", handoff_error)]
    assert runtime.retired
    assert await registry.get_exact(runtime.scope) is None
    assert registry._workers == {}
    assert registry._worker_start_failures == set()

    await runtime.abort_worker_start_failed()
    assert callbacks == [("worker_start_failed", handoff_error)]


@pytest.mark.anyio
async def test_runtime_retire_fences_a_worker_waiting_for_registry_lock(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(_create_db(tmp_path))
    registry = TerminalRuntimeRegistry()
    await registry.register(runtime)
    runtime.retire_callback = registry.retire

    async def bind(
        spawn: Callable[[], asyncio.Task[object]],
    ) -> tuple[asyncio.Task[object], bool]:
        return await registry.spawn_worker_once(
            runtime.scope,
            spawn,
            expected_runtime=runtime,
        )

    runtime.worker_spawn_callback = bind
    spawn_calls = 0

    def spawn() -> asyncio.Task[object]:
        nonlocal spawn_calls
        spawn_calls += 1

        async def run() -> object:
            return None

        return asyncio.create_task(run())

    await registry._lock.acquire()
    try:
        late_spawn = asyncio.create_task(runtime.spawn_worker_once(spawn))
        await asyncio.sleep(0)
        retiring = asyncio.create_task(runtime.retire())
        while not runtime.retired:
            await asyncio.sleep(0)
    finally:
        registry._lock.release()

    await retiring
    with pytest.raises(RuntimeError, match="no longer active"):
        await late_spawn
    assert spawn_calls == 0
    assert registry._workers == {}


@pytest.mark.anyio
async def test_factory_claim_ack_loss_triggers_immediate_recovery(tmp_path: Path) -> None:
    db_path = _create_db(tmp_path)
    triggered: list[tuple[object, str]] = []

    def inject(point: TerminalProtocolFaultPoint) -> None:
        if point is TerminalProtocolFaultPoint.CLAIM_AFTER_COMMIT:
            raise RuntimeError("claim acknowledgement lost")

    async def trigger(scope: object, reason: str) -> None:
        triggered.append((scope, reason))

    factory = TerminalRuntimeFactory(
        TerminalProtocolStore(db_path, _clock=lambda: _NOW, _fault_injector=inject),
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
        recovery_trigger=trigger,
    )
    request = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="hello",
        turn_id="t_1",
        team_id="team_1",
    )
    request.terminal_auth_fence_vector_digest = await factory.prepare_web_auth_fence(
        team_id="team_1",
        session_id="s_1",
        user_id="u_1",
    )

    with pytest.raises(RuntimeError, match="claim acknowledgement lost"):
        await factory.admit_web_turn(
            request,
            request_id="req_1",
            presentation_locale="zh-CN",
        )

    assert len(triggered) == 1
    assert triggered[0][1] == "generation_claim_failed"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state,owner_instance_id,claim_epoch FROM turn_generations WHERE turn_id='t_1' AND generation=1"
        ).fetchone() == ("running", "gateway_1", 1)


@pytest.mark.anyio
async def test_successful_runtime_handoff_triggers_recovery_processor(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    results = []
    writer = TerminalWriterLease(
        owner_instance_id="gateway_1",
        writer_lease_epoch=7,
        writer_admission_epoch=3,
        rollout_config_generation="rollout_1",
        lease_expires_at=(_NOW + timedelta(minutes=5)).isoformat(),
        admission_state="open",
    )

    async def trigger(_scope: object, reason: str) -> None:
        assert reason == "worker_lost:handoff_completed"
        results.append(
            await TerminalRecoveryProcessor(store).run_bounded(
                owner_instance_id="gateway_1",
                rollout_config_generation="rollout_1",
                writer_fence=writer,
            )
        )

    factory = TerminalRuntimeFactory(
        store,
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
        recovery_trigger=trigger,
    )
    request = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="hello",
        turn_id="t_1",
        team_id="team_1",
    )
    request.terminal_auth_fence_vector_digest = await factory.prepare_web_auth_fence(
        team_id="team_1",
        session_id="s_1",
        user_id="u_1",
    )
    runtime = await factory.admit_web_turn(
        request,
        request_id="req_1",
        presentation_locale="zh-CN",
    )
    await runtime.start_execution()
    await runtime.handoff_to_recovery(reason_code="worker_lost")

    assert len(results) == 1
    assert results[0].processed_count == 1
    assert results[0].errors == ()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state FROM turn_generations WHERE turn_id='t_1'").fetchone() == ("closed",)
        assert connection.execute("SELECT status FROM turn_terminal_receipts WHERE turn_id='t_1'").fetchone() == ("aborted",)


@pytest.mark.anyio
async def test_worker_create_failure_aborts_without_started_marker_or_heartbeat(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    factory = TerminalRuntimeFactory(
        store,
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
    )
    request = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="hello",
        turn_id="t_1",
        team_id="team_1",
    )
    request.terminal_auth_fence_vector_digest = await factory.prepare_web_auth_fence(
        team_id="team_1",
        session_id="s_1",
        user_id="u_1",
    )
    runtime = await factory.admit_web_turn(
        request,
        request_id="req_1",
        presentation_locale="zh-CN",
    )

    await runtime.abort_worker_start_failed()

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT execution_started_at,state,terminal_receipt_id FROM turn_generations WHERE turn_id='t_1' AND generation=1"
        ).fetchone() == (None, "closed", runtime.bus.receipt.receipt_id)
        assert connection.execute("SELECT status,reason_code FROM turn_terminal_receipts").fetchone() == ("aborted", "worker_start_failed")
    assert runtime._heartbeat_task is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("member_identity_drift", "fault_point", "drain_before_followup"),
    [
        (False, None, False),
        (True, None, False),
        (
            False,
            TerminalProtocolFaultPoint.FOLLOWUP_AFTER_PARENT_RECEIPT_BEFORE_CLAIM,
            False,
        ),
        (
            False,
            TerminalProtocolFaultPoint.FOLLOWUP_AFTER_CLAIM_BEFORE_TASK,
            False,
        ),
        (False, None, True),
    ],
)
async def test_certified_followup_commits_child_before_single_worker_spawn(
    tmp_path: Path,
    member_identity_drift: bool,
    fault_point: TerminalProtocolFaultPoint | None,
    drain_before_followup: bool,
    datarc_source_change: bool = False,
) -> None:
    from bowhead_core.datarc_selection import DatarcConnectionBinding, DatarcExecutionScope, DatarcRunState, DatarcSelection

    db_path = _create_db(tmp_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    spawned: list[TurnRequest] = []
    worker_done = asyncio.Event()
    fresh_admission_ready = True
    triggered: list[str] = []
    source_binding = DatarcConnectionBinding("team_1", "dc_one", "revision_one", object()) if datarc_source_change else None

    async def trigger_recovery(_scope: object, reason: str) -> None:
        triggered.append(reason)

    def fresh_ready() -> bool:
        return fresh_admission_ready

    async def spawn_followup(request: TurnRequest) -> None:
        spawned.append(request)
        child = request.terminal_runtime
        assert child is not None

        def create() -> asyncio.Task[object]:
            async def worker() -> object:
                await child.start_execution()
                worker_done.set()
                return None

            return asyncio.create_task(worker())

        await child.spawn_worker_once(create)

    async def resolve_followup(template, parent_scope) -> TurnRequest:
        intent = template["execution_intent"]
        current_digest = await factory.prepare_web_auth_fence(
            team_id=parent_scope.team_id,
            session_id=parent_scope.session_id,
            user_id=intent["user_id"],
        )
        return TurnRequest(
            session_id=parent_scope.session_id,
            workspace_id=intent["workspace_id"],
            user_id=intent["user_id"],
            member_uid=("m_identity_drift" if member_identity_drift else intent["member_uid"]),
            text=template["user_text"],
            turn_id=template["turn_id"],
            team_id=parent_scope.team_id,
            planning_mode=intent["planning_mode"],
            attachments=list(intent["attachments"]),
            language_context=LanguageContext.resolve(
                accept_language=intent["presentation_locale"],
            ),
            access_context={"team_id": parent_scope.team_id},
            team_router=object(),
            team_kb_retriever=object(),
            resume_turn_id=parent_scope.turn_id,
            terminal_auth_fence_vector_digest=current_digest,
            active_datarc_project=intent.get("active_datarc_project"),
            datarc_binding=source_binding,
            datarc_source_evidence=intent.get("datarc_selection"),
        )

    injected = False

    def inject(point: TerminalProtocolFaultPoint) -> None:
        nonlocal injected
        if point is fault_point and not injected:
            injected = True
            raise RuntimeError(point.value)

    factory = TerminalRuntimeFactory(
        store,
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
        followup_capability=TerminalFollowupCapability(
            certified=True,
            adapter_generation="rollout_1",
        ),
        followup_worker_spawner=spawn_followup,
        followup_request_resolver=resolve_followup,
        recovery_trigger=trigger_recovery,
        fresh_admission_ready=fresh_ready,
        fault_injector=inject,
    )
    request = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="hello",
        turn_id="t_1",
        team_id="team_1",
    )
    digest = await factory.prepare_web_auth_fence(
        team_id="team_1",
        session_id="s_1",
        user_id="u_1",
    )
    request.terminal_auth_fence_vector_digest = digest
    runtime = await factory.admit_web_turn(
        request,
        request_id="req_1",
        presentation_locale="en-US",
    )
    await runtime.start_execution()
    if datarc_source_change:
        source = DatarcSelection(source_binding, "u_1", "source_a", "Source A", "m_1", source_binding.client)
        owner = DatarcRunState(DatarcExecutionScope(source, 0), team_id="team_1")
        runtime.bus.bind_datarc_run(owner)
        owner.publish(replace(source, datarc_workspace_id="source_b", name="Source B", member_uid="member_b"))
    await factory.steer_web_turn(
        team_id="team_1",
        session_id="s_1",
        user_id="u_1",
        expected_turn_id="t_1",
        expected_execution_generation=1,
        request_id="req_steer_1",
        user_msg_id="msg_steer_1",
        user_text="",
        workspace_id="ws_1",
        auth_fence_vector_digest=digest,
        presentation_locale="en-US",
        attachments=("artifact_1",),
        planning_mode="planning",
    )

    done = OrchestratorEvent(
        "done",
        "t_1",
        {"status": "completed", "final_answer": "parent done"},
    )
    if drain_before_followup:
        fresh_admission_ready = False
    if member_identity_drift:
        with pytest.raises(
            TerminalDurabilityAbort,
            match="terminal durable finalization failed",
        ):
            await runtime.bus.emit(done)
        assert spawned == []
        with sqlite3.connect(db_path) as connection:
            assert connection.execute(
                """
                SELECT state,claim_epoch,owner_instance_id
                FROM turn_generations
                WHERE turn_id<>'t_1'
                """
            ).fetchone() == ("opened", 0, None)
        return
    if drain_before_followup:
        with pytest.raises(
            TerminalDurabilityAbort,
            match="terminal durable finalization failed",
        ):
            await runtime.bus.emit(done)
        assert spawned == []
        assert "followup_writer_not_ready" in triggered
        with sqlite3.connect(db_path) as connection:
            assert connection.execute(
                """
                SELECT state,claim_epoch,owner_instance_id
                FROM turn_generations
                WHERE turn_id<>'t_1'
                """
            ).fetchone() == ("opened", 0, None)
        return
    if fault_point is not None:
        with pytest.raises(
            TerminalDurabilityAbort,
            match="terminal durable finalization failed",
        ):
            await runtime.bus.emit(done)
        assert injected is True
        assert spawned == []
        with sqlite3.connect(db_path) as connection:
            child_turn_id = connection.execute("SELECT id FROM turns WHERE id<>'t_1'").fetchone()[0]
            assert connection.execute("SELECT COUNT(*) FROM turns WHERE session_id='s_1'").fetchone() == (2,)
            assert connection.execute(
                "SELECT COUNT(*) FROM turn_generations WHERE turn_id=?",
                (child_turn_id,),
            ).fetchone() == (1,)
            child_state = connection.execute(
                "SELECT state,claim_epoch,execution_started_at FROM turn_generations WHERE turn_id=?",
                (child_turn_id,),
            ).fetchone()
            assert child_state == (
                ("opened", 0, None) if fault_point is TerminalProtocolFaultPoint.FOLLOWUP_AFTER_PARENT_RECEIPT_BEFORE_CLAIM else ("running", 1, None)
            )

        recovery = await TerminalRecoveryProcessor(
            TerminalProtocolStore(
                db_path,
                _clock=lambda: _NOW + timedelta(minutes=6),
            )
        ).run_bounded(
            owner_instance_id="gateway_followup_recovery",
            rollout_config_generation="recovery_followup",
        )
        assert recovery.errors == ()
        assert recovery.eligible_remaining == 0
        with sqlite3.connect(db_path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM turn_generations WHERE turn_id=?",
                (child_turn_id,),
            ).fetchone() == (1,)
            assert connection.execute(
                "SELECT status FROM turn_terminal_receipts WHERE turn_id=?",
                (child_turn_id,),
            ).fetchone() == ("aborted",)
            assert connection.execute("SELECT cost_usd,tokens_total FROM sessions WHERE id='s_1'").fetchone() == (0, 0)
        return
    await runtime.bus.emit(done)
    await asyncio.wait_for(worker_done.wait(), timeout=1)
    await runtime.bus.emit(done)

    assert runtime.bus.receipt is not None
    assert runtime.bus.receipt.selected_session_variant is SessionVariant.FOLLOWUP
    assert len(spawned) == 1
    child_request = spawned[0]
    assert child_request.text == ""
    assert child_request.attachments == ["artifact_1"]
    assert child_request.planning_mode == "planning"
    assert child_request.language_context is not None
    assert child_request.language_context.ui_language == "en-US"
    assert child_request.resume_turn_id == "t_1"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turns WHERE session_id='s_1'").fetchone() == (2,)
        assert connection.execute("SELECT active_turn_id,active_turn_generation,active_turn_claim_epoch FROM sessions WHERE id='s_1'").fetchone() == (
            child_request.turn_id,
            1,
            1,
        )
        assert connection.execute(
            "SELECT state,execution_started_at IS NOT NULL FROM turn_generations WHERE turn_id=?",
            (child_request.turn_id,),
        ).fetchone() == ("running", 1)
    template = await store.read_committed_followup_template(runtime.bus.receipt)
    assert template is not None
    assert template["turn_id"] == child_request.turn_id
    if datarc_source_change:
        assert child_request.active_datarc_project == "source_b"
        assert child_request.member_uid == "member_b"
        assert template["execution_intent"]["datarc_selection"] == child_request.datarc_source_evidence
        assert child_request.datarc_source_evidence["datarc_workspace_id"] == "source_b"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turns SET user_text='tampered' WHERE id=?",
            (child_request.turn_id,),
        )
        connection.commit()
    with pytest.raises(
        TerminalProtocolMalformedError,
        match="committed child envelope changed",
    ):
        await store.read_committed_followup_template(runtime.bus.receipt)


@pytest.mark.anyio
async def test_followup_persists_current_root_source_after_switch(tmp_path):
    await test_certified_followup_commits_child_before_single_worker_spawn(tmp_path, False, None, False, datarc_source_change=True)


@pytest.mark.anyio
async def test_factory_v1_steer_is_durable_and_ack_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _create_db(tmp_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    recovery: list[tuple[TerminalScope, str]] = []

    async def trigger_recovery(scope: TerminalScope, reason: str) -> None:
        recovery.append((scope, reason))

    writer_ready_calls = 0

    async def ensure_writer_ready() -> bool:
        nonlocal writer_ready_calls
        writer_ready_calls += 1
        return True

    factory = TerminalRuntimeFactory(
        store,
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
        recovery_trigger=trigger_recovery,
        writer_ready=ensure_writer_ready,
    )
    request = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="hello",
        turn_id="t_1",
        team_id="team_1",
    )
    digest = await factory.prepare_web_auth_fence(
        team_id="team_1",
        session_id="s_1",
        user_id="u_1",
    )
    request.terminal_auth_fence_vector_digest = digest
    runtime = await factory.admit_web_turn(
        request,
        request_id="req_1",
        presentation_locale="zh-CN",
    )

    kwargs = {
        "team_id": "team_1",
        "session_id": "s_1",
        "user_id": "u_1",
        "expected_turn_id": "t_1",
        "expected_execution_generation": 1,
        "request_id": "steer_1",
        "user_msg_id": "msg_1",
        "user_text": "also compare north",
        "workspace_id": "ws_1",
        "auth_fence_vector_digest": digest,
        "presentation_locale": "zh-CN",
        "attachments": ("artifact_1",),
        "planning_mode": "planning",
    }
    first = await factory.steer_web_turn(**kwargs)
    assert first.execution_started is False
    assert recovery == []
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM turn_events WHERE event_type='emoji_ack'",
        ).fetchone() == (0,)
    await runtime.start_execution()

    async def cancel_materialize(_accepted_through: int) -> None:
        raise asyncio.CancelledError("request disconnected")

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(
            runtime.bus,
            "materialize_accepted_prefix",
            cancel_materialize,
        )
        with pytest.raises(asyncio.CancelledError, match="request disconnected"):
            await factory.steer_web_turn(**kwargs)
    assert recovery == [(runtime.scope, "steer_materialize_cancelled")]
    replay = await factory.steer_web_turn(**kwargs)
    assert writer_ready_calls == 3
    assert first.disposition is AdmissionDisposition.STEERED
    assert replay.disposition is AdmissionDisposition.STEER_REPLAY
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT steer_buffer_revision FROM sessions WHERE id='s_1'").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM turn_event_proposals WHERE event_type='emoji_ack'").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM turn_events WHERE event_type='emoji_ack'").fetchone() == (1,)
    assert [event.event_type for event in runtime.bus._live.snapshot() if event.event_type == "emoji_ack"] == ["emoji_ack"]
    await runtime.bus.emit(OrchestratorEvent("final_answer", "t_1", {"text": "answer"}))
    await runtime.bus.emit(OrchestratorEvent("done", "t_1", {"status": "completed"}))
    assert runtime.bus.receipt is not None
    assert runtime.bus.receipt.selected_session_variant is SessionVariant.DEFERRED
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT active_turn_id,steer_buffer_state,steer_buffer_revision FROM sessions WHERE id='s_1'").fetchone() == (
            None,
            "deferred",
            1,
        )
    await runtime.retire()

    followup_request = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="finally summarize",
        turn_id="t_2",
        team_id="team_1",
    )
    followup_request.terminal_auth_fence_vector_digest = digest
    followup_runtime = await factory.admit_web_turn(
        followup_request,
        request_id="req_2",
        presentation_locale="zh-CN",
    )
    assert followup_request.text == "also compare north\n\nfinally summarize"
    assert followup_request.attachments == ["artifact_1"]
    assert followup_request.planning_mode == "planning"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT user_text FROM turns WHERE id='t_2'").fetchone() == ("also compare north\n\nfinally summarize",)
        envelope = connection.execute("SELECT execution_intent_json FROM turn_generations WHERE turn_id='t_2' AND generation=1").fetchone()
        assert envelope is not None
        parsed = json.loads(envelope[0])
        assert parsed["text"] == followup_request.text
        assert parsed["attachments"] == ["artifact_1"]
        assert parsed["planning_mode"] == "planning"
        assert parsed["deferred_sources"] == [
            {
                "request_digest": first.request_digest,
                "revision": 1,
                "steer_id": "steer_1",
            }
        ]
        assert connection.execute("SELECT steer_buffer_state,steer_buffer_json,steer_buffer_revision FROM sessions WHERE id='s_1'").fetchone() == (
            "collecting",
            "[]",
            1,
        )
    await followup_runtime.abort_worker_start_failed()


@pytest.mark.anyio
async def test_factory_steer_cancelled_after_atomic_admission_triggers_recovery(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    cancel_steer = False

    def inject(point: TerminalProtocolFaultPoint) -> None:
        if cancel_steer and point is TerminalProtocolFaultPoint.ADMISSION_AFTER_COMMIT:
            raise asyncio.CancelledError("steer acknowledgement lost")

    store = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=inject,
    )
    recovery: list[tuple[TerminalScope, str]] = []

    async def trigger_recovery(scope: TerminalScope, reason: str) -> None:
        recovery.append((scope, reason))

    factory = TerminalRuntimeFactory(
        store,
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
        recovery_trigger=trigger_recovery,
    )
    request = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="hello",
        turn_id="t_1",
        team_id="team_1",
    )
    digest = await factory.prepare_web_auth_fence(
        team_id="team_1",
        session_id="s_1",
        user_id="u_1",
    )
    request.terminal_auth_fence_vector_digest = digest
    runtime = await factory.admit_web_turn(
        request,
        request_id="req_1",
        presentation_locale="zh-CN",
    )
    await runtime.start_execution()

    cancel_steer = True
    with pytest.raises(asyncio.CancelledError, match="steer acknowledgement lost"):
        await factory.steer_web_turn(
            team_id="team_1",
            session_id="s_1",
            user_id="u_1",
            expected_turn_id="t_1",
            expected_execution_generation=1,
            request_id="steer_cancelled",
            user_msg_id="msg_cancelled",
            user_text="continue",
            workspace_id="ws_1",
            auth_fence_vector_digest=digest,
            presentation_locale="zh-CN",
        )
    assert recovery == [(runtime.scope, "steer_admission_cancelled")]
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT steer_buffer_revision FROM sessions WHERE id='s_1'",
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM turn_event_proposals WHERE event_type='emoji_ack'",
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM turn_events WHERE event_type='emoji_ack'",
        ).fetchone() == (0,)

    cancel_steer = False
    await runtime.retire()


@pytest.mark.anyio
async def test_factory_start_replay_wake_preserves_cancellation_until_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = TerminalScope("team_1", "s_1", "t_1", 1)
    registry = TerminalRuntimeRegistry()
    recovery: list[str] = []
    recovery_started = asyncio.Event()
    release_recovery = asyncio.Event()

    async def trigger_recovery(_scope: TerminalScope, reason: str) -> None:
        if reason == "web_action_start_replay_runtime_missing":
            recovery_started.set()
            await release_recovery.wait()
        recovery.append(reason)

    factory = TerminalRuntimeFactory(
        object(),
        GenerationEventBusRegistry(),
        registry,
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
        recovery_trigger=trigger_recovery,
    )

    async def cancel_lookup(_scope: TerminalScope):
        raise asyncio.CancelledError("registry lookup cancelled")

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(registry, "get_exact", cancel_lookup)
        with pytest.raises(
            asyncio.CancelledError,
            match="registry lookup cancelled",
        ):
            await factory.wake_admitted_web_turn(scope)
    assert recovery == ["web_action_start_replay_lookup_cancelled"]

    wake = asyncio.create_task(factory.wake_admitted_web_turn(scope))
    await recovery_started.wait()
    wake.cancel("request disconnected")
    await asyncio.sleep(0)
    assert not wake.done()
    release_recovery.set()
    with pytest.raises(asyncio.CancelledError, match="request disconnected"):
        await wake
    assert recovery[-1] == "web_action_start_replay_runtime_missing"


@pytest.mark.anyio
async def test_v1_steer_never_emits_generation_n_ack_on_generation_n_plus_one_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = TerminalScope(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_same",
        generation=1,
    )
    payload = {
        "emoji": "🤝",
        "steer_id": "steer_1",
        "user_msg_id": "msg_1",
    }

    class Store:
        async def admit_or_steer_v1(self, request):
            return TerminalAdmissionResult(
                disposition=AdmissionDisposition.STEERED,
                code=None,
                request_id=request.context.request_id,
                request_digest="steer_digest",
                scope=scope,
                active_turn_id="t_same",
                active_protocol_version=1,
                active_generation=1,
                steer_revision=1,
                steer_ack_proposal=AcceptedEventProposal(
                    scope=scope,
                    proposal_id="tep_1",
                    event_id="evt_1",
                    accept_ordinal=1,
                    accepted_claim_epoch=1,
                    event_type="emoji_ack",
                    actor_scope=ActorScope.ROOT,
                    phase_scope=PhaseScope.IN_GENERATION,
                    payload_json=json.dumps(
                        payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    payload_digest=canonical_digest_v1(payload),
                    root_durability=RootDurability.COMMITTED,
                    publish_mode=EventPublishMode.BEFORE_LIVE,
                    accepted_at=_NOW.isoformat(),
                ),
                execution_started=True,
            )

    class Bus:
        calls = 0

        async def materialize_accepted_prefix(self, _accepted_through: int) -> None:
            self.calls += 1

    registry = TerminalRuntimeRegistry()
    generation_2 = SimpleNamespace(
        scope=TerminalScope(
            team_id="team_1",
            session_id="s_1",
            turn_id="t_same",
            generation=2,
        ),
        bus=Bus(),
        retired=False,
    )
    registry._runtimes["t_same"] = generation_2
    recovery: list[tuple[TerminalScope, str]] = []

    async def trigger_recovery(
        recovered_scope: TerminalScope,
        reason: str,
    ) -> None:
        recovery.append((recovered_scope, reason))

    factory = TerminalRuntimeFactory(
        Store(),
        GenerationEventBusRegistry(),
        registry,
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
        recovery_trigger=trigger_recovery,
    )

    admitted = await factory.steer_web_turn(
        team_id="team_1",
        session_id="s_1",
        user_id="u_1",
        expected_turn_id="t_same",
        expected_execution_generation=1,
        request_id="steer_1",
        user_msg_id="msg_1",
        user_text="next",
        workspace_id="ws_1",
        auth_fence_vector_digest="auth_1",
        presentation_locale="zh-CN",
    )

    assert admitted.disposition is AdmissionDisposition.STEERED
    assert generation_2.bus.calls == 0
    assert recovery == [(scope, "steer_materialize_runtime_missing")]

    async def cancel_runtime_lookup(_scope: TerminalScope):
        raise asyncio.CancelledError("runtime lookup cancelled")

    monkeypatch.setattr(registry, "get_exact", cancel_runtime_lookup)
    with pytest.raises(asyncio.CancelledError, match="runtime lookup cancelled"):
        await factory.steer_web_turn(
            team_id="team_1",
            session_id="s_1",
            user_id="u_1",
            expected_turn_id="t_same",
            expected_execution_generation=1,
            request_id="steer_1",
            user_msg_id="msg_1",
            user_text="next",
            workspace_id="ws_1",
            auth_fence_vector_digest="auth_1",
            presentation_locale="zh-CN",
        )
    assert recovery[-1] == (scope, "steer_materialize_cancelled")


@pytest.mark.anyio
async def test_factory_rejects_revoke_between_preflight_and_admission(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    runtimes = TerminalRuntimeRegistry()
    factory = TerminalRuntimeFactory(
        store,
        GenerationEventBusRegistry(),
        runtimes,
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
    )
    request = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="must not start",
        turn_id="t_revoked",
        team_id="team_1",
    )
    request.terminal_auth_fence_vector_digest = await factory.prepare_web_auth_fence(
        team_id="team_1",
        session_id="s_1",
        user_id="u_1",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE terminal_auth_scope_epochs
            SET epoch=epoch+1,row_version=row_version+1,updated_at='revoked'
            WHERE scope_kind='team_membership'
              AND team_id='team_1' AND scope_ref='team_1'
            """
        )
        connection.commit()

    with pytest.raises(
        TerminalRuntimeAdmissionError,
        match="terminal_auth_fence_changed",
    ):
        await factory.admit_web_turn(
            request,
            request_id="req_revoked",
            presentation_locale="zh-CN",
        )

    assert runtimes.size == 0
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turns WHERE id='t_revoked'").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM turn_generations WHERE turn_id='t_revoked'").fetchone() == (0,)


@pytest.mark.anyio
async def test_idle_session_delete_rejects_an_open_v1_turn(tmp_path: Path) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path)
    lifecycle = SessionLifecycleCoordinator(SessionStore(db_path), runtime.store)
    with pytest.raises(SessionLifecycleError, match="session_active_turn"):
        await lifecycle.soft_delete(
            session_id="s_1",
            actor_audit_ref="u_1",
            request_id="sidebar-delete-busy",
            expected_user_id="u_1",
            require_idle=True,
        )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT deleted_at,active_turn_id FROM sessions WHERE id='s_1'").fetchone() == (None, "t_1")
        assert connection.execute("SELECT COUNT(*) FROM session_lifecycle_operations").fetchone() == (0,)


@pytest.mark.anyio
async def test_idle_v1_session_delete_and_undo_preserve_the_committed_turn(tmp_path: Path) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path)
    await runtime.bus.emit(OrchestratorEvent("final_answer", "t_1", {"text": "Complete"}))
    await runtime.bus.emit(OrchestratorEvent("done", "t_1", {"status": "completed", "intent": "lookup"}))
    lifecycle = SessionLifecycleCoordinator(SessionStore(db_path), runtime.store)
    deleted = await lifecycle.soft_delete(
        session_id="s_1",
        actor_audit_ref="u_1",
        request_id="sidebar-delete-idle",
        expected_user_id="u_1",
        require_idle=True,
    )
    revived = await lifecycle.revive(
        session_id="s_1",
        actor_audit_ref="u_1",
        request_id="sidebar-undo-idle",
        expected_user_id="u_1",
    )
    assert deleted.changed and revived.changed
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT deleted_at,active_turn_id FROM sessions WHERE id='s_1'").fetchone() == (None, None)
        assert connection.execute("SELECT state FROM turn_generations WHERE turn_id='t_1'").fetchone() == ("closed",)
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_receipts WHERE turn_id='t_1'").fetchone() == (1,)
        assert connection.execute("SELECT final_answer,delivery_fence_epoch FROM turns WHERE id='t_1'").fetchone() == ("Complete", 2)


@pytest.mark.anyio
async def test_idle_delete_retry_reuses_visibility_identity_and_later_deletion_gets_a_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path)
    await runtime.bus.emit(OrchestratorEvent("final_answer", "t_1", {"text": "Complete"}))
    await runtime.bus.emit(OrchestratorEvent("done", "t_1", {"status": "completed", "intent": "lookup"}))
    lifecycle = SessionLifecycleCoordinator(SessionStore(db_path), runtime.store)
    identity = {"session_id": "s_1", "user_id": "u_1", "action": "soft_delete"}
    first_id = await lifecycle.visibility_request_id(**identity)
    commit = lifecycle._commit_operation

    async def fail_commit(*_args, **_kwargs):
        raise RuntimeError("Temporary commit failure")

    monkeypatch.setattr(lifecycle, "_commit_operation", fail_commit)
    mutation = {"session_id": "s_1", "actor_audit_ref": "u_1", "expected_user_id": "u_1", "require_idle": True}
    with pytest.raises(RuntimeError, match="Temporary commit failure"):
        await lifecycle.soft_delete(**mutation, request_id=first_id)
    retry_id = await lifecycle.visibility_request_id(**identity)
    assert retry_id == first_id
    monkeypatch.setattr(lifecycle, "_commit_operation", commit)
    assert (await lifecycle.soft_delete(**mutation, request_id=retry_id)).changed
    await lifecycle.revive(session_id="s_1", actor_audit_ref="u_1", expected_user_id="u_1", request_id="undo-retried-delete")
    next_id = await lifecycle.visibility_request_id(**identity)
    assert next_id != first_id
    assert (await lifecycle.soft_delete(**mutation, request_id=next_id)).changed
    assert (await SessionStore(db_path).get_session("s_1")).deleted_at is not None


@pytest.mark.anyio
async def test_v1_session_lifecycle_fences_open_generation_and_replays(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path)
    await runtime.store.ensure_auth_scope_epochs(
        terminal_auth_resolve_context(
            TurnRequest(
                session_id="s_1",
                workspace_id="ws_1",
                user_id="u_1",
                member_uid="m_1",
                text="hello",
                turn_id="t_1",
                team_id="team_1",
            )
        )
    )
    lifecycle = SessionLifecycleCoordinator(SessionStore(db_path), runtime.store)

    deleted = await lifecycle.soft_delete(
        session_id="s_1",
        actor_audit_ref="u_1",
        request_id="canvas-delete-1",
        expected_user_id="u_1",
        authz_snapshot_digest="auth_1",
    )
    replay = await lifecycle.soft_delete(
        session_id="s_1",
        actor_audit_ref="u_1",
        request_id="canvas-delete-1",
        expected_user_id="u_1",
        authz_snapshot_digest="auth_1",
    )

    assert deleted.changed is True
    assert replay == deleted
    with sqlite3.connect(db_path) as connection:
        session = connection.execute("SELECT deleted_at,active_turn_id FROM sessions WHERE id='s_1'").fetchone()
        generation = connection.execute("SELECT state,recovery_resolution FROM turn_generations WHERE turn_id='t_1'").fetchone()
        operation = connection.execute("SELECT state,result_code FROM session_lifecycle_operations").fetchone()
        deletion_count = connection.execute("SELECT COUNT(*) FROM turn_terminal_deletion_ledgers WHERE turn_id='t_1'").fetchone()[0]
    assert session is not None and session[0] is not None and session[1] is None
    assert generation == ("closed", "fenced_deleted")
    assert operation == ("committed", "deleted")
    assert deletion_count == 1


def test_session_lifecycle_internal_error_inventory_covers_every_raise() -> None:
    tree = ast.parse(inspect.getsource(session_lifecycle_module))
    emitted = frozenset(
        str(node.args[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "SessionLifecycleError"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    )

    assert emitted == SESSION_LIFECYCLE_ERROR_CODES


@pytest.mark.anyio
async def test_legacy_session_lifecycle_primitives_reject_migrated_schema(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    sessions = SessionStore(db_path)

    with pytest.raises(SessionProtocolManagedError):
        await sessions.soft_delete_session("s_1")
    with pytest.raises(SessionProtocolManagedError):
        await sessions.revive_session("s_1", user_id="u_1")
    with pytest.raises(SessionProtocolManagedError):
        await sessions.delete_session(session_id="s_1", user_id="u_1")
    with pytest.raises(SessionProtocolManagedError):
        await sessions.undelete_session(session_id="s_1", user_id="u_1")

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT deleted_at FROM sessions WHERE id='s_1'").fetchone() == (None,)


@pytest.mark.anyio
async def test_partial_terminal_schema_aborts_legacy_startup_and_writes(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "partial.db"
    sessions = SessionStore(db_path)
    await sessions.initialize()
    await sessions.ensure_session(
        session_id="s_partial",
        workspace_id="ws_1",
        user_id="u_1",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO turns(id,session_id,seq,user_text,status,started_at)
            VALUES ('t_partial','s_partial',1,'hello','running','2000-01-01T00:00:00Z')
            """
        )
        connection.execute("UPDATE sessions SET active_turn_id='t_partial' WHERE id='s_partial'")
        connection.execute("CREATE TABLE turn_generations(sentinel TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO turn_generations(sentinel) VALUES ('v1-footprint')")
        connection.commit()

    with pytest.raises(SessionProtocolManagedError):
        await sessions.initialize()
    with pytest.raises(SessionProtocolManagedError):
        await sessions.cleanup_stale_running_turns(older_than_min=0)
    with pytest.raises(SessionProtocolManagedError):
        await sessions.soft_delete_session("s_partial")
    with pytest.raises(SessionProtocolManagedError):
        await sessions.revive_session("s_partial", user_id="u_1")
    with pytest.raises(SessionProtocolManagedError):
        await sessions.delete_session(session_id="s_partial", user_id="u_1")
    with pytest.raises(SessionProtocolManagedError):
        await sessions.undelete_session(session_id="s_partial", user_id="u_1")
    with pytest.raises(SessionProtocolManagedError):
        await sessions.get_active_turn_identity("s_partial")

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT status FROM turns WHERE id='t_partial'").fetchone() == ("running",)
        assert connection.execute("SELECT active_turn_id,deleted_at FROM sessions WHERE id='s_partial'").fetchone() == ("t_partial", None)
        assert connection.execute("SELECT sentinel FROM turn_generations").fetchone() == ("v1-footprint",)


@pytest.mark.anyio
async def test_session_delete_accepts_exact_generation_finalized_after_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path)
    lifecycle = SessionLifecycleCoordinator(SessionStore(db_path), runtime.store)
    commit_protocol_delete = runtime.store.commit_protocol_delete

    async def finalize_before_protocol_delete(context):
        await runtime.bus.emit(OrchestratorEvent("final_answer", "t_1", {"text": "done"}))
        await runtime.bus.emit(
            OrchestratorEvent(
                "done",
                "t_1",
                {"status": "completed", "intent": "lookup"},
            )
        )
        return await commit_protocol_delete(context)

    monkeypatch.setattr(runtime.store, "commit_protocol_delete", finalize_before_protocol_delete)

    deleted = await lifecycle.soft_delete(
        session_id="s_1",
        actor_audit_ref="u_1",
        request_id="delete-after-terminal-commit",
        expected_user_id="u_1",
    )

    assert deleted.result_code == "deleted"
    with sqlite3.connect(db_path) as connection:
        session = connection.execute("SELECT deleted_at,active_turn_id FROM sessions WHERE id='s_1'").fetchone()
        generation = connection.execute("SELECT state,terminal_receipt_id FROM turn_generations WHERE turn_id='t_1'").fetchone()
        receipt_count = connection.execute("SELECT COUNT(*) FROM turn_terminal_receipts WHERE turn_id='t_1' AND generation=1").fetchone()[0]
        deletion_count = connection.execute("SELECT COUNT(*) FROM turn_terminal_deletion_ledgers WHERE turn_id='t_1'").fetchone()[0]
        operation = connection.execute("SELECT state,result_code FROM session_lifecycle_operations").fetchone()
    assert session is not None and session[0] is not None and session[1] is None
    assert generation is not None and generation[0] == "closed" and generation[1]
    assert receipt_count == 1
    assert deletion_count == 0
    assert operation == ("committed", "deleted")


@pytest.mark.anyio
async def test_session_delete_projects_unsafe_protocol_delete_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path)
    lifecycle = SessionLifecycleCoordinator(SessionStore(db_path), runtime.store)

    async def reject_protocol_delete(_context: object) -> None:
        raise TerminalProtocolMalformedError("secret fence detail")

    monkeypatch.setattr(runtime.store, "commit_protocol_delete", reject_protocol_delete)

    with pytest.raises(SessionLifecycleError, match="terminal_protocol_malformed"):
        await lifecycle.soft_delete(
            session_id="s_1",
            actor_audit_ref="u_1",
            request_id="delete-unsafe-fence",
            expected_user_id="u_1",
        )

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT deleted_at FROM sessions WHERE id='s_1'").fetchone() == (None,)
        assert connection.execute("SELECT state FROM session_lifecycle_operations").fetchone() == ("prepared",)
        assert connection.execute("SELECT state,terminal_receipt_id FROM turn_generations WHERE turn_id='t_1'").fetchone() == ("running", None)


@pytest.mark.anyio
async def test_prepared_lifecycle_fences_new_admission_until_delete_commits(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path)
    request = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="hello",
        turn_id="t_1",
        team_id="team_1",
    )
    vector = await runtime.store.ensure_auth_scope_epochs(terminal_auth_resolve_context(request))
    lifecycle = SessionLifecycleCoordinator(SessionStore(db_path), runtime.store)
    await lifecycle._prepare(
        session_id="s_1",
        actor_audit_ref="u_1",
        request_id="delete-race-1",
        action="soft_delete",
    )

    raced = await runtime.store.admit_or_steer_v1(
        TerminalAdmissionRequest(
            context=AdmissionContext(
                team_id="team_1",
                session_id="s_1",
                request_id="req_raced",
                surface="web",
                cohort="all;config=rollout_1",
                actor="u_1",
                writer_instance_id="gateway_1",
                writer_lease_epoch=7,
                writer_admission_epoch=3,
                rollout_config_generation="rollout_1",
                auth_fence_vector_digest=vector.auth_fence_vector_digest,
            ),
            turn_id="t_raced",
            user_text="must not start",
            execution_intent={"text": "must not start"},
            presentation_locale="zh-CN",
            message_catalog_version=1,
            allow_steer=False,
        )
    )
    deleted = await lifecycle.soft_delete(
        session_id="s_1",
        actor_audit_ref="u_1",
        request_id="delete-race-1",
        expected_user_id="u_1",
    )

    assert raced.disposition is AdmissionDisposition.REJECTED
    assert raced.code is AdmissionCode.SESSION_TRANSITIONING
    assert deleted.result_code == "deleted"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turns WHERE id='t_raced'").fetchone() == (0,)


@pytest.mark.anyio
async def test_opposite_lifecycle_action_conflicts_with_prepared_operation(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(_create_db(tmp_path))
    lifecycle = SessionLifecycleCoordinator(
        SessionStore(runtime.store.db_path),
        runtime.store,
    )
    await lifecycle._prepare(
        session_id="s_1",
        actor_audit_ref="u_1",
        request_id="delete_req",
        action="soft_delete",
    )

    with pytest.raises(SessionLifecycleError, match="session_lifecycle_conflict"):
        await lifecycle.revive(
            session_id="s_1",
            actor_audit_ref="u_1",
            request_id="revive_req",
            expected_user_id="u_1",
        )

    with sqlite3.connect(runtime.store.db_path) as connection:
        assert connection.execute("SELECT action,state FROM session_lifecycle_operations").fetchall() == [("soft_delete", "prepared")]


@pytest.mark.anyio
async def test_v1_linked_canvas_and_session_delete_commit_atomically_and_replay(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path)
    request = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="hello",
        turn_id="t_1",
        team_id="team_1",
    )
    await runtime.store.ensure_auth_scope_epochs(terminal_auth_resolve_context(request))
    canvases = CanvasStore(db_path)
    await canvases.initialize()
    canvas = await canvases.ensure_canvas(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
    )
    lifecycle = SessionLifecycleCoordinator(
        SessionStore(db_path),
        runtime.store,
        canvases,
    )

    result = await lifecycle.soft_delete(
        session_id="s_1",
        actor_audit_ref="u_1",
        request_id=f"canvas-delete:{canvas.id}",
        expected_user_id="u_1",
        linked_canvas_id=canvas.id,
    )
    replay = await lifecycle.soft_delete(
        session_id="s_1",
        actor_audit_ref="u_1",
        request_id=f"canvas-delete:{canvas.id}",
        expected_user_id="u_1",
        linked_canvas_id=canvas.id,
    )

    assert result.changed is True
    assert replay == result
    assert await canvases.get_canvas(canvas.id) is None
    assert await lifecycle.canvas_delete_committed(
        canvas_id=canvas.id,
        actor_audit_ref="u_1",
    )
    with sqlite3.connect(db_path) as connection:
        session = connection.execute("SELECT deleted_at FROM sessions WHERE id='s_1'").fetchone()
        operation = connection.execute("SELECT state,result_code FROM session_lifecycle_operations").fetchone()
    assert session is not None and session[0] is not None
    assert operation == ("committed", "deleted")


@pytest.mark.anyio
async def test_v1_session_lifecycle_revive_bumps_visibility_epoch(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path)
    request = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="hello",
        turn_id="t_1",
        team_id="team_1",
    )
    await runtime.store.ensure_auth_scope_epochs(terminal_auth_resolve_context(request))
    lifecycle = SessionLifecycleCoordinator(SessionStore(db_path), runtime.store)
    await lifecycle.soft_delete(
        session_id="s_1",
        actor_audit_ref="u_1",
        request_id="canvas-delete-2",
        expected_user_id="u_1",
    )

    revived = await lifecycle.revive(
        session_id="s_1",
        actor_audit_ref="u_1",
        request_id="feishu-message-2",
        expected_user_id="u_1",
    )
    replay = await lifecycle.revive(
        session_id="s_1",
        actor_audit_ref="u_1",
        request_id="feishu-message-2",
        expected_user_id="u_1",
    )

    assert revived.changed is True
    assert replay == revived
    with sqlite3.connect(db_path) as connection:
        deleted_at = connection.execute("SELECT deleted_at FROM sessions WHERE id='s_1'").fetchone()[0]
        epoch = connection.execute(
            """
            SELECT epoch FROM terminal_auth_scope_epochs
            WHERE scope_kind='session_visibility' AND team_id='team_1' AND scope_ref='s_1'
            """
        ).fetchone()[0]
    assert deleted_at is None
    assert epoch == 3


@pytest.mark.anyio
@pytest.mark.parametrize("action", ["soft_delete", "revive"])
async def test_session_lifecycle_rejects_future_protocol_without_mutation(
    tmp_path: Path,
    action: str,
) -> None:
    db_path = _create_db(tmp_path)
    with sqlite3.connect(db_path) as connection:
        if action == "revive":
            connection.execute("UPDATE sessions SET deleted_at='2026-08-01T00:00:00Z' WHERE id='s_1'")
        connection.execute(
            """
            INSERT INTO turns(
              id,session_id,seq,user_text,status,started_at,
              terminal_protocol_version
            ) VALUES ('t_future','s_1',1,'future','running','t',2)
            """
        )
        connection.commit()
    lifecycle = SessionLifecycleCoordinator(
        SessionStore(db_path),
        TerminalProtocolStore(db_path),
    )

    with pytest.raises(
        SessionLifecycleError,
        match="terminal_protocol_future_version",
    ):
        if action == "soft_delete":
            await lifecycle.soft_delete(
                session_id="s_1",
                actor_audit_ref="u_1",
                request_id="future-delete",
                expected_user_id="u_1",
            )
        else:
            await lifecycle.revive(
                session_id="s_1",
                actor_audit_ref="u_1",
                request_id="future-revive",
                expected_user_id="u_1",
            )

    with sqlite3.connect(db_path) as connection:
        deleted_at = connection.execute("SELECT deleted_at FROM sessions WHERE id='s_1'").fetchone()[0]
        operations = connection.execute("SELECT COUNT(*) FROM session_lifecycle_operations").fetchone()[0]
    assert (deleted_at is not None) is (action == "revive")
    assert operations == 0


@pytest.mark.anyio
async def test_v1_session_lifecycle_rejects_active_legacy_turn_after_rollback(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path)
    await runtime.bus.emit(
        OrchestratorEvent(
            "done",
            "t_1",
            {"status": "completed", "final_answer": "v1 complete"},
        )
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO turns(
              id,session_id,seq,user_text,status,started_at,
              terminal_protocol_version
            ) VALUES ('t_legacy','s_1',2,'legacy','running','t',NULL)
            """
        )
        connection.execute(
            """
            UPDATE sessions
            SET active_turn_id='t_legacy',active_turn_protocol_version=NULL,
                active_turn_generation=NULL,active_turn_claim_epoch=NULL
            WHERE id='s_1'
            """
        )
        connection.commit()
    lifecycle = SessionLifecycleCoordinator(SessionStore(db_path), runtime.store)

    with pytest.raises(SessionLifecycleError, match="terminal_protocol_malformed"):
        await lifecycle.soft_delete(
            session_id="s_1",
            actor_audit_ref="u_1",
            request_id="mixed-delete",
            expected_user_id="u_1",
        )

    with sqlite3.connect(db_path) as connection:
        session = connection.execute("SELECT deleted_at,active_turn_id FROM sessions WHERE id='s_1'").fetchone()
        legacy = connection.execute("SELECT status,terminal_protocol_version FROM turns WHERE id='t_legacy'").fetchone()
        operations = connection.execute("SELECT COUNT(*) FROM session_lifecycle_operations").fetchone()[0]
    assert session == (None, "t_legacy")
    assert legacy == ("running", None)
    assert operations == 0


@pytest.mark.anyio
async def test_auth_mutations_bump_terminal_epochs_in_same_store_transaction(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO users(id,team_id,email,status,created_at)
            VALUES ('u_1','team_1','u1@example.com','active','t')
            """
        )
        connection.commit()

    access = AccessControlStore(db_path, terminal_auth_fence_enabled=True)
    await access.initialize()
    await access.bump_policy_epoch("team_1")
    users = UserStore(db_path, terminal_auth_fence_enabled=True)
    from bowhead_core.account_deletion_ledger import AccountDeletionLedger

    ledger = AccountDeletionLedger(tmp_path / "account-retirements.db", secret="synthetic-test-root")
    await ledger.initialize()
    users.account_deletion_recorder = ledger.record
    await users.soft_delete_user("u_1")

    with sqlite3.connect(db_path) as connection:
        resource_epoch = connection.execute(
            """
            SELECT epoch FROM terminal_auth_scope_epochs
            WHERE scope_kind='resource_grant' AND team_id='team_1' AND scope_ref='team_1'
            """
        ).fetchone()[0]
        account_epoch = connection.execute(
            """
            SELECT epoch FROM terminal_auth_scope_epochs
            WHERE scope_kind='user_account' AND team_id='__global__' AND scope_ref='u_1'
            """
        ).fetchone()[0]
    assert resource_epoch == 1
    assert account_epoch == 1


@pytest.mark.anyio
async def test_password_and_logout_everywhere_advance_account_fence(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO users(id,team_id,email,status,created_at)
            VALUES ('u_1','team_1','u1@example.com','active','t')
            """
        )
        connection.commit()
    users = UserStore(db_path, terminal_auth_fence_enabled=True)

    assert await users.update_user_password("u_1", "hash_1")
    assert await users.revoke_all_refresh_for_user("u_1") == 0

    with sqlite3.connect(db_path) as connection:
        epoch = connection.execute(
            """
            SELECT epoch FROM terminal_auth_scope_epochs
            WHERE scope_kind='user_account'
              AND team_id='__global__' AND scope_ref='u_1'
            """
        ).fetchone()
    assert epoch == (2,)
