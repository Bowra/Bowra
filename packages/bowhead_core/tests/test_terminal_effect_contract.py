from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from bowhead_core.terminal_effect_contract import (
    EFFECT_CONTRACT_VERSION,
    EffectDisclosureKind,
    EffectIntentDisposition,
    EffectUsageEvidence,
    effect_control_identity_digest,
    effect_provider_contract_keys,
    get_effect_provider_contract,
    resolve_effect_execution_contract,
)
from bowhead_core.terminal_protocol import (
    CandidateOrigin,
    ConsumerProfile,
    EffectClass,
    EffectDisclosureSnapshot,
    EffectRetentionContext,
    EffectRetentionMemberKey,
    EffectSettlementContext,
    EffectSettlementOutcome,
    EffectSettlementSource,
    EffectState,
    FinalAnswerMode,
    FinalizationContext,
    TerminalClassification,
    TerminalEvent,
    TerminalProtocolFutureVersionError,
    TerminalProtocolMalformedError,
    TerminalScope,
    TerminalStatus,
    TerminalWriterLease,
    canonical_digest_v1,
    classify_terminal,
)
from bowhead_core.terminal_protocol_store import (
    TerminalProtocolFaultPoint,
    TerminalProtocolStore,
    _effect_disclosure_snapshot_from_extensions,
    _validate_candidate_effect_disclosure,
    open_terminal_protocol_connection,
)
from bowhead_core.terminal_reconciler import TerminalRecoveryProcessor
from bowhead_core.terminal_runtime_factory import terminal_auth_resolve_context_values
from test_terminal_finalizer import (
    _NOW,
    _completed_plan,
    _create_db,
    _done,
    _start_execution,
    _with_effect_disclosure,
)
from test_terminal_retention import _purge_receipt

from bowhead_core import metrics

RATE_CARD_VERSION = "v1"  # Historical fixtures without model-call billing snapshots.


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return _create_db(tmp_path)


def test_effect_provider_registry_is_exact_and_versioned() -> None:
    expected = {
        "approval_decision/v1": (
            EffectDisclosureKind.USER_ACTION,
            EffectUsageEvidence.UNSUPPORTED,
            False,
            False,
        ),
        "credit_ledger/v1": (
            EffectDisclosureKind.ACCOUNTING,
            EffectUsageEvidence.DETERMINISTIC_LOCAL,
            False,
            False,
        ),
        "credit_ledger/v2": (
            EffectDisclosureKind.ACCOUNTING,
            EffectUsageEvidence.DETERMINISTIC_LOCAL,
            False,
            False,
        ),
        "llm_provider/v1": (
            EffectDisclosureKind.INFERENCE_INTERNAL,
            EffectUsageEvidence.AUTHORITATIVE,
            False,
            False,
        ),
        "tool_external/v1": (
            EffectDisclosureKind.USER_ACTION,
            EffectUsageEvidence.UNSUPPORTED,
            True,
            True,
        ),
    }
    assert effect_provider_contract_keys() == tuple(sorted(expected))
    for key, (kind, usage, allows_probe, allows_manual) in expected.items():
        contract = get_effect_provider_contract(key)
        assert contract.key == key
        assert contract.contract_version == EFFECT_CONTRACT_VERSION
        assert contract.disclosure_kind is kind
        assert contract.usage_evidence is usage
        assert contract.outcome_classifier_key.endswith("/v1")
        assert contract.probe_adapter_key.endswith("/v1")
        assert contract.allows_control_probe is allows_probe
        assert contract.allows_manual_settlement is allows_manual
    with pytest.raises(TerminalProtocolFutureVersionError, match="provider contract"):
        get_effect_provider_contract("tool_external/v2")
    with pytest.raises(TerminalProtocolMalformedError, match="provider contract"):
        get_effect_provider_contract("provider-display-name")


@pytest.mark.parametrize(
    ("intent", "disposition"),
    [
        ({}, EffectIntentDisposition.LEGACY),
        (
            {
                "effect_contract_version": EFFECT_CONTRACT_VERSION,
                "billing_rate_card_version": RATE_CARD_VERSION,
            },
            EffectIntentDisposition.RECOVERY_ONLY,
        ),
    ],
)
def test_effect_execution_contract_truth_table(
    intent: dict[str, object],
    disposition: EffectIntentDisposition,
) -> None:
    resolved = resolve_effect_execution_contract(intent)
    assert resolved.disposition is disposition


@pytest.mark.parametrize(
    "intent",
    [
        {"effect_contract_version": EFFECT_CONTRACT_VERSION},
        {"billing_rate_card_version": RATE_CARD_VERSION},
        {
            "effect_contract_version": True,
            "billing_rate_card_version": RATE_CARD_VERSION,
        },
        {
            "effect_contract_version": "2",
            "billing_rate_card_version": RATE_CARD_VERSION,
        },
        {
            "effect_contract_version": EFFECT_CONTRACT_VERSION,
            "billing_rate_card_version": 1,
        },
        {
            "effect_contract_version": EFFECT_CONTRACT_VERSION,
            "billing_rate_card_version": "v999",
        },
    ],
)
def test_effect_execution_contract_malformed_or_partial_pair_fails_closed(
    intent: dict[str, object],
) -> None:
    with pytest.raises(TerminalProtocolMalformedError):
        resolve_effect_execution_contract(intent)


def test_future_effect_execution_contract_fails_closed() -> None:
    with pytest.raises(TerminalProtocolFutureVersionError):
        resolve_effect_execution_contract(
            {
                "effect_contract_version": EFFECT_CONTRACT_VERSION + 1,
                "billing_rate_card_version": RATE_CARD_VERSION,
            }
        )


def test_v1_effect_disclosure_payload_is_byte_shape_compatible() -> None:
    scope = TerminalScope(team_id="team_1", session_id="s_1", turn_id="t_1", generation=1)
    snapshot = EffectDisclosureSnapshot(
        scope=scope,
        attempt_count=1,
        confirmed_applied_count=1,
        intent_count=0,
        unsettled_count=0,
        state="confirmed_applied",
        snapshot_digest="digest_v1",
    )
    assert snapshot.event_payload == {
        "attempt_count": 1,
        "confirmed_applied_count": 1,
        "intent_count": 0,
        "message_key": "turn.effect_confirmed_applied",
        "protocol": 1,
        "snapshot_digest": "digest_v1",
        "state": "confirmed_applied",
        "unsettled_count": 0,
    }
    legacy_mixed = replace(
        snapshot,
        attempt_count=2,
        intent_count=1,
        unsettled_count=1,
    )
    assert legacy_mixed.has_only_uncommitted_intents is False


def test_v2_zero_attempt_disclosure_is_valid_and_content_free() -> None:
    scope = TerminalScope(team_id="team_1", session_id="s_1", turn_id="t_1", generation=1)
    snapshot = EffectDisclosureSnapshot.v2(scope=scope, attempts=())
    assert snapshot.attempt_count == 0
    assert snapshot.unsettled_count == 0
    assert snapshot.event_payload == {
        "attempt_count": 0,
        "counts": {
            "accounting": {"applied": 0, "compensated": 0, "not_applied": 0, "unsettled": 0},
            "inference_internal": {"applied": 0, "compensated": 0, "not_applied": 0, "unsettled": 0},
            "user_action": {"applied": 0, "compensated": 0, "not_applied": 0, "unsettled": 0},
        },
        "protocol": 2,
        "snapshot_digest": snapshot.snapshot_digest,
        "unsettled_count": 0,
        "warning_message_keys": [],
    }


def test_effect_disclosure_protocol_rejects_bool() -> None:
    scope = TerminalScope(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        generation=1,
    )
    with pytest.raises(ValueError, match="protocol"):
        EffectDisclosureSnapshot(
            scope=scope,
            attempt_count=1,
            confirmed_applied_count=0,
            intent_count=0,
            unsettled_count=1,
            state="possibly_applied",
            snapshot_digest="digest",
            protocol=True,
        )


@pytest.mark.parametrize("attempt_count", [True, 0.0, "0"])
def test_v2_frozen_projection_rejects_non_integer_attempt_count(
    attempt_count: object,
) -> None:
    scope = TerminalScope(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        generation=1,
    )
    snapshot = EffectDisclosureSnapshot.v2(scope=scope, attempts=())
    extension = dict(snapshot.projection_extensions[0])
    extension["attempt_count"] = attempt_count
    with pytest.raises(TerminalProtocolMalformedError, match="header"):
        _effect_disclosure_snapshot_from_extensions(
            scope,
            json.dumps([extension]),
        )


