from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from bowhead_core.rate_card import RATE_CARD_VERSION
from bowhead_core.terminal_effect_contract import EFFECT_CONTRACT_VERSION
from bowhead_core.terminal_protocol import (
    EffectAttemptContext,
    EffectClass,
    EffectSettlementOutcome,
    EffectState,
    RecoveryProcessingContext,
    TerminalProtocolMalformedError,
)
from bowhead_core.terminal_protocol_store import (
    TerminalProtocolFaultPoint,
    TerminalProtocolStore,
)
from test_terminal_finalizer import (
    _NOW,
    _create_db,
    _recovery_plan,
    _start_execution,
    _with_effect_disclosure,
)


def _v2_intent() -> dict[str, object]:
    return {
        "billing_rate_card_version": RATE_CARD_VERSION,
        "effect_contract_version": EFFECT_CONTRACT_VERSION,
        "text": "hello",
    }


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


async def _execution_with_usage(
    store: TerminalProtocolStore,
    *,
    input_tokens: int = 1200,
    output_tokens: int = 300,
):
    execution = await _start_execution(
        store,
        execution_intent=_v2_intent(),
    )
    await store.record_execution_usage(
        execution,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_amount=0.25,
    )
    return execution


def _billing_rows(db_path: str) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    with sqlite3.connect(db_path) as connection:
        ledger = connection.execute(
            """
            SELECT id,user_id,team_id,workspace_id,turn_id,delta,reason,
                   tokens_input,tokens_output,rate_card_version
            FROM credit_ledger ORDER BY id
            """,
        ).fetchall()
        settlements = connection.execute(
            """
            SELECT s.outcome,s.settlement_code,a.state,a.provider,a.action_ref
            FROM turn_effect_settlements s
            JOIN turn_effect_attempts a ON a.attempt_id=s.attempt_id
            ORDER BY s.settlement_id
            """,
        ).fetchall()
    return ledger, settlements


def _balances(db_path: str) -> tuple[int, int]:
    with sqlite3.connect(db_path) as connection:
        user = connection.execute(
            "SELECT credit_balance FROM users WHERE id='u_1'",
        ).fetchone()
        team = connection.execute(
            "SELECT team_credit_balance FROM teams WHERE id='team_1'",
        ).fetchone()
    assert user is not None and team is not None
    return int(user[0]), int(team[0])


async def _claim_recovery(
    store: TerminalProtocolStore,
    execution,
) -> RecoveryProcessingContext:
    assert await store.handoff_execution_to_recovery(
        execution,
        reason_code="test_local_billing_recovery",
    )
    discoveries = await store.list_recovery_candidates(
        scanner_id="billing_scanner",
    )
    assert len(discoveries) == 1
    record = await store.discover_recovery(discoveries[0])
    return await store.claim_recovery(
        record,
        owner_instance_id="billing_recovery",
    )


