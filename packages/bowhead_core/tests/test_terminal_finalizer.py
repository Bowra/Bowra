from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anyio
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.migrations.baseline_schema import initial_schema
from bowhead_core.terminal_finalizer import FinalizeRequest, TerminalFinalizer
from bowhead_core.terminal_protocol import (
    ActorScope,
    AdmissionContext,
    CandidateOrigin,
    CandidateOutcome,
    EffectClass,
    EffectDisclosureSnapshot,
    EventPublishMode,
    FinalAnswerMode,
    GenerationClaimContext,
    PhaseScope,
    RootDurability,
    SessionVariant,
    StagedTerminalEvent,
    TerminalAdmissionRequest,
    TerminalCandidatePlan,
    TerminalCandidateVariant,
    TerminalDeleteContext,
    TerminalProtocolMalformedError,
    TerminalScope,
    TerminalStatus,
    canonical_digest_v1,
)
from bowhead_core.terminal_protocol_store import (
    AdmissionCode,
    AdmissionDisposition,
    GenerationClaimDisposition,
    TerminalProtocolFaultPoint,
    TerminalProtocolStore,
    TerminalReadContext,
)
from bowhead_core.terminal_reconciler import TerminalReconciler
from bowhead_core.terminal_runtime_factory import (
    terminal_auth_resolve_context_values,
)

