from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core.approval_store import ApprovalStore
from bowhead_core.clarification_store import ClarificationStore
from bowhead_core.events import OrchestratorEvent
from bowhead_core.plan_store import PlanStatus, PlanStore
from bowhead_core.session_store import SessionStore as DurableSessionStore
from bowhead_core.terminal_finalizer import FinalizeRequest, TerminalFinalizer
from bowhead_core.terminal_generation_bus import GenerationEventBusRegistry
from bowhead_core.terminal_protocol import (
    CandidateOrigin,
    CandidateOutcome,
    ControlGenerationRequest,
    DecisionAction,
    DecisionContext,
    DecisionPrincipalSet,
    DecisionRecordRequest,
    DecisionSideEffectPlan,
    DecisionSourceKind,
    DecisionState,
    EffectClass,
    EffectSettlementContext,
    EffectSettlementOutcome,
    EffectSettlementSource,
    EffectState,
    FinalAnswerMode,
    GenerationClaimContext,
    ResumeGenerationRequest,
    SessionVariant,
    TerminalCandidatePlan,
    TerminalCandidateVariant,
    TerminalScope,
    TerminalStatus,
    TerminalWriterLease,
    canonical_decision_payload_digest_for,
    canonical_digest_v1,
    canonical_json_v1,
    decision_identity_digest,
    stable_approval_effect_action_ref,
    stable_decision_id,
)
from bowhead_core.terminal_protocol_store import (
    AdmissionCode,
    DecisionAccessRevokedError,
    DecisionAuthorizationDeniedError,
    DecisionConflictError,
    GenerationClaimDisposition,
    TerminalProtocolFaultPoint,
    TerminalProtocolStore,
    TerminalReadContext,
)
from bowhead_core.terminal_reconciler import TerminalRecoveryProcessor
from bowhead_core.terminal_runtime_factory import (
    TerminalPreparedDecision,
    TerminalRuntimeAdmissionError,
    TerminalRuntimeFactory,
    TerminalRuntimeRegistry,
    TerminalWriterSnapshot,
    terminal_auth_resolve_context_values,
)
from bowhead_core.types import TurnRequest
from bowhead_gateway.terminal_approval_runner import (
    TerminalApprovalRecovery,
    TerminalApprovalRunner,
)
from bowhead_gateway.terminal_decision_authorization import (
    TerminalDecisionAuthorizer,
    TerminalPendingControlAuthorization,
)
from bowhead_gateway.terminal_decision_effects import (
    TerminalClarificationEffectRecovery,
)
from test_terminal_finalizer import (
    _AUTH_DIGEST,
    _NOW,
    _create_db,
    _done,
    _start_execution,
)
from test_terminal_protocol_store import _admission_request


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return _create_db(tmp_path)


def _bump_terminal_auth_epoch(db_path: str, *, marker: str) -> None:
    with sqlite3.connect(db_path) as connection:
        update = connection.execute(
            """
            UPDATE terminal_auth_scope_epochs
            SET epoch=epoch+1,row_version=row_version+1,updated_at=?
            WHERE scope_kind='resource_grant'
              AND team_id='team_1' AND scope_ref='team_1'
            """,
            (marker,),
        )
        assert update.rowcount == 1
        connection.commit()


def _pause_plan(scope) -> TerminalCandidatePlan:
    candidate_id = "tc_pause_1"
    receipt_id = "tr_pause_1"
    done_id = "evt_done_pause_1"
    primary = TerminalCandidateVariant(
        variant_key="primary_none",
        outcome_variant=CandidateOutcome.PRIMARY,
        session_variant=SessionVariant.NONE,
        status=TerminalStatus.CLARIFICATION_PENDING,
        reason_code=None,
        message_key="turn.clarification_pending",
        final_answer_mode=FinalAnswerMode.NONE,
        final_answer_event_id=None,
        final_answer_digest=None,
        session_mutation_template={},
        events=(
            _done(
                event_id=done_id,
                candidate_id=candidate_id,
                receipt_id=receipt_id,
                variant_key="primary_none",
                status=TerminalStatus.CLARIFICATION_PENDING,
                reason=None,
                message_key="turn.clarification_pending",
                manifest_digest=canonical_digest_v1([]),
            ),
        ),
    )
    recovery = TerminalCandidateVariant(
        variant_key="recovery_none",
        outcome_variant=CandidateOutcome.RECOVERY_INTERRUPT,
        session_variant=SessionVariant.NONE,
        status=TerminalStatus.ABORTED,
        reason_code="terminal_projection_not_committed",
        message_key="turn.result_not_committed",
        final_answer_mode=FinalAnswerMode.NONE,
        final_answer_event_id=None,
        final_answer_digest=None,
        session_mutation_template={},
        events=(
            _done(
                event_id=done_id,
                candidate_id=candidate_id,
                receipt_id=receipt_id,
                variant_key="recovery_none",
                status=TerminalStatus.ABORTED,
                reason="terminal_projection_not_committed",
                message_key="turn.result_not_committed",
                manifest_digest=canonical_digest_v1([]),
            ),
        ),
    )
    return TerminalCandidatePlan(
        scope=scope,
        candidate_id=candidate_id,
        reserved_receipt_id=receipt_id,
        reserved_terminal_event_id=done_id,
        origin=CandidateOrigin.NORMAL,
        closing_accepted_through=0,
        persisted_event_highwater=0,
        variants=(primary, recovery),
        session_disposition_plan={
            "turn_projection": {"intent": "clarification"},
            "variant_templates": {"primary_none": {}, "recovery_none": {}},
        },
    )


def _approval_pause_plan(scope) -> TerminalCandidatePlan:
    plan = _pause_plan(scope)
    primary = replace(
        plan.variants[0],
        status=TerminalStatus.APPROVAL_PENDING,
        message_key="turn.approval_pending",
        events=(
            _done(
                event_id=plan.reserved_terminal_event_id,
                candidate_id=plan.candidate_id,
                receipt_id=plan.reserved_receipt_id,
                variant_key="primary_none",
                status=TerminalStatus.APPROVAL_PENDING,
                reason=None,
                message_key="turn.approval_pending",
                manifest_digest=canonical_digest_v1([]),
            ),
        ),
    )
    return replace(plan, variants=(primary, plan.variants[1]))


def _resume_request(decision) -> ResumeGenerationRequest:
    return ResumeGenerationRequest(
        decision=decision,
        execution_intent_id="resume_intent_fault",
        execution_intent_digest=canonical_digest_v1({"decision": "clarify_1"}),
        execution_intent={"decision": "clarify_1"},
        expected_generation_transition_epoch=1,
        expected_session_claim_epoch=0,
        writer_instance_id="gateway_1",
        writer_lease_epoch=7,
        current_authz_snapshot_digest=_AUTH_DIGEST,
    )


