from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from bowhead_core.terminal_protocol import (
    EffectClass,
    EffectRetentionContext,
    EffectRetentionMemberKey,
    EffectSettlementContext,
    EffectSettlementOutcome,
    EffectSettlementSource,
    EvidencePurgeReceipt,
    TerminalProtocolMalformedError,
    TerminalScope,
    canonical_digest_v1,
)
from bowhead_core.terminal_protocol_store import TerminalProtocolFaultPoint, TerminalProtocolStore
from bowhead_core.terminal_retention import TerminalRetentionService
from test_terminal_finalizer import (
    _NOW,
    _completed_plan,
    _create_db,
    _start_execution,
    _with_effect_disclosure,
)


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return _create_db(tmp_path)


def _settlement_context(scope: TerminalScope, *, attempt_row_version: int) -> EffectSettlementContext:
    return EffectSettlementContext(
        scope=scope,
        attempt_id="eff_1",
        expected_attempt_row_version=attempt_row_version,
        settlement_id="set_1",
        source=EffectSettlementSource.MANUAL,
        outcome=EffectSettlementOutcome.CONFIRMED_APPLIED,
        settlement_code="owner_verified",
        principal_ref="owner_audit_1",
        principal_role="owner",
        authz_snapshot_digest="auth_settlement_1",
        evidence_link_id="evidence_1",
        evidence_digest="evidence_digest_1",
        object_version_digest="object_version_1",
        purge_after="2026-08-03T04:05:05.000000Z",
        audit_object_ref="audit://object/1",
    )


async def _settled_effect(store: TerminalProtocolStore):
    execution = await _start_execution(store)
    attempt = await store.begin_effect_attempt(
        execution,
        attempt_id="eff_1",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="provider_1",
        action_ref="send_1",
        idempotency_or_fence_token="provider-token-1",
        non_retriable=False,
    )
    dispatched = await store.mark_effect_dispatched(execution, attempt)
    settled = await store.record_effect_settlement(_settlement_context(execution.scope, attempt_row_version=dispatched.row_version))
    finalization = await store.begin_finalization(
        execution,
        finalization_owner_instance_id="gateway_1:finalizer",
        closing_reason="completed",
    )
    snapshot = await store.read_effect_disclosure_snapshot(execution.scope)
    assert snapshot is not None
    plan = _with_effect_disclosure(_completed_plan(execution.scope), snapshot)
    prepared = await store.prepare_candidate(finalization, plan)
    await store.commit_candidate(
        prepared,
        plan,
        selected_variant_key="primary_none",
        authz_snapshot_digest="auth_1",
        writer_admission_epoch=3,
    )
    return execution.scope, settled


def _retention_context(scope: TerminalScope) -> EffectRetentionContext:
    attempt_digest = canonical_digest_v1({"attempt_id": "eff_1", "team_id": "team_1"})
    provider_digest = canonical_digest_v1({"provider_token": "provider-token-1", "team_id": "team_1"})
    provider_digests_digest = canonical_digest_v1([provider_digest])
    outcomes_digest = canonical_digest_v1(["confirmed_applied"])
    bundle_digest = canonical_digest_v1(
        {
            "audit_key_version": "audit_key_v1",
            "generation": scope.generation,
            "members": [
                {
                    "attempt_identity_digest": attempt_digest,
                    "provider_token_digest": provider_digest,
                }
            ],
            "team_id": scope.team_id,
            "turn_id": scope.turn_id,
        }
    )
    return EffectRetentionContext(
        scope=scope,
        tombstone_id="tomb_1",
        effect_bundle_digest=bundle_digest,
        audit_key_version="audit_key_v1",
        provider_token_digests_digest=provider_digests_digest,
        settlement_outcomes_digest=outcomes_digest,
        eligible_at="2026-08-03T04:05:05.000000Z",
        provider_dedup_horizon_at="2026-09-03T00:00:00Z",
        expires_at="2026-10-03T00:00:00Z",
        members=(
            EffectRetentionMemberKey(
                member_key_id="member_1",
                attempt_identity_digest=attempt_digest,
                provider_token_digest=provider_digest,
            ),
        ),
    )