@pytest.mark.asyncio
async def test_normal_local_billing_is_stable_and_concurrent_single_winner(
    db_path: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
    assert journal_mode == ("wal",)
    store_a = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    store_b = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _execution_with_usage(store_a)

    attempt = await store_a.freeze_usage_and_claim_billing_attempt(execution)
    assert attempt is not None
    assert attempt.state is EffectState.DISPATCHED
    assert attempt.attempt_id.startswith("tea_bill_")
    assert attempt.action_ref.startswith("billing:")
    assert attempt.idempotency_or_fence_token is not None
    assert attempt.idempotency_or_fence_token.startswith("led_tv1_")

    settled_a, settled_b = await asyncio.gather(
        store_a.record_local_billing_settlement(execution, attempt),
        store_b.record_local_billing_settlement(execution, attempt),
    )
    assert settled_a == settled_b
    assert settled_a.state is EffectState.SETTLED
    assert _balances(db_path) == (98, 98)
    ledger, settlements = _billing_rows(db_path)
    assert ledger == [
        (
            attempt.idempotency_or_fence_token,
            "u_1",
            "team_1",
            "ws_1",
            "t_1",
            -2,
            "turn",
            1200,
            300,
            RATE_CARD_VERSION,
        )
    ]
    assert settlements == [
        (
            EffectSettlementOutcome.CONFIRMED_APPLIED.value,
            "credit_ledger_applied",
            EffectState.SETTLED.value,
            f"credit_ledger/{RATE_CARD_VERSION}",
            attempt.action_ref,
        )
    ]

    replay = await store_a.freeze_usage_and_claim_billing_attempt(execution)
    assert replay == settled_a
    assert await store_a.record_local_billing_settlement(execution, attempt) == settled_a
    await store_a.record_execution_usage(
        execution,
        input_tokens=1200,
        output_tokens=300,
        cost_amount=0.25,
    )
    with pytest.raises(
        TerminalProtocolMalformedError,
        match="already frozen",
    ):
        await store_a.record_execution_usage(
            execution,
            input_tokens=1201,
            output_tokens=300,
            cost_amount=0.25,
        )
    assert _balances(db_path) == (98, 98)


@pytest.mark.asyncio
async def test_local_billing_ledger_and_settlement_roll_back_together(
    db_path: str,
) -> None:
    def inject(point: TerminalProtocolFaultPoint) -> None:
        if point is TerminalProtocolFaultPoint.LOCAL_BILLING_AFTER_LEDGER:
            raise RuntimeError("crash after credit ledger")

    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _execution_with_usage(store, input_tokens=700, output_tokens=0)
    attempt = await store.freeze_usage_and_claim_billing_attempt(execution)
    assert attempt is not None
    faulting = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=inject,
    )

    with pytest.raises(RuntimeError, match="crash after credit ledger"):
        await faulting.record_local_billing_settlement(execution, attempt)
    assert _balances(db_path) == (100, 100)
    assert _billing_rows(db_path) == ([], [])
    persisted = await store.read_effect_attempt(
        execution.scope,
        action_ref=attempt.action_ref,
    )
    assert persisted == attempt

    settled = await store.record_local_billing_settlement(execution, attempt)
    assert settled.state is EffectState.SETTLED
    assert _balances(db_path) == (99, 99)


@pytest.mark.asyncio
async def test_local_billing_conflicting_ledger_fails_without_mutation(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _execution_with_usage(store, input_tokens=700, output_tokens=0)
    attempt = await store.freeze_usage_and_claim_billing_attempt(execution)
    assert attempt is not None and attempt.idempotency_or_fence_token is not None
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO credit_ledger(
              id,user_id,team_id,workspace_id,turn_id,delta,reason,
              tokens_input,tokens_output,created_at,rate_card_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                attempt.idempotency_or_fence_token,
                "u_1",
                "team_1",
                "ws_1",
                "t_1",
                -99,
                "turn",
                700,
                0,
                "t",
                RATE_CARD_VERSION,
            ),
        )
        connection.commit()

    with pytest.raises(
        TerminalProtocolMalformedError,
        match="ledger identity changed",
    ):
        await store.record_local_billing_settlement(execution, attempt)
    assert _balances(db_path) == (100, 100)
    ledger, settlements = _billing_rows(db_path)
    assert len(ledger) == 1
    assert ledger[0][5] == -99
    assert settlements == []


@pytest.mark.asyncio
async def test_local_billing_commit_ack_loss_replays_exactly_once(
    db_path: str,
) -> None:
    def inject(point: TerminalProtocolFaultPoint) -> None:
        if point is TerminalProtocolFaultPoint.LOCAL_BILLING_AFTER_COMMIT_BEFORE_ACK:
            raise RuntimeError("billing ACK lost")

    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _execution_with_usage(store, input_tokens=1000, output_tokens=1)
    attempt = await store.freeze_usage_and_claim_billing_attempt(execution)
    assert attempt is not None
    faulting = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=inject,
    )

    with pytest.raises(RuntimeError, match="ACK lost"):
        await faulting.record_local_billing_settlement(execution, attempt)
    assert _balances(db_path) == (98, 98)
    settled = await store.record_local_billing_settlement(execution, attempt)
    assert settled.state is EffectState.SETTLED
    assert _balances(db_path) == (98, 98)
    assert len(_billing_rows(db_path)[0]) == 1


