from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.migrations.baseline_schema import initial_schema
from bowhead_core.terminal_auth_fence import (
    AuthFenceFaultPoint,
    AuthorizedFrame,
    TerminalAuthFenceCoordinator,
)
from bowhead_core.terminal_event_writer import TurnEventWriter
from bowhead_core.terminal_generation_bus import GenerationBusFaultPoint, GenerationEventBusRegistry
from bowhead_core.terminal_protocol import (
    ActorScope,
    AdmissionContext,
    AuthFenceEpoch,
    AuthFenceFrameContext,
    AuthFenceResolveContext,
    AuthFenceScope,
    AuthFenceScopeKind,
    AuthFenceVector,
    ExecutionGenerationContext,
    GenerationClaimContext,
    GenerationPointer,
    PhaseScope,
    SubscriberFenceToken,
    TerminalAdmissionRequest,
    TerminalProtocolMalformedError,
    TerminalScope,
    canonical_digest_v1,
    canonical_json_v1,
)
from bowhead_core.terminal_protocol_store import (
    TerminalProtocolFaultPoint,
    TerminalProtocolStore,
    TerminalReadContext,
    open_terminal_protocol_connection,
)
from bowhead_core.terminal_runtime_factory import (
    terminal_auth_resolve_context_values,
)
from bowhead_core.terminal_stream import (
    AuthorizedFence,
    ReplayHandoffFaultPoint,
    TerminalReplayHandoff,
    TransitionWatchMultiplexer,
    TransitionWatchSubscription,
)

_NOW = datetime(2026, 8, 3, 9, 10, 11, tzinfo=UTC)


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    path = tmp_path / "bowhead.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()
    upgrade(f"sqlite+aiosqlite:///{path}", target="core")
    return str(path)


def _scope(*, generation: int = 1, turn_id: str = "t_1") -> TerminalScope:
    return TerminalScope(team_id="team_1", session_id="s_1", turn_id=turn_id, generation=generation)


def _auth_scopes(*, resource_ref: str = "t_1") -> tuple[AuthFenceScope, ...]:
    return (
        AuthFenceScope(AuthFenceScopeKind.USER_ACCOUNT, "__global__", "principal_u1"),
        AuthFenceScope(AuthFenceScopeKind.SESSION_VISIBILITY, "team_1", "session_s1"),
        AuthFenceScope(AuthFenceScopeKind.RESOURCE_GRANT, "team_1", resource_ref),
        AuthFenceScope(AuthFenceScopeKind.TEAM_MEMBERSHIP, "team_1", "member_u1"),
    )


def _resolve_context(*, resource_ref: str = "t_1") -> AuthFenceResolveContext:
    return AuthFenceResolveContext(
        team_id="team_1",
        viewer="u_1",
        resource_ref=resource_ref,
        session_id="s_1",
        required_scopes=_auth_scopes(resource_ref=resource_ref),
    )


def _insert_runtime_fixture(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
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
            VALUES ('s_1','ws_1','u_1','Session','t','t','active',0,0,'[]')
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


async def _start_execution(db_path: str) -> tuple[TerminalProtocolStore, ExecutionGenerationContext]:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    admission_vector = await store.ensure_auth_scope_epochs(
        terminal_auth_resolve_context_values(
            team_id="team_1",
            session_id="s_1",
            user_id="u_1",
            resource_ref="t_1",
        )
    )
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
                auth_fence_vector_digest=(admission_vector.auth_fence_vector_digest),
            ),
            turn_id="t_1",
            user_text="hello",
            execution_intent={"text": "hello"},
            presentation_locale="zh-CN",
            message_catalog_version=1,
        )
    )
    assert admitted.scope == _scope()
    claimed = await store.claim_generation(
        GenerationClaimContext(
            scope=_scope(),
            expected_generation_row_version=1,
            expected_session_claim_epoch=0,
            writer_lease_epoch=7,
            owner_instance_id="gateway_1",
        )
    )
    assert claimed.context is not None
    return store, await store.mark_execution_started(claimed.context)