async def _pause_with_clarification(db_path: str) -> DecisionRecordRequest:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(store)

    def candidate(snapshot):
        return replace(
            _pause_plan(execution.scope),
            closing_accepted_through=snapshot.flush.closing_accepted_through,
            persisted_event_highwater=snapshot.flush.persisted_event_highwater,
        )

    await TerminalFinalizer(store).finalize(
        FinalizeRequest(
            execution=execution,
            finalization_owner_instance_id="gateway_1:finalizer",
            closing_reason="clarification_pending",
            selected_variant_key="primary_none",
            authz_snapshot_digest=_AUTH_DIGEST,
            writer_admission_epoch=3,
            candidate_builder=candidate,
            producer_barrier=None,
        )
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO clarification_requests(
              request_id,turn_id,user_id,workspace_id,token,rationale,candidates_json,
              status,created_at,kind,multi_select
            ) VALUES ('clarify_1','t_1','u_1','ws_1','metric','why','[]','pending','t','options',0)
            """
        )
        connection.commit()
    scope = execution.scope
    context = DecisionContext(
        scope=scope,
        decision_id=stable_decision_id(
            scope=scope,
            source_kind=DecisionSourceKind.CLARIFICATION,
            source_id="clarify_1",
            action=DecisionAction.RESUME,
        ),
        source_kind=DecisionSourceKind.CLARIFICATION,
        source_id="clarify_1",
        action=DecisionAction.RESUME,
        principals=DecisionPrincipalSet("u_1", "u_1", "u_1"),
        authz_snapshot_digest=_AUTH_DIGEST,
        prior_receipt_id="tr_pause_1",
    )
    return DecisionRecordRequest(
        context=context,
        decision_payload_digest=canonical_decision_payload_digest_for(
            source_kind=DecisionSourceKind.CLARIFICATION,
            action=DecisionAction.RESUME,
            expected_source_state="pending",
            target_source_state="resolved",
            source_values_json='{"chosen_value":"north"}',
        ),
        expected_source_state="pending",
        target_source_state="resolved",
        source_values_json='{"chosen_value":"north"}',
    )


async def _pause_with_approval(db_path: str):
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(store)

    def candidate(snapshot):
        return replace(
            _approval_pause_plan(execution.scope),
            closing_accepted_through=snapshot.flush.closing_accepted_through,
            persisted_event_highwater=snapshot.flush.persisted_event_highwater,
        )

    await TerminalFinalizer(store).finalize(
        FinalizeRequest(
            execution=execution,
            finalization_owner_instance_id="gateway_1:finalizer",
            closing_reason="approval_pending",
            selected_variant_key="primary_none",
            authz_snapshot_digest=_AUTH_DIGEST,
            writer_admission_epoch=3,
            candidate_builder=candidate,
            producer_barrier=None,
        )
    )
    approvals = ApprovalStore(db_path)
    await approvals.initialize()
    approval = await approvals.enqueue(
        turn_id="t_1",
        session_id="s_1",
        workspace_id="ws_1",
        member_uid="m_approver",
        user_id="u_1",
        tool_name="fake_write",
        arguments={"value": 7},
        reason="test",
        risk_level="high",
    )
    return approvals, approval


@pytest.mark.asyncio
async def test_s5_decision_source_and_protocol_row_are_atomic_and_replayable(db_path: str) -> None:
    request = await _pause_with_clarification(db_path)

    def inject(point: TerminalProtocolFaultPoint) -> None:
        if point is TerminalProtocolFaultPoint.DECISION_AFTER_SOURCE_CAS_BEFORE_INSERT:
            raise RuntimeError("decision fault")

    with pytest.raises(RuntimeError, match="decision fault"):
        await TerminalProtocolStore(
            db_path,
            _clock=lambda: _NOW,
            _fault_injector=inject,
        ).record_decision(request)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT status,chosen_value FROM clarification_requests WHERE request_id='clarify_1'").fetchone() == (
            "pending",
            None,
        )
        assert connection.execute("SELECT COUNT(*) FROM turn_control_decisions").fetchone() == (0,)

    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    first = await store.record_decision(request)
    replay = await store.record_decision(request)
    assert first.state is DecisionState.DECISION_RECORDED
    assert replay.replayed is True
    assert replay.decision_digest == decision_identity_digest(request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("point", "committed"),
    [
        (TerminalProtocolFaultPoint.DECISION_BEFORE_SOURCE_CAS, False),
        (
            TerminalProtocolFaultPoint.DECISION_AFTER_SOURCE_CAS_BEFORE_INSERT,
            False,
        ),
        (TerminalProtocolFaultPoint.DECISION_AFTER_COMMIT_BEFORE_ACK, True),
    ],
)
async def test_s5_decision_fault_windows_are_atomic_or_replayable(
    db_path: str,
    point: TerminalProtocolFaultPoint,
    committed: bool,
) -> None:
    request = await _pause_with_clarification(db_path)

    def inject(observed: TerminalProtocolFaultPoint) -> None:
        if observed is point:
            raise RuntimeError(point.value)

    with pytest.raises(RuntimeError, match=point.value):
        await TerminalProtocolStore(
            db_path,
            _clock=lambda: _NOW,
            _fault_injector=inject,
        ).record_decision(request)

    with sqlite3.connect(db_path) as connection:
        status = connection.execute("SELECT status FROM clarification_requests WHERE request_id='clarify_1'").fetchone()[0]
        decision_count = connection.execute("SELECT COUNT(*) FROM turn_control_decisions").fetchone()[0]
    assert (status, decision_count) == (("resolved", 1) if committed else ("pending", 0))
    replay = await TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
    ).record_decision(request)
    assert replay.replayed is committed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("point", "committed"),
    [
        (TerminalProtocolFaultPoint.RESUME_BEFORE_EVENT_ALLOC, False),
        (
            TerminalProtocolFaultPoint.RESUME_AFTER_EVENT_BEFORE_POINTER,
            False,
        ),
        (TerminalProtocolFaultPoint.RESUME_AFTER_OPEN_BEFORE_CLAIM, True),
    ],
)
async def test_s5_resume_fault_windows_are_atomic_or_replayable(
    db_path: str,
    point: TerminalProtocolFaultPoint,
    committed: bool,
) -> None:
    request = await _pause_with_clarification(db_path)
    decision = await TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
    ).record_decision(request)
    resume = _resume_request(decision)

    def inject(observed: TerminalProtocolFaultPoint) -> None:
        if observed is point:
            raise RuntimeError(point.value)

    with pytest.raises(RuntimeError, match=point.value):
        await TerminalProtocolStore(
            db_path,
            _clock=lambda: _NOW,
            _fault_injector=inject,
        ).open_resume_generation(resume)

    with sqlite3.connect(db_path) as connection:
        generations = connection.execute("SELECT COUNT(*) FROM turn_generations WHERE turn_id='t_1'").fetchone()[0]
    assert generations == (2 if committed else 1)
    replay = await TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
    ).open_resume_generation(resume)
    assert replay.disposition.value == ("replay" if committed else "opened")


@pytest.mark.asyncio
async def test_s5_factory_triggers_recovery_when_decision_commit_ack_is_lost(
    db_path: str,
) -> None:
    await _pause_with_clarification(db_path)
    triggered: list[tuple[TerminalScope, str]] = []

    def inject(point: TerminalProtocolFaultPoint) -> None:
        if point is TerminalProtocolFaultPoint.DECISION_AFTER_COMMIT_BEFORE_ACK:
            raise RuntimeError(point.value)

    def trigger(scope: TerminalScope, reason: str) -> None:
        triggered.append((scope, reason))

    factory = TerminalRuntimeFactory(
        TerminalProtocolStore(
            db_path,
            _clock=lambda: _NOW,
            _fault_injector=inject,
        ),
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
        recovery_trigger=trigger,
    )
    with pytest.raises(
        RuntimeError,
        match=TerminalProtocolFaultPoint.DECISION_AFTER_COMMIT_BEFORE_ACK.value,
    ):
        await factory.record_source_decision(
            team_id="team_1",
            session_id="s_1",
            turn_id="t_1",
            source_kind=DecisionSourceKind.CLARIFICATION,
            source_id="clarify_1",
            action=DecisionAction.RESUME,
            principals=DecisionPrincipalSet("u_1", "u_1", "u_1"),
            source_values={"chosen_value": "north"},
        )

    assert triggered == [
        (
            TerminalScope("team_1", "s_1", "t_1", 1),
            "decision_commit_uncertain",
        )
    ]
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state FROM turn_control_decisions").fetchone() == ("decision_recorded",)


@pytest.mark.asyncio
async def test_s5_factory_triggers_recovery_when_resume_open_ack_is_lost(
    db_path: str,
) -> None:
    await _pause_with_clarification(db_path)
    prepared = await _s5_factory(db_path).record_source_decision(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        source_kind=DecisionSourceKind.CLARIFICATION,
        source_id="clarify_1",
        action=DecisionAction.RESUME,
        principals=DecisionPrincipalSet("u_1", "u_1", "u_1"),
        source_values={"chosen_value": "north"},
    )
    triggered: list[tuple[TerminalScope, str]] = []

    def inject(point: TerminalProtocolFaultPoint) -> None:
        if point is TerminalProtocolFaultPoint.RESUME_AFTER_OPEN_BEFORE_CLAIM:
            raise RuntimeError(point.value)

    def trigger(scope: TerminalScope, reason: str) -> None:
        triggered.append((scope, reason))

    factory = TerminalRuntimeFactory(
        TerminalProtocolStore(
            db_path,
            _clock=lambda: _NOW,
            _fault_injector=inject,
        ),
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
        recovery_trigger=trigger,
    )
    with pytest.raises(
        RuntimeError,
        match=TerminalProtocolFaultPoint.RESUME_AFTER_OPEN_BEFORE_CLAIM.value,
    ):
        await factory.open_decision_runtime(
            prepared,
            TurnRequest(
                session_id="s_1",
                workspace_id="ws_1",
                user_id="u_1",
                member_uid="m_1",
                text="continue north",
                turn_id="t_1",
                team_id="team_1",
                resume_turn_id="t_1",
            ),
        )

    assert triggered == [
        (
            TerminalScope("team_1", "s_1", "t_1", 1),
            "resume_open_commit_uncertain",
        )
    ]
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            """
            SELECT generation,state,claim_epoch,execution_started_at
            FROM turn_generations WHERE turn_id='t_1'
            ORDER BY generation DESC LIMIT 1
            """
        ).fetchone() == (2, "opened", 0, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("point", "committed"),
    [
        (TerminalProtocolFaultPoint.CONTROL_BEFORE_CANDIDATE, False),
        (
            TerminalProtocolFaultPoint.CONTROL_AFTER_RECEIPT_BEFORE_ACK,
            True,
        ),
    ],
)
async def test_s5_control_fault_windows_are_atomic_or_replayable(
    db_path: str,
    point: TerminalProtocolFaultPoint,
    committed: bool,
) -> None:
    source = await _pause_with_clarification(db_path)
    context = replace(
        source.context,
        action=DecisionAction.DISMISS,
        decision_id=stable_decision_id(
            scope=source.context.scope,
            source_kind=DecisionSourceKind.CLARIFICATION,
            source_id="clarify_1",
            action=DecisionAction.DISMISS,
        ),
    )
    source_values = '{"dismissed_at":"2026-08-03T04:05:06Z"}'
    request = replace(
        source,
        context=context,
        decision_payload_digest=canonical_decision_payload_digest_for(
            source_kind=DecisionSourceKind.CLARIFICATION,
            action=DecisionAction.DISMISS,
            expected_source_state="pending",
            target_source_state="dismissed",
            source_values_json=source_values,
        ),
        target_source_state="dismissed",
        source_values_json=source_values,
    )
    decision = await TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
    ).record_decision(request)
    control = ControlGenerationRequest(
        decision=decision,
        status=TerminalStatus.CANCELLED,
        reason_code="clarification_dismissed",
        message_key="turn.clarification_dismissed",
        writer_instance_id="gateway_1",
        writer_lease_epoch=7,
        writer_admission_epoch=3,
        current_authz_snapshot_digest=_AUTH_DIGEST,
    )

    def inject(observed: TerminalProtocolFaultPoint) -> None:
        if observed is point:
            raise RuntimeError(point.value)

    with pytest.raises(RuntimeError, match=point.value):
        await TerminalProtocolStore(
            db_path,
            _clock=lambda: _NOW,
            _fault_injector=inject,
        ).commit_control_generation(control)

    with sqlite3.connect(db_path) as connection:
        receipts = connection.execute("SELECT COUNT(*) FROM turn_terminal_receipts").fetchone()[0]
    assert receipts == (2 if committed else 1)
    replay = await TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
    ).commit_control_generation(control)
    assert replay.status is TerminalStatus.CANCELLED


@pytest.mark.asyncio
async def test_s5_recovery_fence_fault_rolls_back_lease_cas(
    db_path: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE terminal_protocol_writer_lease SET lease_expires_at='2026-08-03T04:00:00Z'")
        connection.commit()

    def inject(point: TerminalProtocolFaultPoint) -> None:
        if point is TerminalProtocolFaultPoint.RECOVERY_FENCE_AFTER_CAS:
            raise RuntimeError(point.value)

    with pytest.raises(
        RuntimeError,
        match=TerminalProtocolFaultPoint.RECOVERY_FENCE_AFTER_CAS.value,
    ):
        await TerminalProtocolStore(
            db_path,
            _clock=lambda: _NOW,
            _fault_injector=inject,
        ).acquire_recovery_writer_fence(
            owner_instance_id="recovery_fault",
            rollout_config_generation="rollout_fault",
        )

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT owner_instance_id FROM terminal_protocol_writer_lease").fetchone() == ("gateway_1",)
    recovered = await TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
    ).acquire_recovery_writer_fence(
        owner_instance_id="recovery_clean",
        rollout_config_generation="rollout_clean",
    )
    assert recovered is not None


@pytest.mark.asyncio
async def test_s5_recovery_startup_fault_precedes_any_lease_mutation(
    db_path: str,
) -> None:
    def inject(point: TerminalProtocolFaultPoint) -> None:
        if point is TerminalProtocolFaultPoint.RECOVERY_STARTUP_BEFORE_OLD_LEASE_EXPIRY:
            raise RuntimeError(point.value)

    expected = TerminalProtocolFaultPoint.RECOVERY_STARTUP_BEFORE_OLD_LEASE_EXPIRY.value
    with pytest.raises(RuntimeError, match=expected):
        await TerminalRecoveryProcessor(
            TerminalProtocolStore(db_path, _clock=lambda: _NOW),
            fault_injector=inject,
        ).run_bounded(
            owner_instance_id="recovery_fault",
            rollout_config_generation="rollout_fault",
        )

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT owner_instance_id,epoch FROM terminal_protocol_writer_lease").fetchone() == ("gateway_1", 7)


@pytest.mark.asyncio
async def test_s5_prepared_recovery_interrupt_replays_after_process_fault(
    db_path: str,
) -> None:
    await _start_execution(TerminalProtocolStore(db_path, _clock=lambda: _NOW))
    injected = False

    def inject(point: TerminalProtocolFaultPoint) -> None:
        nonlocal injected
        if point is TerminalProtocolFaultPoint.RECOVERY_AFTER_PROJECTION_BEFORE_FINALIZER and not injected:
            injected = True
            raise RuntimeError(point.value)

    faulting_store = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW + timedelta(minutes=6),
    )
    first = await TerminalRecoveryProcessor(
        faulting_store,
        fault_injector=inject,
    ).run_bounded(
        owner_instance_id="gateway_recovery_fault",
        rollout_config_generation="recovery_fault",
    )

    assert first.errors == ("terminal_recovery_runtimeerror",)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT candidate_origin,state FROM turn_terminal_candidates").fetchone() == ("recovery", "prepared")
        assert connection.execute("SELECT outcome_variant FROM turn_terminal_candidate_variants").fetchone() == ("recovery_interrupt",)
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_receipts").fetchone() == (0,)

    clean_store = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW + timedelta(minutes=12),
    )
    replay = await TerminalRecoveryProcessor(clean_store).run_bounded(
        owner_instance_id="gateway_recovery_clean",
        rollout_config_generation="recovery_clean",
    )

    assert replay.errors == ()
    assert replay.eligible_remaining == 0
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT candidate_origin,state FROM turn_terminal_candidates").fetchone() == ("recovery", "committed")
        assert connection.execute("SELECT status,selected_variant_key FROM turn_terminal_receipts").fetchone() == ("aborted", "recovery_none")
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_candidates").fetchone() == (1,)


@pytest.mark.asyncio
async def test_s5_decision_effect_is_atomic_redacted_and_fenced(db_path: str) -> None:
    original = await _pause_with_clarification(db_path)
    effect = DecisionSideEffectPlan.from_payload(
        decision_id=original.context.decision_id,
        effect_kind="clarification_projection",
        target_ref="clarify_1",
        payload={"remember": True},
    )
    request = replace(original, side_effects=(effect,))
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)

    recorded = await store.record_decision(request)
    assert recorded.state is DecisionState.DECISION_RECORDED
    first = await store.claim_decision_effects(
        decision_id=recorded.context.decision_id,
        owner="worker_1",
        expected_authz_snapshot_digest=_AUTH_DIGEST,
    )
    assert len(first) == 1
    assert first[0].plan == effect
    assert (
        await store.claim_decision_effects(
            decision_id=recorded.context.decision_id,
            owner="worker_2",
            expected_authz_snapshot_digest=_AUTH_DIGEST,
        )
        == ()
    )
    assert await store.settle_decision_effect(
        first[0],
        applied=False,
        code="temporary_failure",
    )
    # A failed projection is durably backed off; advancing the injected clock
    # proves it cannot hot-loop and remains retryable once due.
    retry_store = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW + timedelta(seconds=31),
    )
    retry = await retry_store.claim_decision_effects(
        decision_id=recorded.context.decision_id,
        owner="worker_2",
        expected_authz_snapshot_digest=_AUTH_DIGEST,
    )
    assert len(retry) == 1
    assert retry[0].claim_epoch == 2
    assert await retry_store.settle_decision_effect(retry[0], applied=True)
    assert (
        await store.claim_decision_effects(
            decision_id=recorded.context.decision_id,
            owner="worker_3",
            expected_authz_snapshot_digest=_AUTH_DIGEST,
        )
        == ()
    )

    with sqlite3.connect(db_path) as connection:
        audit_json, full_digest = connection.execute("SELECT source_projection_json,source_projection_digest FROM turn_control_decisions").fetchone()
        assert "north" not in audit_json
        audit = json.loads(audit_json)
        assert audit["source_value_keys"] == ["chosen_value"]
        assert audit["source_projection_digest"] == full_digest
        assert connection.execute("SELECT state,claim_epoch,attempt_count,last_code FROM turn_decision_effects").fetchone() == ("applied", 2, 2, None)


@pytest.mark.asyncio
async def test_s5_resume_open_is_one_transaction_and_claims_only_after_commit(db_path: str) -> None:
    request = await _pause_with_clarification(db_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    decision = await store.record_decision(request)
    from bowhead_core.terminal_protocol import ResumeGenerationRequest

    resume = ResumeGenerationRequest(
        decision=decision,
        execution_intent_id="resume_intent_1",
        execution_intent_digest=canonical_digest_v1({"decision": "clarify_1"}),
        execution_intent={"decision": "clarify_1"},
        expected_generation_transition_epoch=1,
        expected_session_claim_epoch=0,
        writer_instance_id="gateway_1",
        writer_lease_epoch=7,
        current_authz_snapshot_digest=_AUTH_DIGEST,
    )
    opened = await store.open_resume_generation(resume)
    replay = await store.open_resume_generation(resume)
    assert opened.scope is not None and opened.scope.generation == 2
    assert replay.scope == opened.scope
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        turn = connection.execute(
            "SELECT current_generation,generation_transition_epoch,current_terminal_receipt_id,event_sequence_next FROM turns WHERE id='t_1'"
        ).fetchone()
        session = connection.execute("SELECT active_turn_id,active_turn_generation,active_turn_claim_epoch FROM sessions WHERE id='s_1'").fetchone()
        marker = connection.execute("SELECT event_type,generation,event_seq,phase_scope FROM turn_events WHERE event_type='turn.resumed'").fetchone()
    assert tuple(turn) == (2, 2, None, 3)
    assert tuple(session) == ("t_1", 2, 0)
    assert tuple(marker) == ("turn.resumed", 2, 2, "control_transition")

    claimed = await store.claim_generation(
        GenerationClaimContext(
            scope=opened.scope,
            expected_generation_row_version=1,
            expected_session_claim_epoch=0,
            writer_lease_epoch=7,
            owner_instance_id="gateway_1",
        )
    )
    assert claimed.disposition is GenerationClaimDisposition.CLAIMED
    assert claimed.context is not None
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT execution_started_at FROM turn_generations WHERE turn_id='t_1' AND generation=2").fetchone() == (None,)


@pytest.mark.asyncio
async def test_s5_opened_generation_replay_accepts_legal_epoch_change(
    db_path: str,
) -> None:
    request = await _pause_with_clarification(db_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    decision = await store.record_decision(request)
    from bowhead_core.terminal_protocol import ResumeGenerationRequest

    original = ResumeGenerationRequest(
        decision=decision,
        execution_intent_id="resume_intent_epoch_replay",
        execution_intent_digest=canonical_digest_v1({"decision": "clarify_1"}),
        execution_intent={"decision": "clarify_1"},
        expected_generation_transition_epoch=1,
        expected_session_claim_epoch=0,
        writer_instance_id="gateway_1",
        writer_lease_epoch=7,
        current_authz_snapshot_digest=_AUTH_DIGEST,
    )
    opened = await store.open_resume_generation(original)
    assert opened.scope is not None
    _bump_terminal_auth_epoch(db_path, marker="opened-replay-allowed")
    current = await store.read_current_decision_auth_digest(decision)
    replay = await store.open_resume_generation(replace(original, current_authz_snapshot_digest=current))

    assert replay.disposition.value == "replay"
    assert replay.scope == opened.scope
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT execution_authz_snapshot_digest FROM turn_generations WHERE generation=2").fetchone() == (_AUTH_DIGEST,)


@pytest.mark.asyncio
async def test_s5_control_generation_preserves_unrelated_session_and_replays(db_path: str) -> None:
    source_request = await _pause_with_clarification(db_path)
    dismiss_context = replace(
        source_request.context,
        action=DecisionAction.DISMISS,
        decision_id=stable_decision_id(
            scope=source_request.context.scope,
            source_kind=DecisionSourceKind.CLARIFICATION,
            source_id="clarify_1",
            action=DecisionAction.DISMISS,
        ),
    )
    dismiss_request = replace(
        source_request,
        context=dismiss_context,
        decision_payload_digest=canonical_decision_payload_digest_for(
            source_kind=DecisionSourceKind.CLARIFICATION,
            action=DecisionAction.DISMISS,
            expected_source_state="pending",
            target_source_state="dismissed",
            source_values_json='{"dismissed_at":"2026-08-03T04:05:06Z"}',
        ),
        target_source_state="dismissed",
        source_values_json='{"dismissed_at":"2026-08-03T04:05:06Z"}',
    )
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    decision = await store.record_decision(dismiss_request)
    control = ControlGenerationRequest(
        decision=decision,
        status=TerminalStatus.CANCELLED,
        reason_code="clarification_dismissed",
        message_key="turn.clarification_dismissed",
        writer_instance_id="gateway_1",
        writer_lease_epoch=7,
        writer_admission_epoch=3,
        current_authz_snapshot_digest=_AUTH_DIGEST,
    )
    first = await store.commit_control_generation(control)
    replay = await store.commit_control_generation(control)
    assert replay.receipt_id == first.receipt_id
    assert first.status is TerminalStatus.CANCELLED
    summary = await store.read_protocol_summary(
        TerminalReadContext(
            team_id="team_1",
            session_id="s_1",
            turn_id="t_1",
            viewer="u_1",
            auth_fence_vector_digest=_AUTH_DIGEST,
        )
    )
    assert summary.current_generation == 2
    assert summary.current_generation_kind == "control"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state,target_generation FROM turn_control_decisions").fetchone() == ("terminal_control_committed", 2)
        assert connection.execute("SELECT status FROM turn_terminal_receipts ORDER BY generation").fetchall() == [
            ("clarification_pending",),
            ("cancelled",),
        ]
        assert connection.execute("SELECT disposition FROM turn_terminal_session_mutations ORDER BY rowid DESC LIMIT 1").fetchone() == (
            "control_preserve_unrelated",
        )
        assert connection.execute("SELECT active_turn_id,steer_buffer_state,steer_buffer_json FROM sessions WHERE id='s_1'").fetchone() == (
            None,
            "collecting",
            "[]",
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "requested"),
    [
        (
            "dependency_missing",
            (
                TerminalStatus.ABORTED,
                "access_revoked",
                "turn.access_revoked",
            ),
        ),
        (
            "access_revoked",
            (
                TerminalStatus.FAILED,
                "terminal_dependency_missing",
                "turn.failed",
            ),
        ),
    ],
)
async def test_s5_control_commit_rejects_outcome_mapping_mismatch(
    db_path: str,
    outcome: str,
    requested: tuple[TerminalStatus, str, str],
) -> None:
    source = await _pause_with_clarification(db_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    decision = await store.record_decision(source)
    if outcome == "access_revoked":
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                """
                UPDATE terminal_auth_scope_epochs
                SET epoch=epoch+1,row_version=row_version+1,updated_at='revoked'
                """
            )
            connection.commit()
    revoked, _preflight = await store.supersede_pending_decision_with_revoke(
        decision,
        denial_reason_code=outcome,
    )
    status_value, reason, message_key = requested
    with pytest.raises(
        DecisionConflictError,
        match="durable outcome",
    ):
        await store.commit_control_generation(
            ControlGenerationRequest(
                decision=revoked,
                status=status_value,
                reason_code=reason,
                message_key=message_key,
                writer_instance_id="gateway_1",
                writer_lease_epoch=7,
                writer_admission_epoch=3,
                current_authz_snapshot_digest=(revoked.context.authz_snapshot_digest),
            )
        )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_receipts WHERE generation=2").fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dispatch", "preexisting_event"),
    [(False, False), (True, False), (True, True)],
)
async def test_s5_restart_projects_owner_lost_effect_to_manual_check(
    db_path: str,
    dispatch: bool,
    preexisting_event: bool,
) -> None:
    approvals, approval = await _pause_with_approval(db_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    factory = TerminalRuntimeFactory(
        store,
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
    )
    prepared = await factory.record_source_decision(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        source_kind=DecisionSourceKind.APPROVAL,
        source_id=approval.id,
        action=DecisionAction.APPROVE,
        principals=DecisionPrincipalSet(
            "u_1",
            "u_approver",
            "u_approver",
        ),
        source_values={
            "arguments_digest": approval.arguments_digest or "",
        },
    )
    runtime = await factory.open_decision_runtime(
        prepared,
        TurnRequest(
            session_id="s_1",
            workspace_id="ws_1",
            user_id="u_approver",
            member_uid="m_approver",
            text=f"approval:{approval.id}",
            turn_id="t_1",
            team_id="team_1",
            resume_turn_id="t_1",
        ),
    )
    await runtime.start_execution()
    attempt = await store.begin_effect_attempt(
        runtime.execution,
        attempt_id="tea_kill_window",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="approval:fake_write",
        action_ref="approval_action",
        idempotency_or_fence_token=(prepared.decision.context.decision_id),
        non_retriable=False,
    )
    if dispatch:
        attempt = (
            await store.claim_effect_dispatch(
                runtime.execution,
                attempt,
            )
        ).attempt
    if preexisting_event:
        await runtime.bus.emit_idempotent(
            OrchestratorEvent(
                event_type="interrupted_check_actions",
                turn_id="t_1",
                payload={
                    "approval_id": approval.id,
                    "attempt_id": attempt.attempt_id,
                    "message_key": "turn.interrupted_check_actions",
                    "reason_code": "effect_outcome_unknown",
                },
            ),
            idempotency_key=(f"approval_effect_unknown:{prepared.decision.context.decision_id}"),
        )
    original_version = attempt.row_version
    await runtime.close()

    restarted = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW + timedelta(minutes=6),
    )
    result = await TerminalRecoveryProcessor(restarted).run_bounded(
        owner_instance_id="gateway_restart_unknown",
        rollout_config_generation="recovery_s5",
    )

    assert result.errors == ()
    assert result.processed_count == 1
    with sqlite3.connect(db_path) as connection:
        if dispatch:
            assert result.eligible_remaining == 1
            assert connection.execute("SELECT state,row_version FROM turn_effect_attempts").fetchone() == ("unknown", original_version + 1)
            assert connection.execute("SELECT COUNT(*) FROM turn_events WHERE event_type='interrupted_check_actions'").fetchone() == (1,)
            assert connection.execute("SELECT COUNT(*) FROM turn_terminal_receipts WHERE generation=2").fetchone() == (0,)
        else:
            assert result.eligible_remaining == 0
            assert connection.execute("SELECT COUNT(*) FROM turn_effect_attempts").fetchone() == (0,)
            assert connection.execute("SELECT COUNT(*) FROM turn_events WHERE event_type='interrupted_check_actions'").fetchone() == (0,)
            assert connection.execute("SELECT status FROM turn_terminal_receipts WHERE generation=2").fetchone() == ("aborted",)


@pytest.mark.asyncio
async def test_s5_recovery_writer_fence_only_takes_expired_owner_and_never_admits(db_path: str) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    await store.ensure_auth_scope_epochs(
        terminal_auth_resolve_context_values(
            team_id="team_1",
            session_id="s_1",
            user_id="u_1",
            resource_ref="t_1",
        )
    )
    assert (
        await store.acquire_recovery_writer_fence(
            owner_instance_id="recovery_1",
            rollout_config_generation="recovery_config_1",
        )
        is None
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE terminal_protocol_writer_lease SET lease_expires_at='2026-08-03T04:00:00Z'")
        connection.commit()

    fence = await store.acquire_recovery_writer_fence(
        owner_instance_id="recovery_1",
        rollout_config_generation="recovery_config_1",
    )
    assert fence is not None and fence.recovery_only is True
    assert (
        await store.acquire_recovery_writer_fence(
            owner_instance_id="recovery_1",
            rollout_config_generation="recovery_config_1",
        )
        == fence
    )
    rejected = await store.admit_or_steer_v1(
        replace(
            _admission_request(),
            context=replace(
                _admission_request().context,
                writer_instance_id=fence.owner_instance_id,
                writer_lease_epoch=fence.writer_lease_epoch,
                writer_admission_epoch=fence.writer_admission_epoch,
                rollout_config_generation=fence.rollout_config_generation,
            ),
        )
    )
    assert rejected.code is AdmissionCode.WRITER_NOT_READY
    assert await store.heartbeat_recovery_writer_fence(fence) == fence
    assert await store.release_recovery_writer_fence(fence) is True
    assert await store.release_recovery_writer_fence(fence) is False


@pytest.mark.asyncio
async def test_s5_restart_recovery_defers_resume_until_execution_attacher_is_available(
    db_path: str,
) -> None:
    request = await _pause_with_clarification(db_path)
    await TerminalProtocolStore(db_path, _clock=lambda: _NOW).record_decision(request)

    restarted = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW + timedelta(minutes=6),
    )
    result = await TerminalRecoveryProcessor(restarted).run_bounded(
        owner_instance_id="gateway_restart_1",
        rollout_config_generation="recovery_s5",
    )

    assert result.fence_acquired is True
    assert result.eligible_remaining == 1
    assert result.errors == ("terminal_recovery_runtimeerror",)
    assert result.processed_count == 0
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state,target_generation,recovery_attempt_count,recovery_last_code FROM turn_control_decisions"
        ).fetchone() == (
            "decision_recorded",
            None,
            1,
            "terminal_recovery_runtimeerror",
        )
        assert connection.execute("SELECT COUNT(*) FROM turn_generations WHERE turn_id='t_1'").fetchone() == (1,)


@pytest.mark.asyncio
async def test_s5_factory_rehydrates_decision_after_resume_generation_opened(
    db_path: str,
) -> None:
    await _pause_with_clarification(db_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    runtimes = TerminalRuntimeRegistry()
    factory = TerminalRuntimeFactory(
        store,
        GenerationEventBusRegistry(),
        runtimes,
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
    )
    principals = DecisionPrincipalSet("u_1", "u_1", "u_1")
    prepared = await factory.record_source_decision(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        source_kind=DecisionSourceKind.CLARIFICATION,
        source_id="clarify_1",
        action=DecisionAction.RESUME,
        principals=principals,
        source_values={"chosen_value": "north"},
    )
    turn_request = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="continue north",
        turn_id="t_1",
        team_id="team_1",
        resume_turn_id="t_1",
    )
    runtime = await factory.open_decision_runtime(prepared, turn_request)

    replay = await factory.record_source_decision(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        source_kind=DecisionSourceKind.CLARIFICATION,
        source_id="clarify_1",
        action=DecisionAction.RESUME,
        principals=principals,
        source_values={"chosen_value": "north"},
    )
    assert replay.decision.target_generation == 2
    assert await factory.open_decision_runtime(replay, turn_request) is runtime
    with pytest.raises(DecisionConflictError):
        await factory.record_source_decision(
            team_id="team_1",
            session_id="s_1",
            turn_id="t_1",
            source_kind=DecisionSourceKind.CLARIFICATION,
            source_id="clarify_1",
            action=DecisionAction.RESUME,
            principals=principals,
            source_values={"chosen_value": "south"},
        )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT prior_receipt_id FROM turn_control_decisions").fetchone() == ("tr_pause_1",)


@pytest.mark.asyncio
async def test_s5_clarification_resume_fault_before_task_create_seals_only_n_plus_one(
    db_path: str,
) -> None:
    await _pause_with_clarification(db_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    runtimes = TerminalRuntimeRegistry()
    injected = False

    def inject(point: TerminalProtocolFaultPoint) -> None:
        nonlocal injected
        if point is TerminalProtocolFaultPoint.RESUME_BEFORE_TASK_CREATE and not injected:
            injected = True
            raise RuntimeError(point.value)

    factory = TerminalRuntimeFactory(
        store,
        GenerationEventBusRegistry(),
        runtimes,
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
        fault_injector=inject,
    )
    prepared = await factory.record_source_decision(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        source_kind=DecisionSourceKind.CLARIFICATION,
        source_id="clarify_1",
        action=DecisionAction.RESUME,
        principals=DecisionPrincipalSet("u_1", "u_1", "u_1"),
        source_values={"chosen_value": "north"},
    )
    request = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="continue north",
        turn_id="t_1",
        team_id="team_1",
        resume_turn_id="t_1",
    )
    runtime = await factory.open_decision_runtime(prepared, request)

    from bowhead_gateway.turn_service import spawn_turn

    with pytest.raises(
        RuntimeError,
        match=TerminalProtocolFaultPoint.RESUME_BEFORE_TASK_CREATE.value,
    ):
        await spawn_turn(
            SimpleNamespace(),
            replace(request, terminal_runtime=runtime),
            name_prefix="turn_resume",
        )

    assert injected is True
    assert runtimes.size == 0
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_generations").fetchone() == (2,)
        assert connection.execute("SELECT claim_epoch,execution_started_at,state FROM turn_generations WHERE generation=2").fetchone() == (
            1,
            None,
            "closed",
        )
        assert connection.execute("SELECT generation,status,reason_code FROM turn_terminal_receipts ORDER BY generation").fetchall() == [
            (1, "clarification_pending", None),
            (2, "aborted", "worker_start_failed"),
        ]
        assert connection.execute("SELECT COUNT(*) FROM turn_generations WHERE generation>2").fetchone() == (0,)
        assert connection.execute("SELECT cost_usd,tokens_total FROM sessions WHERE id='s_1'").fetchone() == (0, 0)


@pytest.mark.asyncio
async def test_s5_decision_freezes_clarification_execution_envelope(
    db_path: str,
) -> None:
    request = await _pause_with_clarification(db_path)
    await TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
    ).record_decision(request)

    with sqlite3.connect(db_path) as connection:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="clarification execution context is immutable",
        ):
            connection.execute(
                """
                UPDATE clarification_requests
                SET resume_context_json='{"user_text":"changed"}'
                WHERE request_id='clarify_1'
                """
            )
        connection.rollback()


@pytest.mark.asyncio
async def test_s5_decision_freezes_approval_execution_envelope(
    db_path: str,
) -> None:
    _approvals, approval = await _pause_with_approval(db_path)
    factory = _s5_factory(db_path)
    await factory.record_source_decision(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        source_kind=DecisionSourceKind.APPROVAL,
        source_id=approval.id,
        action=DecisionAction.APPROVE,
        principals=DecisionPrincipalSet(
            "u_1",
            "u_approver",
            "u_approver",
        ),
        source_values={
            "arguments_digest": approval.arguments_digest or "",
        },
    )

    with sqlite3.connect(db_path) as connection:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="approval execution context is immutable",
        ):
            connection.execute(
                """
                UPDATE approvals SET workspace_id='ws_changed'
                WHERE id=?
                """,
                (approval.id,),
            )
        connection.rollback()


@pytest.mark.asyncio
async def test_s5_source_workspace_drift_before_decision_is_zero_write(
    db_path: str,
) -> None:
    _approvals, approval = await _pause_with_approval(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE approvals SET workspace_id='ws_other' WHERE id=?",
            (approval.id,),
        )
        connection.commit()

    with pytest.raises(
        DecisionConflictError,
        match="workspace changed",
    ):
        await _s5_factory(db_path).record_source_decision(
            team_id="team_1",
            session_id="s_1",
            turn_id="t_1",
            source_kind=DecisionSourceKind.APPROVAL,
            source_id=approval.id,
            action=DecisionAction.APPROVE,
            principals=DecisionPrincipalSet(
                "u_1",
                "u_approver",
                "u_approver",
            ),
            source_values={
                "arguments_digest": approval.arguments_digest or "",
            },
        )

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_control_decisions").fetchone() == (0,)
        assert connection.execute(
            "SELECT status FROM approvals WHERE id=?",
            (approval.id,),
        ).fetchone() == ("pending",)


@pytest.mark.asyncio
async def test_s5_approval_requester_workspace_revoke_blocks_n_plus_one(
    db_path: str,
) -> None:
    _approvals, approval = await _pause_with_approval(db_path)
    prepared = await _s5_factory(db_path).record_source_decision(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        source_kind=DecisionSourceKind.APPROVAL,
        source_id=approval.id,
        action=DecisionAction.APPROVE,
        principals=DecisionPrincipalSet(
            "u_1",
            "u_approver",
            "u_approver",
        ),
        source_values={
            "arguments_digest": approval.arguments_digest or "",
        },
    )

    class Users:
        async def get_user(self, user_id):
            return SimpleNamespace(id=user_id, status="active")

        async def get_team_role(self, user_id, _team_id):
            return "team_admin" if user_id == "u_approver" else "member"

        async def get_workspace_team_id(self, _workspace_id):
            return "team_1"

        async def get_member_role(self, _workspace_id, user_id):
            return "manager" if user_id == "u_approver" else None

        async def get_workspace(self, _workspace_id):
            return {"status": "active"}

    class Sessions:
        async def get_session(self, _session_id):
            return SimpleNamespace(
                user_id="u_1",
                workspace_id="ws_1",
                deleted_at=None,
            )

    app = SimpleNamespace(
        state=SimpleNamespace(
            user_store=Users(),
            session_store=Sessions(),
            access_control_store=None,
            deployment_mode="saas",
            entitlements=SimpleNamespace(
                assert_can_run_turn=lambda _team_id: None,
            ),
        )
    )
    with pytest.raises(
        DecisionAccessRevokedError,
        match="workspace access changed",
    ):
        await TerminalDecisionAuthorizer(app=app).authorize(
            prepared,
            workspace_id="ws_1",
        )


@pytest.mark.asyncio
async def test_s5_missing_entitlement_dependency_fails_closed(
    db_path: str,
) -> None:
    _approvals, approval = await _pause_with_approval(db_path)
    prepared = await _s5_factory(db_path).record_source_decision(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        source_kind=DecisionSourceKind.APPROVAL,
        source_id=approval.id,
        action=DecisionAction.APPROVE,
        principals=DecisionPrincipalSet(
            "u_1",
            "u_approver",
            "u_approver",
        ),
        source_values={
            "arguments_digest": approval.arguments_digest or "",
        },
    )

    class Users:
        async def get_user(self, user_id):
            return SimpleNamespace(id=user_id, status="active")

        async def get_team_role(self, user_id, _team_id):
            return "team_admin" if user_id == "u_approver" else "member"

        async def get_workspace_team_id(self, _workspace_id):
            return "team_1"

        async def get_member_role(self, _workspace_id, user_id):
            return "ws_admin" if user_id == "u_approver" else "viewer"

        async def get_workspace(self, _workspace_id):
            return {"status": "active"}

    class Sessions:
        async def get_session(self, _session_id):
            return SimpleNamespace(
                user_id="u_1",
                workspace_id="ws_1",
                deleted_at=None,
            )

    app = SimpleNamespace(
        state=SimpleNamespace(
            user_store=Users(),
            session_store=Sessions(),
            access_control_store=None,
            deployment_mode="saas",
            entitlements=None,
        )
    )
    with pytest.raises(
        DecisionAuthorizationDeniedError,
        match="dependency_missing",
    ):
        await TerminalDecisionAuthorizer(app=app).authorize(
            prepared,
            workspace_id="ws_1",
        )

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_generations WHERE generation=2").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM turn_effect_attempts").fetchone() == (0,)


@pytest.mark.asyncio
async def test_s5_approval_runner_executes_provider_once_and_commits_receipt(db_path: str) -> None:
    approvals, approval = await _pause_with_approval(db_path)
    factory = TerminalRuntimeFactory(
        TerminalProtocolStore(db_path, _clock=lambda: _NOW),
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
    )
    prepared = await factory.record_source_decision(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        source_kind=DecisionSourceKind.APPROVAL,
        source_id=approval.id,
        action=DecisionAction.APPROVE,
        principals=DecisionPrincipalSet("u_1", "u_approver", "u_approver"),
        source_values={"arguments_digest": approval.arguments_digest or ""},
    )
    approved = await approvals.get(approval.id)
    assert approved is not None and approved.status == "approved"

    class FakeExecutor:
        calls = 0

        async def execute_effect(
            self,
            approval_id: str,
            *,
            effect_user_id: str,
            idempotency_key: str,
        ):
            self.calls += 1
            assert approval_id == approval.id
            assert effect_user_id == "u_approver"
            assert idempotency_key == prepared.decision.context.decision_id
            return True, {"updated": 1}, None

    executor = FakeExecutor()
    projected = await TerminalApprovalRunner(
        factory=factory,
        approval_store=approvals,
    ).execute(
        prepared=prepared,
        approval=approved,
        legacy_executor=executor,
    )

    assert executor.calls == 1
    assert projected is not None and projected.status == "executed"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state FROM turn_effect_attempts").fetchone() == ("settled",)
        assert connection.execute("SELECT status FROM turn_terminal_receipts WHERE generation=2").fetchone() == ("completed",)
        assert connection.execute("SELECT COUNT(*) FROM turn_events WHERE event_type='approval.executed'").fetchone() == (1,)
        assert connection.execute("SELECT authz_snapshot_digest FROM turn_effect_settlements").fetchone() == (
            prepared.preflight.authz_snapshot_digest,
        )


@pytest.mark.asyncio
async def test_s5_legal_auth_epoch_change_rehydrates_and_dispatches_once(
    db_path: str,
) -> None:
    approvals, approval = await _pause_with_approval(db_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    factory = TerminalRuntimeFactory(
        store,
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
    )
    principals = DecisionPrincipalSet(
        "u_1",
        "u_approver",
        "u_approver",
    )
    original = await factory.record_source_decision(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        source_kind=DecisionSourceKind.APPROVAL,
        source_id=approval.id,
        action=DecisionAction.APPROVE,
        principals=principals,
        source_values={"arguments_digest": approval.arguments_digest or ""},
    )
    decision_audit_digest = original.decision.context.authz_snapshot_digest
    _bump_terminal_auth_epoch(db_path, marker="still-allowed")
    rehydrated = await store.read_source_decision(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        source_kind=DecisionSourceKind.APPROVAL,
        source_id=approval.id,
        action=DecisionAction.APPROVE,
        principals=principals,
    )
    assert rehydrated is not None
    decision, current = rehydrated
    assert decision.context.authz_snapshot_digest == decision_audit_digest
    assert current.authz_snapshot_digest != decision_audit_digest
    approved = await approvals.get(approval.id)
    assert approved is not None

    class PermittingAuthorizer:
        calls = 0

        async def authorize(self, _prepared, *, workspace_id: str) -> None:
            self.calls += 1
            assert workspace_id == "ws_1"

    class CountingExecutor:
        calls = 0

        async def execute_effect(
            self,
            _approval_id: str,
            *,
            effect_user_id: str,
            idempotency_key: str,
        ):
            self.calls += 1
            assert effect_user_id == "u_approver"
            assert idempotency_key == decision.context.decision_id
            return True, {"updated": 1}, None

    authorizer = PermittingAuthorizer()
    executor = CountingExecutor()
    projected = await TerminalApprovalRunner(
        factory=factory,
        approval_store=approvals,
        authorizer=authorizer,
    ).execute(
        prepared=TerminalPreparedDecision(decision, current),
        approval=approved,
        legacy_executor=executor,
    )

    # First-use open and the later provider boundary are independent TOCTOU
    # fences; both must prove current authorization.
    assert authorizer.calls == 2
    assert executor.calls == 1
    assert projected.status == "executed"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT authz_snapshot_digest FROM turn_control_decisions").fetchone() == (decision_audit_digest,)
        assert connection.execute(
            """
            SELECT execution_authz_snapshot_digest
            FROM turn_generations WHERE generation=2
            """
        ).fetchone() == (current.authz_snapshot_digest,)


@pytest.mark.asyncio
async def test_s5_recovery_opens_recorded_decision_after_legal_epoch_change(
    db_path: str,
) -> None:
    approvals, approval = await _pause_with_approval(db_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    factory = TerminalRuntimeFactory(
        store,
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
    )
    principals = DecisionPrincipalSet(
        "u_1",
        "u_approver",
        "u_approver",
    )
    original = await factory.record_source_decision(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        source_kind=DecisionSourceKind.APPROVAL,
        source_id=approval.id,
        action=DecisionAction.APPROVE,
        principals=principals,
        source_values={"arguments_digest": approval.arguments_digest or ""},
    )
    old_digest = original.decision.context.authz_snapshot_digest
    _bump_terminal_auth_epoch(db_path, marker="recovery-still-allowed")
    opened = []

    async def recover_pending(pending, _fence) -> bool:
        rehydrated = await store.read_source_decision(
            team_id="team_1",
            session_id="s_1",
            turn_id="t_1",
            source_kind=DecisionSourceKind.APPROVAL,
            source_id=approval.id,
            action=DecisionAction.APPROVE,
            principals=principals,
        )
        assert rehydrated is not None
        decision, preflight = rehydrated
        assert decision.decision_digest == pending.decision.decision_digest
        assert preflight.authz_snapshot_digest != old_digest
        runtime = await factory.open_decision_runtime(
            TerminalPreparedDecision(decision, preflight),
            TurnRequest(
                session_id="s_1",
                workspace_id="ws_1",
                user_id="u_approver",
                member_uid="m_approver",
                text="approval",
                turn_id="t_1",
                team_id="team_1",
                resume_turn_id="t_1",
            ),
        )
        opened.append(runtime)
        return True

    result = await TerminalRecoveryProcessor(
        store,
        pending_execution_recoverer=recover_pending,
    ).run_bounded(
        owner_instance_id="gateway_1",
        rollout_config_generation="rollout_1",
        writer_fence=TerminalWriterLease(
            owner_instance_id="gateway_1",
            writer_lease_epoch=7,
            writer_admission_epoch=3,
            rollout_config_generation="rollout_1",
            lease_expires_at=(_NOW + timedelta(minutes=5)).isoformat(),
            admission_state="open",
        ),
    )

    assert result.errors == ()
    assert result.processed_count == 1
    assert len(opened) == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state,target_generation FROM turn_control_decisions").fetchone() == ("generation_opened", 2)
        assert connection.execute("SELECT execution_authz_snapshot_digest FROM turn_generations WHERE generation=2").fetchone()[0] != old_digest
    await opened[0].retire()


@pytest.mark.asyncio
async def test_s5_auth_change_after_open_blocks_provider_dispatch(
    db_path: str,
) -> None:
    approvals, approval = await _pause_with_approval(db_path)
    factory = TerminalRuntimeFactory(
        TerminalProtocolStore(db_path, _clock=lambda: _NOW),
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
    )
    prepared = await factory.record_source_decision(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        source_kind=DecisionSourceKind.APPROVAL,
        source_id=approval.id,
        action=DecisionAction.APPROVE,
        principals=DecisionPrincipalSet(
            "u_1",
            "u_approver",
            "u_approver",
        ),
        source_values={"arguments_digest": approval.arguments_digest or ""},
    )
    approved = await approvals.get(approval.id)
    assert approved is not None

    class GapAuthorizer:
        calls = 0

        async def authorize(self, _prepared, *, workspace_id: str) -> None:
            self.calls += 1
            assert workspace_id == "ws_1"
            if self.calls == 2:
                _bump_terminal_auth_epoch(
                    db_path,
                    marker="revoked-after-open",
                )

    class NeverCalledExecutor:
        calls = 0

        async def execute_effect(self, *_args, **_kwargs):
            self.calls += 1
            return True, {}, None

    executor = NeverCalledExecutor()
    with pytest.raises(
        DecisionAccessRevokedError,
        match="authorization changed",
    ):
        await TerminalApprovalRunner(
            factory=factory,
            approval_store=approvals,
            authorizer=GapAuthorizer(),
        ).execute(
            prepared=prepared,
            approval=approved,
            legacy_executor=executor,
        )

    assert executor.calls == 0
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_effect_attempts").fetchone() == (0,)
        assert connection.execute("SELECT status FROM turn_terminal_receipts WHERE generation=2").fetchone() == ("failed",)
        assert connection.execute("SELECT COUNT(*) FROM turn_events WHERE generation=2 AND event_type='turn_failed'").fetchone() == (1,)


@pytest.mark.asyncio
async def test_s5_auth_change_after_intent_is_not_projected_as_unknown(
    db_path: str,
) -> None:
    approvals, approval = await _pause_with_approval(db_path)

    class GapAfterIntentStore(TerminalProtocolStore):
        async def begin_effect_attempt(self, context, **kwargs):
            attempt = await super().begin_effect_attempt(context, **kwargs)
            _bump_terminal_auth_epoch(
                db_path,
                marker="revoked-after-intent",
            )
            return attempt

    factory = TerminalRuntimeFactory(
        GapAfterIntentStore(db_path, _clock=lambda: _NOW),
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
    )
    prepared = await factory.record_source_decision(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        source_kind=DecisionSourceKind.APPROVAL,
        source_id=approval.id,
        action=DecisionAction.APPROVE,
        principals=DecisionPrincipalSet(
            "u_1",
            "u_approver",
            "u_approver",
        ),
        source_values={"arguments_digest": approval.arguments_digest or ""},
    )
    approved = await approvals.get(approval.id)
    assert approved is not None

    class NeverCalledExecutor:
        calls = 0

        async def execute_effect(self, *_args, **_kwargs):
            self.calls += 1
            return True, {}, None

    executor = NeverCalledExecutor()
    with pytest.raises(
        DecisionAccessRevokedError,
        match="authorization changed",
    ):
        await TerminalApprovalRunner(
            factory=factory,
            approval_store=approvals,
        ).execute(
            prepared=prepared,
            approval=approved,
            legacy_executor=executor,
        )

    assert executor.calls == 0
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_effect_attempts").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM turn_effect_settlements").fetchone() == (0,)
        assert connection.execute("SELECT status FROM turn_terminal_receipts WHERE generation=2").fetchone() == ("failed",)
        assert connection.execute("SELECT COUNT(*) FROM turn_events WHERE generation=2 AND event_type='interrupted_check_actions'").fetchone() == (0,)


@pytest.mark.asyncio
async def test_s5_failed_approval_emits_turn_failed_before_failed_done(
    db_path: str,
) -> None:
    approvals, approval = await _pause_with_approval(db_path)
    factory = TerminalRuntimeFactory(
        TerminalProtocolStore(db_path, _clock=lambda: _NOW),
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
    )
    prepared = await factory.record_source_decision(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        source_kind=DecisionSourceKind.APPROVAL,
        source_id=approval.id,
        action=DecisionAction.APPROVE,
        principals=DecisionPrincipalSet("u_1", "u_approver", "u_approver"),
        source_values={"arguments_digest": approval.arguments_digest or ""},
    )
    approved = await approvals.get(approval.id)
    assert approved is not None

    class FailedExecutor:
        async def execute_effect(
            self,
            _approval_id: str,
            *,
            effect_user_id: str,
            idempotency_key: str,
        ):
            assert effect_user_id == "u_approver"
            assert idempotency_key == prepared.decision.context.decision_id
            return False, None, "provider rejected"

    projected = await TerminalApprovalRunner(
        factory=factory,
        approval_store=approvals,
    ).execute(
        prepared=prepared,
        approval=approved,
        legacy_executor=FailedExecutor(),
    )

    assert projected is not None and projected.status == "execution_failed"
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT event_type FROM turn_events WHERE generation=2 ORDER BY event_seq").fetchall()
        assert ("turn_failed",) in rows
        assert rows[-1] == ("done",)
        assert connection.execute("SELECT status FROM turn_terminal_receipts WHERE generation=2").fetchone() == ("failed",)


@pytest.mark.asyncio
async def test_s5_ambiguous_provider_exception_stays_unknown_without_retry(
    db_path: str,
) -> None:
    approvals, approval = await _pause_with_approval(db_path)
    factory = TerminalRuntimeFactory(
        TerminalProtocolStore(db_path, _clock=lambda: _NOW),
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
    )
    prepared = await factory.record_source_decision(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        source_kind=DecisionSourceKind.APPROVAL,
        source_id=approval.id,
        action=DecisionAction.APPROVE,
        principals=DecisionPrincipalSet(
            "u_1",
            "u_approver",
            "u_approver",
        ),
        source_values={"arguments_digest": approval.arguments_digest or ""},
    )
    approved = await approvals.get(approval.id)
    assert approved is not None

    class AmbiguousExecutor:
        calls = 0

        async def execute_effect(self, *_args, **_kwargs):
            self.calls += 1
            raise RuntimeError("connection lost after possible commit")

    executor = AmbiguousExecutor()
    runner = TerminalApprovalRunner(
        factory=factory,
        approval_store=approvals,
    )
    with pytest.raises(RuntimeError, match="possible commit"):
        await runner.execute(
            prepared=prepared,
            approval=approved,
            legacy_executor=executor,
        )
    with pytest.raises(TerminalRuntimeAdmissionError):
        await runner.execute(
            prepared=prepared,
            approval=approved,
            legacy_executor=executor,
        )

    assert executor.calls == 1
    assert (await approvals.get(approval.id)).status == "approved"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state FROM turn_effect_attempts").fetchone() == ("unknown",)
        assert connection.execute("SELECT COUNT(*) FROM turn_effect_settlements").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_receipts WHERE generation=2").fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recovery_fault_point", "with_mixed_v2_effects"),
    [
        (None, False),
        (
            TerminalProtocolFaultPoint.RECOVERY_AFTER_EFFECT_SETTLEMENT_BEFORE_PROJECTION,
            False,
        ),
        (
            TerminalProtocolFaultPoint.RECOVERY_AFTER_PROJECTION_BEFORE_FINALIZER,
            False,
        ),
        (None, True),
    ],
)
async def test_s5_approval_projection_recovers_after_settlement_ack_gap_without_provider_retry(
    db_path: str,
    recovery_fault_point: TerminalProtocolFaultPoint | None,
    with_mixed_v2_effects: bool,
) -> None:
    approvals, approval = await _pause_with_approval(db_path)

    def inject(point: TerminalProtocolFaultPoint) -> None:
        if point is TerminalProtocolFaultPoint.EFFECT_BEFORE_SETTLE:
            raise RuntimeError("settlement unavailable")

    faulting_store = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=inject,
    )
    runtime_registry = TerminalRuntimeRegistry()
    factory = TerminalRuntimeFactory(
        faulting_store,
        GenerationEventBusRegistry(),
        runtime_registry,
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
    )
    prepared = await factory.record_source_decision(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        source_kind=DecisionSourceKind.APPROVAL,
        source_id=approval.id,
        action=DecisionAction.APPROVE,
        principals=DecisionPrincipalSet("u_1", "u_approver", "u_approver"),
        source_values={"arguments_digest": approval.arguments_digest or ""},
    )
    approved = await approvals.get(approval.id)
    assert approved is not None

    class FakeExecutor:
        calls = 0

        async def execute_effect(
            self,
            _approval_id: str,
            *,
            effect_user_id: str,
            idempotency_key: str,
        ):
            self.calls += 1
            assert effect_user_id == "u_approver"
            assert idempotency_key == prepared.decision.context.decision_id
            if with_mixed_v2_effects:
                runtime = await runtime_registry.get("t_1")
                assert runtime is not None
                clean_store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
                extra = await clean_store.begin_effect_attempt(
                    runtime.execution,
                    attempt_id="eff_mixed_settled",
                    effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
                    provider="tool_external/v1",
                    action_ref="tool:mixed:settled",
                    idempotency_or_fence_token="mixed-settled-key",
                    non_retriable=False,
                )
                extra = await clean_store.mark_effect_dispatched(
                    runtime.execution,
                    extra,
                )
                extra = await clean_store.mark_effect_unknown(extra)
                await clean_store.record_provider_effect_settlement(
                    EffectSettlementContext(
                        scope=runtime.scope,
                        attempt_id=extra.attempt_id,
                        expected_attempt_row_version=extra.row_version,
                        settlement_id="set_mixed_settled",
                        source=EffectSettlementSource.PROVIDER,
                        outcome=EffectSettlementOutcome.CONFIRMED_APPLIED,
                        settlement_code="provider_confirmed",
                        principal_ref="adapter:tool_external_probe/v1",
                        principal_role="provider_adapter",
                        authz_snapshot_digest="adapter-capability-v1",
                        evidence_link_id="evidence_mixed_settled",
                        evidence_digest="evidence-digest-mixed-settled",
                        object_version_digest="object-version-mixed-settled",
                        purge_after=(_NOW + timedelta(days=30)).isoformat(),
                        audit_object_ref="provider-evidence:mixed-settled",
                    )
                )
                await clean_store.begin_effect_attempt(
                    runtime.execution,
                    attempt_id="eff_mixed_intent",
                    effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
                    provider="tool_external/v1",
                    action_ref="tool:mixed:intent",
                    idempotency_or_fence_token="mixed-intent-key",
                    non_retriable=False,
                )
            return True, {"updated": 1}, None

    executor = FakeExecutor()
    with pytest.raises(RuntimeError, match="settlement unavailable"):
        await TerminalApprovalRunner(
            factory=factory,
            approval_store=approvals,
        ).execute(
            prepared=prepared,
            approval=approved,
            legacy_executor=executor,
        )
    if with_mixed_v2_effects:
        # D2b.2 has no fresh contract-v2 writer. Convert the completed legacy
        # setup into the exact future-image persisted shape so rollback-reader
        # recovery is exercised without opening production ingress.
        with sqlite3.connect(db_path) as connection:
            raw_intent = connection.execute("SELECT execution_intent_json FROM turn_generations WHERE turn_id='t_1' AND generation=2").fetchone()[0]
            execution_intent = json.loads(raw_intent)
            execution_intent.update(
                {
                    "billing_rate_card_version": "v1",
                    "effect_contract_version": 2,
                }
            )
            connection.execute("DROP TRIGGER IF EXISTS terminal_generation_envelope_immutable")
            connection.execute("DROP TRIGGER IF EXISTS turn_generation_identity_immutable")
            connection.execute(
                """
                UPDATE turn_generations
                SET execution_intent_json=?,execution_intent_digest=?
                WHERE turn_id='t_1' AND generation=2
                """,
                (
                    canonical_json_v1(execution_intent),
                    canonical_digest_v1(execution_intent),
                ),
            )
            connection.execute(
                """
                UPDATE turn_effect_attempts
                SET provider='approval_decision/v1'
                WHERE turn_id='t_1' AND generation=2
                  AND provider LIKE 'approval:%'
                """
            )
            connection.commit()
    assert executor.calls == 1
    assert (await approvals.get(approval.id)).status == "executed"
    _bump_terminal_auth_epoch(
        db_path,
        marker="projection-committed-after-auth-change",
    )

    recovery_store = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW + timedelta(minutes=6),
    )
    injected = False

    def inject_recovery(point: TerminalProtocolFaultPoint) -> None:
        nonlocal injected
        if point is recovery_fault_point and not injected:
            injected = True
            raise RuntimeError(point.value)

    result = await TerminalRecoveryProcessor(
        recovery_store,
        settled_effect_recoverer=TerminalApprovalRecovery(
            protocol_store=recovery_store,
            approval_store=approvals,
            fault_injector=inject_recovery,
        ),
        fault_injector=inject_recovery,
    ).run_bounded(
        owner_instance_id="gateway_recovery",
        rollout_config_generation="recovery_s5",
    )

    assert executor.calls == 1
    if recovery_fault_point is not None:
        assert result.errors
        with sqlite3.connect(db_path) as connection:
            candidate_count = connection.execute("SELECT COUNT(*) FROM turn_terminal_candidates WHERE generation=2").fetchone()[0]
            assert candidate_count == (1 if recovery_fault_point is TerminalProtocolFaultPoint.RECOVERY_AFTER_PROJECTION_BEFORE_FINALIZER else 0)
            assert connection.execute("SELECT COUNT(*) FROM turn_terminal_receipts WHERE generation=2").fetchone() == (0,)
        clean_store = TerminalProtocolStore(
            db_path,
            _clock=lambda: _NOW + timedelta(minutes=12),
        )
        result = await TerminalRecoveryProcessor(
            clean_store,
            settled_effect_recoverer=TerminalApprovalRecovery(
                protocol_store=clean_store,
                approval_store=approvals,
            ),
        ).run_bounded(
            owner_instance_id="gateway_recovery_clean",
            rollout_config_generation="recovery_s5_clean",
        )
    assert result.errors == ()
    assert result.eligible_remaining == 0
    assert executor.calls == 1
    with sqlite3.connect(db_path) as connection:
        attempts = connection.execute("SELECT attempt_id,state FROM turn_effect_attempts ORDER BY attempt_id").fetchall()
        assert all(state == "settled" for _, state in attempts)
        assert ("eff_mixed_intent", "intent") not in attempts
        assert len(attempts) == (2 if with_mixed_v2_effects else 1)
        assert connection.execute("SELECT status FROM turn_terminal_receipts WHERE generation=2").fetchone() == ("completed",)
        assert connection.execute("SELECT COUNT(*) FROM turn_events WHERE event_type='approval.executed'").fetchone() == (1,)
        assert connection.execute(
            "SELECT s.authz_snapshot_digest FROM turn_effect_settlements s "
            "JOIN turn_effect_attempts a ON a.attempt_id=s.attempt_id "
            "WHERE a.provider='approval_decision/v1' "
            "OR a.provider LIKE 'approval:%'"
        ).fetchone() == (prepared.preflight.authz_snapshot_digest,)
        if with_mixed_v2_effects:
            assert connection.execute(
                "SELECT authz_snapshot_digest FROM turn_effect_settlements WHERE attempt_id='eff_mixed_settled'"
            ).fetchone() == ("adapter-capability-v1",)


@pytest.mark.asyncio
async def test_s5_concurrent_approval_runners_have_one_provider_dispatch_winner(
    db_path: str,
) -> None:
    approvals, approval = await _pause_with_approval(db_path)
    dispatch_arrivals = 0

    class CountingStore(TerminalProtocolStore):
        async def claim_effect_dispatch(self, context, attempt):
            nonlocal dispatch_arrivals
            dispatch_arrivals += 1
            return await super().claim_effect_dispatch(context, attempt)

    factory = TerminalRuntimeFactory(
        CountingStore(db_path, _clock=lambda: _NOW),
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
    )
    prepared = await factory.record_source_decision(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        source_kind=DecisionSourceKind.APPROVAL,
        source_id=approval.id,
        action=DecisionAction.APPROVE,
        principals=DecisionPrincipalSet("u_1", "u_approver", "u_approver"),
        source_values={"arguments_digest": approval.arguments_digest or ""},
    )
    approved = await approvals.get(approval.id)
    assert approved is not None
    provider_entered = asyncio.Event()
    release_provider = asyncio.Event()

    class BarrierExecutor:
        calls = 0

        async def execute_effect(
            self,
            _approval_id: str,
            *,
            effect_user_id: str,
            idempotency_key: str,
        ):
            self.calls += 1
            assert effect_user_id == "u_approver"
            assert idempotency_key == prepared.decision.context.decision_id
            provider_entered.set()
            await release_provider.wait()
            return True, {"updated": 1}, None

    executor = BarrierExecutor()
    runner = TerminalApprovalRunner(factory=factory, approval_store=approvals)
    first = asyncio.create_task(
        runner.execute(
            prepared=prepared,
            approval=approved,
            legacy_executor=executor,
        )
    )
    await provider_entered.wait()
    second = asyncio.create_task(
        runner.execute(
            prepared=prepared,
            approval=approved,
            legacy_executor=executor,
        )
    )
    await asyncio.sleep(0)
    release_provider.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert executor.calls == 1
    assert dispatch_arrivals == 1
    assert all(not isinstance(result, BaseException) for result in results)
    assert results[0] == results[1]


def _bump_user_auth_epoch(db_path: str, *, marker: str) -> None:
    with sqlite3.connect(db_path) as connection:
        update = connection.execute(
            """
            UPDATE terminal_auth_scope_epochs
            SET epoch=epoch+1,row_version=row_version+1,updated_at=?
            WHERE scope_kind='user_account'
              AND team_id='__global__' AND scope_ref='u_1'
            """,
            (marker,),
        )
        assert update.rowcount == 1
        connection.commit()


def _s5_factory(
    db_path: str,
    *,
    store: TerminalProtocolStore | None = None,
) -> TerminalRuntimeFactory:
    return TerminalRuntimeFactory(
        store or TerminalProtocolStore(db_path, _clock=lambda: _NOW),
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        TerminalWriterSnapshot("gateway_1", 7, 3, "rollout_1"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_kind", "system_actor"),
    [
        (
            DecisionSourceKind.CLARIFICATION,
            "system:clarification-expiry",
        ),
        (DecisionSourceKind.APPROVAL, "system:approval-expiry"),
    ],
)
async def test_s5_system_expiry_uses_exact_narrow_auth_context(
    db_path: str,
    source_kind: DecisionSourceKind,
    system_actor: str,
) -> None:
    if source_kind is DecisionSourceKind.CLARIFICATION:
        await _pause_with_clarification(db_path)
        source_id = "clarify_1"
    else:
        _approvals, approval = await _pause_with_approval(db_path)
        source_id = approval.id
    factory = _s5_factory(db_path)
    prepared = await factory.record_source_decision(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        source_kind=source_kind,
        source_id=source_id,
        action=DecisionAction.EXPIRE,
        principals=DecisionPrincipalSet(
            "u_1",
            system_actor,
            "u_1",
        ),
    )

    # Narrow system authority does not depend on the disabled requester's
    # user-account epoch, but remains fenced by team/resource/Session scopes.
    _bump_user_auth_epoch(db_path, marker="requester-disabled")
    await factory.commit_decision_control(
        prepared,
        reason_code="decision_expired",
        message_key="turn.decision_expired",
    )

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT status FROM turn_terminal_receipts WHERE generation=2").fetchone() == ("aborted",)
        assert connection.execute("SELECT state,target_generation FROM turn_control_decisions").fetchone() == ("terminal_control_committed", 2)


@pytest.mark.asyncio
async def test_s5_system_revoke_uses_stable_control_identity(
    db_path: str,
) -> None:
    await _pause_with_clarification(db_path)
    factory = _s5_factory(db_path)
    prepared = await factory.record_source_decision(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        source_kind=DecisionSourceKind.SYSTEM_CONTROL,
        source_id="__auto_control__",
        action=DecisionAction.REVOKE,
        principals=DecisionPrincipalSet(
            "u_1",
            "system:authorization-revocation",
            "u_1",
        ),
        source_values={"denial_reason_code": "access_revoked"},
    )
    assert prepared.decision.context.source_id == "t_1:1:control"
    _bump_user_auth_epoch(db_path, marker="requester-disabled")
    await factory.commit_decision_control(
        prepared,
        reason_code="access_revoked",
        message_key="turn.access_revoked",
    )

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT status FROM turn_terminal_receipts WHERE generation=2").fetchone() == ("aborted",)


@pytest.mark.asyncio
async def test_s5_web_cancel_recovers_after_record_before_commit(
    db_path: str,
) -> None:
    await _pause_with_clarification(db_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    factory = _s5_factory(db_path, store=store)
    await factory.record_source_decision(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        source_kind=DecisionSourceKind.SYSTEM_CONTROL,
        source_id="__auto_control__",
        action=DecisionAction.CANCEL,
        principals=DecisionPrincipalSet(
            "u_1",
            "system:web-cancel",
            "u_1",
        ),
    )

    class SessionStore:
        async def get_session(self, session_id: str):
            assert session_id == "s_1"
            return SimpleNamespace(
                user_id="u_1",
                workspace_id="ws_1",
                deleted_at=None,
            )

    class UserStore:
        async def get_user(self, user_id: str):
            assert user_id == "u_1"
            return SimpleNamespace(id="u_1", status="active")

        async def get_team_role(self, user_id: str, team_id: str):
            assert (user_id, team_id) == ("u_1", "team_1")
            return "member"

        async def get_workspace_team_id(self, workspace_id: str):
            assert workspace_id == "ws_1"
            return "team_1"

        async def get_member_role(self, workspace_id: str, user_id: str):
            assert (workspace_id, user_id) == ("ws_1", "u_1")
            return "member"

        async def get_workspace(self, workspace_id: str):
            assert workspace_id == "ws_1"
            return {
                "id": "ws_1",
                "team_id": "team_1",
                "status": "active",
            }

    class FrozenEntitlements:
        calls = 0

        async def assert_can_run_turn(self, _team_id: str):
            self.calls += 1
            raise AssertionError("safe-stop recovery must not consult run entitlement")

    app = SimpleNamespace(
        state=SimpleNamespace(
            session_store=SessionStore(),
            user_store=UserStore(),
            access_control_store=None,
            entitlements=FrozenEntitlements(),
        )
    )
    authorization = TerminalPendingControlAuthorization(app=app)
    result = await TerminalRecoveryProcessor(
        store,
        pending_control_authorizer=authorization,
    ).run_bounded(
        owner_instance_id="gateway_1",
        rollout_config_generation="rollout_1",
        writer_fence=TerminalWriterLease(
            owner_instance_id="gateway_1",
            writer_lease_epoch=7,
            writer_admission_epoch=3,
            rollout_config_generation="rollout_1",
            lease_expires_at=(_NOW + timedelta(minutes=5)).isoformat(),
            admission_state="open",
        ),
    )

    assert result.errors == ()
    assert app.state.entitlements.calls == 0
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT status,reason_code FROM turn_terminal_receipts WHERE generation=2").fetchone() == (
            "cancelled",
            "user_cancelled",
        )
    session_store = DurableSessionStore(db_path)
    assert (
        await session_store.record_terminal_cancel_receipt(
            team_id="team_1",
            user_id="u_1",
            cancel_request_id="f01af3ae-ae71-58bc-8b67-cb07d2076406",
            workspace_id="ws_1",
            turn_id="t_1",
            expected_execution_generation=2,
            outcome="cancelled",
        )
        == "cancelled"
    )
    assert (
        await session_store.record_terminal_cancel_receipt(
            team_id="team_1",
            user_id="u_1",
            cancel_request_id="2e8bc8e7-d383-59c7-9aaf-c0ddf24f934c",
            workspace_id="ws_1",
            turn_id="t_1",
            expected_execution_generation=1,
            outcome="generation_conflict",
        )
        == "generation_conflict"
    )


@pytest.mark.asyncio
async def test_s5_control_stop_cancels_plan_before_commit_ack(
    db_path: str,
) -> None:
    await _pause_with_clarification(db_path)
    plans = PlanStore(db_path)
    await plans.initialize()
    plan = await plans.create_plan(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        title="Paused Stop",
    )
    assert await plans.try_claim_running(plan.id, "t_1")
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    factory = _s5_factory(db_path, store=store)
    prepared = await factory.record_source_decision(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        source_kind=DecisionSourceKind.SYSTEM_CONTROL,
        source_id="__auto_control__",
        action=DecisionAction.CANCEL,
        principals=DecisionPrincipalSet(
            "u_1",
            "system:web-cancel",
            "u_1",
        ),
    )

    def lose_commit_ack(point: TerminalProtocolFaultPoint) -> None:
        if point is TerminalProtocolFaultPoint.CONTROL_AFTER_RECEIPT_BEFORE_ACK:
            raise RuntimeError("control commit acknowledgement lost")

    store._fault_injector = lose_commit_ack
    with pytest.raises(
        RuntimeError,
        match="control commit acknowledgement lost",
    ):
        await factory.commit_decision_control(
            prepared,
            reason_code="user_cancelled",
            message_key="turn.cancelled",
        )

    current = await plans.get_plan(
        plan.id,
        user_id="u_1",
        workspace_id="ws_1",
    )
    assert current is not None
    assert current.status == PlanStatus.CANCELLED
    assert current.execution_turn_id == "t_1"

    # Historical route/Store split residue converges to the explicit Stop
    # outcome, rather than making a cancelled Plan retryable as BLOCKED.
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE plans SET status='running',updated_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (plan.id,),
        )
        connection.commit()
    assert await plans.recover_orphans() == (1, 0)
    repaired = await plans.get_plan(
        plan.id,
        user_id="u_1",
        workspace_id="ws_1",
    )
    assert repaired is not None
    assert repaired.status == PlanStatus.CANCELLED


@pytest.mark.asyncio
async def test_s5_control_recovery_auth_toctou_never_commits(
    db_path: str,
) -> None:
    original = await _pause_with_clarification(db_path)
    request = replace(
        original,
        context=replace(
            original.context,
            decision_id=stable_decision_id(
                scope=original.context.scope,
                source_kind=DecisionSourceKind.CLARIFICATION,
                source_id="clarify_1",
                action=DecisionAction.DISMISS,
            ),
            action=DecisionAction.DISMISS,
        ),
        decision_payload_digest=canonical_decision_payload_digest_for(
            source_kind=DecisionSourceKind.CLARIFICATION,
            action=DecisionAction.DISMISS,
            expected_source_state="pending",
            target_source_state="dismissed",
            source_values_json=None,
        ),
        target_source_state="dismissed",
        source_values_json=None,
    )
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    await store.record_decision(request)

    async def mutate_auth(_decision, _preflight) -> None:
        _bump_terminal_auth_epoch(db_path, marker="control-auth-gap")

    result = await TerminalRecoveryProcessor(
        store,
        pending_control_authorizer=mutate_auth,
    ).run_bounded(
        owner_instance_id="gateway_1",
        rollout_config_generation="rollout_1",
        writer_fence=TerminalWriterLease(
            owner_instance_id="gateway_1",
            writer_lease_epoch=7,
            writer_admission_epoch=3,
            rollout_config_generation="rollout_1",
            lease_expires_at=(_NOW + timedelta(minutes=5)).isoformat(),
            admission_state="open",
        ),
    )

    assert result.processed_count == 0
    assert result.errors
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_receipts WHERE generation=2").fetchone() == (0,)
        assert connection.execute("SELECT state FROM turn_control_decisions").fetchone() == ("decision_recorded",)


@pytest.mark.asyncio
async def test_s5_superseded_live_effect_expires_to_skipped(
    db_path: str,
) -> None:
    original = await _pause_with_clarification(db_path)
    effect = DecisionSideEffectPlan.from_payload(
        decision_id=original.context.decision_id,
        effect_kind="clarification_projection",
        target_ref="clarify_1",
        payload={"remember": True},
    )
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    recorded = await store.record_decision(replace(original, side_effects=(effect,)))
    claimed = await store.claim_decision_effects(
        decision_id=recorded.context.decision_id,
        owner="crashed-effect-worker",
        expected_authz_snapshot_digest=_AUTH_DIGEST,
    )
    assert len(claimed) == 1
    revoked, preflight = await store.supersede_pending_decision_with_revoke(
        recorded,
    )
    await _s5_factory(db_path, store=store).commit_decision_control(
        TerminalPreparedDecision(revoked, preflight),
        reason_code="access_revoked",
        message_key="turn.access_revoked",
    )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state FROM turn_decision_effects").fetchone() == ("claimed",)

    expired_store = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW + timedelta(seconds=31),
    )
    recovery = TerminalClarificationEffectRecovery(
        app=SimpleNamespace(state=SimpleNamespace()),
        protocol_store=expired_store,
    )
    assert await recovery(recorded.context.decision_id) == 1
    assert await expired_store.count_recovery_inventory() == 0
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state,resolution_code FROM turn_decision_effects").fetchone() == ("skipped", "access_revoked")


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt_state", [EffectState.DISPATCHED, EffectState.UNKNOWN])
async def test_s5_approval_replay_after_provider_boundary_skips_new_auth_denial(
    db_path: str,
    attempt_state: EffectState,
) -> None:
    approvals, approval = await _pause_with_approval(db_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    factory = _s5_factory(db_path, store=store)
    prepared = await factory.record_source_decision(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        source_kind=DecisionSourceKind.APPROVAL,
        source_id=approval.id,
        action=DecisionAction.APPROVE,
        principals=DecisionPrincipalSet("u_1", "u_approver", "u_approver"),
        source_values={"arguments_digest": approval.arguments_digest or ""},
    )
    approved = await approvals.get(approval.id)
    assert approved is not None
    runtime = await factory.open_decision_runtime(
        prepared,
        TurnRequest(
            session_id="s_1",
            workspace_id="ws_1",
            user_id="u_approver",
            member_uid="m_approver",
            text=f"approval:{approval.id}",
            turn_id="t_1",
            team_id="team_1",
            resume_turn_id="t_1",
        ),
    )
    await runtime.start_execution()
    action_ref = stable_approval_effect_action_ref(
        decision_id=prepared.decision.context.decision_id,
        approval_id=approval.id,
        arguments_digest=approval.arguments_digest,
    )
    attempt = await store.begin_effect_attempt(
        runtime.execution,
        attempt_id=("tea_" + canonical_digest_v1({"action_ref": action_ref, "kind": "approval"})[:32]),
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="approval:fake_write",
        action_ref=action_ref,
        idempotency_or_fence_token=(prepared.decision.context.decision_id),
        non_retriable=False,
    )
    attempt = (await store.claim_effect_dispatch(runtime.execution, attempt)).attempt
    if attempt_state is EffectState.UNKNOWN:
        attempt = await store.mark_effect_unknown(attempt)
    else:
        await approvals.record_execution(
            approval_id=approval.id,
            ok=True,
            result={"updated": 1},
            error=None,
        )
    _bump_user_auth_epoch(db_path, marker="access-lost-after-dispatch")

    class DenyIfCalled:
        calls = 0

        async def authorize(self, *_args, **_kwargs):
            self.calls += 1
            raise DecisionAuthorizationDeniedError("access_revoked")

    class NeverCalledExecutor:
        calls = 0

        async def execute_effect(self, *_args, **_kwargs):
            self.calls += 1
            return True, {}, None

    authorizer = DenyIfCalled()
    executor = NeverCalledExecutor()
    runner = TerminalApprovalRunner(
        factory=factory,
        approval_store=approvals,
        authorizer=authorizer,
    )
    if attempt_state is EffectState.UNKNOWN:
        with pytest.raises(
            RuntimeError,
            match="effect_recovery_pending",
        ):
            await runner.execute(
                prepared=prepared,
                approval=approved,
                legacy_executor=executor,
            )
    else:
        projected = await runner.execute(
            prepared=prepared,
            approval=approved,
            legacy_executor=executor,
        )
        assert projected.status == "executed"
    assert authorizer.calls == 0
    assert executor.calls == 0
    with sqlite3.connect(db_path) as connection:
        if attempt_state is EffectState.UNKNOWN:
            assert connection.execute("SELECT COUNT(*) FROM turn_terminal_receipts WHERE generation=2").fetchone() == (0,)
        else:
            assert connection.execute("SELECT status FROM turn_terminal_receipts WHERE generation=2").fetchone() == ("completed",)


@pytest.mark.asyncio
async def test_s5_owned_empty_recovery_sweep_converges_without_lease_due(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    result = await TerminalRecoveryProcessor(store).run_bounded(
        owner_instance_id="gateway_1",
        rollout_config_generation="rollout_1",
        writer_fence=TerminalWriterLease(
            owner_instance_id="gateway_1",
            writer_lease_epoch=7,
            writer_admission_epoch=3,
            rollout_config_generation="rollout_1",
            lease_expires_at=(_NOW + timedelta(minutes=5)).isoformat(),
            admission_state="open",
        ),
    )

    assert result.eligible_remaining == 0
    assert result.next_due_at is None
    assert result.max_due_at is None
    for invalid in (1, 2):
        with pytest.raises(ValueError, match="bounds"):
            await TerminalRecoveryProcessor(store).run_bounded(
                owner_instance_id="gateway_1",
                rollout_config_generation="rollout_1",
                max_items=invalid,
            )


@pytest.mark.asyncio
async def test_s5_real_clarification_expiry_commits_one_aborted_n_plus_one(
    db_path: str,
) -> None:
    await _pause_with_clarification(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE clarification_requests SET created_at='2020-01-01T00:00:00Z' WHERE request_id='clarify_1'")
        connection.commit()
    protocol_store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    clarification_store = ClarificationStore(db_path)
    await clarification_store.initialize()
    app = SimpleNamespace(
        state=SimpleNamespace(
            terminal_runtime_factory=TerminalRuntimeFactory(
                protocol_store,
                GenerationEventBusRegistry(),
                TerminalRuntimeRegistry(),
                TerminalWriterSnapshot(
                    "gateway_1",
                    7,
                    3,
                    "rollout_1",
                ),
            ),
            terminal_protocol_store=protocol_store,
            clarification_store=clarification_store,
            approval_store=None,
        )
    )
    from bowhead_gateway.terminal_control_coordinator import (
        expire_due_terminal_decisions,
    )

    assert await expire_due_terminal_decisions(app, limit=10) == 1
    assert await expire_due_terminal_decisions(app, limit=10) == 0
    assert await clarification_store.expire_old(ttl_hours=1) == 0
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT status FROM clarification_requests WHERE request_id='clarify_1'").fetchone() == ("expired",)
        assert connection.execute("SELECT generation,state,execution_started_at FROM turn_generations ORDER BY generation").fetchall() == [
            (1, "closed", _NOW.isoformat(timespec="microseconds").replace("+00:00", "Z")),
            (2, "closed", None),
        ]
        assert connection.execute("SELECT generation,status,reason_code FROM turn_terminal_receipts ORDER BY generation").fetchall() == [
            (1, "clarification_pending", None),
            (2, "aborted", "decision_expired"),
        ]
        assert connection.execute("SELECT COUNT(*) FROM turn_control_decisions").fetchone() == (1,)


@pytest.mark.asyncio
async def test_s5_real_approval_expiry_commits_one_aborted_n_plus_one(
    db_path: str,
) -> None:
    approvals, approval = await _pause_with_approval(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE approvals SET created_at='2020-01-01T00:00:00Z' WHERE id=?",
            (approval.id,),
        )
        connection.commit()
    protocol_store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    app = SimpleNamespace(
        state=SimpleNamespace(
            terminal_runtime_factory=TerminalRuntimeFactory(
                protocol_store,
                GenerationEventBusRegistry(),
                TerminalRuntimeRegistry(),
                TerminalWriterSnapshot(
                    "gateway_1",
                    7,
                    3,
                    "rollout_1",
                ),
            ),
            terminal_protocol_store=protocol_store,
            clarification_store=None,
            approval_store=approvals,
        )
    )
    from bowhead_gateway.terminal_control_coordinator import (
        expire_due_terminal_decisions,
    )

    assert await expire_due_terminal_decisions(app, limit=10) == 1
    assert await expire_due_terminal_decisions(app, limit=10) == 0
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT status FROM approvals WHERE id=?",
            (approval.id,),
        ).fetchone() == ("expired",)
        assert connection.execute("SELECT generation,status,reason_code FROM turn_terminal_receipts ORDER BY generation").fetchall() == [
            (1, "approval_pending", None),
            (2, "aborted", "decision_expired"),
        ]
        assert connection.execute("SELECT COUNT(*) FROM turn_generations").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM turn_control_decisions").fetchone() == (1,)


async def _record_clarification_effect(
    db_path: str,
) -> tuple[TerminalProtocolStore, str]:
    original = await _pause_with_clarification(db_path)
    effect = DecisionSideEffectPlan.from_payload(
        decision_id=original.context.decision_id,
        effect_kind="clarification_projection",
        target_ref="clarify_1",
        payload={"remember": True},
    )
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    recorded = await store.record_decision(replace(original, side_effects=(effect,)))
    return store, recorded.context.decision_id


@pytest.mark.asyncio
async def test_s5_decision_effect_allows_legal_epoch_refresh(
    db_path: str,
) -> None:
    store, decision_id = await _record_clarification_effect(db_path)
    _bump_terminal_auth_epoch(db_path, marker="effect-still-allowed")
    clarification_store = ClarificationStore(db_path)
    await clarification_store.initialize()

    class Permit:
        async def authorize(self, *_args, **_kwargs):
            return None

    applied = await TerminalClarificationEffectRecovery(
        app=SimpleNamespace(
            state=SimpleNamespace(
                clarification_store=clarification_store,
            )
        ),
        protocol_store=store,
        authorizer=Permit(),
    )(decision_id)

    assert applied == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state,resolution_code FROM turn_decision_effects").fetchone() == ("applied", None)


@pytest.mark.asyncio
async def test_s5_decision_effect_authorization_toctou_blocks_claim(
    db_path: str,
) -> None:
    store, decision_id = await _record_clarification_effect(db_path)
    clarification_store = ClarificationStore(db_path)
    await clarification_store.initialize()

    class MutateAfterPreflight:
        async def authorize(self, *_args, **_kwargs):
            _bump_terminal_auth_epoch(
                db_path,
                marker="effect-auth-gap",
            )

    with pytest.raises(
        DecisionAccessRevokedError,
        match="authorization changed",
    ):
        await TerminalClarificationEffectRecovery(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    clarification_store=clarification_store,
                )
            ),
            protocol_store=store,
            authorizer=MutateAfterPreflight(),
        )(decision_id)

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state,claim_owner FROM turn_decision_effects").fetchone() == ("pending", None)


@pytest.mark.asyncio
async def test_s5_decision_effect_permanent_denial_is_finitely_skipped(
    db_path: str,
) -> None:
    store, decision_id = await _record_clarification_effect(db_path)
    clarification_store = ClarificationStore(db_path)
    await clarification_store.initialize()

    class Deny:
        async def authorize(self, *_args, **_kwargs):
            raise DecisionAuthorizationDeniedError("entitlement_denied")

    skipped = await TerminalClarificationEffectRecovery(
        app=SimpleNamespace(
            state=SimpleNamespace(
                clarification_store=clarification_store,
            )
        ),
        protocol_store=store,
        authorizer=Deny(),
    )(decision_id)

    assert skipped == 1
    assert await store.count_recovery_inventory() == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state,resolution_code FROM turn_decision_effects").fetchone() == ("skipped", "entitlement_denied")