@pytest.mark.asyncio
async def test_local_billing_settled_replay_requires_exact_ledger_truth(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _execution_with_usage(store, input_tokens=700, output_tokens=0)
    attempt = await store.freeze_usage_and_claim_billing_attempt(execution)
    assert attempt is not None
    settled = await store.record_local_billing_settlement(execution, attempt)
    assert settled.state is EffectState.SETTLED

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "DELETE FROM credit_ledger WHERE id=?",
            (attempt.idempotency_or_fence_token,),
        )
        connection.commit()

    with pytest.raises(
        TerminalProtocolMalformedError,
        match="disagrees with ledger truth",
    ):
        await store.record_local_billing_settlement(execution, settled)
    assert _balances(db_path) == (99, 99)
    assert _billing_rows(db_path)[0] == []


@pytest.mark.asyncio
async def test_local_billing_settled_replay_requires_frozen_usage_truth(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _execution_with_usage(store, input_tokens=700, output_tokens=0)
    attempt = await store.freeze_usage_and_claim_billing_attempt(execution)
    assert attempt is not None
    settled = await store.record_local_billing_settlement(execution, attempt)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turn_generations SET cost_amount=0.5 WHERE turn_id='t_1' AND generation=1",
        )
        connection.commit()

    with pytest.raises(
        TerminalProtocolMalformedError,
        match="frozen usage changed",
    ):
        await store.record_local_billing_settlement(execution, settled)
    assert _balances(db_path) == (99, 99)
    assert len(_billing_rows(db_path)[0]) == 1


