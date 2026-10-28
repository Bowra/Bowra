from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from bowhead_core.rate_card import RATE_CARD_VERSION
from bowhead_core.terminal_effect_contract import EFFECT_CONTRACT_VERSION
from bowhead_core.terminal_protocol import (
    AuthFenceScopeKind,
    EffectClass,
    EffectState,
    GenerationState,
    RecoveryKind,
)
from bowhead_core.terminal_protocol_store import (
    DecisionAccessRevokedError,
    TerminalProtocolFaultPoint,
    TerminalProtocolStore,
)
from test_terminal_finalizer import _NOW, _create_db, _start_execution


def _v2_intent() -> dict[str, object]:
    return {
        "billing_rate_card_version": RATE_CARD_VERSION,
        "effect_contract_version": EFFECT_CONTRACT_VERSION,
        "text": "hello",
    }


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return _create_db(tmp_path)


@pytest.mark.asyncio
async def test_normal_effect_dispatch_rechecks_persisted_auth_and_discards_intent(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(store, execution_intent=_v2_intent())
    intent = await store.begin_effect_attempt(
        execution,
        attempt_id="eff_auth_revoked",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="tool_external/v1",
        action_ref="tool:auth-revoked",
        idempotency_or_fence_token="auth-revoked-key",
        non_retriable=False,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE terminal_auth_scope_epochs SET epoch=epoch+1,row_version=row_version+1
            WHERE scope_kind=? AND team_id='team_1' AND scope_ref='s_1'
            """,
            (AuthFenceScopeKind.SESSION_VISIBILITY.value,),
        )
        connection.commit()

    with pytest.raises(DecisionAccessRevokedError, match="authorization changed"):
        await store.claim_effect_dispatch(execution, intent)
    with pytest.raises(DecisionAccessRevokedError, match="authorization changed"):
        await store.begin_effect_attempt(
            execution,
            attempt_id="eff_auth_revoked_new",
            effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
            provider="tool_external/v1",
            action_ref="tool:auth-revoked-new",
            idempotency_or_fence_token="auth-revoked-new-key",
            non_retriable=False,
        )
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT attempt_id FROM turn_effect_attempts WHERE turn_id='t_1'",
        ).fetchall()
    assert rows == []


@pytest.mark.asyncio
async def test_effect_unknown_and_recovery_fence_commit_atomically(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(store, execution_intent=_v2_intent())
    intent = await store.begin_effect_attempt(
        execution,
        attempt_id="eff_unknown_atomic",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="tool_external/v1",
        action_ref="tool:unknown-atomic",
        idempotency_or_fence_token="unknown-atomic-key",
        non_retriable=False,
    )
    dispatched = await store.mark_effect_dispatched(execution, intent)

    transition = await store.mark_effect_unknown_and_discover_recovery(
        execution,
        dispatched,
        scanner_id="runtime_unknown",
    )
    assert transition.attempt.state is EffectState.UNKNOWN
    assert transition.recovery.kind is RecoveryKind.EFFECT_UNKNOWN
    assert transition.recovery.created is True
    replay = await store.mark_effect_unknown_and_discover_recovery(
        execution,
        dispatched,
        scanner_id="runtime_unknown",
    )
    assert replay.attempt == transition.attempt
    assert replay.recovery.recovery_id == transition.recovery.recovery_id
    assert replay.recovery.created is False

    with sqlite3.connect(db_path) as connection:
        generation = connection.execute(
            "SELECT state,recovery_reason FROM turn_generations WHERE turn_id='t_1' AND generation=1",
        ).fetchone()
        session = connection.execute(
            "SELECT recovery_lock_turn_id,recovery_lock_reason,steer_buffer_state FROM sessions WHERE id='s_1'",
        ).fetchone()
    assert generation == (
        GenerationState.RECOVERY_PENDING.value,
        RecoveryKind.EFFECT_UNKNOWN.value,
    )
    assert session == ("t_1", "effect_outcome_unknown", "frozen")


@pytest.mark.asyncio
async def test_effect_unknown_fault_rolls_back_attempt_and_recovery_fence(
    db_path: str,
) -> None:
    def inject(point: TerminalProtocolFaultPoint) -> None:
        if point is TerminalProtocolFaultPoint.EFFECT_UNKNOWN_AFTER_ATTEMPT_CAS:
            raise RuntimeError("crash after UNKNOWN")

    store = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=inject,
    )
    execution = await _start_execution(store, execution_intent=_v2_intent())
    intent = await store.begin_effect_attempt(
        execution,
        attempt_id="eff_unknown_rollback",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="tool_external/v1",
        action_ref="tool:unknown-rollback",
        idempotency_or_fence_token="unknown-rollback-key",
        non_retriable=False,
    )
    dispatched = await store.mark_effect_dispatched(execution, intent)

    with pytest.raises(RuntimeError, match="crash after UNKNOWN"):
        await store.mark_effect_unknown_and_discover_recovery(
            execution,
            dispatched,
            scanner_id="runtime_unknown",
        )
    with sqlite3.connect(db_path) as connection:
        attempt_state = connection.execute(
            "SELECT state,row_version FROM turn_effect_attempts WHERE attempt_id='eff_unknown_rollback'",
        ).fetchone()
        generation_state = connection.execute(
            "SELECT state FROM turn_generations WHERE turn_id='t_1' AND generation=1",
        ).fetchone()
        recovery_count = connection.execute(
            "SELECT COUNT(*) FROM turn_recovery_records WHERE turn_id='t_1'",
        ).fetchone()
    assert attempt_state == (EffectState.DISPATCHED.value, dispatched.row_version)
    assert generation_state == (GenerationState.RUNNING.value,)
    assert recovery_count == (0,)


@pytest.mark.asyncio
async def test_lease_recovery_seals_all_v2_dispatches_unknown(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(store, execution_intent=_v2_intent())
    dispatched = []
    for ordinal in range(2):
        intent = await store.begin_effect_attempt(
            execution,
            attempt_id=f"eff_lease_unknown_{ordinal}",
            effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
            provider="tool_external/v1",
            action_ref=f"tool:lease-unknown:{ordinal}",
            idempotency_or_fence_token=f"lease-unknown-key-{ordinal}",
            non_retriable=False,
        )
        dispatched.append(await store.mark_effect_dispatched(execution, intent))
    assert await store.handoff_execution_to_recovery(
        execution,
        reason_code="test_expired_after_dispatch",
    )

    discoveries = await store.list_recovery_candidates(scanner_id="lease_scanner")
    assert len(discoveries) == 1
    recovery = await store.discover_recovery(discoveries[0])
    assert recovery.kind is RecoveryKind.EFFECT_UNKNOWN
    snapshot = await store.read_effect_disclosure_snapshot(execution.scope)
    assert snapshot is not None
    assert tuple(item.state for item in snapshot.attempts) == (
        EffectState.UNKNOWN,
        EffectState.UNKNOWN,
    )
    assert snapshot.unsettled_count == 2


@pytest.mark.asyncio
async def test_lease_recovery_preserves_legacy_dispatched_semantics(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(store)
    intent = await store.begin_effect_attempt(
        execution,
        attempt_id="eff_legacy_lease_dispatch",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="legacy_external",
        action_ref="legacy:lease-dispatch",
        idempotency_or_fence_token="legacy-lease-key",
        non_retriable=False,
    )
    dispatched = await store.mark_effect_dispatched(execution, intent)
    assert await store.handoff_execution_to_recovery(
        execution,
        reason_code="test_legacy_expired_after_dispatch",
    )

    discoveries = await store.list_recovery_candidates(scanner_id="legacy_scanner")
    recovery = await store.discover_recovery(discoveries[0])
    assert recovery.kind is RecoveryKind.EFFECT_UNKNOWN
    persisted = await store.read_effect_attempt(
        execution.scope,
        action_ref=dispatched.action_ref,
    )
    assert persisted == dispatched


@pytest.mark.asyncio
async def test_lease_recovery_unknown_fault_rolls_back_dispatch_and_fence(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(store, execution_intent=_v2_intent())
    intent = await store.begin_effect_attempt(
        execution,
        attempt_id="eff_lease_unknown_rollback",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="tool_external/v1",
        action_ref="tool:lease-unknown-rollback",
        idempotency_or_fence_token="lease-unknown-rollback-key",
        non_retriable=False,
    )
    dispatched = await store.mark_effect_dispatched(execution, intent)
    assert await store.handoff_execution_to_recovery(
        execution,
        reason_code="test_expired_after_dispatch",
    )

    def inject(point: TerminalProtocolFaultPoint) -> None:
        if point is TerminalProtocolFaultPoint.EFFECT_UNKNOWN_AFTER_ATTEMPT_CAS:
            raise RuntimeError("crash while scanner seals UNKNOWN")

    faulting = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=inject,
    )
    discoveries = await faulting.list_recovery_candidates(scanner_id="lease_scanner")
    with pytest.raises(RuntimeError, match="crash while scanner"):
        await faulting.discover_recovery(discoveries[0])

    with sqlite3.connect(db_path) as connection:
        attempt_state = connection.execute(
            "SELECT state,row_version FROM turn_effect_attempts WHERE attempt_id='eff_lease_unknown_rollback'",
        ).fetchone()
        generation_state = connection.execute(
            "SELECT state FROM turn_generations WHERE turn_id='t_1' AND generation=1",
        ).fetchone()
        recovery_count = connection.execute(
            "SELECT COUNT(*) FROM turn_recovery_records WHERE turn_id='t_1'",
        ).fetchone()
    assert attempt_state == (EffectState.DISPATCHED.value, dispatched.row_version)
    assert generation_state == (GenerationState.RUNNING.value,)
    assert recovery_count == (0,)