async def _accept_and_flush(writer: TurnEventWriter, count: int) -> None:
    for ordinal in range(1, count + 1):
        await writer.accept_event(
            event_type="step_progress",
            actor_scope=ActorScope.STEP,
            phase_scope=PhaseScope.IN_GENERATION,
            payload={"ordinal": ordinal},
        )
    await writer.flush_through(count)


def _commit_completed_receipt(
    db_path: str,
    *,
    answer_event_id: str,
    answer_event_seq: int,
    done_status: str = "completed",
) -> None:
    done_payload = {
        "candidate_id": "ttc_1",
        "generation": 1,
        "message_key": "terminal.completed",
        "protocol": 1,
        "reason": None,
        "receipt_id": "trc_1",
        "required_manifest_template_digest": "manifest_template",
        "selected_variant_key": "primary_none",
        "status": done_status,
    }
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        event = connection.execute(
            """
            SELECT event_type,actor_scope,phase_scope,payload_digest
            FROM turn_events WHERE id=?
            """,
            (answer_event_id,),
        ).fetchone()
        assert event is not None
        row_digest = canonical_digest_v1(
            {
                "actor_scope": event[1],
                "event_id": answer_event_id,
                "event_seq": answer_event_seq,
                "event_type": event[0],
                "payload_digest": event[3],
                "phase_scope": event[2],
            }
        )
        manifest = canonical_json_v1(
            [
                {
                    "actor_scope": event[1],
                    "canonical_row_digest": row_digest,
                    "event_id": answer_event_id,
                    "event_seq": answer_event_seq,
                    "event_type": event[0],
                    "phase_scope": event[2],
                }
            ]
        )
        connection.execute(
            """
            INSERT INTO turn_terminal_candidates(
              candidate_id,turn_id,generation,candidate_origin,reserved_receipt_id,
              reserved_terminal_event_id,prepared_execution_claim_epoch,
              finalization_epoch_at_prepare,primary_status,primary_message_key,
              closing_accepted_through,persisted_event_highwater,
              terminal_event_counts_by_variant_json,candidate_projection_digest,
              canonicalization_version,state,session_disposition_plan_json,
              session_disposition_plan_digest,projection_variant_digests_json,
              projection_extensions_json,created_at,prepared_at,committed_at
            ) VALUES (
              'ttc_1','t_1',1,'normal','trc_1','evt_done',1,1,
              'completed','terminal.completed',1,1,'{}','dc_1',1,'committed',
              '{}','plan_1','{}','{}','t','t','t'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO turn_terminal_candidate_variants(
              candidate_id,variant_key,outcome_variant,session_variant,status,message_key,
              final_answer_mode,final_answer_event_id,final_answer_digest,
              required_manifest_template_digest,session_mutation_template_digest,variant_digest
            ) VALUES (
              'ttc_1','primary_none','primary','none','completed','terminal.completed',
              'precommitted',?,'answer_digest','manifest_template','session_template','variant_digest'
            )
            """,
            (answer_event_id,),
        )
        connection.execute(
            """
            INSERT INTO turn_terminal_receipts(
              receipt_id,turn_id,generation,candidate_id,protocol_version,status,message_key,
              final_answer_mode,final_answer_event_id,selected_variant_key,
              selected_outcome_variant,selected_session_variant,terminal_event_id,
              first_event_seq,last_event_seq,persisted_event_highwater,
              required_event_manifest_json,candidate_projection_digest,
              selected_projection_digest,selected_manifest_digest,
              selected_session_mutation_digest,projection_extensions_json,committed_at
            ) VALUES (
              'trc_1','t_1',1,'ttc_1',1,'completed','terminal.completed',
              'precommitted',?,'primary_none','primary','none','evt_done',1,2,1,?,
              'dc_1','dr_1','manifest_1','mutation_1','{}','t'
            )
            """,
            (answer_event_id, manifest),
        )
        connection.execute(
            """
            INSERT INTO turn_events(
              id,turn_id,seq,event_type,payload_json,occurred_at,protocol_version,generation,
              event_seq,actor_scope,phase_scope,payload_digest,terminal_receipt_id
            ) VALUES (
              'evt_done','t_1',2,'done',?,'t',1,1,2,'root','in_generation',?,'trc_1'
            )
            """,
            (canonical_json_v1(done_payload), canonical_digest_v1(done_payload)),
        )
        connection.execute(
            """
            UPDATE turn_generations
            SET state='closed',row_version=row_version+1,closing_accepted_through=1,
                terminal_candidate_id='ttc_1',terminal_receipt_id='trc_1',closed_at='t'
            WHERE turn_id='t_1' AND generation=1
            """
        )
        connection.execute(
            """
            UPDATE sessions
            SET active_turn_id=NULL,active_turn_protocol_version=NULL,
                active_turn_generation=NULL,active_turn_claim_epoch=NULL
            WHERE id='s_1'
            """
        )
        connection.execute(
            """
            UPDATE turns
            SET status='completed',last_terminal_generation=1,
                current_terminal_receipt_id='trc_1',event_sequence_next=3
            WHERE id='t_1'
            """
        )
        connection.commit()