@pytest.mark.asyncio
async def test_normal_billing_waits_for_every_non_billing_effect(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _execution_with_usage(store, input_tokens=800, output_tokens=300)
    intent = await store.begin_effect_attempt(
        execution,
        attempt_id="eff_external_dispatched",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="tool_external/v1",
        action_ref="tool:external-dispatched",
        idempotency_or_fence_token="external-dispatched-key",
        non_retriable=False,
    )
    await store.mark_effect_dispatched(execution, intent)

    with pytest.raises(
        TerminalProtocolMalformedError,
        match="blocked by unsettled side effects",
    ):
        await store.freeze_usage_and_claim_billing_attempt(execution)
    with sqlite3.connect(db_path) as connection:
        generation = connection.execute(
            "SELECT usage_digest FROM turn_generations WHERE turn_id='t_1' AND generation=1",
        ).fetchone()
        attempts = connection.execute(
            "SELECT action_ref,state FROM turn_effect_attempts ORDER BY action_ref",
        ).fetchall()
    assert generation == (None,)
    assert attempts == [
        ("tool:external-dispatched", EffectState.DISPATCHED.value),
    ]


@pytest.mark.asyncio
async def test_usage_freeze_closes_new_effect_provider_boundaries(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _execution_with_usage(store, input_tokens=800, output_tokens=300)
    billing = await store.freeze_usage_and_claim_billing_attempt(execution)
    assert billing is not None

    with pytest.raises(
        TerminalProtocolMalformedError,
        match="dispatch is closed after usage freeze",
    ):
        await store.begin_effect_attempt(
            execution,
            attempt_id="eff_late_begin",
            effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
            provider="llm_provider/v1",
            action_ref="llm:late-begin",
            idempotency_or_fence_token="late-begin-key",
            non_retriable=False,
        )

    late_intent = EffectAttemptContext(
        scope=execution.scope,
        attempt_id="eff_late_claim",
        action_ref="llm:late-claim",
        provider="llm_provider/v1",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        state=EffectState.INTENT,
        claim_epoch=execution.claim_epoch,
        row_version=1,
        idempotency_or_fence_token="late-claim-key",
        non_retriable=False,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO turn_effect_attempts(
              attempt_id,turn_id,generation,claim_epoch,effect_class,provider,
              action_ref,idempotency_or_fence_token,non_retriable,state,row_version,started_at
            ) VALUES (?,?,?,?,?,?,?,?,0,'intent',1,'t')
            """,
            (
                late_intent.attempt_id,
                late_intent.scope.turn_id,
                late_intent.scope.generation,
                late_intent.claim_epoch,
                late_intent.effect_class.value,
                late_intent.provider,
                late_intent.action_ref,
                late_intent.idempotency_or_fence_token,
            ),
        )
        connection.commit()
    with pytest.raises(
        TerminalProtocolMalformedError,
        match="dispatch is closed after usage freeze",
    ):
        await store.claim_effect_dispatch(execution, late_intent)
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT action_ref,state FROM turn_effect_attempts ORDER BY action_ref",
        ).fetchall()
    assert ("llm:late-begin", EffectState.INTENT.value) not in rows
    assert ("llm:late-claim", EffectState.INTENT.value) in rows


@pytest.mark.asyncio
async def test_exhausted_local_billing_overdraws_and_settles_once(
    db_path: str,
) -> None:
    """余额已 0 仍跑完的 generation 照实扣到负数；结算是终态，事后 grant 不会再扣一次。"""
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE users SET credit_balance=0 WHERE id='u_1'")
        connection.execute(
            "UPDATE teams SET team_credit_balance=0 WHERE id='team_1'",
        )
        connection.commit()
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _execution_with_usage(store, input_tokens=1, output_tokens=0)
    attempt = await store.freeze_usage_and_claim_billing_attempt(execution)
    assert attempt is not None

    settled = await store.record_local_billing_settlement(execution, attempt)
    assert settled.state is EffectState.SETTLED
    assert _balances(db_path) == (-1, -1)
    ledger, settlements = _billing_rows(db_path)
    assert len(ledger) == 1
    assert settlements[0][:2] == (
        EffectSettlementOutcome.CONFIRMED_APPLIED.value,
        "credit_ledger_applied",
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE users SET credit_balance=10 WHERE id='u_1'")
        connection.execute(
            "UPDATE teams SET team_credit_balance=10 WHERE id='team_1'",
        )
        connection.commit()
    assert await store.record_local_billing_settlement(execution, attempt) == settled
    assert _balances(db_path) == (10, 10)
    assert len(_billing_rows(db_path)[0]) == 1


@pytest.mark.asyncio
async def test_recovery_can_create_and_settle_missing_local_billing(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _execution_with_usage(store, input_tokens=500, output_tokens=500)
    processing = await _claim_recovery(store, execution)

    attempt = await store.freeze_usage_and_claim_billing_attempt(processing)
    assert attempt is not None
    assert attempt.state is EffectState.DISPATCHED
    with pytest.raises(
        TerminalProtocolMalformedError,
        match="execution capability changed",
    ):
        await store.record_local_billing_settlement(execution, attempt)
    settled = await store.record_local_billing_settlement(processing, attempt)
    assert settled.state is EffectState.SETTLED
    assert _balances(db_path) == (99, 99)
    assert len(_billing_rows(db_path)[0]) == 1


@pytest.mark.asyncio
async def test_recovery_reconciles_billing_claim_left_by_dead_owner(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _execution_with_usage(store, input_tokens=800, output_tokens=300)
    dispatched = await store.freeze_usage_and_claim_billing_attempt(execution)
    assert dispatched is not None
    processing = await _claim_recovery(store, execution)

    claimed = await store.freeze_usage_and_claim_billing_attempt(processing)
    assert claimed is not None
    assert claimed.state is EffectState.UNKNOWN
    assert claimed.attempt_id == dispatched.attempt_id
    with pytest.raises(
        TerminalProtocolMalformedError,
        match="execution capability changed",
    ):
        await store.record_local_billing_settlement(execution, dispatched)
    settled = await store.record_local_billing_settlement(processing, claimed)
    assert settled.state is EffectState.SETTLED
    assert _balances(db_path) == (98, 98)
    assert len(_billing_rows(db_path)[0]) == 1


@pytest.mark.asyncio
async def test_recovery_billing_waits_for_every_non_billing_effect(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _execution_with_usage(store, input_tokens=800, output_tokens=300)
    intent = await store.begin_effect_attempt(
        execution,
        attempt_id="eff_external_unknown",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="tool_external/v1",
        action_ref="tool:external-unknown",
        idempotency_or_fence_token="external-unknown-key",
        non_retriable=False,
    )
    await store.mark_effect_dispatched(execution, intent)
    processing = await _claim_recovery(store, execution)

    with pytest.raises(
        TerminalProtocolMalformedError,
        match="blocked by unsettled side effects",
    ):
        await store.freeze_usage_and_claim_billing_attempt(processing)
    with sqlite3.connect(db_path) as connection:
        generation = connection.execute(
            "SELECT usage_digest FROM turn_generations WHERE turn_id='t_1' AND generation=1",
        ).fetchone()
        attempts = connection.execute(
            "SELECT action_ref,state FROM turn_effect_attempts ORDER BY action_ref",
        ).fetchall()
    assert generation == (None,)
    assert attempts == [("tool:external-unknown", EffectState.UNKNOWN.value)]


@pytest.mark.asyncio
async def test_recovery_billing_is_frozen_after_candidate_prepare(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _execution_with_usage(store, input_tokens=500, output_tokens=500)
    processing = await _claim_recovery(store, execution)
    billing = await store.freeze_usage_and_claim_billing_attempt(processing)
    assert billing is not None
    await store.record_local_billing_settlement(processing, billing)
    snapshot = await store.read_effect_disclosure_snapshot(execution.scope)
    assert snapshot is not None
    plan = _with_effect_disclosure(
        _recovery_plan(
            execution.scope,
            closing_accepted_through=processing.closing_accepted_through,
            persisted_event_highwater=0,
        ),
        snapshot,
    )
    plan = replace(
        plan,
        projection_extensions=snapshot.projection_extensions,
    )
    prepared = await store.prepare_recovery_candidate(processing, plan)
    fake_attempt = EffectAttemptContext(
        scope=execution.scope,
        attempt_id="tea_bill_candidate_fence",
        action_ref="billing:candidate-fence",
        provider=f"credit_ledger/{RATE_CARD_VERSION}",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        state=EffectState.DISPATCHED,
        claim_epoch=execution.claim_epoch,
        row_version=2,
        idempotency_or_fence_token="led_candidate_fence",
        non_retriable=False,
    )

    for operation in (
        lambda: store.freeze_usage_and_claim_billing_attempt(prepared.processing),
        lambda: store.record_local_billing_settlement(
            prepared.processing,
            fake_attempt,
        ),
    ):
        with pytest.raises(
            TerminalProtocolMalformedError,
            match="frozen after terminal candidate prepare",
        ):
            await operation()

    with sqlite3.connect(db_path) as connection:
        generation = connection.execute(
            "SELECT usage_digest,terminal_candidate_id FROM turn_generations WHERE turn_id='t_1' AND generation=1",
        ).fetchone()
        attempt_count = connection.execute(
            "SELECT COUNT(*) FROM turn_effect_attempts WHERE turn_id='t_1'",
        ).fetchone()
    assert generation is not None and isinstance(generation[0], str)
    assert generation[1] == plan.candidate_id
    assert attempt_count == (1,)


@pytest.mark.asyncio
async def test_usage_freeze_fault_leaves_no_digest_or_billing_attempt(
    db_path: str,
) -> None:
    def inject(point: TerminalProtocolFaultPoint) -> None:
        if point is TerminalProtocolFaultPoint.LOCAL_BILLING_AFTER_USAGE_FREEZE:
            raise RuntimeError("crash after usage freeze")

    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _execution_with_usage(store, input_tokens=200, output_tokens=0)
    faulting = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=inject,
    )
    with pytest.raises(RuntimeError, match="crash after usage freeze"):
        await faulting.freeze_usage_and_claim_billing_attempt(execution)

    with sqlite3.connect(db_path) as connection:
        generation = connection.execute(
            "SELECT usage_digest FROM turn_generations WHERE turn_id='t_1' AND generation=1",
        ).fetchone()
        attempt_count = connection.execute(
            "SELECT COUNT(*) FROM turn_effect_attempts WHERE turn_id='t_1'",
        ).fetchone()
    assert generation == (None,)
    assert attempt_count == (0,)


@pytest.mark.asyncio
async def test_zero_usage_freezes_without_billing_effect(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _execution_with_usage(store, input_tokens=0, output_tokens=0)
    assert await store.freeze_usage_and_claim_billing_attempt(execution) is None
    assert await store.freeze_usage_and_claim_billing_attempt(execution) is None
    with sqlite3.connect(db_path) as connection:
        generation = connection.execute(
            "SELECT usage_digest FROM turn_generations WHERE turn_id='t_1' AND generation=1",
        ).fetchone()
        attempt_count = connection.execute(
            "SELECT COUNT(*) FROM turn_effect_attempts WHERE turn_id='t_1'",
        ).fetchone()
    assert generation is not None and isinstance(generation[0], str)
    assert attempt_count == (0,)