def test_v2_frozen_projection_rejects_noncanonical_or_extra_content() -> None:
    scope = TerminalScope(team_id="team_1", session_id="s_1", turn_id="t_1", generation=1)
    snapshot = EffectDisclosureSnapshot.v2(scope=scope, attempts=())
    extension = dict(snapshot.projection_extensions[0])
    extension["raw_text"] = "must-not-survive"
    with pytest.raises(TerminalProtocolMalformedError, match="not canonical"):
        _effect_disclosure_snapshot_from_extensions(
            scope,
            json.dumps([extension]),
        )


@pytest.mark.parametrize("profile", list(ConsumerProfile))
@pytest.mark.parametrize(
    "event_type",
    ["turn.effect_disclosure_settled", "turn.llm_invocation"],
)
def test_reader_reserved_events_are_valid_nonterminal_control_state(
    profile: ConsumerProfile,
    event_type: str,
) -> None:
    assert (
        classify_terminal(
            1,
            profile,
            TerminalEvent(
                event_type=event_type,
                event_id="evt_reserved_1",
                generation=1,
                actor_scope="root",
                phase_scope=("post_terminal_auxiliary" if event_type == "turn.effect_disclosure_settled" else "in_generation"),
                terminal_receipt_id=("receipt_1" if event_type == "turn.effect_disclosure_settled" else None),
                payload={"protocol": 2},
            ),
        )
        is TerminalClassification.NON_TERMINAL
    )


@pytest.mark.asyncio
async def test_store_reads_v2_zero_to_many_exact_effect_proof(db_path: str) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(
        store,
        execution_intent={
            "billing_rate_card_version": RATE_CARD_VERSION,
            "effect_contract_version": EFFECT_CONTRACT_VERSION,
            "text": "hello",
        },
    )
    contract = await store.read_effect_execution_contract(execution.scope)
    assert contract.disposition is EffectIntentDisposition.RECOVERY_ONLY
    empty = await store.read_effect_disclosure_snapshot(execution.scope)
    assert empty is not None
    assert empty.protocol == 2
    assert empty.attempt_count == 0

    tool = await store.begin_effect_attempt(
        execution,
        attempt_id="eff_tool_1",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="tool_external/v1",
        action_ref="tool:send:1",
        idempotency_or_fence_token="tool-key-1",
        non_retriable=False,
    )
    tool = await store.mark_effect_dispatched(execution, tool)
    tool = await store.mark_effect_unknown(tool)
    llm = await store.begin_effect_attempt(
        execution,
        attempt_id="eff_llm_1",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="llm_provider/v1",
        action_ref="llm:slot:1",
        idempotency_or_fence_token="llm-key-1",
        non_retriable=False,
    )
    llm = await store.mark_effect_dispatched(execution, llm)
    await store.mark_effect_unknown(llm)
    await store.record_provider_effect_settlement(
        _provider_settlement(
            execution.scope,
            attempt_id=tool.attempt_id,
            row_version=tool.row_version,
        )
    )
    snapshot = await store.read_effect_disclosure_snapshot(execution.scope)
    assert snapshot is not None
    assert snapshot.protocol == 2
    assert snapshot.attempt_count == 2
    assert snapshot.unsettled_count == 1
    assert snapshot.kind_outcome_counts == {
        "accounting": {"applied": 0, "compensated": 0, "not_applied": 0, "unsettled": 0},
        "inference_internal": {"applied": 0, "compensated": 0, "not_applied": 0, "unsettled": 1},
        "user_action": {"applied": 1, "compensated": 0, "not_applied": 0, "unsettled": 0},
    }
    assert snapshot.event_payload["warning_message_keys"] == ["turn.effect_inference_unsettled"]
    assert len(snapshot.projection_extensions) == 3
    assert snapshot.projection_extensions[0]["snapshot_digest"] == snapshot.snapshot_digest
    assert list(snapshot.projection_extensions) == sorted(
        snapshot.projection_extensions,
        key=lambda item: str(item["kind"]),
    )

    changed = replace(
        snapshot.attempts[1],
        evidence_digest="different_evidence",
    )
    assert (
        EffectDisclosureSnapshot.v2(
            scope=execution.scope,
            attempts=(snapshot.attempts[0], changed),
        ).snapshot_digest
        != snapshot.snapshot_digest
    )