@pytest.mark.asyncio
async def test_auth_fence_vector_digest_uses_only_canonical_scope_rows(db_path: str) -> None:
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    vector = await store.ensure_auth_scope_epochs(_resolve_context())
    assert [entry.scope.scope_kind for entry in vector.entries] == [
        AuthFenceScopeKind.TEAM_MEMBERSHIP,
        AuthFenceScopeKind.RESOURCE_GRANT,
        AuthFenceScopeKind.SESSION_VISIBILITY,
        AuthFenceScopeKind.USER_ACCOUNT,
    ]
    assert vector.auth_fence_vector_digest == canonical_digest_v1([entry.canonical_projection() for entry in vector.entries])
    assert await store.read_auth_fence_vector(_resolve_context()) == vector


@pytest.mark.asyncio
async def test_auth_scope_ensure_fault_rolls_back_all_missing_rows(db_path: str) -> None:
    def fail(point: TerminalProtocolFaultPoint) -> None:
        if point is TerminalProtocolFaultPoint.AUTH_SCOPE_AFTER_ENSURE:
            raise RuntimeError("injected ensure fault")

    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW, _fault_injector=fail)
    with pytest.raises(RuntimeError, match="ensure fault"):
        await store.ensure_auth_scope_epochs(_resolve_context())
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM terminal_auth_scope_epochs").fetchone() == (0,)


@pytest.mark.asyncio
async def test_checkpoint_is_coherent_viewer_bound_and_never_synthesizes_done(db_path: str) -> None:
    _insert_runtime_fixture(db_path)
    store, context = await _start_execution(db_path)
    vector = await store.ensure_auth_scope_epochs(_resolve_context())
    read = TerminalReadContext(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        viewer="u_1",
        auth_fence_vector_digest=vector.auth_fence_vector_digest,
    )
    first = await store.read_checkpoint(read)
    assert first.generation_pointer == GenerationPointer(1, 1, 0)
    assert first.subscriber_fence_token == SubscriberFenceToken(1, 1, 0, vector.auth_fence_vector_digest)
    assert first.persistent_event_highwater == 0
    assert first.current_receipt_summary is None
    assert first.required_event_manifest == ()
    assert first.session_active_claim is not None
    assert first.session_active_claim.claim_epoch == 1

    buses = GenerationEventBusRegistry()
    bus = await buses.get_or_create(_scope(), claim_epoch=1)
    writer = TurnEventWriter(store, bus, context, batch_delay_seconds=60)
    await _accept_and_flush(writer, 1)
    second = await store.read_checkpoint(read)
    assert second.persistent_event_highwater == 1
    assert second.checkpoint_token != first.checkpoint_token
    assert second.current_receipt_summary is None
    await writer.close()