def _purge_receipt(claim, *, outcome: str = "already_absent") -> EvidencePurgeReceipt:
    delete_outcome_digest = "delete_result_1"
    signed_projection = {
        "delete_outcome_digest": delete_outcome_digest,
        "evidence_link_id": claim.evidence_link_id,
        "object_version_digest": claim.object_version_digest,
        "outcome": outcome,
        "purge_request_id": claim.purge_request_id,
        "team_id": claim.team_id,
    }
    return EvidencePurgeReceipt(
        context=claim,
        outcome=outcome,
        delete_outcome_digest=delete_outcome_digest,
        signed_receipt_digest=canonical_digest_v1(signed_projection),
        signature_verified=True,
    )


@pytest.mark.asyncio
async def test_verified_evidence_purge_is_identity_and_version_fenced(db_path: str) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    await _settled_effect(store)
    claims = await store.claim_evidence_purge(purge_owner="purger_1")
    assert len(claims) == 1
    claim = claims[0]
    receipt = _purge_receipt(claim, outcome="deleted")
    await store.commit_evidence_purge(receipt)
    assert await store.commit_evidence_purge(receipt) == claim
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT retention_state,audit_object_ref,purged_at FROM turn_effect_evidence_links").fetchone() == (
            "purged",
            None,
            "2026-08-03T04:05:06.000000Z",
        )


@pytest.mark.asyncio
async def test_retention_service_calls_external_resolver_without_open_db_transaction(db_path: str) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    await _settled_effect(store)
    resolver_calls: list[str] = []

    async def resolver(claim):
        with sqlite3.connect(db_path, timeout=0.1) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
        resolver_calls.append(claim.purge_request_id)
        return _purge_receipt(claim)

    committed = await TerminalRetentionService(store).purge_evidence_once(
        purge_owner="purger_1",
        resolver=resolver,
    )
    assert resolver_calls == ["evidence_1:object_version_1"]
    assert [context.evidence_link_id for context in committed] == ["evidence_1"]


@pytest.mark.asyncio
async def test_retention_bundle_is_fk_atomic_and_late_callback_visible(db_path: str) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    scope, _settled = await _settled_effect(store)
    claim = (await store.claim_evidence_purge(purge_owner="purger_1"))[0]
    await store.commit_evidence_purge(_purge_receipt(claim))
    context = _retention_context(scope)
    result = await store.aggregate_effect_retention(context)
    assert result.created is True
    assert (await store.aggregate_effect_retention(context)).created is False
    assert await store.has_effect_retention_key(
        team_id="team_1",
        attempt_identity_digest=context.members[0].attempt_identity_digest,
        provider_token_digest=context.members[0].provider_token_digest,
        audit_key_version="audit_key_v1",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT COUNT(*) FROM turn_effect_attempts").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM turn_effect_settlements").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM turn_effect_evidence_links").fetchone() == (0,)
        assert connection.execute("SELECT state,attempt_count FROM turn_effect_retention_tombstones").fetchone() == ("aggregated", 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        TerminalProtocolFaultPoint.EFFECT_BEFORE_SETTLE,
        TerminalProtocolFaultPoint.EFFECT_SETTLEMENT_AFTER_EVIDENCE_LINK,
        TerminalProtocolFaultPoint.EFFECT_SETTLEMENT_AFTER_INSERT,
        TerminalProtocolFaultPoint.EFFECT_SETTLEMENT_BEFORE_ATTEMPT_CAS,
    ],
)
async def test_effect_settlement_faults_leave_no_partial_winner(
    db_path: str,
    fault: TerminalProtocolFaultPoint,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(store)
    attempt = await store.begin_effect_attempt(
        execution,
        attempt_id="eff_1",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="provider_1",
        action_ref="send_1",
        idempotency_or_fence_token="provider-token-1",
        non_retriable=False,
    )
    dispatched = await store.mark_effect_dispatched(execution, attempt)
    faulting = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=lambda point: (_ for _ in ()).throw(RuntimeError(str(point))) if point is fault else None,
    )
    with pytest.raises(RuntimeError, match=str(fault)):
        await faulting.record_effect_settlement(_settlement_context(execution.scope, attempt_row_version=dispatched.row_version))
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state,row_version,settlement_id FROM turn_effect_attempts").fetchone() == (
            "dispatched",
            2,
            None,
        )
        assert connection.execute("SELECT COUNT(*) FROM turn_effect_settlements").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM turn_effect_evidence_links").fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault", "expected_state"),
    [
        (TerminalProtocolFaultPoint.EFFECT_AFTER_INTENT, "intent"),
        (TerminalProtocolFaultPoint.EFFECT_AFTER_DISPATCH, "dispatched"),
    ],
)
async def test_effect_dispatch_boundary_faults_preserve_committed_antiretry_truth(
    db_path: str,
    fault: TerminalProtocolFaultPoint,
    expected_state: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(store)
    faulting = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=lambda point: (_ for _ in ()).throw(RuntimeError(str(point))) if point is fault else None,
    )
    if fault is TerminalProtocolFaultPoint.EFFECT_AFTER_INTENT:
        with pytest.raises(RuntimeError, match=str(fault)):
            await faulting.begin_effect_attempt(
                execution,
                attempt_id="eff_1",
                effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
                provider="provider_1",
                action_ref="send_1",
                idempotency_or_fence_token="provider-token-1",
                non_retriable=False,
            )
    else:
        attempt = await store.begin_effect_attempt(
            execution,
            attempt_id="eff_1",
            effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
            provider="provider_1",
            action_ref="send_1",
            idempotency_or_fence_token="provider-token-1",
            non_retriable=False,
        )
        with pytest.raises(RuntimeError, match=str(fault)):
            await faulting.mark_effect_dispatched(execution, attempt)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state FROM turn_effect_attempts").fetchone() == (expected_state,)


