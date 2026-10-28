from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from bowhead_core.terminal_protocol import (
    EffectClass,
    EffectState,
    FinalAnswerMode,
    TerminalCandidatePlan,
    TerminalProtocolMalformedError,
    TerminalStatus,
    canonical_digest_v1,
    canonical_json_v1,
)
from bowhead_core.terminal_protocol_store import (
    TerminalProtocolFaultPoint,
    TerminalProtocolStore,
)
from bowhead_core.terminal_reconciler import (
    TerminalReconciler,
    TerminalRecoveryProcessor,
)
from test_terminal_effect_contract import (
    _cancelled_normal_plan,
    _cancelled_recovery_plan,
    _provider_settlement,
    _recovery_writer,
)
from test_terminal_finalizer import (
    _NOW,
    _completed_plan,
    _create_db,
    _done,
    _start_execution,
    _with_effect_disclosure,
)
from test_terminal_local_billing import (
    _balances,
    _billing_rows,
    _claim_recovery,
    _v2_intent,
)


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    path = _create_db(tmp_path)
    with sqlite3.connect(path) as connection:
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
    return path


def _status_plan(
    scope,
    status: TerminalStatus,
) -> TerminalCandidatePlan:
    base = _completed_plan(scope)
    if status is TerminalStatus.COMPLETED:
        return base
    reason, message_key = {
        TerminalStatus.FAILED: (
            "terminal_protocol_malformed",
            "turn.failed",
        ),
        TerminalStatus.ABORTED: ("worker_start_failed", "turn.aborted"),
        TerminalStatus.CLARIFICATION_PENDING: (
            None,
            "turn.clarification_pending",
        ),
        TerminalStatus.APPROVAL_PENDING: (
            None,
            "turn.approval_pending",
        ),
    }[status]
    primary = base.primary_variant
    assert primary is not None
    primary = replace(
        primary,
        status=status,
        reason_code=reason,
        message_key=message_key,
        final_answer_mode=FinalAnswerMode.NONE,
        final_answer_event_id=None,
        final_answer_digest=None,
        events=(
            _done(
                event_id=base.reserved_terminal_event_id,
                candidate_id=base.candidate_id,
                receipt_id=base.reserved_receipt_id,
                variant_key=primary.variant_key,
                status=status,
                reason=reason,
                message_key=message_key,
                manifest_digest=canonical_digest_v1([]),
            ),
        ),
    )
    recovery = base.variants[1]
    return replace(
        base,
        variants=tuple(
            sorted(
                (primary, recovery),
                key=lambda variant: variant.variant_key,
            )
        ),
        session_disposition_plan={
            "turn_projection": {"intent": status.value},
            "variant_templates": {
                primary.variant_key: {},
                recovery.variant_key: {},
            },
        },
    )