@pytest.mark.asyncio
async def test_checkpoint_validates_committed_receipt_event_dependencies(db_path: str) -> None:
    _insert_runtime_fixture(db_path)
    store, context = await _start_execution(db_path)
    vector = await store.ensure_auth_scope_epochs(_resolve_context())
    bus = await GenerationEventBusRegistry().get_or_create(_scope(), claim_epoch=1)
    writer = TurnEventWriter(store, bus, context, batch_delay_seconds=60)
    answer = await writer.accept_event(
        event_type="final_answer",
        actor_scope=ActorScope.ROOT,
        phase_scope=PhaseScope.IN_GENERATION,
        payload={"markdown": "42"},
    )
    await writer.flush_through(1)
    await writer.close()
    _commit_completed_receipt(
        db_path,
        answer_event_id=answer.event_id,
        answer_event_seq=1,
    )

    checkpoint = await store.read_checkpoint(
        TerminalReadContext(
            team_id="team_1",
            session_id="s_1",
            turn_id="t_1",
            viewer="u_1",
            auth_fence_vector_digest=vector.auth_fence_vector_digest,
        )
    )
    assert checkpoint.current_receipt_summary is not None
    assert checkpoint.current_receipt_summary.receipt_id == "trc_1"
    assert checkpoint.persistent_event_highwater == 2
    assert [item.event_id for item in checkpoint.required_event_manifest] == [answer.event_id]


@pytest.mark.asyncio
async def test_checkpoint_rejects_done_status_that_disagrees_with_receipt(db_path: str) -> None:
    _insert_runtime_fixture(db_path)
    store, context = await _start_execution(db_path)
    vector = await store.ensure_auth_scope_epochs(_resolve_context())
    bus = await GenerationEventBusRegistry().get_or_create(_scope(), claim_epoch=1)
    writer = TurnEventWriter(store, bus, context, batch_delay_seconds=60)
    answer = await writer.accept_event(
        event_type="final_answer",
        actor_scope=ActorScope.ROOT,
        phase_scope=PhaseScope.IN_GENERATION,
        payload={"markdown": "42"},
    )
    await writer.flush_through(1)
    await writer.close()
    _commit_completed_receipt(
        db_path,
        answer_event_id=answer.event_id,
        answer_event_seq=1,
        done_status="failed",
    )

    with pytest.raises(TerminalProtocolMalformedError, match="terminal event envelope"):
        await store.read_checkpoint(
            TerminalReadContext(
                team_id="team_1",
                session_id="s_1",
                turn_id="t_1",
                viewer="u_1",
                auth_fence_vector_digest=vector.auth_fence_vector_digest,
            )
        )


@pytest.mark.asyncio
async def test_ac_b1b_023_two_checkpoint_handoff_never_synthesizes_done(db_path: str) -> None:
    _insert_runtime_fixture(db_path)
    store, context = await _start_execution(db_path)
    vector = await store.ensure_auth_scope_epochs(_resolve_context())
    buses = GenerationEventBusRegistry()
    bus = await buses.get_or_create(_scope(), claim_epoch=1)
    writer = TurnEventWriter(store, bus, context, batch_delay_seconds=60)
    await _accept_and_flush(writer, 2)

    async def authorize() -> AuthorizedFence:
        return AuthorizedFence(resolve_context=_resolve_context(), vector=await store.read_auth_fence_vector(_resolve_context()))

    handoff = TerminalReplayHandoff(store, buses)
    result = await handoff.connect(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        client_cursor=0,
        authorize=authorize,
    )
    assert result.live
    assert [event.event_seq for event in result.replay_events] == [1, 2]
    assert result.overlap_events == ()
    assert result.promote_certificate is not None
    assert result.promote_certificate.subscriber_fence_token.auth_fence_vector_digest == vector.auth_fence_vector_digest
    assert all(event.event_type != "done" for event in result.replay_events)

    third = await writer.accept_event(
        event_type="step_done",
        actor_scope=ActorScope.STEP,
        phase_scope=PhaseScope.IN_GENERATION,
        payload={"ok": True},
    )
    await writer.flush_through(3)
    assert result.subscription is not None
    live_event = await asyncio.wait_for(anext(result.subscription.__aiter__()), timeout=1)
    assert live_event.event_id == third.event_id
    await result.subscription.aclose()
    await writer.close()