@pytest.mark.asyncio
async def test_effect_dispatch_and_unknown_transitions_replay_current_truth(db_path: str) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(store)
    attempt = await store.begin_effect_attempt(
        execution,
        attempt_id="eff_1",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="provider_1",
        action_ref="send_1",
        idempotency_or_fence_token="provider-token-1",
        non_retriable=False,
    )
    dispatched = await store.mark_effect_dispatched(execution, attempt)
    unknown = await store.mark_effect_unknown(dispatched)
    assert await store.mark_effect_unknown(dispatched) == unknown
    assert await store.mark_effect_dispatched(execution, attempt) == unknown


@pytest.mark.asyncio
async def test_effect_settlement_replay_validates_authority_scope_and_evidence(db_path: str) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(store)
    attempt = await store.begin_effect_attempt(
        execution,
        attempt_id="eff_1",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="provider_1",
        action_ref="send_1",
        idempotency_or_fence_token="provider-token-1",
        non_retriable=False,
    )
    dispatched = await store.mark_effect_dispatched(execution, attempt)
    context = _settlement_context(execution.scope, attempt_row_version=dispatched.row_version)
    settled = await store.record_effect_settlement(context)
    assert await store.record_effect_settlement(context) == settled
    with pytest.raises(TerminalProtocolMalformedError, match="winner is not bound"):
        await store.record_effect_settlement(replace(context, expected_attempt_row_version=context.expected_attempt_row_version + 10))
    with pytest.raises(ValueError, match="owner or admin"):
        replace(context, principal_role="member")
    cross_team = replace(
        context,
        scope=TerminalScope(
            team_id="team_2",
            session_id=context.scope.session_id,
            turn_id=context.scope.turn_id,
            generation=context.scope.generation,
        ),
    )
    with pytest.raises(TerminalProtocolMalformedError, match="scope mismatch"):
        await store.record_effect_settlement(cross_team)
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE turn_effect_evidence_links SET evidence_digest='tampered' WHERE evidence_link_id='evidence_1'")
        connection.commit()
    with pytest.raises(TerminalProtocolMalformedError, match="evidence identity conflict"):
        await store.record_effect_settlement(context)