@pytest.mark.asyncio
async def test_running_overlay_never_presents_intent_as_possible_effect(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(
        store,
        execution_intent={
            "billing_rate_card_version": RATE_CARD_VERSION,
            "effect_contract_version": EFFECT_CONTRACT_VERSION,
            "text": "hello",
        },
    )
    intent = await store.begin_effect_attempt(
        execution,
        attempt_id="eff_intent_not_visible",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="tool_external/v1",
        action_ref="tool:intent:not-visible",
        idempotency_or_fence_token="intent-not-visible-key",
        non_retriable=False,
    )

    before_dispatch = await store.read_current_effect_disclosure(execution.scope)
    assert before_dispatch is not None
    assert before_dispatch.attempt_count == 0
    assert before_dispatch.warning_message_keys == ()

    await store.mark_effect_dispatched(execution, intent)
    after_dispatch = await store.read_current_effect_disclosure(execution.scope)
    assert after_dispatch is not None
    assert after_dispatch.attempt_count == 1
    assert after_dispatch.counts["user_action"]["unsettled"] == 1
    assert after_dispatch.warning_message_keys == ("turn.effect_user_action_unsettled",)


@pytest.mark.asyncio
async def test_v2_reader_rejects_non_side_effect_ledger_rows(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(
        store,
        execution_intent={
            "billing_rate_card_version": RATE_CARD_VERSION,
            "effect_contract_version": EFFECT_CONTRACT_VERSION,
            "text": "hello",
        },
    )
    await store.begin_effect_attempt(
        execution,
        attempt_id="eff_illegal_read_only",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="tool_external/v1",
        action_ref="tool:illegal:read-only",
        idempotency_or_fence_token="illegal-read-only-key",
        non_retriable=False,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE turn_effect_attempts SET effect_class='read_only' WHERE attempt_id='eff_illegal_read_only'")
        connection.commit()

    with pytest.raises(TerminalProtocolMalformedError, match="ledger class"):
        await store.read_effect_disclosure_snapshot(execution.scope)


def _recovery_writer() -> TerminalWriterLease:
    return TerminalWriterLease(
        owner_instance_id="gateway_1",
        writer_lease_epoch=7,
        writer_admission_epoch=3,
        rollout_config_generation="rollout_1",
        lease_expires_at=(_NOW + timedelta(minutes=5)).isoformat(),
        admission_state="open",
    )


def _expire_execution_for_recovery(db_path: str, turn_id: str) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turn_generations SET lease_expires_at=? WHERE turn_id=?",
            (
                (_NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
                turn_id,
            ),
        )
        connection.commit()


async def _validate_effect_candidate(
    store: TerminalProtocolStore,
    plan,
) -> None:
    async with open_terminal_protocol_connection(store.db_path) as connection:
        connection.row_factory = sqlite3.Row
        await _validate_candidate_effect_disclosure(connection, plan)


def _v2_plan_with_disclosure(plan, snapshot: EffectDisclosureSnapshot):
    return replace(
        _with_effect_disclosure(plan, snapshot),
        projection_extensions=snapshot.projection_extensions,
    )


def _cancelled_recovery_plan(
    scope: TerminalScope,
    snapshot: EffectDisclosureSnapshot,
):
    base = _completed_plan(scope)
    primary = _cancelled_primary(base)
    return _v2_plan_with_disclosure(
        replace(
            base,
            origin=CandidateOrigin.RECOVERY,
            variants=(primary,),
            session_disposition_plan={
                "turn_projection": {"intent": "cancelled"},
                "variant_templates": {primary.variant_key: {}},
            },
        ),
        snapshot,
    )


def _cancelled_normal_plan(
    scope: TerminalScope,
    snapshot: EffectDisclosureSnapshot,
):
    base = _completed_plan(scope)
    primary = _cancelled_primary(base)
    recovery = base.variants[1]
    return _v2_plan_with_disclosure(
        replace(
            base,
            variants=tuple(
                sorted(
                    (primary, recovery),
                    key=lambda variant: variant.variant_key,
                )
            ),
            session_disposition_plan={
                "turn_projection": {"intent": "cancelled"},
                "variant_templates": {
                    primary.variant_key: {},
                    recovery.variant_key: {},
                },
            },
        ),
        snapshot,
    )


def _cancelled_primary(base):
    base_primary = base.primary_variant
    assert base_primary is not None
    return replace(
        base_primary,
        status=TerminalStatus.CANCELLED,
        reason_code="user_cancelled",
        message_key="turn.cancelled",
        final_answer_mode=FinalAnswerMode.NONE,
        final_answer_event_id=None,
        final_answer_digest=None,
        events=(
            _done(
                event_id=base.reserved_terminal_event_id,
                candidate_id=base.candidate_id,
                receipt_id=base.reserved_receipt_id,
                variant_key=base_primary.variant_key,
                status=TerminalStatus.CANCELLED,
                reason="user_cancelled",
                message_key="turn.cancelled",
                manifest_digest=canonical_digest_v1([]),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_v2_candidate_matrix_accepts_generic_settled(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(
        store,
        execution_intent={
            "billing_rate_card_version": RATE_CARD_VERSION,
            "effect_contract_version": EFFECT_CONTRACT_VERSION,
            "text": "hello",
        },
    )
    attempt = await store.begin_effect_attempt(
        execution,
        attempt_id="eff_matrix_settled",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="tool_external/v1",
        action_ref="tool:matrix:settled",
        idempotency_or_fence_token="matrix-settled-key",
        non_retriable=False,
    )
    dispatched = await store.mark_effect_dispatched(execution, attempt)
    unknown = await store.mark_effect_unknown(dispatched)
    await store.record_provider_effect_settlement(
        _provider_settlement(
            execution.scope,
            attempt_id=unknown.attempt_id,
            row_version=unknown.row_version,
        )
    )
    assert await store.freeze_usage_and_claim_billing_attempt(execution) is None
    settled = await store.read_effect_disclosure_snapshot(execution.scope)
    assert settled is not None and settled.unsettled_count == 0
    plan = _v2_plan_with_disclosure(
        _completed_plan(execution.scope),
        settled,
    )
    await _validate_effect_candidate(store, plan)
    assert all(sum(event.event_type == "effect_settlement" for event in variant.required_events) == 1 for variant in plan.variants)

    missing_event = replace(
        _completed_plan(execution.scope),
        projection_extensions=settled.projection_extensions,
    )
    with pytest.raises(TerminalProtocolMalformedError, match="does not match"):
        await _validate_effect_candidate(store, missing_event)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [EffectState.INTENT, EffectState.DISPATCHED])
async def test_v2_candidate_rejects_unsealed_intent_or_dispatched(
    db_path: str,
    state: EffectState,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(
        store,
        execution_intent={
            "billing_rate_card_version": RATE_CARD_VERSION,
            "effect_contract_version": EFFECT_CONTRACT_VERSION,
            "text": "hello",
        },
    )
    attempt = await store.begin_effect_attempt(
        execution,
        attempt_id=f"eff_matrix_{state.value}",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="tool_external/v1",
        action_ref=f"tool:matrix:{state.value}",
        idempotency_or_fence_token=f"matrix-{state.value}-key",
        non_retriable=False,
    )
    if state is EffectState.DISPATCHED:
        await store.mark_effect_dispatched(execution, attempt)
    snapshot = await store.read_effect_disclosure_snapshot(execution.scope)
    assert snapshot is not None
    plan = _v2_plan_with_disclosure(
        _completed_plan(execution.scope),
        snapshot,
    )
    with pytest.raises(TerminalProtocolMalformedError, match="unsealed"):
        await _validate_effect_candidate(store, plan)


@pytest.mark.asyncio
async def test_v2_candidate_allows_cancelled_unknown(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(
        store,
        execution_intent={
            "billing_rate_card_version": RATE_CARD_VERSION,
            "effect_contract_version": EFFECT_CONTRACT_VERSION,
            "text": "hello",
        },
    )
    attempt = await store.begin_effect_attempt(
        execution,
        attempt_id="eff_matrix_unknown",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="tool_external/v1",
        action_ref="tool:matrix:unknown",
        idempotency_or_fence_token="matrix-unknown-key",
        non_retriable=False,
    )
    dispatched = await store.mark_effect_dispatched(execution, attempt)
    await store.mark_effect_unknown(dispatched)
    await store.begin_cancelled_finalization_with_local_billing(
        execution,
        finalization_owner_instance_id="gateway_1:finalizer",
    )
    snapshot = await store.read_effect_disclosure_snapshot(execution.scope)
    assert snapshot is not None
    cancelled = _cancelled_recovery_plan(execution.scope, snapshot)
    await _validate_effect_candidate(store, cancelled)
    assert [event.event_type for event in cancelled.variants[0].required_events] == ["effect_disclosure"]


@pytest.mark.asyncio
@pytest.mark.parametrize("settled_attempt_count", [0, 2])
async def test_production_recovery_skips_approval_adapter_for_generic_v2(
    db_path: str,
    settled_attempt_count: int,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(
        store,
        execution_intent={
            "billing_rate_card_version": RATE_CARD_VERSION,
            "effect_contract_version": EFFECT_CONTRACT_VERSION,
            "text": "hello",
        },
    )
    for index in range(settled_attempt_count):
        attempt = await store.begin_effect_attempt(
            execution,
            attempt_id=f"eff_generic_{index}",
            effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
            provider="tool_external/v1",
            action_ref=f"tool:generic:{index}",
            idempotency_or_fence_token=f"generic-key-{index}",
            non_retriable=False,
        )
        dispatched = await store.mark_effect_dispatched(execution, attempt)
        unknown = await store.mark_effect_unknown(dispatched)
        await store.record_provider_effect_settlement(
            _provider_settlement(
                execution.scope,
                attempt_id=unknown.attempt_id,
                row_version=unknown.row_version,
            )
        )
    _expire_execution_for_recovery(db_path, execution.scope.turn_id)
    adapter_calls = 0

    async def approval_adapter(*_args: object) -> bool:
        nonlocal adapter_calls
        adapter_calls += 1
        return False

    result = await TerminalRecoveryProcessor(
        store,
        settled_effect_recoverer=approval_adapter,
    ).run_bounded(
        owner_instance_id="gateway_1",
        rollout_config_generation="rollout_1",
        writer_fence=_recovery_writer(),
    )

    assert result.errors == ()
    assert adapter_calls == 0
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT status FROM turn_terminal_receipts WHERE turn_id=?",
            (execution.scope.turn_id,),
        ).fetchone() == ("aborted",)
        effect_events = connection.execute("SELECT event_type FROM turn_events WHERE event_type='effect_settlement'").fetchall()
    assert len(effect_events) == (1 if settled_attempt_count else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settled_outcome",
    [
        EffectSettlementOutcome.CONFIRMED_APPLIED,
        EffectSettlementOutcome.CONFIRMED_NOT_APPLIED,
    ],
)
async def test_production_recovery_discards_intent_and_keeps_settled_proof(
    db_path: str,
    settled_outcome: EffectSettlementOutcome,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(
        store,
        execution_intent={
            "billing_rate_card_version": RATE_CARD_VERSION,
            "effect_contract_version": EFFECT_CONTRACT_VERSION,
            "text": "hello",
        },
    )
    settled_attempt = await store.begin_effect_attempt(
        execution,
        attempt_id="eff_recovery_settled",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="tool_external/v1",
        action_ref="tool:recovery:settled",
        idempotency_or_fence_token="recovery-settled-key",
        non_retriable=False,
    )
    dispatched = await store.mark_effect_dispatched(execution, settled_attempt)
    unknown = await store.mark_effect_unknown(dispatched)
    settlement = _provider_settlement(
        execution.scope,
        attempt_id=unknown.attempt_id,
        row_version=unknown.row_version,
        outcome=settled_outcome,
        settlement_code=f"provider_{settled_outcome.value}",
    )
    await store.record_provider_effect_settlement(settlement)
    await store.begin_effect_attempt(
        execution,
        attempt_id="eff_recovery_intent",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="tool_external/v1",
        action_ref="tool:recovery:intent",
        idempotency_or_fence_token="recovery-intent-key",
        non_retriable=False,
    )
    _expire_execution_for_recovery(db_path, execution.scope.turn_id)
    adapter_calls = 0

    async def approval_adapter(*_args: object) -> bool:
        nonlocal adapter_calls
        adapter_calls += 1
        return False

    result = await TerminalRecoveryProcessor(
        store,
        settled_effect_recoverer=approval_adapter,
    ).run_bounded(
        owner_instance_id="gateway_1",
        rollout_config_generation="rollout_1",
        writer_fence=_recovery_writer(),
    )

    assert result.errors == ()
    assert adapter_calls == 0
    current = await store.read_current_effect_disclosure(execution.scope)
    assert current is not None
    assert current.attempt_count == 1
    assert sum(outcomes["unsettled"] for outcomes in current.counts.values()) == 0
    outcome_bucket = {
        EffectSettlementOutcome.CONFIRMED_APPLIED: "applied",
        EffectSettlementOutcome.CONFIRMED_NOT_APPLIED: "not_applied",
    }[settled_outcome]
    assert current.counts["user_action"][outcome_bucket] == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT attempt_id,state FROM turn_effect_attempts ORDER BY attempt_id").fetchall() == [
            ("eff_recovery_settled", "settled")
        ]
        assert connection.execute(
            "SELECT status FROM turn_terminal_receipts WHERE turn_id=?",
            (execution.scope.turn_id,),
        ).fetchone() == ("aborted",)


@pytest.mark.asyncio
async def test_production_recovery_defers_generic_v2_unknown_without_approval_adapter(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(
        store,
        execution_intent={
            "billing_rate_card_version": RATE_CARD_VERSION,
            "effect_contract_version": EFFECT_CONTRACT_VERSION,
            "text": "hello",
        },
    )
    attempt = await store.begin_effect_attempt(
        execution,
        attempt_id="eff_generic_unknown",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="tool_external/v1",
        action_ref="tool:generic:unknown",
        idempotency_or_fence_token="generic-unknown-key",
        non_retriable=False,
    )
    dispatched = await store.mark_effect_dispatched(execution, attempt)
    await store.mark_effect_unknown(dispatched)
    _expire_execution_for_recovery(db_path, execution.scope.turn_id)
    adapter_calls = 0

    async def approval_adapter(*_args: object) -> bool:
        nonlocal adapter_calls
        adapter_calls += 1
        return False

    result = await TerminalRecoveryProcessor(
        store,
        settled_effect_recoverer=approval_adapter,
    ).run_bounded(
        owner_instance_id="gateway_1",
        rollout_config_generation="rollout_1",
        writer_fence=_recovery_writer(),
    )

    assert result.errors == ()
    assert adapter_calls == 0
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_receipts").fetchone() == (0,)
        assert connection.execute("SELECT last_code FROM turn_recovery_records").fetchone() == ("effect_outcome_unknown",)


@pytest.mark.asyncio
async def test_production_recovery_routes_exact_v2_approval_to_adapter(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(
        store,
        execution_intent={
            "billing_rate_card_version": RATE_CARD_VERSION,
            "effect_contract_version": EFFECT_CONTRACT_VERSION,
            "text": "hello",
        },
    )
    attempt = await store.begin_effect_attempt(
        execution,
        attempt_id="eff_approval_unknown",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="approval_decision/v1",
        action_ref="approval:approve_1",
        idempotency_or_fence_token="approval-key-1",
        non_retriable=False,
    )
    dispatched = await store.mark_effect_dispatched(execution, attempt)
    await store.mark_effect_unknown(dispatched)
    _expire_execution_for_recovery(db_path, execution.scope.turn_id)
    adapter_calls = 0

    async def approval_adapter(*_args: object) -> bool:
        nonlocal adapter_calls
        adapter_calls += 1
        return False

    await TerminalRecoveryProcessor(
        store,
        settled_effect_recoverer=approval_adapter,
    ).run_bounded(
        owner_instance_id="gateway_1",
        rollout_config_generation="rollout_1",
        writer_fence=_recovery_writer(),
    )

    assert adapter_calls == 1


@pytest.mark.asyncio
async def test_v2_approval_projection_has_narrow_dispatched_settlement_seam(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(
        store,
        execution_intent={
            "billing_rate_card_version": RATE_CARD_VERSION,
            "effect_contract_version": EFFECT_CONTRACT_VERSION,
            "text": "hello",
        },
    )
    attempt = await store.begin_effect_attempt(
        execution,
        attempt_id="eff_approval_dispatched",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="approval_decision/v1",
        action_ref="approval:decision_1",
        idempotency_or_fence_token="approval-decision-key",
        non_retriable=False,
    )
    dispatched = await store.mark_effect_dispatched(execution, attempt)
    context = _approval_settlement(
        execution.scope,
        attempt_id=dispatched.attempt_id,
        row_version=dispatched.row_version,
    )
    with pytest.raises(TerminalProtocolMalformedError, match="provider contract"):
        await store.record_provider_effect_settlement(context)
    settled = await store.record_approval_projection_effect_settlement(context)
    assert settled.state is EffectState.SETTLED


def _provider_settlement(
    scope: TerminalScope,
    *,
    attempt_id: str,
    row_version: int,
    outcome: EffectSettlementOutcome = EffectSettlementOutcome.CONFIRMED_APPLIED,
    settlement_code: str = "provider_confirmed",
) -> EffectSettlementContext:
    identity_fields = {
        "attempt_id": attempt_id,
        "provider_contract_key": "tool_external/v1",
        "source": EffectSettlementSource.PROVIDER.value,
        "outcome": outcome.value,
        "settlement_code": settlement_code,
        "evidence_digest": f"evidence_digest_{attempt_id}",
        "object_version_digest": f"object_version_{attempt_id}",
        "actor_audit_ref": "adapter:tool_external_probe/v1",
        "audit_object_ref": f"provider-evidence://{attempt_id}",
        "provider_external_ref_digest": f"provider_ref_{attempt_id}",
        "provider_external_ref_key_version": "provider_ref_key_v1",
    }
    identity = effect_control_identity_digest(**identity_fields)[:32]
    return EffectSettlementContext(
        scope=scope,
        attempt_id=attempt_id,
        expected_attempt_row_version=row_version,
        settlement_id=f"set_{identity}",
        source=EffectSettlementSource.PROVIDER,
        outcome=outcome,
        settlement_code=settlement_code,
        principal_ref="adapter:tool_external_probe/v1",
        principal_role="provider_adapter",
        authz_snapshot_digest="adapter_capability_1",
        evidence_link_id=f"evidence_{identity}",
        evidence_digest=f"evidence_digest_{attempt_id}",
        object_version_digest=f"object_version_{attempt_id}",
        purge_after=None,
        audit_object_ref=f"provider-evidence://{attempt_id}",
        provider_external_ref_digest=f"provider_ref_{attempt_id}",
        provider_external_ref_key_version="provider_ref_key_v1",
    )


def _approval_settlement(
    scope: TerminalScope,
    *,
    attempt_id: str,
    row_version: int,
) -> EffectSettlementContext:
    identity_fields = {
        "attempt_id": attempt_id,
        "provider_contract_key": "approval_decision/v1",
        "source": EffectSettlementSource.PROVIDER.value,
        "outcome": EffectSettlementOutcome.CONFIRMED_APPLIED.value,
        "settlement_code": "approval_projection_committed",
        "evidence_digest": f"approval_evidence_{attempt_id}",
        "object_version_digest": f"approval_object_{attempt_id}",
        "actor_audit_ref": "adapter:approval_decision_projection/v1",
        "audit_object_ref": None,
        "provider_external_ref_digest": None,
        "provider_external_ref_key_version": None,
    }
    identity = effect_control_identity_digest(**identity_fields)[:32]
    return EffectSettlementContext(
        scope=scope,
        attempt_id=attempt_id,
        expected_attempt_row_version=row_version,
        settlement_id=f"set_{identity}",
        source=EffectSettlementSource.PROVIDER,
        outcome=EffectSettlementOutcome.CONFIRMED_APPLIED,
        settlement_code="approval_projection_committed",
        principal_ref="adapter:approval_decision_projection/v1",
        principal_role="provider_adapter",
        authz_snapshot_digest="approval_projection_capability_v1",
        evidence_link_id=f"evidence_{identity}",
        evidence_digest=f"approval_evidence_{attempt_id}",
        object_version_digest=f"approval_object_{attempt_id}",
        purge_after=None,
        audit_object_ref=None,
    )


@pytest.mark.asyncio
async def test_same_provider_settlement_converges_to_one_store_owned_retention(
    db_path: str,
) -> None:
    tick = 0

    def clock():
        nonlocal tick
        current = _NOW + timedelta(microseconds=tick)
        tick += 1
        return current

    store = TerminalProtocolStore(db_path, _clock=clock)
    scope, unknown = await _manual_unknown(store)
    context = _provider_settlement(
        scope,
        attempt_id=unknown.attempt_id,
        row_version=unknown.row_version,
    )
    first, second = await asyncio.gather(
        store.record_provider_effect_settlement(context),
        store.record_provider_effect_settlement(context),
    )
    assert first == second
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_effect_settlements").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*),COUNT(DISTINCT purge_after) FROM turn_effect_evidence_links").fetchone() == (1, 1)


@pytest.mark.asyncio
async def test_v2_settlement_replay_rejects_retention_corruption(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    scope, unknown = await _manual_unknown(store)
    context = _provider_settlement(
        scope,
        attempt_id=unknown.attempt_id,
        row_version=unknown.row_version,
    )
    await store.record_provider_effect_settlement(context)
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE turn_effect_evidence_links SET purge_after='2030-01-01T00:00:00Z'")
        connection.commit()

    with pytest.raises(TerminalProtocolMalformedError, match="retention"):
        await store.record_provider_effect_settlement(context)


@pytest.mark.asyncio
async def test_v2_control_rejects_caller_retention_and_conflicting_provider_fact(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    scope, unknown = await _manual_unknown(store)
    context = _provider_settlement(
        scope,
        attempt_id=unknown.attempt_id,
        row_version=unknown.row_version,
    )
    with pytest.raises(TerminalProtocolMalformedError, match="store-owned"):
        await store.record_provider_effect_settlement(
            replace(
                context,
                purge_after=(_NOW + timedelta(days=30)).isoformat(),
            )
        )
    forged_role = replace(context)
    object.__setattr__(forged_role, "principal_role", "owner")
    with pytest.raises(TerminalProtocolMalformedError, match="provider contract"):
        await store.record_provider_effect_settlement(forged_role)
    await store.record_provider_effect_settlement(context)
    with pytest.raises(TerminalProtocolMalformedError, match="winner conflict"):
        await store.record_provider_effect_settlement(
            _provider_settlement(
                scope,
                attempt_id=unknown.attempt_id,
                row_version=unknown.row_version,
                outcome=EffectSettlementOutcome.CONFIRMED_NOT_APPLIED,
                settlement_code=context.settlement_code,
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "adapter"),
    [
        ("credit_ledger/v1", "credit_ledger_row/v1"),
        ("llm_provider/v1", "llm_provider_receipt/v1"),
    ],
)
async def test_generic_provider_control_rejects_dedicated_capabilities(
    db_path: str,
    provider: str,
    adapter: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    scope, unknown = await _manual_unknown(store)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turn_effect_attempts SET provider=? WHERE attempt_id=?",
            (provider, unknown.attempt_id),
        )
        connection.commit()
    identity_fields = {
        "attempt_id": unknown.attempt_id,
        "provider_contract_key": provider,
        "source": EffectSettlementSource.PROVIDER.value,
        "outcome": EffectSettlementOutcome.CONFIRMED_APPLIED.value,
        "settlement_code": "provider_confirmed",
        "evidence_digest": "dedicated_evidence",
        "object_version_digest": "dedicated_object",
        "actor_audit_ref": f"adapter:{adapter}",
        "audit_object_ref": "provider-evidence:dedicated",
        "provider_external_ref_digest": "dedicated_ref",
        "provider_external_ref_key_version": "v1",
    }
    identity = effect_control_identity_digest(**identity_fields)[:32]
    context = EffectSettlementContext(
        scope=scope,
        attempt_id=unknown.attempt_id,
        expected_attempt_row_version=unknown.row_version,
        settlement_id=f"set_{identity}",
        source=EffectSettlementSource.PROVIDER,
        outcome=EffectSettlementOutcome.CONFIRMED_APPLIED,
        settlement_code="provider_confirmed",
        principal_ref=f"adapter:{adapter}",
        principal_role="provider_adapter",
        authz_snapshot_digest="adapter_capability_v1",
        evidence_link_id=f"evidence_{identity}",
        evidence_digest="dedicated_evidence",
        object_version_digest="dedicated_object",
        purge_after=None,
        audit_object_ref="provider-evidence:dedicated",
        provider_external_ref_digest="dedicated_ref",
        provider_external_ref_key_version="v1",
    )

    with pytest.raises(TerminalProtocolMalformedError, match="provider contract"):
        await store.record_provider_effect_settlement(context)


@pytest.mark.asyncio
async def test_legacy_settlement_requires_caller_retention(db_path: str) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _start_execution(store)
    attempt = await store.begin_effect_attempt(
        execution,
        attempt_id="eff_legacy_retention",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="legacy-provider",
        action_ref="legacy:retention",
        idempotency_or_fence_token="legacy-retention-key",
        non_retriable=False,
    )
    dispatched = await store.mark_effect_dispatched(execution, attempt)
    with pytest.raises(TerminalProtocolMalformedError, match="retention is required"):
        await store.record_effect_settlement(
            replace(
                _provider_settlement(
                    execution.scope,
                    attempt_id=attempt.attempt_id,
                    row_version=dispatched.row_version,
                ),
                principal_ref="provider:legacy",
            )
        )


async def _closed_cancelled_unknown(
    store: TerminalProtocolStore,
    *,
    provider: str = "tool_external/v1",
):
    scope, unknowns, receipt = await _closed_cancelled_unknowns(
        store,
        attempt_ids=("eff_cancelled_1",),
        provider=provider,
    )
    return scope, unknowns[0], receipt


async def _closed_cancelled_unknowns(
    store: TerminalProtocolStore,
    *,
    attempt_ids: tuple[str, ...],
    provider: str = "tool_external/v1",
):
    execution = await _start_execution(
        store,
        execution_intent={
            "billing_rate_card_version": RATE_CARD_VERSION,
            "effect_contract_version": EFFECT_CONTRACT_VERSION,
            "text": "hello",
        },
    )
    unknowns = []
    for index, attempt_id in enumerate(attempt_ids, start=1):
        attempt = await store.begin_effect_attempt(
            execution,
            attempt_id=attempt_id,
            effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
            provider=provider,
            action_ref=f"tool:cancelled:{index}",
            idempotency_or_fence_token=f"tool-cancelled-key-{index}",
            non_retriable=False,
        )
        dispatched = await store.mark_effect_dispatched(execution, attempt)
        unknowns.append(await store.mark_effect_unknown(dispatched))
    finalization = await store.begin_cancelled_finalization_with_local_billing(
        execution,
        finalization_owner_instance_id="gateway_1:finalizer",
    )
    assert isinstance(finalization, FinalizationContext)
    snapshot = await store.read_effect_disclosure_snapshot(execution.scope)
    assert snapshot is not None and snapshot.protocol == 2
    plan = _cancelled_normal_plan(execution.scope, snapshot)
    primary = plan.primary_variant
    assert primary is not None
    prepared = await store.prepare_candidate(finalization, plan)
    receipt = await store.commit_candidate(
        prepared,
        plan,
        selected_variant_key=primary.variant_key,
        authz_snapshot_digest="auth_1",
        writer_admission_epoch=3,
    )
    return execution.scope, tuple(unknowns), receipt


def _cancelled_retention_context(
    scope: TerminalScope,
    *,
    attempt_id: str,
    provider_token: str,
) -> EffectRetentionContext:
    attempt_digest = canonical_digest_v1({"attempt_id": attempt_id, "team_id": scope.team_id})
    provider_digest = canonical_digest_v1({"provider_token": provider_token, "team_id": scope.team_id})
    member = EffectRetentionMemberKey(
        member_key_id=f"member_{attempt_id}",
        attempt_identity_digest=attempt_digest,
        provider_token_digest=provider_digest,
    )
    return EffectRetentionContext(
        scope=scope,
        tombstone_id=f"tomb_{attempt_id}",
        effect_bundle_digest=canonical_digest_v1(
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
        ),
        audit_key_version="audit_key_v1",
        provider_token_digests_digest=canonical_digest_v1([provider_digest]),
        settlement_outcomes_digest=canonical_digest_v1(["confirmed_applied"]),
        eligible_at=(_NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
        provider_dedup_horizon_at=(_NOW + timedelta(days=30)).isoformat().replace("+00:00", "Z"),
        expires_at=(_NOW + timedelta(days=60)).isoformat().replace("+00:00", "Z"),
        members=(member,),
    )


@pytest.mark.asyncio
async def test_cancelled_unknown_last_settlement_writes_final_overlay_atomically(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    scope, unknown, receipt = await _closed_cancelled_unknown(store)
    frozen = await store.read_current_effect_disclosure(scope)
    assert frozen is not None
    assert frozen.receipt_id == receipt.receipt_id
    assert frozen.unknown_count == 1
    settled = await store.record_provider_effect_settlement(
        _provider_settlement(
            scope,
            attempt_id=unknown.attempt_id,
            row_version=unknown.row_version,
        )
    )
    assert settled.state.value == "settled"
    overlay = await store.read_current_effect_disclosure(scope)
    assert overlay is not None
    assert overlay.finalized is True
    assert overlay.unknown_count == 0
    assert overlay.counts["user_action"]["applied"] == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_events WHERE event_type='turn.effect_disclosure_settled'").fetchone() == (1,)
        assert connection.execute(
            "SELECT current_terminal_receipt_id FROM turns WHERE id=?",
            (scope.turn_id,),
        ).fetchone() == (receipt.receipt_id,)

    purger = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW + timedelta(days=31),
    )
    claim = (await purger.claim_evidence_purge(purge_owner="purger_1"))[0]
    await purger.commit_evidence_purge(_purge_receipt(claim))
    retention = _cancelled_retention_context(
        scope,
        attempt_id=unknown.attempt_id,
        provider_token="tool-cancelled-key-1",
    )
    await store.aggregate_effect_retention(retention)
    after_retention = await store.read_current_effect_disclosure(scope)
    assert after_retention is not None
    assert after_retention.payload == overlay.payload


@pytest.mark.asyncio
async def test_approval_evidence_without_locator_reaches_retention_tombstone(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    scope, unknown, _receipt = await _closed_cancelled_unknown(
        store,
        provider="approval_decision/v1",
    )
    await store.record_approval_projection_effect_settlement(
        _approval_settlement(
            scope,
            attempt_id=unknown.attempt_id,
            row_version=unknown.row_version,
        )
    )
    settled = await store.read_current_effect_disclosure(scope)
    assert settled is not None and settled.unknown_count == 0

    purger = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW + timedelta(days=31),
    )
    # No external delete is claimed; the expired locator-free row is converted
    # to a durable local purge proof in the same Store transaction.
    assert await purger.claim_evidence_purge(purge_owner="purger_1") == ()
    await purger.aggregate_effect_retention(
        _cancelled_retention_context(
            scope,
            attempt_id=unknown.attempt_id,
            provider_token="tool-cancelled-key-1",
        )
    )
    replayed = await purger.read_current_effect_disclosure(scope)
    assert replayed is not None
    assert replayed.payload == settled.payload
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_effect_retention_tombstones").fetchone() == (1,)


@pytest.mark.asyncio
async def test_final_overlay_wrong_counts_cannot_be_frozen_by_retention(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    scope, unknown, receipt = await _closed_cancelled_unknown(store)
    await store.record_provider_effect_settlement(
        _provider_settlement(
            scope,
            attempt_id=unknown.attempt_id,
            row_version=unknown.row_version,
        )
    )
    purger = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW + timedelta(days=31),
    )
    claim = (await purger.claim_evidence_purge(purge_owner="purger_1"))[0]
    await purger.commit_evidence_purge(_purge_receipt(claim))
    with sqlite3.connect(db_path) as connection:
        # Simulate storage corruption below the immutable production writer;
        # the reader/retention gates must still reject a self-digested lie.
        connection.execute("DROP TRIGGER turn_events_terminal_protocol_shape_update")
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM turn_events WHERE terminal_receipt_id=? AND event_type='turn.effect_disclosure_settled'",
                (receipt.receipt_id,),
            ).fetchone()[0]
        )
        payload["counts"]["user_action"]["applied"] = 0
        payload["counts"]["inference_internal"]["applied"] = 1
        connection.execute(
            "UPDATE turn_events SET payload_json=?,payload_digest=? WHERE terminal_receipt_id=? AND event_type='turn.effect_disclosure_settled'",
            (
                json.dumps(payload),
                canonical_digest_v1(payload),
                receipt.receipt_id,
            ),
        )
        connection.commit()
    with pytest.raises(TerminalProtocolMalformedError, match="final event proof"):
        await store.read_current_effect_disclosure(scope)
    with pytest.raises(TerminalProtocolMalformedError, match="final overlay proof"):
        await store.aggregate_effect_retention(
            _cancelled_retention_context(
                scope,
                attempt_id=unknown.attempt_id,
                provider_token="tool-cancelled-key-1",
            )
        )


@pytest.mark.asyncio
async def test_final_overlay_wrong_event_id_cannot_be_frozen_by_retention(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    scope, unknown, receipt = await _closed_cancelled_unknown(store)
    await store.record_provider_effect_settlement(
        _provider_settlement(
            scope,
            attempt_id=unknown.attempt_id,
            row_version=unknown.row_version,
        )
    )
    purger = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW + timedelta(days=31),
    )
    claim = (await purger.claim_evidence_purge(purge_owner="purger_1"))[0]
    await purger.commit_evidence_purge(_purge_receipt(claim))
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TRIGGER turn_events_terminal_protocol_shape_update")
        connection.execute(
            "UPDATE turn_events SET id='evt_corrupt_final_overlay' WHERE terminal_receipt_id=? AND event_type='turn.effect_disclosure_settled'",
            (receipt.receipt_id,),
        )
        connection.commit()
    with pytest.raises(TerminalProtocolMalformedError, match="envelope"):
        await store.read_current_effect_disclosure(scope)
    with pytest.raises(TerminalProtocolMalformedError, match="envelope"):
        await store.aggregate_effect_retention(
            _cancelled_retention_context(
                scope,
                attempt_id=unknown.attempt_id,
                provider_token="tool-cancelled-key-1",
            )
        )


@pytest.mark.asyncio
async def test_final_overlay_fault_rolls_back_settlement_and_event(db_path: str) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    scope, unknown, _receipt = await _closed_cancelled_unknown(store)
    faulting = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=lambda point: (
            (_ for _ in ()).throw(RuntimeError(str(point))) if point is TerminalProtocolFaultPoint.EFFECT_FINAL_OVERLAY_BEFORE_EVENT else None
        ),
    )
    with pytest.raises(RuntimeError, match="EFFECT_FINAL_OVERLAY_BEFORE_EVENT"):
        await faulting.record_provider_effect_settlement(
            _provider_settlement(
                scope,
                attempt_id=unknown.attempt_id,
                row_version=unknown.row_version,
            )
        )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state,settlement_id FROM turn_effect_attempts WHERE attempt_id=?",
            (unknown.attempt_id,),
        ).fetchone() == ("unknown", None)
        assert connection.execute("SELECT COUNT(*) FROM turn_effect_settlements").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM turn_events WHERE event_type='turn.effect_disclosure_settled'").fetchone() == (0,)


@pytest.mark.asyncio
async def test_concurrent_last_settlements_write_one_event_and_replay_exactly(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    scope, unknowns, receipt = await _closed_cancelled_unknowns(
        store,
        attempt_ids=("eff_cancelled_a", "eff_cancelled_b"),
    )
    contexts = tuple(
        _provider_settlement(
            scope,
            attempt_id=unknown.attempt_id,
            row_version=unknown.row_version,
        )
        for unknown in unknowns
    )
    settled = await asyncio.gather(*(store.record_provider_effect_settlement(context) for context in contexts))
    assert {item.state for item in settled} == {EffectState.SETTLED}
    replayed = await store.record_provider_effect_settlement(contexts[0])
    assert replayed == settled[0]
    overlay = await store.read_current_effect_disclosure(scope)
    assert overlay is not None
    assert overlay.receipt_id == receipt.receipt_id
    assert overlay.finalized is True
    assert overlay.unknown_count == 0
    assert overlay.counts["user_action"]["applied"] == 2
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_effect_settlements").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM turn_events WHERE event_type='turn.effect_disclosure_settled'").fetchone() == (1,)


@pytest.mark.asyncio
async def test_provider_settlement_remains_valid_after_admin_revoke(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    scope, unknown, _receipt = await _closed_cancelled_unknown(store)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE teams SET created_by_user_id=NULL WHERE id=?",
            (scope.team_id,),
        )
        connection.commit()
    settled = await store.record_provider_effect_settlement(
        _provider_settlement(
            scope,
            attempt_id=unknown.attempt_id,
            row_version=unknown.row_version,
        )
    )
    assert settled.state is EffectState.SETTLED


@pytest.mark.asyncio
async def test_missing_effect_rows_without_retention_tombstone_fail_closed(
    db_path: str,
) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    scope, unknown, _receipt = await _closed_cancelled_unknown(store)
    await store.record_provider_effect_settlement(
        _provider_settlement(
            scope,
            attempt_id=unknown.attempt_id,
            row_version=unknown.row_version,
        )
    )
    with sqlite3.connect(db_path) as connection:
        evidence_link_id = connection.execute(
            "SELECT evidence_link_id FROM turn_effect_settlements WHERE attempt_id=?",
            (unknown.attempt_id,),
        ).fetchone()[0]
        connection.execute(
            "DELETE FROM turn_effect_settlements WHERE attempt_id=?",
            (unknown.attempt_id,),
        )
        connection.execute(
            "DELETE FROM turn_effect_attempts WHERE attempt_id=?",
            (unknown.attempt_id,),
        )
        connection.execute(
            "DELETE FROM turn_effect_evidence_links WHERE evidence_link_id=?",
            (evidence_link_id,),
        )
        connection.commit()
    with pytest.raises(TerminalProtocolMalformedError, match="retained detail proof"):
        await store.read_current_effect_disclosure(scope)


async def _manual_attempt(
    store: TerminalProtocolStore,
    *,
    mark_unknown: bool,
):
    execution = await _start_execution(
        store,
        execution_intent={
            "billing_rate_card_version": RATE_CARD_VERSION,
            "effect_contract_version": EFFECT_CONTRACT_VERSION,
            "text": "hello",
        },
    )
    attempt = await store.begin_effect_attempt(
        execution,
        attempt_id="eff_manual_1",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="tool_external/v1",
        action_ref="tool:manual:1",
        idempotency_or_fence_token="tool-manual-key-1",
        non_retriable=False,
    )
    dispatched = await store.mark_effect_dispatched(execution, attempt)
    if mark_unknown:
        return execution.scope, await store.mark_effect_unknown(dispatched)
    return execution.scope, dispatched


async def _manual_unknown(store: TerminalProtocolStore):
    return await _manual_attempt(store, mark_unknown=True)


def _install_owner(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO users(id,team_id,email,email_verified,tier,status,created_at)
            VALUES ('u_admin','team_1','admin@example.com',1,'ultra','active','t')
            """
        )
        connection.execute("UPDATE teams SET created_by_user_id='u_admin' WHERE id='team_1'")
        connection.commit()


def _manual_context(
    scope: TerminalScope,
    *,
    row_version: int,
    auth_digest: str,
) -> EffectSettlementContext:
    identity_fields = {
        "attempt_id": "eff_manual_1",
        "provider_contract_key": "tool_external/v1",
        "source": EffectSettlementSource.MANUAL.value,
        "outcome": EffectSettlementOutcome.CONFIRMED_NOT_APPLIED.value,
        "settlement_code": "admin_attested_not_applied",
        "evidence_digest": "evidence_digest_manual_1",
        "object_version_digest": "object_version_manual_1",
        "actor_audit_ref": "admin:u_admin",
        "provider_external_ref_digest": None,
        "provider_external_ref_key_version": None,
    }
    audit_identity = effect_control_identity_digest(
        **identity_fields,
        audit_object_ref=None,
    )
    audit_object_ref = f"admin-attestation:{audit_identity}"
    identity = effect_control_identity_digest(
        **identity_fields,
        audit_object_ref=audit_object_ref,
    )[:32]
    return EffectSettlementContext(
        scope=scope,
        attempt_id="eff_manual_1",
        expected_attempt_row_version=row_version,
        settlement_id=f"set_{identity}",
        source=EffectSettlementSource.MANUAL,
        outcome=EffectSettlementOutcome.CONFIRMED_NOT_APPLIED,
        settlement_code="admin_attested_not_applied",
        principal_ref="admin:u_admin",
        principal_role="owner",
        authz_snapshot_digest=auth_digest,
        evidence_link_id=f"evidence_{identity}",
        evidence_digest="evidence_digest_manual_1",
        object_version_digest="object_version_manual_1",
        purge_after=None,
        audit_object_ref=audit_object_ref,
    )


@pytest.mark.asyncio
async def test_manual_settlement_rechecks_admin_and_auth_in_same_transaction(
    db_path: str,
) -> None:
    _install_owner(db_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    scope, unknown = await _manual_unknown(store)
    resolve_context = terminal_auth_resolve_context_values(
        team_id=scope.team_id,
        session_id=scope.session_id,
        user_id="u_admin",
        resource_ref=scope.turn_id,
    )
    vector = await store.ensure_auth_scope_epochs(resolve_context)
    context = _manual_context(
        scope,
        row_version=unknown.row_version,
        auth_digest=vector.auth_fence_vector_digest,
    )
    with pytest.raises(TerminalProtocolMalformedError, match="provider contract"):
        await store.record_effect_settlement(context)
    with pytest.raises(ValueError, match="auth identity"):
        await store.record_manual_effect_settlement(
            replace(context, principal_ref="admin:u_other"),
            actor_user_id="u_admin",
            resolve_context=resolve_context,
            expected_auth_fence_vector=vector,
        )
    forged_scopes = tuple(replace(scope_item, scope_ref=f"wrong:{scope_item.scope_ref}") for scope_item in resolve_context.required_scopes)
    with pytest.raises(ValueError, match="auth identity"):
        await store.record_manual_effect_settlement(
            context,
            actor_user_id="u_admin",
            resolve_context=replace(
                resolve_context,
                required_scopes=forged_scopes,
            ),
            expected_auth_fence_vector=vector,
        )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_effect_settlements").fetchone() == (0,)
    settled = await store.record_manual_effect_settlement(
        context,
        actor_user_id="u_admin",
        resolve_context=resolve_context,
        expected_auth_fence_vector=vector,
    )
    assert settled.state.value == "settled"
    details = await store.read_effect_attempt_details(
        scope,
        actor_user_id="u_admin",
    )
    assert details[0].manual_settlement_allowed is True
    assert details[0].evidence_digest == "evidence_digest_manual_1"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT action,target_id FROM admin_audit_log").fetchall() == [("terminal_effect_manual_settled", "eff_manual_1")]


@pytest.mark.asyncio
async def test_admin_provider_probe_audit_is_atomic_and_replay_stable(
    db_path: str,
) -> None:
    metrics.get_registry().reset()
    _install_owner(db_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    scope, unknown = await _manual_unknown(store)
    context = _provider_settlement(
        scope,
        attempt_id=unknown.attempt_id,
        row_version=unknown.row_version,
    )
    resolve_context = terminal_auth_resolve_context_values(
        team_id=scope.team_id,
        session_id=scope.session_id,
        user_id="u_admin",
        resource_ref=scope.turn_id,
    )
    vector = await store.ensure_auth_scope_epochs(resolve_context)

    first = await store.record_admin_probed_effect_settlement(
        context,
        actor_user_id="u_admin",
        resolve_context=resolve_context,
        expected_auth_fence_vector=vector,
    )
    replay = await store.record_admin_probed_effect_settlement(
        context,
        actor_user_id="u_admin",
        resolve_context=resolve_context,
        expected_auth_fence_vector=vector,
    )
    assert replay == first
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT action,target_id FROM admin_audit_log").fetchall() == [
            ("terminal_effect_provider_probed", unknown.attempt_id)
        ]
    assert metrics.counter_items("bowhead_terminal_effect_resolution_total") == [
        (
            {"outcome": "confirmed_applied", "source": "provider"},
            1.0,
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tamper_sql",
    [
        "UPDATE turn_effect_evidence_links SET object_version_digest='tampered-object'",
        "UPDATE turn_effect_evidence_links SET audit_object_ref='tampered-audit-ref'",
        "UPDATE turn_effect_evidence_links SET purge_after='2030-01-01T00:00:00Z'",
        "UPDATE turn_effect_evidence_links SET team_id='team_other'",
    ],
)
async def test_exact_effect_details_fail_closed_on_evidence_tamper(
    db_path: str,
    tamper_sql: str,
) -> None:
    _install_owner(db_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    scope, unknown = await _manual_unknown(store)
    resolve_context = terminal_auth_resolve_context_values(
        team_id=scope.team_id,
        session_id=scope.session_id,
        user_id="u_admin",
        resource_ref=scope.turn_id,
    )
    vector = await store.ensure_auth_scope_epochs(resolve_context)
    await store.record_manual_effect_settlement(
        _manual_context(
            scope,
            row_version=unknown.row_version,
            auth_digest=vector.auth_fence_vector_digest,
        ),
        actor_user_id="u_admin",
        resolve_context=resolve_context,
        expected_auth_fence_vector=vector,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(tamper_sql)
        connection.commit()

    with pytest.raises(
        TerminalProtocolMalformedError,
        match="identity|binding",
    ):
        await store.read_effect_attempt_details(
            scope,
            actor_user_id="u_admin",
        )


@pytest.mark.asyncio
async def test_purged_effect_details_bind_redacted_object_version(
    db_path: str,
) -> None:
    _install_owner(db_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    scope, unknown = await _manual_unknown(store)
    resolve_context = terminal_auth_resolve_context_values(
        team_id=scope.team_id,
        session_id=scope.session_id,
        user_id="u_admin",
        resource_ref=scope.turn_id,
    )
    vector = await store.ensure_auth_scope_epochs(resolve_context)
    await store.record_manual_effect_settlement(
        _manual_context(
            scope,
            row_version=unknown.row_version,
            auth_digest=vector.auth_fence_vector_digest,
        ),
        actor_user_id="u_admin",
        resolve_context=resolve_context,
        expected_auth_fence_vector=vector,
    )
    purger = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW + timedelta(days=31),
    )
    claim = (await purger.claim_evidence_purge(purge_owner="purger_1"))[0]
    await purger.commit_evidence_purge(_purge_receipt(claim))
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE turn_effect_evidence_links SET object_version_digest='tampered-after-purge'")
        connection.commit()

    with pytest.raises(TerminalProtocolMalformedError, match="identity"):
        await store.read_effect_attempt_details(
            scope,
            actor_user_id="u_admin",
        )


@pytest.mark.asyncio
async def test_manual_settlement_revoked_admin_leaves_zero_mutation(db_path: str) -> None:
    _install_owner(db_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    scope, unknown = await _manual_unknown(store)
    resolve_context = terminal_auth_resolve_context_values(
        team_id=scope.team_id,
        session_id=scope.session_id,
        user_id="u_admin",
        resource_ref=scope.turn_id,
    )
    vector = await store.ensure_auth_scope_epochs(resolve_context)
    context = _manual_context(
        scope,
        row_version=unknown.row_version,
        auth_digest=vector.auth_fence_vector_digest,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE teams SET created_by_user_id=NULL WHERE id='team_1'")
        connection.commit()
    with pytest.raises(TerminalProtocolMalformedError, match="admin authority"):
        await store.record_manual_effect_settlement(
            context,
            actor_user_id="u_admin",
            resolve_context=resolve_context,
            expected_auth_fence_vector=vector,
        )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state,settlement_id FROM turn_effect_attempts WHERE attempt_id='eff_manual_1'").fetchone() == (
            "unknown",
            None,
        )
        assert connection.execute("SELECT COUNT(*) FROM turn_effect_settlements").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM admin_audit_log").fetchone() == (0,)


@pytest.mark.asyncio
async def test_manual_settlement_changed_auth_epoch_leaves_zero_mutation(
    db_path: str,
) -> None:
    _install_owner(db_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    scope, unknown = await _manual_unknown(store)
    resolve_context = terminal_auth_resolve_context_values(
        team_id=scope.team_id,
        session_id=scope.session_id,
        user_id="u_admin",
        resource_ref=scope.turn_id,
    )
    vector = await store.ensure_auth_scope_epochs(resolve_context)
    context = _manual_context(
        scope,
        row_version=unknown.row_version,
        auth_digest=vector.auth_fence_vector_digest,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE terminal_auth_scope_epochs
            SET epoch=epoch+1,row_version=row_version+1,updated_at='revoked'
            WHERE team_id='team_1'
            """
        )
        connection.commit()
    with pytest.raises(TerminalProtocolMalformedError, match="auth fence"):
        await store.record_manual_effect_settlement(
            context,
            actor_user_id="u_admin",
            resolve_context=resolve_context,
            expected_auth_fence_vector=vector,
        )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state,settlement_id FROM turn_effect_attempts WHERE attempt_id='eff_manual_1'").fetchone() == (
            "unknown",
            None,
        )
        assert connection.execute("SELECT COUNT(*) FROM turn_effect_settlements").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM admin_audit_log").fetchone() == (0,)


@pytest.mark.asyncio
async def test_same_manual_settlement_converges_with_one_audit(db_path: str) -> None:
    metrics.get_registry().reset()
    _install_owner(db_path)
    tick = 0

    def clock():
        nonlocal tick
        current = _NOW + timedelta(microseconds=tick)
        tick += 1
        return current

    store = TerminalProtocolStore(db_path, _clock=clock)
    scope, unknown = await _manual_unknown(store)
    resolve_context = terminal_auth_resolve_context_values(
        team_id=scope.team_id,
        session_id=scope.session_id,
        user_id="u_admin",
        resource_ref=scope.turn_id,
    )
    vector = await store.ensure_auth_scope_epochs(resolve_context)
    context = _manual_context(
        scope,
        row_version=unknown.row_version,
        auth_digest=vector.auth_fence_vector_digest,
    )
    first, second = await asyncio.gather(
        store.record_manual_effect_settlement(
            context,
            actor_user_id="u_admin",
            resolve_context=resolve_context,
            expected_auth_fence_vector=vector,
        ),
        store.record_manual_effect_settlement(
            context,
            actor_user_id="u_admin",
            resolve_context=resolve_context,
            expected_auth_fence_vector=vector,
        ),
    )
    assert first == second
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_effect_settlements").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM admin_audit_log").fetchone() == (1,)
    assert metrics.counter_items("bowhead_terminal_effect_resolution_total") == [
        (
            {"outcome": "confirmed_not_applied", "source": "manual"},
            1.0,
        )
    ]


@pytest.mark.asyncio
async def test_manual_settlement_ack_replay_survives_auth_epoch_advance(
    db_path: str,
) -> None:
    _install_owner(db_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    scope, unknown = await _manual_unknown(store)
    resolve_context = terminal_auth_resolve_context_values(
        team_id=scope.team_id,
        session_id=scope.session_id,
        user_id="u_admin",
        resource_ref=scope.turn_id,
    )
    original_vector = await store.ensure_auth_scope_epochs(resolve_context)
    context = _manual_context(
        scope,
        row_version=unknown.row_version,
        auth_digest=original_vector.auth_fence_vector_digest,
    )
    first = await store.record_manual_effect_settlement(
        context,
        actor_user_id="u_admin",
        resolve_context=resolve_context,
        expected_auth_fence_vector=original_vector,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE terminal_auth_scope_epochs
            SET epoch=epoch+1,row_version=row_version+1,updated_at='later-policy-change'
            WHERE team_id='team_1'
            """
        )
        connection.commit()
    current_vector = await store.read_auth_fence_vector(resolve_context)

    replay = await store.record_manual_effect_settlement(
        context,
        actor_user_id="u_admin",
        resolve_context=resolve_context,
        expected_auth_fence_vector=current_vector,
    )

    assert replay == first
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_effect_settlements").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM admin_audit_log").fetchone() == (1,)
        assert connection.execute("SELECT authz_snapshot_digest FROM turn_effect_settlements").fetchone() == (
            original_vector.auth_fence_vector_digest,
        )


@pytest.mark.asyncio
async def test_manual_settlement_rejects_dispatched_and_provider_conflict(
    db_path: str,
) -> None:
    _install_owner(db_path)
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    scope, dispatched = await _manual_attempt(store, mark_unknown=False)
    resolve_context = terminal_auth_resolve_context_values(
        team_id=scope.team_id,
        session_id=scope.session_id,
        user_id="u_admin",
        resource_ref=scope.turn_id,
    )
    vector = await store.ensure_auth_scope_epochs(resolve_context)
    context = _manual_context(
        scope,
        row_version=dispatched.row_version,
        auth_digest=vector.auth_fence_vector_digest,
    )
    with pytest.raises(TerminalProtocolMalformedError, match="requires unknown"):
        await store.record_manual_effect_settlement(
            context,
            actor_user_id="u_admin",
            resolve_context=resolve_context,
            expected_auth_fence_vector=vector,
        )
    unknown = await store.mark_effect_unknown(dispatched)
    context = replace(context, expected_attempt_row_version=unknown.row_version)
    await store.record_manual_effect_settlement(
        context,
        actor_user_id="u_admin",
        resolve_context=resolve_context,
        expected_auth_fence_vector=vector,
    )
    with pytest.raises(TerminalProtocolMalformedError, match="winner conflict"):
        await store.record_provider_effect_settlement(
            _provider_settlement(
                scope,
                attempt_id=unknown.attempt_id,
                row_version=unknown.row_version,
            )
        )