@pytest.mark.asyncio
async def test_handoff_rejects_authorization_proof_for_another_turn(db_path: str) -> None:
    _insert_runtime_fixture(db_path)
    store, _context = await _start_execution(db_path)
    wrong_resolve = _resolve_context(resource_ref="t_other")
    wrong_vector = await store.ensure_auth_scope_epochs(wrong_resolve)

    async def authorize() -> AuthorizedFence:
        return AuthorizedFence(resolve_context=wrong_resolve, vector=wrong_vector)

    with pytest.raises(TerminalProtocolMalformedError, match="wrong resource identity"):
        await TerminalReplayHandoff(store, GenerationEventBusRegistry()).connect(
            team_id="team_1",
            session_id="s_1",
            turn_id="t_1",
            client_cursor=0,
            authorize=authorize,
        )


@pytest.mark.asyncio
async def test_handoff_rejects_client_cursor_beyond_durable_highwater(db_path: str) -> None:
    _insert_runtime_fixture(db_path)
    store, _context = await _start_execution(db_path)
    await store.ensure_auth_scope_epochs(_resolve_context())

    async def authorize() -> AuthorizedFence:
        return AuthorizedFence(
            resolve_context=_resolve_context(),
            vector=await store.read_auth_fence_vector(_resolve_context()),
        )

    with pytest.raises(TerminalProtocolMalformedError, match="cursor exceeds"):
        await TerminalReplayHandoff(store, GenerationEventBusRegistry()).connect(
            team_id="team_1",
            session_id="s_1",
            turn_id="t_1",
            client_cursor=1,
            authorize=authorize,
        )


@pytest.mark.asyncio
async def test_ac_b1b_055_fanout_disconnect_replays_same_event_ids_once(db_path: str) -> None:
    _insert_runtime_fixture(db_path)
    store, context = await _start_execution(db_path)
    await store.ensure_auth_scope_epochs(_resolve_context())

    def fail(point: GenerationBusFaultPoint) -> None:
        if point is GenerationBusFaultPoint.FANOUT_BEFORE_APPEND:
            raise RuntimeError("injected disconnect")

    buses = GenerationEventBusRegistry(_fault_injector=fail)
    bus = await buses.get_or_create(_scope(), claim_epoch=1)
    writer = TurnEventWriter(store, bus, context, batch_delay_seconds=60)
    handle = await writer.accept_event(
        event_type="step_progress",
        actor_scope=ActorScope.STEP,
        phase_scope=PhaseScope.IN_GENERATION,
        payload={"ordinal": 1},
    )
    persisted = await writer.flush_through(1)
    assert await handle.persisted() == persisted[0]
    assert bus.fanout_failed

    async def authorize() -> AuthorizedFence:
        return AuthorizedFence(
            resolve_context=_resolve_context(),
            vector=await store.read_auth_fence_vector(_resolve_context()),
        )

    result = await TerminalReplayHandoff(store, buses).connect(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        client_cursor=0,
        authorize=authorize,
    )
    assert not result.live
    assert [event.event_id for event in result.replay_events] == [handle.event_id]
    assert result.overlap_events == ()
    await writer.close()


@pytest.mark.asyncio
async def test_handoff_retries_when_generation_changes_before_c3(db_path: str) -> None:
    _insert_runtime_fixture(db_path)
    store, context = await _start_execution(db_path)
    await store.ensure_auth_scope_epochs(_resolve_context())
    buses = GenerationEventBusRegistry()
    bus = await buses.get_or_create(_scope(), claim_epoch=1)
    writer = TurnEventWriter(store, bus, context, batch_delay_seconds=60)
    await _accept_and_flush(writer, 1)
    transitioned = False

    async def fault(point: ReplayHandoffFaultPoint) -> None:
        nonlocal transitioned
        if point is ReplayHandoffFaultPoint.REPLAY_BEFORE_PROMOTE and not transitioned:
            transitioned = True
            async with open_terminal_protocol_connection(db_path) as connection:
                await connection.execute("BEGIN IMMEDIATE")
                await connection.execute(
                    """
                    INSERT INTO turn_generations(
                      turn_id,generation,protocol_version,kind,prior_generation,
                      execution_intent_id,execution_intent_digest,opened_at,state
                    ) VALUES ('t_1',2,1,'execution',1,'req_2','digest_2','t','opened')
                    """,
                )
                await connection.execute(
                    "UPDATE turns SET current_generation=2,generation_transition_epoch=2 WHERE id='t_1'",
                )
                await connection.execute(
                    """
                    UPDATE sessions SET active_turn_generation=2,active_turn_claim_epoch=0
                    WHERE id='s_1' AND active_turn_id='t_1'
                    """,
                )
                await connection.commit()

    async def authorize() -> AuthorizedFence:
        return AuthorizedFence(resolve_context=_resolve_context(), vector=await store.read_auth_fence_vector(_resolve_context()))

    result = await TerminalReplayHandoff(store, buses, _fault_injector=fault).connect(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        client_cursor=0,
        authorize=authorize,
    )
    assert transitioned
    assert not result.live
    assert result.checkpoint.current_generation == 2
    assert [event.event_seq for event in result.replay_events] == [1]
    assert all(event.event_type != "done" for event in result.replay_events)
    await writer.close()