@pytest.mark.asyncio
async def test_evidence_purge_faults_and_expired_lease_fail_closed(db_path: str) -> None:
    current = [_NOW]
    store = TerminalProtocolStore(db_path, _clock=lambda: current[0])
    await _settled_effect(store)
    claim_faulting = TerminalProtocolStore(
        db_path,
        _clock=lambda: current[0],
        _fault_injector=lambda point: (
            (_ for _ in ()).throw(RuntimeError(str(point))) if point is TerminalProtocolFaultPoint.EVIDENCE_PURGE_AFTER_CLAIM else None
        ),
    )
    with pytest.raises(RuntimeError, match="EVIDENCE_PURGE_AFTER_CLAIM"):
        await claim_faulting.claim_evidence_purge(purge_owner="purger_1")
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT retention_state,purge_owner FROM turn_effect_evidence_links").fetchone() == (
            "active",
            None,
        )
    claim = (await store.claim_evidence_purge(purge_owner="purger_1"))[0]
    commit_faulting = TerminalProtocolStore(
        db_path,
        _clock=lambda: current[0],
        _fault_injector=lambda point: (
            (_ for _ in ()).throw(RuntimeError(str(point))) if point is TerminalProtocolFaultPoint.EVIDENCE_PURGE_BEFORE_COMMIT else None
        ),
    )
    with pytest.raises(RuntimeError, match="EVIDENCE_PURGE_BEFORE_COMMIT"):
        await commit_faulting.commit_evidence_purge(_purge_receipt(claim))
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT retention_state,audit_object_ref FROM turn_effect_evidence_links").fetchone() == (
            "purge_pending",
            "audit://object/1",
        )
    current[0] = _NOW + timedelta(seconds=31)
    with pytest.raises(TerminalProtocolMalformedError, match="lease expired"):
        await store.commit_evidence_purge(_purge_receipt(claim))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        TerminalProtocolFaultPoint.RETENTION_AFTER_TOMBSTONE,
        TerminalProtocolFaultPoint.RETENTION_AFTER_BUNDLE_DELETE,
    ],
)
async def test_effect_retention_faults_keep_complete_bundle(
    db_path: str,
    fault: TerminalProtocolFaultPoint,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    scope, _settled = await _settled_effect(store)
    claim = (await store.claim_evidence_purge(purge_owner="purger_1"))[0]
    await store.commit_evidence_purge(_purge_receipt(claim))
    context = _retention_context(scope)
    faulting = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=lambda point: (_ for _ in ()).throw(RuntimeError(str(point))) if point is fault else None,
    )
    with pytest.raises(RuntimeError, match=str(fault)):
        await faulting.aggregate_effect_retention(context)
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT retention_state FROM turn_effect_attempts").fetchone() == ("active",)
        assert connection.execute("SELECT COUNT(*) FROM turn_effect_attempts").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM turn_effect_settlements").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM turn_effect_evidence_links").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM turn_effect_retention_tombstones").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM turn_effect_retention_member_keys").fetchone() == (0,)


@pytest.mark.asyncio
async def test_expired_effect_retention_cleanup_waits_for_callback_horizon(db_path: str) -> None:
    current = [_NOW]
    store = TerminalProtocolStore(db_path, _clock=lambda: current[0])
    scope, _settled = await _settled_effect(store)
    claim = (await store.claim_evidence_purge(purge_owner="purger_1"))[0]
    await store.commit_evidence_purge(_purge_receipt(claim))
    context = _retention_context(scope)
    await store.aggregate_effect_retention(context)
    current[0] = _NOW + timedelta(days=40)
    assert await store.purge_expired_effect_retention() == 0
    assert await store.has_effect_retention_key(
        team_id=scope.team_id,
        attempt_identity_digest=context.members[0].attempt_identity_digest,
        provider_token_digest=context.members[0].provider_token_digest,
        audit_key_version=context.audit_key_version,
    )
    current[0] = _NOW + timedelta(days=62)
    assert not await store.has_effect_retention_key(
        team_id=scope.team_id,
        attempt_identity_digest=context.members[0].attempt_identity_digest,
        provider_token_digest=context.members[0].provider_token_digest,
        audit_key_version=context.audit_key_version,
    )
    assert await store.purge_expired_effect_retention() == 1
    assert not await store.has_effect_retention_key(
        team_id=scope.team_id,
        attempt_identity_digest=context.members[0].attempt_identity_digest,
        provider_token_digest=context.members[0].provider_token_digest,
        audit_key_version=context.audit_key_version,
    )