async def _record_usage(
    store: TerminalProtocolStore,
    execution,
    *,
    tokens: int,
) -> None:
    await store.record_execution_usage(
        execution,
        input_tokens=tokens,
        output_tokens=0,
        cost_amount=0.0 if tokens == 0 else 0.25,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        TerminalStatus.COMPLETED,
        TerminalStatus.FAILED,
        TerminalStatus.ABORTED,
        TerminalStatus.CLARIFICATION_PENDING,
        TerminalStatus.APPROVAL_PENDING,
    ],
)
@pytest.mark.parametrize("tokens", [0, 700])
async def test_contract_v2_every_normal_status_binds_frozen_billing(
    db_path: str,
    status: TerminalStatus,
    tokens: int,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(
        store,
        execution_intent=_v2_intent(),
    )
    await _record_usage(store, execution, tokens=tokens)
    billing = await store.freeze_usage_and_claim_billing_attempt(execution)
    if billing is not None:
        await store.record_local_billing_settlement(execution, billing)
    finalization = await store.begin_finalization(
        execution,
        finalization_owner_instance_id="gateway_1:finalizer",
        closing_reason=f"root_done:{status.value}",
    )
    snapshot = await store.read_effect_disclosure_snapshot(execution.scope)
    assert snapshot is not None and snapshot.protocol == 2
    plan = replace(
        _with_effect_disclosure(_status_plan(execution.scope, status), snapshot),
        projection_extensions=snapshot.projection_extensions,
    )
    prepared = await store.prepare_candidate(finalization, plan)
    primary = plan.primary_variant
    assert primary is not None
    receipt = await store.commit_candidate(
        prepared,
        plan,
        selected_variant_key=primary.variant_key,
        authz_snapshot_digest="auth_1",
        writer_admission_epoch=3,
    )

    assert receipt.status is status
    ledger, settlements = _billing_rows(db_path)
    assert len(ledger) == (1 if tokens else 0)
    assert len(settlements) == (1 if tokens else 0)


@pytest.mark.asyncio
async def test_normal_finalization_fails_before_usage_and_billing_settle(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(
        store,
        execution_intent=_v2_intent(),
    )
    await _record_usage(store, execution, tokens=700)

    with pytest.raises(
        TerminalProtocolMalformedError,
        match="usage is not frozen",
    ):
        await store.begin_finalization(
            execution,
            finalization_owner_instance_id="gateway_1:finalizer",
            closing_reason="completed",
        )
    billing = await store.freeze_usage_and_claim_billing_attempt(execution)
    assert billing is not None
    with pytest.raises(
        TerminalProtocolMalformedError,
        match="local billing is not settled",
    ):
        await store.begin_finalization(
            execution,
            finalization_owner_instance_id="gateway_1:finalizer",
            closing_reason="completed",
        )
    await store.record_local_billing_settlement(execution, billing)
    await store.begin_finalization(
        execution,
        finalization_owner_instance_id="gateway_1:finalizer",
        closing_reason="completed",
    )


@pytest.mark.asyncio
async def test_failed_receipt_binds_applied_tool_and_accounting(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(
        store,
        execution_intent=_v2_intent(),
    )
    await _record_usage(store, execution, tokens=700)
    intent = await store.begin_effect_attempt(
        execution,
        attempt_id="eff_failed_applied",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="tool_external/v1",
        action_ref="tool:failed-applied",
        idempotency_or_fence_token="failed-applied-key",
        non_retriable=False,
    )
    dispatched = await store.mark_effect_dispatched(execution, intent)
    unknown = await store.mark_effect_unknown(dispatched)
    await store.record_provider_effect_settlement(
        _provider_settlement(
            execution.scope,
            attempt_id=unknown.attempt_id,
            row_version=unknown.row_version,
        )
    )
    billing = await store.freeze_usage_and_claim_billing_attempt(execution)
    assert billing is not None
    await store.record_local_billing_settlement(execution, billing)
    finalization = await store.begin_finalization(
        execution,
        finalization_owner_instance_id="gateway_1:finalizer",
        closing_reason="root_done:failed",
    )
    snapshot = await store.read_effect_disclosure_snapshot(execution.scope)
    assert snapshot is not None
    assert snapshot.kind_outcome_counts["accounting"]["applied"] == 1
    assert snapshot.kind_outcome_counts["user_action"]["applied"] == 1
    plan = replace(
        _with_effect_disclosure(
            _status_plan(execution.scope, TerminalStatus.FAILED),
            snapshot,
        ),
        projection_extensions=snapshot.projection_extensions,
    )
    prepared = await store.prepare_candidate(finalization, plan)
    receipt = await store.commit_candidate(
        prepared,
        plan,
        selected_variant_key="primary_none",
        authz_snapshot_digest="auth_1",
        writer_admission_epoch=3,
    )
    assert receipt.status is TerminalStatus.FAILED


@pytest.mark.asyncio
async def test_stop_discards_intent_freezes_dispatch_and_defers_settlement(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(
        store,
        execution_intent=_v2_intent(),
    )
    await _record_usage(store, execution, tokens=1200)
    unclaimed = await store.begin_effect_attempt(
        execution,
        attempt_id="eff_stop_intent",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="tool_external/v1",
        action_ref="tool:stop-intent",
        idempotency_or_fence_token="stop-intent-key",
        non_retriable=False,
    )
    claimed = await store.begin_effect_attempt(
        execution,
        attempt_id="eff_stop_dispatched",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="tool_external/v1",
        action_ref="tool:stop-dispatched",
        idempotency_or_fence_token="stop-dispatched-key",
        non_retriable=False,
    )
    dispatched = await store.mark_effect_dispatched(execution, claimed)
    finalization = await store.begin_cancelled_finalization_with_local_billing(
        execution,
        finalization_owner_instance_id="gateway_1:stop",
    )
    replay = await store.begin_cancelled_finalization_with_local_billing(
        execution,
        finalization_owner_instance_id="gateway_1:stop",
    )
    assert replay == finalization
    assert (
        await store.read_effect_attempt(
            execution.scope,
            action_ref=unclaimed.action_ref,
        )
        is None
    )
    unknown = await store.read_effect_attempt(
        execution.scope,
        action_ref=dispatched.action_ref,
    )
    assert unknown is not None and unknown.state is EffectState.UNKNOWN
    assert _balances(db_path) == (98, 98)
    with pytest.raises(
        TerminalProtocolMalformedError,
        match="waits for terminal receipt",
    ):
        await store.record_provider_effect_settlement(
            _provider_settlement(
                execution.scope,
                attempt_id=unknown.attempt_id,
                row_version=unknown.row_version,
            )
        )
    with pytest.raises(
        TerminalProtocolMalformedError,
        match="ownership fence changed",
    ):
        await store.claim_effect_dispatch(execution, unclaimed)

    snapshot = await store.read_effect_disclosure_snapshot(execution.scope)
    assert snapshot is not None and snapshot.unsettled_count == 1
    plan = _cancelled_normal_plan(execution.scope, snapshot)
    prepared = await store.prepare_candidate(finalization, plan)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turn_generations SET finalization_lease_expires_at=? WHERE turn_id='t_1'",
            ((_NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),),
        )
        connection.commit()
    reconciler = TerminalReconciler(store)
    processing = await reconciler.claim(
        (await reconciler.scan(scanner_id="scanner_1"))[0],
        owner_instance_id="reconciler_1",
    )
    receipt = await reconciler.converge(
        processing,
        candidate_builder=lambda _context: pytest.fail(
            "prepared cancellation must be reused",
        ),
        selected_variant_key="recovery_none",
        authz_snapshot_digest="auth_recovery",
        writer_admission_epoch=3,
    )
    assert receipt.status is TerminalStatus.CANCELLED
    settled = await store.record_provider_effect_settlement(
        _provider_settlement(
            execution.scope,
            attempt_id=unknown.attempt_id,
            row_version=unknown.row_version,
        )
    )
    assert settled.state is EffectState.SETTLED
    overlay = await store.read_current_effect_disclosure(execution.scope)
    assert overlay is not None and overlay.finalized
    assert prepared.candidate_id == plan.candidate_id


@pytest.mark.asyncio
async def test_stop_transaction_fault_rolls_back_every_mutation(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(
        store,
        execution_intent=_v2_intent(),
    )
    await _record_usage(store, execution, tokens=700)
    intent = await store.begin_effect_attempt(
        execution,
        attempt_id="eff_stop_fault",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="tool_external/v1",
        action_ref="tool:stop-fault",
        idempotency_or_fence_token="stop-fault-key",
        non_retriable=False,
    )

    def inject(point: TerminalProtocolFaultPoint) -> None:
        if point is TerminalProtocolFaultPoint.CANCEL_FINALIZATION_BEFORE_COMMIT:
            raise RuntimeError("stop transaction crash")

    faulting = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=inject,
    )
    with pytest.raises(RuntimeError, match="stop transaction crash"):
        await faulting.begin_cancelled_finalization_with_local_billing(
            execution,
            finalization_owner_instance_id="gateway_1:stop",
        )
    with sqlite3.connect(db_path) as connection:
        generation = connection.execute(
            "SELECT state,usage_digest,closing_reason FROM turn_generations WHERE turn_id='t_1'",
        ).fetchone()
        attempts = connection.execute(
            "SELECT attempt_id,state FROM turn_effect_attempts",
        ).fetchall()
    assert generation == ("running", None, None)
    assert attempts == [(intent.attempt_id, EffectState.INTENT.value)]
    assert _billing_rows(db_path) == ([], [])
    assert _balances(db_path) == (100, 100)

    await store.begin_cancelled_finalization_with_local_billing(
        execution,
        finalization_owner_instance_id="gateway_1:stop",
    )
    assert _balances(db_path) == (99, 99)


@pytest.mark.asyncio
async def test_stop_first_rejects_late_inference_usage_and_billing(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(
        store,
        execution_intent=_v2_intent(),
    )
    await store.begin_cancelled_finalization_with_local_billing(
        execution,
        finalization_owner_instance_id="gateway_1:stop",
    )

    with pytest.raises(
        TerminalProtocolMalformedError,
        match="ownership fence changed",
    ):
        await _record_usage(store, execution, tokens=700)
    with pytest.raises(
        TerminalProtocolMalformedError,
        match="ownership fence changed",
    ):
        await store.begin_effect_attempt(
            execution,
            attempt_id="eff_late_llm",
            effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
            provider="llm_provider/v1",
            action_ref="llm:late-after-stop",
            idempotency_or_fence_token="late-llm-key",
            non_retriable=False,
        )
    assert _billing_rows(db_path) == ([], [])
    assert _balances(db_path) == (100, 100)


@pytest.mark.asyncio
async def test_stop_exhausted_settlement_overdraws_and_does_not_rebill_after_grant(
    db_path: str,
) -> None:
    """余额已 0 时 Stop 的已发生用量照实扣到负数；结算是终态，事后 grant 不会再扣一次。"""
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE users SET credit_balance=0 WHERE id='u_1'")
        connection.execute(
            "UPDATE teams SET team_credit_balance=0 WHERE id='team_1'",
        )
        connection.commit()
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(
        store,
        execution_intent=_v2_intent(),
    )
    await _record_usage(store, execution, tokens=700)
    finalization = await store.begin_cancelled_finalization_with_local_billing(
        execution,
        finalization_owner_instance_id="gateway_1:stop",
    )
    assert len(_billing_rows(db_path)[0]) == 1
    assert _billing_rows(db_path)[1][0][0] == "confirmed_applied"
    assert _balances(db_path) == (-1, -1)
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE users SET credit_balance=10 WHERE id='u_1'")
        connection.execute(
            "UPDATE teams SET team_credit_balance=10 WHERE id='team_1'",
        )
        connection.commit()
    assert (
        await store.begin_cancelled_finalization_with_local_billing(
            execution,
            finalization_owner_instance_id="gateway_1:stop",
        )
        == finalization
    )
    assert _balances(db_path) == (10, 10)
    assert len(_billing_rows(db_path)[0]) == 1


@pytest.mark.asyncio
async def test_recovery_stop_rejects_stale_owner_and_commits_cancelled(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(
        store,
        execution_intent=_v2_intent(),
    )
    await _record_usage(store, execution, tokens=700)
    attempt = await store.begin_effect_attempt(
        execution,
        attempt_id="eff_recovery_stop",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="tool_external/v1",
        action_ref="tool:recovery-stop",
        idempotency_or_fence_token="recovery-stop-key",
        non_retriable=False,
    )
    await store.mark_effect_dispatched(execution, attempt)
    stale = await _claim_recovery(store, execution)
    with sqlite3.connect(db_path) as connection:
        expired = (
            (_NOW - timedelta(seconds=1))
            .isoformat()
            .replace(
                "+00:00",
                "Z",
            )
        )
        connection.execute(
            "UPDATE turn_recovery_records SET recovery_lease_expires_at=? WHERE recovery_id=?",
            (expired, stale.recovery_id),
        )
        connection.execute(
            "UPDATE turn_generations SET finalization_lease_expires_at=? WHERE turn_id='t_1'",
            (expired,),
        )
        connection.commit()
    record = await store.discover_recovery(
        (await store.list_recovery_candidates(scanner_id="scanner_takeover"))[0],
    )
    processing = await store.claim_recovery(
        record,
        owner_instance_id="recovery_takeover",
    )
    with pytest.raises(
        TerminalProtocolMalformedError,
        match="ownership fence changed",
    ):
        await store.begin_cancelled_finalization_with_local_billing(
            replace(processing, cancelled_finalization=True),
        )
    with pytest.raises(
        TerminalProtocolMalformedError,
        match="ownership fence changed",
    ):
        await store.begin_cancelled_finalization_with_local_billing(stale)
    cancelled_processing = await store.begin_cancelled_finalization_with_local_billing(processing)
    assert cancelled_processing.cancelled_finalization is True
    assert cancelled_processing.generation_row_version == processing.generation_row_version + 1
    with pytest.raises(
        TerminalProtocolMalformedError,
        match="execution capability changed",
    ):
        await store.freeze_usage_and_claim_billing_attempt(execution)
    snapshot = await store.read_effect_disclosure_snapshot(execution.scope)
    assert snapshot is not None and snapshot.unsettled_count == 1
    plan = _cancelled_recovery_plan(execution.scope, snapshot)
    with pytest.raises(
        TerminalProtocolMalformedError,
        match="processing fence changed",
    ):
        await store.prepare_recovery_candidate(processing, plan)
    prepared = await store.prepare_recovery_candidate(
        cancelled_processing,
        plan,
    )
    primary = plan.primary_variant
    assert primary is not None
    receipt = await store.commit_recovery_candidate(
        prepared,
        plan,
        selected_variant_key=primary.variant_key,
        authz_snapshot_digest="auth_recovery",
        writer_admission_epoch=3,
    )
    assert receipt.status is TerminalStatus.CANCELLED
    assert _balances(db_path) == (99, 99)
    assert len(_billing_rows(db_path)[0]) == 1


@pytest.mark.asyncio
async def test_recovery_stop_replays_after_commit_ack_loss(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(
        store,
        execution_intent=_v2_intent(),
    )
    await _record_usage(store, execution, tokens=700)
    processing = await _claim_recovery(store, execution)

    def lose_ack(point: TerminalProtocolFaultPoint) -> None:
        if point is TerminalProtocolFaultPoint.FINALIZE_AFTER_CLOSING:
            raise RuntimeError("cancel marker acknowledgement lost")

    faulting = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=lose_ack,
    )
    with pytest.raises(RuntimeError, match="acknowledgement lost"):
        await faulting.begin_cancelled_finalization_with_local_billing(
            processing,
        )

    replay = await store.begin_cancelled_finalization_with_local_billing(
        processing,
    )
    assert replay.cancelled_finalization is True
    assert replay.generation_row_version == processing.generation_row_version + 1
    assert await store.begin_cancelled_finalization_with_local_billing(replay) == replay
    assert len(_billing_rows(db_path)[0]) == 1


@pytest.mark.asyncio
async def test_recovery_processor_settles_interrupted_local_billing(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(
        store,
        execution_intent=_v2_intent(),
    )
    await _record_usage(store, execution, tokens=700)
    billing = await store.freeze_usage_and_claim_billing_attempt(execution)
    assert billing is not None and billing.state is EffectState.DISPATCHED
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turn_generations SET lease_expires_at=? WHERE turn_id='t_1'",
            ((_NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),),
        )
        connection.commit()

    result = await TerminalRecoveryProcessor(store).run_bounded(
        owner_instance_id="gateway_1",
        rollout_config_generation="rollout_1",
        writer_fence=_recovery_writer(),
    )

    assert result.errors == ()
    assert len(_billing_rows(db_path)[0]) == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT status FROM turn_terminal_receipts WHERE turn_id='t_1'",
        ).fetchone() == ("aborted",)


@pytest.mark.asyncio
async def test_stop_marker_rejects_normal_finalization_and_candidate(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(
        store,
        execution_intent=_v2_intent(),
    )
    finalization = await store.begin_cancelled_finalization_with_local_billing(
        execution,
        finalization_owner_instance_id="gateway_1:stop",
    )
    with pytest.raises(
        TerminalProtocolMalformedError,
        match="reason changed",
    ):
        await store.begin_finalization(
            execution,
            finalization_owner_instance_id="gateway_1:stop",
            closing_reason="root_done:completed",
        )
    snapshot = await store.read_effect_disclosure_snapshot(execution.scope)
    assert snapshot is not None
    completed = replace(
        _with_effect_disclosure(_completed_plan(execution.scope), snapshot),
        projection_extensions=snapshot.projection_extensions,
    )
    with pytest.raises(
        TerminalProtocolMalformedError,
        match="cancellation candidate",
    ):
        await store.prepare_candidate(finalization, completed)


@pytest.mark.asyncio
async def test_prepared_normal_candidate_rejects_late_stop(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(
        store,
        execution_intent=_v2_intent(),
    )
    assert await store.freeze_usage_and_claim_billing_attempt(execution) is None
    finalization = await store.begin_finalization(
        execution,
        finalization_owner_instance_id="gateway_1:finalizer",
        closing_reason="root_done:completed",
    )
    snapshot = await store.read_effect_disclosure_snapshot(execution.scope)
    assert snapshot is not None
    plan = replace(
        _with_effect_disclosure(_completed_plan(execution.scope), snapshot),
        projection_extensions=snapshot.projection_extensions,
    )
    await store.prepare_candidate(finalization, plan)

    with pytest.raises(
        TerminalProtocolMalformedError,
        match="already sealed",
    ):
        await store.begin_cancelled_finalization_with_local_billing(
            execution,
            finalization_owner_instance_id="gateway_1:finalizer",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("with_external_effect", [False, True])
async def test_recovery_processor_preserves_stop_after_transaction_commit(
    db_path: str,
    with_external_effect: bool,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(
        store,
        execution_intent=_v2_intent(),
    )
    await _record_usage(store, execution, tokens=700)
    steer_buffer = [
        {
            "execution_intent": {
                "attachments": [],
                "planning_mode": None,
                "text": "follow up",
                "workspace_id": "ws_1",
            },
            "request_digest": "steer_digest_1",
            "revision": 1,
            "steer_id": "steer_1",
        }
    ]
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE sessions SET steer_buffer_json=?,steer_buffer_revision=1 WHERE id='s_1'",
            (canonical_json_v1(steer_buffer),),
        )
        connection.commit()
    if with_external_effect:
        intent = await store.begin_effect_attempt(
            execution,
            attempt_id="eff_stop_crash",
            effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
            provider="tool_external/v1",
            action_ref="tool:stop-crash",
            idempotency_or_fence_token="stop-crash-key",
            non_retriable=False,
        )
        await store.mark_effect_dispatched(execution, intent)
    await store.begin_cancelled_finalization_with_local_billing(
        execution,
        finalization_owner_instance_id="gateway_1:stop",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turn_generations SET finalization_lease_expires_at=? WHERE turn_id='t_1'",
            ((_NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),),
        )
        connection.commit()

    result = await TerminalRecoveryProcessor(store).run_bounded(
        owner_instance_id="gateway_1",
        rollout_config_generation="rollout_1",
        writer_fence=_recovery_writer(),
    )

    assert result.errors == ()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT status,selected_session_variant FROM turn_terminal_receipts WHERE turn_id='t_1'",
        ).fetchone() == ("cancelled", "discard")
        assert connection.execute(
            "SELECT active_turn_id,steer_buffer_json,steer_buffer_state FROM sessions WHERE id='s_1'",
        ).fetchone() == (None, "[]", "collecting")
    if with_external_effect:
        unknown = await store.read_effect_attempt(
            execution.scope,
            action_ref="tool:stop-crash",
        )
        assert unknown is not None and unknown.state is EffectState.UNKNOWN
        settled = await store.record_provider_effect_settlement(
            _provider_settlement(
                execution.scope,
                attempt_id=unknown.attempt_id,
                row_version=unknown.row_version,
            )
        )
        assert settled.state is EffectState.SETTLED