@pytest.mark.asyncio
async def test_handoff_retries_when_auth_vector_changes_before_c3(db_path: str) -> None:
    _insert_runtime_fixture(db_path)
    store, context = await _start_execution(db_path)
    old_vector = await store.ensure_auth_scope_epochs(_resolve_context())
    buses = GenerationEventBusRegistry()
    bus = await buses.get_or_create(_scope(), claim_epoch=1)
    writer = TurnEventWriter(store, bus, context, batch_delay_seconds=60)
    await _accept_and_flush(writer, 1)
    revoked = False

    async def fault(point: ReplayHandoffFaultPoint) -> None:
        nonlocal revoked
        if point is not ReplayHandoffFaultPoint.REPLAY_BEFORE_PROMOTE or revoked:
            return
        revoked = True
        async with open_terminal_protocol_connection(db_path) as connection:
            await connection.execute("BEGIN IMMEDIATE")
            for entry in old_vector.entries:
                await connection.execute(
                    """
                    UPDATE terminal_auth_scope_epochs
                    SET epoch=epoch+1,row_version=row_version+1,updated_at='revoked'
                    WHERE scope_kind=? AND team_id=? AND scope_ref=?
                    """,
                    (entry.scope.scope_kind.value, entry.scope.team_id, entry.scope.scope_ref),
                )
            await connection.commit()

    async def authorize() -> AuthorizedFence:
        return AuthorizedFence(
            resolve_context=_resolve_context(),
            vector=await store.read_auth_fence_vector(_resolve_context()),
        )

    result = await TerminalReplayHandoff(store, buses, _fault_injector=fault).connect(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        client_cursor=0,
        authorize=authorize,
    )
    assert revoked
    assert result.live
    assert result.promote_certificate is not None
    assert result.promote_certificate.subscriber_fence_token.auth_fence_vector_digest != old_vector.auth_fence_vector_digest
    assert result.subscription is not None
    await result.subscription.aclose()
    await writer.close()


@pytest.mark.asyncio
async def test_auth_revoke_committed_before_frame_gate_enqueues_zero_frames(db_path: str) -> None:
    _insert_runtime_fixture(db_path)
    store, _context = await _start_execution(db_path)
    resolve = _resolve_context()
    old_vector = await store.ensure_auth_scope_epochs(resolve)
    coordinator = TerminalAuthFenceCoordinator(store, _clock=lambda: _NOW)

    async def revoke() -> None:
        async with open_terminal_protocol_connection(db_path) as connection:
            await connection.execute("BEGIN IMMEDIATE")
            for entry in old_vector.entries:
                await connection.execute(
                    """
                    UPDATE terminal_auth_scope_epochs
                    SET epoch=epoch+1,row_version=row_version+1,updated_at='revoked'
                    WHERE scope_kind=? AND team_id=? AND scope_ref=?
                    """,
                    (entry.scope.scope_kind.value, entry.scope.team_id, entry.scope.scope_ref),
                )
            await connection.commit()

    await coordinator.run_auth_mutation(_auth_scopes(), revoke)
    payload = {"event_id": "evt_1", "turn_id": "t_1"}
    queue: asyncio.Queue[AuthorizedFrame] = asyncio.Queue(maxsize=1)
    closed = False

    def close() -> None:
        nonlocal closed
        closed = True

    certificate = await coordinator.authorize_and_enqueue_frame(
        AuthFenceFrameContext(
            scope=_scope(),
            subscriber_id="sub_1",
            frame_digest=canonical_digest_v1(payload),
            expected_auth_fence_vector=old_vector,
            expected_delivery_fence_epoch=0,
        ),
        resolve_context=resolve,
        frame_payload=payload,
        outbound_queue=queue,
        authorize=lambda _context: asyncio.sleep(0, result=True),
        close_subscriber=close,
    )
    assert certificate is None
    assert queue.empty()
    assert closed