_NOW = datetime(2026, 8, 3, 4, 5, 6, tzinfo=UTC)
_AUTH_DIGEST = "e92e1245ede88788736b176f5dc073b6c08b5e9222228a33b497a64ac2d1d0ce"


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
            """,
        )
        connection.execute(
            """
            INSERT INTO sessions(id,workspace_id,user_id,title,started_at,last_active_at,status,cost_usd,tokens_total,intents)
            VALUES ('s_1','ws_1','u_1','Session','2026-08-01T00:00:00Z','2026-08-01T00:00:00Z','active',0,0,'[]')
            """,
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


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return _create_db(tmp_path)


def _store(db_path: str) -> TerminalProtocolStore:
    return TerminalProtocolStore(db_path, _clock=lambda: _NOW)


async def _start_execution(
    store: TerminalProtocolStore,
    *,
    execution_intent: dict[str, object] | None = None,
):
    vector = await store.ensure_auth_scope_epochs(
        terminal_auth_resolve_context_values(
            team_id="team_1",
            session_id="s_1",
            user_id="u_1",
            resource_ref="t_1",
        )
    )
    assert vector.auth_fence_vector_digest == _AUTH_DIGEST
    admitted = await store.admit_or_steer_v1(
        TerminalAdmissionRequest(
            context=AdmissionContext(
                team_id="team_1",
                session_id="s_1",
                request_id="req_1",
                surface="web",
                cohort="bucket=1;config=rollout_1",
                actor="u_1",
                writer_instance_id="gateway_1",
                writer_lease_epoch=7,
                writer_admission_epoch=3,
                rollout_config_generation="rollout_1",
                auth_fence_vector_digest=_AUTH_DIGEST,
            ),
            turn_id="t_1",
            user_text="hello",
            execution_intent=execution_intent or {"text": "hello"},
            presentation_locale="zh-CN",
            message_catalog_version=1,
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
    return await store.mark_execution_started(claimed.context)


def _done(
    *,
    event_id: str,
    candidate_id: str,
    receipt_id: str,
    variant_key: str,
    status: TerminalStatus,
    reason: str | None,
    message_key: str,
    manifest_digest: str,
) -> StagedTerminalEvent:
    return StagedTerminalEvent.from_payload(
        event_id=event_id,
        event_type="done",
        actor_scope=ActorScope.ROOT,
        phase_scope=PhaseScope.IN_GENERATION,
        payload={
            "candidate_id": candidate_id,
            "generation": 1,
            "message_key": message_key,
            "protocol": 1,
            "reason": reason,
            "receipt_id": receipt_id,
            "required_manifest_template_digest": manifest_digest,
            "selected_variant_key": variant_key,
            "status": status.value,
        },
    )


def _completed_plan(scope: TerminalScope) -> TerminalCandidatePlan:
    candidate_id = "tc_1"
    receipt_id = "tr_1"
    done_id = "evt_done_1"
    answer = StagedTerminalEvent.from_payload(
        event_id="evt_answer_1",
        event_type="final_answer",
        actor_scope=ActorScope.ROOT,
        phase_scope=PhaseScope.IN_GENERATION,
        payload={"text": "answer"},
    )
    primary_template = canonical_digest_v1(
        [
            {
                "event_id": answer.event_id,
                "event_type": answer.event_type,
                "ordinal": 1,
                "payload_digest": answer.payload_digest,
            }
        ]
    )
    primary = TerminalCandidateVariant(
        variant_key="primary_none",
        outcome_variant=CandidateOutcome.PRIMARY,
        session_variant=SessionVariant.NONE,
        status=TerminalStatus.COMPLETED,
        reason_code=None,
        message_key="turn.completed",
        final_answer_mode=FinalAnswerMode.IN_TERMINAL_BATCH,
        final_answer_event_id=answer.event_id,
        final_answer_digest=answer.payload_digest,
        session_mutation_template={},
        events=(
            answer,
            _done(
                event_id=done_id,
                candidate_id=candidate_id,
                receipt_id=receipt_id,
                variant_key="primary_none",
                status=TerminalStatus.COMPLETED,
                reason=None,
                message_key="turn.completed",
                manifest_digest=primary_template,
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
            "turn_projection": {"intent": "answer"},
            "variant_templates": {"primary_none": {}, "recovery_none": {}},
        },
    )


def _recovery_plan(
    scope: TerminalScope,
    *,
    closing_accepted_through: int,
    persisted_event_highwater: int,
) -> TerminalCandidatePlan:
    base = _completed_plan(scope)
    recovery = base.variants[1]
    return replace(
        base,
        origin=CandidateOrigin.RECOVERY,
        closing_accepted_through=closing_accepted_through,
        persisted_event_highwater=persisted_event_highwater,
        variants=(recovery,),
        session_disposition_plan={
            "turn_projection": {"intent": "recovery_interrupt"},
            "variant_templates": {recovery.variant_key: {}},
        },
    )


def _with_effect_disclosure(
    plan: TerminalCandidatePlan,
    snapshot: EffectDisclosureSnapshot,
) -> TerminalCandidatePlan:
    variants: list[TerminalCandidateVariant] = []
    for variant in plan.variants:
        if snapshot.protocol == 1 and (variant.outcome_variant is not CandidateOutcome.RECOVERY_INTERRUPT):
            variants.append(variant)
            continue
        if snapshot.protocol == 2 and snapshot.attempt_count == 0:
            variants.append(variant)
            continue
        event_type = "effect_disclosure" if snapshot.protocol == 1 or snapshot.unsettled_count else "effect_settlement"
        disclosure = StagedTerminalEvent.from_payload(
            event_id=f"evt_effect_disclosure_{variant.variant_key}",
            event_type=event_type,
            actor_scope=ActorScope.ROOT,
            phase_scope=PhaseScope.IN_GENERATION,
            payload=snapshot.event_payload,
        )
        required_events = (*variant.required_events, disclosure)
        manifest_digest = canonical_digest_v1(
            [
                {
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "ordinal": ordinal,
                    "payload_digest": event.payload_digest,
                }
                for ordinal, event in enumerate(required_events, start=1)
            ]
        )
        variants.append(
            replace(
                variant,
                events=(
                    *required_events,
                    _done(
                        event_id=plan.reserved_terminal_event_id,
                        candidate_id=plan.candidate_id,
                        receipt_id=plan.reserved_receipt_id,
                        variant_key=variant.variant_key,
                        status=variant.status,
                        reason=variant.reason_code,
                        message_key=variant.message_key,
                        manifest_digest=manifest_digest,
                    ),
                ),
            )
        )
    return replace(plan, variants=tuple(sorted(variants, key=lambda item: item.variant_key)))


def _precommitted_plan(
    scope: TerminalScope,
    *,
    answer_event_id: str,
    answer_digest: str,
    persisted_event_highwater: int,
) -> TerminalCandidatePlan:
    candidate_id = "tc_pre_1"
    receipt_id = "tr_pre_1"
    done_id = "evt_done_pre_1"
    template = canonical_digest_v1(
        [
            {
                "event_id": answer_event_id,
                "event_type": "final_answer",
                "ordinal": 1,
                "payload_digest": answer_digest,
            }
        ]
    )
    primary = TerminalCandidateVariant(
        variant_key="primary_none",
        outcome_variant=CandidateOutcome.PRIMARY,
        session_variant=SessionVariant.NONE,
        status=TerminalStatus.COMPLETED,
        reason_code=None,
        message_key="turn.completed",
        final_answer_mode=FinalAnswerMode.PRECOMMITTED,
        final_answer_event_id=answer_event_id,
        final_answer_digest=answer_digest,
        session_mutation_template={},
        events=(
            _done(
                event_id=done_id,
                candidate_id=candidate_id,
                receipt_id=receipt_id,
                variant_key="primary_none",
                status=TerminalStatus.COMPLETED,
                reason=None,
                message_key="turn.completed",
                manifest_digest=template,
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
        closing_accepted_through=1,
        persisted_event_highwater=persisted_event_highwater,
        variants=(primary, recovery),
        session_disposition_plan={
            "turn_projection": {"intent": "answer"},
            "variant_templates": {"primary_none": {}, "recovery_none": {}},
        },
    )


def _session_variant_plan(scope: TerminalScope, variant: SessionVariant) -> TerminalCandidatePlan:
    base = _completed_plan(scope)
    primary_template: dict[str, object] = {}
    if variant is SessionVariant.FOLLOWUP:
        primary_template = {
            "next_turn": {
                "turn_id": "t_followup_1",
                "request_id": "req_followup_1",
                "user_text": "follow up",
                "execution_intent": {"text": "follow up"},
            }
        }
    primary_base = base.variants[0]
    primary_key = f"primary_{variant.value}"
    primary = replace(
        primary_base,
        variant_key=primary_key,
        session_variant=variant,
        session_mutation_template=primary_template,
    )
    primary = replace(
        primary,
        events=(
            primary.events[0],
            _done(
                event_id=base.reserved_terminal_event_id,
                candidate_id=base.candidate_id,
                receipt_id=base.reserved_receipt_id,
                variant_key=primary_key,
                status=primary.status,
                reason=primary.reason_code,
                message_key=primary.message_key,
                manifest_digest=primary.required_manifest_template_digest,
            ),
        ),
    )
    recovery_variant = SessionVariant.NONE if variant is SessionVariant.NONE else SessionVariant.DEFERRED
    recovery_key = f"recovery_{recovery_variant.value}"
    recovery_base = base.variants[1]
    recovery = replace(recovery_base, variant_key=recovery_key, session_variant=recovery_variant)
    recovery = replace(
        recovery,
        events=(
            _done(
                event_id=base.reserved_terminal_event_id,
                candidate_id=base.candidate_id,
                receipt_id=base.reserved_receipt_id,
                variant_key=recovery_key,
                status=recovery.status,
                reason=recovery.reason_code,
                message_key=recovery.message_key,
                manifest_digest=recovery.required_manifest_template_digest,
            ),
        ),
    )
    variants = tuple(sorted((primary, recovery), key=lambda item: item.variant_key))
    return replace(
        base,
        variants=variants,
        session_disposition_plan={
            "turn_projection": {"intent": "answer"},
            "variant_templates": {primary_key: primary_template, recovery_key: {}},
            **({"writer_admission_epoch": 3} if variant is SessionVariant.FOLLOWUP else {}),
        },
    )


@pytest.mark.asyncio
async def test_ac_b1b_001_final_answer_waits_for_terminal_commit(db_path: str) -> None:
    store = _store(db_path)
    execution = await _start_execution(store)
    finalization = await store.begin_finalization(
        execution,
        finalization_owner_instance_id="gateway_1:finalizer",
        closing_reason="completed",
    )
    plan = _completed_plan(execution.scope)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT final_answer FROM turns WHERE id='t_1'").fetchone() == (None,)
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_receipts").fetchone() == (0,)
    prepared = await store.prepare_candidate(finalization, plan)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT final_answer FROM turns WHERE id='t_1'").fetchone() == (None,)
        assert connection.execute("SELECT COUNT(*) FROM turn_events").fetchone() == (0,)
    receipt = await store.commit_candidate(
        prepared,
        plan,
        selected_variant_key="primary_none",
        authz_snapshot_digest="auth_1",
        writer_admission_epoch=3,
    )
    assert receipt.receipt_id == "tr_1"
    confirmation = await store.confirm_commit(
        candidate_id=plan.candidate_id,
        candidate_projection_digest=plan.candidate_projection_digest,
    )
    assert confirmation.receipt == receipt
    assert (
        await store.commit_candidate(
            prepared,
            plan,
            selected_variant_key="primary_none",
            authz_snapshot_digest="auth_1",
            writer_admission_epoch=3,
        )
        == receipt
    )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT status,final_answer,current_terminal_receipt_id FROM turns WHERE id='t_1'").fetchone() == (
            "completed",
            "answer",
            "tr_1",
        )
        assert connection.execute("SELECT event_type FROM turn_events ORDER BY event_seq").fetchall() == [
            ("final_answer",),
            ("done",),
        ]


@pytest.mark.asyncio
async def test_ac_b1b_016_commit_ack_loss_has_three_way_confirmation(db_path: str) -> None:
    store = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=lambda point: (
            (_ for _ in ()).throw(RuntimeError("ack lost")) if point is TerminalProtocolFaultPoint.TERMINAL_AFTER_COMMIT_BEFORE_ACK else None
        ),
    )
    execution = await _start_execution(store)
    finalizer = TerminalFinalizer(store)
    receipt = await finalizer.finalize(
        FinalizeRequest(
            execution=execution,
            finalization_owner_instance_id="gateway_1:finalizer",
            closing_reason="completed",
            selected_variant_key="primary_none",
            authz_snapshot_digest="auth_1",
            writer_admission_epoch=3,
            candidate_builder=lambda _context: _completed_plan(execution.scope),
        )
    )
    assert receipt.receipt_id == "tr_1"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_receipts").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM turn_events").fetchone() == (2,)


@pytest.mark.asyncio
async def test_confirmation_recomputes_candidate_variant_and_event_truth(db_path: str) -> None:
    store = _store(db_path)
    execution = await _start_execution(store)
    finalization = await store.begin_finalization(
        execution,
        finalization_owner_instance_id="gateway_1:finalizer",
        closing_reason="completed",
    )
    plan = _completed_plan(execution.scope)
    prepared = await store.prepare_candidate(finalization, plan)
    await store.commit_candidate(
        prepared,
        plan,
        selected_variant_key="primary_none",
        authz_snapshot_digest="auth_1",
        writer_admission_epoch=3,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TRIGGER terminal_candidate_variants_immutable")
        connection.execute(
            "UPDATE turn_terminal_candidate_variants SET message_key='tampered' WHERE candidate_id=? AND variant_key='primary_none'",
            (plan.candidate_id,),
        )
        connection.commit()
    with pytest.raises(TerminalProtocolMalformedError, match="candidate reconstruction digest mismatch"):
        await store.confirm_commit(
            candidate_id=plan.candidate_id,
            candidate_projection_digest=plan.candidate_projection_digest,
        )


@pytest.mark.asyncio
async def test_ac_b1b_027_finalization_lease_takeover_reuses_candidate(db_path: str) -> None:
    store = _store(db_path)
    execution = await _start_execution(store)
    finalization = await store.begin_finalization(
        execution,
        finalization_owner_instance_id="gateway_1:finalizer",
        closing_reason="completed",
    )
    plan = _completed_plan(execution.scope)
    await store.prepare_candidate(finalization, plan)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turn_generations SET finalization_lease_expires_at=? WHERE turn_id='t_1' AND generation=1",
            ((_NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),),
        )
        connection.commit()
    reconciler = TerminalReconciler(store)
    discoveries = await reconciler.scan(scanner_id="scanner_1")
    assert len(discoveries) == 1
    processing = await reconciler.claim(discoveries[0], owner_instance_id="reconciler_1")
    loaded = await store.load_candidate_for_recovery(processing)
    assert loaded == plan
    receipt = await reconciler.converge(
        processing,
        candidate_builder=lambda _context: pytest.fail("prepared candidate must be reused"),
        selected_variant_key="recovery_none",
        authz_snapshot_digest="auth_recovery",
        writer_admission_epoch=3,
    )
    assert receipt.status is TerminalStatus.ABORTED
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT status FROM turns WHERE id='t_1'").fetchone() == ("aborted",)
        assert connection.execute(
            "SELECT state,resolution FROM turn_recovery_records WHERE recovery_id=?",
            (processing.recovery_id,),
        ).fetchone() == ("resolved", "receipt_committed")


@pytest.mark.asyncio
async def test_recovery_commits_interrupt_branch_and_confirms_ack_loss(
    db_path: str,
) -> None:
    store = _store(db_path)
    execution = await _start_execution(store)
    finalization = await store.begin_finalization(
        execution,
        finalization_owner_instance_id="gateway_1:finalizer",
        closing_reason="completed",
    )
    plan = _completed_plan(execution.scope)
    await store.prepare_candidate(finalization, plan)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turn_generations SET finalization_lease_expires_at=? WHERE turn_id='t_1'",
            ((_NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),),
        )
        connection.commit()
    faulting = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=lambda point: (
            (_ for _ in ()).throw(RuntimeError("ack lost")) if point is TerminalProtocolFaultPoint.TERMINAL_AFTER_COMMIT_BEFORE_ACK else None
        ),
    )
    reconciler = TerminalReconciler(faulting)
    processing = await reconciler.claim(
        (await reconciler.scan(scanner_id="scanner_1"))[0],
        owner_instance_id="reconciler_1",
    )
    receipt = await reconciler.converge(
        processing,
        candidate_builder=lambda _context: pytest.fail("prepared candidate must be reused"),
        # A normal candidate prepared before owner loss is immutable, but
        # recovery must select its safe RECOVERY_INTERRUPT branch even when a
        # stale caller asks for PRIMARY.
        selected_variant_key="primary_none",
        authz_snapshot_digest="auth_recovery",
        writer_admission_epoch=3,
    )
    assert receipt.status is TerminalStatus.ABORTED
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT status,final_answer FROM turns WHERE id='t_1'").fetchone() == (
            "aborted",
            None,
        )


@pytest.mark.asyncio
async def test_ac_b1b_046_unknown_write_effect_is_disclosed_and_not_retried(db_path: str) -> None:
    store = _store(db_path)
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
    await store.mark_effect_unknown(dispatched)
    finalization = await store.begin_finalization(
        execution,
        finalization_owner_instance_id="gateway_1:finalizer",
        closing_reason="completed",
    )
    snapshot = await store.read_effect_disclosure_snapshot(execution.scope)
    assert snapshot is not None
    with pytest.raises(TerminalProtocolMalformedError, match="effect disclosure"):
        await store.prepare_candidate(finalization, _completed_plan(execution.scope))
    plan = _with_effect_disclosure(_completed_plan(execution.scope), snapshot)
    await store.prepare_candidate(finalization, plan)
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
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT kind FROM turn_recovery_records WHERE recovery_id=?",
            (processing.recovery_id,),
        ).fetchone() == ("effect_unknown",)
        assert connection.execute("SELECT recovery_lock_reason FROM sessions WHERE id='s_1'").fetchone() == ("effect_outcome_unknown",)
    with pytest.raises(TerminalProtocolMalformedError, match="unsettled side effects"):
        await reconciler.converge(
            processing,
            candidate_builder=lambda _context: pytest.fail("prepared candidate must be reused"),
            selected_variant_key="recovery_none",
            authz_snapshot_digest="auth_recovery",
            writer_admission_epoch=3,
        )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_receipts").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_candidate_events WHERE event_type='effect_disclosure'").fetchone() == (1,)


@pytest.mark.asyncio
async def test_safe_effect_recovery_commits_frozen_disclosure_event(db_path: str) -> None:
    store = _store(db_path)
    execution = await _start_execution(store)
    await store.begin_effect_attempt(
        execution,
        attempt_id="eff_1",
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        provider="provider_1",
        action_ref="send_1",
        idempotency_or_fence_token=None,
        non_retriable=True,
    )
    finalization = await store.begin_finalization(
        execution,
        finalization_owner_instance_id="gateway_1:finalizer",
        closing_reason="interrupted",
    )
    snapshot = await store.read_effect_disclosure_snapshot(execution.scope)
    assert snapshot is not None
    plan = _with_effect_disclosure(_completed_plan(execution.scope), snapshot)
    await store.prepare_candidate(finalization, plan)
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
        candidate_builder=lambda _context: pytest.fail("prepared candidate must be reused"),
        selected_variant_key="recovery_none",
        authz_snapshot_digest="auth_recovery",
        writer_admission_epoch=3,
    )
    assert receipt.status is TerminalStatus.ABORTED
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT event_type FROM turn_events ORDER BY event_seq").fetchall() == [
            ("effect_disclosure",),
            ("done",),
        ]


@pytest.mark.asyncio
async def test_protocol_delete_wins_without_fabricating_receipt(db_path: str) -> None:
    store = _store(db_path)
    execution = await _start_execution(store)
    context = TerminalDeleteContext(
        scope=execution.scope,
        deletion_id="del_1",
        expected_generation_row_version=execution.generation_row_version,
        expected_candidate_id=None,
        expected_delivery_fence_epoch=0,
        actor_audit_ref="actor_audit_1",
        authz_snapshot_digest="auth_delete_1",
        policy_code="session_hard_delete",
        effect_identity_digest="effect_ids_1",
        effect_bundle_digest="effect_bundle_1",
        effect_audit_key_version="audit_key_v1",
        effect_key_required_until="2026-10-01T00:00:00Z",
    )
    result = await store.commit_protocol_delete(context)
    assert result.created is True
    replay = await store.commit_protocol_delete(context)
    assert replay.created is False
    assert replay.deletion_digest == result.deletion_digest
    with pytest.raises(TerminalProtocolMalformedError, match="replay identity conflict"):
        await store.commit_protocol_delete(replace(context, authz_snapshot_digest="different_auth"))
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state,recovery_resolution,terminal_receipt_id FROM turn_generations WHERE turn_id='t_1'").fetchone() == (
            "closed",
            "fenced_deleted",
            None,
        )
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_receipts").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM turn_events").fetchone() == (0,)


@pytest.mark.asyncio
async def test_ac_b1b_002_commit_failure_converges_non_durable(db_path: str) -> None:
    store = _store(db_path)
    execution = await _start_execution(store)
    finalization = await store.begin_finalization(
        execution,
        finalization_owner_instance_id="gateway_1:finalizer",
        closing_reason="completed",
    )
    plan = _completed_plan(execution.scope)
    prepared = await store.prepare_candidate(finalization, plan)
    failing_store = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=lambda point: (
            (_ for _ in ()).throw(RuntimeError("commit failed")) if point is TerminalProtocolFaultPoint.TERMINAL_BEFORE_COMMIT else None
        ),
    )
    with pytest.raises(RuntimeError, match="commit failed"):
        await failing_store.commit_candidate(
            prepared,
            plan,
            selected_variant_key="primary_none",
            authz_snapshot_digest="auth_1",
            writer_admission_epoch=3,
        )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_receipts").fetchone() == (0,)
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
        candidate_builder=lambda _context: pytest.fail("must reuse prepared candidate"),
        selected_variant_key="recovery_none",
        authz_snapshot_digest="auth_recovery",
        writer_admission_epoch=3,
    )
    assert receipt.status is TerminalStatus.ABORTED


@pytest.mark.asyncio
async def test_ac_b1b_003_kill_after_commit_replays_same_batch(db_path: str) -> None:
    store = _store(db_path)
    execution = await _start_execution(store)
    finalization = await store.begin_finalization(
        execution,
        finalization_owner_instance_id="gateway_1:finalizer",
        closing_reason="completed",
    )
    plan = _completed_plan(execution.scope)
    prepared = await store.prepare_candidate(finalization, plan)
    receipt = await store.commit_candidate(
        prepared,
        plan,
        selected_variant_key="primary_none",
        authz_snapshot_digest="auth_1",
        writer_admission_epoch=3,
    )
    reopened = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    checkpoint = await reopened.read_checkpoint(
        TerminalReadContext(
            team_id="team_1",
            session_id="s_1",
            turn_id="t_1",
            viewer="u_1",
            auth_fence_vector_digest=_AUTH_DIGEST,
        )
    )
    assert checkpoint.current_receipt_summary is not None
    assert checkpoint.current_receipt_summary.receipt_id == receipt.receipt_id
    assert [
        event.event_id
        for event in await reopened.list_protocol_events(
            TerminalReadContext(
                team_id="team_1",
                session_id="s_1",
                turn_id="t_1",
                viewer="u_1",
                auth_fence_vector_digest=_AUTH_DIGEST,
            )
        )
    ] == ["evt_answer_1", "evt_done_1"]


@pytest.mark.asyncio
async def test_ac_b1b_009_cancel_final_answer_has_one_receipt(db_path: str) -> None:
    store = _store(db_path)
    execution = await _start_execution(store)
    finalization = await store.begin_finalization(
        execution,
        finalization_owner_instance_id="gateway_1:finalizer",
        closing_reason="completed",
    )
    plan = _completed_plan(execution.scope)
    prepared = await store.prepare_candidate(finalization, plan)

    async def commit_once():
        return await TerminalProtocolStore(db_path, _clock=lambda: _NOW).commit_candidate(
            prepared,
            plan,
            selected_variant_key="primary_none",
            authz_snapshot_digest="auth_1",
            writer_admission_epoch=3,
        )

    first, second = await asyncio.gather(commit_once(), commit_once())
    assert first == second
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_receipts").fetchone() == (1,)


@pytest.mark.asyncio
async def test_ac_b1b_010_conflicting_candidate_digest_fails_closed(db_path: str) -> None:
    store = _store(db_path)
    execution = await _start_execution(store)
    finalization = await store.begin_finalization(
        execution,
        finalization_owner_instance_id="gateway_1:finalizer",
        closing_reason="completed",
    )
    plan = _completed_plan(execution.scope)
    await store.prepare_candidate(finalization, plan)
    conflicting = replace(
        plan,
        projection_extensions=({"kind": "test", "source_digest": "conflict"},),
    )
    with pytest.raises(TerminalProtocolMalformedError, match="digest conflict"):
        await store.prepare_candidate(finalization, conflicting)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_candidates").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_receipts").fetchone() == (0,)


def test_candidate_projection_extensions_reject_raw_content() -> None:
    scope = TerminalScope(team_id="team_1", session_id="s_1", turn_id="t_1", generation=1)
    with pytest.raises(ValueError, match="may not carry content"):
        replace(
            _completed_plan(scope),
            projection_extensions=({"kind": "effect_disclosure_v1", "raw_text": "secret"},),
        )


def test_terminal_batch_answer_must_be_root_final_answer() -> None:
    scope = TerminalScope(team_id="team_1", session_id="s_1", turn_id="t_1", generation=1)
    plan = _completed_plan(scope)
    primary = plan.primary_variant
    assert primary is not None
    worker_answer = replace(primary.events[0], actor_scope=ActorScope.STEP)
    with pytest.raises(ValueError, match="batch answer identity"):
        replace(primary, events=(worker_answer, primary.events[-1]))


async def _precommit_answer(store: TerminalProtocolStore, execution):
    proposal = await store.accept_event(
        execution,
        event_type="final_answer",
        actor_scope=ActorScope.ROOT,
        phase_scope=PhaseScope.IN_GENERATION,
        payload={"text": "precommitted answer"},
        publish_mode=EventPublishMode.BEFORE_LIVE,
        root_durability=RootDurability.COMMITTED,
    )
    await store.flush_through(execution, accepted_through=proposal.accept_ordinal)
    return proposal


@pytest.mark.asyncio
async def test_ac_b1b_011_precommitted_answer_identity_is_reused(db_path: str) -> None:
    store = _store(db_path)
    execution = await _start_execution(store)
    answer = await _precommit_answer(store, execution)
    finalization = await store.begin_finalization(
        execution,
        finalization_owner_instance_id="gateway_1:finalizer",
        closing_reason="completed",
    )
    plan = _precommitted_plan(
        execution.scope,
        answer_event_id=answer.event_id,
        answer_digest=answer.payload_digest,
        persisted_event_highwater=1,
    )
    prepared = await store.prepare_candidate(finalization, plan)
    await store.commit_candidate(
        prepared,
        plan,
        selected_variant_key="primary_none",
        authz_snapshot_digest="auth_1",
        writer_admission_epoch=3,
    )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT id,COUNT(*) FROM turn_events WHERE event_type='final_answer' GROUP BY id").fetchone() == (
            answer.event_id,
            1,
        )


@pytest.mark.asyncio
async def test_ac_b1b_012_no_answer_terminal_modes_do_not_emit_empty_answer(db_path: str) -> None:
    store = _store(db_path)
    execution = await _start_execution(store)
    finalization = await store.begin_finalization(
        execution,
        finalization_owner_instance_id="gateway_1:finalizer",
        closing_reason="interrupted",
    )
    plan = _completed_plan(execution.scope)
    await store.prepare_candidate(finalization, plan)
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
    recovery_prepared = await store.prepare_recovery_candidate(processing, plan)
    recovered = await store.commit_recovery_candidate(
        recovery_prepared,
        plan,
        selected_variant_key="recovery_none",
        authz_snapshot_digest="auth_recovery",
        writer_admission_epoch=3,
    )
    assert recovered.status is TerminalStatus.ABORTED
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_events WHERE event_type='final_answer'").fetchone() == (0,)
        assert connection.execute("SELECT final_answer FROM turns WHERE id='t_1'").fetchone() == (None,)


@pytest.mark.asyncio
async def test_ac_b1b_015_precommitted_answer_without_receipt_is_unfinalized(db_path: str) -> None:
    store = _store(db_path)
    execution = await _start_execution(store)
    answer = await _precommit_answer(store, execution)
    finalization = await store.begin_finalization(
        execution,
        finalization_owner_instance_id="gateway_1:finalizer",
        closing_reason="completed",
    )
    plan = _precommitted_plan(
        execution.scope,
        answer_event_id=answer.event_id,
        answer_digest=answer.payload_digest,
        persisted_event_highwater=1,
    )
    prepared = await store.prepare_candidate(finalization, plan)
    failing = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=lambda point: (
            (_ for _ in ()).throw(RuntimeError("rollback")) if point is TerminalProtocolFaultPoint.TERMINAL_BEFORE_COMMIT else None
        ),
    )
    with pytest.raises(RuntimeError, match="rollback"):
        await failing.commit_candidate(
            prepared,
            plan,
            selected_variant_key="primary_none",
            authz_snapshot_digest="auth_1",
            writer_admission_epoch=3,
        )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT status,final_answer FROM turns WHERE id='t_1'").fetchone() == (
            "running",
            None,
        )
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_receipts").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM turn_events WHERE id=?", (answer.event_id,)).fetchone() == (1,)


@pytest.mark.asyncio
async def test_ac_b1b_020_finalizer_waits_for_accepted_event_fence(db_path: str) -> None:
    store = _store(db_path)
    execution = await _start_execution(store)
    proposal = await store.accept_event(
        execution,
        event_type="progress",
        actor_scope=ActorScope.ROOT,
        phase_scope=PhaseScope.IN_GENERATION,
        payload={"step": 1},
    )
    finalization = await store.begin_finalization(
        execution,
        finalization_owner_instance_id="gateway_1:finalizer",
        closing_reason="completed",
    )
    premature = replace(
        _completed_plan(execution.scope),
        closing_accepted_through=1,
        persisted_event_highwater=0,
    )
    with pytest.raises(TerminalProtocolMalformedError, match="not persisted"):
        await store.prepare_candidate(finalization, premature)
    await store.flush_finalization_through(finalization)
    plan = replace(premature, persisted_event_highwater=1)
    await store.prepare_candidate(finalization, plan)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state,persisted_event_seq FROM turn_event_proposals WHERE proposal_id=?",
            (proposal.proposal_id,),
        ).fetchone() == ("persisted", 1)


@pytest.mark.asyncio
async def test_ac_b1b_025_unknown_outcome_locks_same_session(db_path: str) -> None:
    store = _store(db_path)
    execution = await _start_execution(store)
    await store.begin_finalization(
        execution,
        finalization_owner_instance_id="gateway_1:finalizer",
        closing_reason="completed",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turn_generations SET finalization_lease_expires_at=? WHERE turn_id='t_1'",
            ((_NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),),
        )
        connection.commit()
    discovery = (await store.list_recovery_candidates(scanner_id="scanner_1"))[0]
    await store.discover_recovery(discovery)
    rejected = await store.admit_or_steer_v1(
        TerminalAdmissionRequest(
            context=AdmissionContext(
                team_id="team_1",
                session_id="s_1",
                request_id="req_2",
                surface="web",
                cohort="bucket=1;config=rollout_1",
                actor="u_1",
                writer_instance_id="gateway_1",
                writer_lease_epoch=7,
                writer_admission_epoch=3,
                rollout_config_generation="rollout_1",
                auth_fence_vector_digest=_AUTH_DIGEST,
            ),
            turn_id="t_2",
            user_text="new message",
            execution_intent={"text": "new message"},
            presentation_locale="zh-CN",
            message_catalog_version=1,
        )
    )
    assert rejected.code is AdmissionCode.SESSION_RECOVERY_LOCKED


@pytest.mark.asyncio
async def test_ac_b1b_034_receipt_retry_has_no_projection_side_effect_duplication(db_path: str) -> None:
    store = _store(db_path)
    execution = await _start_execution(store)
    finalization = await store.begin_finalization(
        execution,
        finalization_owner_instance_id="gateway_1:finalizer",
        closing_reason="completed",
    )
    plan = _completed_plan(execution.scope)
    prepared = await store.prepare_candidate(finalization, plan)
    first = await store.commit_candidate(
        prepared,
        plan,
        selected_variant_key="primary_none",
        authz_snapshot_digest="auth_1",
        writer_admission_epoch=3,
    )
    second = await store.commit_candidate(
        prepared,
        plan,
        selected_variant_key="primary_none",
        authz_snapshot_digest="auth_1",
        writer_admission_epoch=3,
    )
    assert first == second
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_session_mutations").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM turn_events").fetchone() == (2,)
        assert connection.execute("SELECT tokens_total FROM sessions WHERE id='s_1'").fetchone() == (0,)


@pytest.mark.asyncio
async def test_ac_b1b_036_recovery_record_survives_observation_ttl(db_path: str) -> None:
    current = [_NOW]
    store = TerminalProtocolStore(db_path, _clock=lambda: current[0])
    execution = await _start_execution(store)
    await store.begin_finalization(
        execution,
        finalization_owner_instance_id="gateway_1:finalizer",
        closing_reason="completed",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turn_generations SET finalization_lease_expires_at=? WHERE turn_id='t_1'",
            ((_NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),),
        )
        connection.commit()
    record = await store.discover_recovery((await store.list_recovery_candidates(scanner_id="scanner_1"))[0])
    current[0] = _NOW + timedelta(days=30)
    processing = await store.claim_recovery(record, owner_instance_id="reconciler_1")
    assert processing.recovery_id == record.recovery_id
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM turn_recovery_records WHERE recovery_id=?",
            (record.recovery_id,),
        ).fetchone() == ("active",)


@pytest.mark.asyncio
async def test_expired_recovery_owner_can_be_taken_over(db_path: str) -> None:
    current = [_NOW]
    store = TerminalProtocolStore(db_path, _clock=lambda: current[0])
    execution = await _start_execution(store)
    await store.begin_finalization(
        execution,
        finalization_owner_instance_id="gateway_1:finalizer",
        closing_reason="completed",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turn_generations SET finalization_lease_expires_at=? WHERE turn_id='t_1'",
            ((_NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),),
        )
        connection.commit()
    discovery = (await store.list_recovery_candidates(scanner_id="scanner_1"))[0]
    record = await store.discover_recovery(discovery)
    first = await store.claim_recovery(record, owner_instance_id="reconciler_1")
    current[0] = _NOW + timedelta(seconds=31)
    rediscovered = await store.discover_recovery((await store.list_recovery_candidates(scanner_id="scanner_2"))[0])
    second = await store.claim_recovery(rediscovered, owner_instance_id="reconciler_2")
    assert second.recovery_claim_epoch == first.recovery_claim_epoch + 1
    assert second.finalization_epoch == first.finalization_epoch + 1


@pytest.mark.asyncio
async def test_ac_b1b_044_barrier_timeout_converges_by_effect_class(db_path: str) -> None:
    store = _store(db_path)
    execution = await _start_execution(store)
    finalizer = TerminalFinalizer(store)

    async def blocked(_context):
        await anyio.sleep(1)

    with pytest.raises(TimeoutError):
        await finalizer.finalize(
            FinalizeRequest(
                execution=execution,
                finalization_owner_instance_id="gateway_1:finalizer",
                closing_reason="completed",
                selected_variant_key="primary_none",
                authz_snapshot_digest="auth_1",
                writer_admission_epoch=3,
                candidate_builder=lambda _context: _completed_plan(execution.scope),
                producer_barrier=blocked,
                producer_barrier_timeout_seconds=0.01,
            )
        )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turn_generations SET lease_expires_at=? WHERE turn_id='t_1'",
            ((_NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),),
        )
        connection.commit()
    assert len(await store.list_recovery_candidates(scanner_id="scanner_1")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault", "expected_proposal_state"),
    [
        (TerminalProtocolFaultPoint.FINALIZE_AFTER_BARRIER, "accepted"),
        (TerminalProtocolFaultPoint.FINALIZE_AFTER_FLUSH, "persisted"),
    ],
)
async def test_finalizer_barrier_and_flush_faults_leave_recoverable_fence(
    db_path: str,
    fault: TerminalProtocolFaultPoint,
    expected_proposal_state: str,
) -> None:
    store = _store(db_path)
    execution = await _start_execution(store)
    await store.accept_event(
        execution,
        event_type="analysis_progress",
        actor_scope=ActorScope.ROOT,
        phase_scope=PhaseScope.IN_GENERATION,
        payload={"step": "accepted"},
    )
    finalizer = TerminalFinalizer(
        store,
        _fault_injector=lambda point: (_ for _ in ()).throw(RuntimeError(str(point))) if point is fault else None,
    )
    with pytest.raises(RuntimeError, match=str(fault)):
        await finalizer.finalize(
            FinalizeRequest(
                execution=execution,
                finalization_owner_instance_id="gateway_1:finalizer",
                closing_reason="completed",
                selected_variant_key="primary_none",
                authz_snapshot_digest="auth_1",
                writer_admission_epoch=3,
                candidate_builder=lambda _context: pytest.fail("fault must precede candidate build"),
            )
        )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state,closing_accepted_through FROM turn_generations").fetchone() == ("closing", 1)
        assert connection.execute("SELECT state FROM turn_event_proposals").fetchone() == (expected_proposal_state,)
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_candidates").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_receipts").fetchone() == (0,)


@pytest.mark.asyncio
async def test_recovery_flushes_accepted_prefix_without_restarting_execution(db_path: str) -> None:
    store = _store(db_path)
    execution = await _start_execution(store)
    proposal = await store.accept_event(
        execution,
        event_type="analysis_progress",
        actor_scope=ActorScope.ROOT,
        phase_scope=PhaseScope.IN_GENERATION,
        payload={"step": "durably accepted"},
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turn_generations SET lease_expires_at=? WHERE turn_id='t_1'",
            ((_NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),),
        )
        connection.commit()
    reconciler = TerminalReconciler(store)
    processing = await reconciler.claim(
        (await reconciler.scan(scanner_id="scanner_1"))[0],
        owner_instance_id="reconciler_1",
    )
    plan = _recovery_plan(
        execution.scope,
        closing_accepted_through=1,
        persisted_event_highwater=1,
    )
    build_snapshots = []

    def build_recovery_candidate(snapshot):
        build_snapshots.append(snapshot)
        return plan

    receipt = await reconciler.converge(
        processing,
        candidate_builder=build_recovery_candidate,
        selected_variant_key="recovery_none",
        authz_snapshot_digest="auth_recovery",
        writer_admission_epoch=3,
    )
    assert receipt.status is TerminalStatus.ABORTED
    assert len(build_snapshots) == 1
    assert build_snapshots[0].flush.persisted_event_highwater == 1
    assert [ticket.event_id for ticket in build_snapshots[0].flush.tickets] == [proposal.event_id]
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state,persisted_event_seq FROM turn_event_proposals").fetchone() == ("persisted", 1)
        assert connection.execute("SELECT id,event_seq FROM turn_events ORDER BY event_seq").fetchall() == [
            (proposal.event_id, 1),
            ("evt_done_1", 2),
        ]
        assert connection.execute("SELECT execution_started_at FROM turn_generations").fetchone() == ("2026-08-03T04:05:06.000000Z",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant,expected_state,expected_active",
    [
        (SessionVariant.DEFERRED, "deferred", None),
        (SessionVariant.DISCARD, "collecting", None),
        (SessionVariant.FOLLOWUP, "collecting", "t_followup_1"),
    ],
)
async def test_ac_b1b_045_receipt_side_effects_are_idempotent(
    db_path: str,
    variant: SessionVariant,
    expected_state: str,
    expected_active: str | None,
) -> None:
    store = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=lambda point: (
            (_ for _ in ()).throw(RuntimeError("ack lost")) if point is TerminalProtocolFaultPoint.TERMINAL_AFTER_COMMIT_BEFORE_ACK else None
        ),
    )
    execution = await _start_execution(store)
    steered = await store.admit_or_steer_v1(
        TerminalAdmissionRequest(
            context=AdmissionContext(
                team_id="team_1",
                session_id="s_1",
                request_id="req_2",
                surface="web",
                cohort="bucket=1;config=rollout_1",
                actor="u_1",
                writer_instance_id="gateway_1",
                writer_lease_epoch=7,
                writer_admission_epoch=3,
                rollout_config_generation="rollout_1",
                auth_fence_vector_digest=_AUTH_DIGEST,
            ),
            turn_id="t_2",
            user_text="follow up",
            execution_intent={"text": "follow up"},
            presentation_locale="zh-CN",
            message_catalog_version=1,
        )
    )
    assert steered.disposition is AdmissionDisposition.STEERED
    plan = _session_variant_plan(execution.scope, variant)
    receipt = await TerminalFinalizer(store).finalize(
        FinalizeRequest(
            execution=execution,
            finalization_owner_instance_id="gateway_1:finalizer",
            closing_reason="completed",
            selected_variant_key=f"primary_{variant.value}",
            authz_snapshot_digest="auth_1",
            writer_admission_epoch=3,
            candidate_builder=lambda _context: plan,
        )
    )
    assert receipt.selected_session_variant is variant
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT steer_buffer_state,active_turn_id FROM sessions WHERE id='s_1'").fetchone() == (
            expected_state,
            expected_active,
        )
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_session_mutations").fetchone() == (1,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault", "expected_candidate", "expected_version", "expected_staged_count"),
    [
        (TerminalProtocolFaultPoint.CANDIDATE_BEFORE_PREPARE_CAS, None, 4, 0),
        (TerminalProtocolFaultPoint.CANDIDATE_AFTER_PREPARE, "tc_1", 5, 1),
    ],
)
async def test_candidate_prepare_faults_preserve_the_correct_commit_boundary(
    db_path: str,
    fault: TerminalProtocolFaultPoint,
    expected_candidate: str | None,
    expected_version: int,
    expected_staged_count: int,
) -> None:
    store = _store(db_path)
    execution = await _start_execution(store)
    finalization = await store.begin_finalization(
        execution,
        finalization_owner_instance_id="gateway_1:finalizer",
        closing_reason="completed",
    )
    faulting = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=lambda point: (_ for _ in ()).throw(RuntimeError(str(point))) if point is fault else None,
    )
    with pytest.raises(RuntimeError, match=str(fault)):
        await faulting.prepare_candidate(finalization, _completed_plan(execution.scope))
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT terminal_candidate_id,row_version FROM turn_generations").fetchone() == (
            expected_candidate,
            expected_version,
        )
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_candidates").fetchone() == (expected_staged_count,)
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_candidate_variants").fetchone() == (expected_staged_count * 2,)
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_candidate_events").fetchone() == (expected_staged_count * 3,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        TerminalProtocolFaultPoint.TERMINAL_BEFORE_SEQ_ALLOC,
        TerminalProtocolFaultPoint.TERMINAL_AFTER_SEQ_ALLOC,
        TerminalProtocolFaultPoint.TERMINAL_BEFORE_COMMIT,
    ],
)
async def test_terminal_commit_faults_rollback_receipt_batch(
    db_path: str,
    fault: TerminalProtocolFaultPoint,
) -> None:
    store = _store(db_path)
    execution = await _start_execution(store)
    finalization = await store.begin_finalization(
        execution,
        finalization_owner_instance_id="gateway_1:finalizer",
        closing_reason="completed",
    )
    plan = _completed_plan(execution.scope)
    prepared = await store.prepare_candidate(finalization, plan)
    faulting = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=lambda point: (_ for _ in ()).throw(RuntimeError(str(point))) if point is fault else None,
    )
    with pytest.raises(RuntimeError, match=str(fault)):
        await faulting.commit_candidate(
            prepared,
            plan,
            selected_variant_key="primary_none",
            authz_snapshot_digest="auth_1",
            writer_admission_epoch=3,
        )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT event_sequence_next,current_terminal_receipt_id FROM turns").fetchone() == (1, None)
        assert connection.execute("SELECT state FROM turn_terminal_candidates").fetchone() == ("prepared",)
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_receipts").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_session_mutations").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM turn_events").fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        TerminalProtocolFaultPoint.DELETE_AFTER_LEDGER,
        TerminalProtocolFaultPoint.DELETE_AFTER_GENERATION_CAS,
        TerminalProtocolFaultPoint.DELETE_BEFORE_COMMIT,
    ],
)
async def test_protocol_delete_faults_rollback_all_fences(
    db_path: str,
    fault: TerminalProtocolFaultPoint,
) -> None:
    store = _store(db_path)
    execution = await _start_execution(store)
    context = TerminalDeleteContext(
        scope=execution.scope,
        deletion_id="del_fault_1",
        expected_generation_row_version=execution.generation_row_version,
        expected_candidate_id=None,
        expected_delivery_fence_epoch=0,
        actor_audit_ref="actor_audit_1",
        authz_snapshot_digest="auth_delete_1",
        policy_code="session_hard_delete",
        effect_identity_digest="effect_ids_1",
        effect_bundle_digest="effect_bundle_1",
        effect_audit_key_version="audit_key_v1",
        effect_key_required_until="2026-10-01T00:00:00Z",
    )
    faulting = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=lambda point: (_ for _ in ()).throw(RuntimeError(str(point))) if point is fault else None,
    )
    with pytest.raises(RuntimeError, match=str(fault)):
        await faulting.commit_protocol_delete(context)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state,row_version FROM turn_generations").fetchone() == (
            "running",
            execution.generation_row_version,
        )
        assert connection.execute("SELECT active_turn_id,recovery_lock_turn_id FROM sessions").fetchone() == ("t_1", None)
        assert connection.execute("SELECT delivery_fence_epoch FROM turns").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_deletion_ledgers").fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        TerminalProtocolFaultPoint.RECOVERY_DISCOVERY_AFTER_ROW_CREATE,
        TerminalProtocolFaultPoint.RECOVERY_AFTER_STATE_CAS,
    ],
)
async def test_recovery_discovery_faults_rollback_record_generation_and_session(
    db_path: str,
    fault: TerminalProtocolFaultPoint,
) -> None:
    store = _store(db_path)
    await _start_execution(store)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turn_generations SET lease_expires_at=? WHERE turn_id='t_1'",
            ((_NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),),
        )
        connection.commit()
    discovery = (await store.list_recovery_candidates(scanner_id="scanner_1"))[0]
    faulting = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=lambda point: (_ for _ in ()).throw(RuntimeError(str(point))) if point is fault else None,
    )
    with pytest.raises(RuntimeError, match=str(fault)):
        await faulting.discover_recovery(discovery)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state FROM turn_generations").fetchone() == ("running",)
        assert connection.execute("SELECT recovery_lock_turn_id FROM sessions").fetchone() == (None,)
        assert connection.execute("SELECT COUNT(*) FROM turn_recovery_records").fetchone() == (0,)


@pytest.mark.asyncio
async def test_recovery_owner_transfer_fault_rolls_back_both_epochs(db_path: str) -> None:
    store = _store(db_path)
    await _start_execution(store)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turn_generations SET lease_expires_at=? WHERE turn_id='t_1'",
            ((_NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),),
        )
        connection.commit()
    record = await store.discover_recovery((await store.list_recovery_candidates(scanner_id="scanner_1"))[0])
    faulting = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=lambda point: (
            (_ for _ in ()).throw(RuntimeError(str(point))) if point is TerminalProtocolFaultPoint.FINALIZATION_AFTER_OWNER_TRANSFER else None
        ),
    )
    with pytest.raises(RuntimeError, match="FINALIZATION_AFTER_OWNER_TRANSFER"):
        await faulting.claim_recovery(record, owner_instance_id="reconciler_1")
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT recovery_owner_instance_id,recovery_claim_epoch FROM turn_recovery_records").fetchone() == (None, 0)
        assert connection.execute("SELECT finalization_epoch FROM turn_generations").fetchone() == (record.finalization_epoch,)


@pytest.mark.asyncio
async def test_finalize_after_closing_fault_preserves_durable_closing_state(db_path: str) -> None:
    store = _store(db_path)
    execution = await _start_execution(store)
    faulting = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=lambda point: (
            (_ for _ in ()).throw(RuntimeError(str(point))) if point is TerminalProtocolFaultPoint.FINALIZE_AFTER_CLOSING else None
        ),
    )
    with pytest.raises(RuntimeError, match="FINALIZE_AFTER_CLOSING"):
        await TerminalFinalizer(faulting).finalize(
            FinalizeRequest(
                execution=execution,
                finalization_owner_instance_id="gateway_1:finalizer",
                closing_reason="completed",
                selected_variant_key="primary_none",
                authz_snapshot_digest="auth_1",
                writer_admission_epoch=3,
                candidate_builder=lambda _context: pytest.fail("fault must precede candidate build"),
            )
        )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state,terminal_candidate_id FROM turn_generations").fetchone() == ("closing", None)
        assert connection.execute("SELECT steer_buffer_state FROM sessions").fetchone() == ("frozen",)


@pytest.mark.asyncio
async def test_recovery_scan_fault_is_read_only(db_path: str) -> None:
    store = _store(db_path)
    await _start_execution(store)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turn_generations SET lease_expires_at=? WHERE turn_id='t_1'",
            ((_NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),),
        )
        connection.commit()
    faulting = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=lambda point: (
            (_ for _ in ()).throw(RuntimeError(str(point))) if point is TerminalProtocolFaultPoint.RECOVERY_DISCOVERY_AFTER_SCAN else None
        ),
    )
    with pytest.raises(RuntimeError, match="RECOVERY_DISCOVERY_AFTER_SCAN"):
        await faulting.list_recovery_candidates(scanner_id="scanner_1")
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state FROM turn_generations").fetchone() == ("running",)
        assert connection.execute("SELECT COUNT(*) FROM turn_recovery_records").fetchone() == (0,)


@pytest.mark.asyncio
async def test_recovery_candidate_prepare_fault_rolls_back_staging(db_path: str) -> None:
    store = _store(db_path)
    execution = await _start_execution(store)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turn_generations SET lease_expires_at=? WHERE turn_id='t_1'",
            ((_NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),),
        )
        connection.commit()
    reconciler = TerminalReconciler(store)
    processing = await reconciler.claim(
        (await reconciler.scan(scanner_id="scanner_1"))[0],
        owner_instance_id="reconciler_1",
    )
    plan = _recovery_plan(
        execution.scope,
        closing_accepted_through=0,
        persisted_event_highwater=0,
    )
    faulting = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=lambda point: (
            (_ for _ in ()).throw(RuntimeError(str(point))) if point is TerminalProtocolFaultPoint.RECOVERY_AFTER_CANDIDATE_PREPARE else None
        ),
    )
    with pytest.raises(RuntimeError, match="RECOVERY_AFTER_CANDIDATE_PREPARE"):
        await faulting.prepare_recovery_candidate(processing, plan)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT terminal_candidate_id,row_version FROM turn_generations").fetchone() == (
            None,
            processing.generation_row_version,
        )
        assert connection.execute("SELECT COUNT(*) FROM turn_terminal_candidates").fetchone() == (0,)