@pytest.mark.asyncio
async def test_generation_transition_before_frame_linearization_enqueues_zero_frames(db_path: str) -> None:
    _insert_runtime_fixture(db_path)
    store, _context = await _start_execution(db_path)
    resolve = _resolve_context()
    vector = await store.ensure_auth_scope_epochs(resolve)
    transitioned = False

    async def transition(point: AuthFenceFaultPoint) -> None:
        nonlocal transitioned
        if point is not AuthFenceFaultPoint.AUTH_FENCE_BEFORE_FRAME_ENQUEUE or transitioned:
            return
        transitioned = True
        async with open_terminal_protocol_connection(db_path) as connection:
            await connection.execute("BEGIN IMMEDIATE")
            await connection.execute(
                """
                INSERT INTO turn_generations(
                  turn_id,generation,protocol_version,kind,prior_generation,
                  execution_intent_id,execution_intent_digest,opened_at,state
                ) VALUES ('t_1',2,1,'execution',1,'req_2','digest_2','t','opened')
                """
            )
            await connection.execute("UPDATE turns SET current_generation=2,generation_transition_epoch=2 WHERE id='t_1'")
            await connection.execute(
                """
                UPDATE sessions SET active_turn_generation=2,active_turn_claim_epoch=0
                WHERE id='s_1' AND active_turn_id='t_1'
                """
            )
            await connection.commit()

    coordinator = TerminalAuthFenceCoordinator(
        store,
        _clock=lambda: _NOW,
        _fault_injector=transition,
    )
    payload = {"event_id": "evt_1", "turn_id": "t_1"}
    queue: asyncio.Queue[AuthorizedFrame] = asyncio.Queue(maxsize=1)
    closed = False

    def close() -> None:
        nonlocal closed
        closed = True

    certificate = await coordinator.authorize_and_enqueue_frame(
        AuthFenceFrameContext(
            scope=_scope(),
            subscriber_id="sub_1",
            frame_digest=canonical_digest_v1(payload),
            expected_auth_fence_vector=vector,
            expected_delivery_fence_epoch=0,
        ),
        resolve_context=resolve,
        frame_payload=payload,
        outbound_queue=queue,
        authorize=lambda _context: asyncio.sleep(0, result=True),
        close_subscriber=close,
    )
    assert transitioned
    assert certificate is None
    assert queue.empty()
    assert closed


@pytest.mark.asyncio
async def test_transition_watch_capacity_merges_duplicate_tabs_by_principal_and_turn(db_path: str) -> None:
    _insert_runtime_fixture(db_path)
    store, _context = await _start_execution(db_path)
    with sqlite3.connect(db_path) as connection:
        for index in range(2, 101):
            connection.execute(
                """
                INSERT INTO turns(
                  id,session_id,seq,user_text,status,started_at,terminal_protocol_version,
                  terminal_ingress_surface,terminal_admission_cohort,protocol_admitted_at,
                  current_generation,generation_transition_epoch,event_sequence_next,
                  delivery_fence_epoch,presentation_locale,message_catalog_version
                ) VALUES (?, 's_1', ?, 'watch', 'running', 't', 1, 'web', 'bucket', 't', 1, 1, 1, 0, 'zh-CN', 1)
                """,
                (f"t_{index}", index),
            )
            connection.execute(
                """
                INSERT INTO turn_generations(
                  turn_id,generation,protocol_version,kind,execution_intent_id,
                  execution_intent_digest,opened_at,state
                ) VALUES (?,1,1,'execution',?,'digest','t','opened')
                """,
                (f"t_{index}", f"req_{index}"),
            )
        connection.commit()

    multiplexer = TransitionWatchMultiplexer(store)
    closed: list[str] = []
    changed: list[str] = []
    authorization_calls = 0

    async def authorize(_context: AuthFenceResolveContext) -> bool:
        nonlocal authorization_calls
        authorization_calls += 1
        return True

    for index in range(1, 101):
        turn_id = f"t_{index}"
        resolve = _resolve_context(resource_ref=turn_id)
        vector = await store.ensure_auth_scope_epochs(resolve)
        for tab in range(10):
            subscriber_id = f"sub_{index}_{tab}"
            multiplexer.register(
                TransitionWatchSubscription(
                    subscriber_id=subscriber_id,
                    read_context=TerminalReadContext(
                        team_id="team_1",
                        session_id="s_1",
                        turn_id=turn_id,
                        viewer="u_1",
                        auth_fence_vector_digest=vector.auth_fence_vector_digest,
                    ),
                    resolve_context=resolve,
                    expected_token=SubscriberFenceToken(1, 1, 0, vector.auth_fence_vector_digest),
                    authorize=authorize,
                    on_generation_changed=lambda _pointer, sid=subscriber_id: changed.append(sid),
                    close=lambda sid=subscriber_id: closed.append(sid),
                )
            )

    await multiplexer.poll_once()
    assert multiplexer.last_unique_turn_reads == 100
    assert multiplexer.last_authorization_checks == 100
    assert authorization_calls == 100
    assert closed == []
    assert changed == []


@pytest.mark.asyncio
async def test_transition_watch_run_fails_closed_on_poll_deadline(db_path: str) -> None:
    _insert_runtime_fixture(db_path)
    store, _context = await _start_execution(db_path)
    resolve = _resolve_context()
    vector = await store.ensure_auth_scope_epochs(resolve)
    closed: list[str] = []
    errors: list[BaseException] = []

    class SlowStore:
        async def read_generation_pointers(
            self,
            _contexts: tuple[TerminalReadContext, ...],
        ) -> dict[str, GenerationPointer]:
            await asyncio.sleep(1)
            return {}

    multiplexer = TransitionWatchMultiplexer(
        SlowStore(),  # type: ignore[arg-type]
        _jitter=lambda: -1.0,
        _on_poll_error=errors.append,
    )
    multiplexer.register(
        TransitionWatchSubscription(
            subscriber_id="sub_slow",
            read_context=TerminalReadContext(
                team_id="team_1",
                session_id="s_1",
                turn_id="t_1",
                viewer="u_1",
                auth_fence_vector_digest=vector.auth_fence_vector_digest,
            ),
            resolve_context=resolve,
            expected_token=SubscriberFenceToken(1, 1, 0, vector.auth_fence_vector_digest),
            authorize=lambda _resolve: asyncio.sleep(0, result=True),
            on_generation_changed=lambda _pointer: None,
            close=lambda: closed.append("sub_slow"),
        )
    )
    stop = asyncio.Event()
    task = asyncio.create_task(multiplexer.run(stop))
    for _attempt in range(100):
        if closed:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await task

    assert closed == ["sub_slow"]
    assert len(errors) == 1
    assert isinstance(errors[0], TimeoutError)


def test_subscriber_fence_token_rejects_legacy_three_tuple() -> None:
    vector = AuthFenceVector.build(
        (
            AuthFenceEpoch(
                AuthFenceScope(AuthFenceScopeKind.USER_ACCOUNT, "__global__", "principal_u1"),
                1,
            ),
        )
    )
    with pytest.raises(TypeError):
        SubscriberFenceToken(1, 1, 0)  # type: ignore[call-arg]
    assert SubscriberFenceToken(1, 1, 0, vector.auth_fence_vector_digest).pointer == GenerationPointer(1, 1, 0)


def test_auth_resolver_rejects_incomplete_fixed_scope_vector() -> None:
    with pytest.raises(ValueError, match="every fixed kind"):
        AuthFenceResolveContext(
            team_id="team_1",
            viewer="u_1",
            resource_ref="t_1",
            session_id="s_1",
            required_scopes=(
                AuthFenceScope(
                    AuthFenceScopeKind.USER_ACCOUNT,
                    "__global__",
                    "principal_u1",
                ),
            ),
        )
